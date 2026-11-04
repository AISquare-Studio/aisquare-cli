"""Who started a turn: a person at the keyboard, or aisquare on an agent's behalf.

Recorded on the turn's ``metric`` row (``origin``) at its ``UserPromptSubmit``, as
``<by>:<kind>``. ``by`` is ``human`` or ``agent``; ``kind`` says how the prompt came:

- ``typed``: typed at the agent's prompt by a person. What a prompt is when nothing below
  claimed it: no aisquare write left a marker for its text.
- ``tell``: a ``fleet tell`` (or the phone's) typed into a waiting agent.
- ``spawn``: a fleet spawn's first message, on the command line or typed.
- ``wake``: the nudge that wakes a manager for board news (``aisquare watch``, a teammate's
  board write).
- ``upkeep``: the quiet compaction's ``/compact`` and its resume line.
- ``continuation``: a Stop hook that kept the turn going with board news; written by the
  Stop hook itself (``services.hooks.turn_stopped``), never through a marker.
- ``notice``: Claude Code's own notice that starts a turn (a background task finished): a
  prompt that is nothing but ``<task-notification>`` blocks. A slash command a person types is
  all harness blocks too, and is ``typed``.

**The marker.** Every aisquare write into an agent's prompt (``services.delivery``) leaves a
file of its own under ``run/origin`` first, named for a digest of the text (whitespace folded,
as the input box folds it) and a nonce, holding the kind and who sent it. The
``UserPromptSubmit`` it starts takes the oldest live file whose digest is its prompt's
(:func:`take_origin`), so the origin is the one of the text that was submitted, not a guess from
timing, and two writes of the same text waiting at once (two wakes) keep one each. A marker
nobody takes (the write was refused, the agent died) is ignored after :data:`MAX_AGE` and
removed by the next :func:`mark_origin`. A text a busy agent's own queue holds
(``AgentAdapter.queue_args``) is submitted only once the turn before it ends, however long that
runs, so its marker (``queued``) waits past :data:`MAX_AGE` for as long as the agent whose queue
holds it runs (its ``holder``), up to :data:`QUEUED_MAX_AGE`. With no marker, the fleet's nudge
(:data:`WAKE_TEXT`) is still a wake.

**Who authored it.** ``wake``, ``upkeep``, ``continuation`` and ``notice`` are ``agent`` by kind
(:data:`AGENT_KINDS`): no person writes them, whatever process sends them. A ``tell`` and a
``spawn`` are their sender's: ``agent`` when the write runs inside an agent (one that marks
every process it starts, its hooks included, by ``AgentCapabilities.process_env``: Claude
Code's ``CLAUDECODE``; another's launch or tool-shell mark, ``core.spawn.run_agents``; a fleet
window carries ``AISQUARE_FLEET_AGENT``), ``human`` otherwise (``asq``, a shell, the phone's
server: :func:`sent_by`). So a ``fleet tell`` typed in a terminal is ``human:tell``, and the
manager's is ``agent:tell``; a spawn is ``human`` when a person ran it and ``agent`` when an
agent session did, its ``--as`` session included (``services.fleet.spawn``). A tell a busy
agent's own queue takes (``AgentAdapter.queue_args``) leaves its marker as a typed one does,
one that waits for the turn the queue starts (``services.fleet._queue_message``).

Nothing here raises: a marker that cannot be written or read costs the record (the turn reads
``human:typed``), never the write or the turn.
"""

from __future__ import annotations

import contextlib
import hashlib
import itertools
import json
import os
import re
import time
from collections.abc import Iterator
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aisquare.core import paths, procs

HUMAN = "human"
AGENT = "agent"

TYPED = "typed"
TELL = "tell"
SPAWN = "spawn"
WAKE = "wake"
UPKEEP = "upkeep"
CONTINUATION = "continuation"
NOTICE = "notice"

KINDS = (TYPED, TELL, SPAWN, WAKE, UPKEEP, CONTINUATION, NOTICE)
"""Every kind, in the order a report lists them."""
AGENT_KINDS = frozenset({WAKE, UPKEEP, CONTINUATION, NOTICE})
"""The kinds no person writes: aisquare or Claude Code composes them, so they are ``agent``
whichever process sends them (the ``aisquare watch`` daemon, ``asq``, a person's ``task done``
that wakes the manager). A ``tell`` and a ``spawn`` are their sender's: the instruction is
whoever ran the command's."""

