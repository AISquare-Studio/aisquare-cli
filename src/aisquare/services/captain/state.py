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
- **The ui socket** — :func:`ui_socket_path`, where a running ``asq`` listens
  for ``ui`` actions: ``$AISQUARE_HOME/captain/ui.sock`` when that fits a unix
  socket's path limit, else a short per-user path keyed by the home. Both sides
  — the ``ui`` tool and T4's receiver — call the same helper.
"""

from __future__ import annotations

import hashlib
import itertools
import logging
import os
import stat
import sys
import tempfile
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

UI_SOCKET_MAX = 100
"""Bytes a unix socket path may take here: ``sun_path`` holds 108 on Linux and 104 on
macOS, both counting the terminating NUL — 100 leaves room on either."""

_log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(tz=UTC)


def captain_dir() -> Path:
    """``$AISQUARE_HOME/captain`` — the captain's files (not created here)."""
    return paths.aisquare_home() / "captain"


def speech_dir() -> Path:
    return captain_dir() / "speech"


def ui_socket_path(*, create: bool = False) -> Path:
    """Where a running ``asq`` listens for ``ui`` actions — the ONE path both sides use.

    ``$AISQUARE_HOME/captain/ui.sock`` when it fits :data:`UI_SOCKET_MAX`; a longer
    home (every isolated test home, a deep checkout) would make ``bind`` and
    ``connect`` fail with "AF_UNIX path too long", so it gets
    ``captain-<hash of the home>.sock`` in the private per-user folder
    ``/tmp/aisquare-<uid>`` instead (the temp dir on Windows, which has no unix
    sockets). The hash keeps two homes from sharing a receiver. Nothing from the
    ENVIRONMENT picks the folder — not ``XDG_RUNTIME_DIR``, not ``TMPDIR``: tmux, cron
    and ``sudo -u`` do not carry them, and a binder and a dialer that disagreed on the
    folder would make ``ui`` say "asq is not running" while asq listens (review of the
    #217 fix round). Only the uid and the resolved home decide.

    ``create`` makes the folder — the binder's job (T4). A folder outside the home
    must be a real directory owned by this user and closed to everyone else
    (``0700``); anything else is refused with :class:`OSError`, because a socket
    there could be squatted or read by another account. The client dials through
    :func:`ui_socket_to_dial`, which holds a folder outside the home to the same rule.

    The home is RESOLVED first, for the length test and the hash alike, so two
    spellings of one home (a symlink) land on one path.
    """
    home = paths.aisquare_home().resolve()
    natural = home / "captain" / "ui.sock"
    if _fits(natural):
        if create:
            natural.parent.mkdir(parents=True, exist_ok=True)
        return natural
    name = f"captain-{hashlib.sha256(str(home).encode()).hexdigest()[:16]}.sock"
    folder = _short_root() / f"aisquare-{_user_tag()}"
    if create:
        _private_folder(folder)
    return folder / name


def _short_root() -> Path:
    """Where a long home's socket folder lives: ``/tmp`` on POSIX (short everywhere, and the
    same for every process of the user), the temp dir on Windows. A test seam too."""
    if sys.platform == "win32":
        return Path(tempfile.gettempdir())
    return Path("/tmp")


def ui_socket_to_dial() -> Path | None:
    """The ui socket a client may connect to, or ``None`` when its folder does not exist.

    A folder outside the home (the short-path fallback, in a shared ``/tmp``) is
    checked BEFORE dialling, exactly as the binder checks it: another account could
    have pre-created it and be listening there, and would then read every ``ui``
    request and answer it as asq (gate on the #217 fix round). A missing folder
    means nothing can be listening — ``None``, and no connect, so there is no
    window for a squatter to create it between a check and the dial. A folder
    that exists but is not a private one of this user raises :class:`OSError`.
    """
    path = ui_socket_path()
    folder = path.parent
    if folder == paths.aisquare_home().resolve() / "captain":
        return path  # inside the user's own home
    try:
        facts = folder.lstat()
    except FileNotFoundError:
        return None
    _check_private(folder, facts)
    return path


def _fits(path: Path) -> bool:
    return len(os.fsencode(str(path))) <= UI_SOCKET_MAX


def _user_tag() -> str:
    if sys.platform == "win32":
        return os.environ.get("USERNAME", "user")
    return str(os.getuid())


def _private_folder(folder: Path) -> None:
    """Make ``folder`` 0700, or refuse one that is shared, foreign or not a directory."""
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    _check_private(folder, folder.lstat())


def _check_private(folder: Path, facts: os.stat_result) -> None:
    if sys.platform == "win32":
        return  # no unix sockets there; nothing binds or dials this folder
    if not stat.S_ISDIR(facts.st_mode) or facts.st_uid != os.getuid() or facts.st_mode & 0o077:
        raise OSError(
            f"refusing the ui socket folder {folder}: not a private folder (0700) of this user"
        )


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


def _state() -> dict[str, object]:
    """``state.json``, strictly: an UNREADABLE file raises its ``OSError`` rather than reading
    as empty — a watermark that silently reads as unset re-reports old events as new."""
    return state_file.read_state(strict=True)


def _malformed(key: str, value: object) -> None:
    _log.warning("state.json %s holds %r, which is not what the captain wrote; ignored", key, value)


