"""asq's ui receiver: the captain's ``ui`` actions arrive over a local socket and run here (T4).

The captain's Actions server (``services/captain/actions.py``, T1) has a ``ui``
tool — and the owner's action list may hold steps ``ui <action> <arg>`` — that
asks a running asq to do something on screen. It dials
``captain_state.ui_socket_path()``, the ONE path both sides compute, sends one
JSON line ``{"v": 1, "action": <str>, "arg": <str or null>}`` and reads one line
back within two seconds, ``{"ok": <bool>, "said": <str>}``; ``ok`` false reaches
the captain as ``refused: asq said: <said>``. This module is the other end.

The vocabulary — :data:`ACTIONS`, one small function of the app and the arg each:

- ``open_spawn [project]`` — the Spawn dialog for that project; with no project,
  the one selected in the sidebar (a selected agent's project).
- ``open_stop <agent>`` — the Stop dialog for that agent. The dialog asks the
  owner; the action stops nothing, and a row with nothing to stop is refused by
  the rule the ``x`` key and the Stop button ask (``STOP_STATES``).
- ``select_project <project>`` / ``select_agent <agent>`` — what a click on the
  row does.
- ``copy_row <agent|project>`` — the row's text to the clipboard: an agent's
  ``label  role  state  pane``, a project's ``name  codename  root``.
- ``focus_project <project>`` — select it and hand the keyboard to its card in
  the sidebar, so ↑/↓ and Enter go on from there.

A **project** is named by its id, an id prefix, its codename or its name (case
ignored; a name two projects share is refused with both). The captain's home
board is never one: the frame's projects do not hold it (T2). An **agent** is
``<project>/<label>``, or a bare label or agent id that names exactly one listed
row — the captain's included, whose board is the home.

Rules the receiver keeps, each for a reason:

- **Actions run on the app's thread.** Textual is not thread-safe
  (``post_message`` is the exception), and every action reads the frame and the
  sidebar, so the receiver's thread hands each one to the loop with
  ``App.call_from_thread`` and answers with what it returned. An action posts a
  message, pushes a dialog through the shell's own handler, or copies — it never
  waits for a spawn or a stop — so the answer is back well inside the client's
  two seconds.
- **A live socket is never stolen.** A socket file already at the path is
  dialled first: an answer means another asq listens there, and this one runs
  without a receiver (said once in the log); a refused dial means an asq crashed
  and left it, and it is replaced. Anything that is not a socket is left alone.
- **Only our own socket is unlinked.** Quit unlinks the path only while it is
  still the file this receiver bound — its device and inode — so a second asq's
  socket at the same path survives the first one's quit.
- **The socket is the user's alone.** The file is ``0600``, in the home's own
  folder or in the private ``0700`` one ``ui_socket_path(create=True)`` checks; a
  folder that fails that check means no receiver, never a crash.
- **Every failure is an answer.** A malformed line, an unknown action, a ref that
  names nothing, a handler that raised: each is ``{"ok": false, "said": ...}``,
  and the receiver serves the next connection.
- **Quit never waits on the loop it runs on.** Quit runs on the app's thread; an
  action inside ``call_from_thread`` waits for that same thread. So quit does not
  join a receiver that is mid-dispatch — it closes the door and lets the thread
  finish that one answer and end itself.
"""

from __future__ import annotations

import json
import logging
import os
import select
import socket
import stat
import sys
import threading
import time
import weakref
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from textual.screen import ModalScreen

from aisquare.cli.ui.sidebar import (
    STOP_STATES,
    AgentSelected,
    ProjectSelected,
    SpawnAgent,
    StopAgent,
    project_name,
)
from aisquare.models import FleetAgentStatus, ProjectInfo
from aisquare.services.captain import state as captain_state

if TYPE_CHECKING:
    from aisquare.cli.ui.app import FleetApp, FleetSnapshot

VERSION = 1
"""The request's ``v``: T1's client sends 1; a line of another version is refused."""

LINE_MAX = 64 * 1024
"""Bytes one request line may take — the bound T1's client reads its answer with."""

READ_S = 1.0
"""How long a connection has to send its line. Connections are served one at a time, so a
client that dials and says nothing holds the next one up for at most this long — and under
the client's two seconds (``actions.UI_TIMEOUT_S``) the next one is still answered in time."""

WRITE_S = 1.0
"""How long an answer may take to leave; a client that has gone costs no more than this."""