AUTHORS = frozenset({HUMAN, AGENT})
"""Who may author a turn: a person, or an agent (whichever agent: none is named here)."""

HUMAN_TYPED = f"{HUMAN}:{TYPED}"
AGENT_CONTINUATION = f"{AGENT}:{CONTINUATION}"
AGENT_NOTICE = f"{AGENT}:{NOTICE}"

WAKE_TEXT = "aisquare: board update — read the delta above."
"""The one line the fleet's nudge types (``services.fleet.NUDGE_TEXT``, held equal by a test):
said here so that the hook and the transcript scan read it without importing the fleet."""

MAX_AGE = timedelta(minutes=30)
"""How long a marker waits for its prompt: a spawn's first message is submitted once Claude Code
has started (seconds; a trust dialog may hold it longer), a typed one within its minute of
re-pressed Enters."""

QUEUED_MAX_AGE = timedelta(hours=24)
"""How long, at most, the marker of a text a busy agent's own queue holds (``AgentAdapter.
queue_args``; :func:`mark_origin`'s ``queued``) waits for the turn the queue starts with it. The
queue gives it to the next turn once the current one ends, and a Codex turn may run for hours,
so past :data:`MAX_AGE` the marker waits for as long as the process whose queue holds it runs
(its ``holder``, the agent's pane's: ``services.fleet._queue_message``). Once that process is
gone the queue went with it, and the marker is removed. The bound is for a holder that cannot
be told gone: none was known, or another process took its pid."""

HARNESS_BLOCK = re.compile(
    r"<(task-notification|local-command-caveat|command-name|command-message|command-args"
    r"|system-reminder)>.*?</\1>",
    re.DOTALL,
)
"""A closed tag Claude Code itself writes into the prompt field: a background task's notice,
a slash command's caveat or its name/message/args, a hook's reminder. Matched only when
CLOSED — ``<command-name> is the tag the parser misses`` has no ``</command-name>`` and is a
person's words, as typed, not this. (It lived in ``services.hooks``, which still strips it from
a recorded prompt.)"""

_FLEET_AGENT_ENV = "AISQUARE_FLEET_AGENT"
_SENDER: ContextVar[str | None] = ContextVar("prompt_origin_sender", default=None)
_WRITES = itertools.count()


def compose_origin(by: str, kind: str) -> str:
    """``by:kind``, as the ``origin`` column holds it."""
    return f"{by}:{kind}"


def split_origin(origin: str) -> tuple[str, str]:
    """``(by, kind)`` of a recorded origin; an origin of another shape is a human's typing."""
    by, _, kind = origin.partition(":")
    if by not in AUTHORS or not kind:
        return HUMAN, TYPED
    return by, kind


def authored(origin: str) -> str:
    """``origin`` with ``by`` set by who authored it: one of :data:`AGENT_KINDS` is ``agent``'s
    whatever a sender wrote down (a build that took the sender's word recorded a daemon's wake as
    ``human:wake``)."""
    by, kind = split_origin(origin)
    return compose_origin(AGENT, kind) if kind in AGENT_KINDS else compose_origin(by, kind)


def origin_markers_dir() -> Path:
    """Where markers wait for their prompt: ``$AISQUARE_HOME/run/origin``."""
    return paths.aisquare_home() / "run" / "origin"


def origin_digest(text: str) -> str:
    """The marker's name for ``text``: whitespace folded, so a paste the input box re-wrapped is
    the same text."""
    return hashlib.sha256(" ".join(text.split()).encode()).hexdigest()[:32]


def origin_sender() -> str:
    """Who is sending from this process: :func:`sent_by`'s word when one is in force, else
    ``agent`` inside an agent (a fleet window's ``AISQUARE_FLEET_AGENT``, the mark an agent
    sets in every process it starts, :func:`_agent_marks`, or any agent's own mark of its
    launch or its tools' shells: ``core.spawn.run_agents``, Codex's ``CODEX_THREAD_ID`` among
    them), else ``human``."""
    pinned = _SENDER.get()
    if pinned is not None:
        return pinned
    if any(os.environ.get(name, "").strip() for name in (_FLEET_AGENT_ENV, *_agent_marks())):
        return AGENT
    from aisquare.core.spawn import run_agents  # lazy: the spawn module imports far more

    return AGENT if run_agents() else HUMAN


