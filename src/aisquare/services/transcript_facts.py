"""What the transcripts say about who steered: prompts, interrupts, denials, edits, cached.

The weekly outcomes report (``services.outcomes``) needs five facts no table records, each with
its moment, read off Claude Code's transcripts:

- **prompt**: a message that opened a turn (``core.usage._is_prompt``), classed as ``text``,
  ``wake`` (the fleet's nudge, :data:`~aisquare.services.fleet.NUDGE_TEXT`), ``notice`` (Claude
  Code's own ``<task-notification>``, ``core.prompt_origin.only_task_notifications``) or
  ``command`` (a slash command a person typed: harness blocks only, no notice). What decides the
  origin of a turn recorded before ``metric.origin`` existed.
- **continuation**: a Stop hook that kept the turn going (its feedback entry, ``isMeta``, opens
  with ``Stop hook feedback:``).
- **interrupt**: the person pressed Esc while it worked: ``[Request interrupted by user]``.
- **denial**: the person refused a tool at its permission prompt: the tool's result is ``The
  user doesn't want to proceed with this tool use``. (The ``[Request interrupted by user for tool
  use]`` that follows it is the same act, and is not counted again as an interrupt.)
- **edit**: an ``Edit``, ``Write``, ``MultiEdit`` or ``NotebookEdit`` call, with the file it
  named; a subagent's included.

**Read once.** A week of transcripts on a busy machine is over a gigabyte, so they are read
incrementally, as the usage sweep reads them: each file from the byte offset reached last time
(complete lines only), its facts appended to a cache, ``cache/transcript-facts.json``. A file that
shrank was rewritten, and is read again from its start. Only the lines that can hold a fact are
parsed: found by their marker in the raw bytes, never by splitting the file into lines.

The cache is a cache: deleted, it is rebuilt from the transcripts that remain (Claude Code keeps
them ``cleanupPeriodDays``, 30 by default). Facts older than :data:`KEEP` are dropped from it.
"""

from __future__ import annotations

import contextlib
import json
import multiprocessing
import os
import re
from collections.abc import Iterable, Iterator
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from aisquare.core import paths, prompt_origin
from aisquare.core import usage as usage_core

KEEP = timedelta(days=120)
"""How long a fact is kept in the cache after its moment."""

CACHE_VERSION = 2

PROMPT = "p"
CONTINUATION = "c"
INTERRUPT = "i"
DENIAL = "d"
EDIT = "e"

TEXT = "text"
WAKE = "wake"
NOTICE = "notice"
COMMAND = "command"
"""A slash command a person typed (``/model``, ``/clear``…): all harness blocks, a person's."""

NUDGE_TEXT = "aisquare: board update — read the delta above."
"""The one line the fleet's nudge types: ``services.fleet.NUDGE_TEXT``, said again here so that
reading a transcript does not import the fleet (a test holds the two equal)."""

_USER = b'"user"'
"""In every user entry (its ``type`` and its ``role``), in no assistant's: the quoted word, as
``core.usage`` finds its lines, whatever spacing the JSON was written with."""
_TOOL_RESULT = b'"tool_result"'
_DENIED = b"The user doesn't want to proceed with this tool use"
_EDIT_CALL = re.compile(rb'"(?:Edit|Write|MultiEdit|NotebookEdit)"')
_INTERRUPTED = "[Request interrupted by user]"
_STOP_FEEDBACK = "Stop hook feedback:"


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
        )
    return out


def _save_facts_cache(files: dict[str, _FactFile], now: datetime) -> None:
    """Write the cache: each file's offset and its facts of the last :data:`KEEP`. A file that
    is gone (Claude Code deleted it) is kept while it has a fact to keep, and then forgotten."""
    oldest = (now - KEEP).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    kept: dict[str, dict[str, Any]] = {}
    for name, entry in sorted(files.items()):
        facts = [fact for fact in entry.facts if fact[0] >= oldest]
        if facts or os.path.exists(name):
            kept[name] = {"offset": entry.offset, "facts": facts}
    payload = {"version": CACHE_VERSION, "files": kept}
    path = facts_cache_path()
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(f".{os.getpid()}.tmp")
        temp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        temp.replace(path)