PROBE_S = 0.5
"""How long the dial to a socket file already at the path may take. A live asq's socket
takes the connect at once (the kernel does, from its backlog); a dial that times out is a
full backlog — taken as live, never stolen."""

JOIN_S = 1.0
"""How long quit waits for the receiver's thread. Woken, it ends at once — and while an
action is on its way to the loop, quit does not wait at all (``UiReceiver.stop_listening``)."""

SAID_MAX = 1000
"""Characters an answer's ``said`` may take. It is read aloud, and a refusal quotes what it
was asked: unbounded, a 64 KiB action name would not fit the client's 64 KiB read."""

_SHOWN = 6
"""Candidates a refusal names before it says how many more there are."""

_log = logging.getLogger(__name__)

Handler = Callable[["FleetApp", str | None], str]
"""One action: on the app's thread, it returns what to say or raises :class:`UiRefusal`."""


class UiRefusal(Exception):
    """An action, or the line that asked for it, said no: the message is the answer's ``said``."""


class _Taken(Exception):
    """The socket's path holds something this asq must not replace."""


# --- resolving what a ref names, against the frame on screen ------------------------------------


def _frame(app: FleetApp) -> FleetSnapshot:
    snapshot = app.snapshot
    if snapshot is None:
        raise UiRefusal("asq has not read the fleet yet — ask again in a moment")
    return snapshot


def _needs(arg: str | None, action: str, what: str) -> str:
    if arg is None:
        raise UiRefusal(f"{action} needs {what}")
    return arg


def _quoted(text: str) -> str:
    """``'text'`` for a refusal — cut short, because the refusal echoes what it was asked."""
    return repr(text if len(text) <= 60 else text[:57] + "...")


def _listed(names: list[str]) -> str:
    more = len(names) - _SHOWN
    return ", ".join(names[:_SHOWN]) + (f" and {more} more" if more > 0 else "")


def _project_matches(snapshot: FleetSnapshot, ref: str) -> list[ProjectInfo]:
    """Every listed project ``ref`` names: an exact id alone, else id prefix, codename or name.

    Only ``snapshot.projects``, which never holds the captain's home board (T2) —
    ``FleetSnapshot.project`` keeps the same rule — so no ref reaches the home here.
    """
    exact = [project for project in snapshot.projects if project.id == ref]
    if exact:
        return exact
    folded = ref.casefold()
    return [
        project
        for project in snapshot.projects
        if project.id.startswith(ref)
        or (project.codename or "").casefold() == folded
        or project_name(project).casefold() == folded
    ]


def _project_label(project: ProjectInfo) -> str:
    codename = f" · {project.codename}" if project.codename else ""
    return f"{project_name(project)}{codename} ({project.id})"


def find_project(snapshot: FleetSnapshot, ref: str) -> ProjectInfo:
    """The one listed project ``ref`` names, or a refusal naming what it matched instead."""
    matches = _project_matches(snapshot, ref)
    if not matches:
        raise UiRefusal(
            f"no project matches {_quoted(ref)} (an id, an id prefix, a codename or a name)"
        )
    if len(matches) > 1:
        names = _listed([_project_label(project) for project in matches])
        raise UiRefusal(
            f"{_quoted(ref)} matches several projects: {names} — use the codename or the id"
        )
    return matches[0]


def _where(snapshot: FleetSnapshot, project_id: str) -> str:
    """How a row's board is named in what the receiver says: its project, or ``home``."""
    if snapshot.is_home(project_id):
        return "home"
    project = snapshot.project(project_id)
    return project_name(project) if project is not None else project_id


def _agent_label(snapshot: FleetSnapshot, row: FleetAgentStatus) -> str:
    return f"{_where(snapshot, row.agent.project_id)}/{row.agent.label} ({row.agent.id})"


def _agents_named(snapshot: FleetSnapshot, ref: str) -> list[FleetAgentStatus]:
    """Every listed row a bare ``ref`` names — by id alone when one matches, else by label.

    Every board the frame holds, the home's included: the captain's row lives in
    ``snapshot.agents[home.id]``.
    """
    rows = [row for listed in snapshot.agents.values() for row in listed]
    by_id = [row for row in rows if row.agent.id == ref]
    if by_id:
        return by_id
    folded = ref.casefold()
    return [row for row in rows if row.agent.label.casefold() == folded]


