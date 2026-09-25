"""The captain's runtime state — shared across processes, so it lives in files, never globals.

The Actions MCP server runs inside the captain's Claude Code session; the CLI
verbs, the voice page and the TUI are other processes. So every piece is kept
where all of them can reach it:

- **The home board** — the board a call that names no project is audited on:
  the project row for ``$AISQUARE_HOME``, CAPTURED (``store.ensure_project``),
  never onboarded, so it does not join the sidebar or ``project list``.
- **One virtual session per board** — ``captain:<first 12 of the project id>``,
  role ``captain``. Every write the captain makes names its session, and a
  session's registered board wins over cwd and ``AISQUARE_TEAM_HUB`` (#20), so
  a write for project B lands on B's board whatever directory the server runs in.
- In ``state.json`` (through :mod:`aisquare.core.state_file`, under its lock):
  ``captain_watermarks`` ({project id: {agent label or ``*``: seq}}) for
  ``since``; ``captain_busy`` (``{"since": iso}`` while thinking, absent
  otherwise); ``captain_brake_at`` (when ``bt`` was last pulled);
  ``captain_waiting`` (an ``ask_manager`` wait in flight); ``captain_undo``
  (the last :data:`UNDO_KEEP` reversible actions).
- **The speech spool** — ``$AISQUARE_HOME/captain/speech/<id>.txt``, one file
  per line to say, ids time-sortable, taken oldest first by the Speaker.
- **The ui socket** — ``$AISQUARE_HOME/captain/ui.sock``, where a running
  ``asq`` listens for ``ui`` actions.
"""

from __future__ import annotations

import contextlib
import itertools
import os
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from aisquare.core import paths, state_file
from aisquare.core.store import store_session
from aisquare.core.workspace import project_id_for
from aisquare.models import ProjectInfo, TeamSession

CAPTAIN_ROLE = "captain"
SESSION_PREFIX = "captain:"

_WATERMARKS = "captain_watermarks"
_BUSY = "captain_busy"
_BRAKE = "captain_brake_at"
_WAITING = "captain_waiting"
_UNDO = "captain_undo"
_WHOLE_BOARD = "*"

UNDO_KEEP = 20
"""How many reversible actions ``bt`` can walk back through, newest first."""

UndoKind = Literal["claim", "done"]


def _now() -> datetime:
    return datetime.now(tz=UTC)


def captain_dir() -> Path:
    """``$AISQUARE_HOME/captain`` — the captain's files (not created here)."""
    return paths.aisquare_home() / "captain"


def speech_dir() -> Path:
    return captain_dir() / "speech"


def ui_socket_path() -> Path:
    """Where a running ``asq`` listens for ``ui`` actions (T4 binds it)."""
    return captain_dir() / "ui.sock"


# --- the home board and the captain's sessions ------------------------------------------


def home_project() -> ProjectInfo:
    """The home board: the project row for ``$AISQUARE_HOME``, captured on first use."""
    root = paths.aisquare_home().resolve()
    project = ProjectInfo(id=project_id_for(root), root=root)
    with store_session() as store:
        store.ensure_project(project)
        stored = store.get_project(project.id)
    return stored if stored is not None else project


def session_id_for(project_id: str) -> str:
    """The captain's virtual session on one board.

    Twelve characters of the project id, not the six ``aisquare serve`` uses: a
    session id names ONE board, and :func:`ensure_session` refuses a clash
    rather than letting a write route to the wrong one.
    """
    return f"{SESSION_PREFIX}{project_id.removeprefix('prj_')[:12]}"


def ensure_session(project: ProjectInfo) -> str:
    """Register (or refresh) the captain's session on ``project``'s board; returns its id."""
    session_id = session_id_for(project.id)
    now = _now()
    with store_session() as store:
        store.ensure_project(project)
        existing = store.get_session(session_id)
        if existing is not None and existing.project_id != project.id:
            raise ValueError(
                f"the captain's session {session_id} already belongs to board "
                f"{existing.project_id}, not {project.id} — two project ids share a prefix"
            )
        store.upsert_session(
            TeamSession(
                id=session_id,
                project_id=project.id,
                role=CAPTAIN_ROLE,
                started_at=now,
                last_seen_at=now,
                cursor=store.latest_seq(project.id),
            )
        )
    return session_id


# --- watermarks ---------------------------------------------------------------------------


def watermark(project_id: str, agent: str | None) -> int | None:
    """The last seq ``since`` reported for this board (``agent`` None) or this agent."""
    marks = state_file.read_state().get(_WATERMARKS)
    board = marks.get(project_id) if isinstance(marks, dict) else None
    seq = board.get(agent or _WHOLE_BOARD) if isinstance(board, dict) else None
    return seq if isinstance(seq, int) and not isinstance(seq, bool) else None