def watermark(project_id: str, agent: str | None) -> int | None:
    """The last seq ``since`` reported for this board (``agent`` None) or this agent."""
    marks = _state().get(_WATERMARKS)
    if marks is None:
        return None
    if not isinstance(marks, dict):
        _malformed(_WATERMARKS, marks)
        return None
    board = marks.get(project_id)
    if board is None:
        return None
    if not isinstance(board, dict):
        _malformed(f"{_WATERMARKS}.{project_id}", board)
        return None
    seq = board.get(agent or _WHOLE_BOARD)
    if seq is None:
        return None
    if not isinstance(seq, int) or isinstance(seq, bool):
        _malformed(f"{_WATERMARKS}.{project_id}.{agent or _WHOLE_BOARD}", seq)
        return None
    return seq


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
    flag = _state().get(_BUSY)
    if flag is None:
        return None
    since = _parse_time(flag.get("since")) if isinstance(flag, dict) else None
    if since is None:
        _malformed(_BUSY, flag)
    return since


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
    speech_id = f"spk_{time.time_ns():020d}_{next(_SPEECH_SEQUENCE):06d}_{os.urandom(3).hex()}"
    temp = folder / f".{speech_id}.tmp"
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, folder / f"{speech_id}.txt")
    return speech_id


def _spooled() -> list[Path]:
    folder = speech_dir()
    if not folder.is_dir():
        return []
    return sorted(path for path in folder.glob("spk_*.txt"))


def _read_line(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def pending_speech() -> list[Speech]:
    """Every line still waiting, oldest first.

    A line taken by the Speaker between the listing and the read is gone, not
    lost (debug log). A line that cannot be read is logged as a warning naming
    its file — it stays in the spool, and the Speaker's own take says why.
    """
    pending: list[Speech] = []
    for path in _spooled():
        try:
            pending.append(Speech(path.stem, _read_line(path)))
        except FileNotFoundError:
            _log.debug("speech %s was taken while listing the spool", path.name)
        except OSError as exc:
            _log.warning("speech %s could not be read and is left in the spool: %s", path, exc)
    return pending


def take_speech() -> Speech | None:
    """Take the oldest line: exactly one taker wins it (a rename claims it first).

    Losing the rename to another taker is the expected race (debug log). Any
    other failure to claim or read a line is logged with its file and the next
    line is tried, so one bad file cannot silence the Speaker.
    """
    for path in _spooled():
        claimed = path.with_name(f".{path.stem}.{os.getpid()}.taking")
        try:
            os.replace(path, claimed)
        except FileNotFoundError:
            _log.debug("speech %s was taken by another reader", path.name)
            continue
        except OSError as exc:
            _log.warning("speech %s could not be claimed: %s", path, exc)
            continue
        try:
            return Speech(path.stem, _read_line(claimed))
        except OSError as exc:
            _log.warning("speech %s was claimed but could not be read, dropped: %s", path, exc)
        finally:
            claimed.unlink(missing_ok=True)
    return None


def clear_speech() -> int:
    """Drop every waiting line; returns how many went (a line the Speaker took first is not one)."""
    cleared = 0
    for path in _spooled():
        try:
            path.unlink()
        except FileNotFoundError:
            _log.debug("speech %s was taken before the brake cleared it", path.name)
            continue
        cleared += 1
    return cleared


# --- the brake and the wait it cancels -----------------------------------------------------


def pull_brake() -> datetime:
    """Stamp the brake: every wait that started before now stops at its next look."""
    at = _now()
    state_file.update_state(_BRAKE, at.isoformat())
    return at


def brake_pulled_after(started: datetime) -> bool:
    raw = _state().get(_BRAKE)
    if raw is None:
        return False
    at = _parse_time(raw)
    if at is None:
        _malformed(_BRAKE, raw)
        return False
    return at >= started


def set_waiting(project_id: str | None) -> None:
    """Mark an ``ask_manager`` wait in flight (a project id) or over (``None``)."""
    state_file.update_state(
        _WAITING,
        None if project_id is None else {"project": project_id, "since": _now().isoformat()},
    )


def waiting_on() -> str | None:
    """The project an ``ask_manager`` wait is in flight for, if one is."""
    wait = _state().get(_WAITING)
    if wait is None:
        return None
    project = wait.get("project") if isinstance(wait, dict) else None
    if not isinstance(project, str):
        _malformed(_WAITING, wait)
        return None
    return project


# --- the undo log ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Undo:
    """One reversible action: a claim the captain made, or a task it closed."""

    kind: UndoKind
    task_id: str
    project_id: str


def _undo_entries(current: object) -> list[dict[str, str]]:
    if current is None:
        return []
    if not isinstance(current, list):
        _log.warning("state.json captain_undo is not a list and is ignored: %r", current)
        return []
    kept = [
        cast(dict[str, str], entry)
        for entry in current
        if isinstance(entry, dict)
        and entry.get("kind") in ("claim", "done")
        and isinstance(entry.get("task"), str)
        and isinstance(entry.get("project"), str)
    ]
    if len(kept) != len(current):
        _log.warning(
            "state.json captain_undo: %d malformed entries ignored", len(current) - len(kept)
        )
    return kept


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