def find_agent(snapshot: FleetSnapshot, ref: str) -> FleetAgentStatus:
    """The one listed row ``<project>/<label>``, a label or an agent id names, or a refusal."""
    board, slash, label = ref.partition("/")
    if slash:
        project = find_project(snapshot, board.strip())
        wanted = label.strip().casefold()
        rows = snapshot.agents.get(project.id, [])
        matches = [row for row in rows if row.agent.label.casefold() == wanted]
        missing = f"no agent {_quoted(label.strip())} in {project_name(project)}"
    else:
        matches = _agents_named(snapshot, ref)
        missing = f"no agent matches {_quoted(ref)} (<project>/<label>, a label or an agent id)"
    if not matches:
        raise UiRefusal(missing)
    if len(matches) > 1:
        # Labels are unique among a project's LIVE agents only: an ended row keeps its label.
        names = _listed([_agent_label(snapshot, row) for row in matches])
        raise UiRefusal(
            f"{_quoted(ref)} names several agents: {names} — say <project>/<label> or the id"
        )
    return matches[0]


def _row_for(snapshot: FleetSnapshot, ref: str) -> FleetAgentStatus | ProjectInfo:
    """``copy_row``'s ref: an agent or a project, whichever it names — and only one of them."""
    if "/" in ref:
        return find_agent(snapshot, ref)
    found: list[FleetAgentStatus | ProjectInfo] = [
        *_agents_named(snapshot, ref),
        *_project_matches(snapshot, ref),
    ]
    if not found:
        raise UiRefusal(f"no agent or project matches {_quoted(ref)}")
    if len(found) > 1:
        names = [
            _project_label(row) if isinstance(row, ProjectInfo) else _agent_label(snapshot, row)
            for row in found
        ]
        raise UiRefusal(
            f"{_quoted(ref)} names several rows: {_listed(names)} — "
            "say <project>/<label> for an agent, or the project's id"
        )
    return found[0]


def _selected_project(app: FleetApp, snapshot: FleetSnapshot) -> ProjectInfo:
    """The project the sidebar's selection is about: a card's, or a selected agent's."""
    kind, _, ident = (app.sidebar.selected_key or "").partition(":")
    project_id: str | None = ident if kind == "project" else None
    if kind == "agent":
        rows = [row for listed in snapshot.agents.values() for row in listed]
        row = next((row for row in rows if row.agent.id == ident), None)
        if row is not None and snapshot.is_home(row.agent.project_id):
            raise UiRefusal(
                "which project? the captain is selected, and its home board takes no spawns — "
                "say open_spawn <project>"
            )
        project_id = row.agent.project_id if row is not None else None
    project = snapshot.project(project_id) if project_id else None
    if project is None:
        raise UiRefusal(
            "which project? nothing selected in asq names one — say open_spawn <project>"
        )
    return project


def _no_dialog_open(app: FleetApp) -> None:
    """A dialog opener refuses while a dialog is up: one dialog over the shell, never a stack.

    A second Spawn over the first, or a Stop over a half-filled Spawn, would leave the
    owner answering questions in an order they did not choose.
    """
    screen = app.screen
    if isinstance(screen, ModalScreen):
        raise UiRefusal(f"{type(screen).__name__} is open in asq — close it first")


# --- the vocabulary ------------------------------------------------------------------------------


def _open_spawn(app: FleetApp, arg: str | None) -> str:
    snapshot = _frame(app)
    _no_dialog_open(app)
    project = find_project(snapshot, arg) if arg is not None else _selected_project(app, snapshot)
    # The sidebar's spawn row posts exactly this, and the shell answers it with the dialog.
    app.post_message(SpawnAgent(project.id))
    return f"spawn dialog open for {project_name(project)}"


def _open_stop(app: FleetApp, arg: str | None) -> str:
    snapshot = _frame(app)
    _no_dialog_open(app)
    row = find_agent(snapshot, _needs(arg, "open_stop", "an agent"))
    agent = row.agent
    if row.state not in STOP_STATES:
        raise UiRefusal(
            f"{agent.label} is {row.state}: there is nothing to stop (a lost row is fleet reap's)"
        )
    app.post_message(StopAgent(agent.project_id, agent.id))
    return f"stop dialog open for {agent.label} ({_where(snapshot, agent.project_id)})"


def _select_project(app: FleetApp, arg: str | None) -> str:
    project = find_project(_frame(app), _needs(arg, "select_project", "a project"))
    app.post_message(ProjectSelected(project.id))
    return f"selected {project_name(project)}"


def _select_agent(app: FleetApp, arg: str | None) -> str:
    snapshot = _frame(app)
    row = find_agent(snapshot, _needs(arg, "select_agent", "an agent"))
    app.post_message(AgentSelected(row.agent.project_id, row.agent.id))
    return f"selected {row.agent.label} ({_where(snapshot, row.agent.project_id)})"


