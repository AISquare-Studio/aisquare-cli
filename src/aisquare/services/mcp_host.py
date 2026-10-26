"""Which board session an ``aisquare serve --stdio`` process is the own MCP server of.

A Claude Code session that mounts ``aisquare serve --stdio`` writes board rows through it,
and nothing in a row says whether a session's own server or a remote teammate's wrote it.
Treating every ``mcp:`` row as a teammate (option A) made a solo session read as a team after
its first board-tool call. Skipping them (#46) or trusting ``CLAUDE_CODE_SESSION_ID`` (#48)
hid real teammates, because Claude Code exports that variable to every shell it runs (#49).
This module is option B of docs/plans/mcp-own-row.md: the server *proves* whose it is, and
when it cannot, it is a teammate (the plan's hard rule: no member of a team may read as solo).

The proof is two records meeting:

1. **The SessionStart hook registers the process.** Claude Code gives its hooks
   ``CLAUDE_PID``, its own pid. The hook confirms that process is one of its own ancestors
   and records, per board, "process P (pid and start token) runs session X"
   (:func:`register`). A ``/clear`` or a fork starts a new session id inside the same
   process, and a ``--resume`` starts the same id in a new one: each start overwrites what
   P runs, so the record always names the session P serves *now*.
2. **The server proves it is P's own.** Claude Code starts a stdio server as its direct
   child: measured for ``uv tool install``, ``pipx``, ``pip`` in a venv and ``python -m``;
   a launcher that stays in between (``uvx`` runs ``uv``, a Windows ``aisquare.exe``
   trampoline runs ``python.exe``) is skipped (:func:`serving_process`). Whatever is left
   as the server's parent must be P, pid and start token (:func:`host_of`).

A shell the Bash tool started is also a child of P, so a ``serve`` run from it has that
shell as its parent, not P: no proof, a teammate. That is why the proof is "my parent is P"
and not "P is an ancestor of mine" (#49 MAJOR-1).

Everything that cannot be completed is "no proof": no ``CLAUDE_PID``, no hook record, a
process that cannot be read, a record on another board (the lookup is this board's, by exact
id: #49 m1), a host whose row has ended, a server older than this code, a row written by
older code. Those are the cases that stay teammates, until their rows go stale.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from collections.abc import Mapping

from aisquare.core import procs
from aisquare.core.store import ContextStore
from aisquare.models import TeamSession

_log = logging.getLogger(__name__)

#: What Claude Code's hooks get: its own pid. Measured on 2.1.310 in the SessionStart hook
#: (and in the Bash tool, which is why it proves nothing alone).
PROCESS_ENV = "CLAUDE_PID"

#: The launchers that start the real command and stay alive above it. A process whose
#: parent is one of these is as much the child of whatever started the launcher.
LAUNCHERS = frozenset({"uv", "uvx", "pipx"})

#: How many launchers one server may sit behind (``uvx``, then a trampoline, then two more
#: for a wrapper nobody measured); a longer chain is not read.
MAX_LAUNCHERS = 4

#: A record younger than this is kept without a look. An older one is kept only while its
#: process is still there with the same start token: the record of a process that is gone
#: matches no live server (its pid and start token are taken by nothing), so dropping it only
#: keeps ``team_meta`` from growing. A session that runs for weeks (a manager) keeps its
#: record for as long as it runs, however old it is.
CHECK_AFTER_SECONDS = 24 * 3600


#: The ``team_meta`` keys, ``host/<project id>/<pid>``. Under the project, as ``signal/``
#: is, so removing the project takes them (``ContextStore._purge_team_meta``).
_PREFIX = "host/"


def _key(project_id: str, pid: int) -> str:
    return f"{_PREFIX}{project_id}/{pid}"


def parent_pid() -> int:
    """This process's parent: the one place the proof asks, so a test says whose child it is."""
    return os.getppid()


def claude_process(
    environ: Mapping[str, str] | None = None,
    *,
    parent: int | None = None,
    look: procs.Inspect | None = None,
) -> procs.ProcessRef | None:
    """The Claude Code process this hook runs under, or ``None``.

    ``CLAUDE_PID`` names it, and it must be one of this process's own ancestors (the hook's
    parent, or the ``sh -c`` between them): a value copied into some other shell names a
    process that is not above this one, and registers nothing."""
    env = os.environ if environ is None else environ
    raw = env.get(PROCESS_ENV, "").strip()
    if not raw.isdigit() or int(raw) <= 1:
        return None
    wanted = int(raw)
    inspect = look or procs.inspect
    pid = parent_pid() if parent is None else parent
    for _ in range(procs.MAX_DEPTH):
        if pid <= 1:
            return None
        info = inspect(pid)
        if info is None:
            return None
        if pid == wanted:
            return procs.ProcessRef(pid, info.started)
        pid = info.ppid
    return None


def register(
    store: ContextStore,
    project_id: str,
    session_id: str,
    *,
    environ: Mapping[str, str] | None = None,
    parent: int | None = None,
    look: procs.Inspect | None = None,
) -> bool:
    """Record that the Claude Code process this hook runs under serves ``session_id`` on this
    board, replacing what it served before. ``False``: nothing recorded (no ``CLAUDE_PID``,
    or a process that is not above this hook), which only costs this session its own row.

    Never raises: a session start must not fail for a proof it can do without."""
    try:
        process = claude_process(environ, parent=parent, look=look)
        if process is None:
            return False
        now = time.time()
        store.set_meta(
            _key(project_id, process.pid),
            json.dumps({"session": session_id, "started": process.started, "at": now}),
        )
        _forget_old(store, project_id, now, look)
        return True
    except Exception:
        _log.debug("could not record the process of session %s", session_id, exc_info=True)
        return False


