"""What the transcripts say about who steered: prompts, interrupts, denials, edits, cached.

The weekly outcomes report (``services.outcomes``) needs five facts no table records, each with
its moment, read off each session's transcript by its agent's own reader
(``AgentTranscript.facts``, ``core.agent_adapters.types``):

- **prompt**: a message that opened a turn, classed as ``text``, ``wake`` (the fleet's nudge,
  :data:`~aisquare.services.fleet.NUDGE_TEXT`), ``notice`` (Claude Code's own
  ``<task-notification>``) or ``command`` (a slash command a person typed). What decides the
  origin of a turn recorded before ``metric.origin`` existed.
- **continuation**: a Stop hook that kept the turn going (Claude Code's ``Stop hook feedback:``
  entry, Codex's ``<hook_prompt hook_run_id="stop:…">`` message).
- **interrupt**: the person pressed Esc while it worked: Claude Code's ``[Request interrupted by
  user]``, Codex's ``turn_aborted`` (``interrupted``).
- **denial**: the person refused a tool at its permission prompt: Claude Code's ``The user
  doesn't want to proceed with this tool use``, Codex's call stopped at its approval. (The
  interruption that refusal makes is the same act, and is not counted again as an interrupt.)
- **edit**: a file an edit named: Claude Code's ``Edit``, ``Write``, ``MultiEdit`` or
  ``NotebookEdit`` (a subagent's included), Codex's ``apply_patch``.

The reader is the one of the agent the session's rows recorded (``agents``), else of the agent
whose transcript roots hold the file (``claude_accounts.transcript_home``), else the default
agent's: a transcript from before adapters. An agent with no transcript reader gives none.

**Read once.** A week of transcripts on a busy machine is over a gigabyte, so they are read
incrementally, as the usage sweep reads them: each file from the byte offset its reader reached
last time (complete lines only), its facts appended to a cache, ``cache/transcript-facts.json``,
beside the agent whose reader read it (a file read by another agent's reader is read again from
its start). A file that shrank was rewritten, and is read again from its start.

The cache is a cache: deleted, it is rebuilt from the transcripts that remain (Claude Code keeps
them ``cleanupPeriodDays``, 30 by default). Facts older than :data:`KEEP` are dropped from it.
"""

from __future__ import annotations

import contextlib
import json
import multiprocessing
import os
from collections.abc import Iterable, Iterator, Mapping
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from aisquare.core import paths, prompt_origin
from aisquare.core.agent_adapters import types as adapter_types

KEEP = timedelta(days=120)
"""How long a fact is kept in the cache after its moment."""

CACHE_VERSION = 2

PROMPT = adapter_types.FACT_PROMPT
CONTINUATION = adapter_types.FACT_CONTINUATION
INTERRUPT = adapter_types.FACT_INTERRUPT
DENIAL = adapter_types.FACT_DENIAL
EDIT = adapter_types.FACT_EDIT

TEXT = "text"
WAKE = "wake"
NOTICE = "notice"
COMMAND = "command"
"""A slash command a person typed (``/model``, ``/clear``…): all harness blocks, a person's."""

NUDGE_TEXT = prompt_origin.WAKE_TEXT
"""The one line the fleet's nudge types (``services.fleet.NUDGE_TEXT``)."""


@dataclass(frozen=True)
class Fact:
    """One fact: when (UTC), what (:data:`PROMPT` …), and its detail (a prompt's class, an
    edit's file)."""

    at: datetime
    kind: str
    detail: str = ""


@dataclass
class _FactFile:
    offset: int = 0
    facts: list[list[str]] = field(default_factory=list)
    """``[timestamp, kind, detail]``, as the cache holds them."""
    agent: str = ""
    """The agent whose reader read it; ``""`` in a cache from before the slot, whose facts
    the default agent's reader read."""


def facts_cache_path() -> Path:
    return paths.cache_dir() / "transcript-facts.json"