def _copy_row(app: FleetApp, arg: str | None) -> str:
    row = _row_for(_frame(app), _needs(arg, "copy_row", "an agent or a project"))
    parts: tuple[str, ...]
    if isinstance(row, ProjectInfo):
        name = project_name(row)
        parts = (name, row.codename or "", str(row.root))
    else:
        name = row.agent.label
        parts = (name, row.agent.role, row.state, row.agent.pane_id)
    app.copy_to_clipboard("  ".join(part for part in parts if part))
    return f"copied {name}'s row"


def _focus_project(app: FleetApp, arg: str | None) -> str:
    project = find_project(_frame(app), _needs(arg, "focus_project", "a project"))
    app.post_message(ProjectSelected(project.id))
    sidebar = app.sidebar
    sidebar.focus()
    if not sidebar.put_cursor(f"project:{project.id}"):
        return (
            f"selected {project_name(project)}; its card is in a folded group, "
            "so the sidebar cursor stayed where it was"
        )
    return f"focused {project_name(project)} in the sidebar"


ACTIONS: dict[str, Handler] = {
    "open_spawn": _open_spawn,
    "open_stop": _open_stop,
    "select_project": _select_project,
    "select_agent": _select_agent,
    "copy_row": _copy_row,
    "focus_project": _focus_project,
}
"""The vocabulary: every action a ``ui`` call — or a ``ui`` step of an owner action — may name."""


def _run(action: str, app: FleetApp, arg: str | None) -> tuple[bool, str]:
    """One action, ON the app's thread. A refusal is its answer; anything else it raises
    reaches the receiver's thread through ``call_from_thread`` and is answered there."""
    handler = ACTIONS.get(action)
    if handler is None:
        return False, f"unknown ui action {_quoted(action)} — known: {', '.join(sorted(ACTIONS))}"
    try:
        return True, handler(app, arg)
    except UiRefusal as exc:
        return False, str(exc)


# --- the wire -------------------------------------------------------------------------------------


def _unix_socket() -> socket.socket:
    """A unix stream socket. The guard is one mypy reads: Windows typeshed has no AF_UNIX."""
    if sys.platform == "win32":
        raise OSError("unix sockets are not available on Windows")
    return socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)


def _read_line(conn: socket.socket, wake: int) -> bytes | None:
    """The request line, without its newline — ``None`` for a dial that sent nothing at all.

    Such a dial is another asq asking, at its start, whether this one is live: it
    hangs up without a word, and is owed no answer. Bounded in size
    (:data:`LINE_MAX`) and in time (:data:`READ_S`, the whole line, not each read),
    and a quit (``wake``) ends the wait.
    """
    deadline = time.monotonic() + READ_S
    data = bytearray()
    while True:
        end = data.find(b"\n", 0, LINE_MAX + 1)
        if end >= 0:
            return bytes(data[:end])
        if len(data) > LINE_MAX:
            raise UiRefusal(f"not a request: the line is longer than {LINE_MAX // 1024} KiB")
        left = deadline - time.monotonic()
        if left <= 0:
            raise UiRefusal(f"not a request: no line within {READ_S:g}s")
        readable, _, _ = select.select([conn.fileno(), wake], [], [], left)
        if wake in readable:
            raise UiRefusal("asq is quitting")
        if readable:
            chunk = conn.recv(LINE_MAX)
            if not chunk:
                return bytes(data) if data else None
            data += chunk


def _parse(line: bytes) -> tuple[str, str | None]:
    """``(action, arg)`` from a request line; a line that is not one is refused, saying why."""
    if not line.strip():
        raise UiRefusal("not a request: the line is empty")
    try:
        request = json.loads(line)
    except (ValueError, RecursionError):
        raise UiRefusal("not a request: the line is not JSON") from None
    if not isinstance(request, dict):
        raise UiRefusal("not a request: the line is not a JSON object")
    version = request.get("v")
    if type(version) is not int or version != VERSION:  # `true == 1`, and true is no version
        shown = _quoted(json.dumps(version, default=str))
        raise UiRefusal(f"not a request: v must be {VERSION}, not {shown}")
    action = request.get("action")
    if not isinstance(action, str) or not action:
        raise UiRefusal("not a request: action must be a string")
    arg = request.get("arg")
    if arg is not None and not isinstance(arg, str):
        raise UiRefusal("not a request: arg must be a string or null")
    return action, (arg.strip() or None) if arg is not None else None