def set_watermark(project_id: str, agent: str | None, seq: int) -> None:
    def change(current: object) -> object:
        marks = cast(dict[str, object], current) if isinstance(current, dict) else {}
        board = marks.get(project_id)
        entries = cast(dict[str, object], board) if isinstance(board, dict) else {}
        entries[agent or _WHOLE_BOARD] = seq
        marks[project_id] = entries
        return marks

    state_file.modify_state(_WATERMARKS, change)


# --- the busy flag --------------------------------------------------------------------------


def set_busy(on: bool) -> None:
    """Thinking on stamps when it started; off removes the flag."""
    state_file.update_state(_BUSY, {"since": _now().isoformat()} if on else None)


def busy_since() -> datetime | None:
    flag = state_file.read_state().get(_BUSY)
    return _parse_time(flag.get("since")) if isinstance(flag, dict) else None


# --- the speech spool -----------------------------------------------------------------------


@dataclass(frozen=True)
class Speech:
    """One line waiting for the Speaker."""

    id: str
    text: str


_SPEECH_SEQUENCE = itertools.count()


def enqueue_speech(text: str) -> str:
    """Spool one line for the Speaker; returns its id (ids sort oldest first).

    Written to a dot-named temp file and renamed into place, so the Speaker
    never reads half a line.
    """
    folder = speech_dir()
    folder.mkdir(parents=True, exist_ok=True)
    speech_id = f"spk_{time.time_ns():020d}_{next(_SPEECH_SEQUENCE):06d}_{secrets.token_hex(3)}"
    temp = folder / f".{speech_id}.tmp"
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, folder / f"{speech_id}.txt")
    return speech_id


def _spooled() -> list[Path]:
    folder = speech_dir()
    if not folder.is_dir():
        return []
    return sorted(path for path in folder.glob("spk_*.txt"))


def pending_speech() -> list[Speech]:
    """Every line still waiting, oldest first."""
    pending: list[Speech] = []
    for path in _spooled():
        with contextlib.suppress(OSError):
            pending.append(Speech(path.stem, path.read_text(encoding="utf-8")))
    return pending


def take_speech() -> Speech | None:
    """Take the oldest line: exactly one taker wins it (a rename claims it first)."""
    for path in _spooled():
        claimed = path.with_name(f".{path.stem}.{os.getpid()}.taking")
        try:
            os.replace(path, claimed)
        except OSError:
            continue  # another taker won it
        try:
            return Speech(path.stem, claimed.read_text(encoding="utf-8"))
        finally:
            claimed.unlink(missing_ok=True)
    return None


def clear_speech() -> int:
    """Drop every waiting line; returns how many went."""
    cleared = 0
    for path in _spooled():
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
            cleared += 1
    return cleared


# --- the brake and the wait it cancels -----------------------------------------------------


def pull_brake() -> datetime:
    """Stamp the brake: every wait that started before now stops at its next look."""
    at = _now()
    state_file.update_state(_BRAKE, at.isoformat())
    return at


def brake_pulled_after(started: datetime) -> bool:
    at = _parse_time(state_file.read_state().get(_BRAKE))
    return at is not None and at >= started


def set_waiting(project_id: str | None) -> None:
    """Mark an ``ask_manager`` wait in flight (a project id) or over (``None``)."""
    state_file.update_state(
        _WAITING,
        None if project_id is None else {"project": project_id, "since": _now().isoformat()},
    )


def waiting_on() -> str | None:
    """The project an ``ask_manager`` wait is in flight for, if one is."""
    wait = state_file.read_state().get(_WAITING)
    project = wait.get("project") if isinstance(wait, dict) else None
    return project if isinstance(project, str) else None


# --- the undo log ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Undo:
    """One reversible action: a claim the captain made, or a task it closed."""

    kind: UndoKind
    task_id: str
    project_id: str


def _undo_entries(current: object) -> list[dict[str, str]]:
    if not isinstance(current, list):
        return []
    return [
        cast(dict[str, str], entry)
        for entry in current
        if isinstance(entry, dict)
        and entry.get("kind") in ("claim", "done")
        and isinstance(entry.get("task"), str)
        and isinstance(entry.get("project"), str)
    ]


def record_undo(kind: UndoKind, task_id: str, project_id: str) -> None:
    def change(current: object) -> object:
        entries = _undo_entries(current)
        entries.append({"kind": kind, "task": task_id, "project": project_id})
        return entries[-UNDO_KEEP:]

    state_file.modify_state(_UNDO, change)


def pop_undo() -> Undo | None:
    """Take the newest reversible action off the log (``None`` when there is none)."""
    popped: list[dict[str, str]] = []

    def change(current: object) -> object:
        entries = _undo_entries(current)
        if entries:
            popped.append(entries.pop())
        return entries or None

    state_file.modify_state(_UNDO, change)
    if not popped:
        return None
    entry = popped[0]
    return Undo(cast(UndoKind, entry["kind"]), entry["task"], entry["project"])


def _parse_time(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