def _agent_marks() -> tuple[str, ...]:
    """Every registered agent's ``AgentCapabilities.process_env``: the variable it sets in
    each process it starts, its hooks included (Claude Code's ``CLAUDECODE``)."""
    from aisquare.core.agent_adapters import adapters  # lazy, as run_agents is

    return tuple(
        adapter.capabilities.process_env
        for adapter in adapters()
        if adapter.capabilities.process_env
    )


@contextlib.contextmanager
def sent_by(by: str) -> Iterator[None]:
    """Every marker written inside says ``by``: the phone's server sends for the person holding
    the phone, whatever process started it."""
    token = _SENDER.set(by)
    try:
        yield
    finally:
        _SENDER.reset(token)


def mark_origin(
    text: str,
    kind: str,
    *,
    by: str | None = None,
    now: datetime | None = None,
    queued: bool = False,
    holder: int | None = None,
) -> Path | None:
    """Say that ``text``, about to reach an agent's prompt, is a ``kind`` sent by ``by`` (the
    process's :func:`origin_sender` when not given; always ``agent`` for one of
    :data:`AGENT_KINDS`). One marker per write, ``<digest>.<nonce>.json``: two writes of the same
    text waiting at once (two wakes, from the watcher and a teammate's ``task done``) keep an
    origin each. The marker's path, for :func:`unmark_origin`; ``None`` when none was written.
    Stale markers are removed on the way.

    ``queued``: the text waits in a busy agent's own queue (``AgentAdapter.queue_args``) for the
    turn it is running to end, and its marker waits as long, past :data:`MAX_AGE`, while
    process ``holder`` (the agent's, whose queue holds it) runs, up to :data:`QUEUED_MAX_AGE`
    (``None``: not known, the bound alone)."""
    if not text.strip():
        return None
    by = AGENT if kind in AGENT_KINDS else by or origin_sender()
    when = now or _now()
    folder = origin_markers_dir()
    marker: dict[str, object] = {"kind": kind, "by": by, "at": when.isoformat()}
    if queued:
        marker.update(queued=True, holder=holder)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        _forget_stale_markers(folder, when)
        # Nanoseconds first, so the names of one text sort oldest first; the pid and a counter
        # keep two writes in one nanosecond, or two processes, apart.
        nonce = f"{time.time_ns():020d}-{os.getpid()}-{next(_WRITES)}"
        path = folder / f"{origin_digest(text)}.{nonce}.json"
        temp = folder / f".{path.name}.tmp"
        temp.write_text(json.dumps(marker), encoding="utf-8")
        temp.replace(path)
    except OSError:
        return None
    return path


def unmark_origin(marker: Path | None) -> None:
    """Withdraw the marker a write left (:func:`mark_origin`'s path): it did not reach the
    prompt after all. Another write's marker for the same text is left alone."""
    if marker is not None:
        with contextlib.suppress(OSError):
            marker.unlink()


def take_origin(prompt: str | None, *, now: datetime | None = None) -> str | None:
    """The origin the oldest live marker for ``prompt`` left, that marker gone (claimed by a
    rename, so two prompts never take the same one); ``None`` when there is none. A marker that
    no longer waits (:func:`_waits`: older than :data:`MAX_AGE`, unless a queue still holds its
    text) is removed and passed over."""
    if not prompt or not prompt.strip():
        return None
    folder = origin_markers_dir()
    digest = origin_digest(prompt)
    try:
        names = sorted(
            entry.name
            for entry in os.scandir(folder)
            if entry.name.startswith(f"{digest}.") and entry.name.endswith(".json")
        )
    except OSError:
        return None
    when = now or _now()
    for name in names:
        claimed = folder / f".{name}.{os.getpid()}.taken"
        try:
            (folder / name).rename(claimed)  # one taker wins; the others try the next one
            raw = claimed.read_text(encoding="utf-8")
        except OSError:
            continue
        finally:
            with contextlib.suppress(OSError):
                claimed.unlink()
        try:
            data = json.loads(raw)
            at = datetime.fromisoformat(str(data["at"]))
            by, kind = str(data["by"]), str(data["kind"])
        except (ValueError, KeyError, TypeError):
            continue
        if not _waits(data, at, when) or by not in AUTHORS or not kind:
            continue
        return compose_origin(AGENT if kind in AGENT_KINDS else by, kind)
    return None