def _forget_old(
    store: ContextStore, project_id: str, now: float, look: procs.Inspect | None = None
) -> None:
    """Drop the records of this board that prove nothing any more: garbage, and the records
    of processes that are gone (or whose pid went to another process). A process that cannot
    be read is read as gone: its record is lost, which costs its session its own row until
    its hook registers again, never a teammate hidden."""
    inspect = look or procs.inspect
    for key, raw in store.list_meta(f"{_PREFIX}{project_id}/").items():
        record = _parse(raw)
        if record is None or _is_dead(key, record, now, inspect):
            store.delete_meta(key)


def _is_dead(key: str, record: tuple[str, str, float], now: float, inspect: procs.Inspect) -> bool:
    """Whether the process a record names is gone: asked only of a record older than
    :data:`CHECK_AFTER_SECONDS`, so the common start (every record young) runs no process
    lookup at all."""
    if now - record[2] <= CHECK_AFTER_SECONDS:
        return False
    pid = key.rsplit("/", 1)[-1]
    if not pid.isdigit():
        return True
    info = inspect(int(pid))
    return info is None or info.started != record[1]


def _parse(raw: str | None) -> tuple[str, str, float] | None:
    """``(session id, start token, written at)`` out of a record, or ``None`` for anything
    else: a record is read for a proof, and a doubtful one proves nothing."""
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        session, started, at = data["session"], data["started"], data["at"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(session, str) or not isinstance(started, str):
        return None
    if isinstance(at, bool) or not isinstance(at, int | float):
        return None
    return session, started, float(at)


#: The serving process found for one parent pid. Only what was found is kept: a walk that
#: could not finish asks again at the next call. A server's parent does not change under it
#: (a parent that goes leaves the server orphaned, and its client gone with it), but the pid
#: is the key all the same, so a reparent asks again.
_serving: dict[int, procs.ProcessRef] = {}


def serving_process(
    script: str | None = None,
    *,
    parent: int | None = None,
    look: procs.Inspect | None = None,
) -> procs.ProcessRef | None:
    """The process that started this server, launchers skipped, or ``None``.

    From the server's parent up, a launcher is passed over and the first process that is
    not one is what started the server (:func:`_is_launcher`). Never a shell: a shell is
    where a ``serve`` run from the Bash tool has its parent, indistinguishable from the
    ``sh -c`` of a client's command string. ``script`` is ``sys.argv[0]``'s file name by
    default."""
    inspect = look or procs.inspect
    pid = parent_pid() if parent is None else parent
    first = pid
    if first in _serving:
        return _serving[first]
    mine = inspect(os.getpid())
    own = procs.image_stem(script if script is not None else os.path.basename(sys.argv[0]))
    for _ in range(MAX_LAUNCHERS + 1):
        if pid <= 1:
            return None
        info = inspect(pid)
        if info is None:
            return None
        if _is_launcher(info, own, mine):
            pid = info.ppid
            continue
        found = procs.ProcessRef(pid, info.started)
        _serving[first] = found
        return found
    return None


def _is_launcher(info: procs.ProcInfo, own: str, mine: procs.ProcInfo | None) -> bool:
    """Whether a process above the server only starts it: a ``uv``, ``uvx`` or ``pipx`` that
    stays alive above the command it runs, this server's own console script (the
    ``aisquare.exe`` a Windows install puts above ``python.exe``), or the venv launcher
    that runs the real interpreter as its child (Windows: ``Scripts\\python.exe`` is a
    redirector, and the server reports its path as its ``sys.executable``).

    The interpreter's own launcher is told by its path, never by being a ``python``: a Python
    program that starts a server is that server's client, and a client is not skipped."""
    name = procs.image_stem(info.image)
    if name in LAUNCHERS or (own != "" and name == own):
        return True
    image = os.path.normcase(info.image)
    if image == "" or (mine is not None and image == os.path.normcase(mine.image)):
        return False
    launchers = (sys.executable, os.environ.get("__PYVENV_LAUNCHER__", ""))
    return image in {os.path.normcase(path) for path in launchers if path}


def host_of(
    store: ContextStore,
    project_id: str,
    *,
    script: str | None = None,
    parent: int | None = None,
    look: procs.Inspect | None = None,
) -> TeamSession | None:
    """The session on this board that this server is the own MCP server of, or ``None``.

    The process that started the server must be the one a hook registered for a session of
    THIS board (pid and start token both equal), and that session's row must be there and not
    ended, found by exact id among this board's sessions: ``store.get_session`` searches
    every board and matches a prefix (#49 m1)."""
    process = serving_process(script, parent=parent, look=look)
    if process is None:
        return None
    record = _parse(store.get_meta(_key(project_id, process.pid)))
    if record is None or record[1] != process.started:
        return None
    for session in store.team_sessions_seen_since(project_id, None, ids=[record[0]]):
        if session.id == record[0] and session.ended_at is None:
            return session
    return None
