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

**The marker.** Every aisquare write into an agent's prompt (``services.delivery``) leaves one
file under ``run/origin`` first, named for a digest of the text (whitespace folded, as the input box
folds it), holding the kind and who sent it. The ``UserPromptSubmit`` it starts takes the file whose
digest is its prompt's (:func:`take_origin`), so the origin is the one of the text that was
submitted, not a guess from timing. A marker nobody takes (the write was refused, the agent died) is
ignored after :data:`MAX_AGE` and removed by the next :func:`mark_origin`.

**Who authored it.** ``wake``, ``upkeep``, ``continuation`` and ``notice`` are ``agent`` by kind
(:data:`AGENT_KINDS`): no person writes them, whatever process sends them. A ``tell`` and a
``spawn`` are their sender's: ``agent`` when the write runs inside an agent (Claude Code exports
``CLAUDECODE`` to its tools; a fleet window carries ``AISQUARE_FLEET_AGENT``), ``human``
otherwise (``asq``, a shell, the phone's server: :func:`sent_by`). So a ``fleet tell`` typed in
a terminal is ``human:tell``, and the manager's is ``agent:tell``; a spawn is ``human`` when a
person ran it and ``agent`` when an agent session did, its ``--as`` session included
(``services.fleet.spawn``).

Nothing here raises: a marker that cannot be written or read costs the record (the turn reads
``human:typed``), never the write or the turn.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import time
from collections.abc import Iterator
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aisquare.core import paths

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

HUMAN_TYPED = f"{HUMAN}:{TYPED}"
AGENT_CONTINUATION = f"{AGENT}:{CONTINUATION}"
AGENT_NOTICE = f"{AGENT}:{NOTICE}"

MAX_AGE = timedelta(minutes=30)
"""How long a marker waits for its prompt: a spawn's first message is submitted once Claude Code
has started (seconds; a trust dialog may hold it longer), a typed one within its minute of
re-pressed Enters."""

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

_ENV_AGENT = ("CLAUDECODE", "AISQUARE_FLEET_AGENT")
_SENDER: ContextVar[str | None] = ContextVar("prompt_origin_sender", default=None)


def compose_origin(by: str, kind: str) -> str:
    """``by:kind``, as the ``origin`` column holds it."""
    return f"{by}:{kind}"


def split_origin(origin: str) -> tuple[str, str]:
    """``(by, kind)`` of a recorded origin; an origin of another shape is a human's typing."""
    by, _, kind = origin.partition(":")
    if by not in (HUMAN, AGENT) or not kind:
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
    ``agent`` inside an agent (``CLAUDECODE``, ``AISQUARE_FLEET_AGENT``), else ``human``."""
    pinned = _SENDER.get()
    if pinned is not None:
        return pinned
    return AGENT if any(os.environ.get(name, "").strip() for name in _ENV_AGENT) else HUMAN


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
    text: str, kind: str, *, by: str | None = None, now: datetime | None = None
) -> None:
    """Say that ``text``, about to reach an agent's prompt, is a ``kind`` sent by ``by`` (the
    process's :func:`origin_sender` when not given; always ``agent`` for one of
    :data:`AGENT_KINDS`). Stale markers are removed on the way."""
    if not text.strip():
        return
    by = AGENT if kind in AGENT_KINDS else by or origin_sender()
    when = now or datetime.now(tz=UTC)
    folder = origin_markers_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        _forget_stale_markers(folder)
        path = folder / f"{origin_digest(text)}.json"
        temp = path.with_suffix(f".{os.getpid()}.tmp")
        temp.write_text(
            json.dumps({"kind": kind, "by": by, "at": when.isoformat()}),
            encoding="utf-8",
        )
        temp.replace(path)
    except OSError:
        return


def unmark_origin(text: str) -> None:
    """Withdraw ``text``'s marker: the write did not reach the prompt after all."""
    with contextlib.suppress(OSError):
        (origin_markers_dir() / f"{origin_digest(text)}.json").unlink()


def take_origin(prompt: str | None, *, now: datetime | None = None) -> str | None:
    """The origin a marker left for ``prompt``, the marker gone; ``None`` when there is none,
    or only one older than :data:`MAX_AGE`."""
    if not prompt or not prompt.strip():
        return None
    path = origin_markers_dir() / f"{origin_digest(prompt)}.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    with contextlib.suppress(OSError):
        path.unlink()
    try:
        data = json.loads(raw)
        at = datetime.fromisoformat(str(data["at"]))
        by, kind = str(data["by"]), str(data["kind"])
    except (ValueError, KeyError, TypeError):
        return None
    if (now or datetime.now(tz=UTC)) - at > MAX_AGE or by not in (HUMAN, AGENT) or not kind:
        return None
    return compose_origin(AGENT if kind in AGENT_KINDS else by, kind)


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
    Code's own notice (``notice``, the caller's reading: :func:`only_task_notifications`), else a
    person typed it."""
    marked = take_origin(prompt, now=now)
    if marked is not None:
        return marked
    return AGENT_NOTICE if notice else HUMAN_TYPED


def _forget_stale_markers(folder: Path) -> None:
    """Remove the markers older than :data:`MAX_AGE` by the wall clock (their files' age)."""
    cutoff = time.time() - MAX_AGE.total_seconds()
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return
    for entry in entries:
        with contextlib.suppress(OSError):
            if entry.stat().st_mtime < cutoff:
                os.unlink(entry.path)