def only_harness_blocks(prompt: str | None) -> bool:
    """Whether ``prompt`` is nothing but :data:`HARNESS_BLOCK` s: a slash command a person typed
    (its name, message and args), or Claude Code's notice (:func:`only_task_notifications`)."""
    return (
        prompt is not None
        and HARNESS_BLOCK.search(prompt) is not None
        and not HARNESS_BLOCK.sub("", prompt).strip()
    )


def only_task_notifications(prompt: str | None) -> bool:
    """Whether ``prompt`` is Claude Code's own notice that a background task finished: a
    ``<task-notification>`` and nothing a person wrote. A slash command is all harness blocks too
    (``/model``, ``/clear``…), but a person typed it."""
    return (
        only_harness_blocks(prompt)
        and prompt is not None
        and "<task-notification>" in prompt
        and all(m.group(1) != "command-name" for m in HARNESS_BLOCK.finditer(prompt))
    )


def origin_of_prompt(
    prompt: str | None, *, notice: bool = False, now: datetime | None = None
) -> str:
    """The origin of the turn ``prompt`` starts: its marker's, else ``agent:notice`` for Claude
    Code's own notice (``notice``, the caller's reading: :func:`only_task_notifications`), else
    ``agent:wake`` for the fleet's nudge (:data:`WAKE_TEXT`), else a person typed it."""
    marked = take_origin(prompt, now=now)
    if marked is not None:
        return marked
    if notice:
        return AGENT_NOTICE
    if prompt is not None and " ".join(prompt.split()) == WAKE_TEXT:
        # The fleet's nudge, its marker taken by another wake or gone stale: no person types
        # it, and the report's inference reads it so too.
        return compose_origin(AGENT, WAKE)
    return HUMAN_TYPED


def _now() -> datetime:
    """The clock markers are aged by (a test's fake one)."""
    return datetime.now(tz=UTC)


def _waits(data: dict[str, object], at: datetime, when: datetime) -> bool:
    """Whether the marker ``data``, written at ``at``, still waits for its prompt at ``when``:
    every marker within :data:`MAX_AGE`; past it, one a busy agent's queue holds the text of
    (``queued``), while its ``holder`` runs (or is not known) and within
    :data:`QUEUED_MAX_AGE`."""
    try:
        age = when - at
    except TypeError:  # a time with no zone, which no marker of ours holds
        return False
    if age <= MAX_AGE:
        return True
    if data.get("queued") is not True or age > QUEUED_MAX_AGE:
        return False
    holder = data.get("holder")
    if isinstance(holder, bool) or not isinstance(holder, int):
        return holder is None
    return procs.alive(holder)


def _forget_stale_markers(folder: Path, when: datetime) -> None:
    """Remove what no longer waits at ``when``: a file older than :data:`MAX_AGE` (by its
    modification time), unless it is a marker that still waits (:func:`_waits`: a queue holds
    its text)."""
    cutoff = when.timestamp() - MAX_AGE.total_seconds()
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return
    for entry in entries:
        with contextlib.suppress(OSError):
            if entry.stat().st_mtime < cutoff and not _still_waits(entry, when):
                os.unlink(entry.path)


def _still_waits(entry: os.DirEntry[str], when: datetime) -> bool:
    """Whether the stale-looking file ``entry`` is a marker that still waits (:func:`_waits`);
    a claimed or half-written one, or one that cannot be read, does not."""
    if entry.name.startswith(".") or not entry.name.endswith(".json"):
        return False
    try:
        data = json.loads(Path(entry.path).read_text(encoding="utf-8"))
        at = datetime.fromisoformat(str(data["at"]))
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return _waits(data, at, when)