def _reply(conn: socket.socket, ok: bool, said: str) -> None:
    if len(said) > SAID_MAX:
        said = said[: SAID_MAX - 1] + "…"
    line = json.dumps({"ok": ok, "said": said}, ensure_ascii=False) + "\n"
    try:
        conn.settimeout(WRITE_S)
        conn.sendall(line.encode("utf-8"))
    except OSError as exc:
        _log.debug("a ui client left before its answer: %s", exc)


def _answered(path: Path) -> bool:
    """Whether something listens at ``path``: a refused dial is a socket a crashed asq left."""
    probe = _unix_socket()
    probe.settimeout(PROBE_S)
    try:
        probe.connect(str(path))
    except (ConnectionRefusedError, FileNotFoundError):
        return False
    except OSError:
        return True  # cannot tell (a full backlog times out): never steal what may be live
    finally:
        probe.close()
    return True


def _unlink_if_same(path: Path, identity: tuple[int, int]) -> bool:
    """Unlink ``path`` only while it is still the file ``identity`` names (device, inode)."""
    try:
        facts = path.lstat()
    except FileNotFoundError:
        return False
    if (facts.st_dev, facts.st_ino) != identity:
        return False
    path.unlink(missing_ok=True)
    return True


def _claim(path: Path) -> tuple[socket.socket, tuple[int, int]]:
    """Bind and listen at ``path``, private (``0600``); returns the socket and the file's identity.

    A socket already there is dialled first: a live one is another asq's and is
    left alone; a dead one is a crashed asq's and is replaced (unlinked only while
    it is still the file that was dialled). Anything else there is not ours to remove.
    """
    facts: os.stat_result | None
    try:
        facts = path.lstat()
    except FileNotFoundError:
        facts = None
    if facts is not None:
        if not stat.S_ISSOCK(facts.st_mode):
            raise _Taken(f"{path} is there and is not a socket — left alone")
        if _answered(path):
            raise _Taken(f"another asq already listens at {path}")
        _unlink_if_same(path, (facts.st_dev, facts.st_ino))
    listener = _unix_socket()
    try:
        listener.bind(str(path))
    except BaseException:
        listener.close()
        raise
    try:
        # chmod after bind, not a umask around it: the umask is the whole process's, and
        # a folder another thread made in that window would come out 0600 — unusable.
        # The window is inside the home's own folder or a private 0700 one.
        os.chmod(path, 0o600)
        listener.listen(8)
        bound = path.lstat()
    except BaseException:
        listener.close()
        path.unlink(missing_ok=True)
        raise
    return listener, (bound.st_dev, bound.st_ino)


# --- the receiver ---------------------------------------------------------------------------------