def _load_facts_cache() -> dict[str, _FactFile]:
    try:
        data = json.loads(facts_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        return {}
    files = data.get("files")
    if not isinstance(files, dict):
        return {}
    out: dict[str, _FactFile] = {}
    for name, entry in files.items():
        if not isinstance(entry, dict):
            continue
        facts = entry.get("facts")
        out[str(name)] = _FactFile(
            offset=int(entry.get("offset", 0) or 0),
            facts=[
                list(map(str, fact)) for fact in facts if isinstance(fact, list) and len(fact) == 3
            ]
            if isinstance(facts, list)
            else [],
            agent=str(entry.get("agent") or ""),
        )
    return out


def _save_facts_cache(files: dict[str, _FactFile], now: datetime) -> None:
    """Write the cache: each file's offset, its reader's agent and its facts of the last
    :data:`KEEP`. A file that is gone (its agent deleted it) is kept while it has a fact to
    keep, and then forgotten."""
    oldest = (now - KEEP).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    kept: dict[str, dict[str, Any]] = {}
    for name, entry in sorted(files.items()):
        facts = [fact for fact in entry.facts if fact[0] >= oldest]
        if facts or os.path.exists(name):
            kept[name] = {"offset": entry.offset, "facts": facts}
            if entry.agent:
                kept[name]["agent"] = entry.agent
    payload = {"version": CACHE_VERSION, "files": kept}
    path = facts_cache_path()
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(f".{os.getpid()}.tmp")
        temp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        temp.replace(path)


def read_facts(
    transcripts: Iterable[Path],
    *,
    since: datetime,
    now: datetime | None = None,
    agents: Mapping[Path, str | None] | None = None,
) -> dict[Path, list[Fact]]:
    """The facts of each of ``transcripts`` (a session's main file; its subagents' files are read
    with it), oldest first, the cache brought up to date first, each read by the reader of its
    agent (``agents``: the agent its session recorded, ``None`` or missing where none did). A
    file the cache has not seen and that was last written before ``since`` is not read: it holds
    nothing from ``since`` on."""
    when = now or datetime.now(UTC)
    cache = _load_facts_cache()
    recorded = agents or {}
    unread = _default_reader()
    files: dict[Path, list[str]] = {}
    jobs: list[_FactJob] = []
    for transcript in transcripts:
        keys = files.setdefault(transcript, [])
        agent = _reader_agent(transcript, recorded.get(transcript))
        if agent is None:
            continue  # its agent reads no transcript: it says nothing of who steered
        for path in _fact_files(transcript):
            key = str(path)
            keys.append(key)
            entry = cache.get(key)
            stale = entry is not None and (entry.agent or unread) != agent
            if stale:
                # Read by another agent's reader (a rollout main's scan read): its own reads
                # it again, from its start.
                cache.pop(key)
                entry = None
            try:
                stat = path.stat()
            except OSError:
                continue
            if entry is None and stat.st_mtime < since.timestamp():
                continue
            offset = entry.offset if entry is not None else 0
            if stale or stat.st_size != offset:
                todo = stat.st_size - offset if stat.st_size > offset else stat.st_size
                jobs.append(_FactJob(key, offset, agent, todo))
    for job, result in zip(jobs, _run_fact_jobs(jobs), strict=True):
        if result is None:
            continue
        rows, offset = result
        entry = cache.get(job.path)
        if entry is None or job.offset == 0 or offset < job.offset:
            # From its start: read for the first time, or rewritten (read again from it).
            cache[job.path] = _FactFile(offset, rows, job.agent)
        else:
            entry.facts.extend(rows)
            entry.offset, entry.agent = offset, job.agent
    if jobs:
        _save_facts_cache(cache, when)
    out: dict[Path, list[Fact]] = {}
    for transcript, keys in files.items():
        facts = [fact for key in keys if key in cache for fact in _fact_rows(cache[key].facts)]
        facts.sort(key=lambda fact: fact.at)
        out[transcript] = facts
    return out


def _reader_agent(transcript: Path, recorded: str | None) -> str | None:
    """The agent whose transcript reader reads ``transcript``: the one its session recorded,
    else the one whose transcript roots hold it, else the default agent (a transcript from
    before adapters, as ``core.agent_adapters.recorded_adapter`` reads a missing agent).
    ``None`` for one with no reader."""
    from aisquare.core import claude_accounts as accounts_core
    from aisquare.core.agent_adapters import recorded_adapter

    adapter = recorded_adapter(recorded) if recorded else None
    if adapter is None:
        home = accounts_core.transcript_home(transcript)
        adapter = recorded_adapter(home[0] if home is not None else None)
    if adapter is None or adapter.transcript_reader is None:
        return None
    return adapter.id


def _default_reader() -> str:
    """The agent whose reader read a cache entry that names none (one from before the slot):
    the default agent's, whose reader main's scan was."""
    from aisquare.core.agent_adapters import recorded_adapter

    adapter = recorded_adapter(None)
    return adapter.id if adapter is not None else ""


def _fact_files(transcript: Path) -> list[Path]:
    """The session's transcript and its subagents' files, transcript first: those under
    ``<session>/subagents/``, where Claude Code keeps them (``core.usage.transcript_files``;
    an agent that keeps none there reads its one file), by ``scandir`` (a thousand ``glob``
    calls cost half a second, most of them of a folder that is not there)."""
    files = [transcript]
    with contextlib.suppress(OSError):
        folder = transcript.with_suffix("") / "subagents"
        files.extend(
            sorted(
                Path(entry.path) for entry in os.scandir(folder) if entry.name.endswith(".jsonl")
            )
        )
    return files


PARALLEL_BYTES = 192 * 1024 * 1024
"""Past this much to read, the files are read by a pool of processes (one a core, at most
:data:`MAX_WORKERS`): the first report on a busy machine reads a week or two of transcripts, a
gigabyte or more, once. Below it, and wherever a pool cannot start, they are read here."""
MAX_WORKERS = 8


@dataclass(frozen=True)
class _FactJob:
    path: str
    offset: int
    agent: str
    """The agent whose reader reads it (``AgentAdapter.id``)."""
    size: int
    """How many bytes it reads, about: the larger ones are handed out first."""


_JobResult = tuple[list[list[str]], int]


def _run_fact_jobs(jobs: list[_FactJob]) -> list[_JobResult | None]:
    workers = min(MAX_WORKERS, os.cpu_count() or 1, len(jobs))
    if workers > 1 and sum(job.size for job in jobs) >= PARALLEL_BYTES:
        order = sorted(range(len(jobs)), key=lambda index: -jobs[index].size)
        try:
            # Spawned, never forked: the report reads git and gh on threads meanwhile, and a fork
            # of a process with threads may copy a lock another thread holds.
            context = multiprocessing.get_context("spawn")
            with ProcessPoolExecutor(max_workers=workers, mp_context=context) as pool:
                done = list(pool.map(_run_fact_job, [jobs[index] for index in order]))
        except Exception:  # no pool here (a sandbox, a frozen build): read them in this process
            pass
        else:
            results: list[_JobResult | None] = [None] * len(jobs)
            for index, result in zip(order, done, strict=True):
                # A worker knows the agents registered at import; one registered here alone
                # (a plugin's, at run time) is read here.
                results[index] = result if result is not None else _run_fact_job(jobs[index])
            return results
    return [_run_fact_job(job) for job in jobs]


def _run_fact_job(job: _FactJob) -> _JobResult | None:
    """Read ``job``'s file from its offset by its agent's reader (``AgentTranscript.facts``):
    ``(the facts read, the offset to read on from)``, an offset below the job's when the file
    was rewritten and read from its start; ``None`` when no reader of that agent is here."""
    from aisquare.core.agent_adapters import recorded_adapter

    adapter = recorded_adapter(job.agent)
    reader = adapter.transcript_reader if adapter is not None else None
    if reader is None:
        return None
    facts, offset = reader.facts(Path(job.path), job.offset)
    return [[fact.at, fact.kind, fact.detail] for fact in facts], offset


def _fact_rows(rows: list[list[str]]) -> Iterator[Fact]:
    for stamp, kind, detail in rows:
        moment = _fact_moment(stamp)
        if moment is not None:
            yield Fact(moment, kind, detail)


def _fact_moment(stamp: str) -> datetime | None:
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None