def read_facts(
    transcripts: Iterable[Path], *, since: datetime, now: datetime | None = None
) -> dict[Path, list[Fact]]:
    """The facts of each of ``transcripts`` (a session's main file; its subagents' files are read
    with it), oldest first, the cache brought up to date first. A file the cache has not seen
    and that was last written before ``since`` is not read: it holds nothing from ``since`` on."""
    when = now or datetime.now(UTC)
    cache = _load_facts_cache()
    files: dict[Path, list[str]] = {}
    jobs: list[_FactJob] = []
    for transcript in transcripts:
        keys = files.setdefault(transcript, [])
        for order, path in enumerate(_fact_files(transcript)):
            key = str(path)
            keys.append(key)
            entry = cache.get(key)
            try:
                stat = path.stat()
            except OSError:
                continue
            if entry is None and stat.st_mtime < since.timestamp():
                continue
            offset = entry.offset if entry is not None else 0
            if stat.st_size != offset:
                todo = stat.st_size - offset if stat.st_size > offset else stat.st_size
                jobs.append(_FactJob(key, offset, order == 0, todo))
    for job, result in zip(jobs, _run_fact_jobs(jobs), strict=True):
        if result is None:
            continue
        start, offset, rows = result
        entry = cache.setdefault(job.path, _FactFile())
        if start == 0:
            entry.facts = rows
        else:
            entry.facts.extend(rows)
        entry.offset = offset
    if jobs:
        _save_facts_cache(cache, when)
    out: dict[Path, list[Fact]] = {}
    for transcript, keys in files.items():
        facts = [fact for key in keys if key in cache for fact in _fact_rows(cache[key].facts)]
        facts.sort(key=lambda fact: fact.at)
        out[transcript] = facts
    return out


def _fact_files(transcript: Path) -> list[Path]:
    """The session's transcript and its subagents' files, transcript first: what
    ``core.usage.transcript_files`` lists, by ``scandir`` (a thousand ``glob`` calls cost half a
    second, most of them of a folder that is not there)."""
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
    main: bool
    size: int
    """How many bytes it reads, about: the larger ones are handed out first."""


def _run_fact_jobs(jobs: list[_FactJob]) -> list[tuple[int, int, list[list[str]]] | None]:
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
            results: list[tuple[int, int, list[list[str]]] | None] = [None] * len(jobs)
            for index, result in zip(order, done, strict=True):
                results[index] = result
            return results
    return [_run_fact_job(job) for job in jobs]


def _run_fact_job(job: _FactJob) -> tuple[int, int, list[list[str]]] | None:
    """Read ``job``'s file from its offset: ``(where it started, the offset past its last
    complete line, the facts read)``, or ``None`` when it cannot be read. A file shorter than the
    offset was rewritten, and is read from its start (``0``: its facts replace the cached)."""
    path = Path(job.path)
    try:
        size = path.stat().st_size
        start = job.offset if job.offset <= size else 0
        with path.open("rb") as handle:
            handle.seek(start)
            data = handle.read()
    except OSError:
        return None
    end = data.rfind(b"\n") + 1  # a last line still being written is read next time
    if end != len(data):
        data = data[:end]
    return start, start + end, scan_fact_bytes(data, main=job.main)


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


def scan_fact_bytes(data: bytes, *, main: bool) -> list[list[str]]:
    """The facts in ``data`` (complete JSONL lines), as ``[timestamp, kind, detail]`` rows.
    ``main``: the session's own file, whose user entries are its prompts; a subagent's file adds
    its edits and denials only."""
    found: list[tuple[int, list[str]]] = []
    if main:
        for position, line in _lines_holding(data, _USER, skip=_TOOL_RESULT):
            fact = _user_fact(line)
            if fact is not None:
                found.append((position, fact))
    for position, line in _lines_holding(data, _DENIED):
        found.extend((position, fact) for fact in _denial_facts(line))
    for position, line in _lines_matching_call(data, _EDIT_CALL):
        found.extend((position, fact) for fact in _edit_facts(line))
    found.sort(key=lambda item: item[0])
    return [fact for _, fact in found]