class UiReceiver:
    """One asq's end of the ui socket: listening from mount to unmount, or saying why not."""

    def __init__(self, app: FleetApp) -> None:
        self._app: weakref.ref[FleetApp] = weakref.ref(app)
        """Weak: the receiver's thread must not keep a quit app alive."""
        self.path: Path | None = None
        """Where it listens; ``None`` while it does not."""
        self.reason: str | None = None
        """Why it does not listen (said once in the log); ``None`` while it does."""
        self._identity: tuple[int, int] | None = None
        """The device and inode of the socket file this receiver bound."""
        self._thread: threading.Thread | None = None
        self._wake: int | None = None
        """The write end of the pipe that wakes the thread: closed, its read end turns readable."""
        self._guard = threading.Lock()
        self._closing = False
        self._dispatching = False
        """An action is on its way to the loop (inside ``call_from_thread``)."""

    @property
    def listening(self) -> bool:
        return self.path is not None and not self._closing

    @property
    def alive(self) -> bool:
        """Whether the receiver's thread is still running."""
        thread = self._thread
        return thread is not None and thread.is_alive()

    def join(self, timeout: float) -> bool:
        """Wait for the receiver's thread to end, at most ``timeout`` seconds; whether it did."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return not self.alive

    def listen(self) -> None:
        """Bind the socket and start the thread — or record why not and say it once."""
        if sys.platform == "win32" or not hasattr(socket, "AF_UNIX"):
            self.reason = "this platform has no unix sockets, so ui actions cannot reach asq"
            _log.debug("no ui receiver: %s", self.reason)
            return
        try:
            path = captain_state.ui_socket_path(create=True)
            listener, identity = _claim(path)
        except (OSError, _Taken) as exc:
            self.decline(str(exc))
            return
        wake_out, wake_in = os.pipe()
        self.path = path
        self._identity = identity
        self._wake = wake_in
        thread = threading.Thread(
            target=self._serve, args=(listener, wake_out), name="asq-ui-receiver", daemon=True
        )
        self._thread = thread
        thread.start()

    def decline(self, reason: str) -> None:
        """Run without a receiver, and say why — once, in the log."""
        self.reason = reason
        _log.warning("asq runs without a ui receiver, so the captain's ui actions cannot reach "
                     "it: %s", reason)  # fmt: skip

    def stop_listening(self) -> None:
        """Unlink the socket if it is still ours, wake the thread, and wait for it to end.

        The thread closes the listening socket itself as it ends, so no socket is
        closed under a thread still selecting on it. It is not waited for while an
        action is on its way to the loop: this runs ON the loop (the app's
        unmount), and ``call_from_thread`` waits for the loop, so the join would
        hold the quit for :data:`JOIN_S` and end nothing. That thread finishes its
        one answer once the loop moves on, and ends itself.
        """
        with self._guard:
            self._closing = True
            busy = self._dispatching
            wake, self._wake = self._wake, None
        # Unlinked BEFORE the thread is woken, while the listening socket is still open:
        # the kernel holds our inode until it closes, so no other asq's socket file can
        # be carrying its number when the two are compared.
        if self.path is not None and self._identity is not None:
            try:
                _unlink_if_same(self.path, self._identity)
            except OSError as exc:
                _log.warning("the ui socket %s could not be removed: %s", self.path, exc)
        if wake is not None:
            os.close(wake)
        thread = self._thread
        if thread is not None and not busy:
            thread.join(JOIN_S)

    # --- the thread ---------------------------------------------------------------------------

    def _serve(self, listener: socket.socket, wake: int) -> None:
        """Accept one connection at a time until quit wakes the thread; then close up."""
        try:
            while True:
                readable, _, _ = select.select([listener.fileno(), wake], [], [])
                if wake in readable:
                    return
                try:
                    conn, _ = listener.accept()
                except OSError:
                    continue  # the client hung up between knocking and being let in
                with conn:
                    try:
                        self._answer(conn, wake)
                    except Exception:
                        _log.warning("the ui receiver could not answer a client", exc_info=True)
        except OSError:
            _log.warning("the ui receiver stopped: its socket failed", exc_info=True)
        finally:
            listener.close()
            os.close(wake)

    def _answer(self, conn: socket.socket, wake: int) -> None:
        try:
            line = _read_line(conn, wake)
            if line is None:
                return
            action, arg = _parse(line)
        except UiRefusal as exc:
            _reply(conn, False, str(exc))
            return
        ok, said = self._dispatch(action, arg)
        _reply(conn, ok, said)

    def _dispatch(self, action: str, arg: str | None) -> tuple[bool, str]:
        """Run the action on the app's thread and wait for what it says."""
        app = self._app()
        with self._guard:
            if self._closing or app is None:
                return False, "asq is quitting"
            self._dispatching = True
        try:
            return app.call_from_thread(_run, action, app, arg)
        except Exception as exc:  # a handler that raised, or a loop that has gone
            _log.warning("ui action %s failed", action, exc_info=True)
            return False, f"error: {type(exc).__name__}: {exc}"
        finally:
            with self._guard:
                self._dispatching = False


# --- the app's handle on its receiver ------------------------------------------------------------

_receivers: weakref.WeakKeyDictionary[FleetApp, UiReceiver] = weakref.WeakKeyDictionary()
"""Each running app's receiver. Weak on the app, and the receiver holds the app weakly too:
a registry that kept a quit app alive would keep everything it ever mounted."""


def listen_for_ui(app: FleetApp) -> UiReceiver:
    """Start ``app``'s receiver (its mount); a reason not to listen is said, never raised."""
    existing = _receivers.get(app)
    if existing is not None:
        return existing
    receiver = UiReceiver(app)
    _receivers[app] = receiver
    try:
        receiver.listen()
    except Exception as exc:  # a bug here must cost the receiver, never the shell
        receiver.decline(f"error: {type(exc).__name__}: {exc}")
    return receiver


def stop_listening_for_ui(app: FleetApp) -> None:
    """Stop ``app``'s receiver (its unmount); nothing to stop is nothing to do."""
    receiver = _receivers.pop(app, None)
    if receiver is not None:
        receiver.stop_listening()


def ui_receiver(app: FleetApp) -> UiReceiver | None:
    """``app``'s receiver, while it has one."""
    return _receivers.get(app)
