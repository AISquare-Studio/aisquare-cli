"""The fleet lifecycle over a FAKE tmux (plan §5.3, §5.7, §7.3; §3.4 to §3.6).

Every test here drives ``services.fleet`` against the real store in an isolated
home and an in-memory :class:`FakeTmux` — no ``claude``, no ``gh``, and no tmux
server except in the one end-to-end test at the bottom, which runs on a private
socket, is skipped when tmux is absent, and kills its server afterwards.

The fake's runner refuses every call, so a method the service uses that the
fake forgot to override fails loudly instead of quietly reaching a real tmux.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import codenames, selfcli
from aisquare.core import store as core_store
from aisquare.core.config import FleetRoleSettings, FleetSettings
from aisquare.core.ids import new_agent_id, new_task_id
from aisquare.core.orchestrator import team_project
from aisquare.core.store import ContextStore, SqliteStore, store_session
from aisquare.core.tmux import (
    _FACTS_FIELDS,
    Completed,
    PaneFacts,
    Runner,
    TmuxError,
    TmuxServer,
    TmuxUnavailable,
    WindowInfo,
)
from aisquare.models import FleetAgent, FleetAgentStatus, ProjectInfo, TeamSession, TeamTask
from aisquare.services import fleet as fleet_service
from aisquare.services import team as team_service
from aisquare.services.fleet import (
    ACTIVITY_WINDOW,
    NUDGE_TEXT,
    PROMPT_TIMEOUT,
    FleetError,
    FleetUnavailable,
    NoSuchAgent,
)

PYTHON_LAUNCHER = "python3"
"""What ``pane_current_command`` reads while ``python -m aisquare launch`` is resolving."""


# --- the fake tmux ---------------------------------------------------------------------


def _facts(pane_id: str, **overrides: object) -> PaneFacts:
    base = PaneFacts(
        pane_id=pane_id,
        width=200,
        height=50,
        cursor_x=0,
        cursor_y=0,
        cursor_visible=True,
        alternate_on=False,
        history_size=0,
        dead=False,
        dead_status=None,
        in_mode=False,
        current_command=PYTHON_LAUNCHER,
        title="",
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


class FakeTmux(TmuxServer):
    """A tmux server kept in memory: sessions, windows, panes, and what was typed."""

    def __init__(self) -> None:
        super().__init__("asq-test", runner=self._unexpected)
        self.installed = True
        """Whether ``tmux`` is on PATH — every fake call checks, as the real server does."""
        self.honours_exit = True
        """Whether a typed ``/exit`` makes the pane die with status 0 (a real agent does)."""
        self.status_lag = 0
        """Reads for which a dead pane's ``dead_status`` is still empty — measured on
        tmux 3.4 (its own -vv server log): one poll can see ``dead=1`` while
        ``pane_dead_status`` expands to ``''``; the status lands a beat later."""
        self.fail_input = False
        self.running = True
        """Whether a server is listening on the socket. False = the shape a hand-run
        `tmux kill-server` leaves: binary present, every question answered empty —
        and every command that WRITES failing, which is why the gate below is
        applied to `_input`, `kill_window`, `kill_session` and `rename_session`
        too. Without that, `send_literal(pane, '/exit')` succeeded on a server
        that was not running and `honours_exit` marked the pane dead with status
        0: the fake simulated an agent gracefully obeying `/exit` on a dead
        server, so a regression that typed into an unreachable server read as a
        clean exit."""
        self.answers_raises: str | None = None
        self.exec_unavailable = False
        self.refuse_kills = False
        """Every KILL is refused while everything SURVIVES: the pane, its window and
        its session all stay exactly where they were. A wedged server answering a
        `kill-window` with a 30 s timeout is this shape, and so is a `kill-session`
        that loses a race with a server reload. It is the one state that separates
        "the kill happened" from "the kill was attempted" — which `stop` used to
        collapse into a suppressed `TmuxError` (review of #121, round 9)."""
        self.socket_denied = False
        """The socket is THERE but this user may not open it: tmux exits 1 with
        `(Permission denied)` — the same exit code as an absent server. `reachable()`
        raises `TmuxError`; `answers()` returns False (its never-raises contract)."""
        """`which` finds the client but running it fails (`FileNotFoundError` from
        `subprocess.run` — a shim whose interpreter is gone): `binary()` succeeds,
        `reachable()` raises `TmuxUnavailable`, `answers()` returns False."""
        """Set to make `answers()` RAISE `TmuxError` — the real one does not catch
        that (only `TmuxUnavailable`), and a 30 s `_COMMAND_TIMEOUT` on a wedged
        server is exactly this shape. The wedged server is the case `shutdown`
        exists for, so it has to be reachable from a test."""
        self.server_killed = False
        self.killed_sessions: list[str] = []
        self.per_socket: dict[str, TmuxServer] = {}
        """Sockets that answer with a DIFFERENT server than this fake (see the
        `tmux` fixture): one gone, one healthy, which is the state `shutdown` and
        the doctor decide per socket and nothing could otherwise reproduce."""
        self.sessions: dict[str, list[WindowInfo]] = {}
        self.facts: dict[str, PaneFacts] = {}
        self.output_at: dict[str, datetime] = {}
        """When each pane last printed — what ``#{window_activity}`` reports."""
        self.pids: dict[str, int] = {}
        """The pid tmux started in each pane — what ``pane_pid`` answers. Unset
        means tmux cannot say, which the identity check reads as "cannot tell"."""
        self.spawned: list[dict[str, object]] = []
        self.typed: list[tuple[str, str, str]] = []
        """``(pane_id, kind, text)`` with kind ``literal`` / ``paste`` / ``key``."""
        self.killed: list[str] = []
        self.renamed: list[tuple[str, str]] = []
        self._counter = 0

    @staticmethod
    def _unexpected(argv: Sequence[str], stdin: bytes | None) -> Completed:
        raise AssertionError(f"the fake tmux was asked to really run: {list(argv)}")

    # -- availability --
    def binary(self) -> str:
        if not self.installed:
            raise TmuxUnavailable("tmux is not installed (fake)")
        return "/fake/tmux"

    def version(self) -> tuple[int, int] | None:
        self.binary()
        return (3, 7)

    def conf_path(self) -> Path:
        return Path("/fake/fleet-tmux.conf")

    def run(self, *args: str, stdin: bytes | None = None) -> str:
        """The one raw command the service sends: last-output times for every pane."""
        self.binary()
        if list(args) != ["list-panes", "-a", "-F", fleet_service._ACTIVITY_FORMAT]:
            raise AssertionError(f"the fake tmux was asked to really run: {list(args)}")
        if not self.running:
            return ""
        return "".join(
            f"{pane_id}{fleet_service._SEP}{int(when.timestamp())}\n"
            for pane_id, when in self.output_at.items()
            if pane_id in self.facts
        )

    # -- sessions and windows --
    def answers(self) -> bool:
        """Whether a server is listening — ``running``, as the real one asks tmux.

        Faithful to :meth:`aisquare.core.tmux.TmuxServer.answers`: an
        unavailable BINARY is not a server that answered (it returns False
        rather than raising), a server that is up answers True, and a socket
        with no server answers False — ``running = False`` is that state, the
        shape a hand-run ``tmux kill-server`` leaves. ``answers_raises`` covers
        the third case the real one has and its docstring's "never raises" does
        not: a bare ``TmuxError`` from a timeout.

        ``_unreachable_server()`` (below) remains the sharper instrument for the
        unreachable-server paths — it drives the REAL ``TmuxServer`` over a
        refusing runner, so every swallow of a non-zero exit is exercised and a
        new ``TmuxServer`` method is covered automatically.
        """
        try:
            return self.reachable()
        except TmuxError:
            return False

    def _read(self) -> bool:
        """What a LENIENT read sees: True when the server answered this query.

        False is tmux's non-zero exit, which every lenient wrapper reports as its
        empty answer (`[]` / `None` / `False`) — no server on the socket
        (`running`), or a socket this user may not open (`socket_denied`, exit 1
        with `Permission denied` on a server that is perfectly alive). A client
        that could not RUN (`installed`, `exec_unavailable`) or a server that
        never answered at all (`answers_raises`, the wedged 30 s timeout) raises
        out of the runner instead, and the real lenient wrappers propagate that
        rather than swallowing it.

        The fake gated only on `running`, so `socket_denied` left it answering
        with the pane's facts where the real client answers non-zero — the one
        state that separates "tmux says the pane is gone" from "tmux could not
        be asked", and therefore the state no test could express (review of #121,
        round 9 verification).
        """
        self.binary()
        if self.exec_unavailable:
            raise TmuxUnavailable("tmux is not runnable: bad interpreter (fake)")
        if self.answers_raises is not None:
            raise TmuxError(self.answers_raises)
        return self.running and not self.socket_denied

    def reachable(self) -> bool:
        """The raising probe: an unavailable client — missing (`installed`) or
        failing at EXECUTION (`exec_unavailable`: the binary is found but its
        interpreter is gone) — is `TmuxUnavailable`; a socket that is there but
        refuses this user (`socket_denied`) is a `TmuxError`; only "no server" is
        False."""
        if self.socket_denied:
            self.binary()
            if self.exec_unavailable:
                raise TmuxUnavailable("tmux is not runnable: bad interpreter (fake)")
            if self.answers_raises is not None:
                raise TmuxError(self.answers_raises)
            raise TmuxError(f"error connecting to /fake/tmux-0/{self.socket} (Permission denied)")
        return self._read()

    def kill_server(self) -> None:
        """As the real one: ``run("kill-server")``, which FAILS with no server up.

        Measured in ``test_live_kill_session_then_kill_server``: a second
        ``kill_server()`` raises ``no server running``. Faithfulness matters here
        because a fleet whose last window was killed has already lost its server
        (``BUNDLED_CONF`` does not set ``exit-empty off``), so the failing kill is
        the ORDINARY path, not the exotic one.
        """
        self.binary()
        if not self.running:
            raise TmuxError(f"no server running on /fake/tmux-0/{self.socket}")
        self.server_killed = True
        self.running = False
        self.sessions.clear()
        self.facts.clear()

    def list_sessions(self) -> list[str]:
        return list(self.sessions) if self._read() else []

    def has_session(self, name: str) -> bool:
        return self._read() and name in self.sessions

    def spawn_window(
        self,
        session: str,
        *,
        name: str,
        cwd: Path,
        command: Sequence[str],
        env: Mapping[str, str] | None = None,
        width: int = 200,
        height: int = 50,
    ) -> WindowInfo:
        self.binary()
        # The real one runs `new-session -d`, which STARTS a server when none is
        # listening and exits 0 — so "the next asq / fleet spawn starts a fresh
        # server", stated in the command's docstring and in docs/fleet.md, is
        # exercisable rather than merely claimed.
        self.running = True
        self._counter += 1
        pane_id = f"%{self._counter}"
        window = WindowInfo(
            session=session,
            window_id=f"@{self._counter}",
            name=name,
            pane_id=pane_id,
            dead=False,
            dead_status=None,
            current_command=PYTHON_LAUNCHER,
            activity=False,
        )
        self.sessions.setdefault(session, []).append(window)
        self.facts[pane_id] = _facts(pane_id)
        self.spawned.append(
            {
                "session": session,
                "name": name,
                "cwd": cwd,
                "command": list(command),
                "env": dict(env or {}),
            }
        )
        return window

    def list_windows(self, session: str) -> list[WindowInfo]:
        if not self._read():
            return []
        return [self._current(window) for window in self.sessions.get(session, [])]

    def _current(self, window: WindowInfo) -> WindowInfo:
        facts = self.facts[window.pane_id]
        dead_status = facts.dead_status
        if facts.dead and self.status_lag > 0:
            self.status_lag -= 1  # the gap: dead, but no status on this read yet
            dead_status = None
        return replace(
            window,
            dead=facts.dead,
            dead_status=dead_status,
            current_command=facts.current_command,
            # Faithful to tmux 3.7c: the flag is set from creation and never clears
            # on a detached session, so it must not be what the service reads.
            activity=True,
        )

    def pane_facts(self, pane_id: str) -> PaneFacts | None:
        return self.facts.get(pane_id) if self._read() else None

    # The strict twins the shutdown paths use: each is the lenient answer UNLESS
    # the server could not be asked, and that single distinction is `reachable()`
    # already — it raises for a denied socket (`socket_denied`), a wedged one
    # (`answers_raises`) and an unrunnable client (`exec_unavailable`), and is
    # False (never raises) for a server that is simply not running. Deriving all
    # four from it keeps the fake from drifting from the real contract.
    def sessions_or_raise(self) -> list[str]:
        self.reachable()
        return self.list_sessions()

    def has_session_or_raise(self, name: str) -> bool:
        self.reachable()
        return self.has_session(name)

    def windows_or_raise(self, session: str) -> list[WindowInfo]:
        self.reachable()
        return self.list_windows(session)

    def pane_facts_or_raise(self, pane_id: str) -> PaneFacts | None:
        self.reachable()
        return self.pane_facts(pane_id)

    def pane_pid(self, pane_id: str) -> int | None:
        self.binary()
        if pane_id not in self.facts:
            return None
        return self.pids.get(pane_id)

    def kill_window(self, pane_id: str) -> None:
        self.binary()
        self._require_server()
        if self.refuse_kills:
            raise TmuxError("kill-window failed (fake)")
        if pane_id not in self.facts:
            raise TmuxError(f"can't find pane: {pane_id}")
        self.vanish(pane_id)
        self.killed.append(pane_id)
        # As tmux does: a session whose last window is killed goes with it (and,
        # without `exit-empty off`, the server follows when it held nothing else).
        for name, windows in list(self.sessions.items()):
            if not windows:
                del self.sessions[name]
        if not self.sessions:
            self.running = False

    def kill_session(self, session: str) -> None:
        self.binary()
        self._require_server()
        if self.refuse_kills:
            raise TmuxError("kill-session failed (fake)")
        if session not in self.sessions:
            raise TmuxError(f"can't find session: {session}")
        for window in self.sessions.pop(session, []):
            self.facts.pop(window.pane_id, None)
        self.killed_sessions.append(session)
        if not self.sessions:
            self.running = False

    def rename_session(self, old: str, new: str) -> None:
        self.binary()
        self._require_server()
        if old not in self.sessions:
            raise TmuxError(f"can't find session: {old}")
        self.sessions[new] = self.sessions.pop(old)
        self.renamed.append((old, new))

    def _require_server(self) -> None:
        """What every WRITE hits when no server is listening: a non-zero exit.

        The real ``kill_window`` / ``kill_session`` / ``rename_session`` /
        ``send_keys`` all route through ``run()``, which raises on a non-zero
        exit, and tmux answers ``error connecting to …`` for each of them — for
        a socket that REFUSED this user (`socket_denied`) exactly as for one
        with no server behind it, which is why both go through `_read`.
        """
        if self._read():
            return
        denied = " (Permission denied)" if self.socket_denied else ""
        raise TmuxError(f"error connecting to /fake/tmux-0/{self.socket}{denied}")

    def attach_argv(self, session: str) -> list[str]:
        return [self.binary(), "-L", self.socket, "attach-session", "-t", f"={session}"]

    # -- input --
    def send_keys(self, pane_id: str, *keys: str) -> None:
        self._input(pane_id, "key", " ".join(keys))

    def send_literal(self, pane_id: str, text: str) -> None:
        self._input(pane_id, "literal", text)
        if text == "/exit" and self.honours_exit:
            self.die(pane_id, 0)

    def paste(self, pane_id: str, text: str) -> None:
        self._input(pane_id, "paste", text)

    def _input(self, pane_id: str, kind: str, text: str) -> None:
        self.binary()
        self._require_server()
        if self.fail_input:
            raise TmuxError("send-keys failed (fake)")
        if pane_id not in self.facts:
            raise TmuxError(f"can't find pane: {pane_id}")
        self.typed.append((pane_id, kind, text))

    # -- what the test does to the world --
    def die(self, pane_id: str, status: int) -> None:
        self.facts[pane_id] = replace(self.facts[pane_id], dead=True, dead_status=status)

    def set_command(self, pane_id: str, command: str) -> None:
        self.facts[pane_id] = replace(self.facts[pane_id], current_command=command)

    def printed(self, pane_id: str, *, ago: timedelta = timedelta(0)) -> None:
        self.output_at[pane_id] = datetime.now(tz=UTC) - ago

    def vanish(self, pane_id: str) -> None:
        self.facts.pop(pane_id, None)
        for windows in self.sessions.values():
            windows[:] = [window for window in windows if window.pane_id != pane_id]


class FakeClock:
    """``_sleep`` / ``_monotonic`` for the service: time passes only when slept."""

    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


# --- fixtures ------------------------------------------------------------------------


@pytest.fixture
def tmux(monkeypatch: pytest.MonkeyPatch) -> FakeTmux:
    """The fake every socket answers with — unless a test registers another.

    ``fleet_service.server`` is the one factory the service uses for every
    socket (``server_for`` routes through it), so one fake covers a
    single-socket fleet. It also USED to mean no service test could produce a
    mixed state across sockets — one gone, one healthy — which is the state
    ``shutdown`` and the doctor decide per socket. A test that needs it puts a
    second server in ``tmux.per_socket["asq-old"]``.
    """
    fake = FakeTmux()

    def factory(config: FleetSettings | None = None) -> TmuxServer:
        socket = (config or fleet_service.settings()).tmux_socket
        return fake.per_socket.get(socket, fake)

    monkeypatch.setattr(fleet_service, "server", factory)
    return fake


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(fleet_service, "_sleep", fake.sleep)
    monkeypatch.setattr(fleet_service, "_monotonic", fake.monotonic)
    return fake


@pytest.fixture
def claude_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A ``claude`` the binary check finds — a shell script, never Claude Code."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text('#!/bin/sh\necho "fake claude: $*"\nread line\nexit 0\n', encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return script


def _git(*args: str, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", "-c", "user.email=fleet@test", "-c", "user.name=fleet", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git("init", "-q", "-b", "main", cwd=path)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=path)
    return path


@pytest.fixture
def project(repo: Path) -> ProjectInfo:
    info = team_project(repo)
    with store_session() as store:
        store.ensure_project(info)
    return info


@pytest.fixture
def plain_project(tmp_path: Path) -> ProjectInfo:
    """A directory that is not a git checkout — a parent of several repos, say."""
    path = tmp_path / "plain"
    path.mkdir()
    info = team_project(path)
    with store_session() as store:
        store.ensure_project(info)
    return info


def _settings(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> FleetSettings:
    config = FleetSettings(**overrides)
    monkeypatch.setattr(fleet_service, "settings", lambda: config)
    return config


def _codename(project: ProjectInfo) -> str:
    with store_session() as store:
        stored = store.get_project(project.id)
    assert stored is not None and stored.codename
    return stored.codename


def _flag(command: Sequence[str], name: str) -> str | None:
    """The value after ``name`` in a command line, or ``None`` when absent."""
    if name not in command:
        return None
    return command[list(command).index(name) + 1]


def _command(tmux: FakeTmux, index: int = -1) -> list[str]:
    command = tmux.spawned[index]["command"]
    assert isinstance(command, list)
    return command


def _board_session(agent: FleetAgent, state: str, *, seen_ago: timedelta = timedelta(0)) -> None:
    """The ``team_session`` row the agent's hooks would have written."""
    assert agent.session_id is not None
    now = datetime.now(tz=UTC)
    with store_session() as store:
        store.upsert_session(
            TeamSession(
                id=agent.session_id,
                project_id=agent.project_id,
                role=agent.role,
                started_at=now - seen_ago,
                last_seen_at=now - seen_ago,
            )
        )
        if seen_ago == timedelta(0):
            store.touch_session(agent.session_id, state=state)
        else:
            assert state == "working", "a stale row can only be inserted in its default state"


def _stale_board_session(agent: FleetAgent, state: str, *, seen_ago: timedelta) -> None:
    """A board row frozen at ``state`` ``seen_ago`` ago — hooks that stopped firing.

    Written as an INSERT on purpose: ``upsert_session`` forces ``state = 'working'``
    on conflict, so a row in any other state can only be created, never updated into
    place. What leaves one behind in the wild: a Stop hook that never ran (an
    unreadable store for a stretch, hooks reinstalled, or the pane's Claude session
    id no longer matching the pinned ``FleetAgent.session_id``).
    """
    assert agent.session_id is not None
    now = datetime.now(tz=UTC)
    with store_session() as store:
        assert store.get_session(agent.session_id) is None, "an INSERT, or the state is lost"
        stored = store.upsert_session(
            TeamSession(
                id=agent.session_id,
                project_id=agent.project_id,
                role=agent.role,
                started_at=now - seen_ago,
                last_seen_at=now - seen_ago,
                state=state,
            )
        )
    assert stored.state == state and now - stored.last_seen_at >= seen_ago


def _add_task(project: ProjectInfo, title: str) -> TeamTask:
    now = datetime.now(tz=UTC)
    with store_session() as store:
        task, _ = store.upsert_task(
            TeamTask(
                id=new_task_id(),
                project_id=project.id,
                key=team_service.task_key(title),
                title=title,
                created_at=now,
                updated_at=now,
            )
        )
    return task


def _events(project: ProjectInfo, kind: str) -> list[str]:
    with store_session() as store:
        return [e.text for e in store.recent_events(project.id, limit=50) if e.kind == kind]


def _coder(project: ProjectInfo, **kwargs: object) -> FleetAgent:
    kwargs.setdefault("worktree", False)
    return fleet_service.spawn(project, "coder", **kwargs).agent  # type: ignore[arg-type]


# --- spawn ---------------------------------------------------------------------------


def test_spawn_manager_builds_the_launch_command_and_records_the_row(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    receipt = fleet_service.spawn(project, "manager")
    agent = receipt.agent

    assert agent.label == "manager" and agent.role == "manager"
    assert receipt.asked_label is None and receipt.notes == []
    assert receipt.tmux_session == f"asq-{_codename(project)}"
    spawned = tmux.spawned[0]
    assert spawned["session"] == receipt.tmux_session and spawned["name"] == "manager"
    command = _command(tmux)
    assert command[:6] == [sys.executable, "-P", "-m", "aisquare", "launch", "manager"]
    assert _flag(command, "--permission-mode") == "auto"
    assert agent.session_id and _flag(command, "--session-id") == agent.session_id
    assert _flag(command, "--name") == "manager"
    assert "--command" not in command, "no --bin given: launch resolves the binary itself"
    assert spawned["env"] == {
        "AISQUARE_FLEET_AGENT": agent.id,
        "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "0",
    }
    assert spawned["cwd"] == project.root and agent.cwd == project.root and not agent.worktree
    assert agent.pane_id == "%1" and agent.binary == "claude" and agent.spawned_by == "user"
    with store_session() as store:
        assert store.fleet_agent_by_label(project.id, "manager") == agent


def test_spawn_with_an_account_carries_the_callers_environment_into_the_window(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`launch --account 1` restores "this shell's" login — inside the window that shell is
    whoever started the server, so the CALLER's view travels with the window."""
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_TMPDIR", raising=False)

    fleet_service.spawn(project, "coder", account="1")

    spawned = tmux.spawned[-1]
    command, env = spawned["command"], spawned["env"]
    assert isinstance(command, list) and isinstance(env, dict)
    # The two variables this process lacks are unset for the child…
    assert command[0].endswith("env")
    assert command[1:5] == ["-u", "CLAUDE_CONFIG_DIR", "-u", "CLAUDE_CODE_TMPDIR"]
    # …then the launcher exactly as it is built without a flag (interpreter switches and all).
    module = command.index("-m")
    assert command[5] == sys.executable
    assert command[module : module + 3] == ["-m", "aisquare", "launch"]
    assert _flag(command, "--account") == "1"
    # …and the aisquare home this process has is set, as an absolute path.
    assert env["AISQUARE_HOME"] == str(Path(os.environ["AISQUARE_HOME"]).absolute())
    assert env["AISQUARE_FLEET_AGENT"]  # the fleet's own variables are still there

    # The control: no --account, no carried environment, the command starts with python.
    fleet_service.spawn(project, "tester")
    plain_command, plain_env = tmux.spawned[-1]["command"], tmux.spawned[-1]["env"]
    assert isinstance(plain_command, list) and isinstance(plain_env, dict)
    assert plain_command[0] == sys.executable  # no env prefix: the launcher comes first…
    assert plain_command.index("-m") == module - 5  # …shaped exactly as the prefixed one after it
    assert "AISQUARE_HOME" not in plain_env


def test_spawn_refuses_a_second_manager(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    fleet_service.spawn(project, "manager")
    with pytest.raises(FleetError, match="already has a manager"):
        fleet_service.spawn(project, "manager")
    assert len(tmux.spawned) == 1, "the refusal must come before any window is created"
    # Negative control: a second agent of another role is welcome.
    assert _coder(project).label == "coder-1"


def test_spawn_refuses_past_the_cap_and_names_the_count(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings(monkeypatch, max_agents_per_project=2)
    _coder(project)
    _coder(project)
    with pytest.raises(FleetError, match=r"2 agents \(max_agents_per_project = 2\)"):
        _coder(project)
    assert len(tmux.spawned) == 2
    # An ended agent frees its slot.
    fleet_service.stop(project, "coder-1", force=True)
    assert _coder(project).label == "coder-1"


def test_spawn_refuses_an_unknown_role_but_accepts_a_bound_one(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(FleetError, match="unknown role 'codr'"):
        fleet_service.spawn(project, "codr")
    assert tmux.spawned == []
    monkeypatch.setattr("aisquare.cli.launch._declared_roles", lambda: {"scribe"})
    receipt = fleet_service.spawn(project, "scribe")
    assert receipt.agent.role == "scribe" and receipt.agent.label == "scribe-1"
    assert _command(tmux)[5] == "scribe"


def test_spawn_refuses_a_worktree_outside_git(
    tmux: FakeTmux, claude_on_path: Path, plain_project: ProjectInfo
) -> None:
    with pytest.raises(
        FleetError,
        match="not a git repository — spawn without --worktree or pick a repo inside it",
    ):
        fleet_service.spawn(plain_project, "coder")  # a coder's default is a worktree
    assert tmux.spawned == []
    receipt = fleet_service.spawn(plain_project, "coder", worktree=False)
    assert receipt.agent.cwd == plain_project.root and not receipt.agent.worktree


def test_spawn_coder_gets_a_worktree_on_the_fleet_branch(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    receipt = fleet_service.spawn(project, "coder")
    agent = receipt.agent
    expected = project.root / ".aisquare-worktrees" / "coder-1"

    assert agent.worktree and agent.cwd == expected and expected.is_dir()
    assert tmux.spawned[0]["cwd"] == expected
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=expected) == (
        f"fleet/{_codename(project)}/coder-1"
    )
    exclude = project.root / ".git" / "info" / "exclude"
    assert ".aisquare-worktrees/" in exclude.read_text(encoding="utf-8").splitlines()
    assert _git("status", "--porcelain", cwd=project.root) == "", "the worktree dir is excluded"
    # A second worktree adds no second exclude line.
    fleet_service.spawn(project, "coder")
    assert exclude.read_text(encoding="utf-8").splitlines().count(".aisquare-worktrees/") == 1


def test_spawn_with_a_task_names_the_label_and_the_branch_after_it(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    task = _add_task(project, "Wire the auth flow!")
    short = task.id.removeprefix("tsk_")[:8]
    receipt = fleet_service.spawn(project, "coder", task_id=task.id[:12])

    assert receipt.agent.label == f"coder-{short}"
    assert receipt.agent.task_id == task.id, "the full id is recorded, not the prefix given"
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=receipt.agent.cwd) == (
        f"fleet/{_codename(project)}/{short}-wire-the-auth-flow"
    )
    with pytest.raises(FleetError, match="no task matches 'tsk_nope'"):
        fleet_service.spawn(project, "coder", task_id="tsk_nope")


def test_a_reused_worktree_is_put_on_the_branch_this_spawn_asked_for(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """A label outlives one task, so the tree it names may sit on the PREVIOUS task's
    branch — and an agent spawned for task B must not commit to task A's branch."""
    first_task = _add_task(project, "First job")
    first = fleet_service.spawn(project, "coder", label="coder-auth", task_id=first_task.id).agent
    was_on = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=first.cwd)
    fleet_service.stop(project, "coder-auth", force=True)  # ending frees the label (§5.7)

    next_task = _add_task(project, "Second job")
    receipt = fleet_service.spawn(project, "coder", label="coder-auth", task_id=next_task.id)

    assert receipt.agent.cwd == first.cwd, "the same label reuses the tree"
    short = next_task.id.removeprefix("tsk_")[:8]
    wanted = f"fleet/{_codename(project)}/{short}-second-job"
    assert wanted != was_on
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=receipt.agent.cwd) == wanted
    assert any(f"it was on {was_on}, now on {wanted}" in note for note in receipt.notes)
    # Negative control: reused for the SAME task, nothing is switched.
    fleet_service.stop(project, "coder-auth", force=True)
    again = fleet_service.spawn(project, "coder", label="coder-auth", task_id=next_task.id)
    assert any(f"already on {wanted}" in note for note in again.notes)
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=again.agent.cwd) == wanted


def test_a_reused_worktree_with_uncommitted_work_on_another_branch_is_refused(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """Switching branches over somebody's uncommitted work is not this code's call —
    and neither is leaving the agent on the wrong branch."""
    first_task = _add_task(project, "First job")
    first = fleet_service.spawn(project, "coder", label="coder-auth", task_id=first_task.id).agent
    (first.cwd / "wip.txt").write_text("half a change\n", encoding="utf-8")
    fleet_service.stop(project, "coder-auth", force=True)
    next_task = _add_task(project, "Second job")

    with pytest.raises(FleetError, match="holds uncommitted work on fleet/"):
        fleet_service.spawn(project, "coder", label="coder-auth", task_id=next_task.id)
    assert len(tmux.spawned) == 1, "refused before any window was started"
    assert (first.cwd / "wip.txt").exists(), "the work is left exactly where it was"
    # Negative control: the same dirty tree, reused for its OWN branch, is fine.
    receipt = fleet_service.spawn(project, "coder", label="coder-auth", task_id=first_task.id)
    assert receipt.agent.cwd == first.cwd and (first.cwd / "wip.txt").exists()


def test_a_worktree_is_not_checked_out_from_under_an_agent_whose_row_lands_late(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_refuse_occupied_worktree`` reads live ROWS, and a spawn's row is written only
    after its tmux window exists — so the loser of a label race can arrive at
    ``_reuse_worktree`` with no holder in sight and ``git checkout`` the winner's tree
    onto its own branch. ``_relabel`` refuses the second ROW afterwards, but it cannot
    undo a checkout: the first agent goes on committing to the second spawn's branch.
    One more look, in the last moment before the checkout, catches the row that landed
    while the branch queries were running.
    """
    first_task = _add_task(project, "First job")
    first = fleet_service.spawn(project, "coder", label="coder-auth", task_id=first_task.id).agent
    on_first = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=first.cwd)
    with store_session() as store:
        store.end_fleet_agent(first.id)  # the pre-check sees a free label and a free tree
    second_task = _add_task(project, "Second job")
    real_git = fleet_service._git

    def revive_during_the_dirty_check(cwd: Path, *args: str) -> object:
        if args[:2] == ("status", "--porcelain"):  # the winner's row lands here
            with store_session() as store:
                store.upsert_fleet_agent(first.model_copy(update={"ended_at": None}))
        return real_git(cwd, *args)

    monkeypatch.setattr(fleet_service, "_git", revive_during_the_dirty_check)
    with pytest.raises(FleetError, match=r"is the live worktree of coder-auth \(agt_"):
        fleet_service.spawn(project, "coder", label="coder-auth", task_id=second_task.id)

    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=first.cwd) == on_first, (
        "the live agent's tree is still on the live agent's branch"
    )
    assert len(tmux.spawned) == 1, "refused before a second window existed"
    # Negative control: with no holder in the way the same reuse switches the branch,
    # as test_a_reused_worktree_is_put_on_the_branch_this_spawn_asked_for requires.
    monkeypatch.setattr(fleet_service, "_git", real_git)
    with store_session() as store:
        store.end_fleet_agent(first.id)
    receipt = fleet_service.spawn(project, "coder", label="coder-auth", task_id=second_task.id)
    assert receipt.agent.cwd == first.cwd
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=first.cwd) != on_first


def test_a_reused_worktree_on_a_detached_head_is_called_detached(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """``git rev-parse --abbrev-ref HEAD`` on a detached HEAD exits 0 and prints the
    literal ``HEAD`` (measured with the git in this environment), so the failure
    branch the wording was written for is never taken: the refusal used to say "on
    HEAD" and the note "it was on HEAD", neither of which is a branch.
    """
    first_task = _add_task(project, "First job")
    first = fleet_service.spawn(project, "coder", label="coder-auth", task_id=first_task.id).agent
    fleet_service.stop(project, "coder-auth", force=True)  # ending frees the label (§5.7)
    _git("checkout", "-q", "--detach", cwd=first.cwd)
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=first.cwd) == "HEAD"
    (first.cwd / "wip.txt").write_text("half a change\n", encoding="utf-8")
    second_task = _add_task(project, "Second job")

    with pytest.raises(FleetError, match="holds uncommitted work on a detached HEAD"):
        fleet_service.spawn(project, "coder", label="coder-auth", task_id=second_task.id)

    # The same wording carries the reuse note when the detached tree is clean.
    (first.cwd / "wip.txt").unlink()
    receipt = fleet_service.spawn(project, "coder", label="coder-auth", task_id=second_task.id)
    on_second = fleet_service.branch_name(
        _codename(project), task_id=second_task.id, title="Second job"
    )
    assert any(f"it was on a detached HEAD, now on {on_second}" in n for n in receipt.notes)
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=first.cwd) == on_second
    # Negative control: a real branch is named as a branch, never as detached.
    fleet_service.stop(project, "coder-auth", force=True)
    (first.cwd / "wip.txt").write_text("half a change\n", encoding="utf-8")
    with pytest.raises(FleetError, match=f"holds uncommitted work on {on_second}"):
        fleet_service.spawn(project, "coder", label="coder-auth", task_id=first_task.id)


def test_spawn_permission_mode_flag_beats_role_config_beats_default(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings(
        monkeypatch,
        roles={"coder": FleetRoleSettings(permission_mode="acceptEdits", worktree=False)},
    )
    _coder(project)
    assert _flag(_command(tmux), "--permission-mode") == "acceptEdits", "role config"
    _coder(project, permission_mode="plan")
    assert _flag(_command(tmux), "--permission-mode") == "plan", "the flag wins"
    _coder(project, permission_mode="")
    assert "--permission-mode" not in _command(tmux), "empty string = no flag"
    fleet_service.spawn(project, "tester")
    assert _flag(_command(tmux), "--permission-mode") == "auto", "built-in default"


def test_spawn_carries_role_extra_args_then_the_callers_args(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    fleet_service.spawn(project, "reviewer", worktree=False, agent_args=["--model", "opus"])
    command = _command(tmux)
    assert "--restricted" in command, "the reviewer's built-in extra arg (§3.6)"
    assert command[-2:] == ["--model", "opus"]
    assert command.index("--restricted") < command.index("--model")


def test_spawn_respects_a_caller_supplied_session_id(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    given = "cccc3333-0000-0000-0000-000000000000"
    agent = _coder(project, agent_args=["--session-id", given])
    command = _command(tmux)
    assert command.count("--session-id") == 1 and _flag(command, "--session-id") == given
    assert agent.session_id == given


def test_spawn_records_no_session_for_a_binary_that_takes_no_session_id(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    receipt = fleet_service.spawn(project, "coder", worktree=False, binary=sys.executable)
    command = _command(tmux)
    assert receipt.agent.session_id is None and "--session-id" not in command
    assert command[6:8] == ["--command", sys.executable], "an explicit --bin reaches launch"
    assert receipt.agent.binary == sys.executable
    assert any("no board join" in note for note in receipt.notes)
    assert fleet_service.status_of(receipt.agent).detail == "no hooks"


def test_spawn_forwards_a_bound_binary_the_tmux_server_cannot_see(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fleet window re-resolves its binary in the tmux SERVER's environment.

    That environment is not this shell's: `core/tmux.py` spawns with
    `untraced_env()` and passes two per-window keys, so `AISQUARE_BIN_<ROLE>`
    and `AISQUARE_AGENT_BIN` never arrive. Forwarding only an explicit `--bin`
    left the row naming `claude2` while the pane silently ran `claude` — and
    decided the role's binary-keyed flags against the wrong executable.
    `docs/fleet.md` promises the variable works for a fleet launch.
    """
    other = claude_on_path.with_name("claude2")
    other.write_text(claude_on_path.read_text(encoding="utf-8"), encoding="utf-8")
    other.chmod(0o755)
    monkeypatch.setenv("AISQUARE_BIN_CODER", "claude2")

    receipt = fleet_service.spawn(project, "coder", worktree=False)
    command = _command(tmux)
    assert _flag(command, "--command") == "claude2", "the binding reaches the window"
    assert receipt.agent.binary == "claude2", "and the row and the pane agree"


def test_spawn_leaves_the_default_binary_to_the_window(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """Only a CHOSEN binary is forwarded: `resolution.source == "default"` means
    nothing asked for anything, and `launch` resolving it itself keeps the fleet's
    command line the shortest true one."""
    fleet_service.spawn(project, "coder", worktree=False)
    assert "--command" not in _command(tmux)


def test_spawn_suffixes_a_label_a_live_agent_holds(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    first = fleet_service.spawn(project, "coder", worktree=False, label="coder-auth")
    second = fleet_service.spawn(project, "coder", worktree=False, label="coder-auth")
    assert first.agent.label == "coder-auth" and first.notes == []
    assert second.agent.label == "coder-auth-2" and second.asked_label == "coder-auth"
    assert any("'coder-auth-2'" in note for note in second.notes)
    # An ended agent frees its label.
    fleet_service.stop(project, "coder-auth", force=True)
    third = fleet_service.spawn(project, "coder", worktree=False, label="coder-auth")
    assert third.agent.label == "coder-auth" and third.notes == []


def test_spawn_retries_the_label_when_the_live_index_trips(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The race: another spawn takes the label between ``next_label`` and the write."""
    _coder(project, label="coder-auth")
    real = fleet_service.next_label
    calls: list[str | None] = []

    def racing(
        project_: ProjectInfo,
        role: str,
        *,
        wanted: str | None = None,
        task_id: str | None = None,
        store: object = None,
    ) -> str:
        calls.append(wanted)
        if len(calls) == 1:
            return "coder-auth"  # looked free a moment ago
        return real(project_, role, wanted=wanted, task_id=task_id, store=store)  # type: ignore[arg-type]

    monkeypatch.setattr(fleet_service, "next_label", racing)
    receipt = fleet_service.spawn(project, "coder", worktree=False, label="coder-auth")

    assert receipt.agent.label == "coder-auth-2"
    assert any("was taken while starting" in note for note in receipt.notes)
    assert tmux.killed == [], "the window stays; only the row's label changed"
    with store_session() as store:
        labels = {agent.label for agent in store.fleet_agents(project.id, live_only=True)}
    assert labels == {"coder-auth", "coder-auth-2"}


def test_spawn_kills_the_window_when_no_label_can_be_recorded(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manager race has no suffix to fall back on: the second window must not linger."""
    fleet_service.spawn(project, "manager")
    with store_session() as store:
        store.end_fleet_agent(store.fleet_agents(project.id)[0].id)  # looks free to the pre-check
    original = tmux.spawn_window

    def spawn_then_revive(*args: object, **kwargs: object) -> WindowInfo:
        window = original(*args, **kwargs)  # type: ignore[arg-type]
        with store_session() as store:  # the other spawn wins the row before we write ours
            first = store.fleet_agents(project.id)[0]
            store.upsert_fleet_agent(first.model_copy(update={"ended_at": None}))
        return window

    monkeypatch.setattr(tmux, "spawn_window", spawn_then_revive)
    with pytest.raises(FleetError, match="already has a manager"):
        fleet_service.spawn(project, "manager")
    assert tmux.killed == ["%2"]


def test_spawn_never_puts_a_second_live_agent_in_one_worktree(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The race of the test above, with a worktree. The tree is named after the label
    this spawn LOST, so a suffixed row would leave two live agents editing one checkout
    on one branch — and the race has two halves, guarded in two places.
    """
    first = fleet_service.spawn(project, "coder", label="coder-auth").agent
    real = fleet_service.next_label
    racing = {"on": False}

    def once(
        project_: ProjectInfo,
        role: str,
        *,
        wanted: str | None = None,
        task_id: str | None = None,
        store: object = None,
    ) -> str:
        if racing["on"]:
            racing["on"] = False
            return "coder-auth"  # looked free a moment ago
        return real(project_, role, wanted=wanted, task_id=task_id, store=store)  # type: ignore[arg-type]

    monkeypatch.setattr(fleet_service, "next_label", once)

    # Half one: the winner's row is already there — refused before the tree is
    # touched at all (a reuse would also check ITS branch out from under it).
    racing["on"] = True
    with pytest.raises(FleetError, match=r"is the live worktree of coder-auth \(agt_"):
        fleet_service.spawn(project, "coder", label="coder-auth")
    assert len(tmux.spawned) == 1 and tmux.killed == []

    # Half two: the winner's row lands only after this window exists.
    fleet_service.stop(project, "coder-auth", force=True)  # frees the label; the tree stays
    tmux.killed.clear()  # that stop's own kill; what matters below is the spawn's
    original = tmux.spawn_window

    def spawn_then_revive(*args: object, **kwargs: object) -> WindowInfo:
        window = original(*args, **kwargs)  # type: ignore[arg-type]
        with store_session() as store:  # the other spawn wins the row before we write ours
            store.upsert_fleet_agent(first.model_copy(update={"ended_at": None}))
        return window

    monkeypatch.setattr(tmux, "spawn_window", spawn_then_revive)
    with pytest.raises(FleetError, match="refused rather than made to share a checkout"):
        fleet_service.spawn(project, "coder", label="coder-auth")
    assert tmux.killed == ["%2"], "the second window is killed, not left in the first tree"
    with store_session() as store:
        live = store.fleet_agents(project.id, live_only=True)
    assert [(agent.label, agent.cwd) for agent in live] == [("coder-auth", first.cwd)]

    # The other direction: no worktree, no checkout to share — the same collision is
    # recorded under a suffixed label, as test_spawn_retries_the_label... requires.
    monkeypatch.setattr(tmux, "spawn_window", original)
    racing["on"] = True
    second = fleet_service.spawn(project, "coder", worktree=False, label="coder-auth")
    assert second.agent.label == "coder-auth-2" and second.agent.cwd == project.root
    assert tmux.killed == ["%2"], "no further window was killed"


def test_spawn_kills_the_window_when_the_store_refuses_the_row(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store that refuses for any reason but a taken label — locked past its busy
    timeout, or damaged — must not leave a live agent no row knows about."""
    original = tmux.spawn_window

    def wedged() -> object:
        raise sqlite3.OperationalError("database is locked")

    def spawn_then_wedge(*args: object, **kwargs: object) -> WindowInfo:
        window = original(*args, **kwargs)  # type: ignore[arg-type]
        monkeypatch.setattr(fleet_service, "store_session", wedged)
        return window

    monkeypatch.setattr(tmux, "spawn_window", spawn_then_wedge)
    with pytest.raises(FleetError, match="could not record the agent"):
        _coder(project)

    assert tmux.killed == ["%1"], "the window was killed, not left running unrecorded"
    monkeypatch.setattr(fleet_service, "store_session", store_session)
    with store_session() as store:
        assert store.fleet_agents(project.id) == []
    # Negative control: with the store answering, the same spawn records and lives.
    monkeypatch.setattr(tmux, "spawn_window", original)
    assert _coder(project).label == "coder-1"
    assert tmux.killed == ["%1"], "no second window was killed"


def test_a_spawn_that_races_past_the_cap_backs_its_row_out(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-check and the insert are different transactions with a worktree and a
    tmux window between them, so the count is settled again at the write: the row
    past the cap backs out and its window is killed."""
    _settings(monkeypatch, max_agents_per_project=2)
    _coder(project)
    original = tmux.spawn_window

    def spawn_then_fill(*args: object, **kwargs: object) -> WindowInfo:
        window = original(*args, **kwargs)  # type: ignore[arg-type]
        with store_session() as store:  # a parallel spawn takes the last slot
            store.upsert_fleet_agent(
                FleetAgent(
                    id=new_agent_id(),
                    project_id=project.id,
                    label="coder-racer",
                    role="coder",
                    pane_id="%99",
                    cwd=project.root,
                    created_at=datetime.now(tz=UTC) - timedelta(seconds=1),
                )
            )
        return window

    monkeypatch.setattr(tmux, "spawn_window", spawn_then_fill)
    with pytest.raises(FleetError, match=r"3 live agents \(max_agents_per_project = 2\)"):
        _coder(project)

    assert tmux.killed == ["%2"], "the window of the row that backed out is killed"
    with store_session() as store:
        live = {agent.label for agent in store.fleet_agents(project.id, live_only=True)}
    assert live == {"coder-1", "coder-racer"}, "the cap holds"
    # Negative control: the same write under a cap with room records and lives.
    monkeypatch.setattr(tmux, "spawn_window", original)
    _settings(monkeypatch, max_agents_per_project=4)
    assert _coder(project).label == "coder-2"
    assert tmux.killed == ["%2"], "no second window was killed"


def test_spawn_refuses_the_reserved_manager_label_for_other_roles(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    with pytest.raises(FleetError, match="reserved for the manager"):
        _coder(project, label="manager")
    assert tmux.spawned == []


def test_spawn_manager_ignores_another_label_and_says_so(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    receipt = fleet_service.spawn(project, "manager", label="boss")
    assert receipt.agent.label == "manager" and receipt.asked_label == "boss"
    assert receipt.notes == ["the manager is always labelled 'manager' (asked: 'boss')"]


@pytest.mark.parametrize("command", ["claude", "node", "aider"])
def test_the_agent_is_running_when_the_foreground_command_is_neither_shell_nor_launcher(
    command: str,
) -> None:
    assert fleet_service._agent_running(command)


@pytest.mark.parametrize(
    "command", ["", "  ", "sh", "bash", "zsh", "tmux", "python3", "python3.13"]
)
def test_the_agent_is_not_running_behind_a_shell_the_launcher_or_a_pane_before_its_exec(
    command: str,
) -> None:
    assert not fleet_service._agent_running(command)


def test_spawn_types_the_prompt_once_the_agent_is_up(
    tmux: FakeTmux,
    clock: FakeClock,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    polls = 0
    original = tmux.pane_facts

    def coming_up(pane_id: str) -> PaneFacts | None:
        nonlocal polls
        polls += 1
        if polls == 3:
            tmux.set_command(pane_id, "claude")  # the launcher exec'd the agent
        return original(pane_id)

    monkeypatch.setattr(tmux, "pane_facts", coming_up)
    receipt = fleet_service.spawn(project, "coder", worktree=False, prompt="start on tsk_1")
    pane = receipt.agent.pane_id
    assert tmux.typed == [(pane, "paste", "start on tsk_1"), (pane, "key", "Enter")]
    assert receipt.notes == []
    assert 0 < clock.now < PROMPT_TIMEOUT, "waited for the agent, not for the timeout"
    assert polls == 3, "typed as soon as the agent was up"
    # Negative control: no prompt, nothing typed.
    tmux.typed.clear()
    _coder(project)
    assert tmux.typed == []


def test_spawn_prompt_waits_out_the_pane_before_its_exec(
    tmux: FakeTmux,
    clock: FakeClock,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Right after creation a pane reads ``tmux``; that is not the agent listening."""
    polls = 0
    original = tmux.pane_facts

    def forking(pane_id: str) -> PaneFacts | None:
        nonlocal polls
        polls += 1
        tmux.set_command(pane_id, "tmux" if polls < 3 else "claude")
        return original(pane_id)

    monkeypatch.setattr(tmux, "pane_facts", forking)
    fleet_service.spawn(project, "coder", worktree=False, prompt="hello")
    assert polls == 3, "the two 'tmux' polls were not mistaken for a running agent"


def test_spawn_prompt_stops_waiting_at_the_timeout_and_types_anyway(
    tmux: FakeTmux, clock: FakeClock, claude_on_path: Path, project: ProjectInfo
) -> None:
    receipt = fleet_service.spawn(project, "coder", worktree=False, prompt="hello")
    assert PROMPT_TIMEOUT <= clock.now < PROMPT_TIMEOUT + 5
    assert any("did not come up within 20 s" in note for note in receipt.notes)
    assert (receipt.agent.pane_id, "paste", "hello") in tmux.typed


def test_a_multi_line_prompt_is_not_typed_before_the_agent_is_up(
    tmux: FakeTmux,
    clock: FakeClock,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``paste-buffer -p`` brackets a paste only for an application that asked for
    bracketed paste mode; past the timeout the foreground is still the launcher, which
    has not, so tmux would replace every LF with a CR and the agent would read N
    submitted messages. Not typed — and the note says what to do instead."""
    prompt = "first line\nsecond line"
    receipt = fleet_service.spawn(project, "coder", worktree=False, prompt=prompt)

    assert tmux.typed == [], "N lines would arrive as N messages"
    assert any("NOT typed" in note and "several lines" in note for note in receipt.notes)
    # Negative control: once the agent IS up it has asked for bracketed paste, and
    # the very same prompt goes in as one paste.
    original = tmux.pane_facts

    def up(pane_id: str) -> PaneFacts | None:
        tmux.set_command(pane_id, "claude")
        return original(pane_id)

    monkeypatch.setattr(tmux, "pane_facts", up)
    second = fleet_service.spawn(project, "coder", worktree=False, prompt=prompt)
    pane = second.agent.pane_id
    assert tmux.typed == [(pane, "paste", prompt), (pane, "key", "Enter")]
    assert second.notes == []


def test_spawn_prompt_is_not_typed_into_a_dead_pane(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = tmux.spawn_window

    def spawn_and_crash(*args: object, **kwargs: object) -> WindowInfo:
        window = original(*args, **kwargs)  # type: ignore[arg-type]
        tmux.die(window.pane_id, 1)
        return window

    monkeypatch.setattr(tmux, "spawn_window", spawn_and_crash)
    receipt = fleet_service.spawn(project, "coder", worktree=False, prompt="hello")
    assert tmux.typed == []
    assert any("exited before the prompt" in note for note in receipt.notes)


def test_the_window_command_carries_the_safe_path_flag_and_not_a_variable(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#81, second manifestation: a window inherits the tmux SERVER's environment,
    so a PYTHONSAFEPATH exported by the spawning ``asq`` never reaches it once the
    server exists (measured 2026-09-05: the manager died with "No module named
    aisquare.__main__" from a repo with its own ``aisquare/``). The guard has to
    travel in the command. And as the ``-P`` flag, not a variable in ``env``:
    ``launch`` execve's the agent with the window's whole environment, and a
    coder's own ``python -m pytest`` must not inherit a changed ``sys.path``."""
    monkeypatch.delenv("PYTHONSAFEPATH", raising=False)
    _coder(project)
    command = _command(tmux)
    assert command[:4] == [sys.executable, "-P", "-m", "aisquare"]
    assert command == selfcli.argv_for(command[4:]), "one builder for every self-invocation"
    env = tmux.spawned[0]["env"]
    assert isinstance(env, dict) and "PYTHONSAFEPATH" not in env


def test_spawn_can_keep_native_agent_teams_on(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings(monkeypatch, disable_native_agent_teams=False)
    agent = _coder(project)
    env = tmux.spawned[0]["env"]
    assert env == {"AISQUARE_FLEET_AGENT": agent.id}


def test_spawn_without_tmux_is_fleet_unavailable(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    tmux.installed = False
    with pytest.raises(FleetUnavailable, match="tmux is not installed"):
        fleet_service.spawn(project, "manager")
    with store_session() as store:
        assert store.fleet_agents(project.id) == []


def test_spawn_without_the_agent_binary_names_it_and_who_chose_it(
    tmux: FakeTmux, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda cmd: None)
    with pytest.raises(FleetError, match=r"'claude' is not on your PATH \(chosen by: default\)"):
        fleet_service.spawn(project, "manager")
    assert tmux.spawned == []


# --- derived state (§5.1) --------------------------------------------------------------


_UNREACHABLE = "error connecting to /tmp/tmux-1000/asq (No such file or directory)"
"""How tmux reports a server it cannot reach: this on stderr, with exit 1.

Measured on tmux 3.7c against a socket name with no server — ``list-panes -a``,
``display-message -p -t %7`` and ``list-sessions`` all answer that way — which is
why ``list_windows`` ([]), ``pane_facts`` (None) and ``_activity_times`` ({}) each
hand back an empty answer without raising.
"""


def _real_server(runner: Runner) -> TmuxServer:
    """The REAL ``TmuxServer`` — every swallow of a non-zero exit intact — over ``runner``."""
    return TmuxServer("asq-test", binary=sys.executable, conf=Path("/fake.conf"), runner=runner)


def _unreachable_server() -> TmuxServer:
    def refusing(argv: Sequence[str], stdin: bytes | None) -> Completed:
        return Completed(1, "", _UNREACHABLE)

    return _real_server(refusing)


_MISMATCH = "protocol version mismatch (client 8, server 7)"
"""tmux's answer when the package was upgraded in place under a running server:
every client call exits 1, and every agent behind that server is alive."""


def _mismatched_server() -> TmuxServer:
    def refusing(argv: Sequence[str], stdin: bytes | None) -> Completed:
        return Completed(1, "", _MISMATCH)

    return _real_server(refusing)


def _answering_server(*sessions: str) -> TmuxServer:
    """A server that answers every question and knows none of the panes asked about."""

    def answering(argv: Sequence[str], stdin: bytes | None) -> Completed:
        args = list(argv)
        if "list-sessions" in args:
            return Completed(0, "".join(f"{name}\n" for name in sessions), "")
        if "display-message" in args:
            # tmux 3.7c's answer for a pane it cannot find: status 0 and every
            # field empty, because display-message's target may fail.
            return Completed(0, "|~|" * (len(_FACTS_FIELDS) - 1) + "\n", "")
        return Completed(0, "", "")  # list-panes -s / -a: the server holds no panes

    return _real_server(answering)


def test_an_unreachable_tmux_server_is_not_read_as_every_pane_gone(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_observe`` promises ``None`` when tmux cannot be asked at all, and an
    unreachable server is exactly that — though nothing raises: its non-zero exit
    reaches ``_observe`` as an empty answer, which used to read as "every pane is
    gone". ``reap`` then ended every live row as ``lost`` for processes that are
    still running, and ``_remove_merged_worktrees`` — the same pass, now seeing
    ``ended_at`` set — removed their clean worktrees out from under them. A stale
    socket path is all it takes (a /tmp sweep, or a ``TMUX_TMPDIR`` that differs
    between the spawning shell and the reaping one).
    """
    agent = fleet_service.spawn(project, "coder").agent
    session = f"asq-{_codename(project)}"
    assert agent.worktree and agent.cwd.is_dir()
    dead = _unreachable_server()
    monkeypatch.setattr(fleet_service, "server", lambda config=None: dead)

    assert fleet_service._observe(dead, session, [agent]) is None, "could not ask"
    status = fleet_service.status_of(agent)
    assert status.state == "unknown" and status.detail == "tmux unavailable"
    report = fleet_service.reap(project)

    assert report.lost == [] and report.ended == []
    assert report.worktrees_removed == [] and agent.cwd.is_dir(), (
        "the row is still live, so the running agent's tree is not a merged leftover"
    )
    with store_session() as store:
        assert [a.id for a in store.fleet_agents(project.id, live_only=True)] == [agent.id]

    # Negative control: a server that ANSWERS and does not name the pane is a real
    # loss, and must still be recorded as one — the refusal above is about silence.
    answering = _answering_server(session)
    monkeypatch.setattr(fleet_service, "server", lambda config=None: answering)
    assert fleet_service._observe(answering, session, [agent]) == {}
    assert fleet_service.status_of(agent).state == "lost"
    assert [a.id for a in fleet_service.reap(project).lost] == [agent.id]


def test_reap_server_down_marks_rows_on_a_silent_server_lost(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fix doctor prescribes after a reboot has to exist.

    Measured on the reporting box: doctor said "10 recorded live but the private
    tmux server 'asq' is not running" and pointed at ``fleet reap``, which
    reconciled 0 — the refusal above is correct by default and left the
    operator with no command that acted. ``--server-down`` is their word that
    the server is gone; the rows on it are then lost, and nothing else changes.
    """
    agent = fleet_service.spawn(project, "coder").agent
    dead = _unreachable_server()
    monkeypatch.setattr(fleet_service, "server", lambda config=None: dead)

    assert fleet_service.reap(project).lost == [], "the default still refuses"
    report = fleet_service.reap(project, server_down=True)
    assert [a.id for a in report.lost] == [agent.id]
    assert report.ended == [], "lost, never ended: no exit status was ever observed"
    with store_session() as store:
        assert store.fleet_agents(project.id, live_only=True) == []


def test_reap_server_down_only_touches_the_socket_that_is_silent(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rows span sockets; the flag vouches for the silent one, not for all of them."""
    _settings(monkeypatch, tmux_socket="asq-old")
    on_old = _coder(project)
    _settings(monkeypatch, tmux_socket="asq-new")
    on_new = _coder(project)
    dead = _unreachable_server()

    def per_socket(config: FleetSettings | None = None) -> TmuxServer:
        return dead if config is not None and config.tmux_socket == "asq-old" else tmux

    monkeypatch.setattr(fleet_service, "server", per_socket)
    report = fleet_service.reap(project, server_down=True)
    assert [a.id for a in report.lost] == [on_old.id]
    with store_session() as store:
        assert [a.id for a in store.fleet_agents(project.id, live_only=True)] == [on_new.id]


def test_server_absent_needs_tmux_to_say_so() -> None:
    """Positive evidence only: the two stderr shapes tmux uses for 'no server'
    (measured on 3.4) — never the bare fact that a client call exited non-zero."""
    assert _unreachable_server().server_absent() is True
    absent_file = _real_server(lambda argv, stdin: Completed(1, "", "no server running on /x\n"))
    assert absent_file.server_absent() is True
    assert _mismatched_server().server_absent() is False
    assert _answering_server("asq-a").server_absent() is False

    def raising(argv: Sequence[str], stdin: bytes | None) -> Completed:
        raise TmuxUnavailable("tmux is not installed")

    assert _real_server(raising).server_absent() is False, "no binary: no evidence"


def test_reap_server_down_leaves_a_server_with_a_protocol_mismatch_alone(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of the first version: the predicate was ``not answers()``, which is
    also False for a protocol mismatch (tmux upgraded in place, old server still
    running) and for a wedged server — with the flag, every live row was ended
    for agents still working. The word may act only where tmux says no server."""
    agent = _coder(project)
    monkeypatch.setattr(fleet_service, "server", lambda config=None: _mismatched_server())

    report = fleet_service.reap(project, server_down=True)

    assert report.lost == [] and report.ended == []
    with store_session() as store:
        assert [a.id for a in store.fleet_agents(project.id, live_only=True)] == [agent.id]


def test_reap_server_down_asks_each_socket_once_across_every_project(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    plain_project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One probe per socket per sweep — not one per project — so a server coming
    up mid-``reap --all`` cannot mark the first projects' rows lost and keep the
    rest: one command, one answer. (Review of the first version.)"""
    here = _coder(project)
    there = fleet_service.spawn(plain_project, "coder", worktree=False).agent
    probes: list[Sequence[str]] = []

    def refusing(argv: Sequence[str], stdin: bytes | None) -> Completed:
        if "display-message" in argv and "#{version}" in argv:
            probes.append(argv)
        return Completed(1, "", _UNREACHABLE)

    dead = _real_server(refusing)
    monkeypatch.setattr(fleet_service, "server", lambda config=None: dead)
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: dead)

    report = fleet_service.reap(None, server_down=True)

    assert {a.id for a in report.lost} == {here.id, there.id}
    # _observe asks once per project to learn the socket is silent (that is the
    # refusal the default relies on); the flag's own question is asked ONCE.
    observe_probes = 2
    assert len(probes) == observe_probes + 1, probes


def test_reap_server_down_still_marks_nothing_without_a_tmux_binary(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """No tmux at all is a question that could not be asked, not a silent server."""
    coder = _coder(project)
    tmux.installed = False
    report = fleet_service.reap(project, server_down=True)
    assert report.lost == [] and report.ended == []
    tmux.installed = True
    assert [s.agent.id for s in fleet_service.list_agents(project)] == [coder.id]


def test_unknown_blames_tmux_first_and_the_missing_hooks_second(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """``detail`` is "why the state is what it is" (models.py). When tmux cannot be
    asked, tmux is why — a hookless binary is a second, smaller fact and must not
    stand in front of the reason and leave the operator thinking the fleet is healthy.
    """
    hookless = _coder(project, binary=sys.executable)  # takes no --session-id
    with_hooks = _coder(project)
    assert hookless.session_id is None and with_hooks.session_id is not None
    tmux.installed = False

    states = {s.agent.id: (s.state, s.detail) for s in fleet_service.list_agents(project)}

    assert states[hookless.id] == ("unknown", "tmux unavailable; no hooks")
    assert states[with_hooks.id] == ("unknown", "tmux unavailable")
    # Negative control: a server that answered is never blamed. The hookless agent's
    # detail is then the hooks alone, and the agent with hooks says nothing at all.
    tmux.installed = True
    states = {s.agent.id: (s.state, s.detail) for s in fleet_service.list_agents(project)}
    assert states[hookless.id] == ("waiting", "no hooks")
    assert states[with_hooks.id] == ("waiting", None)


@pytest.mark.parametrize("state", ["working", "waiting", "attention"])
def test_a_fresh_board_row_decides_the_state(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, state: str
) -> None:
    agent = _coder(project)
    _board_session(agent, state)
    status = fleet_service.status_of(agent)
    assert status.state == state and status.detail is None
    assert status.session is not None and status.session.id == agent.session_id
    assert status.tmux_session == f"asq-{_codename(project)}"
    [listed] = fleet_service.list_agents(project)
    assert listed.state == state and listed.agent == agent


def test_a_stale_board_row_defers_to_the_pane(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    agent = _coder(project)
    _board_session(agent, "working", seen_ago=team_service._STALE_AFTER + timedelta(minutes=1))
    assert fleet_service.status_of(agent).state == "waiting"
    tmux.printed(agent.pane_id)
    assert fleet_service.status_of(agent).state == "working"
    # Control: a row seen just now wins over recent output.
    _board_session(agent, "attention")
    assert fleet_service.status_of(agent).state == "attention"


def test_recent_output_is_working_and_old_output_is_waiting_without_hooks(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    agent = _coder(project, binary=sys.executable)  # no --session-id: tmux is the only source
    assert fleet_service.status_of(agent).state == "waiting", "no output yet"
    tmux.printed(agent.pane_id, ago=ACTIVITY_WINDOW - timedelta(seconds=1))
    assert fleet_service.status_of(agent).state == "working"
    tmux.printed(agent.pane_id, ago=ACTIVITY_WINDOW + timedelta(seconds=2))
    status = fleet_service.status_of(agent)
    assert status.state == "waiting" and status.detail == "no hooks"


def test_last_output_times_come_from_one_list_panes_and_fail_open() -> None:
    """The parser over tmux's own answer shape — and nothing when tmux cannot answer."""
    calls: list[list[str]] = []

    def answering(argv: Sequence[str], stdin: bytes | None) -> Completed:
        calls.append(list(argv))
        return Completed(0, "%3|~|1700000000\n%7|~|1700000042\n%9|~|\nbroken line\n", "")

    srv = TmuxServer("asq-test", binary=sys.executable, conf=Path("/fake.conf"), runner=answering)
    times = fleet_service._activity_times(srv)
    assert times == {
        "%3": datetime.fromtimestamp(1700000000, tz=UTC),
        "%7": datetime.fromtimestamp(1700000042, tz=UTC),
    }, "a pane with no time and a malformed line are skipped, not guessed"
    assert len(calls) == 1 and calls[0][-4:] == [
        "list-panes",
        "-a",
        "-F",
        "#{pane_id}|~|#{window_activity}",
    ]

    def refusing(argv: Sequence[str], stdin: bytes | None) -> Completed:
        return Completed(1, "", "no server running")

    dead = TmuxServer("asq-test", binary=sys.executable, conf=Path("/fake.conf"), runner=refusing)
    assert fleet_service._activity_times(dead) == {}


def test_a_dead_pane_is_exited_whatever_the_board_says(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    agent = _coder(project)
    _board_session(agent, "waiting")
    tmux.die(agent.pane_id, 3)
    status = fleet_service.status_of(agent)
    assert status.state == "exited" and status.detail == "exit 3"


def test_a_vanished_pane_is_lost(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    agent = _coder(project)
    _board_session(agent, "waiting")
    tmux.vanish(agent.pane_id)
    status = fleet_service.status_of(agent)
    assert status.state == "lost" and status.detail == "pane gone"


def test_a_pane_under_the_old_session_name_is_still_found(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """A codename rename tmux never heard of must not read as every agent lost."""
    agent = _coder(project)
    tmux.sessions["asq-elsewhere"] = tmux.sessions.pop(f"asq-{_codename(project)}")
    assert fleet_service.status_of(agent).state == "waiting"


def test_an_ended_row_is_exited_with_its_status_and_hidden_from_the_live_list(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    agent = _coder(project)
    fleet_service.stop(project, agent.label)  # the fake honours /exit → status 0
    assert fleet_service.list_agents(project) == []
    [status] = fleet_service.list_agents(project, live_only=False)
    assert status.state == "exited" and status.detail == "exit 0"


def test_without_tmux_the_state_is_unknown_unless_the_board_knows(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    agent = _coder(project)
    tmux.installed = False
    status = fleet_service.status_of(agent)
    assert status.state == "unknown" and status.detail == "tmux unavailable"
    _board_session(agent, "working")
    assert fleet_service.status_of(agent).state == "working"


def test_list_agents_orders_by_creation_and_knows_the_session(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    manager = fleet_service.spawn(project, "manager").agent
    coder = _coder(project)
    listed = fleet_service.list_agents(project)
    assert [status.agent.id for status in listed] == [manager.id, coder.id]
    assert {status.tmux_session for status in listed} == {f"asq-{_codename(project)}"}


# --- manager_of / resolve_project ------------------------------------------------------


def test_manager_of_is_the_live_manager_or_none(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    assert fleet_service.manager_of(project) is None
    manager = fleet_service.spawn(project, "manager").agent
    assert fleet_service.manager_of(project) == manager
    fleet_service.stop(project, "manager", force=True)
    assert fleet_service.manager_of(project) is None


def test_resolve_project_by_codename(project: ProjectInfo) -> None:
    named = fleet_service.ensure_codename(project)
    assert named.codename is not None
    assert fleet_service.resolve_project(named.codename).id == project.id
    with pytest.raises(fleet_service.NoSuchProject):
        fleet_service.resolve_project("nothing-here")


def test_ensure_codename_walks_on_when_another_process_took_the_name(
    project: ProjectInfo,
    plain_project: ProjectInfo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``codename_for`` walks past a SNAPSHOT of the taken names, so two processes can
    both pick one candidate; the unique index refuses the loser, which must retry the
    walk rather than escape as a raw ``sqlite3.IntegrityError``."""
    held = fleet_service.ensure_codename(plain_project).codename
    assert held is not None
    real = codenames.codename_for
    calls: list[str] = []
    collide = {"once": True}

    def racing(seed: str, *, taken: Collection[str] = ()) -> str:
        calls.append(seed)
        if collide["once"]:
            collide["once"] = False
            return held  # another process took it between our read and our write
        return real(seed, taken=taken)

    monkeypatch.setattr(codenames, "codename_for", racing)
    named = fleet_service.ensure_codename(project)
    assert named.codename and named.codename != held
    assert len(calls) == 2, "the walk was re-run past the name that had been taken"
    assert fleet_service.resolve_project(named.codename).id == project.id
    # Negative control: with nothing in the way the walk runs once.
    calls.clear()
    third = fleet_service.ensure_codename(team_project(tmp_path / "third"))
    assert third.codename and len(calls) == 1


def test_ensure_codename_gives_up_as_a_fleet_error_not_a_traceback(
    project: ProjectInfo, plain_project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retry is bounded, and losing every attempt is still a FleetError — the CLI
    catches those, and nothing here may reach the operator as a traceback."""
    held = fleet_service.ensure_codename(plain_project).codename
    assert held is not None
    monkeypatch.setattr(codenames, "codename_for", lambda seed, *, taken=(): held)
    with pytest.raises(FleetError, match=r"could not give .* a codename"):
        fleet_service.ensure_codename(project)
    assert _codename(plain_project) == held, "the project that holds it keeps it"


def test_resolve_project_names_the_codenames_when_basenames_collide(tmp_path: Path) -> None:
    """``~/work/api`` and ``~/oss/api`` (§5.7): the codename is what tells them apart."""
    infos = []
    for parent in ("work", "oss"):
        root = tmp_path / parent / "api"
        root.mkdir(parents=True)
        info = fleet_service.ensure_codename(team_project(root))
        infos.append(info)
    with pytest.raises(fleet_service.NoSuchProject, match="matches several projects") as caught:
        fleet_service.resolve_project("api")
    for info in infos:
        assert info.codename and info.codename in str(caught.value)
        assert fleet_service.resolve_project(info.codename).id == info.id


def test_the_pure_name_rules() -> None:
    """Labels, slugs and branches — the rules every other function builds on (§5.7)."""
    assert fleet_service.session_name("amber-otter") == "asq-amber-otter"
    for good in ("manager", "coder-auth", "tester-py311", "a1", "x" * 24):
        assert fleet_service.is_label(good), good
    for bad in ("", "a", "Coder", "coder.auth", "coder:auth", "coder auth", "1coder", "x" * 25):
        assert not fleet_service.is_label(bad), bad
    assert fleet_service.slugify("Wire the auth flow!") == "wire-the-auth-flow"
    assert fleet_service.slugify("--Émigré--") == "migr"
    assert fleet_service.slugify("a" * 40) == "a" * 32
    assert fleet_service.slugify("abcdefg-" * 5) == "abcdefg-" * 3 + "abcdefg", (
        "clip lands on a dash"
    )
    assert fleet_service.slugify("!!!") == ""
    assert (
        fleet_service.branch_name("amber-otter", task_id="tsk_01k9q8p3zzzz", title="Wire auth")
        == "fleet/amber-otter/01k9q8p3-wire-auth"
    )
    assert fleet_service.branch_name("amber-otter", task_id="tsk_01k9q8p3", title=None) == (
        "fleet/amber-otter/01k9q8p3"
    )
    assert fleet_service.branch_name("amber-otter", task_id=None, title="coder-auth") == (
        "fleet/amber-otter/coder-auth"
    )
    assert fleet_service.branch_name("amber-otter", task_id=None, title=None) == (
        "fleet/amber-otter/work"
    )
    with pytest.raises(FleetError, match="not valid"):
        fleet_service.next_label(ProjectInfo(id="prj_x", root=Path("/x")), "coder", wanted="Bad")


# --- tell ----------------------------------------------------------------------------


def test_tell_types_into_a_waiting_agent(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    agent = _coder(project)
    _board_session(agent, "waiting")
    tmux.set_command(agent.pane_id, "claude")  # the agent, not the launcher, is up
    result = fleet_service.tell(project, "coder-1", "please rebase")
    assert result.delivered and "typed" in result.how
    assert tmux.typed == [
        (agent.pane_id, "paste", "please rebase"),
        (agent.pane_id, "key", "Enter"),
    ]
    assert _events(project, "note") == []


@pytest.mark.parametrize("state", ["working", "attention"])
def test_tell_files_a_board_note_when_the_agent_is_not_waiting(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, state: str
) -> None:
    agent = _coder(project)
    _board_session(agent, state)
    result = fleet_service.tell(project, "coder-1", "please rebase")
    assert not result.delivered and state in result.how and "board note" in result.how
    assert tmux.typed == [], "never typed into a busy agent, never into a permission prompt"
    with store_session() as store:
        notes = [e for e in store.recent_events(project.id) if e.kind == "note"]
    assert len(notes) == 1 and notes[0].text == "please rebase" and notes[0].to_role == "coder-1"


def test_tell_falls_back_to_a_note_when_tmux_cannot_type(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    agent = _coder(project)
    _board_session(agent, "waiting")
    tmux.set_command(agent.pane_id, "claude")
    tmux.fail_input = True
    result = fleet_service.tell(project, "coder-1", "please rebase")
    assert not result.delivered and "tmux could not type" in result.how
    assert _events(project, "note") == ["please rebase"]


def test_tell_does_not_type_into_a_pane_that_is_not_the_agent_yet(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """A pane whose foreground is still ``aisquare launch`` reads ``waiting`` as soon
    as its last output is older than ACTIVITY_WINDOW, and the launcher has requested
    no bracketed paste — so ``paste-buffer -p`` would replace every LF with a CR and
    the agent would read N submitted messages instead of one. That is the harm
    ``_type_prompt`` documents and declines to cause, and ``nudge_manager`` already
    applies the same readiness test; ``tell`` consulted the state alone.
    """
    agent = _coder(project)
    _board_session(agent, "waiting")
    assert tmux.facts[agent.pane_id].current_command == PYTHON_LAUNCHER
    assert fleet_service.status_of(agent).state == "waiting", "the state alone says type"
    message = "first line\nsecond line"

    result = fleet_service.tell(project, "coder-1", message)

    assert not result.delivered and "board note" in result.how
    assert tmux.typed == [], "two lines would have arrived as two messages"
    assert _events(project, "note") == [message]
    # Negative control: the same waiting agent with the AGENT in the foreground is
    # typed into, several lines and all — by then it has asked for bracketed paste.
    tmux.set_command(agent.pane_id, "claude")
    again = fleet_service.tell(project, "coder-1", message)
    assert again.delivered and tmux.typed == [
        (agent.pane_id, "paste", message),
        (agent.pane_id, "key", "Enter"),
    ]
    assert _events(project, "note") == [message], "no second note was filed"


def test_tell_an_unknown_label_is_no_such_agent(tmux: FakeTmux, project: ProjectInfo) -> None:
    with pytest.raises(NoSuchAgent, match="no live agent 'coder-9'"):
        fleet_service.tell(project, "coder-9", "hello")
    with pytest.raises(NoSuchAgent, match="no live agent 'coder-9'"):
        fleet_service.stop(project, "coder-9")


def test_tell_attributes_the_note_to_the_sender(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    manager = fleet_service.spawn(project, "manager").agent
    _board_session(manager, "waiting")
    coder = _coder(project)
    _board_session(coder, "working")
    result = fleet_service.tell(project, "coder-1", "rebase first", sender=manager.session_id)
    assert not result.delivered
    with store_session() as store:
        [note] = [e for e in store.recent_events(project.id) if e.kind == "note"]
    assert note.session_id == manager.session_id and note.to_role == "coder-1"
    # Negative control: a sender the board has never seen is refused, not invented.
    with pytest.raises(FleetError, match="unknown sender session 'nope'"):
        fleet_service.tell(project, "coder-1", "again", sender="nope")
    assert _events(project, "note") == ["rebase first"]


# --- stop ----------------------------------------------------------------------------


def test_stop_exits_gracefully_and_records_the_status(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    agent = _coder(project)
    ended = fleet_service.stop(project, "coder-1").agent
    assert tmux.typed == [(agent.pane_id, "literal", "/exit"), (agent.pane_id, "key", "Enter")]
    assert ended.ended_at is not None and ended.exit_status == 0
    assert tmux.killed == [agent.pane_id]
    assert fleet_service.list_agents(project) == []


def test_stop_outwaits_the_dead_without_status_gap(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """A dead pane whose status has not landed yet is polled again, not recorded.

    The gap is real: tmux 3.4's own -vv server log showed a poll where
    ``pane_dead`` was 1 while ``pane_dead_status`` still expanded to '' —
    reading that as final cost the exit status (and CI three red runs).
    """
    tmux.status_lag = 2
    agent = _coder(project)
    ended = fleet_service.stop(project, "coder-1").agent
    assert ended.exit_status == 0, "the status that landed a beat later was collected"
    assert tmux.killed == [agent.pane_id]
    assert tmux.status_lag == 0, "the gap reads were consumed — the lag was exercised"


def test_stop_accepts_a_dead_pane_whose_status_never_lands(
    tmux: FakeTmux, clock: FakeClock, claude_on_path: Path, project: ProjectInfo
) -> None:
    """tmux 3.4 sometimes never exposes a dead pane's status: stop() waits the
    one-second window, then ends the row honestly with no exit status — it must
    not sit out the whole grace for a status that will never land."""
    tmux.status_lag = 10**9
    agent = _coder(project)
    ended = fleet_service.stop(project, "coder-1", grace=15.0).agent
    assert ended.exit_status is None and ended.ended_at is not None
    assert tmux.killed == [agent.pane_id]
    assert clock.now < 15.0, "the status window, not the whole grace"


def test_stop_kills_after_the_grace_period_when_exit_is_ignored(
    tmux: FakeTmux, clock: FakeClock, claude_on_path: Path, project: ProjectInfo
) -> None:
    tmux.honours_exit = False
    agent = _coder(project)
    ended = fleet_service.stop(project, "coder-1", grace=2.0).agent
    assert clock.now >= 2.0, "waited the grace period"
    assert tmux.killed == [agent.pane_id] and ended.exit_status is None


def test_stop_force_skips_the_exit(
    tmux: FakeTmux, clock: FakeClock, claude_on_path: Path, project: ProjectInfo
) -> None:
    agent = _coder(project)
    ended = fleet_service.stop(project, "coder-1", force=True).agent
    assert tmux.typed == [] and tmux.killed == [agent.pane_id] and clock.now == 0
    assert ended.ended_at is not None


def test_stop_ends_the_row_even_when_the_pane_is_already_gone(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    agent = _coder(project)
    tmux.vanish(agent.pane_id)
    ended = fleet_service.stop(project, "coder-1").agent
    assert ended.ended_at is not None and tmux.killed == []


def test_stop_exits_gracefully_when_the_pane_sits_under_another_session_name(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """``rename`` fails open when tmux is unreachable, and the escape hatch lets anyone
    rename a session by hand — so a window list that does not name the pane is not
    proof it is gone. Hard-killing a healthy agent there drops its ``SessionEnd``
    hook, its claims and its exit status."""
    agent = _coder(project)
    tmux.sessions["asq-elsewhere"] = tmux.sessions.pop(f"asq-{_codename(project)}")

    ended = fleet_service.stop(project, "coder-1").agent

    assert tmux.typed == [(agent.pane_id, "literal", "/exit"), (agent.pane_id, "key", "Enter")]
    assert ended.exit_status == 0 and tmux.killed == [agent.pane_id]
    # Negative control: a pane that really is gone is not typed into.
    second = _coder(project)
    tmux.vanish(second.pane_id)
    tmux.typed.clear()
    assert fleet_service.stop(project, second.label).agent.ended_at is not None
    assert tmux.typed == []


def test_stop_leaves_the_row_live_when_tmux_cannot_confirm_the_agent_stopped(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """A ``TmuxError`` is not proof of death — a wedged server's 30 s timeout is one —
    so it must not end the row and print ``✓ stopped`` over a running agent."""
    agent = _coder(project)
    tmux.installed = False
    with pytest.raises(FleetError, match="could not be asked whether 'coder-1' stopped"):
        fleet_service.stop(project, "coder-1")
    tmux.installed = True
    assert [s.agent.id for s in fleet_service.list_agents(project)] == [agent.id]

    tmux.fail_input = True  # the server answers, but /exit does not go through
    with pytest.raises(FleetError, match="its pane is still alive"):
        fleet_service.stop(project, "coder-1")
    tmux.fail_input = False
    assert [s.agent.id for s in fleet_service.list_agents(project)] == [agent.id]
    # Negative control: with tmux answering, the same call stops it and ends the row.
    ended = fleet_service.stop(project, "coder-1").agent
    assert ended.ended_at is not None and ended.exit_status == 0
    assert fleet_service.list_agents(project) == []


def test_stop_leaves_the_row_live_when_the_window_kill_was_refused_and_the_pane_survives(
    tmux: FakeTmux, clock: FakeClock, claude_on_path: Path, project: ProjectInfo
) -> None:
    """Review of #121, round 9 (P1): the kill was wrapped in ``suppress(TmuxError)``
    — "already gone, which is what the kill wanted" — so a kill tmux REFUSED over a
    pane that is still RUNNING ended the row anyway and returned it as stopped.

    A kill that failed is not a pane that died. It goes through
    :func:`_verify_gone` now, like every other tmux failure in this call, and only
    a pane that reads dead or gone ends the row.
    """
    agent = _coder(project)
    tmux.honours_exit = False  # the agent ignores /exit, so the kill is what must decide
    tmux.refuse_kills = True

    with pytest.raises(FleetError, match="its pane is still alive"):
        fleet_service.stop(project, "coder-1", grace=2.0)

    assert [s.agent.id for s in fleet_service.list_agents(project)] == [agent.id], "row LEFT LIVE"
    assert tmux.facts[agent.pane_id].dead is False, "and the pane really is still running"
    assert tmux.killed == [], "nothing died"
    # Negative control: the SAME refused kill over a pane that did die is still
    # "already gone, which is what the kill wanted" — the row ends, with its status.
    tmux.die(agent.pane_id, 7)
    ended = fleet_service.stop(project, "coder-1", grace=2.0).agent
    assert ended.ended_at is not None and ended.exit_status == 7, "the observed status is kept"
    assert fleet_service.list_agents(project) == []


def test_stop_leaves_the_row_live_when_a_denied_socket_cannot_confirm_the_pane_died(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """Review of #121, round 9 verification (major): round 9 closed the suppressed
    ``kill_window``, but ``_window`` — the very look :func:`_verify_gone` acts on —
    still read through the LENIENT ``list_windows``/``pane_facts``. Those answer
    ``[]``/``None`` for ANY non-zero exit, so a socket that is THERE and refuses
    this user (``Permission denied``, exit 1 — the live server of round 7) read as
    "the pane is really gone": the row was ended and ``✓ stopped`` printed over an
    agent still running. The strict twins raise, so the refusal lands in
    ``_verify_gone``'s "could not be asked" branch instead.

    A wedged server was already safe here (``_tmux`` raises on the timeout, through
    either twin). What this covers is the answered-but-refused socket, the one
    shape the lenient reads could not tell from death.
    """
    agent = _coder(project)
    tmux.honours_exit = False
    tmux.socket_denied = True  # the socket is there; it just will not open for us

    with pytest.raises(FleetError, match="could not be asked whether"):
        fleet_service.stop(project, "coder-1", grace=2.0)

    with store_session() as store:
        live = store.fleet_agents(project.id, live_only=True)
    assert [a.id for a in live] == [agent.id], "the row is LEFT LIVE"
    assert tmux.facts[agent.pane_id].dead is False, "and the pane really is still running"
    assert tmux.killed == [], "nothing died"
    # Negative control: the same non-zero exit from a server tmux says is GONE is
    # positive evidence of absence, and ends the row exactly as it always did.
    tmux.socket_denied = False
    tmux.running = False
    ended = fleet_service.stop(project, "coder-1", grace=2.0).agent
    assert ended.ended_at is not None and ended.exit_status is None


# --- reap ----------------------------------------------------------------------------


# --- a spawned agent learns the task it was spawned for --------------------------------
#
# The row belongs to the PROCESS in its pane (the three rules above
# ``team._fleet_row_named``). These tests act as that process the way Claude Code
# would: the fake pane reports a pid, and ``CLAUDE_PID`` — what Claude Code hands
# every hook, measured on 2.1.272 — carries the same number. A nested child is a
# different number under the same window variables. A ``/clear`` is fired in the
# order the real binary fires it: ``SessionEnd(reason=clear)`` for the old id,
# THEN ``SessionStart(source=clear)`` for the new one (the PR's tests skipped the
# end, which is where the claim was being lost — review of #135, finding 1).

PANE_PID = 4242
CHILD_PID = 9999


def _task(
    project: ProjectInfo, title: str, role: str = "coder", needs: list[str] | None = None
) -> TeamTask:
    task, _created = team_service.add_task(title, role=role, needs=needs, cwd=project.root)
    return task


def _become(
    agent: FleetAgent, tmux: FakeTmux, monkeypatch: pytest.MonkeyPatch, *, pid: int, role: str
) -> None:
    """Run as ``pid`` under ``agent``'s window: its variables, and the pane's own pid."""
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", agent.id)
    monkeypatch.setenv("AISQUARE_ROLE", role)
    monkeypatch.setenv("CLAUDE_PID", str(pid))
    tmux.pids[agent.pane_id] = PANE_PID


def _stranger(monkeypatch: pytest.MonkeyPatch, role: str) -> None:
    """Run as a session in some OTHER pane: no window variables, no fleet pid.

    Without this a teammate registered after ``_spawned`` would look like the
    spawned agent's own process and adopt its row — which is the rule working,
    on a fixture that lied about who was calling.
    """
    monkeypatch.delenv("AISQUARE_FLEET_AGENT", raising=False)
    monkeypatch.delenv("CLAUDE_PID", raising=False)
    monkeypatch.setenv("AISQUARE_ROLE", role)


def _spawned(
    project: ProjectInfo,
    role: str,
    task_id: str | None,
    tmux: FakeTmux,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[FleetAgent, str]:
    """Spawn ``role`` for ``task_id`` and become the process in its pane.

    Returns the row and the session id minted for it — the id the agent's first
    ``SessionStart`` reports, since ``launch`` starts it with ``--session-id``.
    """
    receipt = fleet_service.spawn(project, role, task_id=task_id, worktree=False)
    agent = receipt.agent
    assert agent.session_id is not None, "the fake claude on PATH takes --session-id"
    _become(agent, tmux, monkeypatch, pid=PANE_PID, role=role)
    return agent, agent.session_id


def _clear(session_id: str, new_id: str, project: ProjectInfo) -> str:
    """A ``/clear`` as Claude Code fires it; returns the board the new id is handed."""
    team_service.hook_session_end(session_id, project.root, reason="clear")
    return team_service.hook_session_start(new_id, project.root, "clear")


def _row(agent_id: str) -> FleetAgent:
    with store_session() as store:
        row = store.get_fleet_agent(agent_id)
    assert row is not None
    return row


def _task_now(task_id: str) -> TeamTask:
    with store_session() as store:
        task = store.get_task(task_id)
    assert task is not None
    return task


def _assignment_block(board: str) -> str:
    """The ASSIGNED TO YOU lines of a briefing, up to the board's sessions list."""
    assert "ASSIGNED TO YOU" in board, board
    return board.split("ASSIGNED TO YOU", 1)[1].split("sessions:", 1)[0]


def test_a_spawned_agent_is_told_its_task_on_its_first_start(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``fleet spawn --task`` recorded the task on the row and named the label
    and branch after it — and stopped there. The session inside got the generic
    board and its role's ``task next``, which hands out the OLDEST ready task;
    the manager was left posting "you are coder-x, run task show …" notes by
    hand (observed 2026-09-10). The row's env var now reaches the briefing."""
    older = _task(project, "the older task")
    mine = _task(project, "the task this coder is for")
    agent, first = _spawned(project, "coder", mine.id, tmux, monkeypatch)

    board = team_service.hook_session_start(first, project.root, "startup")

    assert f"ASSIGNED TO YOU: {mine.id} [todo] the task this coder is for" in board
    assert f"aisquare task claim {mine.id} --as {first[:8]}" in board, "claim it FIRST, by id"
    assert older.id not in _assignment_block(board)
    assert _row(agent.id).session_id == first, "the minted id is the row's from the spawn"


def test_task_next_prefers_the_callers_assigned_task_over_the_oldest(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Oldest-first is right for a looper picking from a pool and wrong for an
    agent the manager started FOR a task: two coders spawned together raced
    for the same oldest task while their own sat idle."""
    older = _task(project, "the older task")
    mine = _task(project, "the task this coder is for")
    _agent, first = _spawned(project, "coder", mine.id, tmux, monkeypatch)
    team_service.hook_session_start(first, project.root, "startup")

    picked = team_service.next_task(role="coder", claim=True, session_ref=first, cwd=project.root)

    assert picked is not None and picked.id == mine.id
    assert picked.status == "doing" and picked.claimed_by == first
    assert _task_now(older.id).status == "todo", "the pool is left alone"
    # A session with no row keeps the pool order: oldest first.
    monkeypatch.delenv("AISQUARE_FLEET_AGENT")
    team_service.hook_session_start("sess-plain-3", project.root, "startup")
    plain = team_service.next_task(
        role="coder", claim=True, session_ref="sess-plain-3", cwd=project.root
    )
    assert plain is not None and plain.id == older.id


def test_an_assigned_task_someone_else_holds_on_a_live_lease_is_reported_not_retaken(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    mine = _task(project, "already in hand")
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    team_service.hook_session_start("sess-other-4", project.root, "startup")
    assert team_service.claim_task(mine.id, session_ref="sess-other-4").status == "doing"
    _agent, first = _spawned(project, "coder", mine.id, tmux, monkeypatch)

    board = team_service.hook_session_start(first, project.root, "startup")

    assert f"ASSIGNED TO YOU: {mine.id} [doing] already in hand" in board
    assert "already doing by sess-oth" in board
    assert "ask the manager" in board, "never silently take another task"
    assert "task claim" not in _assignment_block(board), "a live lease is not to be taken"


def test_an_assigned_task_whose_holders_lease_ran_out_is_told_to_claim_it(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stop order was given for every ``doing`` task somebody else held —
    including one whose holder died with an expired lease, which ``task claim``
    accepts (review of #135, finding 12c). A replacement spawned for exactly
    that task was told to stand down from the work it was started for."""
    mine = _task(project, "the first holder died on this")
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    team_service.hook_session_start("sess-dead-holder", project.root, "startup")
    team_service.claim_task(mine.id, session_ref="sess-dead-holder")
    with store_session() as store:
        store.renew_leases("sess-dead-holder", datetime.now(tz=UTC) - timedelta(minutes=1))
    _agent, first = _spawned(project, "coder", mine.id, tmux, monkeypatch)

    board = team_service.hook_session_start(first, project.root, "startup")

    block = _assignment_block(board)
    assert "lease on it has run out" in block and f"aisquare task claim {mine.id}" in block
    assert "Do not take another" not in block and "ask the manager" not in block
    # The control: the instruction is one the command honours.
    assert team_service.claim_task(mine.id, session_ref=first).claimed_by == first


def test_a_clear_keeps_the_agents_claim_and_moves_it_to_the_new_id(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 1 of the #135 review. Claude Code fires ``SessionEnd(reason=clear)``
    for the old id BEFORE ``SessionStart(source=clear)`` for the new one, and the
    end hook released the claim to the pool: the new id then found an unclaimed
    task and was told "Claim it FIRST" — or, if a looper had taken it in the
    gap, to stand down from its own work. The end hook now keeps the claims of
    a fleet agent that is only clearing, and the start hook moves them."""
    mine = _task(project, "the task this coder is for")
    agent, first = _spawned(project, "coder", mine.id, tmux, monkeypatch)
    team_service.hook_session_start(first, project.root, "startup")
    team_service.claim_task(mine.id, session_ref=first)

    cleared = _clear(first, "sess-c2", project)

    held = _task_now(mine.id)
    assert (held.status, held.claimed_by) == ("doing", "sess-c2"), "kept, then moved"
    assert _row(agent.id).session_id == "sess-c2"
    assert f"ASSIGNED TO YOU: {mine.id} [doing]" in cleared
    assert "You are the one working it" in cleared
    assert "Claim it FIRST" not in cleared and "already doing by" not in cleared
    with store_session() as store:
        kinds = [e.kind for e in store.recent_events(project.id, limit=20)]
    assert "task_released" not in kinds, "nothing was released, so nothing says so"
    # And again: a second clear starts from the id the first one moved to.
    again = _clear("sess-c2", "sess-c3", project)
    assert "You are the one working it" in again
    assert _task_now(mine.id).claimed_by == "sess-c3"
    # A resume keeps the id and needs no move at all.
    resumed = team_service.hook_session_start("sess-c3", project.root, "resume")
    assert "You are the one working it" in resumed


def test_a_looper_in_the_clear_gap_cannot_take_the_clearing_agents_task(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gap between the two hooks is where a looper's ``task next --claim``
    used to find the task back in the pool. It must find it still ``doing``."""
    mine = _task(project, "the task this coder is for")
    agent, first = _spawned(project, "coder", mine.id, tmux, monkeypatch)
    team_service.hook_session_start(first, project.root, "startup")
    team_service.claim_task(mine.id, session_ref=first)
    _stranger(monkeypatch, "coder")
    team_service.hook_session_start("sess-looper", project.root, "startup")
    _become(agent, tmux, monkeypatch, pid=PANE_PID, role="coder")

    team_service.hook_session_end(first, project.root, reason="clear")
    grabbed = team_service.next_task(
        role="coder", claim=True, session_ref="sess-looper", cwd=project.root
    )
    board = team_service.hook_session_start("sess-c2", project.root, "clear")

    assert grabbed is None, "the pool had nothing: the clearing agent's task is still doing"
    assert _task_now(mine.id).claimed_by == "sess-c2"
    assert "You are the one working it" in board


def test_the_hook_cli_keeps_a_clearing_fleet_agents_claim_in_claude_codes_order(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    """The same sequence through the real hook commands, payloads as Claude Code
    sends them: ``reason`` on the end, ``source`` on the start."""
    mine = _task(project, "the task this coder is for")
    _agent, first = _spawned(project, "coder", mine.id, tmux, monkeypatch)

    def hook(name: str, **payload: object) -> str:
        body = json.dumps({"cwd": str(project.root), **payload})
        result = runner.invoke(app, ["hook", name], input=body)
        assert result.exit_code == 0, result.output
        return result.stdout

    hook("session-start", session_id=first, source="startup")
    claimed = runner.invoke(app, ["task", "claim", mine.id, "--as", first])
    assert claimed.exit_code == 0, claimed.output
    hook("session-end", session_id=first, reason="clear")
    started = hook("session-start", session_id="sess-cli-c2", source="clear")

    assert "You are the one working it" in started, started
    assert _task_now(mine.id).claimed_by == "sess-cli-c2"


def test_a_child_process_of_the_agent_neither_takes_the_row_nor_the_task(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window's env is inherited by every descendant: a nested ``claude -p``
    reaches the same hook under the same ``AISQUARE_FLEET_AGENT``. What tells it
    apart is the process: its ``CLAUDE_PID`` is not the pid in the row's pane."""
    mine = _task(project, "the task this coder is for")
    agent, first = _spawned(project, "coder", mine.id, tmux, monkeypatch)
    team_service.hook_session_start(first, project.root, "startup")
    _become(agent, tmux, monkeypatch, pid=CHILD_PID, role="coder")

    child = team_service.hook_session_start("sess-child", project.root, "startup")

    assert "ASSIGNED TO YOU" not in child
    assert _row(agent.id).session_id == first, "the row stays the parent's"
    picked = team_service.next_task(role="coder", claim=True, session_ref=first, cwd=project.root)
    assert picked is not None and picked.id == mine.id, "the parent still gets its own task first"


@pytest.mark.parametrize("source", ["resume", "fork", "compact", "clear", "startup"])
def test_a_child_cannot_take_the_row_whatever_start_it_reports(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    """Finding 4 of the #135 review: the guard let any caller whose source was not
    ``startup`` through — and ``claude -p --resume``, ``--fork-session`` and a
    long child that compacts all report something else. Identity is the process,
    and a child's start is refused whatever it says about itself."""
    mine = _task(project, "the task this coder is for")
    agent, first = _spawned(project, "coder", mine.id, tmux, monkeypatch)
    team_service.hook_session_start(first, project.root, "startup")
    team_service.claim_task(mine.id, session_ref=first)
    _become(agent, tmux, monkeypatch, pid=CHILD_PID, role="coder")

    child = team_service.hook_session_start("sess-child", project.root, source)

    assert "ASSIGNED TO YOU" not in child
    assert _row(agent.id).session_id == first
    assert _task_now(mine.id).claimed_by == first, "the claim never moved"


@pytest.mark.parametrize("source", ["clear", "compact", "resume", "startup"])
def test_the_process_in_the_pane_adopts_its_row_under_any_source(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    """The other half of the rule: the source plays no part for the pane's own
    process either. Compaction keeps the session id in practice; should a start
    under a new id ever come from the pane's process, it is that process."""
    mine = _task(project, "the task this coder is for")
    agent, first = _spawned(project, "coder", mine.id, tmux, monkeypatch)
    team_service.hook_session_start(first, project.root, "startup")
    team_service.claim_task(mine.id, session_ref=first)

    board = team_service.hook_session_start("sess-next", project.root, source)

    assert _row(agent.id).session_id == "sess-next"
    assert _task_now(mine.id).claimed_by == "sess-next"
    assert "You are the one working it" in board


def test_a_child_started_after_prune_retired_the_parents_presence_cannot_take_the_row(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old guard also asked whether the holder's row was still live — and
    ``team prune`` retires a silent agent's PRESENCE after thirty minutes while
    its claims stay (a long tool call is silence). A plain ``startup`` child
    then rebound the row (finding 4). The parent's own next start still does."""
    mine = _task(project, "the task this coder is for")
    agent, first = _spawned(project, "coder", mine.id, tmux, monkeypatch)
    team_service.hook_session_start(first, project.root, "startup")
    team_service.claim_task(mine.id, session_ref=first)
    with store_session() as store:
        stored = store.get_session(first)
        assert stored is not None
        store.upsert_session(
            stored.model_copy(update={"last_seen_at": datetime.now(tz=UTC) - timedelta(hours=1)})
        )
    report = team_service.prune_sessions(cwd=project.root)
    assert [p.id for p in report.pruned] == [first] and report.released_total == 0
    _become(agent, tmux, monkeypatch, pid=CHILD_PID, role="coder")

    child = team_service.hook_session_start("sess-child", project.root, "startup")

    assert "ASSIGNED TO YOU" not in child
    assert _row(agent.id).session_id == first and _task_now(mine.id).claimed_by == first
    _become(agent, tmux, monkeypatch, pid=PANE_PID, role="coder")
    own = _clear(first, "sess-c2", project)
    assert "You are the one working it" in own and _task_now(mine.id).claimed_by == "sess-c2"


def test_the_managers_task_less_row_follows_the_manager_and_never_its_child(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bind used to happen before the "no task" check, so a manager's child
    rebound the manager's row — after which ``nudge_manager`` read the child's
    dead session and never woke the manager, and ``fleet tell`` typed into a
    working one (finding 4). The row must name the manager's live session."""
    agent, first = _spawned(project, "manager", None, tmux, monkeypatch)
    assert agent.task_id is None
    team_service.hook_session_start(first, project.root, "startup")
    _become(agent, tmux, monkeypatch, pid=CHILD_PID, role="manager")

    team_service.hook_session_start("sess-mgr-child", project.root, "startup")

    assert _row(agent.id).session_id == first
    _become(agent, tmux, monkeypatch, pid=PANE_PID, role="manager")
    _clear(first, "sess-mgr-2", project)
    assert _row(agent.id).session_id == "sess-mgr-2", "the manager's own clear rebinds"


def test_a_child_process_cannot_claim_the_parents_assigned_task(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 2 of the #116 review: the hook refused to brief a child, but ``task
    next`` resolved the row from the inherited variable alone and claimed the
    parent's task out from under it. A claim needs a session bound to the row."""
    older = _task(project, "the older task")
    mine = _task(project, "the task this coder is for")
    agent, first = _spawned(project, "coder", mine.id, tmux, monkeypatch)
    team_service.hook_session_start(first, project.root, "startup")
    _become(agent, tmux, monkeypatch, pid=CHILD_PID, role="coder")
    team_service.hook_session_start("sess-child-a", project.root, "startup")

    # The child carries the parent's AISQUARE_FLEET_AGENT and asks for work.
    stolen = team_service.next_task(
        role="coder", claim=True, session_ref="sess-child-a", cwd=project.root
    )

    assert stolen is not None and stolen.id == older.id, "a child picks from the pool"
    picked = team_service.next_task(role="coder", claim=True, session_ref=first, cwd=project.root)
    assert picked is not None and picked.id == mine.id, "the agent's own task is still there"


def test_a_childs_own_clear_still_releases_its_own_claims(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 2 is for the pane's process only: a nested child that ends a session
    releases what it held, as every session end always did."""
    pool = _task(project, "pool work")
    agent, first = _spawned(project, "coder", None, tmux, monkeypatch)
    team_service.hook_session_start(first, project.root, "startup")
    _become(agent, tmux, monkeypatch, pid=CHILD_PID, role="coder")
    team_service.hook_session_start("sess-child", project.root, "startup")
    team_service.claim_task(pool.id, session_ref="sess-child")

    team_service.hook_session_end("sess-child", project.root, reason="clear")

    released = _task_now(pool.id)
    assert (released.status, released.claimed_by) == ("todo", None)


def test_without_a_process_identity_only_an_unbound_row_binds(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A binary that exports no ``CLAUDE_PID`` cannot be told from its children,
    so a bound row stays with the session it has — and a row nobody is bound to
    yet (the binary could not be started on a chosen id) binds on first arrival,
    because nothing better is known."""
    mine = _task(project, "the task this coder is for")
    agent, first = _spawned(project, "coder", mine.id, tmux, monkeypatch)
    team_service.hook_session_start(first, project.root, "startup")
    team_service.claim_task(mine.id, session_ref=first)
    monkeypatch.delenv("CLAUDE_PID")

    cleared = _clear(first, "sess-c2", project)

    assert "ASSIGNED TO YOU" not in cleared and _row(agent.id).session_id == first
    unbound = fleet_service.spawn(
        project, "coder", task_id=mine.id, worktree=False, agent_args=["--continue"]
    ).agent
    assert unbound.session_id is None, "the premise: no id could be minted"
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", unbound.id)
    board = team_service.hook_session_start("sess-first-arrival", project.root, "startup")
    assert "ASSIGNED TO YOU" in board and _row(unbound.id).session_id == "sess-first-arrival"


def test_a_coder_spawned_for_rework_is_told_to_do_the_rework(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gating ``[review]`` on the verifying roles sent every other role to the
    generic stop order, so a coder spawned to address review feedback stalled on
    arrival while holding a slot (review of #116, round 2)."""
    task = _task(project, "verify the auth flow")
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    team_service.hook_session_start("sess-first-coder", project.root, "startup")
    team_service.claim_task(task.id, session_ref="sess-first-coder")
    team_service.review_task(task.id, session_ref="sess-first-coder")
    _agent, first = _spawned(project, "coder", task.id, tmux, monkeypatch)

    board = team_service.hook_session_start(first, project.root, "startup")

    assert f"ASSIGNED TO YOU: {task.id} [review]" in board
    assert "spawned for the rework" in board
    assert "Do not take another task on your own" not in board


def test_a_blocked_assignment_is_told_to_unblock_it_not_to_stand_down(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``blocked`` fell to the stop order too — and a blocked task is claimable,
    so there was work to name."""
    task = _task(project, "needs a decision")
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    team_service.hook_session_start("sess-blocker", project.root, "startup")
    team_service.claim_task(task.id, session_ref="sess-blocker")
    team_service.block_task(task.id, reason="waiting on the API key", session_ref="sess-blocker")
    _agent, first = _spawned(project, "coder", task.id, tmux, monkeypatch)

    board = team_service.hook_session_start(first, project.root, "startup")

    assert f"ASSIGNED TO YOU: {task.id} [blocked]" in board
    assert "names why" in board and f"aisquare task claim {task.id}" in board
    assert "Do not take another task on your own" not in board


def test_an_assigned_task_with_open_needs_is_not_told_to_claim_it(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 12a: the ``todo`` branch said "Claim it FIRST" whatever the task
    needed, and ``task claim`` accepts any todo task — so the readiness rule
    ``task next`` enforces was bypassed by the briefing that named the command."""
    prerequisite = _task(project, "the schema first")
    mine = _task(project, "the endpoint on top of it", needs=[prerequisite.id])
    _agent, first = _spawned(project, "coder", mine.id, tmux, monkeypatch)

    board = team_service.hook_session_start(first, project.root, "startup")

    block = _assignment_block(board)
    assert f"waits on {prerequisite.id[-8:]}" in block
    assert "Claim it FIRST" not in block and "task claim" not in block
    picked = team_service.next_task(role="coder", claim=True, session_ref=first, cwd=project.root)
    assert picked is not None and picked.id == prerequisite.id, "the pool, not the waiting task"
    team_service.finish_task(prerequisite.id, session_ref=first)
    ready = team_service.hook_session_start(first, project.root, "resume")
    assert "Claim it FIRST" in _assignment_block(ready), "ready now, so claim it"


@pytest.mark.parametrize("state", ["todo", "blocked", "doing"])
def test_a_verifier_spawned_for_a_task_that_is_not_in_review_is_not_told_to_claim_it(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    """Finding 12b: a tester that reopened its review task was told, on its next
    clear, to claim it and "work it to review/done" — and ``task claim`` has no
    role check, so it raced the coder for the rework. A verifier's part is the
    review; every other state is somebody else's turn."""
    task = _task(project, "verify the auth flow")
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    team_service.hook_session_start("sess-coder", project.root, "startup")
    team_service.claim_task(task.id, session_ref="sess-coder")
    team_service.review_task(task.id, session_ref="sess-coder")
    _agent, first = _spawned(project, "tester", task.id, tmux, monkeypatch)
    team_service.hook_session_start(first, project.root, "startup")
    team_service.reopen_task(task.id, reason="FAIL: the login step 500s", session_ref=first)
    if state == "blocked":
        team_service.claim_task(task.id, session_ref="sess-coder")
        team_service.block_task(task.id, reason="needs the fixture", session_ref="sess-coder")
    elif state == "doing":
        team_service.claim_task(task.id, session_ref="sess-coder")

    board = _clear(first, "sess-tester-2", project)

    block = _assignment_block(board)
    assert f"[{state}]" in block and "not in review" in block
    assert "task claim" not in block and "Do not take another" not in block


def test_a_row_with_no_session_id_does_not_read_an_unclaimed_task_as_its_own(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``FleetAgent.session_id`` is None for any binary that cannot be started on
    a chosen id — ``--continue`` takes that branch here. Testing "is this claim
    mine?" by membership then compared ``None`` against ``(sid, None)`` and said
    yes, so an untouched `todo` task was reported as work in flight: the agent
    was told to carry on, never claimed it, and the pool handed it to somebody
    else (review of #116, round 3)."""
    mine = _task(project, "the task this coder is for")
    receipt = fleet_service.spawn(
        project, "coder", task_id=mine.id, worktree=False, agent_args=["--continue"]
    )
    assert receipt.agent.session_id is None, "the premise: no id could be minted"
    _become(receipt.agent, tmux, monkeypatch, pid=PANE_PID, role="coder")

    board = team_service.hook_session_start("sess-nosid", project.root, "startup")

    assert f"ASSIGNED TO YOU: {mine.id} [todo]" in board
    assert f"aisquare task claim {mine.id} --as sess-nos" in board, "claim it FIRST"
    assert "You are the one working it" not in board
    assert _row(receipt.agent.id).session_id == "sess-nosid", "and it still binds"
    # The same `None == None` read, on the branch that does consult it: a task
    # sent to review without ever being claimed keeps `claimed_by = NULL`.
    unclaimed = _task(project, "never claimed, straight to review")
    team_service.review_task(unclaimed.id)
    second = fleet_service.spawn(
        project, "coder", task_id=unclaimed.id, worktree=False, agent_args=["--continue"]
    )
    _become(second.agent, tmux, monkeypatch, pid=PANE_PID, role="coder")

    review = team_service.hook_session_start("sess-nosid-2", project.root, "startup")

    assert f"ASSIGNED TO YOU: {unclaimed.id} [review]" in review
    assert "spawned for the rework" in review
    assert "it is a verifier's now" not in review, "nobody put it there; it is not 'yours'"


def test_a_clear_after_review_moves_the_claim_and_does_not_reopen_the_work(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``set_task_status`` clears ``claimed_by`` for done/dropped only, so review
    and blocked keep theirs — the two statuses the first claim-move missed. And
    "you hold it, carry on to `task review`" is the wrong thing to tell an agent
    whose task is already WITH a verifier (review of #116, round 3)."""
    mine = _task(project, "the task this coder is for")
    _agent, first = _spawned(project, "coder", mine.id, tmux, monkeypatch)
    team_service.hook_session_start(first, project.root, "startup")
    team_service.claim_task(mine.id, session_ref=first)
    team_service.review_task(mine.id, session_ref=first)

    cleared = _clear(first, "sess-rv2", project)

    assert f"ASSIGNED TO YOU: {mine.id} [review]" in cleared
    assert "it is a verifier's now" in cleared
    assert "Carry on" not in cleared and "spawned for the rework" not in cleared
    assert _task_now(mine.id).claimed_by == "sess-rv2", "the claim follows the agent"


def test_adopting_a_row_moves_every_claim_the_old_id_held(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not only the assigned task: the pool task the agent took, the one it put
    in review, the one it blocked — every claim the old id holds, or their leases
    run out under a working agent. A closed task carries no claim and is left."""
    assigned = _task(project, "assigned")
    pool = _task(project, "a pool task")
    reviewed = _task(project, "in review")
    blocked = _task(project, "blocked")
    finished = _task(project, "done already")
    _agent, first = _spawned(project, "coder", assigned.id, tmux, monkeypatch)
    team_service.hook_session_start(first, project.root, "startup")
    for task in (assigned, pool, reviewed, blocked, finished):
        team_service.claim_task(task.id, session_ref=first)
    team_service.review_task(reviewed.id, session_ref=first)
    team_service.block_task(blocked.id, reason="waiting", session_ref=first)
    team_service.finish_task(finished.id, session_ref=first)

    _clear(first, "sess-c2", project)

    holders = {t.id: _task_now(t.id).claimed_by for t in (assigned, pool, reviewed, blocked)}
    assert holders == {t.id: "sess-c2" for t in (assigned, pool, reviewed, blocked)}
    assert _task_now(finished.id).claimed_by is None


def test_adopting_a_row_re_leases_the_doing_claims_alone(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #203. The claim move stamped the new lease on review and
    blocked tasks too — a lease nothing renews (``renew_leases`` is
    ``doing``-only) and nothing reclaims (``claim_task`` too), so the board read
    "lease until …" on a claim that had lapsed and could not be taken. The
    claim still follows the agent in every status that keeps one; the lease is
    ``doing``'s alone."""
    working = _task(project, "being worked")
    reviewed = _task(project, "in review")
    blocked = _task(project, "blocked")
    _agent, first = _spawned(project, "coder", working.id, tmux, monkeypatch)
    team_service.hook_session_start(first, project.root, "startup")
    for task in (working, reviewed, blocked):
        team_service.claim_task(task.id, session_ref=first)
    team_service.review_task(reviewed.id, session_ref=first)
    team_service.block_task(blocked.id, reason="waiting", session_ref=first)
    leases_before = {t.id: _task_now(t.id).claim_expires_at for t in (reviewed, blocked)}
    working_lease_before = _task_now(working.id).claim_expires_at
    time.sleep(0.01)  # so a re-stamped lease is distinguishable from the old one

    _clear(first, "sess-lease", project)

    for task in (working, reviewed, blocked):
        assert _task_now(task.id).claimed_by == "sess-lease", "the claim follows the agent"
    doing_lease = _task_now(working.id).claim_expires_at
    assert doing_lease is not None and doing_lease != working_lease_before, "doing: a new lease"
    assert {t.id: _task_now(t.id).claim_expires_at for t in (reviewed, blocked)} == leases_before, (
        "review and blocked keep whatever lease they had: nothing renews one there"
    )


def test_spawn_sets_its_own_identity_and_opt_out_on_the_window(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """Every variable a spawn needs travels on ITS window (``-e``); none is left
    for a later window to inherit from the session. Round 3 of the #203 review
    had the identity alone taken back out of a new session's environment, and
    round 4 found what that leaves: a plain ``fleet spawn coder`` after
    ``fleet spawn manager --account 2`` ran under the manager's slot. So the
    window carries everything it needs, and ``spawn_window`` keeps all of it out
    of the session (``tests/test_tmux.py`` measures that half)."""
    fleet_service.spawn(project, "coder", worktree=False)
    env = tmux.spawned[-1]["env"]
    assert isinstance(env, dict)
    assert "AISQUARE_FLEET_AGENT" in env
    assert env.get("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS") == "0", "the opt-out is the window's"


def test_adopting_a_row_moves_the_row_and_the_claims_together_or_not_at_all(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bind and the claim move used to commit separately, with the second
    failure swallowed: a row bound to the new id, the work still held by the
    old one (review of #135). One transaction: a claim move that fails leaves
    the row where it was."""
    mine = _task(project, "the task this coder is for")
    agent, first = _spawned(project, "coder", mine.id, tmux, monkeypatch)
    team_service.hook_session_start(first, project.root, "startup")
    team_service.claim_task(mine.id, session_ref=first)

    class RefusesTheClaimMove:
        """``sqlite3.Connection`` with one statement — the claim move — refused."""

        def __init__(self, conn: sqlite3.Connection) -> None:
            self._conn = conn

        def execute(self, sql: str, parameters: Sequence[object] = (), /) -> sqlite3.Cursor:
            if sql.startswith("UPDATE team_task SET claimed_by"):
                raise sqlite3.OperationalError("database is locked")
            return self._conn.execute(sql, parameters)

        def __getattr__(self, name: str) -> object:
            return getattr(self._conn, name)

    lease = datetime.now(tz=UTC) + timedelta(hours=2)
    with store_session() as store:
        assert isinstance(store, SqliteStore)
        store._conn = RefusesTheClaimMove(store._conn)  # type: ignore[assignment]
        with pytest.raises(sqlite3.OperationalError):
            store.adopt_fleet_agent_session(agent.id, first, "sess-c2", lease)

    assert _row(agent.id).session_id == first, "the row did not move without its claims"
    assert _task_now(mine.id).claimed_by == first


def test_a_row_that_ends_mid_hook_briefs_nobody(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`fleet stop` and this hook are different processes, which is why the
    adoption is a targeted UPDATE — so the stop can land between the read and
    the write. Its False was being discarded, and the stopped session was briefed
    "claim it FIRST" and held the task under a dead row (review of #116, round 3)."""
    mine = _task(project, "the task this coder is for")
    agent, first = _spawned(project, "coder", mine.id, tmux, monkeypatch)
    team_service.hook_session_start(first, project.root, "startup")
    original = SqliteStore.adopt_fleet_agent_session

    def stop_first(
        self: SqliteStore, agent_id: str, previous: str | None, session_id: str, lease: datetime
    ) -> bool:
        # The race, made deterministic: the row ends after it was read.
        self.end_fleet_agent(agent_id, exit_status=0)
        return original(self, agent_id, previous, session_id, lease)

    monkeypatch.setattr(SqliteStore, "adopt_fleet_agent_session", stop_first)

    board = _clear(first, "sess-raced", project)

    assert "ASSIGNED TO YOU" not in board
    assert _task_now(mine.id).status == "todo"
    assert _row(agent.id).session_id == first, "an ended row is not rebound"


def test_an_unreadable_fleet_row_costs_the_line_and_not_the_board(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The docstring has promised fail-open since the first version; only
    AmbiguousIdError was ever caught, so any sqlite error propagated out of
    `hook_session_start` and took the whole board with it (review of #116)."""
    task = _task(project, "some task")
    _agent, first = _spawned(project, "coder", task.id, tmux, monkeypatch)

    def boom(self: SqliteStore, ref: str) -> FleetAgent | None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(SqliteStore, "get_fleet_agent", boom)

    board = team_service.hook_session_start(first, project.root, "startup")

    assert "ASSIGNED TO YOU" not in board
    assert "<aisquare-team>" in board and task.id in board, "the board itself survives"
    assert "Your standing cycle (coder)" in board


def test_a_tmux_that_does_not_answer_costs_the_adoption_and_not_the_board(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The identity check asks tmux; a tmux that raises is "cannot tell". For the
    end hook that means the claims are RELEASED, as every end always did — parked
    on an id nothing proven can come back for, they would strand — and for the
    start hook that a bound row stays put. The board still renders either way."""
    mine = _task(project, "the task this coder is for")
    agent, first = _spawned(project, "coder", mine.id, tmux, monkeypatch)
    team_service.hook_session_start(first, project.root, "startup")
    team_service.claim_task(mine.id, session_ref=first)

    def wedged(self: FakeTmux, pane_id: str) -> int | None:
        raise TmuxError("server wedged")

    monkeypatch.setattr(FakeTmux, "pane_pid", wedged)

    board = _clear(first, "sess-c2", project)

    assert "ASSIGNED TO YOU" not in board and "<aisquare-team>" in board
    assert _row(agent.id).session_id == first, "a bound row is not adopted on a guess"
    released = _task_now(mine.id)
    assert (released.status, released.claimed_by) == ("todo", None), "released, not parked"


def test_a_bind_the_start_hook_could_not_make_lands_at_the_next_prompt(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 5 of the second #135 review. A ``/clear``'s hand-off proves the
    pane's process twice, in two hook processes: the end hook keeps the claims,
    the start hook adopts them. tmux not answering the second time left the row
    bound to the ended id and the claims parked on it — nothing to adopt them,
    nothing to release them, for the length of the lease. The bind is tried
    again at every prompt of a session under a fleet window that no row is
    bound to, and the briefing it was owed comes with it, once."""
    mine = _task(project, "the task this coder is for")
    agent, first = _spawned(project, "coder", mine.id, tmux, monkeypatch)
    team_service.hook_session_start(first, project.root, "startup")
    team_service.claim_task(mine.id, session_ref=first)
    team_service.hook_session_end(first, project.root, reason="clear")  # kept: the pane's process
    answering = FakeTmux.pane_pid

    def wedged(self: FakeTmux, pane_id: str) -> int | None:
        raise TmuxError("server wedged")

    monkeypatch.setattr(FakeTmux, "pane_pid", wedged)
    started = team_service.hook_session_start("sess-c2", project.root, "clear")
    assert "ASSIGNED TO YOU" not in started
    parked = _task_now(mine.id)
    assert parked.claimed_by == first and _row(agent.id).session_id == first, (
        "the premise: the claim sits on the ended id, the row with it"
    )
    still_down = team_service.hook_prompt_heartbeat("sess-c2", project.root)
    assert "ASSIGNED TO YOU" not in still_down and _row(agent.id).session_id == first

    monkeypatch.setattr(FakeTmux, "pane_pid", answering)
    prompt = team_service.hook_prompt_heartbeat("sess-c2", project.root)

    assert f"ASSIGNED TO YOU: {mine.id} [doing]" in prompt, prompt
    assert "You are the one working it" in prompt
    assert _row(agent.id).session_id == "sess-c2"
    assert _task_now(mine.id).claimed_by == "sess-c2", "the parked claim moved with the row"
    again = team_service.hook_prompt_heartbeat("sess-c2", project.root)
    assert "ASSIGNED TO YOU" not in again, "briefed once; a bound session is not re-briefed"


def test_a_bound_session_pays_no_tmux_call_per_prompt(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retry above runs on every prompt, so its cost has to be nothing for the
    common case: a session whose row is bound already is two indexed reads and
    no identity check. A child under the same window pays one ``display-message``
    per prompt to be refused — as at its start — and never binds."""
    mine = _task(project, "the task this coder is for")
    agent, first = _spawned(project, "coder", mine.id, tmux, monkeypatch)
    team_service.hook_session_start(first, project.root, "startup")
    asked: list[str] = []
    answering = FakeTmux.pane_pid

    def counting(self: FakeTmux, pane_id: str) -> int | None:
        asked.append(pane_id)
        return answering(self, pane_id)

    monkeypatch.setattr(FakeTmux, "pane_pid", counting)
    team_service.hook_prompt_heartbeat(first, project.root)
    team_service.hook_prompt_heartbeat(first, project.root)
    assert asked == [], "a bound session asks tmux nothing at its prompts"

    _become(agent, tmux, monkeypatch, pid=CHILD_PID, role="coder")
    team_service.hook_session_start("sess-child", project.root, "startup")
    team_service.hook_prompt_heartbeat("sess-child", project.root)
    assert asked == [agent.pane_id, agent.pane_id], "a child is checked at its start and its prompt"
    assert _row(agent.id).session_id == first, "and never binds"


def test_stopping_an_agent_releases_the_claims_parked_on_its_ended_session(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other strand of finding 5: ``fleet stop`` landing in the ``/clear`` gap
    ends the row while the claims sit on the id the end hook just ended. No
    start hook adopts an ended row, and the process is dead — so the row's end
    releases what its session still held, and the board is told why."""
    mine = _task(project, "the task this coder is for")
    agent, first = _spawned(project, "coder", mine.id, tmux, monkeypatch)
    team_service.hook_session_start(first, project.root, "startup")
    team_service.claim_task(mine.id, session_ref=first)
    team_service.hook_session_end(first, project.root, reason="clear")
    assert _task_now(mine.id).claimed_by == first, "the premise: parked for the start hook"

    stopped = fleet_service.stop(project, agent.label, force=True).agent

    assert stopped.ended_at is not None
    released = _task_now(mine.id)
    assert (released.status, released.claimed_by) == ("todo", None)
    assert "the task this coder is for (agent stopped)" in _events(project, "task_released")
    with store_session() as store:
        session = store.get_session(first)
    assert session is not None and session.ended_at is not None


def test_reaping_a_dead_pane_releases_what_its_session_still_held(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A killed agent fires no ``SessionEnd``: its claims stayed ``doing`` under a
    dead holder until the lease ran out. ``reap`` ends the row for a dead pane
    and now releases with it; a vanished pane is the same case."""
    mine = _task(project, "the task this coder is for")
    agent, first = _spawned(project, "coder", mine.id, tmux, monkeypatch)
    team_service.hook_session_start(first, project.root, "startup")
    team_service.claim_task(mine.id, session_ref=first)
    tmux.die(agent.pane_id, 137)

    report = fleet_service.reap(project)

    assert [a.id for a in report.ended] == [agent.id]
    released = _task_now(mine.id)
    assert (released.status, released.claimed_by) == ("todo", None)
    assert "the task this coder is for (agent exited)" in _events(project, "task_released")


def test_a_closed_assignment_leaves_the_header_chip_and_keeps_the_label(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 7 of the second #135 review. ``retire_fleet_assignments`` claimed
    "nothing the operator sees changes", but the agent header renders its
    ``task 01k…`` chip from ``task_id``. The chip goes — the agent is no longer
    on that task — and the docstring now says so; the label, named after the
    task at spawn, is what keeps the tie visible."""
    from aisquare.cli.ui.views.agent import header_text

    mine = _task(project, "the task this coder is for")
    agent, first = _spawned(project, "coder", mine.id, tmux, monkeypatch)
    short = mine.id.removeprefix("tsk_")[: fleet_service.TASK_SHORT]
    before = header_text(FleetAgentStatus(agent=agent)).plain
    assert f"task {mine.id[-8:]}" in before and agent.label == f"coder-{short}"

    team_service.hook_session_start(first, project.root, "startup")
    team_service.claim_task(mine.id, session_ref=first)
    team_service.finish_task(mine.id, note="shipped", session_ref=first)

    after = header_text(FleetAgentStatus(agent=_row(agent.id))).plain
    assert "task " not in after, "the chip goes with the assignment"
    assert f"coder-{short}" in after, "the label still names the task the agent was spawned for"


def test_a_tester_spawned_for_a_review_task_is_told_to_verify_it(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The manager spawns a tester once work reaches review; the assignment must
    read as 'verify this', not as a coder's 'claim it' or a stop order."""
    task = _task(project, "verify the auth flow")
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    team_service.hook_session_start("sess-coder", project.root, "startup")
    team_service.claim_task(task.id, session_ref="sess-coder")
    team_service.review_task(task.id, session_ref="sess-coder")
    _agent, first = _spawned(project, "tester", task.id, tmux, monkeypatch)

    board = team_service.hook_session_start(first, project.root, "startup")

    assert f"ASSIGNED TO YOU: {task.id} [review]" in board
    assert "awaits your verification" in board and "Do not take another" not in board
    picked = team_service.next_task(status="review", session_ref=first, cwd=project.root)
    assert picked is not None and picked.id == task.id


def test_a_verifier_cycle_and_the_mcp_door_get_their_own_review_task_first(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 11 of the #135 review: ``task next`` applied the spawned-for
    preference only to a session bound to a row — and the tester, runner and
    reviewer cycles ran ``task next --status review`` with no ``--as``, while
    the MCP server's ``task_next`` acts as a virtual ``mcp:`` session no row is
    bound to. Two testers spawned for two review tasks were both handed the
    older one. The window's variable orders the pool for those callers."""
    older = _task(project, "the older review task")
    mine = _task(project, "the review task this tester is for")
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    team_service.hook_session_start("sess-coder", project.root, "startup")
    for task in (older, mine):
        team_service.claim_task(task.id, session_ref="sess-coder")
        team_service.review_task(task.id, session_ref="sess-coder")
    _agent, first = _spawned(project, "tester", mine.id, tmux, monkeypatch)
    team_service.hook_session_start(first, project.root, "startup")
    now = datetime.now(tz=UTC)
    with store_session() as store:
        store.upsert_session(
            TeamSession(
                id="mcp:test:abc123",
                project_id=project.id,
                role="remote",
                started_at=now,
                last_seen_at=now,
            )
        )

    by_cycle = team_service.next_task(status="review", cwd=project.root)
    by_mcp = team_service.next_task(status="review", session_ref="mcp:test:abc123")

    assert by_cycle is not None and by_cycle.id == mine.id, "the cycle, typed with no --as"
    assert by_mcp is not None and by_mcp.id == mine.id, "the MCP door"
    # The control: without the variable the pool order is the oldest first.
    monkeypatch.delenv("AISQUARE_FLEET_AGENT")
    plain = team_service.next_task(status="review", cwd=project.root)
    assert plain is not None and plain.id == older.id


def test_the_window_variable_orders_the_pool_but_never_claims_from_it(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The variable is inherited by every process the agent starts, so it is not
    an identity: a claim on its word alone would let a nested child take its
    parent's task (review of #116, round 2). A claim keeps needing a session the
    row is bound to; a caller without one claims in pool order."""
    older = _task(project, "the older task")
    mine = _task(project, "the task this coder is for")
    _agent, first = _spawned(project, "coder", mine.id, tmux, monkeypatch)
    team_service.hook_session_start(first, project.root, "startup")

    anonymous = team_service.next_task(role="coder", claim=True, cwd=project.root)

    assert anonymous is not None and anonymous.id == older.id
    assert _task_now(mine.id).status == "todo", "the row's task was not taken on the name"


def test_the_verifier_cycles_pass_their_session_to_task_next() -> None:
    """The cycles are where the missing ``--as`` was: with it, ``task next`` joins
    the caller to its row by session, which is the identity the claim guard
    trusts — the env fallback above is for callers that have nothing better."""
    from aisquare.core import harness

    for role in ("tester", "runner", "reviewer", "ui-tester"):
        first = harness.role_cycle(role, "abcd1234")[0]
        assert "task next --status review --as abcd1234" in first, (role, first)


def test_every_verifying_role_is_known_to_the_assignment(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The set of roles that VERIFY lives here while the cycles live in the
    harness, so a role added there can go missing here — `ui-tester` did, and its
    `[review]` assignment read as the coder's rework briefing: edit and re-submit
    someone else's work, against its own lane rule (review of #116, round 4).

    The harness is the source of truth: any role whose standing cycle pulls from
    the review pool is a verifier, whatever it is called."""
    from aisquare.core import harness
    from aisquare.services.team import _VERIFYING_ROLES

    pulls_review = {
        role
        for role in harness.ROLE_PROFILES
        if "task next --status review" in " ".join(harness.role_cycle(role, "sess-x"))
    }
    assert pulls_review <= _VERIFYING_ROLES, (
        f"roles that pull from the review pool but get the rework briefing: "
        f"{sorted(pulls_review - _VERIFYING_ROLES)}"
    )
    assert set(harness.ROLE_PROFILES) >= _VERIFYING_ROLES, (
        f"named here but not a role: {sorted(_VERIFYING_ROLES - set(harness.ROLE_PROFILES))}"
    )


def test_a_ui_tester_spawned_for_a_review_task_is_told_to_verify_it(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`base_role('ui-tester')` is `ui-tester`, and it was not in the verifying
    set — so the browser verifier was handed "you were spawned for the rework …
    do not take pool work first" for a task another agent holds."""
    task = _task(project, "UI: the settings dialog")
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    team_service.hook_session_start("sess-ui-coder", project.root, "startup")
    team_service.claim_task(task.id, session_ref="sess-ui-coder")
    team_service.review_task(task.id, session_ref="sess-ui-coder")
    _agent, first = _spawned(project, "ui-tester", task.id, tmux, monkeypatch)

    board = team_service.hook_session_start(first, project.root, "startup")

    assert f"ASSIGNED TO YOU: {task.id} [review]" in board
    assert "awaits your verification" in board
    assert "spawned for the rework" not in board and "Do not take another" not in board
    # And the line names no verdict command: the validator is a verifier whose
    # cycle is a one-shot GATE note and never runs `task next --status review`.
    assert "task next --status review" not in board.split("sessions:")[0]


def test_a_finished_assignment_is_forgotten_and_never_re_briefed(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 5 of the #135 review: ``fleet_agent.task_id`` never expired, so
    every later start of an agent that had finished its task re-briefed it
    through the done branch — "tell the manager: `aisquare note … --kind
    question`" — and each such note woke the manager for nothing. Reopened and
    claimed by someone else, the same row ordered the busy agent to stand down."""
    mine = _task(project, "the task this coder is for")
    pool = _task(project, "the pool task it took next")
    agent, first = _spawned(project, "coder", mine.id, tmux, monkeypatch)
    team_service.hook_session_start(first, project.root, "startup")
    team_service.claim_task(mine.id, session_ref=first)
    team_service.review_task(mine.id, session_ref=first)
    _stranger(monkeypatch, "tester")
    team_service.hook_session_start("sess-tester", project.root, "startup")
    team_service.finish_task(mine.id, note="verified", session_ref="sess-tester")
    _become(agent, tmux, monkeypatch, pid=PANE_PID, role="coder")
    team_service.claim_task(pool.id, session_ref=first)

    assert _row(agent.id).task_id is None, "the assignment ended with its task"
    cleared = _clear(first, "sess-c2", project)
    assert "ASSIGNED TO YOU" not in cleared and "--kind question" not in cleared
    assert _task_now(pool.id).claimed_by == "sess-c2", "its pool work still moved with it"
    # Reopened and taken by somebody else: no longer this agent's business.
    team_service.reopen_task(mine.id, reason="regressed in prod", session_ref="sess-tester")
    _stranger(monkeypatch, "coder")
    team_service.hook_session_start("sess-other", project.root, "startup")
    team_service.claim_task(mine.id, session_ref="sess-other")
    _become(agent, tmux, monkeypatch, pid=PANE_PID, role="coder")
    again = _clear("sess-c2", "sess-c3", project)
    assert "Do not take another" not in again and "ASSIGNED TO YOU" not in again


def test_a_task_closed_behind_the_services_back_still_ends_the_assignment(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The briefing forgets a closed task itself, for a row spawned before the
    rule existed or a task closed by a store write that knew no rows."""
    mine = _task(project, "closed by hand")
    agent, first = _spawned(project, "coder", mine.id, tmux, monkeypatch)
    with store_session() as store:
        store.set_task_status(mine.id, "dropped")
    assert _row(agent.id).task_id == mine.id, "the premise: the row still names it"

    board = team_service.hook_session_start(first, project.root, "startup")

    assert "ASSIGNED TO YOU" not in board and "--kind question" not in board
    assert _row(agent.id).task_id is None


def test_the_first_prompt_board_names_the_claim_holder_after_the_move(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The heartbeat's first-prompt door binds a fleet row too, and it computed
    the assignment as an ARGUMENT of the board render — after the tasks had been
    read — so the board it handed the agent showed the claim under the old id
    (review of #135). A session-start hook that failed open is how a session
    first meets the orchestrator on a prompt."""
    mine = _task(project, "the task this coder is for")
    _agent, first = _spawned(project, "coder", mine.id, tmux, monkeypatch)
    team_service.hook_session_start(first, project.root, "startup")
    team_service.claim_task(mine.id, session_ref=first)
    team_service.hook_session_end(first, project.root, reason="clear")

    board = team_service.hook_prompt_heartbeat("sess-c2", project.root)

    assert f"[doing @{team_service.short_id('sess-c2')}] the task this coder is for" in board
    assert f"@{team_service.short_id(first)}" not in board, "the old holder is gone from the board"
    assert "You are the one working it" in board


def test_task_next_survives_a_fleet_row_it_cannot_read(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_assignment` was guarded in round 3 and `task next` reaches the same
    lookup by another door. A row that will not parse turned "which task comes
    first" into a hard failure of the core work loop — for plain CLI callers
    too, which had no such dependency before this PR (review of #116, round 4)."""
    older = _task(project, "the older task")
    _task(project, "the task this coder is for")
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    team_service.hook_session_start("sess-plain-r4", project.root, "startup")

    def boom(self: SqliteStore, project_id: str, session_id: str) -> FleetAgent | None:
        raise sqlite3.OperationalError("no such column: worktree")

    monkeypatch.setattr(SqliteStore, "fleet_agent_for_session", boom)

    picked = team_service.next_task(
        role="coder", claim=True, session_ref="sess-plain-r4", cwd=project.root
    )

    assert picked is not None and picked.id == older.id, "the pool order still works"


def test_spawning_for_a_finished_task_is_refused(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Harmless when the id only named a label; now it would brief an agent on
    work that is over and hold a slot for nothing."""
    task = _task(project, "already shipped")
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    team_service.hook_session_start("sess-x", project.root, "startup")
    team_service.claim_task(task.id, session_ref="sess-x")
    team_service.finish_task(task.id, session_ref="sess-x")
    with pytest.raises(fleet_service.FleetError, match="is done"):
        fleet_service.spawn(project, "coder", task_id=task.id, worktree=False)


def test_a_stopped_agents_row_is_not_resurrected_by_a_late_hook(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hook runs in another process from ``fleet stop``; a whole-row write
    from a stale snapshot brought a stopped agent back to life (review)."""
    mine = _task(project, "some task")
    agent, first = _spawned(project, "coder", mine.id, tmux, monkeypatch)
    fleet_service.stop(project, agent.label)

    board = team_service.hook_session_start(first, project.root, "startup")

    assert "ASSIGNED TO YOU" not in board, "an ended row assigns nothing"
    assert _row(agent.id).ended_at is not None, "still ended"


def test_no_assignment_line_without_the_fleet_env(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plain `launch coder` (no fleet row) reads exactly as before."""
    _task(project, "some task")
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    monkeypatch.delenv("AISQUARE_FLEET_AGENT", raising=False)
    board = team_service.hook_session_start("sess-plain-6", project.root, "startup")
    assert "ASSIGNED TO YOU" not in board
    assert "Your standing cycle (coder)" in board


# --- shutdown: the operator's off switch, and the one path that may end unconfirmed rows ---


def _session_of(project: ProjectInfo, socket: str | None = None) -> str:
    """The qualified ``<socket>:<session>`` the report names for this project."""
    return f"{socket or fleet_service.settings().tmux_socket}:asq-{_codename(project)}"


def test_shutdown_stops_every_agent_records_each_row_and_takes_its_session_down(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    manager = fleet_service.spawn(project, "manager").agent
    coder = _coder(project)

    report = fleet_service.shutdown(project)

    assert [a.label for a in report.stopped] == ["manager", "coder-1"], (
        "the manager goes first: it is the one agent that spawns others mid-shutdown"
    )
    assert [a.exit_status for a in report.stopped] == [0, 0], "the graceful path has a status"
    assert report.recorded == [] and report.failed == []
    assert sorted(tmux.killed) == sorted([manager.pane_id, coder.pane_id]), "each window killed"
    assert report.sessions_absent == [_session_of(project)] and report.sessions_killed == []
    assert not tmux.running, "killing the last window took the session, and so the server"
    assert not tmux.server_killed, "the SERVER is never killed — only the fleet's sessions"
    assert sorted(_events(project, "agent_exited")) == ["coder-1 exited (0)", "manager exited (0)"]
    with store_session() as store:
        assert store.fleet_agents(project.id, live_only=True) == []
    assert fleet_service.list_agents(project) == [], "no live agent is left to list"


def test_shutdown_force_records_no_exit_status_and_still_releases_the_claims(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """``--force`` skips the ``/exit``, so there is no ``SessionEnd`` hook to release
    the agent's claims and no exit status to record — a status is only ever read from
    a pane that already reads dead. Both were claimed as facts by the first draft."""
    coder = _coder(project)
    _board_session(coder, "working")
    task = _add_task(project, "wire the auth callback")
    assert coder.session_id is not None
    with store_session() as store:
        lease = datetime.now(tz=UTC) + timedelta(hours=1)
        assert store.claim_task(task.id, coder.session_id, lease)

    report = fleet_service.shutdown(project, force=True)

    assert tmux.typed == [], "no /exit was typed"
    assert [a.exit_status for a in report.stopped] == [None], "force kills a live pane: no status"
    assert report.claims_released == [task.id]
    with store_session() as store:
        held = store.get_task(task.id)
        session = store.get_session(coder.session_id)
    assert held is not None and held.status == "todo" and held.claimed_by is None
    assert session is not None and session.ended_at is not None, "the board session is retired"


def test_shutdown_records_rows_lost_with_a_reason_when_the_server_was_killed_by_hand(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """Measured 2026-09-10: after a hand-run `tmux -L asq kill-server` the rows read
    `unknown (tmux unavailable)` and `reap` reaped 0 — correctly, it could not ask.
    The operator's `shutdown` is the word that resolves it."""
    coder = _coder(project)
    tmux.running = False  # the shape kill-server leaves: binary present, server gone
    assert fleet_service.status_of(coder).state == "unknown", "the control: reap may not guess"
    before = fleet_service.reap(project)
    assert before.lost == [] and before.ended == []

    report = fleet_service.shutdown(project)

    assert [row.agent.id for row in report.recorded] == [coder.id]
    assert "no server answered on socket" in report.recorded[0].reason
    assert report.stopped == [], "nothing answered, so nothing was 'stopped'"
    assert report.servers_absent == [coder.tmux_socket]
    assert report.sessions_killed == [] and report.sessions_absent == []
    assert report.recorded[0].agent.ended_at is not None
    assert report.recorded[0].agent.exit_status is None
    assert _events(project, "agent_exited") == [], "lost is recorded, not announced as an exit"
    assert fleet_service.status_of(report.recorded[0].agent).state == "exited", "the row says so"
    with store_session() as store:
        assert store.fleet_agents(project.id, live_only=True) == []


def test_shutdown_records_rows_lost_over_the_real_tmux_wrapper(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same absent-socket path against the REAL ``TmuxServer`` over a refusing
    runner (``_unreachable_server``), not the fake: every swallow of a non-zero exit
    is exercised, so ``answers()`` reading False here is tmux's own behaviour rather
    than the fake's ``running`` flag agreeing with the service."""
    coder = _coder(project)
    dead = _unreachable_server()
    monkeypatch.setattr(fleet_service, "server", lambda config=None: dead)

    report = fleet_service.shutdown(project)

    assert [row.agent.id for row in report.recorded] == [coder.id]
    assert report.stopped == [] and report.failed == []
    assert report.servers_absent == [coder.tmux_socket]
    assert report.sessions_killed == [] and report.sessions_absent == []


def test_shutdown_defaults_to_one_project_and_all_reaches_every_project(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, plain_project: ProjectInfo
) -> None:
    """Scope is ``reap``'s: an operator with two projects running who means "take
    THIS project's fleet down" must not lose the other project's agents."""
    here = _coder(project)
    there = fleet_service.spawn(plain_project, "coder", worktree=False).agent

    scoped = fleet_service.shutdown(project, force=True)

    assert [a.id for a in scoped.stopped] == [here.id]
    assert [s.agent.id for s in fleet_service.list_agents(plain_project)] == [there.id]
    assert f"asq-{_codename(plain_project)}" in tmux.sessions, "the other session is untouched"

    every = fleet_service.shutdown(force=True)

    assert [a.id for a in every.stopped] == [there.id]
    assert fleet_service.list_agents(plain_project) == []


def test_shutdown_counts_an_agent_that_exited_on_its_own_as_stopped(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The third falsehood in the same family: an agent that exits between the
    snapshot and its turn makes ``stop`` raise ``NoSuchAgent``. Reporting that as
    LEFT LIVE is false in every part — the row IS ended, nothing needs stopping —
    and would make a clean shutdown exit 1; recording it lost would drop the exit
    status the row already has."""
    coder = _coder(project)
    real_row = fleet_service._shutdown_row

    def exit_first(*args: object, **kwargs: object) -> None:
        with store_session() as store:  # its own SessionEnd hook got there first
            store.end_fleet_agent(coder.id, exit_status=7)
        real_row(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(fleet_service, "_shutdown_row", exit_first)

    report = fleet_service.shutdown(project)

    assert [(a.label, a.exit_status) for a in report.stopped] == [("coder-1", 7)]
    assert report.failed == [] and report.recorded == []


def test_shutdown_reaches_a_forgotten_projects_live_rows(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """A tombstoned registration can still hold live rows (``forget`` reads liveness
    and writes the tombstone in separate statements, and a concurrent ``spawn``
    revives the row). Hidden from the scan, that agent's pane went down with the
    session while its row stayed live — and NOTHING could reconcile it afterwards."""
    coder = _coder(project)
    with store_session() as store:
        store.forget_project(project.id)
        assert [p.id for p in store.list_projects()] == [], "hidden from every ordinary read"

    report = fleet_service.shutdown(force=True)

    assert [a.id for a in report.stopped] == [coder.id]
    with store_session() as store:
        assert store.fleet_agents(project.id, live_only=True) == []


def test_shutdown_refuses_when_tmux_is_not_usable_and_touches_no_row(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """``answers()`` is False for "no usable tmux" as well as "no server", and a tmux
    that left `PATH` leaves every agent RUNNING. Ending those rows as lost hid live
    agents from every listing and handed their worktrees to the next reap."""
    coder = _coder(project)
    tmux.installed = False

    with pytest.raises(FleetUnavailable, match="tmux is not installed"):
        fleet_service.shutdown(project)

    tmux.installed = True
    assert [s.agent.id for s in fleet_service.list_agents(project)] == [coder.id]
    assert tmux.killed == [] and tmux.killed_sessions == []


def test_shutdown_refuses_when_the_socket_cannot_be_asked(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """A 30 s command timeout on a wedged server raises a bare ``TmuxError`` from
    ``answers()``, which is not a ``FleetError``: unguarded it escaped as a traceback
    (and `--json` printed nothing) in exactly the case this command exists for.
    "The socket did not answer" and "we could not ask" are different facts."""
    coder = _coder(project)
    tmux.answers_raises = "display-message timed out after 30.0s"

    with pytest.raises(FleetError, match="could not be asked whether a server is running"):
        fleet_service.shutdown(project)

    tmux.answers_raises = None
    assert [s.agent.id for s in fleet_service.list_agents(project)] == [coder.id]
    assert tmux.killed == [] and tmux.killed_sessions == []


def test_shutdown_refuses_from_inside_the_fleets_own_server(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``fleet attach`` is the documented escape hatch and every fleet window runs
    ``aisquare`` inside that same server, so this is ordinary use. The kill SIGHUPs the
    pane running the command: the store writes have happened, and none of the report
    prints — losing the one thing this command offers over `tmux kill-server`."""
    coder = _coder(project)
    socket_path = fleet_service.server().socket_path()
    monkeypatch.setenv("TMUX", f"{socket_path},4242,0")

    with pytest.raises(FleetError, match="INSIDE the fleet's own tmux server"):
        fleet_service.shutdown(project)
    with pytest.raises(FleetError, match="detach"):
        fleet_service.shutdown_plan(project)

    assert [s.agent.id for s in fleet_service.list_agents(project)] == [coder.id]
    assert tmux.killed == [] and tmux.killed_sessions == []
    monkeypatch.setenv("TMUX", "/tmp/tmux-0/somebody-elses,4242,0")
    assert fleet_service.shutdown(project, force=True).stopped, "another socket is not this one"


def test_shutdown_keeps_a_row_live_when_its_pane_was_seen_alive_and_spares_its_session(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """``_verify_gone``'s refusal is a POSITIVE observation — the pane is still alive —
    and the first draft swallowed it with `except FleetError: pass` and ended the row
    anyway. Killing the session would be the same mistake by another route: the agent
    would die and its live row would sit on a dead pane."""
    coder = _coder(project)
    tmux.fail_input = True  # the server answers, but /exit does not go through

    report = fleet_service.shutdown(project)

    assert report.stopped == [] and report.recorded == []
    assert [row.agent.id for row in report.failed] == [coder.id]
    assert "its pane is still alive" in report.failed[0].reason
    assert report.sessions_left_up == [_session_of(project)]
    assert report.sessions_killed == [] and tmux.killed_sessions == []
    assert [s.agent.id for s in fleet_service.list_agents(project)] == [coder.id], "still live"


def test_shutdown_records_a_row_spawned_during_the_run(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The snapshot is taken before the first stop and each stop can cost the whole
    grace, so the window is real: the manager, a second terminal or the UI can spawn
    into it. Without this pass that row was neither stopped nor recorded and its pane
    went down with the session — a live row reading `unknown (tmux unavailable)`."""
    manager = fleet_service.spawn(project, "manager").agent
    late: list[FleetAgent] = []
    real_stop = fleet_service._stop_row

    def stop_and_spawn(*args: object, **kwargs: object) -> fleet_service.StopReceipt:
        if not late:  # the spawn lands while the manager is being stopped
            late.append(_coder(project))
        return real_stop(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(fleet_service, "_stop_row", stop_and_spawn)

    report = fleet_service.shutdown(project, force=True)

    assert [a.id for a in report.stopped] == [manager.id], "the only row in the snapshot"
    assert [row.agent.id for row in report.recorded] == [late[0].id]
    assert "spawned during the shutdown" in report.recorded[0].reason
    assert report.sessions_killed == [_session_of(project)], "the late row's session was killed"
    with store_session() as store:
        assert store.fleet_agents(project.id, live_only=True) == []


def test_shutdown_leaves_a_row_live_when_the_late_spawn_is_still_running(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A spawn that lands AFTER the kill phase brought a session of its own back up,
    and that agent is running. The re-read asks its pane before ending anything."""
    coder = _coder(project)
    spawned: list[FleetAgent] = []
    real_kill = fleet_service._kill_fleet_sessions

    def kill_then_spawn(*args: object, **kwargs: object) -> None:
        real_kill(*args, **kwargs)  # type: ignore[arg-type]
        spawned.append(_coder(project))

    monkeypatch.setattr(fleet_service, "_kill_fleet_sessions", kill_then_spawn)

    report = fleet_service.shutdown(project, force=True)

    assert [a.id for a in report.stopped] == [coder.id]
    assert report.recorded == [], "a running pane is never recorded lost"
    assert [row.agent.id for row in report.failed] == [spawned[0].id]
    assert "still alive" in report.failed[0].reason
    assert [s.agent.id for s in fleet_service.list_agents(project)] == [spawned[0].id]


def test_shutdown_leaves_a_late_row_live_when_its_pane_cannot_be_asked(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #121, round 2 (P1): a `pane_facts()` timeout on a row spawned during
    the run left `facts=None`, which fell through to `_record_lost` — a running
    agent's row ended, its claims released, and the report saying its session was
    killed. "Could not ask" is not "dead"."""
    coder = _coder(project)
    spawned: list[FleetAgent] = []
    real_kill = fleet_service._kill_fleet_sessions
    real_facts = tmux.pane_facts

    def kill_then_spawn(*args: object, **kwargs: object) -> None:
        real_kill(*args, **kwargs)  # type: ignore[arg-type]
        spawned.append(_coder(project))

    def facts_or_timeout(pane_id: str) -> PaneFacts | None:
        if spawned and pane_id == spawned[0].pane_id:
            raise TmuxError("timed out after 30 s (fake)")
        return real_facts(pane_id)

    monkeypatch.setattr(fleet_service, "_kill_fleet_sessions", kill_then_spawn)
    monkeypatch.setattr(tmux, "pane_facts", facts_or_timeout)

    report = fleet_service.shutdown(project, force=True)

    assert [a.id for a in report.stopped] == [coder.id]
    assert report.recorded == [], "an unqueryable pane is never recorded lost"
    assert [row.agent.id for row in report.failed] == [spawned[0].id]
    assert "could not be asked" in report.failed[0].reason
    assert report.incomplete_projects == [project.id]
    assert [s.agent.id for s in fleet_service.list_agents(project)] == [spawned[0].id], "live"


def test_shutdown_spares_the_old_name_session_that_hosts_a_left_live_pane(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """Review of #121, round 2: under `--all` the prefix sweep killed an `asq-*`
    session left under an OLD name even though the row inside it had just been
    marked LEFT LIVE — the spare rule only knew the project's CURRENT session name.
    It now follows the pane."""
    coder = _coder(project)
    current = _session_of(project).split(":", 1)[1]
    tmux.sessions["asq-old-name"] = tmux.sessions.pop(current)  # a failed rename
    tmux.fail_input = True  # the server answers, but /exit does not go through

    report = fleet_service.shutdown()  # every project: the prefix sweep runs

    assert [row.agent.id for row in report.failed] == [coder.id]
    socket = fleet_service.settings().tmux_socket
    assert f"{socket}:asq-old-name" in report.sessions_left_up
    assert "asq-old-name" not in tmux.killed_sessions
    assert coder.pane_id in tmux.facts, "the pane is untouched"
    assert [s.agent.id for s in fleet_service.list_agents(project)] == [coder.id]
    assert report.incomplete_projects == [project.id]


def test_shutdown_reports_a_failed_session_listing_as_a_partial_shutdown(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #121, round 2: a socket that answered the probe but whose
    `list-sessions` then raised was skipped in silence — an EMPTY report over a
    surviving session, which the CLI printed as `✓ fleet shut down`, exit 0."""
    tmux.spawn_window("asq-stale-otter", name="manager", cwd=project.root, command=["cat"])

    def listing_times_out() -> list[str]:
        raise TmuxError("timed out after 30 s (fake)")

    monkeypatch.setattr(tmux, "list_sessions", listing_times_out)

    report = fleet_service.shutdown()

    assert report.sessions_killed == [] and "asq-stale-otter" in tmux.sessions
    socket = fleet_service.settings().tmux_socket
    assert any(
        s.startswith(f"{socket}:*") and "could not list sessions" in s
        for s in report.sessions_failed
    ), report.sessions_failed
    # `sessions_failed` is what the CLI reads as PARTIAL (⚠ + exit 1), so the
    # surviving session can no longer hide behind a clean report.


def test_shutdown_keeps_a_surviving_project_paused(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """Review of #121, round 2: when the paused manager could not be stopped, the
    row stayed live and its session was spared — but the cleanup still called
    `resume()`, removing "spawn nothing while fleet-paused" precisely while the
    operator was trying to shut it down."""
    _coder(project)
    fleet_service.pause(project)
    tmux.fail_input = True  # the server answers, but /exit does not go through

    report = fleet_service.shutdown(project)

    assert report.failed and report.sessions_left_up
    assert fleet_service.is_paused(project), "a surviving fleet keeps its standing order"
    assert report.paused_cleared == [] and report.paused_kept == [project.root.name]


def test_shutdown_spared_panes_are_scoped_to_their_socket(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #121, round 3: pane ids are unique only within one tmux server
    (every server starts at %0). A failed `%1` on the old socket must not spare an
    unrelated `asq-*` session holding `%1` on the current socket."""
    old = FakeTmux()
    tmux.per_socket["asq-old"] = old
    _settings(monkeypatch, tmux_socket="asq-old")
    stale = _coder(project)  # %1 on asq-old
    old.fail_input = True  # its /exit does not go through: the row will be LEFT LIVE
    _settings(monkeypatch, tmux_socket="asq")
    tmux.spawn_window("asq-other", name="manager", cwd=project.root, command=["cat"])  # %1 on asq
    assert stale.pane_id == "%1" and tmux.sessions["asq-other"][0].pane_id == "%1"

    report = fleet_service.shutdown()  # every project: both sockets, the prefix sweep runs

    assert [row.agent.id for row in report.failed] == [stale.id]
    assert f"asq-old:{_session_of(project).split(':', 1)[1]}" in report.sessions_left_up
    assert "asq:asq-other" in report.sessions_killed, "an unrelated %1 elsewhere is not spared"
    assert "asq-other" not in tmux.sessions


def test_shutdown_reconciles_a_late_row_on_an_initially_absent_socket(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #121, round 3: the initial reachability snapshot is stale by the
    final pass. A replacement manager started by another terminal on a socket the
    probe found ABSENT was skipped — its live row unreported, the project "down",
    its pause cleared, exit 0."""
    old = FakeTmux()
    tmux.per_socket["asq-old"] = old
    _settings(monkeypatch, tmux_socket="asq-old")
    stale = _coder(project)
    old.running = False  # killed outside the CLI before the shutdown started
    fleet_service.pause(project)
    late: list[FleetAgent] = []
    real_kill = fleet_service._kill_fleet_sessions

    def kill_then_replacement(*args: object, **kwargs: object) -> None:
        real_kill(*args, **kwargs)  # type: ignore[arg-type]
        late.append(_coder(project))  # spawn_window brings the old socket's server back up

    monkeypatch.setattr(fleet_service, "_kill_fleet_sessions", kill_then_replacement)

    report = fleet_service.shutdown(project, force=True)

    assert [row.agent.id for row in report.recorded] == [stale.id]
    assert [row.agent.id for row in report.failed] == [late[0].id]
    assert "still alive" in report.failed[0].reason
    assert report.incomplete_projects == [project.id]
    assert fleet_service.is_paused(project), "a surviving replacement keeps the standing order"
    assert report.paused_kept == [project.root.name] and report.paused_cleared == []


def test_shutdown_keeps_a_late_row_live_when_tmux_left_path_before_the_final_pass(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #121, round 4 (P1): `answers()` returns False for an unavailable
    BINARY as well as for an absent server, so the round-3 fresh probe read "tmux
    left PATH after the initial guard" as "no server" and recorded a still-running
    late agent lost — session ended, claim released, pause cleared."""
    coder = _coder(project)
    fleet_service.pause(project)
    late: list[FleetAgent] = []
    real_kill = fleet_service._kill_fleet_sessions

    def kill_spawn_then_lose_tmux(*args: object, **kwargs: object) -> None:
        real_kill(*args, **kwargs)  # type: ignore[arg-type]
        late.append(_coder(project))
        tmux.installed = False  # the client vanishes; the server and the pane are still up

    monkeypatch.setattr(fleet_service, "_kill_fleet_sessions", kill_spawn_then_lose_tmux)

    report = fleet_service.shutdown(project, force=True)

    assert [a.id for a in report.stopped] == [coder.id]
    assert report.recorded == [], "an unaskable socket is never proof of a dead pane"
    assert [row.agent.id for row in report.failed] == [late[0].id]
    assert "could not be asked" in report.failed[0].reason
    assert report.incomplete_projects == [project.id]
    tmux.installed = True
    assert fleet_service.is_paused(project) and report.paused_kept == [project.root.name]
    assert [s.agent.id for s in fleet_service.list_agents(project)] == [late[0].id], "still live"


def test_shutdown_keeps_a_late_row_live_when_the_client_fails_at_execution(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #121, round 5 (P1): `binary()` succeeding does not stop `answers()`
    from swallowing an execution-time `TmuxUnavailable` (an executable whose
    interpreter is missing) into False — so the round-4 guard still recorded a
    live late agent lost. `reachable()` propagates it: unknown, row kept."""
    coder = _coder(project)
    fleet_service.pause(project)
    late: list[FleetAgent] = []
    real_kill = fleet_service._kill_fleet_sessions

    def kill_spawn_then_break_the_client(*args: object, **kwargs: object) -> None:
        real_kill(*args, **kwargs)  # type: ignore[arg-type]
        late.append(_coder(project))
        tmux.exec_unavailable = True  # `which` still finds it; running it fails

    monkeypatch.setattr(fleet_service, "_kill_fleet_sessions", kill_spawn_then_break_the_client)

    report = fleet_service.shutdown(project, force=True)

    assert [a.id for a in report.stopped] == [coder.id]
    assert report.recorded == [], "a client that cannot run is never proof of a dead pane"
    assert [row.agent.id for row in report.failed] == [late[0].id]
    assert "could not be asked" in report.failed[0].reason
    tmux.exec_unavailable = False
    assert fleet_service.is_paused(project) and report.paused_kept == [project.root.name]
    assert [s.agent.id for s in fleet_service.list_agents(project)] == [late[0].id], "still live"


@pytest.mark.parametrize("how", ["missing", "not_runnable", "denied"])
def test_shutdown_refuses_when_the_client_goes_away_between_the_guard_and_the_probe(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    how: str,
) -> None:
    """Review of #121, round 6 (P1): the initial socket map came from `answers()`,
    which swallows an unavailable client into False — so a client that vanished or
    failed at execution AFTER `_require_usable_tmux` read as "no server", and every
    row on that socket was recorded lost before a single stop was attempted, panes
    alive, claims released, pause cleared. The strict probe refuses instead."""
    coder = _coder(project)
    fleet_service.pause(project)
    real_targets = fleet_service._shutdown_targets

    def targets_then_lose_the_client(*args: object, **kwargs: object) -> object:
        result = real_targets(*args, **kwargs)  # type: ignore[arg-type]
        if how == "missing":
            tmux.installed = False
        elif how == "not_runnable":
            tmux.exec_unavailable = True
        else:
            tmux.socket_denied = True  # round 7: exit 1 with (Permission denied), server alive
        return result

    monkeypatch.setattr(fleet_service, "_shutdown_targets", targets_then_lose_the_client)

    with pytest.raises(fleet_service.FleetError, match="could not be asked"):
        fleet_service.shutdown(project, force=True)

    tmux.installed, tmux.exec_unavailable, tmux.socket_denied = True, False, False
    assert [s.agent.id for s in fleet_service.list_agents(project)] == [coder.id], "untouched"
    assert coder.pane_id in tmux.facts, "the pane was never touched either"
    assert fleet_service.is_paused(project), "and the standing order stands"


def test_reachable_tells_an_absent_server_from_a_denied_socket() -> None:
    """Review of #121, round 7 (P1): tmux exits 1 for BOTH a server that is not there
    (`No such file or directory`) and a live socket this user may not open
    (`Permission denied`). Only the first is "no server"; the exit code alone must
    never decide, or a denied live fleet is recorded lost."""

    def scripted(stderr: str, code: int = 1) -> TmuxServer:
        return _real_server(lambda argv, stdin: Completed(code, "", stderr))

    assert scripted(_UNREACHABLE).reachable() is False
    assert scripted("no server running on /tmp/tmux-1000/asq").reachable() is False
    assert scripted("", code=0).reachable() is True
    with pytest.raises(TmuxError, match="Permission denied"):
        scripted("error connecting to /tmp/tmux-1000/asq (Permission denied)").reachable()
    with pytest.raises(TmuxError, match="exited 1"):
        scripted("").reachable()  # an unexplained refusal is still not an absence
    # answers() keeps its never-raises contract: a denied socket is "not an answer"
    assert scripted("error connecting to /tmp/tmux-1000/asq (Permission denied)").answers() is False


def test_shutdown_leaves_a_late_row_live_when_its_socket_is_denied(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The final pass with a socket that is there but refuses this user: `TmuxError`,
    so the late row is unknown — live, claim kept, pause kept — not lost."""
    coder = _coder(project)
    fleet_service.pause(project)
    late: list[FleetAgent] = []
    real_kill = fleet_service._kill_fleet_sessions

    def kill_spawn_then_deny(*args: object, **kwargs: object) -> None:
        real_kill(*args, **kwargs)  # type: ignore[arg-type]
        late.append(_coder(project))
        tmux.socket_denied = True

    monkeypatch.setattr(fleet_service, "_kill_fleet_sessions", kill_spawn_then_deny)
    report = fleet_service.shutdown(project, force=True)
    tmux.socket_denied = False

    assert [a.id for a in report.stopped] == [coder.id] and report.recorded == []
    assert [row.agent.id for row in report.failed] == [late[0].id]
    assert "could not be asked" in report.failed[0].reason
    assert fleet_service.is_paused(project) and report.paused_kept == [project.root.name]


def test_shutdown_inside_guard_keeps_commas_in_the_socket_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #121, round 7: a socket name (or TMUX_TMPDIR) may contain commas,
    which tmux preserves in `$TMUX`; `split(",")[0]` truncated the path and let the
    guard accept the very pane it was about to kill. The two trailing fields are the
    numbers — everything before them is the path."""
    srv = TmuxServer("asq,shared")  # the REAL path derivation, no tmux run
    socket_path = srv.socket_path()
    assert "," in str(socket_path)
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: srv)
    config = FleetSettings(tmux_socket="asq,shared")

    monkeypatch.setenv("TMUX", f"{socket_path},4242,0")
    with pytest.raises(FleetError, match="INSIDE the fleet's own tmux server"):
        fleet_service._refuse_from_inside(["asq,shared"], config)

    # the truncation the old parse produced is NOT this socket — and a different
    # comma-free socket is not either
    monkeypatch.setenv("TMUX", f"{str(socket_path).split(',')[0]},4242,0")
    fleet_service._refuse_from_inside(["asq,shared"], config)
    monkeypatch.setenv("TMUX", "/tmp/tmux-0/somebody-elses,4242,0")
    fleet_service._refuse_from_inside(["asq,shared"], config)


def test_shutdown_reports_a_failed_final_scan_and_keeps_the_pause(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #121, round 7: a store that refused the final re-read returned
    silently — a late-spawned agent unreported, its project "down", its pause
    cleared, exit 0. A scan that did not run cannot vouch for anything."""
    coder = _coder(project)
    fleet_service.pause(project)
    late: list[FleetAgent] = []
    real_kill = fleet_service._kill_fleet_sessions
    real_targets = fleet_service._shutdown_targets
    calls = {"n": 0}

    def kill_then_spawn(*args: object, **kwargs: object) -> None:
        real_kill(*args, **kwargs)  # type: ignore[arg-type]
        late.append(_coder(project))

    def targets_failing_the_second_time(*args: object, **kwargs: object) -> object:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("database is locked (fake)")
        return real_targets(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(fleet_service, "_kill_fleet_sessions", kill_then_spawn)
    monkeypatch.setattr(fleet_service, "_shutdown_targets", targets_failing_the_second_time)

    report = fleet_service.shutdown(project, force=True)

    assert [a.id for a in report.stopped] == [coder.id]
    assert report.late_scan_failed is not None and "database is locked" in report.late_scan_failed
    assert report.incomplete_projects == [project.id]
    assert fleet_service.is_paused(project) and report.paused_kept == [project.root.name]
    assert [s.agent.id for s in fleet_service.list_agents(project)] == [late[0].id], "still live"


def test_shutdown_does_not_revive_a_forgotten_project_while_checking_its_pause(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """Review of #121, round 7: `is_paused()` resolves the board through
    `ensure_project`, which clears a tombstone — so `shutdown --all` undid
    `project forget` on a registration with no agents, no session and no signal."""
    from aisquare.services import project as project_service

    with store_session() as store:
        assert any(p.id == project.id for p in store.list_projects())
    project_service.forget(project.id)
    with store_session() as store:
        assert not any(p.id == project.id for p in store.list_projects()), "tombstoned"

    report = fleet_service.shutdown()  # every project: reads past tombstones on purpose

    assert report.paused_cleared == [] and report.paused_kept == []
    with store_session() as store:
        assert not any(p.id == project.id for p in store.list_projects()), "still forgotten"


def test_shutdown_kills_only_the_fleets_own_sessions(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """``tmux kill-server`` is server-scoped; the fleet's claim is only ever to
    ``asq-<codename>`` sessions. Reproduced against the first draft: a socket holding
    one hand-made session and no fleet rows reported destroying nothing and destroyed
    it — and `[fleet] tmux_socket` is a free-form string with no validator, so it can
    be pointed at the operator's personal server."""
    coder = _coder(project)
    tmux.spawn_window("by-hand", name="notes", cwd=project.root, command=["cat"])

    report = fleet_service.shutdown(project, force=True)

    assert [a.id for a in report.stopped] == [coder.id]
    assert "by-hand" in tmux.sessions, "a session the fleet does not own survives"
    assert not tmux.server_killed and tmux.running, "and so does the server holding it"
    assert report.sessions_absent == [_session_of(project)], "the fleet's own went with its window"


def test_shutdown_sweeps_a_session_left_under_an_old_name_only_with_all(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """``rename`` fails open when tmux is unreachable and says a session can be left
    under the OLD name; ``attach`` and ``reap`` both know that shape exists. It is the
    fleet's session, so an every-project shutdown sweeps it — a scoped one does not,
    because nothing attributes it to a project."""
    tmux.spawn_window("asq-stale-otter", name="manager", cwd=project.root, command=["cat"])

    scoped = fleet_service.shutdown(project)
    assert scoped.sessions_killed == [], "not this project's session"
    assert "asq-stale-otter" in tmux.sessions

    every = fleet_service.shutdown()

    assert every.sessions_killed == [f"{fleet_service.settings().tmux_socket}:asq-stale-otter"]
    assert "asq-stale-otter" not in tmux.sessions


def test_shutdown_reports_a_session_it_could_not_kill_as_failed_not_killed(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kill that raised must never be reported as a kill. The first draft appended to
    ``servers_killed`` outside the ``suppress``, so a wedged server that timed out was
    reported killed — over panes that were still alive with their rows already ended."""
    coder = _coder(project)
    tmux.spawn_window(f"asq-{_codename(project)}", name="by-hand", cwd=project.root, command=["c"])

    def wedged(session: str) -> None:
        raise TmuxError("kill-session timed out after 30.0s")

    monkeypatch.setattr(tmux, "kill_session", wedged)
    report = fleet_service.shutdown(project, force=True)

    assert [a.id for a in report.stopped] == [coder.id]
    assert report.sessions_failed == [_session_of(project)] and report.sessions_killed == []


def test_shutdown_decides_reachability_per_socket(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One socket gone and one healthy is the state this command decides row by row —
    and the state no service test could produce while one fake answered for every
    socket. A row on the gone socket is recorded lost; the healthy socket's agent is
    stopped, and its server is not touched on the strength of the other's absence."""
    old = FakeTmux()
    tmux.per_socket["asq-old"] = old
    _settings(monkeypatch, tmux_socket="asq-old")
    stale = _coder(project)
    old.running = False  # and then that server was killed outside the CLI
    _settings(monkeypatch, tmux_socket="asq")
    fresh = _coder(project)
    assert (stale.tmux_socket, fresh.tmux_socket) == ("asq-old", "asq")

    report = fleet_service.shutdown(project, force=True)

    assert [a.id for a in report.stopped] == [fresh.id]
    assert [row.agent.id for row in report.recorded] == [stale.id]
    assert "asq-old" in report.recorded[0].reason
    assert report.servers_absent == ["asq-old"]
    assert tmux.killed == [fresh.pane_id] and old.killed == []


def test_shutdown_asks_each_socket_only_about_the_sessions_that_lived_there(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    plain_project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #203, round 4. The session list was keyed on the project's
    codename alone and asked of EVERY socket in scope, so with rows on an old
    socket and today's, a project that only ever lived on one was probed on the
    other too — and the report gained "``<other>:asq-a`` was already gone" for a
    session that never existed there, beside the real lines. A project's session
    is looked for on the sockets its rows live on."""
    old = FakeTmux()
    tmux.per_socket["asq-old"] = old
    _settings(monkeypatch, tmux_socket="asq-old")
    on_old = _coder(project)  # this project's fleet lives on the old socket only
    _settings(monkeypatch, tmux_socket="asq")
    on_new = _coder(plain_project)  # the other project's on today's
    assert (on_old.tmux_socket, on_new.tmux_socket) == ("asq-old", "asq")

    report = fleet_service.shutdown(None, force=True)

    assert sorted(a.id for a in report.stopped) == sorted([on_old.id, on_new.id])
    assert sorted(report.sessions_absent) == sorted(
        [_session_of(project, "asq-old"), _session_of(plain_project, "asq")]
    ), "each session is named once, on the socket it lived on"
    assert report.sessions_killed == [] and report.incomplete_projects == []


def test_stop_returns_the_claims_it_released(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #203, round 4. ``stop`` released a dead pane's claims behind a
    ``release_claims`` flag that ``shutdown`` turned OFF so it could release
    again and count — two paths deciding the pane was dead. One release, in
    ``stop``, and the receipt says what it returned to the pool."""
    mine = _task(project, "the task this coder is for")
    agent, first = _spawned(project, "coder", mine.id, tmux, monkeypatch)
    team_service.hook_session_start(first, project.root, "startup")
    team_service.claim_task(mine.id, session_ref=first)

    receipt = fleet_service.stop(project, agent.label, force=True)

    assert receipt.agent.ended_at is not None
    assert [t.id for t in receipt.released] == [mine.id]
    assert _task_now(mine.id).claimed_by is None and _task_now(mine.id).status == "todo"
    # And a second look finds nothing left to release: the receipt IS the count.
    assert fleet_service.shutdown(project, force=True).claims_released == []


def test_a_release_the_store_refuses_costs_the_release_and_never_the_stop(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of the fold. With the release moved into ``stop``, a store that
    refused it AFTER the row was ended raised out of ``stop`` — and ``shutdown``
    reported a dead, ended row as LEFT LIVE, kept the pause and exited 1. The
    release is the courtesy owed after the stop: refused, it is NAMED on the
    receipt and in the report, and the row is counted where it belongs."""
    mine = _task(project, "the task this coder is for")
    agent, first = _spawned(project, "coder", mine.id, tmux, monkeypatch)
    team_service.hook_session_start(first, project.root, "startup")
    team_service.claim_task(mine.id, session_ref=first)

    def refuse(*args: object, **kwargs: object) -> list[TeamTask]:
        raise sqlite3.OperationalError("database is locked (fake)")

    monkeypatch.setattr(team_service, "release_agent_claims", refuse)
    receipt = fleet_service.stop(project, agent.label, force=True)

    assert receipt.agent.ended_at is not None, "the stop happened"
    assert receipt.released == [] and receipt.release_failed is not None
    assert "locked" in receipt.release_failed
    assert _task_now(mine.id).claimed_by == first, "the claim stayed — and is said to have"

    another = _task(project, "another")
    second, second_session = _spawned(project, "coder", another.id, tmux, monkeypatch)
    team_service.hook_session_start(second_session, project.root, "startup")
    team_service.claim_task(another.id, session_ref=second_session)
    report = fleet_service.shutdown(project, force=True)

    assert [a.id for a in report.stopped] == [second.id], "ended and counted as stopped"
    assert report.failed == [] and report.incomplete_projects == []
    assert len(report.release_failures) == 1 and second.label in report.release_failures[0]
    assert "locked" in report.release_failures[0]


def test_is_paused_opens_no_store_for_a_disabled_orchestrator(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of the fold. The enabled check moved inside the store session, so a
    disabled orchestrator paid a connect, WAL switch and migration to learn
    nothing can be paused. Asked first, before any connection."""
    opens = 0
    real_open = core_store.open_store

    def counting_open() -> ContextStore:
        nonlocal opens
        opens += 1
        return real_open()

    monkeypatch.setattr(core_store, "open_store", counting_open)
    monkeypatch.setenv("AISQUARE_TEAM", "0")

    assert fleet_service.is_paused(project) is False
    assert opens == 0, f"a disabled board opened the store {opens} time(s)"


def test_an_interrupt_during_the_stop_loop_does_not_kill_the_fleet(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of the fold. The kill, late-scan and pause phases ran in a
    ``finally``, so Ctrl-C during one agent's graceful /exit still SIGHUP'd every
    other session with no /exit, recorded them under a false reason, cleared the
    pauses and then re-raised with no report. ``_shutdown_row`` catches every
    ``Exception`` of its own, so the belt is for a fault in the loop — never for
    the operator's interrupt, which is theirs to have."""
    first = _coder(project)
    second = _coder(project)
    fleet_service.pause(project)
    real_row = fleet_service._shutdown_row
    turns: list[str] = []

    def interrupted_on_the_second(
        project_: ProjectInfo, agent: FleetAgent, *a: object, **k: object
    ) -> None:
        turns.append(agent.label)
        if len(turns) == 2:  # the first row is down; Ctrl-C lands on the second
            raise KeyboardInterrupt
        real_row(project_, agent, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(fleet_service, "_shutdown_row", interrupted_on_the_second)
    with pytest.raises(fleet_service.FleetInterrupted) as caught:
        fleet_service.shutdown(project, force=True)

    report = caught.value.report
    assert [a.id for a in report.stopped] == [first.id], "the row already down is reported"
    assert report.interrupted is not None and second.label in report.interrupted
    assert tmux.killed == [first.pane_id] and tmux.killed_sessions == [], (
        "nothing beyond the row in hand was taken down behind the operator"
    )
    assert _row(second.id).ended_at is None
    assert fleet_service.is_paused(project), "and the pause is still the manager's standing order"

    # The control: a FAULT in the loop still runs the kill phase — "shutdown means
    # down" holds when something other than a row breaks — and re-raises after.
    def faulted(*args: object, **kwargs: object) -> None:
        raise RuntimeError("a fault in the loop itself")

    monkeypatch.setattr(fleet_service, "_shutdown_row", faulted)
    with pytest.raises(RuntimeError, match="fault in the loop"):
        fleet_service.shutdown(project, force=True)
    assert tmux.killed_sessions == [_session_of(project).split(":", 1)[1]]


def test_an_interrupt_before_any_rows_turn_still_carries_the_report(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 6. The interrupt handler read the inner loop's variable, so a Ctrl-C
    landing before the first row's turn (a scope whose projects hold no live
    rows) raised ``UnboundLocalError`` inside the handler — no report, no 130,
    and the kill phase skipped. The row in hand is bound before the loop."""
    fleet_service.pause(project)
    real_targets = fleet_service._shutdown_targets(project)

    class InterruptsOnItsSecondWalk(list):  # type: ignore[type-arg]
        """The snapshot: walked once for the sockets, then the stop loop itself."""

        walks = 0

        def __iter__(self):  # type: ignore[no-untyped-def]
            type(self).walks += 1
            if type(self).walks == 2:
                raise KeyboardInterrupt
            return super().__iter__()

    monkeypatch.setattr(
        fleet_service, "_shutdown_targets", lambda project_: InterruptsOnItsSecondWalk(real_targets)
    )
    with pytest.raises(fleet_service.FleetInterrupted) as caught:
        fleet_service.shutdown(project, force=True)

    report = caught.value.report
    assert report.interrupted == (
        "interrupted before the first row was stopped; the rest of the fleet was left as it was"
    )
    assert report.stopped == [] and tmux.killed_sessions == []
    assert fleet_service.is_paused(project)


def test_a_scoped_shutdown_takes_down_a_leftover_session_on_todays_socket(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of the fold. A project's session was looked for only on the sockets
    its LIVE rows were on, plus today's when it had none at all — so with live
    rows on the old socket and a leftover session (dead remain-on-exit panes of
    rows long ended) standing on today's, a scoped shutdown stopped the rows,
    printed ✓ and left that session up. Today's socket is always asked."""
    old = FakeTmux()
    tmux.per_socket["asq-old"] = old
    _settings(monkeypatch, tmux_socket="asq-old")
    on_old = _coder(project)
    _settings(monkeypatch, tmux_socket="asq")
    leftover = _session_of(project).split(":", 1)[1]
    tmux.sessions[leftover] = []  # a session with no live row pointing at it

    report = fleet_service.shutdown(project, force=True)

    assert [a.id for a in report.stopped] == [on_old.id]
    assert f"asq:{leftover}" in report.sessions_killed, "the leftover on today's socket went too"
    assert report.incomplete_projects == []


def test_a_label_reused_mid_shutdown_stops_the_snapshots_row_and_not_the_new_agent(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of the fold. ``_shutdown_row`` held the snapshot's row but stopped
    by LABEL, so when that row exited and the manager spawned a new ``coder-1``
    before its turn, the new agent was stopped under the old one's turn, counted
    as stopped — and, its id absent from ``handled``, recorded lost as a late row
    as well: one agent in two lists, released twice. The row in hand is the row
    stopped; a row that ended meanwhile went away on its own."""
    old_row = _coder(project)
    real_probe = fleet_service._shutdown_probe
    new_ids: list[str] = []

    def exit_and_reuse_the_label(*args: object, **kwargs: object) -> dict[str, bool]:
        # Between the snapshot and the first row's turn: the old coder-1 exits
        # on its own (its hook ends the row) and a new coder-1 is spawned.
        with store_session() as store:
            store.end_fleet_agent(old_row.id, exit_status=3)
        new_ids.append(_coder(project).id)
        return real_probe(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(fleet_service, "_shutdown_probe", exit_and_reuse_the_label)
    report = fleet_service.shutdown(project, force=True)

    assert _row(new_ids[0]).label == "coder-1" == old_row.label, "the premise: one label, two rows"
    stopped = [a.id for a in report.stopped]
    recorded = [row.agent.id for row in report.recorded]
    assert stopped == [old_row.id], "the snapshot's row, with the status it exited with"
    assert report.stopped[0].exit_status == 3
    assert new_ids[0] not in stopped, "the new agent was not stopped under the old one's turn"
    assert recorded.count(new_ids[0]) + stopped.count(new_ids[0]) <= 1, "counted at most once"


def test_a_release_whose_board_event_fails_is_still_a_release(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of the fold. ``_release_session`` commits the release and THEN
    emits a ``task_released`` event per task; an event write that raised made
    ``release_agent_claims`` raise, and ``stop`` reported ``released=[]`` with
    "its claims could not be released" over tasks already back on the board.
    The rows are the record; the event is the announcement."""
    mine = _task(project, "the task this coder is for")
    agent, first = _spawned(project, "coder", mine.id, tmux, monkeypatch)
    team_service.hook_session_start(first, project.root, "startup")
    team_service.claim_task(mine.id, session_ref=first)
    real_emit = team_service._emit

    def refuse_task_released(
        store: object, project_id: str, kind: str, *a: object, **kw: object
    ) -> object:
        if kind == "task_released":
            raise sqlite3.OperationalError("team_event is locked (fake)")
        return real_emit(store, project_id, kind, *a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(team_service, "_emit", refuse_task_released)
    receipt = fleet_service.stop(project, agent.label, force=True)

    assert [t.id for t in receipt.released] == [mine.id], "released — the row says so"
    assert receipt.release_failed is not None and "board was not told" in receipt.release_failed
    assert mine.id in receipt.release_failed, "the task the manager will not hear about is named"
    assert _task_now(mine.id).status == "todo" and _task_now(mine.id).claimed_by is None


def test_reap_reports_a_refused_release_and_finishes_the_sweep(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 5. ``reap`` was given ``claims_released`` but not the courtesy rule:
    ``release_agent_claims`` raising after ``end_fleet_agent`` had committed took
    the whole sweep down — the remaining agents, every later project, the
    worktree pass, the nudges, and a traceback instead of a report."""
    mine = _task(project, "the task this coder is for")
    agent, first = _spawned(project, "coder", mine.id, tmux, monkeypatch)
    team_service.hook_session_start(first, project.root, "startup")
    team_service.claim_task(mine.id, session_ref=first)
    other = _coder(project)
    for row in (agent, other):
        tmux.facts[row.pane_id] = replace(tmux.facts[row.pane_id], dead=True, dead_status=0)

    def refuse(*args: object, **kwargs: object) -> object:
        raise sqlite3.OperationalError("database is locked (fake)")

    monkeypatch.setattr(team_service, "release_agent_claims", refuse)
    report = fleet_service.reap(project)

    assert sorted(a.id for a in report.ended) == sorted([agent.id, other.id]), "the sweep finished"
    assert len(report.release_failures) == 2
    assert all("could not be released" in f and "locked" in f for f in report.release_failures)
    assert report.claims_released == []


def test_a_just_in_case_session_that_cannot_be_asked_is_not_a_failed_kill(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 5. ``expected`` silenced only the confirmed-absent answer while the
    just-in-case probe was widened to every in-scope project, so a ``TmuxError``
    on today's socket — for a project whose rows all live on the old one — read
    as a failed kill: PARTLY shut down, exit 1, and the plan refused outright."""
    old = FakeTmux()
    tmux.per_socket["asq-old"] = old
    _settings(monkeypatch, tmux_socket="asq-old")
    on_old = _coder(project)
    _settings(monkeypatch, tmux_socket="asq")

    def cannot_ask(name: str) -> bool:
        raise TmuxError("error connecting to /tmp/tmux-501/asq (transient)")

    monkeypatch.setattr(tmux, "has_session_or_raise", cannot_ask)  # today's socket only

    plan = fleet_service.shutdown_plan(project)
    assert plan.sessions == [_session_of(project, "asq-old")], "the plan lists what it can see"

    report = fleet_service.shutdown(project, force=True)
    assert [a.id for a in report.stopped] == [on_old.id]
    assert report.sessions_failed == [] and report.incomplete_projects == [], (
        "a socket the project never lived on cannot fail its shutdown"
    )


def test_the_pause_pass_reads_and_clears_every_signal_through_one_store(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    plain_project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #203, round 4. ``is_paused`` and ``resume`` each opened a store
    of their own — connect, WAL switch, migrations — so the pause pass paid two
    opens per project. One connection now, by project id."""
    fleet_service.pause(project)
    fleet_service.pause(plain_project)
    opens = 0
    real_open = core_store.open_store

    def counting_open() -> ContextStore:
        # Counted where EVERY store connection is made — `store_session` in any
        # module goes through here — so a pass that opened its own through
        # `services.team` counts the same as one opened in `services.fleet`.
        nonlocal opens
        opens += 1
        return real_open()

    monkeypatch.setattr(core_store, "open_store", counting_open)
    report = fleet_service.ShutdownReport()

    fleet_service._clear_pause([(project, []), (plain_project, [])], report)

    assert sorted(report.paused_cleared) == sorted([project.root.name, plain_project.root.name])
    assert opens == 1, f"two projects, one connection — not {opens}"
    assert not fleet_service.is_paused(project) and not fleet_service.is_paused(plain_project)


def test_shutdown_clears_the_fleet_paused_signal(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """`pause` then `shutdown` used to leave the signal set, so the next
    `fleet spawn manager` came up staffing nothing (the manager's instructions say to
    spawn nothing while it is set) with no output from either command naming it."""
    _coder(project)
    fleet_service.pause(project)
    assert fleet_service.is_paused(project)

    report = fleet_service.shutdown(project, force=True)

    assert not fleet_service.is_paused(project)
    assert report.paused_cleared == [project.root.name]
    # A shutdown does not put a fleet-paused row on a board that had none.
    assert fleet_service.shutdown(project).paused_cleared == []


# --- round 8: the tmux wrapper must never read "could not ask" as "not there" ---


def _scripted(stderr: str, stdout: str = "", code: int = 1) -> TmuxServer:
    """The REAL wrapper over a runner that answers one fixed way — the sharp
    instrument for the absence/could-not-ask boundary (as in
    ``test_reachable_tells_an_absent_server_from_a_denied_socket``)."""
    return _real_server(lambda argv, stdin: Completed(code, stdout, stderr))


def test_reachable_raises_for_a_loader_failure_instead_of_reading_it_as_absent() -> None:
    """Review of #121, round 8 (P1): the absence regex matched a bare ``No such
    file or directory``, so the dynamic loader's exit-127 ``cannot open shared
    object file: No such file or directory`` (a shared library gone) read as an
    ABSENT SERVER — ``reachable()`` False while the fleet's panes were alive, and
    shutdown retired their rows. Absence is now only tmux's own socket
    diagnostics; a loader failure is "could not ask" and raises."""
    loader = (
        "aisquare: error while loading shared libraries: libevent-core.so: "
        "cannot open shared object file: No such file or directory"
    )
    with pytest.raises(TmuxError, match="cannot open shared object file"):
        _scripted(loader, code=127).reachable()
    assert _scripted(loader, code=127).server_absent() is False, "not an absent server"
    # tmux's real absence diagnostics still read as absent
    assert _scripted(_UNREACHABLE).reachable() is False
    assert _scripted("no server running on /tmp/tmux-1000/asq").reachable() is False


def test_strict_session_queries_raise_only_when_the_server_could_not_be_asked() -> None:
    """Review of #121, round 8 (P2): the lenient ``list_sessions``/``has_session``/
    ``list_windows`` turn EVERY non-zero exit into ``[]``/``False``/``[]``, so a
    denied socket reads as "nothing here". The strict twins the shutdown paths use
    return the empty answer ONLY for a confirmed absence — the server gone, or a
    live server with no such session/window — and raise for anything else."""
    denied = "error connecting to /tmp/tmux-1000/asq (Permission denied)"

    assert _scripted(_UNREACHABLE).sessions_or_raise() == []
    with pytest.raises(TmuxError, match="Permission denied"):
        _scripted(denied).sessions_or_raise()

    assert _scripted(_UNREACHABLE).has_session_or_raise("asq-x") is False
    assert _scripted("can't find session: asq-x").has_session_or_raise("asq-x") is False
    assert _scripted("", code=0).has_session_or_raise("asq-x") is True
    with pytest.raises(TmuxError, match="Permission denied"):
        _scripted(denied).has_session_or_raise("asq-x")

    assert _scripted(_UNREACHABLE).windows_or_raise("asq-x") == []
    assert _scripted("can't find window: asq-x").windows_or_raise("asq-x") == []
    with pytest.raises(TmuxError, match="Permission denied"):
        _scripted(denied).windows_or_raise("asq-x")


def test_pane_facts_or_raise_keeps_a_denied_probe_from_reading_as_a_dead_pane() -> None:
    """Review of #121, round 8 (P1): the final pane query returned ``None`` for a
    non-zero exit such as ``(Permission denied)``, so a late agent still running was
    recorded lost and its claim released. ``None`` now means only what tmux
    confirmed — an empty answer, or an absent server — and a denied probe raises."""
    with pytest.raises(TmuxError, match="Permission denied"):
        _scripted("error connecting to /tmp/tmux-1000/asq (Permission denied)").pane_facts_or_raise(
            "%1"
        )
    assert _scripted(_UNREACHABLE).pane_facts_or_raise("%1") is None  # server gone -> pane gone


def test_shutdown_does_not_kill_a_spared_old_name_session_it_could_not_enumerate(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #121, round 8 (P2): the old-name session holds a LEFT LIVE agent,
    but a transient ``list-panes`` failure became ``[]`` in the lenient wrapper — an
    empty ``hosted`` set that did NOT match the spared pane, so the session was
    killed and the live agent's pane went with it. The strict enumeration raises, so
    the session is reported failed and left standing."""
    coder = _coder(project)
    current = _session_of(project).split(":", 1)[1]
    tmux.sessions["asq-old-name"] = tmux.sessions.pop(current)  # a failed rename
    tmux.fail_input = True  # /exit does not go through: the row is LEFT LIVE

    real_windows = tmux.windows_or_raise

    def deny_old_name(session: str) -> list[WindowInfo]:
        if session == "asq-old-name":
            raise TmuxError("error connecting to /fake (Permission denied)")
        return real_windows(session)

    monkeypatch.setattr(tmux, "windows_or_raise", deny_old_name)

    report = fleet_service.shutdown()  # every project: the prefix sweep runs

    assert "asq-old-name" not in tmux.killed_sessions
    assert "asq-old-name" in tmux.sessions, "the session with the live pane still stands"
    assert coder.pane_id in tmux.facts, "the left-live pane is untouched"
    assert any(
        "asq-old-name" in s and "could not list its panes" in s for s in report.sessions_failed
    ), report.sessions_failed
    assert project.id in report.incomplete_projects


def test_shutdown_reports_a_denied_session_check_as_failed_not_absent(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #121, round 8 (P2): with the socket denied AFTER a session was
    listed, the lenient ``has_session`` returned False for the failed command and
    the session was reported ABSENT — a survivor read as already gone, success
    reported, pause cleared. The strict check raises, so it is reported failed and
    left standing."""
    tmux.spawn_window("asq-stray-otter", name="manager", cwd=project.root, command=["cat"])
    real_has = tmux.has_session_or_raise

    def deny(name: str) -> bool:
        if name == "asq-stray-otter":
            raise TmuxError("error connecting to /fake (Permission denied)")
        return real_has(name)

    monkeypatch.setattr(tmux, "has_session_or_raise", deny)

    report = fleet_service.shutdown()  # --all sweep reaches the stray session

    socket = fleet_service.settings().tmux_socket
    assert "asq-stray-otter" not in tmux.killed_sessions
    assert "asq-stray-otter" in tmux.sessions, "a survivor is not read as absent"
    failed = [s for s in report.sessions_failed if s.startswith(f"{socket}:asq-stray-otter")]
    assert failed and "could not be asked" in failed[0], "a denied check is a failed kill, with why"
    assert f"{socket}:asq-stray-otter" not in report.sessions_absent


def test_shutdown_keeps_a_late_dead_panes_observed_exit_status(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #121, round 8 (P2): a row spawned AFTER the kill phase that then
    exited on its own leaves tmux holding its pane DEAD with a real status. It was
    recorded lost with ``exit_status=None`` and "its session was killed under it" —
    a kill that never targeted it, an observed 42 discarded. It now joins
    ``stopped`` with the status tmux kept, the way every other self-exit is."""
    coder = _coder(project)
    late: list[FleetAgent] = []
    real_kill = fleet_service._kill_fleet_sessions

    def kill_then_late_exit(*args: object, **kwargs: object) -> None:
        real_kill(*args, **kwargs)  # type: ignore[arg-type]
        agent = _coder(project)
        tmux.die(agent.pane_id, 42)  # it came up, then exited on its own
        late.append(agent)

    monkeypatch.setattr(fleet_service, "_kill_fleet_sessions", kill_then_late_exit)

    report = fleet_service.shutdown(project, force=True)

    assert coder.id in {a.id for a in report.stopped}
    late_rows = [a for a in report.stopped if a.id == late[0].id]
    assert late_rows and late_rows[0].exit_status == 42, "the observed status is kept"
    assert all(row.agent.id != late[0].id for row in report.recorded), "not recorded lost"
    assert [s.agent.id for s in fleet_service.list_agents(project)] == [], "its row is ended"


def test_shutdown_keeps_the_claim_of_a_row_whose_pane_death_was_not_confirmed(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """Review of #121, round 9 (P1): ``--force`` over a window tmux would not kill
    still RETURNED from ``stop`` (the window-kill failure was suppressed), so the
    row joined ``stopped`` and ``_release_session`` ended its board session and
    handed its claimed task back to the board — while the agent kept running and
    could still be writing to it. Confirmed death is the only thing that releases
    a claim; an unconfirmed stop is reported LEFT LIVE.
    """
    coder = _coder(project)
    _board_session(coder, "working")
    task = _add_task(project, "wire the auth callback")
    assert coder.session_id is not None
    with store_session() as store:
        lease = datetime.now(tz=UTC) + timedelta(hours=1)
        assert store.claim_task(task.id, coder.session_id, lease)
    tmux.refuse_kills = True  # tmux answers; it just will not kill anything

    report = fleet_service.shutdown(project, force=True)

    assert report.stopped == [] and report.claims_released == []
    assert [row.agent.id for row in report.failed] == [coder.id]
    assert "still alive" in report.failed[0].reason
    assert [s.agent.id for s in fleet_service.list_agents(project)] == [coder.id], "row LEFT LIVE"
    assert tmux.facts[coder.pane_id].dead is False, "and its pane is still running"
    assert report.sessions_left_up == [_session_of(project)], "its session is spared with it"
    assert report.incomplete_projects == [project.id], "the project is NOT confirmed down"
    with store_session() as store:
        held = store.get_task(task.id)
        session = store.get_session(coder.session_id)
    assert held is not None and held.status == "doing" and held.claimed_by == coder.session_id
    assert session is not None and session.ended_at is None, "its board session is kept too"


def test_shutdown_removes_a_late_self_exited_pane_and_takes_its_session_down(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #121, round 9 (P2): ``_record_self_exit`` ended the late row but
    left tmux holding its pane (``remain-on-exit``), so its window, its session and
    the server behind it all outlived a shutdown that reported itself COMPLETE —
    the kill phase had already run, so nothing else would ever take them down.
    """
    _coder(project)
    late: list[FleetAgent] = []
    real_kill = fleet_service._kill_fleet_sessions

    def kill_then_late_exit(*args: object, **kwargs: object) -> None:
        real_kill(*args, **kwargs)  # type: ignore[arg-type]
        agent = _coder(project)  # a spawn that landed after the kill phase
        tmux.die(agent.pane_id, 42)  # …and exited on its own before this pass
        late.append(agent)

    monkeypatch.setattr(fleet_service, "_kill_fleet_sessions", kill_then_late_exit)

    report = fleet_service.shutdown(project, force=True)

    assert late[0].pane_id in tmux.killed, "the retained dead pane is removed"
    assert tmux.sessions == {}, "its session went with its last window"
    assert not tmux.running, "…and the server with the session: the fleet really is down"
    assert not tmux.server_killed, "the SERVER is still never killed — it exits on its own"
    assert [a.exit_status for a in report.stopped if a.id == late[0].id] == [42], "status kept"
    assert report.failed == [] and report.sessions_failed == []
    assert report.incomplete_projects == [], "nothing is left for the report to qualify"


def test_shutdown_reports_a_late_dead_pane_it_could_not_remove(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of round 9's P2: when tmux will not remove the late pane, the
    session it holds up is reported — never a completed shutdown over a live one."""
    _coder(project)
    late: list[FleetAgent] = []
    real_kill = fleet_service._kill_fleet_sessions

    def kill_then_late_exit(*args: object, **kwargs: object) -> None:
        real_kill(*args, **kwargs)  # type: ignore[arg-type]
        agent = _coder(project)
        tmux.die(agent.pane_id, 42)
        tmux.refuse_kills = True  # only the late retirement meets the refusal
        late.append(agent)

    monkeypatch.setattr(fleet_service, "_kill_fleet_sessions", kill_then_late_exit)

    report = fleet_service.shutdown(project, force=True)

    assert late[0].pane_id in tmux.facts, "the pane is still there"
    assert tmux.running, "and so is its session"
    assert [s for s in report.sessions_failed if late[0].pane_id in s], report.sessions_failed
    assert report.incomplete_projects == [project.id], "so the project is NOT down"
    assert [a.exit_status for a in report.stopped if a.id == late[0].id] == [42], "status kept"


def test_shutdown_keeps_the_claim_of_a_row_on_a_socket_that_refused_after_the_probe(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same major finding, reached the way an operator would: the socket ANSWERS
    ``_shutdown_probe`` — so the command runs rather than refusing — and refuses
    from then on. Through the lenient reads that was indistinguishable from every
    pane being gone: rows ended, board sessions closed and a running agent's claimed
    task handed back to the next worker, over agents that were all still alive.
    """
    coder = _coder(project)
    _board_session(coder, "working")
    task = _add_task(project, "wire the auth callback")
    assert coder.session_id is not None
    with store_session() as store:
        lease = datetime.now(tz=UTC) + timedelta(hours=1)
        assert store.claim_task(task.id, coder.session_id, lease)
    real_probe = fleet_service._shutdown_probe

    def probe_then_refuse(*args: object, **kwargs: object) -> dict[str, bool]:
        answering = real_probe(*args, **kwargs)  # type: ignore[arg-type]
        tmux.socket_denied = True  # it answered, and only THEN refused
        return answering

    monkeypatch.setattr(fleet_service, "_shutdown_probe", probe_then_refuse)

    report = fleet_service.shutdown(project, force=True)

    assert report.stopped == [] and report.claims_released == []
    assert [row.agent.id for row in report.failed] == [coder.id]
    assert "could not be asked" in report.failed[0].reason
    assert tmux.facts[coder.pane_id].dead is False, "its pane is still running"
    assert tmux.killed == [], "and nothing was killed on a socket that would not answer"
    assert report.incomplete_projects == [project.id], "the project is NOT confirmed down"
    with store_session() as store:
        live = store.fleet_agents(project.id, live_only=True)
        held = store.get_task(task.id)
        session = store.get_session(coder.session_id)
    assert [a.id for a in live] == [coder.id], "the row is LEFT LIVE"
    assert held is not None and held.status == "doing" and held.claimed_by == coder.session_id
    assert session is not None and session.ended_at is None, "its board session is kept too"


# The three branches of `_retire_late_panes`' session reconcile. The round-9 tests
# both take its `has_session_or_raise(...) is False` exit — "the window took the
# session with it" — so everything after it, including the spare rule the docstring
# leans on hardest, was unexecuted by any test (review of #121, round 9
# verification). Each of the three is the same late self-exit with one thing
# different about what its session still holds.


def test_shutdown_leaves_up_a_session_the_late_dead_pane_shared_with_a_live_one(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two rows land after the kill phase in one session; one exits on its own, the
    other is still running. Removing the dead pane must not take the session — that
    would kill an agent tmux had just confirmed alive, the bug rounds 2-3 closed.
    """
    _coder(project)
    late: list[FleetAgent] = []
    real_kill = fleet_service._kill_fleet_sessions

    def kill_then_two_late_rows(*args: object, **kwargs: object) -> None:
        real_kill(*args, **kwargs)  # type: ignore[arg-type]
        exited = _coder(project)  # both came up after the kill phase, same session
        running = _coder(project)
        tmux.die(exited.pane_id, 42)  # …and only one of them exited
        late.extend([exited, running])

    monkeypatch.setattr(fleet_service, "_kill_fleet_sessions", kill_then_two_late_rows)

    report = fleet_service.shutdown(project, force=True)

    exited, running = late
    assert exited.pane_id in tmux.killed, "the retained dead pane is removed"
    assert tmux.facts[running.pane_id].dead is False, "the live late agent survives"
    assert report.sessions_left_up == [_session_of(project)], "its session is SPARED"
    assert report.sessions_killed == [] and tmux.killed_sessions == []
    assert [row.agent.id for row in report.failed] == [running.id], "the live row is reported"
    assert [a.exit_status for a in report.stopped if a.id == exited.id] == [42], "status kept"
    assert report.incomplete_projects == [project.id], "so the project is NOT confirmed down"


def test_shutdown_kills_the_session_a_late_dead_pane_left_holding_only_a_stray_window(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The session outlives the late pane because a window the OPERATOR opened by
    hand is in it (``prefix c``, ``fleet attach``). No row names that window, so
    nothing spares the session on its account: it is the fleet's own session and it
    comes down, rather than being left standing under "✓ fleet shut down".
    """
    _coder(project)
    late: list[FleetAgent] = []
    real_kill = fleet_service._kill_fleet_sessions

    def kill_then_late_exit_beside_a_stray_window(*args: object, **kwargs: object) -> None:
        real_kill(*args, **kwargs)  # type: ignore[arg-type]
        agent = _coder(project)
        tmux.die(agent.pane_id, 42)
        tmux.spawn_window(
            f"asq-{_codename(project)}", name="by-hand", cwd=project.root, command=["sh"]
        )
        late.append(agent)

    monkeypatch.setattr(
        fleet_service, "_kill_fleet_sessions", kill_then_late_exit_beside_a_stray_window
    )

    report = fleet_service.shutdown(project, force=True)

    assert late[0].pane_id in tmux.killed, "the retained dead pane is removed"
    assert report.sessions_killed == [_session_of(project)], "and its session with it"
    assert tmux.killed_sessions == [f"asq-{_codename(project)}"]
    assert tmux.sessions == {} and not tmux.running, "the fleet really is down"
    assert not tmux.server_killed, "the SERVER is still never killed — it exits on its own"
    assert report.sessions_failed == [] and report.sessions_left_up == []
    assert report.incomplete_projects == [], "nothing is left for the report to qualify"


def test_shutdown_reports_the_session_of_a_late_dead_pane_that_tmux_would_not_kill(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other outcome of the same branch: the session survived the late pane, is
    spared by nothing, and tmux refuses to kill it. A refused kill is not a session
    that went down — it is reported, and the project is not confirmed down.
    """
    _coder(project)
    late: list[FleetAgent] = []
    real_kill = fleet_service._kill_fleet_sessions

    def kill_then_late_exit_beside_a_stray_window(*args: object, **kwargs: object) -> None:
        real_kill(*args, **kwargs)  # type: ignore[arg-type]
        agent = _coder(project)
        tmux.die(agent.pane_id, 42)
        tmux.spawn_window(
            f"asq-{_codename(project)}", name="by-hand", cwd=project.root, command=["sh"]
        )
        late.append(agent)
        real_kill_window = tmux.kill_window

        def kill_window_then_refuse_the_rest(pane_id: str) -> None:
            real_kill_window(pane_id)
            tmux.refuse_kills = True  # the pane went; the session will not

        monkeypatch.setattr(tmux, "kill_window", kill_window_then_refuse_the_rest)

    monkeypatch.setattr(
        fleet_service, "_kill_fleet_sessions", kill_then_late_exit_beside_a_stray_window
    )

    report = fleet_service.shutdown(project, force=True)

    assert late[0].pane_id in tmux.killed, "the dead pane was removed before the refusal"
    assert report.sessions_failed == [_session_of(project)], "the session is reported, not killed"
    assert report.sessions_killed == [] and tmux.killed_sessions == []
    assert f"asq-{_codename(project)}" in tmux.sessions, "and it really is still standing"
    assert [a.exit_status for a in report.stopped if a.id == late[0].id] == [42], "status kept"
    assert report.incomplete_projects == [project.id], "so the project is NOT confirmed down"


def test_shutdown_reports_a_session_it_could_not_recheck_after_a_late_pane_exited(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The socket refuses between removing the late pane and re-reading its session.
    An answer that never came is not "the session is gone": it is reported, and the
    project stays not confirmed down, rather than the run claiming a clean finish.
    """
    _coder(project)
    late: list[FleetAgent] = []
    real_kill = fleet_service._kill_fleet_sessions

    def kill_then_late_exit(*args: object, **kwargs: object) -> None:
        real_kill(*args, **kwargs)  # type: ignore[arg-type]
        agent = _coder(project)
        tmux.die(agent.pane_id, 42)
        late.append(agent)
        real_kill_window = tmux.kill_window

        def kill_window_then_deny(pane_id: str) -> None:
            real_kill_window(pane_id)
            tmux.socket_denied = True  # still THERE; it just stops opening for us

        # Set here, not before the run: the stop phase's own kills must land
        # normally, so the refusal meets the late retirement and nothing else.
        monkeypatch.setattr(tmux, "kill_window", kill_window_then_deny)

    monkeypatch.setattr(fleet_service, "_kill_fleet_sessions", kill_then_late_exit)

    report = fleet_service.shutdown(project, force=True)

    assert late[0].pane_id in tmux.killed, "the pane was removed before the socket refused"
    assert [s for s in report.sessions_failed if "could not be re-checked" in s], (
        report.sessions_failed
    )
    assert report.sessions_killed == [] and report.sessions_left_up == []
    assert [a.exit_status for a in report.stopped if a.id == late[0].id] == [42], "status kept"
    assert report.incomplete_projects == [project.id], "an unverified session is not down"


def test_shutdown_reports_a_pause_signal_it_could_not_read_or_clear(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #121, round 9 (P2): a blanket ``suppress(Exception)`` around the
    per-project pause read and write left the signal ON with ``paused_kept=[]``,
    ``pause_scan_failed=null`` and exit 0 — the next manager still under orders to
    spawn nothing, and nothing in the report saying so. Both failures are surfaced
    the way the visible-projects lookup already is, and the report is preserved.
    """
    real_read = fleet_service._pause_is_on
    real_clear = fleet_service._clear_pause_signal

    def refuse(*args: object, **kwargs: object) -> bool:
        raise sqlite3.OperationalError("database is locked (fake)")

    _coder(project)
    fleet_service.pause(project)
    name = fleet_service._name(project)

    monkeypatch.setattr(fleet_service, "_pause_is_on", refuse)  # the READ fails
    report = fleet_service.shutdown(project, force=True)
    monkeypatch.setattr(fleet_service, "_pause_is_on", real_read)

    assert len(report.stopped) == 1, "the report is preserved, not discarded"
    assert report.pause_scan_failed is not None and "locked" in report.pause_scan_failed
    assert report.pause_scan_failed.startswith(f"{name}: could not be read")
    assert report.paused_cleared == [], "nothing is cleared on a signal that could not be read"
    assert report.paused_kept == [], "and nothing is CLAIMED kept either — it was never read"
    assert project.id in report.incomplete_projects, "an unread pause is not a project down"
    assert fleet_service.is_paused(project), "the pause is in fact still on"

    monkeypatch.setattr(fleet_service, "_clear_pause_signal", refuse)  # now the WRITE fails
    second = fleet_service.shutdown(project, force=True)
    monkeypatch.setattr(fleet_service, "_clear_pause_signal", real_clear)

    assert second.pause_scan_failed is not None and "locked" in second.pause_scan_failed
    assert "could not be cleared" in second.pause_scan_failed, "the WRITE is the one that failed"
    assert second.paused_kept == [name], "read ON, not cleared: kept"
    assert second.paused_cleared == []
    assert project.id in second.incomplete_projects
    assert fleet_service.is_paused(project), "the signal that could not be written stays on"
    # Negative control: with the store answering, the same run clears it and exits clean.
    third = fleet_service.shutdown(project, force=True)
    assert third.pause_scan_failed is None and third.paused_cleared == [name]
    assert third.incomplete_projects == [] and not fleet_service.is_paused(project)


def test_shutdown_returns_the_report_when_the_pause_lookup_store_is_locked(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #121, round 8 (P2): after the final scan, ``_clear_pause`` opened
    the store UNGUARDED to list visible projects; a store still locked raised
    straight through the finished report — empty stdout under ``--json`` after
    agents were already stopped. The lookup is guarded now: the partial report is
    returned and every pause is kept."""
    coder = _coder(project)
    fleet_service.pause(project)
    boom = {"on": False}
    real_store_session = store_session  # the module-level import is the same object
    real_late = fleet_service._record_late_rows

    @contextmanager
    def maybe_locked():  # type: ignore[no-untyped-def]
        if boom["on"]:
            raise sqlite3.OperationalError("database is locked (fake)")
        with real_store_session() as store:
            yield store

    def late_then_lock(*args: object, **kwargs: object) -> None:
        real_late(*args, **kwargs)  # type: ignore[arg-type]
        boom["on"] = True

    monkeypatch.setattr(fleet_service, "_record_late_rows", late_then_lock)
    monkeypatch.setattr(fleet_service, "store_session", maybe_locked)

    report = fleet_service.shutdown(project, force=True)  # must NOT raise
    boom["on"] = False

    assert coder.id in {a.id for a in report.stopped}
    assert report.paused_cleared == [], "no pause is cleared blind"
    assert fleet_service.is_paused(project), "every pause is kept"
    assert report.pause_scan_failed is not None and "locked" in report.pause_scan_failed
    assert report.late_scan_failed is None, "the scan itself ran; only the pause lookup failed"


def test_shutdown_clears_the_pause_by_the_target_id_not_a_redirected_board(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #121, round 8 (P2): with ``AISQUARE_TEAM_HUB`` pointing at project
    A, shutting down explicit project B resolved its pause through cwd and cleared
    A's signal instead — B reported in ``paused_cleared`` while A's agent kept its
    standing order removed. The pause is read and written by the target project's
    ID, so the hub cannot redirect it."""
    hub_root = project.root.parent / "hub-project"
    hub_root.mkdir()
    hub = team_project(hub_root)
    with store_session() as store:
        store.ensure_project(hub)
    fleet_service.pause(hub)
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(hub_root))  # cwd resolution now points at A

    _coder(project)
    fleet_service.pause(project)
    assert fleet_service.is_paused(project) and fleet_service.is_paused(hub)

    report = fleet_service.shutdown(project, force=True)

    assert report.paused_cleared == [project.root.name], "B, not the hub"
    assert not fleet_service.is_paused(project), "B's own signal is cleared"
    assert fleet_service.is_paused(hub), "the hub's pause is untouched"


def test_shutdown_with_no_agents_takes_the_fleets_session_down_and_reports_nothing_else(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    report = fleet_service.shutdown(project)
    assert report.stopped == [] and report.recorded == [] and report.failed == []
    assert report.sessions_killed == [] and report.sessions_absent == []
    assert not tmux.server_killed


def test_shutdown_plan_reads_without_touching_anything(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """What the CLI prints before it asks. ``fleet shutdown`` ends running work, so it
    is confirmable the way ``project prune`` is for stale registrations."""
    coder = _coder(project)

    plan = fleet_service.shutdown_plan(project)

    assert [a.id for a in plan.agents] == [coder.id]
    assert [p.id for p in plan.projects] == [project.id]
    assert plan.sessions == [_session_of(project)] and plan.absent_sockets == []
    assert tmux.killed == [] and tmux.killed_sessions == []
    assert [s.agent.id for s in fleet_service.list_agents(project)] == [coder.id]

    tmux.running = False  # nothing to stop on a socket with no server: recorded, not stopped
    absent = fleet_service.shutdown_plan(project)
    assert absent.absent_sockets == [coder.tmux_socket] and absent.sessions == []


def test_shutdown_plan_refuses_rather_than_showing_a_plan_short_of_a_session(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #121, round 9 (P2): ``suppress(TmuxError)`` plus the LENIENT
    ``has_session`` dropped from the plan exactly the sessions tmux could not be
    asked about — the operator confirmed one session and the run then killed two,
    and a scope holding only the inaccessible session read "nothing to shut down".
    A query that fails after a good probe is a refusal, never a short plan.
    """
    coder = _coder(project)
    real_probe = fleet_service._shutdown_probe

    def probe_then_deny(*args: object, **kwargs: object) -> dict[str, bool]:
        answering = real_probe(*args, **kwargs)  # type: ignore[arg-type]
        tmux.socket_denied = True  # the socket stops answering right after the probe
        return answering

    monkeypatch.setattr(fleet_service, "_shutdown_probe", probe_then_deny)

    with pytest.raises(FleetError, match="could not list the fleet's sessions"):
        fleet_service.shutdown_plan(project)

    tmux.socket_denied = False
    monkeypatch.setattr(fleet_service, "_shutdown_probe", real_probe)
    assert tmux.killed == [] and tmux.killed_sessions == [], "a plan touches nothing"
    assert [s.agent.id for s in fleet_service.list_agents(project)] == [coder.id]
    # Negative control: with the socket answering, the same plan names the session.
    assert fleet_service.shutdown_plan(project).sessions == [_session_of(project)]


def test_reap_ends_dead_panes_and_tells_the_board(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    manager = fleet_service.spawn(project, "manager").agent
    coder = _coder(project)
    tmux.die(coder.pane_id, 2)

    report = fleet_service.reap(project)

    assert [a.id for a in report.ended] == [coder.id] and report.ended[0].exit_status == 2
    assert report.lost == [] and report.worktrees_removed == []
    with store_session() as store:
        events = [e for e in store.recent_events(project.id) if e.kind == "agent_exited"]
    assert len(events) == 1
    assert events[0].text == "coder-1 exited (2)" and events[0].session_id == coder.session_id
    assert [s.agent.id for s in fleet_service.list_agents(project)] == [manager.id]
    # Idempotent: a second pass finds nothing new and emits nothing more.
    assert fleet_service.reap(project).ended == []
    assert len(_events(project, "agent_exited")) == 1


def test_reap_marks_vanished_panes_lost_without_an_exit_event(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    coder = _coder(project)
    tmux.vanish(coder.pane_id)
    report = fleet_service.reap(project)
    assert [a.id for a in report.lost] == [coder.id] and report.ended == []
    assert report.lost[0].ended_at is not None and report.lost[0].exit_status is None
    assert _events(project, "agent_exited") == []


def test_reap_leaves_live_agents_alone(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    coder = _coder(project)
    report = fleet_service.reap(project)
    assert report.ended == [] and report.lost == []
    assert [s.agent.id for s in fleet_service.list_agents(project)] == [coder.id]


def test_reap_nudges_a_waiting_manager_when_an_agent_exits(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    manager = fleet_service.spawn(project, "manager").agent
    _board_session(manager, "waiting")
    tmux.set_command(manager.pane_id, "claude")
    coder = _coder(project)
    tmux.die(coder.pane_id, 1)
    fleet_service.reap(project)
    assert tmux.typed == [
        (manager.pane_id, "literal", NUDGE_TEXT),
        (manager.pane_id, "key", "Enter"),
    ]


def test_reap_does_nothing_when_tmux_cannot_be_asked(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    coder = _coder(project)
    tmux.installed = False
    report = fleet_service.reap(project)
    assert report.ended == [] and report.lost == []
    tmux.installed = True
    assert [s.agent.id for s in fleet_service.list_agents(project)] == [coder.id]


def test_the_lifecycle_addresses_the_socket_each_row_was_started_on(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every row records its ``tmux_socket``. An operator who edits ``[fleet]
    tmux_socket`` must not thereby lose the agents still running on the old server: the
    new socket answers nothing about them, which reads exactly like panes that are
    gone and would end rows for processes that are still running."""
    _settings(monkeypatch, tmux_socket="asq-first")
    live = _coder(project)
    dead = _coder(project)
    tmux.die(dead.pane_id, 3)
    assert live.tmux_socket == "asq-first" and dead.tmux_socket == "asq-first"

    moved = FakeTmux()  # the server the config now names: it holds none of these panes
    _settings(monkeypatch, tmux_socket="asq-second")

    def per_socket(config: FleetSettings | None = None) -> FakeTmux:
        return tmux if config is not None and config.tmux_socket == "asq-first" else moved

    monkeypatch.setattr(fleet_service, "server", per_socket)

    listed = {status.agent.id: status.state for status in fleet_service.list_agents(project)}
    assert listed == {live.id: "waiting", dead.id: "exited"}, listed
    report = fleet_service.reap(project)
    assert [a.id for a in report.ended] == [dead.id], "the dead pane was seen on its own socket"
    assert report.lost == [], "a live agent must not be lost because the config moved"

    ended = fleet_service.stop(project, live.label).agent
    assert tmux.killed == [live.pane_id] and moved.killed == []
    assert ended.exit_status == 0, "the /exit reached the pane on the row's socket"


def test_reap_over_every_project(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, plain_project: ProjectInfo
) -> None:
    here = _coder(project)
    there = fleet_service.spawn(plain_project, "coder", worktree=False).agent
    tmux.die(here.pane_id, 0)
    tmux.die(there.pane_id, 1)
    report = fleet_service.reap()
    assert {a.id for a in report.ended} == {here.id, there.id}


def test_reap_removes_a_merged_worktree_and_keeps_an_unmerged_one(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    merged = fleet_service.spawn(project, "coder", label="coder-merged").agent
    unmerged = fleet_service.spawn(project, "coder", label="coder-open").agent
    (unmerged.cwd / "work.txt").write_text("wip\n", encoding="utf-8")
    _git("add", "work.txt", cwd=unmerged.cwd)
    _git("commit", "-q", "-m", "wip", cwd=unmerged.cwd)
    fleet_service.stop(project, "coder-merged", force=True)
    fleet_service.stop(project, "coder-open", force=True)

    report = fleet_service.reap(project)

    assert report.worktrees_removed == [merged.cwd]
    assert not merged.cwd.exists() and unmerged.cwd.exists()
    assert fleet_service.reap(project).worktrees_removed == []


def test_reap_never_removes_a_live_agents_worktree(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    first = fleet_service.spawn(project, "coder", label="coder-auth").agent
    fleet_service.stop(project, "coder-auth", force=True)
    second = fleet_service.spawn(project, "coder", label="coder-auth")
    assert second.agent.cwd == first.cwd, "a respawn on the same label reuses the tree"
    assert any("reusing the existing worktree" in note for note in second.notes)
    report = fleet_service.reap(project)
    assert report.worktrees_removed == [] and first.cwd.exists()


def test_reap_leaves_worktrees_alone_when_they_are_still_live(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    live = fleet_service.spawn(project, "coder").agent
    assert fleet_service.reap(project).worktrees_removed == []
    assert live.cwd.exists()


# --- nudge (§7.3) ------------------------------------------------------------------------


def _waiting_manager(tmux: FakeTmux, project: ProjectInfo) -> FleetAgent:
    manager = fleet_service.spawn(project, "manager").agent
    _board_session(manager, "waiting")
    tmux.set_command(manager.pane_id, "claude")
    return manager


def test_nudge_types_one_fixed_line_into_a_waiting_manager(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    manager = _waiting_manager(tmux, project)
    assert fleet_service.nudge_manager(project.id, reason="task_review") is True
    assert tmux.typed == [
        (manager.pane_id, "literal", NUDGE_TEXT),
        (manager.pane_id, "key", "Enter"),
    ]
    assert not NUDGE_TEXT.startswith(("/", "!")), "Claude Code's command prefixes"
    assert "task_review" not in NUDGE_TEXT, "the nudge carries nothing; the delta does"


def test_nudge_is_debounced_through_team_meta(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    manager = _waiting_manager(tmux, project)
    assert fleet_service.nudge_manager(project.id, reason="a") is True
    assert fleet_service.nudge_manager(project.id, reason="b") is False
    assert len(tmux.typed) == 2, "one nudge, not two"
    stale = (datetime.now(tz=UTC) - fleet_service.NUDGE_DEBOUNCE - timedelta(seconds=1)).isoformat()
    with store_session() as store:
        store.set_meta(f"nudge:{manager.session_id}", stale)
    assert fleet_service.nudge_manager(project.id, reason="c") is True
    assert len(tmux.typed) == 4


@pytest.mark.parametrize("state", ["attention", "working"])
def test_nudge_never_touches_a_manager_that_is_not_waiting(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, state: str
) -> None:
    manager = fleet_service.spawn(project, "manager").agent
    _board_session(manager, state)
    tmux.set_command(manager.pane_id, "claude")
    assert fleet_service.nudge_manager(project.id, reason="x") is False
    assert tmux.typed == []


def test_nudge_refuses_a_stale_waiting_row_that_ls_and_tell_call_working(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """The nudge must not type where ``tell`` refuses to. Every other consumer goes
    through ``_derive``, which trusts a board row's ``state`` only while the row is
    FRESH (``_STALE_AFTER``) and otherwise lets pane activity decide; ``nudge_manager``
    read the field raw. So a row frozen at "waiting" over a pane that keeps printing
    was ``working`` in ``fleet ls``, refused by ``fleet tell`` — and typed into by the
    nudge, which is the interleaving ``tell``'s rule exists to prevent.
    """
    manager = fleet_service.spawn(project, "manager").agent
    tmux.set_command(manager.pane_id, "claude")
    _stale_board_session(
        manager, "waiting", seen_ago=team_service._STALE_AFTER + timedelta(minutes=1)
    )
    tmux.printed(manager.pane_id)  # the pane printed just now: mid-turn

    assert fleet_service.status_of(manager).state == "working", "what ls and tell see"
    assert fleet_service.nudge_manager(project.id, reason="task_review") is False
    assert tmux.typed == []
    # Negative control: the same stale row over a QUIET pane is waiting everywhere,
    # and the nudge goes in — the freshness term must not refuse everything.
    tmux.printed(manager.pane_id, ago=ACTIVITY_WINDOW + timedelta(seconds=2))
    assert fleet_service.status_of(manager).state == "waiting"
    assert fleet_service.nudge_manager(project.id, reason="task_review") is True
    assert tmux.typed == [
        (manager.pane_id, "literal", NUDGE_TEXT),
        (manager.pane_id, "key", "Enter"),
    ]


@pytest.mark.parametrize("command", ["bash", PYTHON_LAUNCHER, "zsh"])
def test_nudge_needs_the_agent_in_the_foreground(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, command: str
) -> None:
    manager = _waiting_manager(tmux, project)
    tmux.set_command(manager.pane_id, command)
    assert fleet_service.nudge_manager(project.id, reason="x") is False
    assert tmux.typed == []


def test_nudge_needs_a_live_pane(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    manager = _waiting_manager(tmux, project)
    tmux.die(manager.pane_id, 0)
    assert fleet_service.nudge_manager(project.id, reason="x") is False
    tmux.vanish(manager.pane_id)
    assert fleet_service.nudge_manager(project.id, reason="x") is False
    assert tmux.typed == []


def test_nudge_without_a_manager_or_its_board_row_is_false(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    assert fleet_service.nudge_manager(project.id, reason="x") is False
    fleet_service.spawn(project, "manager")  # no hooks have fired yet: no team_session row
    assert fleet_service.nudge_manager(project.id, reason="x") is False
    assert tmux.typed == []


def test_nudge_never_raises(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    _waiting_manager(tmux, project)
    tmux.fail_input = True
    assert fleet_service.nudge_manager(project.id, reason="x") is False
    tmux.fail_input = False
    tmux.installed = False
    assert fleet_service.nudge_manager(project.id, reason="x") is False

    def broken() -> Iterator[None]:
        raise RuntimeError("context.db is toast")

    monkeypatch.setattr(fleet_service, "store_session", broken)
    assert fleet_service.nudge_manager(project.id, reason="x") is False


# --- pause / resume ------------------------------------------------------------------------


def test_pause_and_resume_flip_the_board_signal(project: ProjectInfo) -> None:
    assert not fleet_service.is_paused(project)
    fleet_service.pause(project)
    assert fleet_service.is_paused(project)
    assert _events(project, "signal") == ["fleet-paused: on"]
    fleet_service.resume(project)
    assert not fleet_service.is_paused(project)
    assert _events(project, "signal")[-1] == "fleet-paused: off (was on)"


def test_pause_with_the_team_disabled_is_a_fleet_error(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_TEAM", "0")
    with pytest.raises(FleetError, match="pause signal"):
        fleet_service.pause(project)
    assert not fleet_service.is_paused(project)


# --- rename --------------------------------------------------------------------------


def test_rename_sets_the_codename_and_renames_the_tmux_session(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    fleet_service.spawn(project, "manager")
    old = _codename(project)
    updated = fleet_service.rename(project, "amber-otter")
    assert updated.codename == "amber-otter" and _codename(project) == "amber-otter"
    assert tmux.renamed == [(f"asq-{old}", "asq-amber-otter")]
    [status] = fleet_service.list_agents(project)
    assert status.tmux_session == "asq-amber-otter" and status.state == "waiting"


def test_rename_says_what_a_swallowed_tmux_rename_cost(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row is renamed even when tmux refuses — ``reap`` addresses panes by id — but
    that fail-open costs the plan's full-fidelity escape hatch, permanently: the
    session keeps the OLD name, ``fleet attach`` looks for the new one and refuses
    with advice that cannot work, and a second ``rename`` cannot repair it (it drives
    from the stored codename, which is already the new one). So it says so.
    """
    fleet_service.spawn(project, "manager")
    first = _codename(project)
    accepted: list[str] = []

    # Negative control first: a rename tmux accepts reports no cost at all.
    assert fleet_service.rename(project, "amber-otter", notes=accepted).codename == "amber-otter"
    assert accepted == [] and tmux.renamed == [(f"asq-{first}", "asq-amber-otter")]

    def refusing(old: str, new: str) -> None:
        raise TmuxError("duplicate session: asq-quiet-lynx")

    monkeypatch.setattr(tmux, "rename_session", refusing)
    notes: list[str] = []
    updated = fleet_service.rename(project, "quiet-lynx", notes=notes)

    assert updated.codename == "quiet-lynx" and _codename(project) == "quiet-lynx"
    said = " ".join(notes)
    assert said, "the failure was swallowed"
    assert "asq-amber-otter" in said and "asq-quiet-lynx" in said, said
    assert "rename-session" in said and "attach" in said, said
    # Not decoration: the escape hatch really is refused now, exactly as the note says.
    with pytest.raises(FleetError, match="nothing to attach to"):
        fleet_service.attach_argv(project)
    # And the note channel is optional — the CLI's plain call must not blow up.
    assert fleet_service.rename(project, "ruby-fox").codename == "ruby-fox"


@pytest.mark.parametrize("bad", ["Amber-Otter", "amber_otter", "amber-otter-x"])
def test_rename_rejects_a_bad_shape(tmux: FakeTmux, project: ProjectInfo, bad: str) -> None:
    before = fleet_service.ensure_codename(project).codename
    with pytest.raises(FleetError, match="adjective-animal"):
        fleet_service.rename(project, bad)
    assert _codename(project) == before and tmux.renamed == []


def test_rename_refuses_a_codename_another_project_holds(
    tmux: FakeTmux, project: ProjectInfo, plain_project: ProjectInfo
) -> None:
    fleet_service.rename(plain_project, "amber-otter")
    with pytest.raises(FleetError, match="already taken"):
        fleet_service.rename(project, "amber-otter")
    # Negative control: a free codename is accepted by the same call.
    assert fleet_service.rename(project, "ruby-fox").codename == "ruby-fox"


def test_rename_without_tmux_still_renames_the_row(tmux: FakeTmux, project: ProjectInfo) -> None:
    fleet_service.ensure_codename(project)
    tmux.installed = False
    assert fleet_service.rename(project, "quiet-lynx").codename == "quiet-lynx"
    assert tmux.renamed == []


def test_rename_with_no_session_yet_touches_nothing_in_tmux(
    tmux: FakeTmux, project: ProjectInfo
) -> None:
    fleet_service.ensure_codename(project)
    assert fleet_service.rename(project, "quiet-lynx").codename == "quiet-lynx"
    assert tmux.renamed == [] and tmux.sessions == {}
    # Renaming to the current name is a no-op, not a clash with itself.
    assert fleet_service.rename(project, "quiet-lynx").codename == "quiet-lynx"


# --- attach --------------------------------------------------------------------------


def test_attach_argv_targets_the_project_session_exactly(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    fleet_service.spawn(project, "manager")
    argv = fleet_service.attach_argv(project)
    assert argv[-3:] == ["attach-session", "-t", f"=asq-{_codename(project)}"]


def test_attach_argv_refuses_when_there_is_no_session_to_attach_to(
    tmux: FakeTmux, project: ProjectInfo
) -> None:
    """The CLI execs this argv, so a session that does not exist is refused here,
    with a message — not handed to tmux to fail on (and not exec'd by a test sweep)."""
    with pytest.raises(FleetError, match="nothing to attach to"):
        fleet_service.attach_argv(project)
    assert tmux.sessions == {}


def test_attach_argv_without_tmux_is_fleet_unavailable(
    tmux: FakeTmux, project: ProjectInfo
) -> None:
    tmux.installed = False
    with pytest.raises(FleetUnavailable):
        fleet_service.attach_argv(project)


# --- the real thing, once ------------------------------------------------------------


_WINDOW_PROBE = (
    "#{window_id} #{window_name} #{pane_id} dead=#{pane_dead} status=#{pane_dead_status}"
)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
def test_spawn_and_stop_on_a_real_tmux_server(
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """End to end on a private socket: the command we build is one ``aisquare launch``
    accepts, the fake agent receives our flags, and ``/exit`` + Enter reaches it.

    Readiness is read off the SCREEN here, not off ``pane_current_command``: the
    fake agent is a ``#!/bin/sh`` script, which tmux reports as ``sh`` — the one
    shape the heuristic deliberately never trusts.
    """
    monkeypatch.setattr(fleet_service, "_sleep", time.sleep)
    monkeypatch.setattr(fleet_service, "_monotonic", time.monotonic)
    monkeypatch.chdir(tmp_path)  # the -vv server logs land in the server's cwd

    class LoggedServer(TmuxServer):
        """The real server, with tmux's own logging on — the CI autopsy channel."""

        def argv(self, *args: str) -> list[str]:
            base = super().argv(*args)
            return [base[0], "-vv", *base[1:]]

    def _autopsy(server: TmuxServer, session: str) -> str:
        """Everything tmux can still tell us, for an assert message on a runner."""
        parts: list[str] = []
        for label, args in (
            ("sessions", ("list-sessions", "-F", "#{session_name}")),
            ("windows", ("list-panes", "-s", "-t", f"={session}", "-F", _WINDOW_PROBE)),
        ):
            try:
                parts.append(f"{label}: {server.run(*args)!r}")
            except TmuxError as exc:
                parts.append(f"{label}: TmuxError({exc})")
        interesting = re.compile(
            r"destroy|kill|exited|dead|signal|lost|session_|window_|spawn|got \d+|loop exit"
        )
        for log in sorted(tmp_path.glob("tmux-*.log")):
            lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
            events = [line for line in lines if interesting.search(line)]
            parts.append(
                f"--- {log.name}: {len(lines)} lines; the events ---\n"
                + "\n".join(events[-160:])
                + "\n--- raw tail ---\n"
                + "\n".join(lines[-40:])
            )
        return "\n".join(parts)

    # A socket of our OWN: another file's test on a shared name can be mid
    # kill-server when this new-session arrives, which lands the session on the
    # dying server — it then vanishes with it (seen on CI as an empty window
    # list). Every real-tmux test in this suite now suffixes its socket.
    real = LoggedServer(f"asq-test-{os.getpid()}-fleet")
    monkeypatch.setattr(fleet_service, "server", lambda config=None: real)
    try:
        receipt = fleet_service.spawn(project, "coder", worktree=False)
        pane = receipt.agent.pane_id
        assert pane.startswith("%")
        deadline = time.monotonic() + 60
        screen = ""
        while time.monotonic() < deadline:
            facts = real.pane_facts(pane)
            assert facts is not None, "the pane vanished"
            screen = "\n".join(real.capture(pane).lines)
            assert not facts.dead, screen
            if "fake claude:" in screen:
                break
            time.sleep(0.2)
        assert "fake claude:" in screen, screen
        # The control behind the "gone" hunts: this server must keep dead panes.
        assert real.run("show-options", "-gv", "remain-on-exit").strip() == "on"
        assert f"--session-id {receipt.agent.session_id}" in screen, screen
        assert "--name coder-1" in screen and "--permission-mode auto" in screen, screen
        # ``#{window_activity}`` must be the time of the last OUTPUT, not the
        # window's creation time: it is the whole working/waiting distinction for
        # an agent with no hooks (ACTIVITY_WINDOW), and a creation-only stamp —
        # which is exactly what ``window_activity_flag`` does, see test_tmux.py's
        # live flag test — would leave every such agent reading "waiting" forever
        # while every assertion stayed green. Both directions, on a real server.
        first = fleet_service._activity_times(real)
        assert pane in first, first
        assert datetime.now(tz=UTC) - first[pane] < timedelta(minutes=1)
        time.sleep(1.5)  # more than one whole second, the field's resolution
        quiet = fleet_service._activity_times(real)
        assert quiet[pane] == first[pane], f"advanced while the pane was quiet: {quiet} vs {first}"
        # The pty echoes what is typed, so this is output from the pane — and the
        # fake agent's `read` is still waiting, so it does not end it.
        real.send_literal(pane, "x")
        deadline = time.monotonic() + 30
        after = quiet
        while time.monotonic() < deadline:
            after = fleet_service._activity_times(real)
            if after[pane] > quiet[pane]:
                break
            time.sleep(0.2)
        assert after[pane] > quiet[pane], f"output did not advance it: {after} vs {quiet}"
        [status] = fleet_service.list_agents(project)
        assert status.state == "working", status  # output inside ACTIVITY_WINDOW

        ended = fleet_service.stop(project, "coder-1", grace=15.0).agent

        if ended.exit_status != 0:
            # print(), not the assert message: pytest truncates long reprs, and
            # the whole point is the server log — captured stdout survives whole.
            print(f"ENDED ROW: {ended!r}")
            print(_autopsy(real, receipt.tmux_session))
        # None is tmux 3.4 being tmux 3.4: it sometimes NEVER exposes a dead
        # pane's exit status (`pane_dead_status` empty on every poll — its own
        # -vv log, ~1 death in 30 under one CPU). The row must still end, and
        # 0 is required wherever the server does expose it.
        assert ended.exit_status in (0, None)
        assert ended.ended_at is not None
        assert real.list_windows(receipt.tmux_session) == []
    finally:
        with suppress(TmuxError):
            real.run("kill-server")