def _lines_holding(
    data: bytes, marker: bytes, *, skip: bytes | None = None
) -> Iterator[tuple[int, bytes]]:
    """Each line of ``data`` holding ``marker`` (and not ``skip``), once, with where it starts."""
    position = data.find(marker)
    while position != -1:
        start = data.rfind(b"\n", 0, position) + 1
        end = data.find(b"\n", position)
        if end == -1:
            end = len(data)
        if skip is None or data.find(skip, start, end) == -1:
            yield start, data[start:end]
        position = data.find(marker, end)


def _lines_matching_call(data: bytes, pattern: re.Pattern[bytes]) -> Iterator[tuple[int, bytes]]:
    position = 0
    while (match := pattern.search(data, position)) is not None:
        # `span`, not `start()` and `end()`: the call-graph guards resolve a call by its bare
        # name, and both name hook-unsafe functions elsewhere (tests/test_config_writes_…).
        found_at, found_end = match.span()
        start = data.rfind(b"\n", 0, found_at) + 1
        end = data.find(b"\n", found_end)
        if end == -1:
            end = len(data)
        yield start, data[start:end]
        position = end


def _fact_entry(line: bytes) -> dict[str, Any] | None:
    try:
        entry = json.loads(line)
    except ValueError:
        return None
    return entry if isinstance(entry, dict) else None


def _fact_stamp(entry: dict[str, Any]) -> str | None:
    stamp = entry.get("timestamp")
    return stamp if isinstance(stamp, str) and stamp else None


def _user_words(entry: dict[str, Any]) -> str | None:
    """A user entry's words: its string content, or its text blocks joined."""
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(parts) if parts else None
    return None


def _user_fact(line: bytes) -> list[str] | None:
    entry = _fact_entry(line)
    if entry is None or entry.get("type") != "user" or entry.get("isSidechain"):
        return None
    stamp = _fact_stamp(entry)
    text = _user_words(entry)
    if stamp is None or text is None:
        return None
    if entry.get("isMeta"):
        return [stamp, CONTINUATION, ""] if text.startswith(_STOP_FEEDBACK) else None
    if text.strip() == _INTERRUPTED:
        return [stamp, INTERRUPT, ""]
    if text.startswith("[Request interrupted by user"):
        return None  # "… for tool use": the denial beside it is the fact
    if not usage_core._is_prompt(entry):
        return None
    if " ".join(text.split()) == NUDGE_TEXT:
        return [stamp, PROMPT, WAKE]
    if prompt_origin.only_task_notifications(text):
        return [stamp, PROMPT, NOTICE]
    if prompt_origin.only_harness_blocks(text):
        return [stamp, PROMPT, COMMAND]
    return [stamp, PROMPT, TEXT]


def _denial_facts(line: bytes) -> list[list[str]]:
    entry = _fact_entry(line)
    if entry is None or entry.get("type") != "user":
        return []
    stamp = _fact_stamp(entry)
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if stamp is None or not isinstance(content, list):
        return []
    return [
        [stamp, DENIAL, ""]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "tool_result"
        and usage_core._result_text(block.get("content")).startswith(_DENIED.decode())
    ]


def _edit_facts(line: bytes) -> list[list[str]]:
    entry = _fact_entry(line)
    if entry is None or entry.get("type") != "assistant":
        return []
    stamp = _fact_stamp(entry)
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if stamp is None or not isinstance(content, list):
        return []
    out: list[list[str]] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        if block.get("name") not in usage_core.EDIT_TOOLS:
            continue
        tool_input = block.get("input")
        target = None
        if isinstance(tool_input, dict):
            target = tool_input.get("file_path") or tool_input.get("notebook_path")
        if isinstance(target, str) and target:
            out.append([stamp, EDIT, target])
    return out
