"""The fleet lifecycle over a FAKE tmux (plan §5.3, §5.7, §7.3; §3.4 to §3.6).

Every test here drives ``services.fleet`` against the real store in an isolated
home and an in-memory :class:`FakeTmux` — no ``claude``, no ``gh``, and no tmux
server except in the one end-to-end test at the bottom, which runs on a private
socket, is skipped when tmux is absent, and kills its server afterwards.

The fake's runner refuses every call, so a method the service uses that the
fake forgot to override fails loudly instead of quietly reaching a real tmux.
"""

from __future__ import annotations

import ast
import errno
import json
import logging
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import claude_accounts as accounts_core
from aisquare.core import claude_sharing, codenames, harness, locking, paths, selfcli
from aisquare.core import store as core_store
from aisquare.core import tmux as tmux_core
from aisquare.core.config import (
    REVIEWER_ARGS,
    FleetRoleSettings,
    FleetSettings,
    replacing_the_former_reviewer_default,
    replacing_the_former_reviewer_default_first,
)
from aisquare.core.ids import new_agent_id, new_task_id
from aisquare.core.orchestrator import FLEET_AGENT_ENV_VAR, team_project
from aisquare.core.store import ContextStore, SqliteStore, store_session
from aisquare.core.tmux import (
    _FACTS_FIELDS,
    Capture,
    Completed,
    PaneFacts,
    Runner,
    SocketAnswer,
    TmuxError,
    TmuxPartlyTyped,
    TmuxServer,
    TmuxTimedOut,
    TmuxUnavailable,
    WindowInfo,
)
from aisquare.core.version import __version__
from aisquare.core.workspace import project_id_for
from aisquare.models import (
    CheckStatus,
    FleetAgent,
    FleetAgentStatus,
    LaunchSpec,
    ProjectInfo,
    TeamEvent,
    TeamSession,
    TeamTask,
)
from aisquare.services import auto_mode, delivery, install_route, lifecycle
from aisquare.services import claude_accounts as accounts_service
from aisquare.services import fleet as fleet_service
from aisquare.services import project as project_service
from aisquare.services import team as team_service
from aisquare.services.captain import screen as screen_reader
from aisquare.services.explainability import PIPELINE_ID_ENV_VAR
from aisquare.services.fleet import (
    ACTIVITY_WINDOW,
    NUDGE_TEXT,
    PROMPT_TIMEOUT,
    TRUST_PROMPT,
    FleetError,
    FleetUnavailable,
    NoSuchAgent,
)
from tests import bundled_personas, fakebin
from tests.captain_screens import INPUT_BOX, MARK
from tests.cycles import unscoped
from tests.fsperms import can_deny_reads, can_symlink, remove_tree
from tests.projects import add_project

PYTHON_LAUNCHER = "python3"
"""What ``pane_current_command`` reads while ``python -m aisquare launch`` is resolving."""


# --- the fake tmux ---------------------------------------------------------------------


def _is_paused(project: ProjectInfo) -> bool:
    """Whether ``project``'s pause reads on, on any board, found by its name as ``fleet
    resume`` and ``fleet shutdown`` find it (``fleet._pause_where_on``); a blob that does not
    parse reads as none (``team.board_signals_in``). The tests' own: nothing else asks it
    (review of #259, round 37, 7/7)."""
    with store_session() as store:
        return bool(fleet_service._pause_where_on(project, team_service.board_signals_in(store)))


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
        self.socket_file = True
        """Whether the socket FILE is where the server made it. tmux leaves it when its
        server exits, so a dead server's file is stale and shows no server; False is the
        file taken from under the server (systemd-tmpfiles, logind), which shows nothing
        (`listener`, the rule `uninstall` asks too)."""
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
        self.per_path: dict[Path, TmuxServer] = {}
        """Servers asked for at a path (``tmux -S``, :meth:`at`) that is not where ``-L``
        reaches this one: a server started under another ``TMUX_TMPDIR``."""
        self.sessions: dict[str, list[WindowInfo]] = {}
        self.facts: dict[str, PaneFacts] = {}
        self.output_at: dict[str, datetime] = {}
        """When each pane last printed — what ``#{window_activity}`` reports."""
        self.pids: dict[str, int] = {}
        """The pid tmux started in each pane — what ``pane_pid`` answers. Unset
        means tmux cannot say, which the identity check reads as "cannot tell"."""
        self.started: datetime | None = None
        """When the server started — what ``#{start_time}`` answers. The server the
        fake begins with never says, so ids alone decide as they did before the
        question was asked; a spawn that brings a server up on a socket with none
        (a reboot, a hand-run `kill-server`) sets it, as the real one's
        `new-session` starts a new server. Kept to the microsecond where tmux
        rounds down to the second, so a test never waits out a second boundary."""
        self.spawned: list[dict[str, object]] = []
        self.refuse_resize: str | None = None
        """What tmux says when it refuses the resize ``spawn_window`` makes after
        ``new-window`` — the real one returns the window anyway, carrying these
        words in ``WindowInfo.resize_refused`` (#149; review of #162, round 1)."""
        self.typed: list[tuple[str, str, str]] = []
        """``(pane_id, kind, text)`` with kind ``literal`` / ``paste`` / ``key``."""
        self.screens: dict[str, list[str]] = {}
        """What each pane shows — what ``capture`` answers; blank until a test draws one."""
        self.captured: list[str] = []
        """Every pane whose screen was asked for, in order."""
        self.draws_box = False
        """Opt in (:func:`typed_first_lines`): a pane running the agent draws Claude Code's idle
        input box where a test drew nothing, a paste lands in that box, and an Enter sends
        it, which the window's ``UserPromptSubmit`` reports (``store.mark_prompted``) — what
        a typed first message meets in a real Claude Code (P0-W1a)."""
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

    def server_absent(self) -> bool:
        """Faithful to :meth:`aisquare.core.tmux.TmuxServer.server_absent`: True only on
        tmux's OWN word that no server is behind the socket (``running = False`` is the
        shape ``kill-server`` and a swept ``/tmp`` leave); a question that could not be
        put — no client, an unrunnable one, a denied socket, a wedged server — is no
        evidence of absence and answers False, as the real one does."""
        if not self.installed or self.exec_unavailable or self.answers_raises is not None:
            return False
        if self.socket_denied:
            return False
        return not self.running

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
        width: int = tmux_core.DEFAULT_WINDOW_WIDTH,
        height: int = tmux_core.DEFAULT_WINDOW_HEIGHT,
    ) -> WindowInfo:
        self.binary()
        # The real one runs `new-session -d`, which STARTS a server when none is
        # listening and exits 0 — so "the next asq / fleet spawn starts a fresh
        # server", stated in the command's docstring and in docs/fleet.md, is
        # exercisable rather than merely claimed.
        if not self.running:
            self.started = datetime.now(tz=UTC)
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
            resize_refused=self.refuse_resize,
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
                "size": (width, height),
                "pane_id": pane_id,
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
            server_started=self.started,  # asked in the same `list-panes`, as the real one
        )

    def pane_facts(self, pane_id: str) -> PaneFacts | None:
        """As the real one, which asks ``#{start_time}`` and ``#{window_activity}`` (to the
        second, rounded down) in the same ``display-message``."""
        facts = self.facts.get(pane_id) if self._read() else None
        if facts is None:
            return None
        printed = self.output_at.get(pane_id)
        last = None if printed is None else datetime.fromtimestamp(int(printed.timestamp()), UTC)
        return replace(facts, server_started=self.started, last_output=last)

    def capture(
        self,
        pane_id: str,
        *,
        scrollback: int = 0,
        height: int | None = None,
        flags: bool = False,
    ) -> Capture:
        """The pane's screen as a test drew it (:attr:`screens`); a gone pane is an error."""
        self.binary()
        self._require_server()
        facts = self.facts.get(pane_id)
        if facts is None:
            raise TmuxError(f"can't find pane: {pane_id}")
        self.captured.append(pane_id)
        if self.draws_box and pane_id not in self.screens:
            agent_up = facts.current_command not in (PYTHON_LAUNCHER, "tmux")
            return Capture(list(INPUT_BOX) if agent_up else [], facts, 0)
        return Capture(list(self.screens.get(pane_id, [])), facts, 0)

    # The strict twins the shutdown paths use: each is the lenient answer UNLESS
    # the server could not be asked, and that single distinction is `reachable()`
    # already — it raises for a denied socket (`socket_denied`), a wedged one
    # (`answers_raises`) and an unrunnable client (`exec_unavailable`), and is
    # False (never raises) for a server that is simply not running. Deriving every
    # one from it (the start time too) keeps the fake from drifting from the real contract.
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

    def started_at(self) -> datetime | None:
        self.reachable()
        return self.started if self._read() else None

    def listener(self, recorded: Path | None = None) -> SocketAnswer | None:
        """What the socket file says, asked of it rather than of tmux (:attr:`socket_file`)."""
        if not self.socket_file:
            return "gone"
        return "answers" if self.running else None

    def socket_path(self) -> Path:
        """Where this server keeps its socket, and where ``-L`` from this shell finds it."""
        return Path("/run/fake/tmux-1000") / self.socket

    def server_socket_path(self) -> Path | None:
        """What ``#{socket_path}`` answers: where this server listens, while it runs."""
        return self.socket_path() if self._read() else None

    def at(self, path: Path | None) -> TmuxServer:
        """The server listening at ``path`` (:attr:`per_path`), else this one."""
        return self.per_path.get(path, self) if path is not None else self

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
        """As the real one: ``-S`` for a handle asked for at a path, else ``-L``."""
        where = ["-L", self.socket] if self.path is None else ["-S", str(self.path)]
        return [self.binary(), *where, "attach-session", "-t", f"={session}"]

    # -- input --
    def send_keys(self, pane_id: str, *keys: str) -> None:
        self._input(pane_id, "key", " ".join(keys))
        sending = self.draws_box and keys == ("Enter",) and pane_id in self.screens
        if sending and screen_reader.composer_text(self.screens[pane_id]):
            del self.screens[pane_id]  # sent: the box is empty again
            self._submitted(pane_id)

    def _submitted(self, pane_id: str) -> None:
        """What the window's ``UserPromptSubmit`` hook reports: a prompt started a turn."""
        for spawned in self.spawned:
            if spawned.get("pane_id") == pane_id:
                env = spawned["env"]
                assert isinstance(env, dict)
                agent_id = env.get(FLEET_AGENT_ENV_VAR)
                with store_session() as store:
                    store.mark_prompted("", datetime.now(tz=UTC), agent_id=agent_id)

    def send_literal(self, pane_id: str, text: str) -> None:
        self._input(pane_id, "literal", text)
        if text == "/exit" and self.honours_exit:
            self.die(pane_id, 0)

    def paste(self, pane_id: str, text: str) -> None:
        self._input(pane_id, "paste", text)
        if self.draws_box:  # in the input line, between the box's two rules
            self.screens[pane_id] = [INPUT_BOX[0], f"{MARK} {text}", *INPUT_BOX[2:]]

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
    # The desktop variables a spawn carries per window (#147) are this PROCESS's,
    # which a test must not depend on; one test below hands in its own.
    monkeypatch.setattr(tmux_core, "desktop_environment", lambda environ=None: {})
    return fake


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(fleet_service, "_sleep", fake.sleep)
    monkeypatch.setattr(fleet_service, "_monotonic", fake.monotonic)
    return fake


@pytest.fixture
def typed_first_lines(tmux: FakeTmux, monkeypatch: pytest.MonkeyPatch) -> FakeTmux:
    """First messages TYPED (``[fleet] first_prompt = "type"``), into panes that draw Claude
    Code's input box once the agent runs (:attr:`FakeTmux.draws_box`): the typed way's own
    rules — the wait for the agent, the trust question, what tmux refused — which a Claude
    Code agent meets on its command line otherwise (P0-W1a)."""
    tmux.draws_box = True
    _settings(monkeypatch, first_prompt="type")
    return tmux


def _first_lines(tmux: FakeTmux, pane_id: str | None = None) -> list[str]:
    """The first message each window was given (``pane_id``'s alone, when named), whichever
    way it went: the file its launch was named on Claude Code's command line
    (``--first-prompt-file``), or pasted into its pane (the typed way)."""
    lines: list[str] = []
    for spawned in tmux.spawned:
        if pane_id is not None and spawned.get("pane_id") != pane_id:
            continue
        command = spawned["command"]
        assert isinstance(command, list)
        path = _flag(command, delivery.FIRST_PROMPT_FLAG)
        if path is not None:
            lines.append(Path(path).read_text(encoding="utf-8"))
    pasted = [
        text
        for pane, kind, text in tmux.typed
        if kind == "paste" and (pane_id is None or pane == pane_id)
    ]
    return [*lines, *pasted]


@pytest.fixture
def claude_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A ``claude`` the binary check finds — a script, never Claude Code.

    Named so ``shutil.which("claude")`` resolves it on either platform: a
    shebanged script on POSIX, a ``.cmd`` on Windows, which is the shape pip and
    npm use and what makes PATHEXT match. `fleet.spawn` gates on exactly that
    call, so an extensionless file left every test using this fixture failing
    with "'claude' is not on your PATH" — the same PATHEXT bug as the gbrain
    fake in test_brain.py, in code written after that one was fixed.
    """
    bin_dir = tmp_path / "bin"
    script = fakebin.executable_fake(
        bin_dir,
        "claude",
        # `%*` quoted, or an argument carrying `&`, `|`, `>` or `^` is
        # interpreted by cmd.exe rather than echoed back.
        windows='echo fake claude: "%*"\nset /p line=',
        posix='echo "fake claude: $*"\nread line',
    )
    fakebin.prepend_to_path(bin_dir, monkeypatch)
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
    """Added on purpose, as a fleet's project is: with ``[projects] capture`` off, the
    default, a hook or a board write in a directory nobody added records nothing."""
    info = team_project(repo)
    with store_session() as store:
        store.onboard_project(info)
    return info


@pytest.fixture
def plain_project(tmp_path: Path) -> ProjectInfo:
    """A directory that is not a git checkout — a parent of several repos, say."""
    path = tmp_path / "plain"
    path.mkdir()
    info = team_project(path)
    with store_session() as store:
        store.onboard_project(info)
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


def _board_events(project: ProjectInfo, kind: str) -> list[TeamEvent]:
    """:func:`_events` as the events themselves, actor and details included."""
    with store_session() as store:
        return [e for e in store.recent_events(project.id, limit=50) if e.kind == kind]


def _fleet(verb: str, project: ProjectInfo, *words: str) -> str:
    """``project_service.fleet_command`` for ``project`` as the store holds it now: a spawn
    gives it its codename, which the commands a refusal or a note gives then name."""
    with store_session() as store:
        stored = store.get_project(project.id)
    return project_service.fleet_command(verb, stored or project, *words)


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
        "AISQUARE_TEAM_HUB": "",  # no hub here, so none from the server either
        "AISQUARE_EXPLAINABILITY_TARGET": "",  # nor a deployment override
        "AISQUARE_PERSONA": "",  # no persona: blank, never the server's (review of #201, F3)
        "AISQUARE_BOARD_CONTEXT": "all",  # the role's, per window (P1 WS-2, #279)
    }
    assert spawned["cwd"] == project.root and agent.cwd == project.root and not agent.worktree
    assert agent.pane_id == "%1" and agent.binary == "claude" and agent.spawned_by == "user"
    with store_session() as store:
        assert store.fleet_agent_by_label(project.id, "manager") == agent


def test_spawn_with_a_persona_carries_it_to_launch_and_records_it(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    receipt = fleet_service.spawn(project, "tester", persona="skeptic")

    command = _command(tmux)
    assert command[4:6] == ["launch", "tester"]
    assert _flag(command, "--persona") == "skeptic"
    assert command.index("--persona") < command.index("--name")
    assert receipt.agent.persona == "skeptic"
    assert receipt.notes == []
    with store_session() as store:
        stored = store.fleet_agent_by_label(project.id, receipt.agent.label)
    assert stored is not None and stored.persona == "skeptic"


def test_spawn_without_a_persona_passes_no_flag_and_records_none(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    receipt = fleet_service.spawn(project, "tester")

    assert "--persona" not in _command(tmux)
    assert receipt.agent.persona is None


def test_spawn_refuses_an_unknown_persona_before_any_window_worktree_or_row(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    with pytest.raises(fleet_service.FleetError) as caught:
        fleet_service.spawn(project, "coder", persona="nope")

    assert bundled_personas.known() in str(caught.value)
    assert tmux.spawned == []
    assert not (project.root / ".aisquare-worktrees").exists()
    with store_session() as store:
        assert store.fleet_agents(project.id, live_only=False) == []


def test_spawn_uses_the_roles_default_persona_and_a_flag_beats_it(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aisquare.core.config import FleetRoleSettings

    _settings(monkeypatch, roles={"coder": FleetRoleSettings(persona="minimalist")})

    by_default = fleet_service.spawn(project, "coder")
    by_flag = fleet_service.spawn(project, "coder", persona="careful")

    assert _flag(_command(tmux, 0), "--persona") == "minimalist"
    assert by_default.agent.persona == "minimalist" and by_default.notes == []
    assert _flag(_command(tmux, 1), "--persona") == "careful"
    assert by_flag.agent.persona == "careful" and by_flag.notes == []


def test_a_stale_default_persona_refuses_naming_the_config_key(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aisquare.core.config import FleetRoleSettings

    _settings(monkeypatch, roles={"coder": FleetRoleSettings(persona="retired")})

    with pytest.raises(fleet_service.FleetError) as caught:
        fleet_service.spawn(project, "coder")

    assert "[fleet.roles.coder].persona = 'retired'" in str(caught.value)
    assert bundled_personas.known() in str(caught.value)
    assert tmux.spawned == []


def test_a_persona_written_for_other_roles_is_a_receipt_note_never_a_refusal(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    receipt = fleet_service.spawn(project, "coder", worktree=False, persona="skeptic")

    assert receipt.agent.persona == "skeptic"
    assert receipt.notes == [
        "persona skeptic is written for tester, reviewer, runner, not coder — "
        "spawned with it anyway"
    ]


def test_spawn_records_where_its_server_listens_and_in_which_boot(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What uninstall and upgrade ask whether the row may still run (round 25 of #257): the
    socket tmux says its server listens on, which the asking shell's ``TMUX_TMPDIR`` cannot
    rebuild, and the boot, which no process outlives. Kept by the store as written."""
    monkeypatch.setattr(tmux_core, "boot_id", lambda: "b00t-1")

    agent = fleet_service.spawn(project, "manager").agent

    assert agent.socket_path == Path("/run/fake/tmux-1000/asq-test")
    assert agent.boot_id == "b00t-1"
    with store_session() as store:
        assert store.fleet_agent_by_label(project.id, "manager") == agent

    def refused() -> Path | None:
        raise TmuxError("tmux did not answer within 30s")

    monkeypatch.setattr(tmux, "server_socket_path", refused)  # the spawn goes on without it
    assert fleet_service.spawn(project, "coder", worktree=False).agent.socket_path is None


def test_spawn_records_when_its_server_started(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Follow-up of #257: the identity of the server the pane id names, as the server says
    it (``#{start_time}``), kept by the store as written. A server at the row's socket that
    started at another time is not the one the row runs on. Not saying costs the spawn
    nothing, and a row that recorded no start is judged by its own time, as before."""
    tmux.started = datetime(2026, 10, 13, 9, 0, 7, tzinfo=UTC)

    agent = fleet_service.spawn(project, "manager").agent

    assert agent.server_started_at == tmux.started
    with store_session() as store:
        assert store.fleet_agent_by_label(project.id, "manager") == agent
    assert not fleet_service._outlived(agent, tmux.started)
    assert fleet_service._outlived(agent, tmux.started + timedelta(seconds=1))
    assert fleet_service._outlived(agent, tmux.started - timedelta(days=1)), "any other time"
    assert not fleet_service._outlived(agent, None), "a start tmux does not report judges nothing"

    def refused() -> datetime | None:
        raise TmuxError("tmux did not answer within 30s")

    monkeypatch.setattr(tmux, "started_at", refused)  # the spawn goes on without it
    coder = fleet_service.spawn(project, "coder", worktree=False).agent
    assert coder.server_started_at is None
    assert fleet_service._outlived(coder, coder.created_at + timedelta(seconds=1))
    assert not fleet_service._outlived(coder, coder.created_at - timedelta(seconds=1))


def test_spawn_with_an_account_carries_the_callers_environment_into_the_window(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """`launch --account 1` restores "this shell's" login — inside the window that shell is
    whoever started the server, so the CALLER's view travels with the window."""
    fleet_service.spawn(project, "coder", account="1")

    spawned = tmux.spawned[-1]
    command, env = spawned["command"], spawned["env"]
    assert isinstance(command, list) and isinstance(env, dict)
    # The two variables this process lacks are unset for the child…
    # The STEM: this is `shutil.which("env")`, which finds Git's `env.EXE` on
    # Windows, so `endswith("env")` asserted the POSIX spelling of a program the
    # product looks up on both.
    assert Path(command[0]).stem.lower() == "env"
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


def test_a_window_resolves_the_hub_its_spawner_resolves(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A window inherits the tmux SERVER's environment, so its ``launch`` joined the hub
    the server was started under while the UI's Explainability tab, in the spawning
    process, resolved the key for another project: the tab's key was not the
    launches' (review of #170, D1b round 2, B1). The window now carries the
    spawner's hub, blank for none, which also overrides whatever the server kept."""
    hub = tmp_path / "hub"
    hub.mkdir()
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(hub))
    fleet_service.spawn(project, "coder")
    carried = tmux.spawned[-1]["env"]
    assert isinstance(carried, dict)
    assert carried.get("AISQUARE_TEAM_HUB") == str(hub.resolve())
    # The project the window's launch joins is the one the spawning process (the tab's
    # `key_project`) resolves, whatever the server was started under.
    spawner_sees = team_project(project.root)
    monkeypatch.setenv("AISQUARE_TEAM_HUB", carried["AISQUARE_TEAM_HUB"])
    assert team_project(project.root) == spawner_sees

    monkeypatch.setenv("AISQUARE_TEAM_HUB", "./relative")  # ignored here, so there
    fleet_service.spawn(project, "tester")
    relative = tmux.spawned[-1]["env"]
    assert isinstance(relative, dict) and relative.get("AISQUARE_TEAM_HUB") == ""


def test_a_window_traces_to_the_deployment_its_spawner_names(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The UI's Explainability tab resolves the target, and the deployment a project key is
    bound to, with the spawning process's AISQUARE_EXPLAINABILITY_TARGET, while a window's
    `launch` read the tmux server's: the tab named one deployment and the agents traced
    to another (review of #170's Setup-form merge, G2). Carried like the hub."""
    monkeypatch.setenv("AISQUARE_EXPLAINABILITY_TARGET", "prod")
    fleet_service.spawn(project, "coder")
    carried = tmux.spawned[-1]["env"]
    assert isinstance(carried, dict) and carried.get("AISQUARE_EXPLAINABILITY_TARGET") == "prod"


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


@pytest.mark.parametrize(
    ("role", "expected"),
    [("tester", True), ("coder2", True), ("scribe", True), ("codr", False)],
)
def test_role_ok_is_the_public_name_of_the_rule_spawn_applies(role: str, expected: bool) -> None:
    """A fleet role, a numbered seat, a name bound with `team bind`, and a typo."""
    bound = CliRunner().invoke(app, ["team", "bind", "scribe", "--bin", "claude"])
    assert bound.exit_code == 0, bound.output
    assert fleet_service.role_ok(role) is expected
    assert fleet_service.role_ok(role) is fleet_service._role_ok(role)


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
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings(monkeypatch, roles={"reviewer": FleetRoleSettings(extra_args=["--verbose"])})
    fleet_service.spawn(project, "reviewer", worktree=False, agent_args=["--model", "opus"])
    command = _command(tmux)
    assert "--verbose" in command, "the role's configured extra arg"
    assert command[-2:] == ["--model", "opus"]
    assert command.index("--verbose") < command.index("--model")


def test_the_built_in_reviewer_is_launched_with_a_shell(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """`--restricted` removed Bash, so the reviewer could run none of `aisquare task
    next`, `gh pr diff` or `gh pr review`, and every subagent it spawned to fetch the
    diff had no shell either (PR #257's review, run by a fleet reviewer, saw nothing).
    """
    fleet_service.spawn(project, "reviewer", worktree=False)
    command = _command(tmux)
    assert "--restricted" not in command
    # No editing tools either: the deny is the role's own, which the window's `launch`
    # adds (`harness.tool_policy`), so the command it is handed need not carry it.
    tail = command[command.index("reviewer") + 1 :]
    policy = harness.tool_policy("reviewer", binary="claude", args=tail, fleet=FleetSettings())
    assert {"Edit", "Write", "NotebookEdit"} <= set(policy.denied), "and no editing tools"


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
    # `+ .suffix`, because the fixture's file is `claude.cmd` on Windows and a
    # sibling built from a bare name would be `claude2` — which PATHEXT does not
    # consider a program, so `shutil.which("claude2")` finds nothing and the
    # spawn refuses. Deriving a NEW name from the fixture's path is the one way
    # to lose the extension after `executable_fake` went to the trouble of
    # returning it.
    other = claude_on_path.with_name("claude2" + claude_on_path.suffix)
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
    typed_first_lines: FakeTmux,
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
    typed_first_lines: FakeTmux,
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
    tmux: FakeTmux,
    typed_first_lines: FakeTmux,
    clock: FakeClock,
    claude_on_path: Path,
    project: ProjectInfo,
) -> None:
    receipt = fleet_service.spawn(project, "coder", worktree=False, prompt="hello")
    assert PROMPT_TIMEOUT <= clock.now < PROMPT_TIMEOUT + 5
    assert any("did not come up within 20 s" in note for note in receipt.notes)
    assert (receipt.agent.pane_id, "paste", "hello") in tmux.typed


def test_a_prompt_typed_past_the_timeout_is_not_typed_into_the_trust_question(
    tmux: FakeTmux,
    typed_first_lines: FakeTmux,
    clock: FakeClock,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow first start draws the trust question just past the timeout: the prompt and its
    Enter went in, Enter picked "No, exit", and the receipt said ``yes`` (review of #259,
    round 13). One look first, on that path too."""
    original = tmux.capture

    def drawn_once_the_timeout_passed(pane_id: str, **kwargs: Any) -> Capture:
        if clock.now >= PROMPT_TIMEOUT:
            tmux.screens[pane_id] = TRUST_SCREEN
        return original(pane_id, **kwargs)

    monkeypatch.setattr(tmux, "capture", drawn_once_the_timeout_passed)
    receipt = fleet_service.spawn(project, "coder", worktree=False, prompt="hello")

    assert tmux.typed == [] and receipt.prompt_typed == "no"
    assert any("folder-trust question" in note for note in receipt.notes)
    assert PROMPT_TIMEOUT <= clock.now < PROMPT_TIMEOUT + 5, "one look, not a second wait"


def test_a_multi_line_prompt_is_not_typed_before_the_agent_is_up(
    tmux: FakeTmux,
    typed_first_lines: FakeTmux,
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


@pytest.mark.parametrize(
    ("late", "says"),
    [
        ("paste", "tmux did not answer in time with the prompt for the agent's pane"),
        ("Enter", "the prompt: pasted it at its prompt, but tmux did not answer in time"),
        ("refused Enter", "the prompt: pasted it at its prompt, but tmux could not press Enter"),
    ],
)
def test_spawn_prompt_that_tmux_did_not_take_says_how_far_it_got(
    tmux: FakeTmux,
    typed_first_lines: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    late: str,
    says: str,
) -> None:
    """Review of #259, round 10, 8/9: a paste or an Enter that timed out, or an Enter refused
    after the paste went in, was "could not type the prompt", and the prompt sent again with
    ``fleet tell`` went in beside the first. One that timed out may still arrive; one whose
    Enter was refused waits at the agent's prompt, unsent."""
    original = tmux.pane_facts

    def up(pane_id: str) -> PaneFacts | None:
        tmux.set_command(pane_id, "claude")
        return original(pane_id)

    def wedged(*args: object) -> None:
        if late == "refused Enter":
            raise TmuxError("send-keys failed (fake)")
        raise TmuxTimedOut("tmux did not answer within 30s: send-keys")

    monkeypatch.setattr(tmux, "pane_facts", up)
    monkeypatch.setattr(tmux, "paste" if late == "paste" else "send_keys", wedged)
    receipt = fleet_service.spawn(project, "coder", worktree=False, prompt="start on tsk_1")
    # Round 11, 2/13: and the receipt does not say "not typed" of it either.
    assert receipt.prompt_typed == ("unsent" if late == "refused Enter" else "maybe")
    (note,) = [note for note in receipt.notes if "prompt" in note]
    assert note.startswith(says), note
    assert ("it may still arrive" in note) is (late != "refused Enter"), note
    assert "could not type the prompt" not in note
    assert receipt.failures == [note]


@pytest.mark.parametrize(
    ("late", "typed", "how"),
    [
        (
            "refused Enter",
            "unsent",
            "started fresh, but its hand-off prompt waits unsent at its prompt: press Enter in "
            "its pane",
        ),
        (
            "paste",
            "maybe",
            "started fresh, but tmux did not answer in time with its hand-off prompt: it may "
            "still arrive — read its screen before you send it again",
        ),
    ],
)
def test_a_replacement_whose_first_line_is_at_its_prompt_is_not_said_to_be_untyped(
    tmux: FakeTmux,
    typed_first_lines: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    late: str,
    typed: str,
    how: str,
) -> None:
    """Review of #259, round 11, 2/13: a hand-off prompt pasted with its Enter refused, or
    one tmux did not answer in time with, was ``prompt_typed`` false: "started fresh, but
    its hand-off prompt was NOT typed" on the receipt and the board's ``restarted`` line,
    and the page added "Tell it what to do". The Tell went in beside the first copy, both
    submitted as one message."""
    agent = _coder(project)
    _with_transcript(agent, tmp_path / "missing.jsonl")  # named, not on disk: a fresh start
    original_spawn, original_paste, original_keys = tmux.spawn_window, tmux.paste, tmux.send_keys
    pasted: list[str] = []

    def ready_spawn(*args: Any, **kwargs: Any) -> WindowInfo:
        window = original_spawn(*args, **kwargs)
        tmux.set_command(window.pane_id, "claude")
        return window

    def paste(pane_id: str, text: str) -> None:
        if late == "paste":
            raise TmuxTimedOut("tmux did not answer within 30s: paste-buffer")
        original_paste(pane_id, text)
        pasted.append(pane_id)

    def send_keys(pane_id: str, *keys: str) -> None:
        if pasted and keys == ("Enter",):
            raise TmuxError("send-keys failed (fake)")
        original_keys(pane_id, *keys)

    monkeypatch.setattr(tmux, "spawn_window", ready_spawn)
    monkeypatch.setattr(tmux, "paste", paste)
    monkeypatch.setattr(tmux, "send_keys", send_keys)
    receipt = fleet_service.restart(project, agent.label)

    assert receipt.resumed is False and receipt.prompt_typed == typed
    assert receipt.how == how and "NOT typed" not in receipt.how
    assert _events(project, "restarted") == [f"{agent.label} restarted — {how}"]
    (note,) = [note for note in receipt.failures if "prompt" in note]
    assert ("press Enter in its pane" in note) is (typed == "unsent"), note


def test_spawn_prompt_is_not_typed_into_a_dead_pane(
    tmux: FakeTmux,
    typed_first_lines: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
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
    assert env == {
        "AISQUARE_FLEET_AGENT": agent.id,
        "AISQUARE_TEAM_HUB": "",
        "AISQUARE_EXPLAINABILITY_TARGET": "",
        "AISQUARE_PERSONA": "",
        "AISQUARE_BOARD_CONTEXT": "all",
    }


def _window_env(tmux: FakeTmux) -> dict[str, str]:
    """The environment the newest window was opened with."""
    env = tmux.spawned[-1]["env"]
    assert isinstance(env, dict)
    return env


def test_a_reviewer_reads_no_board_by_default_and_a_flag_is_recorded_for_its_restart(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """P1 WS-2 (#279): the reviewer's default board context is `none`, so it judges a change
    cold; `--board-context` overrides it, per window, and the spec a restart or switch
    replays keeps it."""
    reviewer = fleet_service.spawn(project, "reviewer").agent
    assert _window_env(tmux)["AISQUARE_BOARD_CONTEXT"] == "none"
    assert reviewer.launch_spec is not None and reviewer.launch_spec.board_context == "none"

    told = fleet_service.spawn(project, "reviewer", label="reviewer-all", board_context="all")
    assert _window_env(tmux)["AISQUARE_BOARD_CONTEXT"] == "all"
    spec = told.agent.launch_spec
    assert spec is not None and spec.board_context == "all"
    role = fleet_service.role_settings("reviewer")
    assert fleet_service._board_context("reviewer", None, spec, role) == "all", "a replay keeps it"


def test_a_manager_is_refused_a_board_context_narrower_than_all(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    with pytest.raises(FleetError, match="a manager reads the whole board"):
        fleet_service.spawn(project, "manager", board_context="none")
    with pytest.raises(FleetError, match="--board-context must be one of all, relevant, none"):
        fleet_service.spawn(project, "coder", board_context="some")
    assert tmux.spawned == []


def test_fleet_stop_acts_for_its_as_else_its_own_window_else_the_user(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The actor an exit names (P1 WS-2, #279): `--as`, else the session of the fleet row
    whose window the command runs in (a manager's), else `user`."""
    now = datetime.now(UTC)
    with store_session() as store:
        store.upsert_session(
            TeamSession(id="ses_manager_1", project_id=project.id, role="manager",
                        started_at=now, last_seen_at=now)
        )  # fmt: skip
        store.upsert_fleet_agent(
            FleetAgent(id="agt_mgr", project_id=project.id, label="manager", role="manager",
                       pane_id="%1", session_id="ses_manager_1", cwd=project.root,
                       created_at=now)
        )  # fmt: skip
    assert fleet_service.acting_as() == "user"
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", "agt_mgr")
    assert fleet_service.acting_as() == "ses_manager_1"
    assert fleet_service.acting_as("ses_manager_1") == "ses_manager_1"


def test_a_spawn_runs_the_roles_model_ladder_and_its_restart_keeps_it(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#129 (b): the fleet path never asked `harness.resolve_model`, so every agent ran on
    the account's default model. A spawn now passes the ladder's model and effort, cached
    probe only, and records both; arguments that already choose one are the caller's."""
    ladder = harness.resolve_model("coder", probe=False)
    assert ladder is not None
    agent = _coder(project)
    command = _command(tmux)
    assert _flag(command, "--model") == ladder.model and _flag(command, "--effort") == ladder.effort
    spec = agent.launch_spec
    assert spec is not None and (spec.model, spec.effort) == (ladder.model, ladder.effort)
    assert command.count("--model") == 1
    assert "--model" not in spec.extra_args, "the spec's arguments stay the role's and yours"

    # The ladder moves (a probe verdict, an entitlement); a restart starts what it recorded.
    moved = ladder.model_copy(update={"model": "elsewhere", "effort": "low"})
    monkeypatch.setattr(harness, "resolve_model", lambda *a, **k: moved)
    tmux.die(agent.pane_id, 0)
    fleet_service.restart(project, agent.label)
    replayed = _command(tmux)
    assert (
        _flag(replayed, "--model") == ladder.model and _flag(replayed, "--effort") == ladder.effort
    )

    _coder(project, label="coder-pinned", agent_args=["--model", "sonnet"])
    pinned = _command(tmux)
    assert pinned.count("--model") == 1 and _flag(pinned, "--model") == "sonnet"
    assert "--effort" not in pinned, "a caller who chose the model chose the effort too"


def test_another_binary_gets_no_model_flag(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(harness, "is_default_agent", lambda binary: False)
    agent = _coder(project)
    assert "--model" not in _command(tmux)
    assert agent.launch_spec is not None and agent.launch_spec.model is None


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


@pytest.mark.parametrize("via", ["spawn", "restart"])
def test_a_spawn_onboards_a_captured_project_that_already_has_a_codename(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, via: str
) -> None:
    """A spawn is a deliberate add (#139) whatever the row already carries. The
    onboarding rode on ``ensure_codename``'s assignment, which a codenamed row never
    reaches — and ``project forget`` keeps the codename while clearing the mark. So
    forget, a prompt there (the row comes back captured), then ``fleet spawn`` — or a
    ``restart``/``switch``, which spawn through ``_respawn`` — recorded live agents on
    a project neither the sidebar nor ``project list`` showed."""
    from aisquare.services import project as project_service

    first = _coder(project)
    fleet_service.stop(project, first.label, force=True)
    project_service.forget(project.id)
    with store_session() as store:
        store.ensure_project(project)  # the next prompt there: captured again, not listed
        captured = store.get_project(project.id)
        assert captured is not None and captured.codename and captured.onboarded_at is None
        assert project.id not in {p.id for p in store.list_projects()}

    if via == "spawn":
        _coder(captured)
    else:
        fleet_service.restart(captured, first.label)

    with store_session() as store:
        live = store.fleet_agents(project.id, live_only=True)
        listed = {p.id for p in store.list_projects()}
    assert len(live) == 1
    assert project.id in listed, "the project its agent runs in is listed"


@pytest.mark.parametrize("refusal", ["account", "worktree", "cap"])
@pytest.mark.parametrize("state", ["captured", "forgotten"])
def test_a_refused_spawn_leaves_the_registration_as_it_found_it(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    plain_project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    refusal: str,
    state: str,
) -> None:
    """Review of #168, round 2: ``spawn`` onboarded the project before its refusals, so
    a spawn refused for an account the machine does not have, a worktree outside git or
    the cap listed a captured directory, or brought a forgotten one back, for an agent
    that never started. A spawn that does start still adds its project, and one in a
    forgotten directory comes back under the codename its tombstone kept."""
    target = plain_project if refusal == "worktree" else project
    first = _coder(target)
    fleet_service.stop(target, first.label, force=True)
    with store_session() as store:
        kept = store.get_project(target.id)
        assert kept is not None and kept.codename
        store.forget_project(target.id)
        if state == "captured":
            store.ensure_project(target)  # a prompt there: back, not listed
    # What `resolve_project` hands a spawn: the stored row, or for a forgotten
    # directory the bare one its cwd resolves to.
    asked = kept if state == "captured" else team_project(target.root)
    spawned_before = len(tmux.spawned)
    with pytest.raises(FleetError):
        if refusal == "account":
            fleet_service.spawn(asked, "coder", worktree=False, account="9")
        elif refusal == "worktree":
            fleet_service.spawn(asked, "coder", worktree=True)
        else:
            _settings(monkeypatch, max_agents_per_project=0)
            fleet_service.spawn(asked, "coder", worktree=False)

    assert len(tmux.spawned) == spawned_before, "nothing was started"
    with store_session() as store:
        assert target.id not in {p.id for p in store.list_projects()}, "not listed"
        row = store.get_project(target.id)
    if state == "captured":
        assert row is not None and row.onboarded_at is None, "still captured"
    else:
        assert row is None, "still forgotten"
    _settings(monkeypatch)  # the default cap again
    started = fleet_service.spawn(asked, "coder", worktree=False)
    assert started.tmux_session == f"asq-{kept.codename}"
    with store_session() as store:
        assert target.id in {p.id for p in store.list_projects()}, "a spawn that starts adds it"


def test_a_codename_is_not_an_onboarding(
    tmux: FakeTmux, repo: Path, tmp_path: Path, project_capture: None
) -> None:
    """Review of #168, round 2: ``ensure_codename`` onboarded, so ``fleet attach`` in a
    captured directory listed it and was then refused — it had no session to attach
    to. A codename needs a live row, so a directory never seen is captured for it,
    while ``[projects] capture`` is on."""
    project = team_project(repo)
    with store_session() as store:
        store.ensure_project(project)  # a prompt there: captured
    with pytest.raises(FleetError, match="nothing to attach to"):
        fleet_service.attach_argv(project)
    unseen = team_project(tmp_path / "unseen")
    named = fleet_service.ensure_codename(unseen)

    assert named.codename
    with store_session() as store:
        assert store.list_projects() == []
        captured = {p.id: p for p in store.captured_projects()}
    assert set(captured) == {project.id, unseen.id}
    assert captured[project.id].codename, "the attach still named the project"
    # A deliberate caller still adds it.
    fleet_service.rename(named, "quiet-lynx")
    with store_session() as store:
        assert [p.id for p in store.list_projects()] == [unseen.id]


def test_with_capture_off_a_codename_or_an_attach_writes_no_row(
    tmux: FakeTmux, tmp_path: Path
) -> None:
    """``[projects] capture`` off: ``fleet attach`` and a stop ask for the codename, and
    neither is an add, so a directory nobody added gets no row — its session is named
    by the walk with nothing taken, as the callers fall back to."""
    unseen = team_project(tmp_path / "unseen")
    assert fleet_service.ensure_codename(unseen).codename is None
    with pytest.raises(FleetError, match="nothing to attach to"):
        fleet_service.attach_argv(unseen)
    with store_session() as store:
        assert store.list_projects(all=True) == []


def test_with_capture_off_a_row_captured_before_gets_no_codename_but_from_a_spawn(
    tmux: FakeTmux, claude_on_path: Path, tmp_path: Path
) -> None:
    """Review of #259, round 18: ``ensure_codename`` held back only for a directory with no
    live row, so with capture off a row captured before it went off — hundreds of scratch
    directories on a 0.8.0 machine — was given a codename by ``fleet attach`` there, which
    then refused. Nobody added it: it gets none until a spawn adds it, with one."""
    where = tmp_path / "scratch"
    where.mkdir()
    scratch = team_project(where)
    with store_session() as store:
        store.ensure_project(scratch)  # what 0.8.0's capture wrote

    assert fleet_service.ensure_codename(scratch).codename is None
    with pytest.raises(FleetError, match="nothing to attach to"):
        fleet_service.attach_argv(scratch)
    with store_session() as store:
        [row] = store.list_projects(all=True)
    assert row.codename is None and row.onboarded_at is None, "nothing written"

    started = fleet_service.spawn(scratch, "coder", worktree=False)
    with store_session() as store:
        [listed] = store.list_projects()
    assert listed.id == scratch.id and listed.codename
    assert started.tmux_session == f"asq-{listed.codename}"


def test_with_capture_off_a_row_captured_with_a_codename_keeps_it(tmp_path: Path) -> None:
    """Review of #259, round 19: the guard that gives a directory nobody added no codename
    came before the stored row was read, so with capture off a row captured under 0.8.0,
    codename and agents and all, came back without its codename to a caller that passed a
    project with none: a stop then looked for its agents in the session ``asq-``. A codename
    the row holds comes back, and nothing is written."""
    where = tmp_path / "old"
    where.mkdir()
    old = team_project(where)
    with store_session() as store:
        store.ensure_project(old)  # what 0.8.0's capture wrote
        store.set_codename(old.id, "quiet-lynx")  # and its first spawn

    assert fleet_service.ensure_codename(old).codename == "quiet-lynx"
    with store_session() as store:
        [row] = store.list_projects(all=True)
    assert row.onboarded_at is None, "nothing written"


def test_the_codename_a_row_kept_is_read_by_its_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 18: ``_kept_codename`` read and built every project row,
    tombstones included, to find one by its key, on each ``fleet attach`` and first spawn in
    a directory nobody added. It reads the one row, live, captured or forgotten."""
    from aisquare.core.store import SqliteStore

    live, gone, unseen = (team_project(tmp_path / name) for name in ("live", "gone", "unseen"))
    with store_session() as store:
        store.ensure_project(live)
        store.set_codename(live.id, "quiet-lynx")
        store.onboard_project(gone)
        store.set_codename(gone.id, "brave-otter")
        store.forget_project(gone.id)

    def every_row(self: SqliteStore, **_kwargs: object) -> list[ProjectInfo]:
        raise AssertionError("read every project row for one")

    monkeypatch.setattr(SqliteStore, "list_projects", every_row)
    with store_session() as store:
        assert fleet_service._kept_codename(store, live) == "quiet-lynx"
        assert fleet_service._kept_codename(store, gone) == "brave-otter"
        assert fleet_service._kept_codename(store, unseen) is None
        assert store.get_project(gone.id) is None, "a tombstone is still hidden by default"


def test_with_capture_off_an_attach_where_nothing_was_added_never_finds_anothers_session(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, tmp_path: Path
) -> None:
    """Review of #259, round 17: with no row, ``attach_argv`` named the session by the walk
    with nothing taken, ``asq-<its first candidate>``, past no name in use. Another project
    that holds that codename has a session of that name on the default socket, and ``fleet
    attach`` in the directory nobody added attached to that project's manager. With no
    row, live or forgotten, no session is the directory's: it is refused."""
    unseen = team_project(tmp_path / "unseen")
    taken = codenames.codename_for(unseen.id)
    with store_session() as store:
        store.set_codename(project.id, taken)
    fleet_service.spawn(project, "manager")
    assert tmux.has_session(f"asq-{taken}"), "the premise: another project's session"

    with pytest.raises(FleetError, match="nothing to attach to"):
        fleet_service.attach_argv(unseen)

    with store_session() as store:
        assert unseen.id not in {p.id for p in store.list_projects(all=True)}


def test_an_attach_in_a_forgotten_directory_finds_the_session_its_codename_kept(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """A forget keeps the codename (its tombstone), and the agents' session stays under it:
    a forget that raced a spawn, or the window an ended manager left (``remain-on-exit``).
    With capture off, ``attach_argv`` takes the codename from the tombstone, which nothing
    revives, rather than the walk's first name, which is not this project's."""
    with store_session() as store:
        store.set_codename(project.id, "quiet-lynx")
    fleet_service.spawn(project, "manager")
    with store_session() as store:
        store.forget_project(project.id)

    argv = fleet_service.attach_argv(team_project(project.root))

    assert argv[-1] == "=asq-quiet-lynx"
    with store_session() as store:
        assert store.get_project(project.id) is None, "still forgotten"


def test_with_capture_off_a_spawn_where_nothing_was_added_adds_it(
    tmux: FakeTmux, claude_on_path: Path, repo: Path
) -> None:
    """The deliberate path still adds: a spawn someone asked for in a directory nobody
    added onboards it at the codename, which names the window's session, and leaves
    nothing captured."""
    asked = team_project(repo)
    receipt = fleet_service.spawn(asked, "coder", worktree=False)
    with store_session() as store:
        [listed] = store.list_projects()
        assert store.captured_projects() == []
    assert listed.id == asked.id and listed.codename
    assert receipt.tmux_session == f"asq-{listed.codename}"


@pytest.mark.parametrize("refusal", ["worktree", "window"])
@pytest.mark.parametrize("state", ["unseen", "forgotten"])
def test_with_capture_off_a_spawn_refused_after_its_codename_adds_nothing(
    tmux: FakeTmux,
    claude_on_path: Path,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    refusal: str,
    state: str,
) -> None:
    """Review of #259, round 17: with ``[projects] capture`` off, ``ensure_codename`` added
    the project for a spawn before its worktree and window existed, and a spawn refused
    after that — a directory at the tree's path that is no worktree, a window tmux would
    not start — left a directory nobody added listed with no agent, or brought a forgotten
    one back, as review of #168, round 2 had stopped. The codename is worked out without a
    write and stored with the add, once the agent's row is written; a spawn that starts
    names its session by it, and a forgotten project comes back under the one it kept."""
    asked = team_project(repo)
    kept: str | None = None
    if state == "forgotten":
        with store_session() as store:
            store.onboard_project(asked)
        first = fleet_service.spawn(asked, "coder", worktree=False).agent
        fleet_service.stop(asked, first.label, force=True)
        kept = _codename(asked)
        with store_session() as store:
            store.forget_project(asked.id)
    blocked = repo / fleet_service.settings().worktree_dir / "coder-1"
    real_spawn_window = tmux.spawn_window
    if refusal == "worktree":
        blocked.mkdir(parents=True)
        (blocked / "notes.txt").write_text("somebody's\n", encoding="utf-8")
    else:

        def refused(*args: Any, **kwargs: Any) -> WindowInfo:
            raise TmuxError("no server running")

        monkeypatch.setattr(tmux, "spawn_window", refused)
    spawned_before = len(tmux.spawned)

    with pytest.raises(FleetError):
        fleet_service.spawn(asked, "coder", worktree=refusal == "worktree")

    assert len(tmux.spawned) == spawned_before, "nothing was started"
    with store_session() as store:
        rows = {p.id: p for p in store.list_projects(all=True, include_forgotten=True)}
        assert store.list_projects(all=True) == [], "nothing listed, nothing captured"
    if state == "unseen":
        assert asked.id not in rows, "no row at all"
    else:
        assert rows[asked.id].codename == kept, "still forgotten, the codename kept"
    if refusal == "worktree":
        shutil.rmtree(blocked)
    else:
        monkeypatch.setattr(tmux, "spawn_window", real_spawn_window)
    started = fleet_service.spawn(asked, "coder", worktree=refusal == "worktree")
    with store_session() as store:
        [listed] = store.list_projects()
    assert listed.id == asked.id and listed.codename
    assert started.tmux_session == f"asq-{listed.codename}"
    if kept is not None:
        assert listed.codename == kept


def test_a_spawn_whose_codename_another_project_took_meanwhile_is_refused_and_adds_nothing(
    tmux: FakeTmux,
    claude_on_path: Path,
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The codename is stored with the add, after the window: another project that took it
    in between makes the add fail, and the spawn is refused, its window killed, rather than
    left in a session that is another project's."""
    asked = team_project(repo)
    other = team_project(tmp_path / "other")
    real = fleet_service.open_window

    def another_takes_it(*args: Any, **kwargs: Any) -> WindowInfo:
        window = real(*args, **kwargs)
        with store_session() as store:
            store.onboard_project(other)
            store.set_codename(other.id, codenames.codename_for(asked.id))
        return window

    monkeypatch.setattr(fleet_service, "open_window", another_takes_it)

    with pytest.raises(FleetError, match="another project took the codename"):
        fleet_service.spawn(asked, "coder", worktree=False)

    assert tmux.sessions.get(f"asq-{codenames.codename_for(asked.id)}", []) == []
    with store_session() as store:
        assert [p.id for p in store.list_projects(all=True)] == [other.id]
        assert store.fleet_agents(asked.id) == []


def test_the_automatic_hand_over_lists_nothing_a_forget_left_captured(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #168, round 2: the usage-limit hand-over starts its replacement through
    ``spawn``, which onboarded the project — listed with no action of anyone's, against
    #139's "only deliberate actions list a project". A switch by hand still does."""
    _two_slots_with_usage(monkeypatch, work=95, personal=10)
    agent = fleet_service.spawn(project, "coder", worktree=False, account="2").agent
    _with_transcript(agent, None)
    with store_session() as store:
        # What a forget that raced the spawn leaves once a prompt there captures the row.
        store.forget_project(project.id)
        store.ensure_project(project)
        captured = store.get_project(project.id)
    assert captured is not None

    moved = fleet_service.switch(captured, agent.label, automatic=True, reason="session limit")

    assert moved.to_slot == 3 and moved.started.ended_at is None
    with store_session() as store:
        assert store.list_projects() == [], "the hook's hand-over added nothing"
    fleet_service.switch(captured, agent.label, to="2")
    with store_session() as store:
        assert [p.id for p in store.list_projects()] == [project.id], "a switch by hand does"


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
    still running, and ``_remove_finished_worktrees`` — the same pass, now seeing
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
    A reboot, here: the row is from another boot, so its agent cannot be running.
    """
    monkeypatch.setattr(tmux_core, "boot_id", lambda: "b00t-1")
    agent = fleet_service.spawn(project, "coder").agent
    dead = _unreachable_server()
    monkeypatch.setattr(fleet_service, "server", lambda config=None: dead)
    monkeypatch.setattr(tmux_core, "boot_id", lambda: "b00t-2")

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
    monkeypatch.setattr(tmux_core, "boot_id", lambda: "b00t-1")
    _settings(monkeypatch, tmux_socket="asq-old")
    on_old = _coder(project)
    _settings(monkeypatch, tmux_socket="asq-new")
    on_new = _coder(project)
    monkeypatch.setattr(tmux_core, "boot_id", lambda: "b00t-2")  # a reboot since
    dead = _unreachable_server()

    def per_socket(config: FleetSettings | None = None) -> TmuxServer:
        return dead if config is not None and config.tmux_socket == "asq-old" else tmux

    monkeypatch.setattr(fleet_service, "server", per_socket)
    report = fleet_service.reap(project, server_down=True)
    assert [a.id for a in report.lost] == [on_old.id]
    with store_session() as store:
        assert [a.id for a in store.fleet_agents(project.id, live_only=True)] == [on_new.id]


def test_reap_server_down_holds_a_row_whose_server_may_still_run(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Follow-up of #257: tmux reports no server for a socket file taken from under a
    running server too (`error connecting to … (No such file or directory)`), and
    `reap --server-down` marked such rows lost under their agents, on another server under
    another `TMUX_TMPDIR` as well; only the doctor warned. The word now ends only what the
    one rule `stop`, `shutdown` and `uninstall` ask says cannot be running: a row whose
    socket file is gone in this boot is left live and named, with the way back to its
    server and `fleet shutdown --forget`; a reboot, or a stale file with nothing behind
    it, ends it as before."""
    if not hasattr(socket, "AF_UNIX") or not hasattr(os, "getuid"):
        pytest.skip("AF_UNIX sockets and a POSIX uid are what tmux needs: the rule asks one")
    monkeypatch.setattr(tmux_core, "boot_id", lambda: "b00t-1")
    coder = _coder(project)
    dead = _unreachable_server()  # tmux: `No such file or directory`, as for a taken file
    monkeypatch.setattr(fleet_service, "server", lambda config=None: dead)
    assert dead.listener(coder.socket_path) == "gone", "no file at the path it recorded"

    report = fleet_service.reap(project, server_down=True)

    assert report.lost == [] and report.ended == []
    assert [row.agent.id for row in report.held] == [coder.id]
    reason = report.held[0].reason
    assert "is gone, so no tmux client can reach the server" in reason, reason
    assert "left live: if it is, send that server SIGUSR1" in reason
    assert f"`{_fleet('shutdown', project, '--forget')}` ends its row on your word" in reason
    with store_session() as store:
        assert [a.id for a in store.fleet_agents(project.id, live_only=True)] == [coder.id]
    assert _events(project, "agent_exited") == []
    # The CLI says so, in both shapes.
    human = runner.invoke(app, ["fleet", "reap", "--server-down", "-P", project.id])
    assert human.exit_code == 0, human.output
    assert "0 lost, 0 worktrees removed, 1 left live" in human.stdout
    assert "⚠ coder-1  LEFT LIVE — its server's socket" in human.stdout
    machine = runner.invoke(app, ["--json", "fleet", "reap", "--server-down", "-P", project.id])
    assert machine.exit_code == 0, machine.output
    payload = json.loads(machine.stdout)
    assert [row["agent"]["id"] for row in payload["held"]] == [coder.id]
    assert payload["held"][0]["reason"] == reason and payload["lost"] == []
    # Negative controls: a stale socket file, nothing behind it, is a server that exited
    # (`kill-server`), and a row from another boot cannot be running: both are lost.
    with _socket_file(coder.tmux_socket, listening=False) as stale:
        coder = _recorded_at(coder, stale)
        swept = fleet_service.reap(project, server_down=True)
    assert [a.id for a in swept.lost] == [coder.id] and swept.held == []
    monkeypatch.setattr(fleet_service, "server", lambda config=None: tmux)
    rebooted = _coder(project)
    monkeypatch.setattr(fleet_service, "server", lambda config=None: dead)
    monkeypatch.setattr(tmux_core, "boot_id", lambda: "b00t-2")
    swept = fleet_service.reap(project, server_down=True)
    assert [a.id for a in swept.lost] == [rebooted.id] and swept.held == []


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
    monkeypatch.setattr(tmux_core, "boot_id", lambda: "b00t-1")
    here = _coder(project)
    there = fleet_service.spawn(plain_project, "coder", worktree=False).agent
    monkeypatch.setattr(tmux_core, "boot_id", lambda: "b00t-2")  # a reboot since
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
    # Well inside the window: tmux, and so the fake, says when in whole seconds, and a second
    # short of the edge left a loaded runner a fraction of one (ambient leg, run 38185297691).
    tmux.printed(agent.pane_id, ago=ACTIVITY_WINDOW / 2)
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


def test_list_agents_works_out_each_rows_address_once(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Follow-up of #257: a listing asked `_address` about five times per row — the
    observe, the reconcile, the lingering windows, the status and its `socket_at` — each
    resolving two paths, on asq's tick every two seconds. Once per row per listing now;
    `_address` itself stays uncached, as a stale "same socket" would send `-L` to another
    server. Live rows, one recorded elsewhere, and an ended row whose window lingers."""
    manager = fleet_service.spawn(project, "manager").agent
    elsewhere = _recorded_at(_coder(project), Path("/run/user/1000/tmux-1000/asq-test"))
    tmux.per_path[Path("/run/user/1000/tmux-1000/asq-test")] = tmux
    exited = _coder(project)
    tmux.die(exited.pane_id, 0)  # ended by this listing, its window kept (💤)
    asked: list[str] = []
    real = fleet_service._address

    def counted(agent: FleetAgent, config: FleetSettings | None = None) -> fleet_service._Address:
        asked.append(agent.id)
        return real(agent, config)

    monkeypatch.setattr(fleet_service, "_address", counted)

    listed = {status.agent.id: status for status in fleet_service.list_agents(project)}

    assert {listed[manager.id].state, listed[elsewhere.id].state} == {"waiting"}
    assert listed[exited.id].state == "exited" and listed[exited.id].agent.ended_at is not None
    assert listed[elsewhere.id].socket_at == elsewhere.socket_path
    assert listed[manager.id].socket_at is None
    assert sorted(asked) == sorted([manager.id, elsewhere.id, exited.id]), "once per row"


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
    third = fleet_service.ensure_codename(add_project(tmp_path / "third"))
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
        info = fleet_service.ensure_codename(add_project(root))
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


def test_tell_from_under_a_hub_nobody_added_files_the_note_where_the_agent_reads(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #259, round 17: an agent not at its prompt is told through a board note,
    which was filed on the board the CALLER resolved, ``AISQUARE_TEAM_HUB``'s when it set
    one, and with ``[projects] capture`` off a hub nobody added ended ``fleet tell`` in a
    traceback, then a ``FleetError``. Round 26, 2/11: the note goes to the board the agent
    reads, as its spawn recorded, so the caller's hub is never asked: filed, not refused."""
    agent = _coder(project)
    _board_session(agent, "working")
    hub = tmp_path / "hub"
    hub.mkdir()
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(hub))

    result = fleet_service.tell(project, "coder-1", "please rebase")

    assert not result.delivered and "filed as board note" in result.how, result
    assert agent.board == project.id and _events(project, "note") == ["please rebase"]
    assert tmux.typed == []


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


@pytest.mark.parametrize("timed_out", [False, True], ids=["refused", "timed out"])
def test_tell_whose_enter_did_not_go_through_leaves_the_text_at_its_prompt_and_files_no_note(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    timed_out: bool,
) -> None:
    """Review of #259, round 10, 4/9: the paste landed and only its Enter failed, but the tell
    said "tmux could not type it" and filed the text as a note too. The agent read the note
    at its next prompt, and the text left unsent in its input box went out with whatever was
    typed there next. It says the text is at its prompt, and files nothing. One whose Enter
    timed out may still send it, and is ``maybe``. Round 12, 2/9: one whose Enter was refused
    is ``unsent``, not ``no``, the word a caller that retries on "not typed" retries on."""
    agent = _coder(project)
    _board_session(agent, "waiting")
    tmux.set_command(agent.pane_id, "claude")

    def enter_fails(pane_id: str, *keys: str) -> None:
        if timed_out:
            raise TmuxTimedOut("tmux did not answer within 30s: send-keys")
        raise TmuxError("send-keys failed (fake)")

    monkeypatch.setattr(tmux, "send_keys", enter_fails)
    result = fleet_service.tell(project, "coder-1", "please rebase")
    assert result.typed == ("maybe" if timed_out else "unsent")
    assert result.how.startswith("pasted it at its prompt, but "), result.how
    assert "tmux could not type it" not in result.how and "board note" not in result.how
    if timed_out:
        assert "it may still arrive, and send it" in result.how
    else:
        assert result.how.endswith("— press Enter in its pane to send it")
    assert (agent.pane_id, "paste", "please rebase") in tmux.typed
    assert _events(project, "note") == []


@pytest.mark.parametrize(
    "cause",
    [TmuxError("server exited unexpectedly"), TmuxTimedOut("tmux did not answer within 30s")],
    ids=["refused", "timed out"],
)
def test_a_write_tmux_typed_a_part_of_says_how_much_and_is_maybe_only_while_more_may_come(
    cause: TmuxError,
) -> None:
    """Review of #259, round 11, 7/13: a ``send_literal`` chunk after the first that failed
    left a prefix in the pane. Refused, it was "no" (nothing typed); timed out, "it may still
    arrive", true of that prefix only, and of the one chunk the timeout asked for. Round 12,
    7/9: refused, it was ``maybe`` all the same, audited ``delivered=maybe``, where nothing
    more can arrive: it is ``unsent``, a part at its prompt."""

    def write() -> None:
        raise TmuxPartlyTyped(cause, 512, 2048)

    wrote = fleet_service.pane_write("the text", write)
    assert wrote.typed == ("maybe" if isinstance(cause, TmuxTimedOut) else "unsent")
    said = wrote.late("coder-1")
    assert said.startswith(
        "tmux typed 512 of the 2048 bytes of the text into coder-1's pane, then "
    )
    assert ("more of it may still arrive" in said) is isinstance(cause, TmuxTimedOut), said
    assert said.endswith("— read its live screen before you send it again"), said


def test_a_chain_of_writes_says_what_went_in_before_the_one_that_did_not() -> None:
    """Review of #259, round 12, 8/9: ``pane_writes`` answered only the write that failed, and
    each caller worked out by hand what was already in the pane, a quick answer not at all
    (1/9). It says what went in (``went``); refused past it, the chain is ``unsent``, and
    one sentence and one audit word say it for every caller."""
    typed: list[str] = []

    def refused() -> None:
        raise TmuxError("send-keys failed (fake)")

    def late() -> None:
        raise TmuxTimedOut("tmux did not answer within 30s: send-keys")

    wrote = fleet_service.pane_writes(
        ("the text", lambda: typed.append("text")),
        None,
        ("Enter", refused),
        ("more", lambda: typed.append("more")),
    )
    assert (wrote.typed, wrote.what, wrote.went, wrote.audited) == (
        "unsent",
        "Enter",
        ("the text",),
        "delivered=unsent",
    )
    assert typed == ["text"], "nothing after the write that did not go"
    assert wrote.said("coder-1", press="press Enter on the pad") == (
        "coder-1: typed it at its prompt, but tmux could not press Enter (send-keys failed "
        "(fake)) — press Enter on the pad to send it"
    )
    first = fleet_service.pane_writes(("the text", refused), ("Enter", lambda: None))
    assert (first.typed, first.went, first.audited) == ("no", (), "failed")
    waits = fleet_service.pane_writes(("the keys", lambda: None), ("Enter", late))
    assert (waits.typed, waits.went, waits.audited) == ("maybe", ("the keys",), "delivered=maybe")
    assert "it may still arrive, and send it" in waits.said("coder-1", press="press Enter")


def test_tell_says_a_paste_that_timed_out_may_be_typed_as_well_as_noted(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 9, 1/8: a wedged server may still run the queued paste once it
    recovers, so the note it falls back to may reach the agent beside the typed text. Round
    10, 6/9: and it is ``maybe``, which the remote's trail says (``delivered=maybe``)."""
    agent = _coder(project)
    _board_session(agent, "waiting")
    tmux.set_command(agent.pane_id, "claude")

    def wedged(pane_id: str, text: str) -> None:
        raise TmuxTimedOut("tmux did not answer within 30s: paste-buffer")

    monkeypatch.setattr(tmux, "paste", wedged)
    result = fleet_service.tell(project, "coder-1", "please rebase")
    assert not result.delivered and result.typed == "maybe"
    assert result.how.startswith("filed as board note #1 to coder-1, and tmux did not answer")
    assert "it may still arrive at its prompt as well, unsent" in result.how
    assert _events(project, "note") == ["please rebase"]


def test_a_tell_whose_paste_timed_out_is_maybe_when_its_store_is_locked(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 12, 3/9: only a ``FleetError`` from the note was caught, and a
    store locked past its busy timeout raises ``sqlite3.OperationalError``: it went out of
    ``tell``, the CLI failed and the remote answered 400 ``write_failed`` (``delivered=no``),
    while the paste may still land and a Send again would type it a second time."""
    from aisquare.services import team

    agent = _coder(project)
    _board_session(agent, "waiting")
    tmux.set_command(agent.pane_id, "claude")

    def wedged(pane_id: str, text: str) -> None:
        raise TmuxTimedOut("tmux did not answer within 30s: paste-buffer")

    def locked(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(tmux, "paste", wedged)
    monkeypatch.setattr(team, "add_note", locked)
    result = fleet_service.tell(project, "coder-1", "please rebase")
    assert result.typed == "maybe", result
    assert "it may still arrive at its prompt, unsent" in result.how
    assert result.how.endswith("It was not filed as a board note: database is locked"), result.how


def test_a_tell_whose_paste_timed_out_is_maybe_when_its_note_cannot_be_filed(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 11, 6/13: the note a timed-out paste falls back to is filed after
    a paste that may still land, and an unknown sender (or a Team switched off) raised from
    it: the tell answered only the note's error, and the remote's trail `delivered=no`."""
    agent = _coder(project)
    _board_session(agent, "waiting")
    tmux.set_command(agent.pane_id, "claude")

    def wedged(pane_id: str, text: str) -> None:
        raise TmuxTimedOut("tmux did not answer within 30s: paste-buffer")

    monkeypatch.setattr(tmux, "paste", wedged)
    result = fleet_service.tell(project, "coder-1", "please rebase", sender="nope")
    assert result.typed == "maybe", result
    assert result.how.startswith("tmux did not answer in time with the text"), result.how
    assert "it may still arrive at its prompt, unsent" in result.how
    assert result.how.endswith("It was not filed as a board note: unknown sender session 'nope'"), (
        result.how
    )
    assert _events(project, "note") == []

    # Control: a paste tmux refused typed nothing, and the note's error is the tell's.
    monkeypatch.setattr(tmux, "paste", lambda pane_id, text: (_ for _ in ()).throw(TmuxError("x")))
    with pytest.raises(FleetError, match="unknown sender session 'nope'"):
        fleet_service.tell(project, "coder-1", "please rebase", sender="nope")


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


def test_tell_files_a_note_for_a_held_row_rather_than_type_into_the_pane_under_its_id(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 2: after a `kill-server` and any respawn, a row whose board still
    reads `waiting` (a killed agent fires no SessionEnd) is held as another server's, and
    `tell` asked only whether the pane under its id ran an agent. On the server that took
    its socket that id is another agent's, here another project's manager at its prompt,
    which got the text and an Enter. The pane must be the row's own, on its own server."""
    monkeypatch.setattr(tmux_core, "boot_id", lambda: "b00t-1")
    tmux.started = datetime(2026, 10, 13, 9, 0, 7, tzinfo=UTC)
    coder = _coder(project)
    _board_session(coder, "waiting")
    newcomer = FakeTmux()  # at the same path, so `-L` reaches it
    newcomer.started = tmux.started + timedelta(minutes=5)
    newcomer.spawn_window("asq-elsewhere", name="manager", cwd=project.root, command=["claude"])
    newcomer.set_command(coder.pane_id, "claude")  # its %1 runs an agent: someone else's
    monkeypatch.setattr(fleet_service, "server", lambda config=None: newcomer)

    result = fleet_service.tell(project, "coder-1", "please rebase")

    assert not result.delivered and "board note" in result.how, result.how
    assert newcomer.typed == [], "nothing pasted into the pane that took its id"
    assert _events(project, "note") == ["please rebase"]
    # Negative control: on its own server, the same waiting row is typed into.
    monkeypatch.setattr(fleet_service, "server", lambda config=None: tmux)
    tmux.set_command(coder.pane_id, "claude")
    assert fleet_service.tell(project, "coder-1", "please rebase").delivered
    assert tmux.typed == [
        (coder.pane_id, "paste", "please rebase"),
        (coder.pane_id, "key", "Enter"),
    ]


def test_tell_an_unknown_label_is_no_such_agent(tmux: FakeTmux, project: ProjectInfo) -> None:
    with pytest.raises(NoSuchAgent, match="no live agent 'coder-9'"):
        fleet_service.tell(project, "coder-9", "hello")
    with pytest.raises(NoSuchAgent, match="no live agent 'coder-9'"):
        fleet_service.stop(project, "coder-9")


# --- attach a persona to a running agent (docs/plans/spawn-personas.md §4.7, §7 "P8") ---


def _preface(name: str, *, replaces: str | None = None) -> str:
    tail = f"; it replaces {replaces}" if replaces else ""
    return f"aisquare: the operator attached persona {name} to you — it applies from now on{tail}"


def test_attaching_to_a_waiting_agent_records_it_and_types_the_briefing(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    from aisquare.core import personas

    agent = _coder(project)
    _board_session(agent, "waiting")
    tmux.set_command(agent.pane_id, "claude")

    receipt = fleet_service.attach_persona(project, "coder-1", "skeptic")

    briefing = personas.briefing(personas.resolve("skeptic", project.root))
    assert (receipt.persona, receipt.replaced, receipt.delivered) == ("skeptic", None, "typed")
    assert tmux.typed == [
        (agent.pane_id, "paste", "\n".join([_preface("skeptic"), *briefing])),
        (agent.pane_id, "key", "Enter"),
    ]
    with store_session() as store:
        row = store.get_fleet_agent(agent.id)
        session = store.get_session(agent.session_id or "")
    assert row is not None and row.persona == "skeptic"
    assert session is not None and session.persona == "skeptic"
    assert _events(project, "persona_attached") == ["persona skeptic attached to coder-1"]
    assert _events(project, "note") == []


def test_attaching_to_a_busy_agent_records_it_and_files_no_briefing_on_the_board(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """The briefing is typed or it is not delivered. Filed as a board note, which is what
    this pinned before, it carried the persona's body to every other session on the board
    (review of #240, finding 7). The whole rule is pinned in
    ``tests/test_attaching_to_a_busy_agent_keeps_the_persona_off_the_board.py``."""
    agent = _coder(project)
    _board_session(agent, "working")

    receipt = fleet_service.attach_persona(project, "coder-1", "skeptic")

    assert receipt.delivered == "noted" and "with its next prompt" in receipt.how
    assert tmux.typed == [], "never typed into a busy agent"
    assert _events(project, "note") == [], "and nothing filed on the board in its place"
    assert _events(project, "persona_attached") == ["persona skeptic attached to coder-1"]
    with store_session() as store:
        row = store.get_fleet_agent(agent.id)
    assert row is not None and row.persona == "skeptic"


def test_attaching_an_unknown_persona_refuses_before_the_store_or_tmux(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    agent = _coder(project)
    tmux.installed = False  # from here on any tmux call would raise TmuxUnavailable instead

    with pytest.raises(FleetError, match=bundled_personas.known()):
        fleet_service.attach_persona(project, "coder-1", "nope")

    with store_session() as store:
        row = store.get_fleet_agent(agent.id)
    assert row is not None and row.persona is None
    assert _events(project, "persona_attached") == []


def test_attaching_to_an_unknown_label_is_no_such_agent(
    tmux: FakeTmux, project: ProjectInfo
) -> None:
    with pytest.raises(NoSuchAgent, match="no live agent 'coder-9'"):
        fleet_service.attach_persona(project, "coder-9", "skeptic")
    assert _events(project, "persona_attached") == []


def test_replacing_a_persona_records_the_new_one_and_the_preface_names_the_old(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    agent = _coder(project, persona="minimalist")
    _board_session(agent, "waiting")
    tmux.set_command(agent.pane_id, "claude")

    receipt = fleet_service.attach_persona(project, "coder-1", "skeptic")

    assert (receipt.persona, receipt.replaced) == ("skeptic", "minimalist")
    assert tmux.typed[0][2].startswith(_preface("skeptic", replaces="minimalist") + "\n")
    assert _events(project, "persona_attached") == [
        "persona skeptic attached to coder-1 (replaces minimalist)"
    ]


def test_attaching_to_an_agent_that_has_not_joined_records_only_the_fleet_row(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    agent = _coder(project)

    fleet_service.attach_persona(project, "coder-1", "careful")

    with store_session() as store:
        row = store.get_fleet_agent(agent.id)
        session = store.get_session(agent.session_id or "")
    assert row is not None and row.persona == "careful"
    assert session is None


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


def test_a_managers_own_stop_says_so_and_nudges_nobody_and_a_kill_by_anyone_else_does(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1 WS-2 (#279): a manager's `fleet stop` names it as the actor, with --force in the
    details, and types no nudge into it; a kill by anyone else is a death it must hear of,
    and a clean stop by the human is news for its next wake, not a nudge."""
    manager = fleet_service.spawn(project, "manager").agent
    _coder(project, label="coder-a")
    _coder(project, label="coder-b")
    _coder(project, label="coder-c")
    nudges = _recorded_nudges(monkeypatch)

    fleet_service.stop(project, "coder-a", force=True, actor=manager.session_id)
    assert nudges == []
    fleet_service.stop(project, "coder-b", force=True, actor="user")
    assert nudges == ["coder-b exited"]
    fleet_service.stop(project, "coder-c", actor="user")
    assert nudges == ["coder-b exited"], "a clean stop is news, not a nudge"

    exits = {e.text.split(" ")[0]: e for e in _board_events(project, "agent_exited")}
    assert exits["coder-a"].actor == manager.session_id
    assert exits["coder-a"].meta == {"status": None, "force": True, "released": 0, "why": None}
    assert exits["coder-a"].text == "coder-a exited (?) — stopped by manager (--force)"
    assert exits["coder-c"].text == "coder-c exited (0) — stopped by user"


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
    # No longer live — but the refused kill left the dead window standing, and the
    # listing keeps an ENDED row beside its window as 💤 exited (#138).
    [lingering] = fleet_service.list_agents(project)
    assert lingering.agent.id == agent.id and lingering.agent.ended_at is not None
    assert lingering.state == "exited"


def test_stop_and_restart_leave_live_a_row_whose_socket_was_taken_from_under_its_server(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """Round 29 of #257: tmux's strict reads answer "no such pane" for a socket file that is
    gone (`error connecting to <path> (No such file or directory)`), so `fleet stop` ended
    the row of an agent whose server ran on, which `shutdown` and `uninstall` count as
    running. `uninstall` then went on under it, and `restart` stopped it the same way and
    started a second agent in its worktree. Both now ask the rule those two ask."""
    coder = _coder(project)
    spawned = len(tmux.spawned)
    tmux.running = False  # no tmux client reaches the server…
    tmux.socket_file = False  # …because its socket file is gone, not because it exited

    with pytest.raises(FleetError, match=r"'coder-1' was not stopped: .* is gone") as stopped:
        fleet_service.stop(project, "coder-1")
    assert "SIGUSR1" in str(stopped.value)
    assert f"shutdown --project {project.id} --forget`" in str(stopped.value)
    # Refused before its stop is asked: its replacement could not start at that path either
    # (`_refuse_a_freed_socket`, asked with the replay's refusals: review of #259, round 2).
    with pytest.raises(FleetError, match=r"coder-1 .* runs on lost its socket file") as again:
        fleet_service.restart(project, "coder-1")
    assert "SIGUSR1" in str(again.value)
    assert f"`{_fleet('shutdown', project, '--forget')}`" in str(again.value)

    assert len(tmux.spawned) == spawned, "no second agent in its worktree"
    with store_session() as store:
        assert [a.id for a in store.fleet_agents(project.id, live_only=True)] == [coder.id]
    assert _events(project, "agent_exited") == []
    # Negative control: a server that exited leaves its socket file behind, where nothing
    # listens, and that row's pane is gone: stopped as before.
    tmux.socket_file = True
    ended = fleet_service.stop(project, "coder-1").agent
    assert ended.ended_at is not None and tmux.killed == []


def test_spawn_refuses_to_start_a_server_at_a_socket_a_running_server_lost(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    plain_project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Follow-up of #257: `tmux -L` finds no file at the socket path and starts a server
    there. A live row's server whose socket file was taken from under it (systemd-tmpfiles,
    logind) may still run, which `stop`, `shutdown` and `uninstall` all count; a spawn
    started a second server at the freed path, which answered for the row from then on,
    and the agent kept running where nothing could reach it. The server is shared, so
    another project's spawn is refused the same way. A stale file (the server exited) and
    a row from another boot are no reason: tmux replaces the one, and nothing outlives
    the other."""
    monkeypatch.setattr(tmux_core, "boot_id", lambda: "b00t-1")
    coder = _coder(project)
    spawned = len(tmux.spawned)
    tmux.running = False  # no tmux client reaches the server…
    tmux.socket_file = False  # …because its socket file is gone, not because it exited

    with pytest.raises(FleetError, match=r"not started: .* lost its socket file") as refused:
        fleet_service.spawn(project, "coder")
    said = str(refused.value)
    assert f"the tmux server that coder-1 ({project.root.name}) runs on lost its" in said
    assert "may still be running" in said
    assert "SIGUSR1" in said and f"`{_fleet('shutdown', project, '--forget')}`" in said
    with pytest.raises(FleetError, match="lost its socket file"):
        fleet_service.spawn(plain_project, "manager")  # the server is every project's

    assert len(tmux.spawned) == spawned and tmux.running is False, "no second server"
    with store_session() as store:
        assert [a.id for a in store.fleet_agents(project.id, live_only=True)] == [coder.id]
        assert store.fleet_agents(plain_project.id, live_only=False) == []
    # Negative control: a server that exited leaves its socket file, where nothing listens,
    # and a spawn starts a fresh server at the path, as it always did.
    tmux.socket_file = True
    manager = fleet_service.spawn(plain_project, "manager").agent
    assert tmux.running and manager.pane_id in tmux.facts
    # And a row from another boot cannot be running on any server, socket file or not.
    tmux.socket_file = False
    monkeypatch.setattr(tmux_core, "boot_id", lambda: "b00t-2")
    assert fleet_service.spawn(project, "coder").agent.label == "coder-2"


def test_the_rows_a_new_server_would_answer_for_are_read_in_one_query(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    plain_project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #259, round 4: before a server starts at the fleet's socket, every live row
    recorded there is asked about, in every project, forgotten ones' included. That walked
    `list_projects` and ran one `fleet_agents` query per project ever registered, captured
    directories included, twice per spawn. One query now, and the refusal still names a
    forgotten project's row by its project. Whether the store has ``team_session`` to read a
    row's unrecorded board from (round 27, 5/7) is told by the names its open read, not by a
    ``sqlite_master`` query of each store's first fleet read (round 28, 9/10)."""
    monkeypatch.setattr(tmux_core, "boot_id", lambda: "b00t-1")
    coder = _coder(project)
    other = fleet_service.spawn(plain_project, "manager").agent
    with store_session() as store:
        store.forget_project(plain_project.id)
    tmux.running = False  # no tmux client reaches the server…
    tmux.socket_file = False  # …because its socket file is gone, not because it exited

    with store_session() as store:
        asked: list[str] = []
        store._conn.set_trace_callback(asked.append)  # type: ignore[attr-defined]
        try:
            rows = list(fleet_service._live_rows_here(store, tmux, fleet_service.settings()))
        finally:
            store._conn.set_trace_callback(None)  # type: ignore[attr-defined]

    assert {agent.id: answer for agent, answer in rows} == {coder.id: "gone", other.id: "gone"}
    reads = [sql for sql in asked if sql.lstrip().upper().startswith("SELECT")]
    assert len(reads) == 1 and "FROM fleet_agent WHERE ended_at IS NULL" in reads[0], reads
    with pytest.raises(FleetError, match="lost its socket file") as refused:
        fleet_service.spawn(project, "coder")
    assert f"manager ({plain_project.root.name})" in str(refused.value), "forgotten, named"


def test_a_spawn_over_a_server_that_exited_leaves_its_rows_lost_not_held(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    plain_project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #259, round 2: a `kill-server` (or a tmux crash) leaves the socket file with
    nothing listening, and the first spawn in any project starts the next server there.
    Every row of the old server had recorded its start, and under the new one each was
    held as another server's for good: `reap` left it, `stop` refused it, and it still
    counted toward the cap and the one-manager rule, until `shutdown --forget`. 0.8.0 read
    them lost. The spawn forgets the start of the server that exited while the stale file
    still says so, in every project, and the rows read lost as before: `reap` records
    them, `stop` ends them, `restart` brings a coder back. A row whose server listens at
    another path is not that server's."""
    monkeypatch.setattr(tmux_core, "boot_id", lambda: "b00t-1")
    tmux.started = datetime(2026, 10, 13, 9, 0, 7, tzinfo=UTC)
    manager = fleet_service.spawn(project, "manager").agent
    coder = _coder(project)
    other = fleet_service.spawn(plain_project, "manager").agent
    apart = Path("/elsewhere/tmux-1000/asq-test")  # another TMUX_TMPDIR's server, still up
    elsewhere = tmux.per_path[apart] = FakeTmux()
    elsewhere.started = tmux.started
    window = elsewhere.spawn_window("asq-x", name="coder-2", cwd=project.root, command=["c"])
    moved = {"socket_path": apart, "pane_id": window.pane_id}
    with store_session() as store:
        kept = store.upsert_fleet_agent(_coder(project).model_copy(update=moved))
    tmux.kill_server()  # `tmux -L asq kill-server`: the file stays, nothing listens
    assert tmux.listener() is None

    receipt = fleet_service.spawn(project, "coder")  # any spawn, in any project

    assert receipt.agent.label == "coder-3" and tmux.running
    states = {s.agent.label: (s.state, s.held) for s in fleet_service.list_agents(project)}
    assert states == {
        "manager": ("lost", False),
        "coder-1": ("lost", False),
        "coder-2": ("waiting", False),  # its server is not the one that exited
        "coder-3": ("waiting", False),
    }, states
    [theirs] = fleet_service.list_agents(plain_project)
    assert (theirs.agent.id, theirs.state, theirs.held) == (other.id, "lost", False)
    [said] = [note for note in receipt.notes if "had exited" in note]
    assert said.startswith(f"the tmux server at {tmux.socket_path()} had exited, leaving its")
    for name in ("manager", "coder-1"):
        assert f"{name} ({project.root.name})" in said, said
    assert f"manager ({plain_project.root.name})" in said, "every project's, on a shared server"
    assert " read lost from now on, as their panes went with it " in said, said
    # Rows of every project: `reap --all`, and a restart naming each row's project and label
    # (review of #259, round 21) — bare, each acted on the active project, or was refused.
    assert "(`aisquare fleet reap --all` records them; " in said, said
    with store_session() as store:
        ours, theirs_project = store.get_project(project.id), store.get_project(plain_project.id)
    assert ours is not None and theirs_project is not None
    for where, label in ((ours, "manager"), (ours, "coder-1"), (theirs_project, "manager")):
        restart = project_service.fleet_command("restart", where, label)
        assert f"`{restart}`" in said, said
    assert "`aisquare fleet reap`" not in said and "`aisquare fleet restart`" not in said
    # Lost, as in 0.8.0: a coder comes back under its own label, the manager's row ends at
    # a stop, and `reap` records the rest; nothing is held, and nothing of coder-3's touched.
    assert fleet_service.restart(project, "coder-1").replaced.id == coder.id
    assert fleet_service.stop(project, "manager").agent.id == manager.id
    report = fleet_service.reap()
    assert [row.id for row in report.lost] == [other.id] and report.held == []
    with store_session() as store:
        still = store.get_fleet_agent(kept.id)
    assert still is not None and still.ended_at is None and still.server_started_at is not None
    assert [typed for typed in tmux.typed if typed[0] == receipt.agent.pane_id] == []


def test_the_exited_note_tells_a_forgotten_projects_rows_to_add_it_back_first(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, plain_project: ProjectInfo
) -> None:
    """Review of #259, round 21: the note names rows of forgotten projects too, which neither
    ``reap --all`` nor ``restart --project`` reaches: a restart for one was refused as no
    such project. Such a row gets no restart, and its project is said to need adding back.
    Round 23, 5/10: nor is ``reap --all`` said to record it, which it never walks."""
    ours = fleet_service.spawn(project, "manager").agent
    theirs = fleet_service.spawn(plain_project, "manager").agent
    with store_session() as store:
        store.forget_project(plain_project.id)
        named = store.get_project(project.id)
    assert named is not None

    said = fleet_service._exited_note(tmux, [ours, theirs])

    restart = project_service.fleet_command("restart", named, "manager")
    assert f"`{restart}` starts it again" in said, said
    assert said.count("aisquare fleet restart") == 1, said
    onboard = project_service.onboard_command(plain_project.root)
    assert f"{plain_project.root.name} is forgotten: {onboard} adds it back first" in said
    # Round 23, 5/10: `reap --all` walks the projects it reaches, so it records the one row
    # of a live project, "it", and never the forgotten project's alone.
    assert "(`aisquare fleet reap --all` records it; " in said, said
    alone = fleet_service._exited_note(tmux, [theirs])
    assert "reap --all" not in alone and "aisquare fleet restart" not in alone, alone
    assert f"({plain_project.root.name} is forgotten: {onboard} adds it back first)" in alone


def test_rows_in_no_recorded_project_are_said_to_be_out_of_reach_not_left_mid_sentence(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 22, 5/9: a live row can be in a project with no record at all,
    and the rows a spawn asks about are read whatever their project. When every stranded row
    was such a row, the refusal over a freed socket gave no way out and ended mid-sentence,
    "if it is not, ", and the exited note said ``reap --all`` records rows it never walks.
    Both now say no fleet command reaches them, and the refusal how long it holds."""
    monkeypatch.setattr(tmux_core, "boot_id", lambda: "b00t-1")
    coder = _coder(project)
    with store_session() as store:  # ended in its own project, and live in none
        store.end_fleet_agent(coder.id)
        orphan = store.upsert_fleet_agent(
            coder.model_copy(update={"id": new_agent_id(), "project_id": "prj_nowhere"})
        )
    tmux.running, tmux.socket_file = False, False  # a server that lost its socket file

    with pytest.raises(FleetError, match="lost its socket file") as refused:
        fleet_service.spawn(project, "coder")
    said = str(refused.value)
    note = fleet_service._exited_note(tmux, [orphan])

    unreached = "no fleet command reaches coder-1 (prj_nowhere): its project is not one this"
    assert said.endswith(
        f"if it is not, {unreached} machine has a record of, so a spawn here is refused until "
        "the machine restarts"
    ), said
    assert f"({unreached} machine has a record of)" in note, note
    assert "reap --all" not in note, note


def test_the_freed_socket_refusal_and_the_exited_note_name_rows_projects_alike(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    plain_project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #259, round 22, 9/9: the spawn's refusal over a freed socket and its note on
    an exited server each read the rows' projects and wrote what a forgotten one needs, and
    the copies had drifted ("adds it back first" against "for both", sorted against row
    order). One reading names them in both, in row order, with one sentence."""
    monkeypatch.setattr(tmux_core, "boot_id", lambda: "b00t-1")
    theirs = fleet_service.spawn(plain_project, "manager").agent
    ours = _coder(project)
    with store_session() as store:
        store.forget_project(plain_project.id)
    rows = [theirs, ours]
    onboard = project_service.onboard_command(plain_project.root)
    forgotten = f"{plain_project.root.name} is forgotten: {onboard} adds it back first"
    named = f"manager ({plain_project.root.name}), coder-1 ({project.root.name})"

    note = fleet_service._exited_note(tmux, rows)
    tmux.running, tmux.socket_file = False, False  # a server that lost its socket file
    with pytest.raises(FleetError, match="lost its socket file") as refused:
        fleet_service.spawn(project, "coder")
    said = str(refused.value)

    for message in (note, said):
        assert named in message and forgotten in message, message
        assert message.count("is forgotten") == 1, message


def test_a_check_at_a_path_with_no_file_reads_the_fleets_socket_once(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 6, 7/10: `_refuse_a_freed_socket` read the fleet's socket again
    right after `_before_a_server_starts` had read "gone" there, two reads a check where a
    listening server costs one. The refusal stands on the one read. Round 7, 6/9: so do the
    rows recorded at that path, which `_live_rows_here` asked again, in a `SocketReads` of
    its own, exactly where it matters: a path with no file, and rows recorded there."""
    monkeypatch.setattr(tmux_core, "boot_id", lambda: "b00t-1")
    coder = _coder(project)
    tmux.running, tmux.socket_file = False, False  # a server that lost its socket file
    asked: list[Path | None] = []
    listener = tmux.listener

    def counted(recorded: Path | None = None) -> SocketAnswer | None:
        asked.append(recorded)
        return listener(recorded)

    monkeypatch.setattr(tmux, "listener", counted)
    with pytest.raises(FleetError, match="lost its socket file"):
        fleet_service._before_a_server_starts(tmux, fleet_service.settings())

    assert coder.socket_path is not None, "the row recorded where its server listened"
    assert asked == [tmux.socket_path()], asked


def test_a_check_before_a_server_starts_reads_the_socket_once_and_a_spawn_says_both_checks(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 5: `_forget_an_exited_server` and `_refuse_a_freed_socket` each
    read the fleet's socket, so a check made two connects, and a spawn, which checks before
    it captures the project or makes a worktree and again as its window opens, made four.
    One read says which of the two applies. And the rows the window's check forgot, a
    server having exited while the spawn checked its worktree out, went unsaid: the spawn
    threw that check's answer away."""
    monkeypatch.setattr(tmux_core, "boot_id", lambda: "b00t-1")
    tmux.started = datetime(2026, 10, 13, 9, 0, 7, tzinfo=UTC)
    asked: list[Path | None] = []
    listener = tmux.listener

    def counted(recorded: Path | None = None) -> SocketAnswer | None:
        asked.append(recorded)
        return listener(recorded)

    monkeypatch.setattr(tmux, "listener", counted)
    manager = fleet_service.spawn(project, "manager").agent

    assert len(asked) == 2, asked  # a server listens: one read per check
    checks = fleet_service._before_a_server_starts
    early = [True]

    def then_the_server_exits(srv: TmuxServer, config: FleetSettings) -> list[FleetAgent]:
        exited = checks(srv, config)
        if early:  # the spawn's own check, before its worktree
            early.clear()
            tmux.kill_server()  # the file stays, nothing listens
        return exited

    monkeypatch.setattr(fleet_service, "_before_a_server_starts", then_the_server_exits)
    receipt = fleet_service.spawn(project, "coder", worktree=False)

    [said] = [note for note in receipt.notes if "had exited" in note]
    assert f"manager ({project.root.name}) reads lost from now on" in said, said
    with store_session() as store:
        forgotten = store.get_fleet_agent(manager.id)
    assert forgotten is not None and forgotten.server_started_at is None


def test_the_sign_in_window_asks_its_check_with_the_config_its_server_was_made_for(
    tmux: FakeTmux, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 5: the Accounts sign-in window passed no `config`, so the check
    before a server starts read `config.toml` again on Textual's thread, and asked the rows
    of today's `[fleet] tmux_socket` while the page probed the server it had kept from its
    first sign-in. The page reads `[fleet]` once and passes it with the server it made."""
    from aisquare.core import claude_accounts as claude_accounts_core
    from aisquare.services import claude_accounts as claude_accounts_service

    config = fleet_service.settings()
    monkeypatch.setattr(fleet_service, "settings", lambda: pytest.fail("[fleet] read again"))
    account = claude_accounts_core.default_account()

    window = claude_accounts_service.open_sign_in_window(account, tmux, config=config)

    assert window.pane_id in tmux.facts


def test_a_server_named_by_its_socket_is_had_without_reading_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #259, round 9, 7/8: ``server_for`` read ``config.toml`` for a name it was
    given, the socket being all ``server`` reads of it. A row with none still takes today's."""
    sockets: list[str] = []

    def server(config: FleetSettings | None = None) -> TmuxServer:
        assert config is not None
        sockets.append(config.tmux_socket)
        return TmuxServer(config.tmux_socket)

    monkeypatch.setattr(fleet_service, "server", server)
    monkeypatch.setattr(fleet_service, "settings", lambda: pytest.fail("config.toml read"))
    assert fleet_service.server_for("asq-old").socket == "asq-old"
    monkeypatch.setattr(fleet_service, "settings", lambda: FleetSettings(tmux_socket="asq-new"))
    assert fleet_service.server_for("").socket == "asq-new"
    assert sockets == ["asq-old", "asq-new"]


def _callers_of(name: str) -> set[tuple[str, str]]:
    """``(module under src/aisquare, function)`` for each function whose body calls ``.name(…)``
    or holds the string ``name`` as a whole constant (a tmux command word)."""
    root = Path(fleet_service.__file__).resolve().parents[1]
    found: set[tuple[str, str]] = set()
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(function):
                called = (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == name
                )
                if called or (isinstance(node, ast.Constant) and node.value == name):
                    found.add((path.relative_to(root).as_posix(), function.name))
    return found


@pytest.mark.parametrize("way_in", ["spawn", "restart", "switch", "sign-in"])
def test_every_window_at_the_fleet_socket_asks_what_must_hold_before_a_server_starts(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    plain_project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    way_in: str,
) -> None:
    """Review of #259, round 3: rounds 1 to 3 each found another path that starts the fleet's
    tmux server past the rules for doing so. Only `spawn` forgot the start of a server that
    exited (a stale socket file, after which the next server holds its rows for good) and
    refused a socket path a running server lost (no file, where a second server strands
    its agents). The Accounts sign-in window, on the same server, started one past both.
    Every window there now opens through `fleet.open_window`, which asks both: the source
    has no other caller of `spawn_window`, and each way in is asked here."""
    from aisquare.core import claude_accounts as claude_accounts_core
    from aisquare.services import claude_accounts as claude_accounts_service

    assert _callers_of("spawn_window") == {("services/fleet.py", "open_window")}
    assert _callers_of("new-session") == {("core/tmux.py", "spawn_window")}, "no other start"
    _two_slots_with_usage(monkeypatch, work=95, personal=10)
    monkeypatch.setattr(tmux_core, "boot_id", lambda: "b00t-1")
    tmux.started = datetime(2026, 10, 13, 9, 0, 7, tzinfo=UTC)
    manager = fleet_service.spawn(project, "manager").agent  # on this shell's socket path
    # The agent a restart or a switch replaces runs on a server at another path (another
    # `TMUX_TMPDIR`), so its own stop goes through whatever this socket's state.
    apart = Path("/elsewhere/tmux-1000/asq-test")
    elsewhere = tmux.per_path[apart] = FakeTmux()
    elsewhere.started = tmux.started
    window = elsewhere.spawn_window("asq-x", name="coder-1", cwd=project.root, command=["c"])
    elsewhere.set_command(window.pane_id, "claude")
    moved = {"socket_path": apart, "pane_id": window.pane_id, "server_started_at": None}
    spawned = fleet_service.spawn(project, "coder", worktree=False, account="2").agent
    with store_session() as store:
        agent = store.upsert_fleet_agent(spawned.model_copy(update=moved))
    _with_transcript(agent, None)

    def enter() -> None:
        if way_in == "spawn":
            fleet_service.spawn(plain_project, "manager")  # any project: the server is shared
        elif way_in == "sign-in":
            account = claude_accounts_core.default_account()
            claude_accounts_service.open_sign_in_window(account, fleet_service.server())
        else:
            getattr(fleet_service, way_in)(project, agent.label)

    tmux.running = False  # no tmux client reaches the manager's server…
    tmux.socket_file = False  # …because its socket file is gone: it may run on
    started = len(tmux.spawned)

    with pytest.raises(FleetError, match=r"not started: the tmux server that manager .* lost"):
        enter()

    assert len(tmux.spawned) == started and not tmux.running, "no second server at the path"
    # A server that exited leaves its socket file, where nothing listens: the next server
    # starts there, and the manager's row reads lost under it, as in 0.8.0, not held.
    tmux.socket_file = True
    enter()

    assert tmux.running and len(tmux.spawned) == started + 1
    [status] = [s for s in fleet_service.list_agents(project) if s.agent.id == manager.id]
    assert (status.state, status.held) == ("lost", False), status
    with store_session() as store:
        forgotten = store.get_fleet_agent(manager.id)
    assert forgotten is not None and forgotten.server_started_at is None


def test_a_row_whose_socket_another_server_took_is_held_not_ended(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Follow-up of #257: a server that lost its socket file runs on, and the next `tmux -L`
    there (a 0.8.0 client, a spawn into a project with no rows, a hand-run tmux) starts a
    second server at the freed path. That server answered for the old rows from then on:
    their panes read as gone, their socket answered, and `stop`, `shutdown` and `restart`
    ended them under their running agents, and `reap` marked them lost. Every row records
    when its server started, and a server at its socket that started at another time is
    not its own: the listing holds the row as unknown, saying why; `reap` leaves it, with
    `--server-down` too; `stop` refuses with the way out; `shutdown` leaves it live until
    `--forget`; and nothing of the newcomer's is typed into or killed."""
    monkeypatch.setattr(tmux_core, "boot_id", lambda: "b00t-1")
    tmux.started = datetime(2026, 10, 13, 9, 0, 7, tzinfo=UTC)
    coder = _coder(project)
    newcomer = FakeTmux()  # at the same path, so `-L` reaches it: the row's server is lost
    newcomer.started = tmux.started + timedelta(minutes=5)
    # Another project's agent there took its id. (This project's own session there would
    # be the shutdown's to take down: the held row spares none on the newcomer.)
    newcomer.spawn_window("asq-elsewhere", name="coder-1", cwd=project.root, command=["claude"])
    assert newcomer.facts.keys() == {coder.pane_id}, "its %1 is another agent's"
    newcomer.set_command(coder.pane_id, "claude")
    monkeypatch.setattr(fleet_service, "server", lambda config=None: newcomer)

    [listed] = fleet_service.list_agents(project)
    assert (listed.state, listed.detail) == ("unknown", "another tmux server on its socket")
    status = fleet_service.status_of(coder)
    assert (status.state, status.detail) == ("unknown", "another tmux server on its socket")
    assert listed.held and status.held, "said, so no view shows the newcomer's %1 under it"
    _board_session(coder, "waiting")  # its hooks may still fire: the board does not decide
    [fresh] = fleet_service.list_agents(project)
    assert (fresh.state, fresh.detail) == ("unknown", "another tmux server on its socket")
    assert fresh.held, "held whatever the board says"
    reaped = fleet_service.reap(project)
    swept = fleet_service.reap(project, server_down=True)
    assert reaped.lost == reaped.ended == swept.lost == swept.ended == []
    # Left live, and said so, with the way out (review of #259, round 1): unnamed, `reap`
    # printed `0 ended, 0 lost` while the row still counted toward the cap and stop refused.
    for said in (reaped, swept):
        [named] = said.held
        assert named.agent.id == coder.id, said
        assert named.reason.startswith("another tmux server answers on its socket "), named
        assert "— left live: if it is, send that server SIGUSR1" in named.reason
        forget = _fleet("shutdown", project, "--forget")
        assert f"`{forget}` ends its row on your word" in named.reason
    with pytest.raises(FleetError, match="another tmux server answers on its socket") as stop:
        fleet_service.stop(project, "coder-1")
    assert "SIGUSR1" in str(stop.value)
    assert f"shutdown --project {project.id} --forget`" in str(stop.value)
    with pytest.raises(FleetError, match="another tmux server"):
        fleet_service.restart(project, "coder-1")
    plan = fleet_service.shutdown_plan(project)
    report = fleet_service.shutdown(project)

    assert "another tmux server answers on its socket" in plan.held[coder.id]
    assert report.stopped == [] and report.recorded == []
    assert [row.agent.id for row in report.failed] == [coder.id]
    assert "another tmux server" in report.failed[0].reason
    assert "--forget ends its row on your word" in report.failed[0].reason
    assert report.incomplete_projects == [project.id]
    assert newcomer.typed == [] and newcomer.killed == [] and newcomer.killed_sessions == []
    with store_session() as store:
        assert [a.id for a in store.fleet_agents(project.id, live_only=True)] == [coder.id]
    assert _events(project, "agent_exited") == []

    forgotten = fleet_service.shutdown(project, forget=True)

    assert [row.agent.id for row in forgotten.recorded] == [coder.id]
    assert forgotten.recorded[0].reason.startswith(
        "ended on your word (--forget): another tmux server answers on its socket"
    )
    assert newcomer.typed == [] and newcomer.killed == [], "still nothing of the newcomer's"
    # Negative control: a row from another boot cannot be running on any server, so under
    # the newcomer it is lost, and a stop ends it, touching nothing of the newcomer's.
    stale = _coder(project)  # spawned on the newcomer, as the config now reaches it
    with store_session() as store:
        stale = store.upsert_fleet_agent(stale.model_copy(update={"boot_id": "b00t-0"}))
    newcomer.started = newcomer.started + timedelta(minutes=1)  # the newcomer restarted
    [listed] = fleet_service.list_agents(project)
    assert (listed.agent.id, listed.state, listed.detail) == (stale.id, "lost", "pane gone")
    assert not listed.held
    assert fleet_service.stop(project, stale.label).agent.ended_at is not None
    assert newcomer.killed == [] and newcomer.typed == []


def test_a_held_row_is_held_in_its_servers_view_not_absent_from_it(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 3: in what `_observe` returns, `None` under a row's id meant
    held, and a missing id meant its pane is gone, so a reader that did `.get(agent.id)`
    without `_own_view` took a held row for a gone one; `reap` was safe only because
    `_view` screened the row first. A held row is `HELD`, which no `.get()` gives for a
    gone pane, and the one accessor that reads a pane refuses it."""
    monkeypatch.setattr(tmux_core, "boot_id", lambda: "b00t-1")
    tmux.started = datetime(2026, 10, 13, 9, 0, 7, tzinfo=UTC)
    coder = _coder(project)
    session = f"asq-{_codename(project)}"
    newcomer = FakeTmux()  # took the socket, and gave %1 to another agent
    newcomer.started = tmux.started + timedelta(minutes=5)
    newcomer.spawn_window(session, name="other", cwd=project.root, command=["claude"])
    monkeypatch.setattr(fleet_service, "server", lambda config=None: newcomer)

    views = fleet_service._observe_sockets([coder], session)

    [observed] = views.values()
    assert observed is not None and observed.get(coder.id) is fleet_service.HELD
    assert fleet_service._own_view(coder, observed) is None
    assert fleet_service._held_by_another(coder, observed)
    with pytest.raises(FleetError, match="held as another server's"):
        fleet_service._pane_in(coder, observed)
    # The readers of the raw map claim nothing for it, though its id is a pane that died.
    newcomer.die(coder.pane_id, 0)
    dead = fleet_service._observe_sockets([coder], session)
    assert fleet_service._lingering_windows([coder], dead) == {}
    assert fleet_service._end_dead_rows([coder], dead) == []
    with store_session() as store:
        assert [a.id for a in store.fleet_agents(project.id, live_only=True)] == [coder.id]


def test_a_stop_and_a_restart_read_the_servers_start_in_the_answer_that_names_the_pane(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 3: `_stop_row`'s look at the window asked `srv.started_at()` in
    a `display-message` of its own each time the pane answered, on every poll of a stop's
    grace period, and `_pane_alive` (whether a restart hands over) did the same after
    `pane_facts`, though tmux says when its server started in the answer that names the
    pane (`#{start_time}`: `PaneFacts.server_started`, and now `WindowInfo.server_started`).
    Both read it there, and still judge a pane on another server the same way."""
    monkeypatch.setattr(tmux_core, "boot_id", lambda: "b00t-1")
    tmux.started = datetime(2026, 10, 13, 9, 0, 7, tzinfo=UTC)
    coder = _coder(project)
    tmux.set_command(coder.pane_id, "claude")
    asked: list[datetime | None] = []
    started_at = tmux.started_at

    def counted() -> datetime | None:
        asked.append(started_at())
        return asked[-1]

    monkeypatch.setattr(tmux, "started_at", counted)

    assert fleet_service._pane_alive(coder) is True
    tmux.honours_exit = False  # it lets its grace run out: every poll looks at the window
    receipt = fleet_service._stop_row(project, coder, grace=2.0)

    assert receipt.agent.ended_at is not None and tmux.killed == [coder.pane_id]
    assert asked == [], "not one display-message for the start"
    # The start read there still decides: on a server that took the socket, a pane under
    # the row's id is another agent's, not the row's alive.
    other = _coder(project)
    newcomer = FakeTmux()
    newcomer.started = tmux.started + timedelta(minutes=5)
    newcomer.spawn_window("asq-elsewhere", name="x", cwd=project.root, command=["claude"])
    newcomer.spawn_window("asq-elsewhere", name="y", cwd=project.root, command=["claude"])
    assert other.pane_id in newcomer.facts
    monkeypatch.setattr(fleet_service, "server", lambda config=None: newcomer)
    assert fleet_service._pane_alive(other) is False


def test_a_look_at_the_fleet_reads_the_servers_start_in_the_answers_that_named_its_panes(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 4: `_observe` asked `srv.started_at()` in a `display-message` of
    its own though the window list and a pane's facts it had just read carry the start
    (`#{start_time}`): one more tmux process per server on asq's 2 s tick, every needs-you
    scan, every `status_of` and `tell`. It reads the start there, and asks only when no
    answer carried it (live rows, no pane seen), as it must to tell a lost row from a held
    one."""
    monkeypatch.setattr(tmux_core, "boot_id", lambda: "b00t-1")
    tmux.started = datetime(2026, 10, 13, 9, 0, 7, tzinfo=UTC)
    coder = _coder(project)
    tmux.set_command(coder.pane_id, "claude")
    session = f"asq-{_codename(project)}"
    asked: list[datetime | None] = []
    started_at = tmux.started_at

    def counted() -> datetime | None:
        asked.append(started_at())
        return asked[-1]

    monkeypatch.setattr(tmux, "started_at", counted)

    assert coder.id in (fleet_service._observe(tmux, session, [coder]) or {}), "the window list"
    # A session renamed under it: the pane answers to `pane_facts` alone.
    assert coder.id in (fleet_service._observe(tmux, "asq-renamed", [coder]) or {}), "facts"
    assert fleet_service.status_of(coder).state != "lost"
    assert asked == [], "not one display-message for the start"
    # On a server that took the socket, a start read there still holds the row's pane.
    tmux.started += timedelta(minutes=5)
    assert fleet_service._observe(tmux, session, [coder]) == {coder.id: fleet_service.HELD}
    assert asked == []
    # Control: a live row and no pane seen, so no answer carried the start: asked, once.
    tmux.facts.pop(coder.pane_id)
    tmux.sessions[session] = []
    tmux.started -= timedelta(minutes=5)
    assert fleet_service._observe(tmux, session, [coder]) == {}
    assert asked == [tmux.started]


def _planned_ends(plan: fleet_service.ShutdownPlan) -> dict[str, str]:
    """What a shutdown plan says it does with each row."""
    return {
        agent.id: "live" if agent.id in plan.held else "lost" if agent.id in plan.lost else "stop"
        for agent in plan.agents
    }


def _ends(report: fleet_service.ShutdownReport) -> dict[str, str]:
    """What a shutdown did with each row."""
    return {
        **{agent.id: "stop" for agent in report.stopped},
        **{row.agent.id: "lost" for row in report.recorded},
        **{row.agent.id: "live" for row in report.failed},
    }


def test_a_forget_plan_says_a_row_another_server_answers_for_is_recorded_lost_as_its_run_does(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    """Review of #259, round 3: under `--forget` the plan's filter dropped a row held as
    another server's from `held`, and its `lost` covered only sockets that did not answer,
    so `fleet shutdown --forget` printed `💤 coder-1 · … — stopped (%1)`, the other
    server's pane id, and `--json`'s `lost` left it out, while the run recorded it lost
    without stopping anything. Round 29 of #257 fixed the same mismatch for silent
    sockets. The plan and the run now read one verdict per row, with or without the word."""
    monkeypatch.setattr(tmux_core, "boot_id", lambda: "b00t-1")
    tmux.started = datetime(2026, 10, 13, 9, 0, 7, tzinfo=UTC)
    held = _coder(project)
    newcomer = FakeTmux()  # took the socket: its %1 is another agent's
    newcomer.started = tmux.started + timedelta(minutes=5)
    newcomer.spawn_window("asq-elsewhere", name="coder-9", cwd=project.root, command=["c"])
    monkeypatch.setattr(fleet_service, "server", lambda config=None: newcomer)

    for forget in (False, True):
        own = _coder(project)  # on the newcomer, its own server: stopped either way
        plan = fleet_service.shutdown_plan(project, forget=forget)
        report = fleet_service.shutdown(project, forget=forget)
        assert _planned_ends(plan) == _ends(report), (forget, plan, report)
        assert _ends(report) == {held.id: "lost" if forget else "live", own.id: "stop"}

    assert plan.lost == {held.id: str(fleet_service._address(held))}
    [recorded] = report.recorded
    assert plan.lost_why[held.id] == recorded.reason
    assert recorded.reason.startswith("ended on your word (--forget): another tmux server")
    # And the operator reads it so before confirming, in both shapes.
    again = _coder(project, label="coder-1")  # a row held as before, once more
    with store_session() as store:
        store.upsert_fleet_agent(again.model_copy(update={"server_started_at": tmux.started}))
    human = runner.invoke(app, ["fleet", "shutdown", "--forget", "-P", project.id])
    machine = runner.invoke(app, ["--json", "fleet", "shutdown", "--forget", "-P", project.id])
    assert human.exit_code == 0, human.output
    said = " ".join(human.stdout.split())
    assert "coder-1 · " in said and " — recorded lost: ended on your word (--forget): " in said
    assert "stopped (" not in said, said
    payload = json.loads(machine.stdout)
    assert payload["lost"] == {again.id: str(fleet_service._address(again))}
    assert payload["lost_why"][again.id].startswith("ended on your word (--forget): another")


def test_a_row_left_live_as_held_spares_no_session_on_the_server_that_took_its_socket(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 3: a row held as another server's lands in the report's
    `failed`, and the kill phase spared every session of a `failed` row's project at its
    address: the socket where the newer server answers. So `fleet shutdown` left the
    project's `asq-<codename>` session on that server up, listed in `sessions_left_up`,
    with every window in it and the server itself, though the held agent does not run
    there: the spare is for a pane `_verify_gone` saw alive. A held row spares nothing."""
    monkeypatch.setattr(tmux_core, "boot_id", lambda: "b00t-1")
    tmux.started = datetime(2026, 10, 13, 9, 0, 7, tzinfo=UTC)
    held = _coder(project)
    session = f"asq-{_codename(project)}"
    newcomer = FakeTmux()  # took the socket
    newcomer.started = tmux.started + timedelta(minutes=5)
    monkeypatch.setattr(fleet_service, "server", lambda config=None: newcomer)
    own = _coder(project)  # this project's, on the newcomer: in its session there
    left = newcomer.spawn_window(session, name="scratch", cwd=project.root, command=["sh"])

    report = fleet_service.shutdown(project)

    assert [row.agent.id for row in report.failed] == [held.id] and report.failed[0].held
    assert [agent.id for agent in report.stopped] == [own.id]
    assert report.sessions_killed == [f"asq:{session}"] and report.sessions_left_up == []
    assert left.pane_id not in newcomer.facts and session not in newcomer.sessions
    assert report.incomplete_projects == [project.id], "the held row is still live"
    # Negative control: a row whose pane its own server shows alive spares its session.
    kept = _coder(project)
    newcomer.honours_exit, newcomer.refuse_kills = False, True  # alive, and stays so
    again = fleet_service.shutdown(project)
    assert [row.agent.id for row in again.failed if not row.held] == [kept.id]
    assert again.sessions_left_up == [f"asq:{session}"]


@pytest.mark.parametrize(("answer", "spared"), [("answers", True), ("another", False)])
def test_a_row_held_as_its_socket_answering_now_spares_its_session_from_a_late_take_down(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, answer: SocketAnswer, spared: bool
) -> None:
    """Review of #259, round 6, 3/10: a row whose socket was silent at the probe and answers
    now (``"answers"``) is left live, held, and `_spared` dropped every held row. Its pane
    may be on the server that came up, so a late row's take-down on that socket killed the
    session of an agent the report said it left live. Only a row out of every client's
    reach (``"another"``, a silent socket) spares nothing."""
    config = fleet_service.settings()
    held = _coder(project)
    late = _coder(project)  # spawned during the run, exited on its own since
    tmux.die(late.pane_id, 0)
    unreached: dict[str, tuple[SocketAnswer, str, str]] = {held.id: (answer, "why", "do")}
    report = fleet_service.ShutdownReport()
    with store_session() as store:
        named = store.get_project(project.id)  # with the codename its spawn gave it
    assert named is not None

    fleet_service._shutdown_row(
        named, held, {}, report, force=False, grace=0, held=unreached, config=config
    )
    fleet_service._retire_late_panes([(named, late)], report, config)

    assert [row.agent.id for row in report.failed] == [held.id]
    session = f"asq-{_codename(project)}"
    assert (session in tmux.sessions) is spared
    assert (report.sessions_left_up == [_session_of(project)]) is spared


@pytest.mark.parametrize("board", ["waiting", "working", "attention"])
def test_a_held_row_reads_unknown_saying_why_whatever_its_fresh_board_says(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    board: str,
) -> None:
    """Review of #259, round 2: the held reason reached a row only once its board went stale.
    While the board was fresh (30 minutes: a killed agent fires no SessionEnd) the row read
    its board's state with no detail, so `fleet ls`, asq's header and the phone showed a
    healthy `waiting` that no tab gave a pane and every action refused, `tell` typed into
    the agent that got its id, and a held manager counted as one taking its coders'
    questions. Held, it reads `unknown`, saying why, whatever its board says."""
    monkeypatch.setattr(tmux_core, "boot_id", lambda: "b00t-1")
    tmux.started = datetime(2026, 10, 13, 9, 0, 7, tzinfo=UTC)
    coder = _coder(project)
    _board_session(coder, board)
    newcomer = FakeTmux()
    newcomer.started = tmux.started + timedelta(minutes=5)
    newcomer.spawn_window("asq-elsewhere", name="manager", cwd=project.root, command=["claude"])
    newcomer.set_command(coder.pane_id, "claude")
    monkeypatch.setattr(fleet_service, "server", lambda config=None: newcomer)

    [listed] = fleet_service.list_agents(project)
    status = fleet_service.status_of(coder)

    for said in (listed, status):
        assert (said.state, said.detail, said.held) == (
            "unknown",
            "another tmux server on its socket",
            True,
        ), said
    told = fleet_service.tell(project, "coder-1", "please rebase")
    assert not told.delivered and told.how.startswith("it is unknown — filed as board note")
    assert newcomer.typed == []
    # Negative control: on its own server the same fresh board decides, as for any row.
    monkeypatch.setattr(fleet_service, "server", lambda config=None: tmux)
    [own] = fleet_service.list_agents(project)
    assert (own.state, own.held) == (board, False)


@pytest.mark.parametrize("shape", ["another", "gone"])
def test_every_path_that_decides_a_held_rows_fate_gives_the_one_verdict(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
) -> None:
    """Reviews of #259, rounds 1 to 3: each found another path that decided a held row's
    fate its own way: a shutdown plan that said `stopped` where its run recorded the row
    lost, a kill phase that spared a session for it on another server, an uninstall that
    advised `SIGUSR1` while another server listened at its socket. Every path that ends a
    row, keeps it, or says what to do with it asks the one rule (`server_may_run`) and says
    the one way back (`_unreachable_why`): for a row another server answers for (`another`)
    and for one whose server lost its socket file (`gone`), this asks each in turn, so the
    next path is one more line. Only `--forget` ends it, plan and run alike, and every
    `SIGUSR1` said for a socket another server answers on waits for it to be free."""
    from aisquare.services import diagnostics

    monkeypatch.setattr(tmux_core, "boot_id", lambda: "b00t-1")
    # Uninstall asks whether `fleet shutdown` could reach the fleet: here it can, as it
    # does through the fake, whether or not this machine has a usable tmux (Windows: none).
    monkeypatch.setattr(TmuxServer, "require", lambda _self: None)
    tmux.started = datetime(2026, 10, 13, 9, 0, 7, tzinfo=UTC)
    coder = _coder(project)
    if shape == "another":
        answering = FakeTmux()  # took the socket; its %1 is another agent's
        answering.started = tmux.started + timedelta(minutes=5)
        answering.spawn_window("asq-elsewhere", name="x", cwd=project.root, command=["c"])
        monkeypatch.setattr(fleet_service, "server", lambda config=None: answering)
        monkeypatch.setattr(TmuxServer, "listener", lambda _self, _recorded=None: "answers")
        monkeypatch.setattr(TmuxServer, "started_at", lambda _self: answering.started)
        doctor_sees: TmuxServer = answering
    else:
        tmux.running = False  # no tmux client reaches its server…
        tmux.socket_file = False  # …as its socket file is gone: it may run on
        monkeypatch.setattr(tmux, "server_absent", lambda: True)  # as tmux says for that
        monkeypatch.setattr(TmuxServer, "listener", lambda _self, _recorded=None: "gone")
        doctor_sees = tmux
    said: list[str] = []

    swept = fleet_service.reap(project, server_down=True)
    plain = fleet_service.reap(project)  # a server that does not answer is not asked to say
    for reaped in (swept, plain) if shape == "another" else (swept,):
        [held] = reaped.held
        assert held.agent.id == coder.id
        said.append(held.reason)
    assert swept.lost == swept.ended == plain.lost == plain.ended == []
    for verb in (fleet_service.stop, fleet_service.restart):
        with pytest.raises(FleetError) as refused:
            verb(project, "coder-1")
        said.append(str(refused.value))
    plan = fleet_service.shutdown_plan(project)
    report = fleet_service.shutdown(project)
    assert _planned_ends(plan) == _ends(report) == {coder.id: "live"}, (plan, report)
    said += [plan.held[coder.id], report.failed[0].reason]
    assert lifecycle.running_fleet()[0] != (), "uninstall counts it as running"
    refusal = lifecycle.uninstall_plan().refusal
    assert refusal is not None and refusal.error == "fleet_running"
    said.append(str(refusal))
    check = diagnostics._check_fleet(lambda socket: doctor_sees)
    assert check.status is CheckStatus.warn and "may still be running" in check.detail
    said.append(check.fix or "")
    with store_session() as store:
        assert [a.id for a in store.fleet_agents(project.id, live_only=True)] == [coder.id]
    assert all("SIGUSR1" in words for words in said), said
    if shape == "another":
        # tmux's handler takes the path from whatever listens on it: only once it is free.
        for words in said:
            assert re.search(r"SIGUSR1 (\(once|once)", words), words

    forget = fleet_service.shutdown_plan(project, forget=True)
    forgotten = fleet_service.shutdown(project, forget=True)

    assert _planned_ends(forget) == _ends(forgotten) == {coder.id: "lost"}
    assert forget.lost_why[coder.id] == forgotten.recorded[0].reason
    assert lifecycle.running_fleet()[0] == ()


@pytest.mark.parametrize("said", ["unasked", "gone"])
def test_a_row_on_a_socket_tmux_reached_is_held_only_as_another_servers(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    said: str,
) -> None:
    """Review of #259, round 5: on a socket tmux had just reached, a row that recorded its
    server's start was held for any answer of its socket but `answers`: a connect that timed
    out on a busy server (`unasked`), or a file removed since (`gone`). Held, it spared no
    session, so the kill phase killed its agent with the session while the report said
    `left live`, and `--forget` recorded lost a row whose socket tmux answered on. Only
    `another` holds it there; any other answer stops it as any row, plan and run alike."""
    monkeypatch.setattr(tmux_core, "boot_id", lambda: "b00t-1")
    tmux.started = datetime(2026, 10, 13, 9, 0, 7, tzinfo=UTC)
    monkeypatch.setattr(tmux, "listener", lambda recorded=None: said)

    for forget in (False, True):
        coder = _coder(project)
        assert coder.server_started_at == tmux.started, "asked whether it is another's"
        plan = fleet_service.shutdown_plan(project, forget=forget)
        report = fleet_service.shutdown(project, forget=forget)

        assert _planned_ends(plan) == _ends(report) == {coder.id: "stop"}, (forget, plan, report)
        assert [agent.exit_status for agent in report.stopped] == [0], "its own /exit"


def test_every_path_that_types_into_or_shows_a_pane_refuses_a_held_rows_id(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reviews of #259, rounds 1 and 2: each found another path that acted on a row held as
    another tmux server's as if its pane id were its own: asq's tabs, needs-you's server,
    `tell`, needs-you's cards, the status detail. On the server that took the socket those
    ids are other agents', here each at its prompt. Every path that types into a pane or
    shows one goes through the one derivation (`_derive`, held: `unknown`) or the one pane
    predicate (`_its_agent`); this asks each of them, so the next path is one more line."""
    from aisquare.cli.ui.views.agent import shown_pane
    from aisquare.services import remote_needs, remote_server

    monkeypatch.setattr(tmux_core, "boot_id", lambda: "b00t-1")
    tmux.started = datetime(2026, 10, 13, 9, 0, 7, tzinfo=UTC)
    manager = fleet_service.spawn(project, "manager").agent
    coder = _coder(project)
    for row in (manager, coder):
        _board_session(row, "waiting")  # fresh: a killed agent fires no SessionEnd
    newcomer = FakeTmux()  # at the same path, so `-L` reaches it
    newcomer.started = tmux.started + timedelta(minutes=5)
    for name in ("manager", "coder-1"):
        newcomer.spawn_window("asq-elsewhere", name=name, cwd=project.root, command=["claude"])
    assert newcomer.facts.keys() == {manager.pane_id, coder.pane_id}, "their ids, others' panes"
    for row in (manager, coder):
        newcomer.set_command(row.pane_id, "claude")  # agents at their prompts: not theirs

    def capture(pane_id: str, **_: object) -> Capture:
        facts = newcomer.pane_facts(pane_id)
        assert facts is not None
        return Capture(["another agent's screen"], facts, 0)

    monkeypatch.setattr(newcomer, "capture", capture)
    monkeypatch.setattr(remote_server, "_resolve_project", lambda ref=None: project)
    monkeypatch.setattr(fleet_service, "server", lambda config=None: newcomer)
    now = datetime.now(tz=UTC)

    # The listing, status_of and so `fleet ls`, asq and the phone's api/fleet.
    statuses = {status.agent.id: status for status in fleet_service.list_agents(project)}
    statuses[coder.id] = fleet_service.status_of(coder)
    for status in statuses.values():
        assert (status.state, status.detail, status.held) == (
            "unknown",
            fleet_service.ANOTHER_SERVER,
            True,
        ), status
        assert shown_pane(status) is None, "asq's Agent and Manager tabs"
    status = statuses[coder.id]
    assert not fleet_service.tell(project, "coder-1", "please rebase").delivered
    assert fleet_service.nudge_manager(project.id, reason="coder-1 exited") is False
    assert remote_server._remote_pane_refusal(newcomer, coder) == remote_server.PANE_OUTLIVED
    snap = remote_needs._needs_snapshot(project, status, None, (), now)
    assert snap.pane_is_agent is False, "what quick answers and agent actions type on"
    [card] = remote_needs.needs_from_agent(status, None, project=project, events=(), now=now)
    assert (card.kind, card.reason) == (
        "lost",
        "coder-1 is out of reach: another tmux server answers on its socket",
    )
    with pytest.raises(remote_server.RequestError) as shown:
        remote_server._live_panes("coder-1")
    assert (shown.value.status, shown.value.error) == (409, "not_agent"), "the phone's pane"
    assert remote_server._pane_width(coder) == 80, "not the other agent's width"
    with pytest.raises(FleetError, match="another tmux server answers on its socket"):
        fleet_service.stop(project, "coder-1")

    assert newcomer.typed == [] and newcomer.killed == []
    # Negative control: on their own server every path takes the same panes as theirs.
    monkeypatch.setattr(fleet_service, "server", lambda config=None: tmux)
    for row in (manager, coder):
        tmux.set_command(row.pane_id, "claude")
    own = fleet_service.status_of(coder)
    assert (own.state, own.held, shown_pane(own)) == ("waiting", False, coder.pane_id)
    assert remote_server._remote_pane_refusal(tmux, coder) is None
    assert remote_needs._needs_snapshot(project, own, None, (), now).pane_is_agent is True
    assert fleet_service.nudge_manager(project.id, reason="coder-1 exited") is True
    assert fleet_service.tell(project, "coder-1", "please rebase").delivered
    assert newcomer.typed == [], "and still nothing of the newcomer's"


def test_a_listing_asks_for_the_boot_only_for_rows_that_may_be_held_and_once_per_read(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 2: `boot_id()` keeps only an id it found (round 28 of #257), so
    where `sysctl` fails each question is another `sysctl`, with a 5 s timeout. `_observe`
    asked it of every read with live rows (asq's 2 s tick, needs-you's 3 s scan, every
    `status_of`), though only a row another server may hold needs it, and asked
    `boot_started()` once more per such row. Now neither for rows on their own server, and
    each once per read however many rows need it."""
    asked = {"id": 0, "started": 0}

    def boot_id() -> str | None:
        asked["id"] += 1
        return None  # a macOS whose `sysctl` fails: kept by nothing, asked again each time

    def boot_started() -> datetime | None:
        asked["started"] += 1
        return None

    monkeypatch.setattr(tmux_core, "boot_id", boot_id)
    monkeypatch.setattr(tmux_core, "boot_started", boot_started)
    tmux.started = datetime(2026, 10, 13, 9, 0, 7, tzinfo=UTC)
    coders = [_coder(project) for _ in range(3)]
    asked.update(id=0, started=0)  # what the spawns recorded

    assert {status.state for status in fleet_service.list_agents(project)} == {"waiting"}
    fleet_service.status_of(coders[0])
    assert asked == {"id": 0, "started": 0}, "no row on its own server needs the boot"

    newcomer = FakeTmux()  # three rows another server at their socket may hold
    newcomer.started = tmux.started + timedelta(minutes=5)
    monkeypatch.setattr(fleet_service, "server", lambda config=None: newcomer)
    assert all(status.held for status in fleet_service.list_agents(project))
    assert asked == {"id": 1, "started": 1}, "once each for the read, not once per row"
    # Review of #259, round 6, 9/10: one cache of the boot per read, the one the rule asks
    # (`SocketReads`), so a sweep's listing and its rule share the answer.
    asked.update(id=0, started=0)
    fleet_service.reap(project, server_down=True)
    assert asked["id"] == 1, asked


def test_the_one_rule_asks_each_server_once_per_read_not_once_per_row(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 3: `server_may_run` asked the server answering at a row's socket
    when it started, a `display-message` each time, and connected to the socket, for every
    row, though both answers are the server's. A shutdown plan over ten agents on one server
    ran ten of each, its run as many again, and so did `reap --server-down`. Now once per
    server for one read, however many rows it holds."""
    monkeypatch.setattr(tmux_core, "boot_id", lambda: "b00t-1")
    tmux.started = datetime(2026, 10, 13, 9, 0, 7, tzinfo=UTC)
    coders = [_coder(project) for _ in range(3)]
    newcomer = FakeTmux()  # took the socket: every row asks it whether it is theirs
    newcomer.started = tmux.started + timedelta(minutes=5)
    asked = {"started": 0, "listener": 0}
    started_at, listener = newcomer.started_at, newcomer.listener

    def counted_start() -> datetime | None:
        asked["started"] += 1
        return started_at()

    def counted_listener(recorded: Path | None = None) -> SocketAnswer | None:
        asked["listener"] += 1
        return listener(recorded)

    monkeypatch.setattr(newcomer, "started_at", counted_start)
    monkeypatch.setattr(newcomer, "listener", counted_listener)
    monkeypatch.setattr(fleet_service, "server", lambda config=None: newcomer)

    plan = fleet_service.shutdown_plan(project)

    assert set(plan.held) == {coder.id for coder in coders}
    assert asked == {"started": 1, "listener": 1}, "once for the server, not once per row"
    # And a sweep that holds every row of a server tmux reports absent.
    newcomer.running, newcomer.socket_file = False, False  # its socket file taken too
    monkeypatch.setattr(newcomer, "server_absent", lambda: True)  # as tmux says for that
    asked.update(started=0, listener=0)
    swept = fleet_service.reap(project, server_down=True)
    assert {row.agent.id for row in swept.held} == {coder.id for coder in coders}
    assert asked["listener"] == 1, asked


def test_with_no_boot_id_a_row_from_before_the_boot_is_lost_not_held_by_the_next_server(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 1: on a host with no boot id (the BSDs, a macOS whose `sysctl`
    failed) every row counted as from this boot, so after a reboot a row that recorded its
    server's start was held against the server the next spawn started: `unknown (another
    tmux server on its socket)` for good, which `reap` could not end, `stop` refused and
    `shutdown` held until `--forget`. Created before this boot started, it is lost and
    ended as before; one created since is still held, as its server may run."""
    monkeypatch.setattr(tmux_core, "boot_id", lambda: None)
    tmux.started = datetime(2026, 10, 13, 9, 0, 7, tzinfo=UTC)
    coder = _coder(project)
    assert coder.boot_id is None and coder.server_started_at == tmux.started
    newcomer = FakeTmux()  # the server the first spawn after the reboot started
    newcomer.started = tmux.started + timedelta(hours=2)
    newcomer.spawn_window(f"asq-{_codename(project)}", name="m", cwd=project.root, command=["c"])
    monkeypatch.setattr(fleet_service, "server", lambda config=None: newcomer)

    monkeypatch.setattr(tmux_core, "boot_started", lambda: coder.created_at - timedelta(hours=1))
    [since] = fleet_service.list_agents(project)
    assert (since.state, since.held) == ("unknown", True), "since the boot: its server may run"

    booted = coder.created_at + timedelta(hours=1)  # the reboot came after the row
    monkeypatch.setattr(tmux_core, "boot_started", lambda: booted)
    [before] = fleet_service.list_agents(project)
    assert (before.state, before.detail, before.held) == ("lost", "pane gone", False)
    assert [row.id for row in fleet_service.reap(project).lost] == [coder.id]
    assert newcomer.typed == [] and newcomer.killed == []


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
# The row belongs to the PROCESS in its pane (the rules above
# ``team._named_fleet_row``). These tests act as that process the way Claude Code
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


def test_a_fleet_agents_cycle_names_its_own_project_under_a_hub(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #259, round 22, 1/9: under ``AISQUARE_TEAM_HUB`` every session is on the
    hub's board, and the cycle named the board's project in every fleet command. So the
    manager was told to spawn its coders in the hub's directory, and its ``stop``,
    ``tell`` and ``switch`` found no live agent there. They name the agent's own row's
    project, by its codename. A session with no row names its own directory's project, the
    hub aside (round 23, 3/10, below)."""
    hub = tmp_path / "hub"
    hub.mkdir()
    with store_session() as store:
        store.onboard_project(team_project(hub))
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(hub))
    _agent, session_id = _spawned(project, "manager", None, tmux, monkeypatch)
    with store_session() as store:
        stored = store.get_project(project.id)
    assert stored is not None and stored.codename, "a spawn gives its project a codename"

    board = team_service.hook_session_start(session_id, project.root, "startup")

    assert "Your standing cycle (manager)" in board
    commands = re.findall(r"aisquare fleet [a-z]+(?: --project [\w.-]+)?", board)
    assert {command.split()[2] for command in commands} == {
        "spawn",
        "tell",
        "switch",
        "stop",
        "reap",
    }
    assert all(command.endswith(f"--project {stored.codename}") for command in commands), commands
    assert team_project(hub).id not in board


def test_under_a_hub_a_session_with_no_fleet_row_names_its_own_directorys_project(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #259, round 23, 3/10: under ``AISQUARE_TEAM_HUB`` a session with no fleet
    row, one the human launched, got every fleet command of its cycle without
    ``--project``. Alone, a fleet command acts on the active project, the machine-wide pin
    before the working directory: the manager launched in ``/work/app`` while another shell
    had pinned ``site`` spawned its coders in ``site``, and stopped and reaped there, the
    bug round 21 fixed. Its commands name its directory's project, the hub aside. A
    directory nobody registered names a ``<project>`` to fill in, and says so: never a bare
    command."""
    hub = tmp_path / "hub"
    hub.mkdir()
    with store_session() as store:
        store.onboard_project(team_project(hub))
        named = store.onboard_project(project, codename="calm-otter")
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(hub))
    _stranger(monkeypatch, "manager")

    started = team_service.hook_session_start("sess-no-row", project.root, "startup")
    joined = team_service.hook_prompt_heartbeat("sess-late-no-row", project.root)

    for board in (started, joined):
        assert "`aisquare fleet reap --project calm-otter` again, then" in board
        commands = re.findall(r"aisquare fleet [a-z]+(?: --project [\w.-]+)?", board)
        assert commands and all(c.endswith(f"--project {named.codename}") for c in commands)
        assert team_project(hub).id not in board

    elsewhere = tmp_path / "unregistered"
    elsewhere.mkdir()
    loose = team_service.hook_session_start("sess-loose", elsewhere, "startup")

    assert "Your standing cycle (manager)" in loose
    assert "`aisquare fleet reap --project <project>` again, then" in loose
    assert "The fleet commands below name <project>:" in loose
    assert not re.search(r"aisquare fleet [a-z]+(?! --project)\b", loose), loose


def test_under_a_hub_a_session_with_no_project_is_told_a_pause_to_fill_in_not_the_hubs(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #259, round 27, 6/7: a session whose project could not be told names a
    ``<project>`` in its fleet commands, but was told the bare ``fleet-paused``: under a hub,
    the hub project's own pause. Filled in with ``app``, the manager spawned there and kept
    spawning while ``fleet pause -P app`` set ``app``'s on the hub's board. It is told a
    ``fleet-paused.<project id>`` to fill in, and how: the ID, not the codename."""
    hub = tmp_path / "hub"
    hub.mkdir()
    with store_session() as store:
        board = store.onboard_project(team_project(hub))
        app = store.onboard_project(project, codename="calm-otter")
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(hub))
    _stranger(monkeypatch, "manager")
    elsewhere = tmp_path / "unregistered"
    elsewhere.mkdir()

    loose = team_service.hook_session_start("sess-loose", elsewhere, "startup")

    assert "Spawn\nnothing while the `fleet-paused.<project id>` signal is set." in loose, loose
    assert "`fleet-paused` signal" not in loose, "the hub project's own pause"
    told = next(line for line in loose.splitlines() if line.startswith("Its pause is "))
    assert "that project's ID, not its codename," in told, told
    assert "(`prj_ab12` → `fleet-paused.prj-ab12`)" in told, told
    assert f"`fleet-paused` alone when it is this board's own project ({board.id})" in told

    fleet_service.pause(app)  # what the human runs once the manager is spawning in `app`
    filled = "fleet-paused." + app.id.lower().replace("_", "-")
    with store_session() as store:
        state = team_service._read_signal(store, board.id, filled)
    assert state is not None and state.value == "on", "the name filled in is the one set"


def test_a_cycle_with_no_fleet_row_names_its_project_by_its_codename(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 22, 6/9: the session-start hook and the prompt heartbeat build
    the board from ``orchestrator.team_project``, which carries no codename, and the cycle
    named its fleet commands by it: ``--project prj_<24 hex>`` in every manager's and
    coder's briefing, for a project ``fleet ls``, the sidebar and every receipt call by its
    codename. The stored project is named, as ``fleet_command`` says."""
    with store_session() as store:
        named = store.onboard_project(project, codename="calm-otter")
    assert named.codename == "calm-otter"
    _stranger(monkeypatch, "manager")

    started = team_service.hook_session_start("sess-plain-6", project.root, "startup")
    _stranger(monkeypatch, "coder")
    joined = team_service.hook_prompt_heartbeat("sess-late-6", project.root)

    # The manager's state file is under the project's data directory, which its id names
    # (P1 WS-4): a path, not a command.
    state = str(paths.manager_state_file(project.id))
    for board in (started, joined):
        commands = re.findall(r"aisquare fleet [a-z]+(?: --project [\w.-]+)?", board)
        assert commands and all(c.endswith("--project calm-otter") for c in commands), board
        assert project.id not in board.replace(state, "")


def test_a_hook_reads_the_row_its_window_names_once_for_the_assignment_and_the_cycle(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 23, 10/10: at every session start of a fleet agent, and at
    every late join through the heartbeat, the row ``AISQUARE_FLEET_AGENT`` names was read
    twice in the same store session, a GLOB on its id each time: once for the assignment
    (``_named_fleet_row``) and again for the cycle's project (``_cycle_project``). It is
    read once and handed to both, and each still gets what it got."""
    mine = _task(project, "the task this manager is for")
    agent, session_id = _spawned(project, "manager", mine.id, tmux, monkeypatch)
    with store_session() as store:
        stored = store.get_project(project.id)
    assert stored is not None and stored.codename
    reads: list[str] = []
    real = SqliteStore.get_fleet_agent

    def counted(self: SqliteStore, agent_id: str) -> FleetAgent | None:
        reads.append(agent_id)
        return real(self, agent_id)

    monkeypatch.setattr(SqliteStore, "get_fleet_agent", counted)

    started = team_service.hook_session_start(session_id, project.root, "startup")

    assert reads == [agent.id]
    assert f"ASSIGNED TO YOU: {mine.id} [todo]" in started
    assert f"aisquare fleet reap --project {stored.codename}" in started
    with store_session() as store:  # a session the hook has not seen joins at a prompt
        store.end_session(session_id, release_claims=False)
    reads.clear()
    joined = team_service.hook_prompt_heartbeat("sess-late-10", project.root)

    assert reads == [agent.id]
    assert f"aisquare fleet reap --project {stored.codename}" in joined


def test_a_cycle_whose_project_cannot_be_read_still_gets_its_board(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Review of #259, round 23, 6/10: ``_cycle_project`` guarded the fleet row's read and
    not the stored board's, so a ``get_project`` that raised there (a locked database, a
    schema regression) went out of ``hook_session_start`` and the heartbeat, and the session
    lost its whole ``<aisquare-team>`` injection. The read is guarded and logged, and the
    cycle names the board it already holds."""
    real = SqliteStore.get_project

    def flaky(self: SqliteStore, project_id: str) -> ProjectInfo | None:
        if sys._getframe(1).f_code.co_name == "_cycle_project":
            raise sqlite3.OperationalError("database is locked")
        return real(self, project_id)

    monkeypatch.setattr(SqliteStore, "get_project", flaky)
    _stranger(monkeypatch, "manager")

    with caplog.at_level(logging.WARNING, logger="aisquare.services.team"):
        started = team_service.hook_session_start("sess-plain-r23", project.root, "startup")
        joined = team_service.hook_prompt_heartbeat("sess-late-r23", project.root)

    for board in (started, joined):
        assert "Your standing cycle (manager)" in board
        commands = re.findall(r"aisquare fleet [a-z]+(?: --project [\w.-]+)?", board)
        assert commands and all(c.endswith(f"--project {project.id}") for c in commands), board
    warned = [r.getMessage() for r in caplog.records if r.name == "aisquare.services.team"]
    assert len(warned) == 2 and all("OperationalError: database is locked" in w for w in warned)


def test_the_boards_limited_chip_names_the_agents_own_project_under_a_hub(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #259, round 22, 2/9: ``aisquare board`` and the board panel read the hub's
    board under ``AISQUARE_TEAM_HUB``, and the ``limited`` chip's ``fleet switch`` named
    that board's project, where the agent's row is not: it refused with "no live agent".
    It names the agent's own row's project and label. A session with no row has none."""
    from aisquare.cli.watch import board_frame

    hub = tmp_path / "hub"
    hub.mkdir()
    with store_session() as store:
        store.onboard_project(team_project(hub))
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(hub))
    agent, session_id = _spawned(project, "coder", None, tmux, monkeypatch)
    team_service.hook_session_start(session_id, project.root, "startup")
    _stranger(monkeypatch, "coder")
    team_service.hook_session_start("sess-no-row", project.root, "startup")
    with store_session() as store:
        store.mark_limited(session_id, None)
        store.mark_limited("sess-no-row", None)
        stored = store.get_project(project.id)
    assert stored is not None and stored.codename

    frame = board_frame(height=40, width=200).plain

    switch = f"aisquare fleet switch --project {stored.codename} {agent.label}"
    assert f"⏳ limited — `{switch}`" in frame
    assert frame.count("aisquare fleet switch") == 1, "the session with no row: the chip alone"
    assert team_project(hub).id not in frame


def test_the_limited_chips_switch_is_read_in_the_boards_own_store_session(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 23, 9/10: the board panel and ``aisquare board``'s frame asked
    ``fleet_switches`` for the limited chips after ``board_data`` had closed its store
    session, and it opened a second one: a connection, the migration check and the presence
    pass, at every 2 s tick of every visible board for as long as a session stayed limited.
    The frame is read in one, the chip's switch included."""
    from aisquare.cli.watch import board_frame

    monkeypatch.chdir(project.root)  # the frame reads the board of where it runs
    agent, session_id = _spawned(project, "coder", None, tmux, monkeypatch)
    team_service.hook_session_start(session_id, project.root, "startup")
    with store_session() as store:
        store.mark_limited(session_id, None)
        stored = store.get_project(project.id)
    assert stored is not None and stored.codename
    opened: list[str] = []

    @contextmanager
    def counted(*args: Any, **kwargs: Any) -> Iterator[ContextStore]:
        opened.append("store")
        with store_session(*args, **kwargs) as store:
            yield store

    monkeypatch.setattr(team_service, "store_session", counted)

    frame = board_frame(height=40, width=200).plain

    assert f"`aisquare fleet switch --project {stored.codename} {agent.label}`" in frame
    assert opened == ["store"]


def test_a_limited_chips_switch_that_cannot_be_read_is_logged_not_swallowed(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Review of #259, round 23, 8/10: ``fleet_switches`` fails open, and it did so silently.
    A store or schema regression took the ``fleet switch`` off every limited chip, on the
    board panel and in ``aisquare board``, with nothing anywhere saying so. It logs what it
    swallowed, as ``_cycle_project`` does, and the board still gets no command."""
    now = datetime.now(tz=UTC)
    limited = TeamSession(
        id="sess-limited-8",
        project_id=project.id,
        role="coder",
        started_at=now,
        last_seen_at=now,
        state="limited",
    )

    def changed(self: SqliteStore, *args: object) -> FleetAgent | None:
        raise sqlite3.OperationalError("no such column: session_id")

    monkeypatch.setattr(SqliteStore, "fleet_agent_for_session", changed)

    with (
        caplog.at_level(logging.WARNING, logger="aisquare.services.team"),
        store_session() as store,
    ):
        assert team_service.fleet_switches(store, [limited]) == {}

    [record] = [r for r in caplog.records if r.name == "aisquare.services.team"]
    assert "could not be read" in record.getMessage()
    assert "OperationalError: no such column: session_id" in record.getMessage()


# --- under a hub: one board, many fleet projects ------------------------------------------
#
# docs/fleet.md's rule ("Under a hub: one board, many fleet projects"): what the team reads
# and writes goes to the session's board, the hub's under one; what finds an agent goes to
# its row's own project and never asks the board. Without a hub the two are one project, so
# every call site that asked the wrong one passed every other test (review of #259, rounds
# 21 to 24). Each case below runs under a hub whose fleet project is another, and names the
# project where it landed.


@dataclass(frozen=True)
class _Hubbed:
    """A hub's board and a fleet project that is another: a manager and a coder spawned
    into ``fleet``, both started on ``hub``'s board, the coder holding ``task``, its process
    the one this test runs as. ``woken`` is the project of each manager a nudge went to."""

    hub: ProjectInfo
    fleet: ProjectInfo
    manager: FleetAgent
    coder: FleetAgent
    task: TeamTask
    tmux: FakeTmux
    monkeypatch: pytest.MonkeyPatch
    woken: list[str]
    nudge: Any
    """``fleet.nudge_manager`` itself, which the fixture replaces with a recorder."""

    @property
    def manager_sid(self) -> str:
        assert self.manager.session_id is not None
        return self.manager.session_id

    @property
    def coder_sid(self) -> str:
        assert self.coder.session_id is not None
        return self.coder.session_id


@pytest.fixture
def hubbed(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> _Hubbed:
    hub_root = tmp_path / "hub"
    hub_root.mkdir()
    with store_session() as store:
        hub = store.onboard_project(team_project(hub_root))
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(hub_root))
    manager, manager_sid = _spawned(project, "manager", None, tmux, monkeypatch)
    team_service.hook_session_start(manager_sid, project.root, "startup")
    task = _task(project, "the coder's task, on the hub's board")
    coder, coder_sid = _spawned(project, "coder", None, tmux, monkeypatch)
    team_service.hook_session_start(coder_sid, project.root, "startup")
    team_service.claim_task(task.id, session_ref=coder_sid)
    woken: list[str] = []
    real = fleet_service.nudge_manager

    def nudge(project_id: str, *, reason: str, board: str | None = None) -> bool:
        woken.append(project_id)
        return True

    monkeypatch.setattr(fleet_service, "nudge_manager", nudge)
    assert task.project_id == hub.id != project.id
    return _Hubbed(hub, project, manager, coder, task, tmux, monkeypatch, woken, real)


def _delivered() -> str | None:
    """The board the last attributed write was delivered to."""
    delivery = team_service.last_delivery()
    return delivery.board_id if delivery is not None else None


def _named_by(command: str, w: _Hubbed) -> str | None:
    """The id of the project ``command``'s ``--project`` names, of the hub and the fleet's."""
    named = re.search(r"--project (\S+)", command)
    with store_session() as store:
        for project in (w.hub, w.fleet):
            stored = store.get_project(project.id) or project
            if named is not None and named.group(1) in (stored.codename, stored.id):
                return project.id
    return None


def _case_task_add(w: _Hubbed) -> str | None:
    task, _ = team_service.add_task("another task", session_ref=w.manager_sid)
    return task.project_id


def _case_note(w: _Hubbed) -> str | None:
    team_service.add_note("which branch?", kind="question", session_ref=w.coder_sid)
    return _delivered()


def _case_task_next(w: _Hubbed) -> str | None:
    team_service.add_task("a task for the pool", role="coder", session_ref=w.manager_sid)
    picked = team_service.next_task(role="coder", claim=True, session_ref=w.coder_sid)
    assert picked is not None
    return picked.project_id


def _case_task_review(w: _Hubbed) -> str | None:
    team_service.review_task(w.task.id, note="green", session_ref=w.coder_sid)
    return _delivered()


def _case_task_done(w: _Hubbed) -> str | None:
    team_service.finish_task(w.task.id, session_ref=w.coder_sid)
    return _delivered()


def _case_spawn_task(w: _Hubbed) -> str | None:
    task, _ = team_service.add_task("a task to spawn for", role="coder", session_ref=w.manager_sid)
    row = fleet_service.spawn(w.fleet, "coder", task_id=f"#{task.number}", worktree=False).agent
    assert row.task_id is not None
    return _task_now(row.task_id).project_id


def _kept_by(w: _Hubbed, session_id: str) -> str | None:
    """The board of the coder's task while ``session_id`` still holds it, else ``None``."""
    task = _task_now(w.task.id)
    return task.project_id if task.status == "doing" and task.claimed_by == session_id else None


def _case_hand_over_keeps(w: _Hubbed) -> str | None:
    with store_session() as store:
        store.touch_session(w.coder_sid, state=team_service.HANDOVER_STATE)
    team_service.hook_session_end(w.coder_sid, w.fleet.root, reason="prompt_input_exit")
    return _kept_by(w, w.coder_sid)


def _case_clear_keeps(w: _Hubbed) -> str | None:
    team_service.hook_session_end(w.coder_sid, w.fleet.root, reason="clear")
    return _kept_by(w, w.coder_sid)


def _case_clear_adopts(w: _Hubbed) -> str | None:
    _clear(w.coder_sid, "sess-after-clear", w.fleet)
    with store_session() as store:
        row = store.fleet_agent_for_session("sess-after-clear")
    return row.project_id if row is not None else None


def _fed_to(w: _Hubbed, kind: str) -> str | None:
    """The one board, of the hub's and the fleet's, whose feed has a ``kind`` line."""
    with store_session() as store:
        boards = [
            project.id
            for project in (w.hub, w.fleet)
            if any(event.kind == kind for event in store.recent_events(project.id, limit=100))
        ]
    return boards[0] if len(boards) == 1 else None


def _ready_windows(w: _Hubbed) -> None:
    """Every window the fleet opens from here runs ``claude`` at once, so a hand-off prompt
    is pasted into it."""
    original = w.tmux.spawn_window

    def ready(*args: Any, **kwargs: Any) -> WindowInfo:
        window = original(*args, **kwargs)
        w.tmux.set_command(window.pane_id, "claude")
        return window

    w.monkeypatch.setattr(w.tmux, "spawn_window", ready)


def _case_exited(w: _Hubbed) -> str | None:
    w.tmux.die(w.coder.pane_id, 1)
    fleet_service.reap()
    return _fed_to(w, "agent_exited")


def _case_restarted(w: _Hubbed) -> str | None:
    w.tmux.die(w.coder.pane_id, 0)
    fleet_service.restart(w.fleet, w.coder.label)
    return _fed_to(w, "restarted")


def _case_switched(w: _Hubbed) -> str | None:
    _two_slots_with_usage(w.monkeypatch, work=95, personal=10)
    fleet_service.switch(w.fleet, w.coder.label, to="2")
    return _fed_to(w, "switched")


def _case_hand_off_entries(w: _Hubbed) -> str | None:
    team_service.add_note("halfway through the router", session_ref=w.coder_sid)
    _ready_windows(w)
    w.tmux.die(w.coder.pane_id, 0)
    fleet_service.restart(w.fleet, w.coder.label)
    [prompt] = _first_lines(w.tmux)
    return _delivered() if "- note: halfway through the router" in prompt else None


def _woke(w: _Hubbed, write: Callable[[], object]) -> str | None:
    """The project of the one manager ``write`` nudged."""
    w.woken.clear()
    write()
    return w.woken[0] if len(w.woken) == 1 else repr(w.woken)


def _case_question_wakes(w: _Hubbed) -> str | None:
    return _woke(
        w, lambda: team_service.add_note("which branch?", kind="question", session_ref=w.coder_sid)
    )


def _case_result_wakes(w: _Hubbed) -> str | None:
    return _woke(w, lambda: team_service.add_note("green", kind="result", session_ref=w.coder_sid))


def _case_note_to_manager_wakes(w: _Hubbed) -> str | None:
    return _woke(
        w, lambda: team_service.add_note("fyi", to_role="manager", session_ref=w.coder_sid)
    )


def _case_done_wakes(w: _Hubbed) -> str | None:
    return _woke(w, lambda: team_service.finish_task(w.task.id, session_ref=w.coder_sid))


def _case_review_wakes(w: _Hubbed) -> str | None:
    return _woke(w, lambda: team_service.review_task(w.task.id, session_ref=w.coder_sid))


def _case_block_wakes(w: _Hubbed) -> str | None:
    return _woke(
        w, lambda: team_service.block_task(w.task.id, reason="no spec", session_ref=w.coder_sid)
    )


def _case_reopen_wakes(w: _Hubbed) -> str | None:
    team_service.review_task(w.task.id, session_ref=w.coder_sid)
    return _woke(
        w, lambda: team_service.reopen_task(w.task.id, reason="red", session_ref=w.coder_sid)
    )


def _case_deferred_wake(w: _Hubbed) -> str | None:
    team_service.add_note(
        "which branch?", kind="question", to_role="manager", session_ref=w.coder_sid
    )
    _become(w.manager, w.tmux, w.monkeypatch, pid=PANE_PID, role="manager")
    return _woke(
        w,
        lambda: team_service.hook_stop(w.manager_sid, w.fleet.root, stop_hook_active=True),
    )


def _case_cycle_project(w: _Hubbed) -> str | None:
    with store_session() as store:
        named = team_service._cycle_project(store, w.hub, w.fleet.root, w.manager_sid, named=None)
    return named.id if named is not None else None


def _case_limited_chip(w: _Hubbed) -> str | None:
    with store_session() as store:
        store.mark_limited(w.coder_sid, None)
        session = store.get_session(w.coder_sid)
        assert session is not None
        switches = team_service.fleet_switches(store, [session])
    return _named_by(switches[w.coder_sid], w)


def _case_phone_note_wakes(w: _Hubbed) -> str | None:
    # The phone's Board note to the manager: no session, the project's root (`write_note`).
    return _woke(w, lambda: team_service.add_note("fyi", to_role="manager", cwd=w.fleet.root))


def _case_phone_done_wakes(w: _Hubbed) -> str | None:
    from aisquare.services.remote_server import live_writes

    other, _ = _another_fleet_on_the_hub(w)
    w.monkeypatch.chdir(other.root)  # Remote runs in the other project's folder
    done = live_writes().handlers["task/done"]
    return _woke(w, lambda: done({"ref": w.task.id, "project": w.fleet.id}))


def _case_rowless_session_wakes(w: _Hubbed) -> str | None:
    # A session no row holds: the human's own claude, an MCP client, a manager by hand.
    _stranger(w.monkeypatch, "human")
    team_service.hook_session_start("sess-by-hand", w.hub.root, "startup")
    return _woke(w, lambda: team_service.finish_task(w.task.id, session_ref="sess-by-hand"))


def _case_limit_nudge(w: _Hubbed) -> str | None:
    team_service.hook_stop_failure(
        w.coder_sid, error="rate_limit", message="You've hit your session limit", details=None
    )
    (woken,) = w.woken
    return woken


def _without_the_hub(w: _Hubbed) -> None:
    """Run on as a caller that does not export the hub: ``asq``, the phone's Remote or a
    plain shell (review of #259, round 25)."""
    w.monkeypatch.delenv("AISQUARE_TEAM_HUB")


def _for_its_task(w: _Hubbed) -> FleetAgent:
    """The coder's row, now naming the task it holds, as a ``spawn --task`` row does."""
    with store_session() as store:
        return store.upsert_fleet_agent(w.coder.model_copy(update={"task_id": w.task.id}))


def _window_board(w: _Hubbed, row: FleetAgent) -> str | None:
    """The board the last window's ``launch`` and hooks resolve, under the hub it was given,
    when that is the board ``row`` recorded; else ``None``."""
    env, cwd = w.tmux.spawned[-1]["env"], w.tmux.spawned[-1]["cwd"]
    assert isinstance(env, dict) and isinstance(cwd, (str, Path))
    with pytest.MonkeyPatch.context() as window:
        window.setenv("AISQUARE_TEAM_HUB", env["AISQUARE_TEAM_HUB"])
        resolved = team_project(Path(cwd)).id
    return resolved if resolved == row.board else None


def _case_spawn_records(w: _Hubbed) -> str | None:
    return _row(w.coder.id).board


def _case_spawn_records_the_hub(w: _Hubbed) -> str | None:
    spec = _row(w.coder.id).launch_spec
    assert spec is not None and spec.hub is not None
    with pytest.MonkeyPatch.context() as window:
        window.setenv("AISQUARE_TEAM_HUB", spec.hub)
        return team_project(w.fleet.root).id


def _case_restart_without_the_hub(w: _Hubbed) -> str | None:
    row = _for_its_task(w)
    w.tmux.die(row.pane_id, 0)
    _without_the_hub(w)
    started = fleet_service.restart(w.fleet, row.label).started
    assert started.task_id == w.task.id
    return _window_board(w, started)


def _case_switch_without_the_hub(w: _Hubbed) -> str | None:
    row = _for_its_task(w)
    _two_slots_with_usage(w.monkeypatch, work=95, personal=10)
    _without_the_hub(w)
    started = fleet_service.switch(w.fleet, row.label, to="2").started
    assert started.task_id == w.task.id
    return _window_board(w, started)


def _case_exited_without_a_session_or_the_hub(w: _Hubbed) -> str | None:
    row = fleet_service.spawn(w.fleet, "coder", worktree=False).agent  # its hook never ran
    _without_the_hub(w)
    w.tmux.die(row.pane_id, 1)
    fleet_service.reap()
    return _fed_to(w, "agent_exited")


def _spawned_without_the_hub(w: _Hubbed) -> FleetAgent:
    """A coder spawned by a caller that exported no hub, the hub set again after."""
    _without_the_hub(w)
    row = fleet_service.spawn(w.fleet, "coder", worktree=False).agent
    w.monkeypatch.setenv("AISQUARE_TEAM_HUB", str(w.hub.root))
    return row


def _case_restart_under_the_hub_of_a_row_without(w: _Hubbed) -> str | None:
    row = _spawned_without_the_hub(w)
    w.tmux.die(row.pane_id, 0)
    return _window_board(w, fleet_service.restart(w.fleet, row.label).started)


def _case_exited_under_the_hub_of_a_row_without(w: _Hubbed) -> str | None:
    row = _spawned_without_the_hub(w)
    w.tmux.die(row.pane_id, 1)
    fleet_service.reap()
    return _fed_to(w, "agent_exited")


def _paused_on(w: _Hubbed, project: ProjectInfo | None = None) -> list[str]:
    """The boards, of the hub's and the fleet's, where the ``fleet-paused`` signal of
    ``project`` (the fleet's by default) reads ON, by its name there
    (``harness.pause_signal``)."""
    paused = project or w.fleet
    with store_session() as store:
        return [
            board.id
            for board in (w.hub, w.fleet)
            for name in [harness.pause_signal(paused.id, board.id)]
            for state in [team_service._read_signal(store, board.id, name)]
            if state is not None and state.value == "on"
        ]


def _case_pause(w: _Hubbed) -> str | None:
    """The board the manager reads, if the pause is on it. It is also on the fleet's own
    board, its checkout's, where a manager spawned without the hub reads
    (``fleet._pause_homes``), and nowhere else."""
    fleet_service.pause(w.fleet)
    paused = _paused_on(w)
    assert _is_paused(w.fleet)
    fleet_service.resume(w.fleet)
    assert _paused_on(w) == [] and not _is_paused(w.fleet)
    assert sorted(paused) == sorted([w.hub.id, w.fleet.id]), paused
    return _row(w.manager.id).board if _row(w.manager.id).board in paused else None


def _case_pause_without_the_hub(w: _Hubbed) -> str | None:
    _without_the_hub(w)
    return _case_pause(w)


def _case_tell_unhubbed(w: _Hubbed) -> str | None:
    _without_the_hub(w)  # `asq`, the phone's tell or a plain shell
    told = fleet_service.tell(w.fleet, w.coder.label, "please rebase")
    assert not told.delivered and "filed as board note" in told.how, told
    return _delivered()


def _case_pause_is_its_projects(w: _Hubbed) -> str | None:
    fleet_service.pause(w.fleet)
    paused = [project.id for project in (w.hub, w.fleet) if _is_paused(project)]
    return paused[0] if len(paused) == 1 else repr(paused)


def _case_shutdown_clears_the_pause(w: _Hubbed) -> str | None:
    # Set by the manager itself, `team signal <its pause> on`, where its cycle reads it.
    team_service.set_signal(
        harness.pause_signal(w.fleet.id, w.hub.id), "on", session_ref=w.manager_sid
    )
    (paused,) = _paused_on(w)
    report = fleet_service.shutdown(w.fleet)
    assert _paused_on(w) == [] and report.paused_cleared, report
    return paused


#: Each case: which of the rule's two places it goes to, what it is, and how to run it.
#: A board case is about an agent spawned under the hub; the board is the one its spawn
#: recorded, whoever asks, with the hub or without it.
_HUB_RULE_CASES: list[tuple[str, str, Any]] = [
    ("board", "task add", _case_task_add),
    ("board", "a question on the feed", _case_note),
    ("board", "task next --claim", _case_task_next),
    ("board", "task review", _case_task_review),
    ("board", "task done", _case_task_done),
    ("board", "the task fleet spawn --task gives", _case_spawn_task),
    ("board", "the claim a hand-over's SessionEnd keeps", _case_hand_over_keeps),
    ("board", "the claim a /clear's SessionEnd keeps", _case_clear_keeps),
    ("board", "agent_exited", _case_exited),
    ("board", "restarted", _case_restarted),
    ("board", "switched", _case_switched),
    ("board", "the entries a hand-off prompt quotes", _case_hand_off_entries),
    ("board", "the board a spawn records", _case_spawn_records),
    ("board", "the board under the hub a spawn records", _case_spawn_records_the_hub),
    ("board", "a restart from a caller without the hub", _case_restart_without_the_hub),
    ("board", "a switch from a caller without the hub", _case_switch_without_the_hub),
    ("board", "agent_exited, no session, the hub unset", _case_exited_without_a_session_or_the_hub),
    ("board", "fleet pause and resume", _case_pause),
    ("board", "fleet pause from a caller without the hub", _case_pause_without_the_hub),
    ("board", "the pause fleet shutdown clears", _case_shutdown_clears_the_pause),
    ("fleet", "the project a pause on the hub's board pauses", _case_pause_is_its_projects),
    ("board", "the note fleet tell files from a caller without the hub", _case_tell_unhubbed),
    (
        "fleet",
        "a restart under the hub of a row without",
        _case_restart_under_the_hub_of_a_row_without,
    ),
    (
        "fleet",
        "agent_exited under the hub of a row without",
        _case_exited_under_the_hub_of_a_row_without,
    ),
    ("fleet", "the row a /clear's next id is adopted onto", _case_clear_adopts),
    ("fleet", "the cycle's --project", _case_cycle_project),
    ("fleet", "a limited chip's fleet switch", _case_limited_chip),
    ("fleet", "the manager a usage limit wakes", _case_limit_nudge),
    ("fleet", "the manager a question wakes", _case_question_wakes),
    ("fleet", "the manager a result wakes", _case_result_wakes),
    ("fleet", "the manager a note --to manager wakes", _case_note_to_manager_wakes),
    ("fleet", "the manager task done wakes", _case_done_wakes),
    ("fleet", "the manager task review wakes", _case_review_wakes),
    ("fleet", "the manager task block wakes", _case_block_wakes),
    ("fleet", "the manager task reopen wakes", _case_reopen_wakes),
    ("fleet", "the manager a deferred Stop wakes", _case_deferred_wake),
    ("fleet", "the manager the phone's note wakes", _case_phone_note_wakes),
    ("fleet", "the manager the phone's task done wakes", _case_phone_done_wakes),
    ("fleet", "the manager a session with no row wakes", _case_rowless_session_wakes),
]


def test_under_a_hub_a_manager_spawns_its_coder_for_a_task_it_added(hubbed: _Hubbed) -> None:
    """Review of #259, round 24, 2/6: since round 22 a hub manager's cycle spawns its coders
    in its own row's project, while ``task add`` puts every task on the hub's board, and
    ``fleet spawn --task`` looked the task up on the project it spawns into: "task … belongs
    to another project's board" for every task the manager had added, and ``--task 12`` read
    that project's #12. The task is looked up on the board the agent will read, the hub's;
    ``restart`` checks the task there too. A task of the fleet project's own board is the
    one now refused: the agent would never see it."""
    task, _ = team_service.add_task("the hub's task", role="coder", session_ref=hubbed.manager_sid)

    row = fleet_service.spawn(hubbed.fleet, "coder", task_id=f"#{task.number}", worktree=False)

    assert row.agent.project_id == hubbed.fleet.id and row.agent.task_id == task.id
    hubbed.tmux.die(row.agent.pane_id, 0)
    assert fleet_service.restart(hubbed.fleet, row.agent.label).started.task_id == task.id
    hubbed.monkeypatch.delenv("AISQUARE_TEAM_HUB")
    stray, _ = team_service.add_task("the fleet project's own task", cwd=hubbed.fleet.root)
    hubbed.monkeypatch.setenv("AISQUARE_TEAM_HUB", str(hubbed.hub.root))
    assert stray.project_id == hubbed.fleet.id
    with pytest.raises(FleetError, match="belongs to another project's board"):
        fleet_service.spawn(hubbed.fleet, "coder", task_id=stray.id, worktree=False)


@pytest.mark.parametrize("reason", ["switch", "clear"])
def test_under_a_hub_a_hand_over_or_a_clear_keeps_the_agents_claims(
    hubbed: _Hubbed, reason: str
) -> None:
    """Review of #259, round 24, 1/6: the SessionEnd of a hand-over or a ``/clear`` keeps the
    agent's claims when the session is a live fleet row's, and it asked for the row with the
    session's board. Under a hub that is the hub's, where the row is not: a coder's
    usage-limit switch released its task back to the pool, with ``task_released`` on the
    feed, and its replacement inherited nothing; its ``/clear`` released it the same way.
    The row is found in any project, and the claims wait for the id that follows."""
    with store_session() as store:
        if reason == "switch":
            store.touch_session(hubbed.coder_sid, state=team_service.HANDOVER_STATE)

    team_service.hook_session_end(
        hubbed.coder_sid, hubbed.fleet.root, reason="clear" if reason == "clear" else "other"
    )

    task = _task_now(hubbed.task.id)
    assert (task.status, task.claimed_by) == ("doing", hubbed.coder_sid)
    with store_session() as store:
        assert not [e for e in store.recent_events(hubbed.hub.id) if e.kind == "task_released"]
        ended = store.get_session(hubbed.coder_sid)
    assert ended is not None and ended.ended_at is not None, "the presence is retired"


@pytest.mark.parametrize("bound_at", ["the start hook", "the next prompt"])
def test_under_a_hub_a_clears_next_id_is_bound_to_the_row_and_told_its_task(
    hubbed: _Hubbed, bound_at: str
) -> None:
    """Review of #259, round 24, 5/6: the start hook after a ``/clear``, and the prompt that
    tries again when tmux did not answer it, bind the new id to the row through the
    assignment, which asked for the row with the session's board and dropped a row of
    another project. Under a hub that is every row: the row stayed bound to the ended id,
    the claims parked on it, and every lookup by the new id missed, the usage-limit
    hand-over's and the board chip's included. The row is adopted in any project, and the
    ASSIGNED TO YOU block still names only a task of the session's board."""
    task, _ = team_service.add_task("the hub's task", role="coder", session_ref=hubbed.manager_sid)
    row = fleet_service.spawn(hubbed.fleet, "coder", task_id=task.id, worktree=False).agent
    assert row.session_id is not None
    _become(row, hubbed.tmux, hubbed.monkeypatch, pid=PANE_PID, role="coder")
    team_service.hook_session_start(row.session_id, hubbed.fleet.root, "startup")
    team_service.claim_task(task.id, session_ref=row.session_id)

    team_service.hook_session_end(row.session_id, hubbed.fleet.root, reason="clear")
    if bound_at == "the next prompt":
        pid = hubbed.tmux.pids.pop(row.pane_id)  # tmux does not answer the start hook
        started = team_service.hook_session_start("sess-next", hubbed.fleet.root, "clear")
        assert "ASSIGNED TO YOU" not in started and _row(row.id).session_id == row.session_id
        hubbed.tmux.pids[row.pane_id] = pid
        briefed = team_service.hook_prompt_heartbeat("sess-next", hubbed.fleet.root)
    else:
        briefed = team_service.hook_session_start("sess-next", hubbed.fleet.root, "clear")

    assert _row(row.id).session_id == "sess-next"
    assert (_task_now(task.id).status, _task_now(task.id).claimed_by) == ("doing", "sess-next")
    assert f"ASSIGNED TO YOU: {task.id} [doing]" in briefed
    assert "You are the one working it" in briefed


def test_under_a_hub_the_briefing_names_no_task_of_another_board(hubbed: _Hubbed) -> None:
    """The other half of 5/6: the row is found in any project, its task only on the
    session's board. A row spawned for a task of its own project's board, before the hub
    was set, is still this session's row, and the briefing tells it of no task it cannot
    see on its board."""
    hubbed.monkeypatch.delenv("AISQUARE_TEAM_HUB")
    stray, _ = team_service.add_task("the fleet project's own task", cwd=hubbed.fleet.root)
    row = fleet_service.spawn(hubbed.fleet, "coder", task_id=stray.id, worktree=False).agent
    hubbed.monkeypatch.setenv("AISQUARE_TEAM_HUB", str(hubbed.hub.root))
    assert row.session_id is not None and stray.project_id == hubbed.fleet.id
    _become(row, hubbed.tmux, hubbed.monkeypatch, pid=PANE_PID, role="coder")

    started = team_service.hook_session_start(row.session_id, hubbed.fleet.root, "startup")

    assert "ASSIGNED TO YOU" not in started and stray.id not in started
    with store_session() as store:
        found = store.fleet_agent_for_session(row.session_id)
    assert found is not None and found.id == row.id


def test_a_prompt_under_a_fleet_window_reads_the_row_it_names_once(hubbed: _Hubbed) -> None:
    """Review of #259, round 24, 6/6: ``_late_assignment``, on every prompt of a session
    under a fleet window that no row is bound to, read the row ``AISQUARE_FLEET_AGENT``
    names twice, a GLOB on its id each time: once to ask whether the session is bound,
    and again for the assignment. Read once, it is handed to both. A child of the agent,
    which inherits the variable and is refused the row, is that session at every prompt."""
    _become(hubbed.coder, hubbed.tmux, hubbed.monkeypatch, pid=CHILD_PID, role="coder")
    team_service.hook_session_start("sess-child", hubbed.fleet.root, "startup")
    reads: list[str] = []
    real = SqliteStore.get_fleet_agent

    def counted(self: SqliteStore, agent_id: str) -> FleetAgent | None:
        reads.append(agent_id)
        return real(self, agent_id)

    hubbed.monkeypatch.setattr(SqliteStore, "get_fleet_agent", counted)

    team_service.hook_prompt_heartbeat("sess-child", hubbed.fleet.root)

    assert reads == [hubbed.coder.id]
    assert _row(hubbed.coder.id).session_id == hubbed.coder_sid, "the child is refused it"


def test_under_a_hub_a_manager_is_told_its_coder_exited_and_was_restarted(
    hubbed: _Hubbed,
) -> None:
    """Review of #259, round 24, 3/6: since round 22 a hub manager's coders are spawned
    into its row's project, and the fleet wrote ``agent_exited``, ``switched`` and
    ``restarted`` to the row's project's board. The manager reads its session's, the hub's,
    at its Stop and in its prompt deltas, so it never heard that a coder had died, and a
    nudge woke it to an empty delta. They go to the board the row's session reads."""
    _become(hubbed.manager, hubbed.tmux, hubbed.monkeypatch, pid=PANE_PID, role="manager")
    team_service.hook_prompt_heartbeat(hubbed.manager_sid, hubbed.fleet.root)  # caught up
    hubbed.tmux.die(hubbed.coder.pane_id, 1)

    fleet_service.reap()
    fleet_service.restart(hubbed.fleet, hubbed.coder.label)

    delta = team_service.hook_prompt_heartbeat(hubbed.manager_sid, hubbed.fleet.root)
    assert f"{hubbed.coder.label} exited (1)" in delta
    assert f"{hubbed.coder.label} restarted" in delta
    assert hubbed.woken and set(hubbed.woken) == {hubbed.fleet.id}, "its own manager"
    with store_session() as store:
        assert not store.recent_events(hubbed.fleet.id), "nothing on a board nobody reads"


def test_under_a_hub_a_write_with_no_row_wakes_the_manager_of_the_writers_project(
    hubbed: _Hubbed, plain_project: ProjectInfo
) -> None:
    """Review of #259, round 25, 5/10: a writer with no fleet row (the phone, a shell, an
    MCP client) nudged the board's project's manager, under a hub the hub's, where none has
    a row. It wakes the manager that reads the board, and with two on one hub, the one
    whose project holds the writer's directory; in neither, none."""
    other, _ = _spawned(plain_project, "manager", None, hubbed.tmux, hubbed.monkeypatch)
    assert other.board == hubbed.manager.board == hubbed.hub.id

    for where, woken in (
        (plain_project.root, [plain_project.id]),
        (hubbed.fleet.root, [hubbed.fleet.id]),
        (hubbed.hub.root, [hubbed.hub.id]),  # neither project's: the board's, as before
    ):
        hubbed.woken.clear()
        team_service.add_note("which branch?", kind="question", cwd=where)
        assert hubbed.woken == woken, where


def test_a_usage_limit_reads_the_agents_row_once(hubbed: _Hubbed) -> None:
    """Review of #259, round 25, 8/10: ``hook_stop_failure`` read the session's row for the
    limit line's label and command, then ``_nudge_target`` read the same row again for the
    manager to wake. The row read once names both; a re-fire of the limit nudges nobody."""
    reads: list[str] = []
    real = SqliteStore.fleet_agent_for_session

    def counted(self: SqliteStore, session_id: str) -> FleetAgent | None:
        reads.append(session_id)
        return real(self, session_id)

    hubbed.monkeypatch.setattr(SqliteStore, "fleet_agent_for_session", counted)
    limit = "You've hit your session limit · resets 12:30am"

    team_service.hook_stop_failure(
        hubbed.coder_sid, error="rate_limit", message=limit, details=None
    )
    team_service.hook_stop_failure(
        hubbed.coder_sid, error="rate_limit", message=limit, details=None
    )

    assert reads == [hubbed.coder_sid, hubbed.coder_sid], "once a call"
    assert hubbed.woken == [hubbed.fleet.id], "its own manager, once"


@pytest.mark.parametrize(("verb", "looked_up"), [("drop", 0), ("done", 1)])
def test_a_task_dropped_looks_up_no_manager_to_wake(
    hubbed: _Hubbed, verb: str, looked_up: int
) -> None:
    """Review of #259, round 25, 9/10: ``_finish_task`` worked out the manager a write wakes,
    a lookup of the writer's live row, for every status, ``drop`` too, whose caller threw
    it away. Only a status that wakes looks."""
    reads: list[str] = []
    real = SqliteStore.fleet_agent_for_session

    def counted(self: SqliteStore, session_id: str) -> FleetAgent | None:
        reads.append(session_id)
        return real(self, session_id)

    hubbed.monkeypatch.setattr(SqliteStore, "fleet_agent_for_session", counted)
    finish = team_service.drop_task if verb == "drop" else team_service.finish_task

    finish(hubbed.task.id, session_ref=hubbed.coder_sid)

    assert len(reads) == looked_up
    assert hubbed.woken == ([hubbed.fleet.id] if looked_up else [])


@pytest.mark.parametrize("nested", ["a sub-folder", "a linked worktree"])
def test_a_project_rooted_below_its_checkout_spawns_for_a_task_on_the_checkouts_board(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    nested: str,
) -> None:
    """Review of #259, round 25, 3/10: a project rooted in a sub-folder of its repository,
    or at a linked worktree, has its manager, its sessions and every ``task add`` on its
    principal checkout's board, as ``team_project`` maps it. ``fleet spawn --task`` looked
    the task up on the project's own id ("no task #1 on this board"; "belongs to another
    project's board" by id). The board is the one the agent's window resolves, recorded."""
    if nested == "a sub-folder":
        root = project.root / "api"
        root.mkdir()
    else:
        root = tmp_path / "wt"
        _git("worktree", "add", "-q", "-b", "wt", str(root), cwd=project.root)
    below = ProjectInfo(id=project_id_for(root), root=root, linked_repos=[])
    with store_session() as store:
        store.onboard_project(below)
    task, _ = team_service.add_task("the checkout's task", role="coder", cwd=root)
    assert task.project_id == project.id != below.id, "the checkout's board, added"

    row = fleet_service.spawn(below, "coder", task_id=f"#{task.number}", worktree=False).agent

    assert (row.project_id, row.task_id, row.board) == (below.id, task.id, task.project_id)
    tmux.die(row.pane_id, 0)
    assert fleet_service.restart(below, row.label).started.task_id == task.id


def _last_window_hub(tmux: FakeTmux) -> object:
    """The ``AISQUARE_TEAM_HUB`` the last window the fleet opened was given."""
    env = tmux.spawned[-1]["env"]
    assert isinstance(env, dict)
    return env["AISQUARE_TEAM_HUB"]


def test_a_replacement_gets_the_hub_its_agents_window_was_given_never_one_worked_out(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #259, round 26, 8/11: a restart's window was given a hub worked out from the
    board the row recorded, "a board that is not the checkout's is a hub, at its root". For a
    project rooted at ``repo/api``, whose window reads ``repo``'s board, a row that read as its
    own project's (0.8.0 writes no board, and a session that never registered gives the
    project's) got ``AISQUARE_TEAM_HUB=repo/api``: a private board its manager never reads,
    recorded again by every restart after. And a window spawned under the hub ``repo``, whose
    board is the checkout's too, came back with none. The spawn records the hub its window
    was given (``LaunchSpec.hub``), and a replacement is given it again."""
    root = project.root / "api"
    root.mkdir()
    with store_session() as store:
        below = store.onboard_project(ProjectInfo(id=project_id_for(root), root=root))
    plain = _coder(below, label="coder-plain")
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(project.root))  # the checkout, as a hub
    under = _coder(below, label="coder-under")
    monkeypatch.delenv("AISQUARE_TEAM_HUB")
    assert plain.launch_spec is not None and plain.launch_spec.hub == ""
    assert under.launch_spec is not None and under.launch_spec.hub == str(project.root)
    assert plain.board == under.board == project.id, "the checkout's board, both"
    raw = sqlite3.connect(str(paths.db_path()))
    try:
        legacy = json.loads(plain.launch_spec.model_dump_json())
        del legacy["hub"]  # as a build from before the field wrote it
        raw.execute(
            "UPDATE fleet_agent SET board = NULL, launch_spec = ? WHERE id = ?",
            (json.dumps(legacy), plain.id),
        )
        raw.commit()
    finally:
        raw.close()
    assert _row(plain.id).board == below.id, "no session registered: its project's"

    tmux.die(plain.pane_id, 0)
    again = fleet_service.restart(below, plain.label).started

    assert _last_window_hub(tmux) == "", "no hub: the window reads the checkout's board"
    assert again.board == project.id and again.launch_spec is not None
    assert again.launch_spec.hub == ""
    tmux.die(under.pane_id, 0)
    kept = fleet_service.restart(below, under.label).started
    assert _last_window_hub(tmux) == str(project.root), "the hub it was spawned under"
    assert kept.board == project.id


def _as_080_left_it(agent: FleetAgent) -> None:
    """The row as 0.8.0 wrote it: no board recorded, and a launch spec without the hub."""
    assert agent.launch_spec is not None
    legacy = json.loads(agent.launch_spec.model_dump_json())
    del legacy["hub"]
    raw = sqlite3.connect(str(paths.db_path()))
    try:
        raw.execute(
            "UPDATE fleet_agent SET board = NULL, launch_spec = ? WHERE id = ?",
            (json.dumps(legacy), agent.id),
        )
        raw.commit()
    finally:
        raw.close()


def _project_below(project: ProjectInfo) -> ProjectInfo:
    """A project rooted at ``<checkout>/api``, a sub-folder of ``project``'s repository."""
    root = project.root / "api"
    root.mkdir()
    with store_session() as store:
        return store.onboard_project(ProjectInfo(id=project_id_for(root), root=root))


@pytest.mark.parametrize("verb", ["restart", "switch"])
def test_a_080_agent_of_a_sub_folder_project_with_its_own_board_is_replaced_on_it(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    verb: str,
) -> None:
    """Review of #259, round 27, 2/7: in 0.8.0 a project rooted at ``repo/api`` had a board
    of its own only under ``AISQUARE_TEAM_HUB=repo/api``, its sessions and its ``--task``
    tasks on it, and the backfill gives its rows that board, their session's. A spec from
    before the hub field mapped the project's own board to the checkout's: a restart or a
    switch checked the task on the row's board, stopped the agent, and its replacement was
    refused the task ("belongs to another project's board"), the agent left down. The
    session registered on it says the board was read: the replacement reads it, under the
    hub that gives it, and takes the task."""
    _two_slots_with_usage(monkeypatch, work=95, personal=10)
    below = _project_below(project)
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(below.root))
    task = _add_task(below, "api's own task")
    agent = _coder(below, task_id=task.id, account="2")
    monkeypatch.delenv("AISQUARE_TEAM_HUB")
    _with_transcript(agent, None)  # its session, registered on the board it read
    _as_080_left_it(agent)
    assert _row(agent.id).board == below.id != project.id

    if verb == "restart":
        again = fleet_service.restart(below, agent.label).started
    else:
        again = fleet_service.switch(below, agent.label).started

    assert (again.board, again.task_id) == (below.id, task.id)
    assert _last_window_hub(tmux) == str(below.root), "the hub that gives that board"
    assert again.launch_spec is not None and again.launch_spec.hub == str(below.root)


def test_a_080_agent_whose_session_never_registered_keeps_the_board_of_its_task(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 27, 2/7: a 0.8.0 row whose session never registered reads as its
    project's board, and round 26 read such a board as a default, from the checkout. But
    0.8.0 spawned for a task only on the board it gave the agent: a task on the project's own
    board says that board was read, and the replacement keeps it, and the task."""
    below = _project_below(project)
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(below.root))
    task = _add_task(below, "api's own task")
    agent = _coder(below, task_id=task.id)
    monkeypatch.delenv("AISQUARE_TEAM_HUB")
    _as_080_left_it(agent)
    with store_session() as store:
        assert agent.session_id is not None and store.get_session(agent.session_id) is None

    again = fleet_service.restart(below, agent.label).started

    assert (again.board, again.task_id) == (below.id, task.id)
    assert _last_window_hub(tmux) == str(below.root)


@pytest.mark.parametrize("ended", [False, True], ids=["live", "stopped"])
def test_a_080_row_a_restart_moves_to_the_checkouts_board_holds_the_checkout_against_forget(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    ended: bool,
) -> None:
    """Review of #259, round 38, 3/4: the rows that hold a board were filed by the board each
    recorded, and a 0.8.0 row of ``repo/api`` whose session never registered reads as its own
    project's. A restart does not take that board at its word (round 27, 2/7): with no task
    on it, the replacement reads the checkout's, ``repo``'s, and its ``launch`` adds ``repo``
    back. So ``project forget repo`` went through, and the next restart undid it. The row is
    filed under the board a restart starts it on, and the checkout's forget is refused. The
    checkout is worked out once a project (a ``git`` call), not once a row."""
    from aisquare.core import orchestrator

    below = _project_below(project)
    agent, other = _coder(below), _coder(below)
    for each in (agent, other):
        _as_080_left_it(each)
    asked: list[Path | None] = []
    real = orchestrator.directory_project

    def counted(cwd: Path | None = None) -> ProjectInfo:
        asked.append(cwd)
        return real(cwd)

    with store_session() as store:
        assert agent.session_id is not None and store.get_session(agent.session_id) is None
        if ended:
            store.end_fleet_agent(agent.id, exit_status=0)
        row = _row(agent.id)
        assert row.board == below.id, "the premise: read as its project's"
        assert fleet_service._board_and_hub(store, below, row, None) == (project.id, "")
        monkeypatch.setattr(orchestrator, "directory_project", counted)
        rows = project_service.reading_rows(store)
    assert [each.id for each in rows.get(project.id, [])] == [agent.id, other.id], rows
    assert below.id not in rows
    assert asked == [below.root], "once for the project"

    with pytest.raises(project_service.ProjectBusyError) as refused:
        project_service.forget_project(project)

    assert [reader.project.id for reader in refused.value.readers] == [below.id]
    with store_session() as store:
        assert store.get_project(project.id) is not None, "not forgotten"


def test_a_080_row_that_shows_it_read_its_own_board_is_filed_without_a_git_call(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 39, 1/9: the rows that hold a board worked out each 0.8.0 row's
    checkout (a ``git`` call) before asking the store whether the row read its own board,
    and a row that did keeps it whatever the checkout is. Every 0.8.0 row's session
    registered on its project's board, so each ``project forget``, prune and ``asq`` start
    ran ``git`` once a project. A session registered on the board, or a task on it, says
    so: no ``git``."""
    from aisquare.core import orchestrator

    below = _project_below(project)
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(below.root))
    task = _add_task(below, "api's own task")
    tasked = _coder(below, task_id=task.id)
    monkeypatch.delenv("AISQUARE_TEAM_HUB")
    registered = _coder(project)
    _with_transcript(registered, None)  # its session, registered on its project's board
    for each in (registered, tasked):
        _as_080_left_it(each)
    asked: list[Path | None] = []
    real = orchestrator.directory_project

    def counted(cwd: Path | None = None) -> ProjectInfo:
        asked.append(cwd)
        return real(cwd)

    monkeypatch.setattr(orchestrator, "directory_project", counted)
    with store_session() as store:
        rows = project_service.reading_rows(store)

    assert {board: [row.id for row in theirs] for board, theirs in rows.items()} == {
        project.id: [registered.id],
        below.id: [tasked.id],
    }
    assert asked == [], "the store said: no git"


def test_a_projects_rows_after_its_checkout_is_known_are_filed_with_no_lookup(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 40, 4/6: since round 39 the rows that hold a board asked the
    store of every 0.8.0 row (its session, then its task) before the checkout, and asked it
    again for each row the checkout had already settled: a project rooted at its own
    checkout, whose rows after its first ran no query before. Once a project's checkout is
    known, a row whose board it is is filed with no lookup."""
    from aisquare.core import orchestrator

    rows = [_coder(project) for _ in range(3)]
    for each in rows:
        _as_080_left_it(each)  # no session registered, no task: the checkout settles it
    asked: list[str] = []
    real = orchestrator.directory_project

    def counted(cwd: Path | None = None) -> ProjectInfo:
        asked.append("git")
        return real(cwd)

    def counting(name: str) -> Callable[..., Any]:
        read = getattr(SqliteStore, name)

        def lookup(self: SqliteStore, *args: Any, **kwargs: Any) -> Any:
            asked.append(name)
            return read(self, *args, **kwargs)

        return lookup

    for name in ("get_session", "get_task"):
        monkeypatch.setattr(SqliteStore, name, counting(name))
    monkeypatch.setattr(orchestrator, "directory_project", counted)
    with store_session() as store:
        filed = project_service.reading_rows(store)

    assert {board: [row.id for row in theirs] for board, theirs in filed.items()} == {
        project.id: [row.id for row in rows]
    }
    assert asked == ["get_session", "git"], "the first row alone asks"


@pytest.mark.parametrize("shape", ["shows nothing", "session", "task", "hub kept"])
def test_the_holds_and_a_restart_read_a_rows_board_by_one_rule(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
) -> None:
    """Review of #259, round 39, 3/9: the board a restart starts a replacement on
    (``_board_and_hub``) and the one the holds file its row under (``restart_board``) were
    each worked out by a copy of the rule, and the next change to one would leave ``project
    forget`` and prune judging holds by the old one. Both read one rule, and agree on every
    shape a row of a sub-folder project comes in."""
    below = _project_below(project)
    if shape == "task":
        monkeypatch.setenv("AISQUARE_TEAM_HUB", str(below.root))
        agent = _coder(below, task_id=_add_task(below, "api's own task").id)
        monkeypatch.delenv("AISQUARE_TEAM_HUB")
    else:
        agent = _coder(below)
    if shape == "session":
        _with_transcript(agent, None)
    if shape != "hub kept":
        _as_080_left_it(agent)

    with store_session() as store:
        row = _row(agent.id)
        session = store.get_session(agent.session_id) if agent.session_id else None
        board, _ = fleet_service._board_and_hub(store, below, row, session)
        filed = fleet_service.restart_board(store, row, {})

    assert filed == board == (below.id if shape in ("session", "task") else project.id)


@pytest.mark.parametrize("root", ["a symlink loop", "a mount that answers EIO"])
def test_a_080_row_whose_root_cannot_be_asked_keeps_its_board_and_fails_no_forget(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    root: str,
) -> None:
    """Review of #259, round 39, 2/9: the rows that hold a board work out the checkout of a
    0.8.0 row's project, and nothing caught what that raised. A root that became a symlink
    loop (``Path.resolve`` raises ``RuntimeError`` on 3.11 and 3.12), or a mount answering
    ``EIO``, ended ``project forget`` of any other project in a traceback. Such a root is no
    evidence: the row keeps its board, and the forget goes through."""
    from aisquare.core import orchestrator

    below = _project_below(project)
    agent = _coder(below)
    _as_080_left_it(agent)
    if root == "a symlink loop":
        shutil.rmtree(below.root)
        below.root.symlink_to(below.root)
    else:
        real = orchestrator.directory_project

        def unanswered(cwd: Path | None = None) -> ProjectInfo:
            if cwd == below.root:
                raise OSError(errno.EIO, os.strerror(errno.EIO), str(cwd))
            return real(cwd)

        monkeypatch.setattr(orchestrator, "directory_project", unanswered)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    with store_session() as store:
        unrelated = store.onboard_project(ProjectInfo(id=project_id_for(elsewhere), root=elsewhere))
        rows = project_service.reading_rows(store)

    project_service.forget_project(unrelated)

    filed = {board: [row.id for row in theirs] for board, theirs in rows.items()}
    assert filed == {below.id: [agent.id]}, "on every Python (round 41, 1/6)"
    with store_session() as store:
        assert store.get_project(unrelated.id) is None, "forgotten"


def test_a_080_row_whose_root_is_gone_keeps_its_board_and_holds_no_ancestor(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """Review of #259, round 41, 3/6: a root that is simply gone raises nothing, and its
    checkout was asked of it: ``git`` failed there, and ``find_project_root`` walked from the
    missing path to the nearest ancestor with a marker. For a project that was its own
    repository that ancestor is unrelated, as a dotfiles ``~/.git`` is to ``~/code/app``:
    the stopped row was filed under the ancestor's board, and its ``project forget`` was
    refused, naming ``app``. A gone root is no evidence of a checkout: the row keeps its
    board, and the ancestor's forget goes through."""
    app = project.root / "app"  # its own repository, below one that holds it (``~``)
    app.mkdir()
    _git("init", "-q", "-b", "main", cwd=app)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=app)
    own = team_project(app)
    with store_session() as store:
        store.onboard_project(own)
    agent = _coder(own)
    _as_080_left_it(agent)
    with store_session() as store:
        store.end_fleet_agent(agent.id, exit_status=0)
    remove_tree(app)  # a checkout with a commit: its objects are read-only

    with store_session() as store:
        rows = project_service.reading_rows(store)
    project_service.forget_project(project)

    assert {board: [row.id for row in theirs] for board, theirs in rows.items()} == {
        own.id: [agent.id]
    }
    with store_session() as store:
        assert store.get_project(project.id) is None, "forgotten"


@pytest.mark.skipif(not can_deny_reads(), reason="mode 000 does not stop this user from reading")
def test_a_080_row_whose_root_may_not_be_searched_keeps_its_board_on_every_python(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """Review of #259, round 42, 5/11: a root that cannot be asked keeps its row's board only
    while ``find_project_root`` raises for it, and from 3.14 ``Path.exists`` said False for
    a marker under ``chmod 000``: the walk went on to the repository above, the stopped row
    was filed under its board, and its ``project forget`` was refused. The markers are
    stat-ed, so it raises on every Python: the row keeps its board, the forget goes through."""
    below = _project_below(project)
    agent = _coder(below)
    _as_080_left_it(agent)
    with store_session() as store:
        store.end_fleet_agent(agent.id, exit_status=0)
    below.root.chmod(0)
    try:
        with store_session() as store:
            rows = project_service.reading_rows(store)
        project_service.forget_project(project)
    finally:
        below.root.chmod(0o700)

    assert {board: [row.id for row in theirs] for board, theirs in rows.items()} == {
        below.id: [agent.id]
    }
    with store_session() as store:
        assert store.get_project(project.id) is None, "forgotten"


def test_a_restart_the_holds_and_a_pause_read_a_gone_roots_own_board_not_its_ancestors(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 42, 3/11: round 41 kept a gone root's row on its board in
    ``restart_board`` alone. A restart (``_board_and_hub``) and a pause (``_pause_homes``)
    still asked ``directory_project`` of the deleted ``~/code/app``, which walked to the
    dotfiles ``~/.git``: the holds filed the row under ``app`` while its restart moved it onto
    ``~``'s board, and the pause went on ``~``'s. ``find_project_root`` walks from no start
    that is not a directory, and each reads ``app``'s own board."""
    monkeypatch.delenv("AISQUARE_TEAM_HUB", raising=False)
    app = project.root / "app"  # its own repository, below one that holds it (``~``)
    app.mkdir()
    _git("init", "-q", "-b", "main", cwd=app)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=app)
    own = team_project(app)
    with store_session() as store:
        store.onboard_project(own)
    agent = _coder(own)
    _as_080_left_it(agent)  # no session on its own board, no task: read from the checkout
    remove_tree(app)  # a checkout with a commit: its objects are read-only

    with store_session() as store:
        row = _row(agent.id)
        board_and_hub = fleet_service._board_and_hub(store, own, row, None)
        filed = fleet_service.restart_board(store, row, {})

    assert board_and_hub == (own.id, ""), "not the ancestor's board"
    assert filed == own.id
    assert fleet_service._pause_homes(own) == {own.id}


@pytest.mark.parametrize("root", ["a symlink loop", "under a folder it may not search"])
def test_a_pause_restart_or_switch_whose_board_its_root_cannot_give_is_a_fleet_error(
    tmux: FakeTmux,
    claude_on_path: Path,
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    root: str,
) -> None:
    """Review of #259, round 43, 4/7: a pause (``_pause_homes``), and a restart or switch of a
    row whose launch kept no hub (``_board_and_hub``), ask ``directory_project`` of the
    project's root, which raises ``OSError`` for a root that became a symlink loop, or one
    under a folder this user may not search. The ``fleet`` commands catch ``FleetError``
    alone: ``fleet pause`` ended in a traceback, ``--json`` printing nothing. Each is a
    ``FleetError`` that names the root."""
    if root == "under a folder it may not search" and not can_deny_reads():
        pytest.skip("mode 000 does not stop this user from reading")
    monkeypatch.delenv("AISQUARE_TEAM_HUB", raising=False)
    where = tmp_path / "locked" / "app"
    where.mkdir(parents=True)
    own = team_project(where)
    with store_session() as store:
        store.onboard_project(own)
    agent = _coder(own)
    _as_080_left_it(agent)  # no hub kept: its board is read from the checkout
    codename = _codename(own)
    if root == "a symlink loop":
        where.rmdir()
        where.symlink_to(where)
    else:
        where.parent.chmod(0)
    try:
        paused = runner.invoke(app, ["--json", "fleet", "pause", "--project", codename])
        with pytest.raises(FleetError, match="cannot be worked out") as restarted:
            fleet_service.restart(own, agent.label)
        with store_session() as store, pytest.raises(FleetError) as switched:
            fleet_service._board_and_hub(store, own, _row(agent.id), None)  # switch's
    finally:
        where.parent.chmod(0o700)

    assert paused.exit_code == 1, paused.output
    refused = json.loads(paused.stdout)
    assert refused["error"] == "fleet_error"
    assert f"its root {where} cannot be looked at" in refused["detail"]
    assert str(where) in str(restarted.value) and str(where) in str(switched.value)
    assert _row(agent.id).ended_at is None, "the restart stopped nothing"


@pytest.mark.parametrize("table", ["team_session", "team_task"])
def test_a_080_row_on_a_store_without_its_session_or_task_table_fails_no_forget_or_prune(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    tmp_path: Path,
    table: str,
) -> None:
    """Review of #259, round 40, 1/6: the rows that hold a board asked the store whether a
    0.8.0 row read its own board before the checkout (round 39, 1/9), and a store without
    ``team_session`` or ``team_task`` (doctor's damaged-store case, which every fleet read
    tolerates) raised "no such table" for a row with a session, or a task. ``project
    forget`` of any other project and ``prune`` ended in a traceback, and doctor's
    capture-off line was dropped without a word. That line was lost on such a store before
    too: a captured row's last activity (``project_activity``) named both tables. A table
    the store lacks shows nothing, and the row is read from the checkout."""
    from aisquare.services import diagnostics

    agent = _coder(project, task_id=_add_task(project, "Ship auth").id)
    if table == "team_session":
        _with_transcript(agent, None)  # its session, registered on its project's board
    _as_080_left_it(agent)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    with store_session() as store:
        unrelated = store.onboard_project(ProjectInfo(id=project_id_for(elsewhere), root=elsewhere))
        store.ensure_project(ProjectInfo(id="prj_gone", root=tmp_path / "gone"))  # captured
    raw = sqlite3.connect(str(paths.db_path()))
    try:
        raw.execute(f"DROP TABLE {table}")
        raw.commit()
    finally:
        raw.close()

    with store_session() as store:
        assert not store.has_table(table) and store.has_table("fleet_agent")
        rows = project_service.reading_rows(store)
    [check] = diagnostics._check_captured_projects()
    plan = project_service.prune_candidates(missing=True, worktrees=True)
    report = project_service.prune(plan, purge=False, missing=True, worktrees=True)
    project_service.forget_project(unrelated)

    assert {board: [row.id for row in theirs] for board, theirs in rows.items()} == {
        project.id: [agent.id]
    }
    assert check.detail.startswith("1 captured directory left from before project capture")
    assert report.dropped == ["prj_gone"]
    with store_session() as store:
        assert store.get_project(unrelated.id) is None, "forgotten"


@pytest.mark.parametrize("verb", ["restart", "switch"])
def test_a_replacement_that_could_not_take_the_task_is_refused_before_the_stop(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    verb: str,
) -> None:
    """Review of #259, round 27, 2/7: the task was checked on the board the row recorded,
    and the replacement looked it up on the board it would read, after the stop. Where the
    two differ (here a board whose project the store no longer has, so the replacement
    reads the board this process gives), the agent was stopped and then refused its
    replacement. The task is checked on the replacement's board, before anything is
    stopped."""
    _two_slots_with_usage(monkeypatch, work=95, personal=10)
    task = _add_task(project, "Ship auth")
    agent = _coder(project, task_id=task.id, account="2")
    _with_transcript(agent, None)
    _as_080_left_it(agent)
    raw = sqlite3.connect(str(paths.db_path()))
    try:
        for table, column, key in (
            ("fleet_agent", "board", agent.id),
            ("team_task", "project_id", task.id),
        ):
            raw.execute(f"UPDATE {table} SET {column} = 'prj_gone' WHERE id = ?", (key,))
        raw.commit()
    finally:
        raw.close()
    assert _row(agent.id).board == "prj_gone"

    with pytest.raises(FleetError, match="belongs to another project's board"):
        if verb == "restart":
            fleet_service.restart(project, agent.label)
        else:
            fleet_service.switch(project, agent.label)

    assert tmux.typed == [] and tmux.killed == [], "no /exit typed, no window killed"
    assert len(tmux.spawned) == 1
    with store_session() as store:
        live = store.fleet_agent_by_label(project.id, agent.label, live_only=True)
    assert live is not None and live.id == agent.id
    assert _session_state(agent.session_id or "") == "working", "the session is not marked"


@pytest.mark.parametrize("with_task", [True, False], ids=["with a task", "without one"])
@pytest.mark.parametrize("verb", ["restart", "switch"])
def test_a_080_agent_spawned_under_a_hub_forgotten_since_is_replaced_on_that_hub(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verb: str,
    with_task: bool,
) -> None:
    """Review of #259, round 31, 3/7: a row 0.8.0 spawned under hub H keeps no hub in its
    launch, and the v32 backfill recorded H, its session's board. With H forgotten since
    (``project forget``, to tidy the list), H counted as a board the store does not hold, so a
    restart or a switch from a shell without the hub took the caller's board and hub: with a
    task on H it was refused ("belongs to another project's board"), and without one the
    replacement read P's checkout board, off the board its manager reads. H's tasks and
    events are kept and the agent reads it, as Remote reads it since round 30, 2/8."""
    _two_slots_with_usage(monkeypatch, work=95, personal=10)
    (tmp_path / "hub").mkdir()
    with store_session() as store:
        hub = store.onboard_project(team_project(tmp_path / "hub"))
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(hub.root))
    task = _add_task(hub, "the hub's task") if with_task else None
    agent = _coder(project, task_id=None if task is None else task.id, account="2")
    monkeypatch.delenv("AISQUARE_TEAM_HUB")
    assert agent.launch_spec is not None and agent.board == hub.id
    legacy = json.loads(agent.launch_spec.model_dump_json())
    del legacy["hub"]  # as 0.8.0 wrote it; the backfill recorded the board
    raw = sqlite3.connect(str(paths.db_path()))
    try:
        raw.execute(
            "UPDATE fleet_agent SET launch_spec = ? WHERE id = ?", (json.dumps(legacy), agent.id)
        )
        raw.commit()
    finally:
        raw.close()
    with store_session() as store:
        store.forget_project(hub.id)
        assert store.get_project(hub.id) is None, "forgotten"

    if verb == "restart":
        again = fleet_service.restart(project, agent.label).started
    else:
        again = fleet_service.switch(project, agent.label).started

    assert (again.board, again.task_id) == (hub.id, None if task is None else task.id)
    assert _last_window_hub(tmux) == str(hub.root), "the hub that gives that board"


def test_a_fleet_row_from_before_v32_reads_its_sessions_board(hubbed: _Hubbed) -> None:
    """v32's backfill: a row recorded before the board was reads the board of the session
    bound to it, where its hooks registered, else its own project's."""
    hubbed.monkeypatch.delenv("AISQUARE_TEAM_HUB")
    unbound = fleet_service.spawn(hubbed.fleet, "coder", worktree=False).agent
    raw = sqlite3.connect(str(paths.db_path()))
    try:
        raw.executescript(
            "UPDATE fleet_agent SET board = NULL; "  # what a v31 store's rows hold
            "ALTER TABLE fleet_agent DROP COLUMN board; PRAGMA user_version = 31;"
        )
    finally:
        raw.close()

    core_store.open_store().close()

    assert _row(hubbed.coder.id).board == hubbed.hub.id, "its session's"
    assert _row(unbound.id).board == hubbed.fleet.id, "no session registered: its project's"


def test_a_fleet_row_an_older_build_writes_into_a_v32_store_reads_its_sessions_board(
    hubbed: _Hubbed,
) -> None:
    """Review of #259, round 26, 9/11: 0.8.0 opens a v32 store as it is and writes no board,
    and a row with none was read as its own project's, where the backfill gives a row from
    before v32 its session's. Under a hub its exit went to the fleet project's board and its
    pause was set there, neither where its manager reads. Read, it is the backfill's answer:
    its session's board, else its project's."""
    hubbed.monkeypatch.delenv("AISQUARE_TEAM_HUB")
    unbound = fleet_service.spawn(hubbed.fleet, "coder", worktree=False).agent
    raw = sqlite3.connect(str(paths.db_path()))
    try:
        raw.execute("UPDATE fleet_agent SET board = NULL")  # what a 0.8.0 spawn writes
        raw.commit()
    finally:
        raw.close()

    assert _row(hubbed.coder.id).board == hubbed.hub.id, "its session's"
    assert _row(unbound.id).board == hubbed.fleet.id, "no session registered: its project's"
    with store_session() as store:
        listed = {row.id: row.board for row in store.fleet_agents(hubbed.fleet.id)}
        assert listed[hubbed.manager.id] == hubbed.hub.id, "every read, not one of them"
        by_session = store.fleet_agent_for_session(hubbed.coder_sid)
        assert by_session is not None and by_session.board == hubbed.hub.id


def test_of_two_managers_on_one_board_a_note_wakes_the_one_whose_root_holds_it_deepest(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 26, 5/11: with no hub, a project rooted at ``repo/api`` reads
    ``repo``'s board, as ``repo``'s own project does. A writer with no row wakes the manager
    that reads the board, and of several, the one whose root holds its directory: the oldest
    of those, so the phone's note to ``api``'s manager, written in ``repo/api``, inside both
    roots, woke ``repo``'s. The deepest root is the writer's project."""
    sub = project.root / "api"
    sub.mkdir()
    with store_session() as store:
        below = store.onboard_project(ProjectInfo(id=project_id_for(sub), root=sub))
    for each in (project, below):  # the outer one first: the older manager
        manager, sid = _spawned(each, "manager", None, tmux, monkeypatch)
        team_service.hook_session_start(sid, each.root, "startup")
        assert _row(manager.id).board == project.id, "the checkout's board, both"
    woken: list[str] = []

    def nudge(project_id: str, *, reason: str, board: str | None = None) -> bool:
        woken.append(project_id)
        return True

    monkeypatch.setattr(fleet_service, "nudge_manager", nudge)

    team_service.add_note("fyi", to_role="manager", cwd=sub)  # the phone's, in api's root
    team_service.add_note("fyi", to_role="manager", cwd=project.root)

    assert woken == [below.id, project.id]


def test_under_a_hub_a_manager_launched_by_hand_defers_its_wake_to_nobody_else(
    hubbed: _Hubbed,
) -> None:
    """Review of #259, round 26, 7/11: a manager's Stop that defers its wake-up
    (``stop_hook_active``) nudges its own row. A manager the human launched by hand in
    another project's folder has none, and was asked as a writer with no row: the one
    manager that reads the hub's board, the fleet project's, got the nudge for the other's
    turn typed into its pane. It wakes nobody; its news waits for its next prompt."""
    other = hubbed.fleet.root.parent / "by-hand"
    other.mkdir()
    _stranger(hubbed.monkeypatch, "manager")
    team_service.hook_session_start("sess-manager-by-hand", other, "startup")
    team_service.add_note(
        "which branch?", kind="question", to_role="manager", session_ref=hubbed.coder_sid
    )
    hubbed.woken.clear()

    team_service.hook_stop("sess-manager-by-hand", other, stop_hook_active=True)

    assert hubbed.woken == [], "no row of its own: nobody's pane"
    _become(hubbed.manager, hubbed.tmux, hubbed.monkeypatch, pid=PANE_PID, role="manager")
    team_service.hook_stop(hubbed.manager_sid, hubbed.fleet.root, stop_hook_active=True)
    assert hubbed.woken == [hubbed.fleet.id], "a manager with a row still wakes its own"


def test_a_nudge_wakes_a_manager_only_for_a_write_on_the_board_it_reads(hubbed: _Hubbed) -> None:
    """Review of #259, round 26, 4/11: a write woke the manager of the writer's row's project
    whatever board that manager reads. Spawned from ``asq`` under the hub, the manager reads
    the hub's; a coder the human spawned from a shell without it reads its project's, where
    its question lands. The nudge woke the manager to the hub's delta, which does not hold
    the question, and it waited again. A manager is woken only for a write on its board,
    and so for an exit on the board the row recorded."""
    hubbed.monkeypatch.setattr(fleet_service, "nudge_manager", hubbed.nudge)
    stray = _spawned_without_the_hub(hubbed)
    assert stray.session_id is not None
    hubbed.monkeypatch.delenv("AISQUARE_TEAM_HUB")
    _become(stray, hubbed.tmux, hubbed.monkeypatch, pid=PANE_PID, role="coder")
    team_service.hook_session_start(stray.session_id, hubbed.fleet.root, "startup")
    hubbed.monkeypatch.setenv("AISQUARE_TEAM_HUB", str(hubbed.hub.root))
    assert stray.board == hubbed.fleet.id != hubbed.manager.board == hubbed.hub.id
    hubbed.tmux.set_command(hubbed.manager.pane_id, "claude")
    with store_session() as store:
        store.touch_session(hubbed.manager_sid, state="waiting")
    hubbed.tmux.typed.clear()

    team_service.add_note("which branch?", kind="question", session_ref=stray.session_id)
    hubbed.tmux.die(stray.pane_id, 1)
    fleet_service.reap()

    assert hubbed.tmux.typed == [], "its manager reads the hub's board, where neither is"
    team_service.add_note("which branch?", kind="question", session_ref=hubbed.coder_sid)
    assert hubbed.tmux.typed == [
        (hubbed.manager.pane_id, "paste", NUDGE_TEXT),
        (hubbed.manager.pane_id, "key", "Enter"),
    ], "a write on the board it reads does wake it"


@pytest.mark.parametrize("writer", ["the coder's task done", "the manager's deferred Stop"])
def test_under_a_hub_a_waiting_manager_is_woken_by_its_coders_writes_and_its_own_stop(
    hubbed: _Hubbed, writer: str
) -> None:
    """Review of #259, round 24, 4/6: round 23 moved the usage-limit nudge to the row's
    project, and every other nudge still named the session's board, under a hub the hub's,
    where no manager has a row: a coder's ``task done``, ``task review`` or question never
    woke the manager waiting on it, and a Stop that deferred its wake (``stop_hook_active``)
    lost it, the stall the deferral exists to prevent. Each nudges the manager of the
    writer's own row's project, and the manager wakes to the hub board's delta."""
    hubbed.monkeypatch.setattr(fleet_service, "nudge_manager", hubbed.nudge)
    if writer == "the manager's deferred Stop":
        team_service.add_note(
            "which branch?", kind="question", to_role="manager", session_ref=hubbed.coder_sid
        )
    hubbed.tmux.set_command(hubbed.manager.pane_id, "claude")
    with store_session() as store:
        store.touch_session(hubbed.manager_sid, state="waiting")
    hubbed.tmux.typed.clear()

    if writer == "the manager's deferred Stop":
        _become(hubbed.manager, hubbed.tmux, hubbed.monkeypatch, pid=PANE_PID, role="manager")
        team_service.hook_stop(hubbed.manager_sid, hubbed.fleet.root, stop_hook_active=True)
    else:
        team_service.finish_task(hubbed.task.id, session_ref=hubbed.coder_sid)

    assert hubbed.tmux.typed == [
        (hubbed.manager.pane_id, "paste", NUDGE_TEXT),
        (hubbed.manager.pane_id, "key", "Enter"),
    ]
    delta = team_service.hook_prompt_heartbeat(hubbed.manager_sid, hubbed.fleet.root)
    assert "which branch?" in delta or hubbed.task.title in delta, delta


@pytest.mark.parametrize(
    ("where", "case"),
    [pytest.param(where, run, id=f"{where}: {name}") for where, name, run in _HUB_RULE_CASES],
)
def test_under_a_hub_each_read_and_write_goes_where_the_rule_says(
    hubbed: _Hubbed, where: str, case: Any
) -> None:
    """docs/fleet.md, "Under a hub: one board, many fleet projects": a board write lands on
    the hub's board, a fleet lookup on the row's own project, and neither on the other."""
    expected = hubbed.hub.id if where == "board" else hubbed.fleet.id

    assert case(hubbed) == expected


def test_a_note_on_a_forgotten_hub_is_drained_there_and_named_by_it(hubbed: _Hubbed) -> None:
    """Review of #259, round 32, 7/9: ``add_note(board_id=...)`` named its board through a
    lookup that read a forgotten project as none. A note ``fleet tell`` filed on a hub
    forgotten since, where its agents still read, went there, but its receipt named the hub
    by its id, and its drain ran in the caller's directory (``spawn_drain(root=None)``),
    distilling the caller's board, not the hub's. Both are the hub's own."""
    from aisquare.services import distill as distill_service

    w = hubbed
    drained: list[tuple[Path | None, str | None]] = []
    w.monkeypatch.setattr(
        distill_service,
        "spawn_drain",
        lambda cwd=None, *, root=None, board=None: drained.append((root, board)),
    )
    with store_session() as store:
        store.forget_project(w.hub.id)  # as 0.8.1 before round 32 allowed, to tidy the list
        assert store.get_project(w.hub.id) is None

    team_service.add_note("ship it", to_role="coder-1", board_id=w.hub.id)

    assert drained == [(w.hub.root, w.hub.id)]
    receipt = team_service.last_delivery()
    assert receipt is not None and (receipt.board_id, receipt.board_name) == (w.hub.id, "hub")
    with store_session() as store:
        assert store.get_project(w.hub.id) is None, "still forgotten"


def _another_fleet_on_the_hub(w: _Hubbed, name: str = "other") -> tuple[ProjectInfo, str]:
    """A second fleet project whose manager, spawned under the hub, reads the hub's board as
    ``w.fleet``'s does; returns it and its manager's session."""
    root = w.fleet.root.parent / name
    root.mkdir()
    with store_session() as store:
        other = store.onboard_project(ProjectInfo(id=project_id_for(root), root=root))
    _, sid = _spawned(other, "manager", None, w.tmux, w.monkeypatch)
    team_service.hook_session_start(sid, root, "startup")
    return other, sid


def _manager_cycle(session_id: str, project: ProjectInfo) -> str:
    """The standing cycle a manager session is briefed with, as one text."""
    with store_session() as store:
        me = store.get_session(session_id)
    assert me is not None
    return "\n".join(team_service._role_cycle(me, project))


def test_under_a_hub_each_fleet_project_pauses_and_resumes_alone(hubbed: _Hubbed) -> None:
    """Review of #259, round 26, 1/11: since round 25 a hub project's ``fleet-paused`` signal
    is on the hub's board, which every fleet project under the hub reads, by one name. So
    ``fleet pause -P a`` paused ``b``'s manager too, and ``fleet shutdown -P a`` cleared the
    pause ``b``'s manager was still obeying, with ``--all`` saying ``b`` kept it. Each project's
    pause is its own signal on the shared board, and each manager is told its own's name."""
    other, other_sid = _another_fleet_on_the_hub(hubbed)
    mine = harness.pause_signal(hubbed.fleet.id, hubbed.hub.id)
    theirs = harness.pause_signal(other.id, hubbed.hub.id)
    assert mine != theirs and fleet_service.PAUSE_SIGNAL not in (mine, theirs)
    assert f"`{mine}`" in _manager_cycle(hubbed.manager_sid, hubbed.fleet)
    assert f"`{theirs}`" in _manager_cycle(other_sid, other)

    fleet_service.pause(other)

    assert _is_paused(other) and not _is_paused(hubbed.fleet)
    assert _paused_on(hubbed, other) == [hubbed.hub.id] and _paused_on(hubbed) == []
    fleet_service.pause(hubbed.fleet)
    fleet_service.resume(hubbed.fleet)
    assert _is_paused(other), "a resume of one is not the other's"
    fleet_service.pause(hubbed.fleet)

    report = fleet_service.shutdown(hubbed.fleet)

    assert report.paused_cleared == [fleet_service._name(hubbed.fleet)], report
    assert not _is_paused(hubbed.fleet)
    assert _is_paused(other), "the other project's manager still obeys its pause"


@pytest.mark.parametrize("hubless", [False, True], ids=["under the hub", "without the hub"])
@pytest.mark.parametrize("clear", ["resume", "shutdown"])
def test_a_pause_is_cleared_where_a_manager_that_has_stopped_read_it(
    hubbed: _Hubbed, clear: str, hubless: bool
) -> None:
    """Review of #259, round 29, 1/8: a pause's boards were the boards the project's live
    rows recorded. With a manager spawned under the hub and a coder from a plain shell, the
    pause went on both boards. Once the manager stopped, ``fleet shutdown`` and ``fleet
    resume`` asked only the coder's, the checkout's, and the hub's stayed on: the next
    manager spawned under the hub came up staffing nothing. The pause is on every board the
    project's rows recorded, whichever are live, and the caller's hub does not change that."""
    coder = _spawned_without_the_hub(hubbed)
    assert coder.board == hubbed.fleet.id
    fleet_service.pause(hubbed.fleet)
    assert sorted(_paused_on(hubbed)) == sorted([hubbed.hub.id, hubbed.fleet.id])
    fleet_service.stop(hubbed.fleet, fleet_service.MANAGER_LABEL, force=True)
    fleet_service.stop(hubbed.fleet, hubbed.coder.label, force=True)
    if hubless:
        _without_the_hub(hubbed)

    if clear == "resume":
        fleet_service.resume(hubbed.fleet)
    else:
        report = fleet_service.shutdown(hubbed.fleet, force=True)
        assert report.paused_cleared == [fleet_service._name(hubbed.fleet)], report

    assert _paused_on(hubbed) == [] and not _is_paused(hubbed.fleet)


def test_with_nothing_live_a_pause_goes_on_every_board_the_project_read(
    hubbed: _Hubbed,
) -> None:
    """Review of #259, round 29, 1/8: with nothing live, ``fleet pause`` went to the newest
    row's board, whatever its role: here a coder's, on the checkout's board, while only a
    manager obeys the pause, and the next one spawned under the hub reads the hub's."""
    coder = _spawned_without_the_hub(hubbed)
    for label in (fleet_service.MANAGER_LABEL, hubbed.coder.label, coder.label):
        fleet_service.stop(hubbed.fleet, label, force=True)

    fleet_service.pause(hubbed.fleet)

    assert sorted(_paused_on(hubbed)) == sorted([hubbed.hub.id, hubbed.fleet.id])


def test_under_a_hub_the_phones_task_done_wakes_the_manager_of_the_project_it_names(
    hubbed: _Hubbed,
) -> None:
    """Review of #259, round 26, 6/11: the phone's ``task done`` passed no directory, so of
    the hub's two managers, the one it woke was the one whose root holds the directory
    Remote runs in: started in the other project's folder, a done on this project's task
    woke the other's manager. It writes in the project's root, as the phone's note does."""
    from aisquare.services.remote_server import live_writes

    other, _ = _another_fleet_on_the_hub(hubbed)
    hubbed.monkeypatch.chdir(other.root)  # `asq`, and its Remote, started in the other's
    hubbed.woken.clear()

    live_writes().handlers["task/done"]({"ref": hubbed.task.id, "project": hubbed.fleet.id})

    assert _task_now(hubbed.task.id).status == "done"
    assert hubbed.woken == [hubbed.fleet.id]


def test_under_a_hub_a_task_write_with_no_row_wakes_the_manager_of_the_tasks_own_row(
    hubbed: _Hubbed, tmp_path: Path
) -> None:
    """Review of #259, round 27, 3/7: for a writer with no row, the manager a task write
    woke was picked by the writer's directory alone. Under a hub with two fleet projects,
    ``task done`` from ``~`` or an MCP client, outside both roots, woke nobody, and from the
    other project's folder woke the other's manager. A task write wakes the manager of the
    task's own row: the one spawned for it, else the row of the session that claimed it.
    The directory still picks for a task no row works."""
    other, _ = _another_fleet_on_the_hub(hubbed)
    spawned_for = _task(hubbed.fleet, "spawned for this one")
    fleet_service.spawn(hubbed.fleet, "coder", task_id=spawned_for.id, worktree=False)
    claimed = _task(hubbed.fleet, "claimed, then blocked and reopened")
    team_service.claim_task(claimed.id, session_ref=hubbed.coder_sid)
    loose = _task(hubbed.fleet, "nobody's")
    home = tmp_path / "home"
    home.mkdir()

    hubbed.woken.clear()
    team_service.finish_task(spawned_for.id, cwd=home)  # `task done 7` from `~`
    assert hubbed.woken == [hubbed.fleet.id], "the row spawned for it, retired by the done"

    hubbed.monkeypatch.chdir(other.root)  # and from the other project's folder
    writes: list[Callable[[], TeamTask]] = [
        lambda: team_service.review_task(hubbed.task.id),
        lambda: team_service.block_task(claimed.id, reason="needs a key"),
        lambda: team_service.reopen_task(claimed.id, reason="try again"),
    ]
    for write in writes:
        hubbed.woken.clear()
        write()
        assert hubbed.woken == [hubbed.fleet.id], "the claimant's row"

    hubbed.woken.clear()
    team_service.finish_task(loose.id)
    assert hubbed.woken == [other.id], "no row works it: the directory's, as for a note"


def test_under_a_hub_the_phones_task_done_naming_no_project_wakes_the_tasks_manager(
    hubbed: _Hubbed,
) -> None:
    """Review of #259, round 27, 3/7: with no ``project``, the phone's ``task done`` wrote in
    the current project's root, the pinned one's else Remote's directory, so a done on B's
    task woke A's manager while A was pinned, and a done by id read ``state.json`` and asked
    git, which it needs for nothing else. It wakes the task's own row's manager and asks
    no current project."""
    from aisquare.services import remote_server
    from aisquare.services.remote_server import live_writes

    other, _ = _another_fleet_on_the_hub(hubbed)
    hubbed.monkeypatch.chdir(other.root)  # the current project: the other one

    def no_current(ref: str | None) -> ProjectInfo:
        assert ref is not None, "a done by id asks for no current project"
        raise AssertionError(ref)

    hubbed.monkeypatch.setattr(remote_server, "_resolve_project", no_current)
    hubbed.woken.clear()

    live_writes().handlers["task/done"]({"ref": hubbed.task.id})

    assert _task_now(hubbed.task.id).status == "done"
    assert hubbed.woken == [hubbed.fleet.id]


def test_under_a_hub_the_phones_done_naming_no_project_picks_no_manager_by_remotes_folder(
    hubbed: _Hubbed,
) -> None:
    """Review of #259, round 28, 5/10: with no ``project``, the phone's ``task done`` passed
    no directory, and for a task no row works ``_reading_manager`` took this process's: the
    folder Remote (or ``asq``) was started in, the heuristic round 26, 6/11 rejected. Started
    in the other project's folder, a done from the phone woke the other's manager, whichever
    project the task was for. The phone has no directory: of the hub's two managers it picks
    neither, and the nudge goes to the board's own project, as for a writer outside every
    root. A shell's done there still picks by its directory."""
    from aisquare.services.remote_server import live_writes

    other, _ = _another_fleet_on_the_hub(hubbed)
    loose = _task(hubbed.fleet, "nobody's")
    shell = _task(hubbed.fleet, "nobody's either")
    hubbed.monkeypatch.chdir(other.root)  # `asq`, and its Remote, started in the other's
    hubbed.woken.clear()

    live_writes().handlers["task/done"]({"ref": loose.id})

    assert _task_now(loose.id).status == "done"
    assert hubbed.woken == [hubbed.hub.id], "no manager by Remote's folder"
    hubbed.woken.clear()
    team_service.finish_task(shell.id)  # `task done` from a shell in the other's folder
    assert hubbed.woken == [other.id]


def test_a_task_write_with_no_row_reads_the_live_rows_once(hubbed: _Hubbed) -> None:
    """Review of #259, round 28, 7/10: for a writer with no row, ``_nudge_target`` read every
    live fleet row for the task's own row, then again for the board's managers when no row
    worked the task: two reads on each phone, shell and MCP task write. One read serves
    both."""
    other, _ = _another_fleet_on_the_hub(hubbed)
    loose = _task(hubbed.fleet, "nobody's")
    reads: list[int] = []
    with store_session() as store:
        real = store.live_fleet_agents

        def counted() -> list[FleetAgent]:
            reads.append(1)
            return real()

        hubbed.monkeypatch.setattr(store, "live_fleet_agents", counted)
        woken = team_service._nudge_target(store, None, hubbed.hub.id, other.root, task=loose)

    assert woken == other.id, "the manager whose root holds the writer's directory"
    assert len(reads) == 1, reads


def test_under_a_hub_shutdown_all_keeps_the_pause_it_says_it_kept(hubbed: _Hubbed) -> None:
    """Review of #259, round 26, 1/11: under ``--all``, a project that was not confirmed down
    is named in ``paused_kept``, and another project's pass then cleared the hub's one signal
    anyway. Each pass clears its own project's pause alone."""
    other, _ = _another_fleet_on_the_hub(hubbed)
    fleet_service.pause(other)
    fleet_service.pause(hubbed.fleet)
    real = fleet_service._clear_pause

    def other_left_up(targets: Any, report: Any) -> None:
        report.incomplete_projects.append(other.id)  # a survivor: not confirmed down
        real(targets, report)

    hubbed.monkeypatch.setattr(fleet_service, "_clear_pause", other_left_up)
    report = fleet_service.shutdown(force=True)

    assert report.paused_kept == [fleet_service._name(other)], report
    assert _is_paused(other), "kept, as the report says"
    assert report.paused_cleared == [fleet_service._name(hubbed.fleet)]
    assert not _is_paused(hubbed.fleet)


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
    `hook_session_start` and took the whole board with it (review of #116). Both
    lookups, by the name and by the session: the hook reads the named row on its own
    since round 23 of #259, and with that read alone failing, the row recorded against
    the session still briefs it."""
    task = _task(project, "some task")
    _agent, first = _spawned(project, "coder", task.id, tmux, monkeypatch)

    def boom(self: SqliteStore, ref: str) -> FleetAgent | None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(SqliteStore, "get_fleet_agent", boom)
    monkeypatch.setattr(SqliteStore, "fleet_agent_for_session", boom)

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


def test_a_closed_assignment_leaves_the_row_and_keeps_the_label(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 7 of the second #135 review. ``retire_fleet_assignments`` claimed
    "nothing the operator sees changes", but the agent header rendered a
    ``task 01k…`` chip from ``task_id``. The assignment goes — the agent is no
    longer on that task — and the docstring says so; the label, named after the
    task at spawn, is what keeps the tie visible. The info line that replaced the
    header shows no task at all, before or after: the label is the tie there."""
    from aisquare.cli.ui.views.agent import info_line

    mine = _task(project, "the task this coder is for")
    agent, first = _spawned(project, "coder", mine.id, tmux, monkeypatch)
    short = mine.id.removeprefix("tsk_")[: fleet_service.TASK_SHORT]
    before = info_line(FleetAgentStatus(agent=agent)).plain
    assert agent.task_id == mine.id and agent.label == f"coder-{short}"
    assert "task " not in before and mine.id[-8:] not in before
    assert f"coder-{short}" in before

    team_service.hook_session_start(first, project.root, "startup")
    team_service.claim_task(mine.id, session_ref=first)
    team_service.finish_task(mine.id, note="shipped", session_ref=first)

    retired = _row(agent.id)
    assert retired.task_id is None, "the assignment goes with the task"
    after = info_line(FleetAgentStatus(agent=retired)).plain
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
        first = harness.role_cycle(role, "abcd1234", fleet=unscoped)[0]
        assert "task next --status review --as abcd1234" in first, (role, first)


def test_every_verifying_role_is_known_to_the_assignment(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The set of roles that VERIFY lives here while the cycles live in the
    harness, so a role added there can go missing here — `ui-tester` did, and its
    `[review]` assignment read as the coder's rework briefing: edit and re-submit
    someone else's work, against its own lane rule (review of #116, round 4).

    The harness is the source of truth: any role whose standing cycle pulls from
    the review pool is a verifier, whatever it is called — and no other role is.
    Pinned both ways (review of #116, round 5): a subset check caught the missing
    `ui-tester` and could not see the extra `validator`, whose cycle is one GATE
    note over the whole deliverable, never a verdict on a task. The roles whose
    verdict is a GATE note are the gating set, the same way."""
    from aisquare.core import harness
    from aisquare.services.team import _GATING_ROLES, _VERIFYING_ROLES

    cycles = {
        role: " ".join(harness.role_cycle(role, "sess-x", fleet=unscoped))
        for role in harness.ROLE_PROFILES
    }
    pulls_review = {role for role, cycle in cycles.items() if "task next --status review" in cycle}
    gates = {role for role, cycle in cycles.items() if 'note "GATE:' in cycle}
    assert pulls_review == _VERIFYING_ROLES, (
        f"pull from the review pool but get the rework briefing: "
        f"{sorted(pulls_review - _VERIFYING_ROLES)}; told to verify but never pull "
        f"review work: {sorted(_VERIFYING_ROLES - pulls_review)}"
    )
    assert gates == _GATING_ROLES, (
        f"gate but get a task briefing: {sorted(gates - _GATING_ROLES)}; told to gate "
        f"but write no GATE note: {sorted(_GATING_ROLES - gates)}"
    )
    assert not _VERIFYING_ROLES & _GATING_ROLES, "one role, one briefing"


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
    # And the line names no verdict command: the verifiers' verdicts differ (a
    # tester's `task done`/`task reopen`, a reviewer's PR review), and the
    # standing cycle carries each one.
    assert "task next --status review" not in board.split("sessions:")[0]


@pytest.mark.parametrize("status", ["todo", "review"])
def test_a_validator_spawned_for_a_task_is_told_to_gate_in_every_state(
    status: str,
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #116, round 5. The validator sat in the verifying set, so at
    `[review]` it was sent to "the verdict your standing cycle below describes"
    — a cycle that gates the whole deliverable once and describes no verdict on
    a task — and at `[todo]` to "take review work with your standing cycle", a
    cycle that takes none. Out of that set, it would have been handed a coder's
    claim or rework. It gates: the task is context, in every state."""
    task = _task(project, "assemble the release")
    if status == "review":
        monkeypatch.setenv("AISQUARE_ROLE", "coder")
        team_service.hook_session_start("sess-gate-coder", project.root, "startup")
        team_service.claim_task(task.id, session_ref="sess-gate-coder")
        team_service.review_task(task.id, session_ref="sess-gate-coder")
    _agent, first = _spawned(project, "validator", task.id, tmux, monkeypatch)

    board = team_service.hook_session_start(first, project.root, "startup")

    block = board.split("sessions:")[0]
    assert f"ASSIGNED TO YOU: {task.id} [{status}]" in block
    assert "You gate the assembled deliverable" in block
    assert f"aisquare task show {task.id}" in block
    for told in ("Claim it", "task claim", "awaits your verification", "not in review"):
        assert told not in block, f"{told!r} is not the validator's to hear: {block}"


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

    def boom(self: SqliteStore, session_id: str) -> FleetAgent | None:
        raise sqlite3.OperationalError("no such column: worktree")

    monkeypatch.setattr(SqliteStore, "fleet_agent_for_session", boom)

    picked = team_service.next_task(
        role="coder", claim=True, session_ref="sess-plain-r4", cwd=project.root
    )

    assert picked is not None and picked.id == older.id, "the pool order still works"


def test_a_fleet_row_task_next_cannot_read_is_logged_not_swallowed(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Review of #116, round 5. The fail-open catch took a `TypeError` from a
    changed signature as quietly as a locked database, and `task next` went back
    to oldest-first with nothing in the notes or the logs to say the preference
    had stopped working — the race the PR fixed, made invisible. Both doors log
    one line naming what they swallowed: the row the session is bound to, and
    the row the window's variable names (a cycle with no session). Each is read
    off its own call: a bound session's `task next` falls back to the name for
    the order too, so it logs the name door's line as well, and read together
    the call with no session proved nothing (review of #203, round 1)."""
    older = _task(project, "the older task")
    mine = _task(project, "the task this coder is for")
    _agent, first = _spawned(project, "coder", mine.id, tmux, monkeypatch)
    team_service.hook_session_start(first, project.root, "startup")

    def changed(self: SqliteStore, *args: object) -> FleetAgent | None:
        raise TypeError("fleet_agent_for_session() takes 1 positional argument")

    monkeypatch.setattr(SqliteStore, "fleet_agent_for_session", changed)
    monkeypatch.setattr(SqliteStore, "get_fleet_agent", changed)

    ours = "aisquare.services.team"

    def logged() -> list[logging.LogRecord]:
        lines = [record for record in caplog.records if record.name == ours]
        caplog.clear()
        return lines

    with caplog.at_level(logging.WARNING, logger=ours):
        caplog.clear()
        bound = team_service.next_task(role="coder", session_ref=first, cwd=project.root)
        bound_lines = logged()
        named = team_service.next_task(role="coder", cwd=project.root)
        named_lines = logged()

    assert bound is not None and bound.id == older.id, "still fail-open: the pool order"
    assert named is not None and named.id == older.id
    assert any(
        f"session {first}" in record.getMessage() and "TypeError" in record.getMessage()
        for record in bound_lines
    ), [record.getMessage() for record in bound_lines]
    assert [
        "AISQUARE_FLEET_AGENT" in record.getMessage() and "TypeError" in record.getMessage()
        for record in named_lines
    ] == [True], [record.getMessage() for record in named_lines]
    assert all(
        record.levelno == logging.WARNING and not record.exc_info
        for record in bound_lines + named_lines
    ), "one line each, no traceback"


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


@contextmanager
def _socket_file(name: str, *, listening: bool) -> Iterator[Path]:
    """A real socket file at ``<short dir>/tmux-<uid>/<name>``, the shape tmux makes:
    listening, or stale, as tmux leaves it when its server exits. Under ``/tmp``, as an
    AF_UNIX path is small (about 100 bytes). Skips where tmux cannot run."""
    import tempfile

    if not hasattr(socket, "AF_UNIX") or not hasattr(os, "getuid"):
        pytest.skip("AF_UNIX sockets and a POSIX uid are what tmux needs")
    base = Path(tempfile.mkdtemp(prefix="asq-", dir="/tmp"))
    path = base / f"tmux-{os.getuid()}" / name
    path.parent.mkdir()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(path))
        server.listen(1)
        if not listening:
            server.close()
        yield path
    finally:
        server.close()
        shutil.rmtree(base, ignore_errors=True)


def _recorded_at(agent: FleetAgent, path: Path) -> FleetAgent:
    """``agent`` as if its server had said, at spawn, that it listens at ``path``."""
    with store_session() as store:
        return store.upsert_fleet_agent(agent.model_copy(update={"socket_path": path}))


def test_shutdown_records_rows_lost_over_the_real_tmux_wrapper(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same absent-socket path against the REAL ``TmuxServer`` over a refusing
    runner (``_unreachable_server``), not the fake: every swallow of a non-zero exit
    is exercised, so ``answers()`` reading False here is tmux's own behaviour rather
    than the fake's ``running`` flag agreeing with the service. The socket file the
    row recorded is real and stale, as tmux leaves it, which shows no server."""
    asked: list[list[str]] = []

    def refusing(argv: Sequence[str], stdin: bytes | None) -> Completed:
        asked.append(list(argv))
        return Completed(1, "", _UNREACHABLE)

    dead = _real_server(refusing)
    coder = _coder(project)
    with _socket_file(coder.tmux_socket, listening=False) as stale:
        coder = _recorded_at(coder, stale)
        monkeypatch.setattr(fleet_service, "server", lambda config=None: dead)

        report = fleet_service.shutdown(project)

    assert [row.agent.id for row in report.recorded] == [coder.id]
    assert report.recorded[0].reason == f"no server answered on socket '{stale}'"
    assert report.stopped == [] and report.failed == []
    assert report.servers_absent == [coder.tmux_socket, str(stale)], "today's, and the row's"
    assert any(argv[1:3] == ["-S", str(stale)] for argv in asked), "asked where it recorded"
    assert report.sessions_killed == [] and report.sessions_absent == []


def test_shutdown_leaves_live_a_row_whose_socket_was_taken_from_under_its_server(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """Round 27 of #257: `uninstall` counts a row whose socket file is gone as running, as
    systemd-tmpfiles or logind can take it while the server runs on, and names `fleet
    shutdown`. That ended the row as lost because `tmux -L` found no server, stopping
    nothing, and the next `uninstall` went on under the running agent. Both now ask one
    rule: the row is left live with what to do, until `--forget` is the operator's word."""
    if not hasattr(socket, "AF_UNIX") or not hasattr(os, "getuid"):
        pytest.skip("AF_UNIX sockets and a POSIX uid are what tmux needs: uninstall asks one")
    coder = _coder(project)
    tmux.running = False  # no tmux client reaches the server…
    tmux.socket_file = False  # …because its socket file is gone, not because it exited

    plan = fleet_service.shutdown_plan(project)
    report = fleet_service.shutdown(project)

    assert "is gone" in plan.held[coder.id]
    assert report.recorded == [] and report.stopped == []
    assert [row.agent.id for row in report.failed] == [coder.id]
    reason = report.failed[0].reason
    assert "is gone, so no tmux client can reach the server" in reason
    assert "SIGUSR1" in reason and "--forget ends its row on your word" in reason
    assert report.incomplete_projects == [project.id], "not confirmed down"
    assert lifecycle.running_fleet()[0] != (), "uninstall still counts it: the two agree"
    with store_session() as store:
        assert [a.id for a in store.fleet_agents(project.id, live_only=True)] == [coder.id]

    forgotten = fleet_service.shutdown(project, forget=True)

    assert [row.agent.id for row in forgotten.recorded] == [coder.id]
    assert forgotten.recorded[0].reason.startswith("ended on your word (--forget): its server's")
    assert forgotten.failed == []
    assert fleet_service.shutdown_plan(project).agents == []
    assert lifecycle.running_fleet()[0] == ()


def test_shutdown_ends_a_row_from_before_v25_whose_socket_a_reboot_cleared(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 28 of #257: a 0.7.0 row recorded no boot, and a reboot clears its socket, so it
    read "gone", which may still run: `fleet shutdown --all --yes` left it live and exited
    1 until `--forget`. Created before this boot started, it is ended as lost."""
    coder = _coder(project)
    with store_session() as store:  # as 0.7.0 wrote it: neither its socket nor its boot
        coder = store.upsert_fleet_agent(
            coder.model_copy(update={"socket_path": None, "boot_id": None})
        )
    booted = coder.created_at + timedelta(minutes=5)  # this boot began after the row
    monkeypatch.setattr(tmux_core, "boot_started", lambda: booted)
    tmux.running = False
    tmux.socket_file = False  # what a reboot leaves: no file at all

    plan = fleet_service.shutdown_plan(project)
    report = fleet_service.shutdown(project)

    assert plan.held == {} and coder.id in plan.lost
    assert [row.agent.id for row in report.recorded] == [coder.id] and report.failed == []


def test_shutdown_forget_never_ends_a_row_whose_socket_answers(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 28 of #257: `--forget` ended every row its probe found no server for, and its
    plan hid them as recorded lost. A row whose recorded socket has a server listening (one
    that came up after tmux was asked) is one `uninstall` counts as running, so ending it
    let the next `uninstall` go on under it. The plan shows it left live, and so is it."""
    dead = _unreachable_server()  # tmux says no server…
    coder = _coder(project)
    with _socket_file(coder.tmux_socket, listening=True) as listening:  # …and one listens
        coder = _recorded_at(coder, listening)
        monkeypatch.setattr(fleet_service, "server", lambda config=None: dead)

        plan = fleet_service.shutdown_plan(project, forget=True)
        report = fleet_service.shutdown(project, forget=True)
        counted = lifecycle.running_fleet()[0]

    assert "a server came up on its socket" in plan.held[coder.id] and plan.lost == {}
    assert report.recorded == [] and [row.agent.id for row in report.failed] == [coder.id]
    assert "left live, which --forget does not end as a server answers there" in (
        report.failed[0].reason
    )
    assert counted != (), "uninstall counts it: the two agree"
    with store_session() as store:
        assert [a.id for a in store.fleet_agents(project.id, live_only=True)] == [coder.id]


def test_shutdown_stops_a_row_on_the_server_at_the_path_it_recorded(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 28 of #257: a row is asked of the server at the path its spawn recorded (``tmux
    -S``), not whatever ``tmux -L`` reaches from this shell. A shell without the spawning
    shell's ``TMUX_TMPDIR`` reaches another server on the same name, whose ``%1`` is a
    stranger: ``/exit`` was typed into it, or, were it younger than the row, the row was
    ended as gone under its running agent. Its own server is stopped and its session
    killed; the stranger is not touched."""
    coder = _coder(project)  # its own server: `tmux`, which holds its pane
    elsewhere = Path("/run/user/1000/tmux-1000") / coder.tmux_socket
    coder = _recorded_at(coder, elsewhere)
    stranger = FakeTmux()  # what `-L` reaches from this shell
    stranger.spawn_window("asq-someone", name="manager", cwd=project.root, command=["claude"])
    assert stranger.facts.keys() == tmux.facts.keys() == {coder.pane_id}, "the same %1"
    stranger.per_path[elsewhere] = tmux
    monkeypatch.setattr(fleet_service, "server", lambda config=None: stranger)

    plan = fleet_service.shutdown_plan(project)
    report = fleet_service.shutdown(project)

    assert plan.sessions == [f"{elsewhere}:asq-{_codename(project)}"] and plan.lost == {}
    assert [a.id for a in report.stopped] == [coder.id], report
    assert report.recorded == [] and report.failed == []
    assert (coder.pane_id, "literal", "/exit") in tmux.typed and tmux.killed == [coder.pane_id]
    assert report.sessions_absent == [f"{elsewhere}:asq-{_codename(project)}"], "with its window"
    assert stranger.typed == [] and stranger.killed == [] and stranger.killed_sessions == []
    assert not stranger.facts[coder.pane_id].dead, "the stranger runs on"


def _behind_a_stranger(
    tmux: FakeTmux, project: ProjectInfo, coder: FleetAgent, monkeypatch: pytest.MonkeyPatch
) -> tuple[FleetAgent, FakeTmux]:
    """``coder`` recorded at a path where ``tmux -L`` from this shell does not look, with its
    own server (``tmux``) answering only there, and the stranger ``-L`` reaches instead:
    another server on the same name, whose ``%1`` is another agent's (round 28 of #257)."""
    elsewhere = Path("/run/user/1000/tmux-1000") / coder.tmux_socket
    coder = _recorded_at(coder, elsewhere)
    stranger = FakeTmux()
    stranger.spawn_window("asq-someone", name="manager", cwd=project.root, command=["claude"])
    assert stranger.facts.keys() == tmux.facts.keys() == {coder.pane_id}, "the same %1"
    stranger.per_path[elsewhere] = tmux
    monkeypatch.setattr(fleet_service, "server", lambda config=None: stranger)
    return coder, stranger


def test_every_read_asks_a_row_of_the_server_at_the_path_it_recorded(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 29 of #257: after shutdown and stop, the listing (``fleet ls``, the UI's tick),
    ``status_of``, ``tell``, a restart's probe and ``reap`` — ``--server-down`` too — still
    asked a row's server by its socket's name (``tmux -L``). From a shell without the spawning
    shell's ``TMUX_TMPDIR`` that is another server on the same name, and its dead ``%1`` ended
    the row under its running agent. Each asks the server at the path the row recorded."""
    coder, stranger = _behind_a_stranger(tmux, project, _coder(project), monkeypatch)
    stranger.die(coder.pane_id, 1)  # the stranger's %1 has exited; the row's runs on
    _board_session(coder, "waiting")
    tmux.set_command(coder.pane_id, "claude")

    [listed] = fleet_service.list_agents(project)
    status = fleet_service.status_of(coder)
    reaped = fleet_service.reap(project)
    swept = fleet_service.reap(project, server_down=True)
    alive = fleet_service._pane_alive(coder)
    told = fleet_service.tell(project, coder.label, "please rebase")

    assert listed.state == status.state == "waiting", (listed, status)
    assert reaped.ended == reaped.lost == swept.ended == swept.lost == []
    assert alive and told.delivered, told
    assert tmux.typed == [
        (coder.pane_id, "paste", "please rebase"),
        (coder.pane_id, "key", "Enter"),
    ]
    assert stranger.typed == [] and stranger.killed == []
    with store_session() as store:
        row = store.get_fleet_agent(coder.id)
    assert row is not None and row.ended_at is None, "never ended on the stranger's dead %1"
    assert _events(project, "agent_exited") == []


def test_a_listed_status_carries_where_its_server_is_asked(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 29 of #257: asq's panes and the Manager tab's attach line asked `socket_at(agent)`
    on the UI thread for every open project each frame, and that reloads config.toml. The
    listing and `status_of` work it out once, with their config, and carry it."""
    coder, _ = _behind_a_stranger(tmux, project, _coder(project), monkeypatch)

    [listed] = fleet_service.list_agents(project)

    assert listed.socket_at == fleet_service.status_of(coder).socket_at == coder.socket_path
    with store_session() as store:  # a row from before v25 is asked by its socket's name
        named = store.upsert_fleet_agent(coder.model_copy(update={"socket_path": None}))
    [listed] = fleet_service.list_agents(project)
    assert listed.socket_at is None and fleet_service.status_of(named).socket_at is None


def test_a_symlink_loop_on_the_way_to_a_rows_socket_is_another_path_not_a_traceback(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Round 29 of #257: every read compares a row's recorded socket path with this shell's
    (`_address`), and pathlib on 3.11 and 3.12 raises `RuntimeError`, not `OSError`, for a
    symlink loop on the way. The listing, asq's tick and every command that reads a row
    ended in a traceback. The two now read as different paths, so the row is asked at the
    one it recorded."""
    looped = tmp_path / "loop" / "tmux-1000" / "asq"
    with suppress(OSError):
        (tmp_path / "loop").symlink_to(tmp_path / "loop")  # a loop, where links can be made
    coder = _recorded_at(_coder(project), looped)
    resolve = Path.resolve

    def resolve_as_3_12(self: Path, strict: bool = False) -> Path:
        if "loop" in self.parts:
            raise RuntimeError(f"Symlink loop from {str(self)!r}")
        return resolve(self, strict)

    monkeypatch.setattr(Path, "resolve", resolve_as_3_12)

    [listed] = fleet_service.list_agents(project)

    assert listed.agent.id == coder.id and listed.state == "waiting", listed
    assert fleet_service.socket_at(coder) == looped, "asked at the path it recorded"


def test_a_real_symlink_loop_on_the_way_to_a_rows_socket_is_another_path_on_every_python(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, tmp_path: Path
) -> None:
    """The test above emulates the `RuntimeError` 3.11 and 3.12 raise. From 3.13 pathlib's
    `resolve` raises nothing for a loop and hands back the path as far as it got, so on
    3.13 that emulation runs code no real loop reaches. A real loop here, nothing faked:
    whatever this Python makes of it, the row is listed and asked at the path it recorded
    (follow-up of #257)."""
    if os.name == "nt" or not can_symlink():
        pytest.skip("a symlink loop is a POSIX shape; NTFS links need a privilege")
    (tmp_path / "loop").symlink_to(tmp_path / "loop")
    looped = tmp_path / "loop" / "tmux-1000" / "asq"
    with pytest.raises(OSError):  # the premise: a real loop, which no stat gets through
        looped.stat()
    coder = _recorded_at(_coder(project), looped)

    [listed] = fleet_service.list_agents(project)

    assert listed.agent.id == coder.id and listed.state == "waiting", listed
    assert listed.socket_at == fleet_service.socket_at(coder) == looped, "the path it recorded"


def test_a_row_dead_on_its_own_server_is_ended_and_its_window_killed_there(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of round 29 of #257: a row whose pane died on its OWN server, at the path
    it recorded, is ended by the listing though ``tmux -L`` reaches a server where ``%1``
    runs, and stopping its 💤 row removes its window from its own server
    (``_kill_lingering_window``, ``_kill_dead``). The stranger's live ``%1`` is not touched."""
    coder, stranger = _behind_a_stranger(tmux, project, _coder(project), monkeypatch)
    tmux.die(coder.pane_id, 3)

    [listed] = fleet_service.list_agents(project)
    receipt = fleet_service.stop(project, coder.label)

    assert listed.agent.id == coder.id and listed.state == "exited", listed
    assert listed.agent.exit_status == 3, "its own pane's status, not the stranger's"
    assert receipt.agent.id == coder.id and tmux.killed == [coder.pane_id]
    assert stranger.killed == [] and not stranger.facts[coder.pane_id].dead


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
        assert [p.id for p in store.list_projects(all=True)] == [], (
            "hidden from every ordinary read"
        )

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

    with pytest.raises(FleetUnavailable, match="tmux is not installed") as refused:
        fleet_service.shutdown(project)

    assert "`--forget` ends their rows on your word" in str(refused.value), "the way out"
    tmux.installed = True
    assert [s.agent.id for s in fleet_service.list_agents(project)] == [coder.id]
    assert tmux.killed == [] and tmux.killed_sessions == []


def test_shutdown_forget_ends_every_row_unchecked_where_tmux_is_not_usable(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """Round 27 of #257: with tmux gone, a row that may still run blocked `uninstall`, and
    the one remedy it named, `fleet shutdown`, refused without tmux. A server outlives its
    program, so no tmux is no evidence; ``forget`` is the operator's word that the agents
    are not running, and ends each row unchecked, stopping and killing nothing. But not a
    row whose socket answers, which needs no tmux to ask: a server listens there, and
    `uninstall` counts it (round 28)."""
    coder = _coder(project)
    tmux.installed = False  # its server runs on, and its socket answers

    plan = fleet_service.shutdown_plan(project, forget=True)
    report = fleet_service.shutdown(project, forget=True)

    assert "and no usable tmux here can stop it" in plan.held[coder.id]
    assert report.recorded == [] and [row.agent.id for row in report.failed] == [coder.id]
    assert report.failed[0].reason.endswith(
        "left live, which --forget does not end as a server answers there: put a usable tmux "
        "back and run this again"
    )
    assert tmux.killed == [] and tmux.killed_sessions == [] and tmux.typed == []

    tmux.running = False  # its server exited, leaving the socket file: nobody listens
    plan = fleet_service.shutdown_plan(project, forget=True)
    report = fleet_service.shutdown(project, forget=True)

    assert plan.tmux_unusable is not None and "tmux is not installed" in plan.tmux_unusable
    assert [a.id for a in plan.agents] == [coder.id] and plan.sessions == []
    assert plan.held == {}
    assert [row.agent.id for row in report.recorded] == [coder.id]
    assert report.recorded[0].reason.startswith("ended on your word (--forget), unchecked: ")
    assert report.recorded[0].agent.exit_status is None
    assert report.stopped == [] and report.failed == [] and report.servers_absent == []
    assert tmux.killed == [] and tmux.killed_sessions == [] and tmux.typed == []
    with store_session() as store:
        assert store.fleet_agents(project.id, live_only=True) == []


def test_a_forget_plan_without_usable_tmux_names_the_rows_it_ends_as_lost(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """Round 29 of #257: with tmux unusable, `shutdown(forget=True)` ends every row its plan
    does not hold, unchecked, but the plan's `lost` stayed empty, so `fleet shutdown --forget
    --json` printed `"lost": {}` over the rows it was about to end. It names them, with the
    socket no tmux here could ask, and still none it leaves live."""
    coder = _coder(project)
    tmux.installed = False

    held = fleet_service.shutdown_plan(project, forget=True)  # its socket answers: left live
    tmux.running = False  # its server exited, leaving the socket file: nobody listens
    plan = fleet_service.shutdown_plan(project, forget=True)
    report = fleet_service.shutdown(project, forget=True)

    assert coder.id in held.held and held.lost == {}
    assert plan.tmux_unusable is not None and plan.held == {}
    assert plan.lost == {coder.id: coder.tmux_socket}
    assert [row.agent.id for row in report.recorded] == list(plan.lost)


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


#: ``TmuxServer.socket_path`` raises ``TmuxUnavailable`` where there is no uid (Windows
#: outside WSL): no tmux, so no socket for ``$TMUX`` to name and no guard to test.
_needs_a_socket_path = pytest.mark.skipif(
    not hasattr(os, "getuid"), reason="no uid, no tmux socket: the fleet needs a POSIX host"
)


@_needs_a_socket_path
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


def test_a_row_spawned_during_the_run_whose_agent_may_run_out_of_reach_is_left_live_held(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 7, 7/9: whether a row left live is held was worked out in
    `_shutdown_row`, apart from `_shutdown_verdict`, and the late rows' path built its row
    without it: a late row whose server lost its socket file was left live as a pane seen
    alive, which spares its project's session on that socket (`_spared`), against
    `ShutdownRow.held`'s own rule. Every row left live takes it from the one verdict."""
    monkeypatch.setattr(tmux_core, "boot_id", lambda: "b00t-1")
    fleet_service.spawn(project, "manager")
    late: list[FleetAgent] = []
    real_stop, real_kill = fleet_service._stop_row, fleet_service._kill_fleet_sessions

    def stop_and_spawn(*args: object, **kwargs: object) -> fleet_service.StopReceipt:
        if not late:  # the spawn lands while the manager is being stopped
            late.append(_coder(project))
        return real_stop(*args, **kwargs)  # type: ignore[arg-type]

    def kill_then_lose_the_file(*args: object, **kwargs: object) -> None:
        real_kill(*args, **kwargs)  # type: ignore[arg-type]
        tmux.running, tmux.socket_file = False, False  # its server lost its socket file

    monkeypatch.setattr(fleet_service, "_stop_row", stop_and_spawn)
    monkeypatch.setattr(fleet_service, "_kill_fleet_sessions", kill_then_lose_the_file)

    report = fleet_service.shutdown(project, force=True)

    (row,) = report.failed
    assert row.agent.id == late[0].id
    assert row.reason.startswith("it was spawned during the shutdown; ")
    assert "is gone, so no tmux client can" in row.reason and "— left live: " in row.reason
    assert row.held, "out of reach, as `_shutdown_row` holds such a row"
    assert fleet_service._spared(report, fleet_service.settings()) == (set(), set())


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
    assert _is_paused(project), "a surviving fleet keeps its standing order"
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
    assert _is_paused(project), "a surviving replacement keeps the standing order"
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
    assert _is_paused(project) and report.paused_kept == [project.root.name]
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
    assert _is_paused(project) and report.paused_kept == [project.root.name]
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
    assert _is_paused(project), "and the standing order stands"


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
    assert _is_paused(project) and report.paused_kept == [project.root.name]


@_needs_a_socket_path
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
        fleet_service._refuse_from_inside([fleet_service._Address("asq,shared")], config)

    # the truncation the old parse produced is NOT this socket — and a different
    # comma-free socket is not either
    monkeypatch.setenv("TMUX", f"{str(socket_path).split(',')[0]},4242,0")
    fleet_service._refuse_from_inside([fleet_service._Address("asq,shared")], config)
    monkeypatch.setenv("TMUX", "/tmp/tmux-0/somebody-elses,4242,0")
    fleet_service._refuse_from_inside([fleet_service._Address("asq,shared")], config)


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
    assert _is_paused(project) and report.paused_kept == [project.root.name]
    assert [s.agent.id for s in fleet_service.list_agents(project)] == [late[0].id], "still live"


def test_shutdown_does_not_revive_a_forgotten_project_while_checking_its_pause(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """Review of #121, round 7: `is_paused()` resolves the board through
    `ensure_project`, which clears a tombstone — so `shutdown --all` undid
    `project forget` on a registration with no agents, no session and no signal."""
    from aisquare.services import project as project_service

    with store_session() as store:  # `all`: the fixture's registration is a capture (#139)
        assert any(p.id == project.id for p in store.list_projects(all=True))
    project_service.forget(project.id)
    with store_session() as store:
        assert not any(p.id == project.id for p in store.list_projects(all=True)), "tombstoned"

    report = fleet_service.shutdown()  # every project: reads past tombstones on purpose

    assert report.paused_cleared == [] and report.paused_kept == []
    with store_session() as store:
        assert not any(p.id == project.id for p in store.list_projects(all=True)), "still forgotten"


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
    assert _is_paused(project), "and the pause is still the manager's standing order"

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
        "interrupted before anything was stopped; the rest of the fleet was left as it was"
    )
    assert report.stopped == [] and tmux.killed_sessions == []
    assert _is_paused(project)


_SHUTDOWN_PHASES = {
    "probe": ("_shutdown_probe", "before anything was stopped"),
    "stop-loop": ("_shutdown_row", "while stopping "),
    "kill-phase": ("_kill_fleet_sessions", "during the kill phase"),
    "late-scan": ("_record_late_rows", "during the final scan"),
    "pause-pass": ("_clear_pause", "while reconciling the fleet-paused signals"),
}


@pytest.mark.parametrize("phase", sorted(_SHUTDOWN_PHASES), ids=sorted(_SHUTDOWN_PHASES))
def test_an_interrupt_at_any_phase_of_shutdown_carries_the_report(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    """The INVARIANT (rounds 5 to 7 of #203, one phase per round): wherever the
    operator's Ctrl-C lands inside ``shutdown`` — before the first row, on a
    row, in the kill phase, the late scan or the pause pass — it raises
    ``FleetInterrupted(report)`` naming where, with the report of how far it
    got, and nothing further is killed. A Ctrl-C in the slow half used to be
    click's ``Aborted!`` with nothing printed."""
    first = _coder(project)
    seam, said = _SHUTDOWN_PHASES[phase]
    real = getattr(fleet_service, seam)

    def interrupt(*args: object, **kwargs: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(fleet_service, seam, interrupt)
    with pytest.raises(fleet_service.FleetInterrupted) as caught:
        fleet_service.shutdown(project, force=True)

    report = caught.value.report
    assert report.interrupted is not None and said in report.interrupted, report.interrupted
    if phase in ("kill-phase", "late-scan", "pause-pass"):
        assert [a.id for a in report.stopped] == [first.id], "the row already down is reported"
    monkeypatch.setattr(fleet_service, seam, real)
    assert tmux.killed_sessions == [], "nothing beyond the rows in hand was taken down"


def test_reap_with_server_down_survives_a_socket_that_cannot_be_asked(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 7 of #203. ``server_absent()`` raised a plain ``TmuxError`` for a
    wedged server (it caught the missing client alone), and ``reap`` called it
    unguarded — a traceback, no report, on the command ``doctor`` prescribes
    for a silent server. The predicate answers False now, and reap has the
    belt: nothing is marked lost on a question that could not be put."""
    old = FakeTmux()
    tmux.per_socket["asq-old"] = old
    _settings(monkeypatch, tmux_socket="asq-old")
    stale = _coder(project)
    old.running = False
    _settings(monkeypatch, tmux_socket="asq")

    def cannot_ask() -> bool:
        raise TmuxError("tmux display-message timed out after 30 s")

    monkeypatch.setattr(old, "server_absent", cannot_ask)
    report = fleet_service.reap(project, server_down=True)

    assert report.lost == [] and report.ended == [], "no evidence, nothing marked"
    assert _row(stale.id).ended_at is None


def test_shutdown_reports_a_store_damaged_mid_run(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 7 of #203 (on round 4's sweep). The store opens behind ``--yes`` —
    the late scan and the pause pass — sit behind handlers whose job is to
    turn a store failure into a report field. Damaged AFTER the rows are
    stopped (the file overwritten mid-run), both fields are set and the report
    is returned, never a traceback."""
    coder = _coder(project)
    real_kill = fleet_service._kill_fleet_sessions

    def kill_then_damage(*args: object, **kwargs: object) -> None:
        real_kill(*args, **kwargs)  # type: ignore[arg-type]
        paths.db_path().write_bytes(b"this is not a sqlite database, and open_store must say so")

    monkeypatch.setattr(fleet_service, "_kill_fleet_sessions", kill_then_damage)
    report = fleet_service.shutdown(project, force=True)

    assert [a.id for a in report.stopped] == [coder.id]
    assert report.late_scan_failed and "StoreUnopenable" in report.late_scan_failed
    assert report.pause_scan_failed and "StoreUnopenable" in report.pause_scan_failed
    assert report.incomplete_projects == [project.id], "an unscanned project is not confirmed down"


def test_a_clean_exit_that_cannot_announce_its_release_says_so_on_stderr(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Round 8 of #203. The agent's own clean ``SessionEnd`` — the commonest
    release path of all — threw ``Released.unannounced`` away, so the failure
    the class was built for survived exactly there: claims back in the pool,
    the board never told, the manager treating the task as held. The hook has
    no report to ride on, but it has stderr (Claude Code's hook log)."""
    mine = _task(project, "the task this coder is for")
    _agent, first = _spawned(project, "coder", mine.id, tmux, monkeypatch)
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
    team_service.hook_session_end(first, project.root, reason="exit")

    assert _task_now(mine.id).status == "todo" and _task_now(mine.id).claimed_by is None
    err = capsys.readouterr().err
    assert "could not announce" in err and mine.id in err and "locked" in err, err


def test_the_briefing_falls_back_to_the_sessions_own_row_when_the_named_one_is_refused(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 8 of #203. ``task next`` read the row bound to the session when the
    name in the environment was a row it may not have; the briefing's resolver
    gave up on the refused adopt and never consulted the session's own row —
    so the same session lost its ASSIGNED TO YOU block while ``task next`` still
    put its task first. One rule for both doors."""
    task_a = _task(project, "the task A is for")
    task_b = _task(project, "the task B is for")
    agent_a, session_a = _spawned(project, "coder", task_a.id, tmux, monkeypatch)
    team_service.hook_session_start(session_a, project.root, "startup")
    agent_b, session_b = _spawned(project, "coder", task_b.id, tmux, monkeypatch)
    team_service.hook_session_start(session_b, project.root, "startup")
    assert _row(agent_b.id).session_id == session_b, "the premise: B is bound to its own row"

    # Still B's process (its pane's pid), but the environment names A's row —
    # a row this process may not have, since A's pane is somebody else's.
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", agent_a.id)
    tmux.pids[agent_a.pane_id] = PANE_PID + 1
    board = team_service.hook_session_start(session_b, project.root, "compact")

    assert f"ASSIGNED TO YOU: {task_b.id}" in board, board
    assert _row(agent_a.id).session_id == session_a, "A's row was not taken"


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

    def exit_and_reuse_the_label(
        *args: object, **kwargs: object
    ) -> dict[fleet_service._Address, bool]:
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

    fleet_service._clear_pause([project, plain_project], report)

    assert sorted(report.paused_cleared) == sorted([project.root.name, plain_project.root.name])
    assert opens == 1, f"two projects, one connection — not {opens}"
    assert not _is_paused(project) and not _is_paused(plain_project)


def test_the_pause_pass_asks_git_for_no_project_and_opens_no_store_with_the_board_off(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    plain_project: ProjectInfo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #259, round 30, 7/8: the shutdown's pause pass worked out every target's homes
    (``fleet._pause_homes``, a ``git rev-parse`` with a 5 s timeout) before the store opened:
    a forgotten project's too, which the pass then skips, and all of them with the board off,
    where it stops at the first. ``directory_project`` asked git twice for a root that is not
    a checkout. Then git was asked once for each visible project, and not at all with the
    board off, where no store is opened either. Since round 31, 5/7 the pass finds each pause
    by its name and asks git for none."""
    from aisquare.core import workspace

    (tmp_path / "gone").mkdir()
    forgotten = team_project(tmp_path / "gone")
    with store_session() as store:
        store.onboard_project(forgotten)
        store.forget_project(forgotten.id)
    asked: list[Path] = []
    real_git = workspace.git_common_root

    def counted_git(start: Path) -> Path | None:
        asked.append(start.resolve())
        return real_git(start)

    monkeypatch.setattr(workspace, "git_common_root", counted_git)
    targets = [project, plain_project, forgotten]

    fleet_service.pause(project)
    asked.clear()
    report = fleet_service.ShutdownReport()
    fleet_service._clear_pause(targets, report)
    assert (asked, report.paused_cleared) == ([], [fleet_service._name(project)])

    opens = 0
    real_open = core_store.open_store

    def counting_open() -> ContextStore:
        nonlocal opens
        opens += 1
        return real_open()

    monkeypatch.setattr(core_store, "open_store", counting_open)
    monkeypatch.setenv("AISQUARE_TEAM", "0")
    report = fleet_service.ShutdownReport()
    fleet_service._clear_pause(targets, report)
    assert (asked, opens, report.pause_scan_failed, report.pause_unsettled) == ([], 0, None, [])


def test_a_root_the_pause_pass_cannot_look_at_is_cleared_like_any_other(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    plain_project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #259, round 30, 4/8: every project's homes (``fleet._pause_homes``) were worked
    out in one comprehension under the pass's outer ``try``, so one root that could not be
    looked at (``Path.exists`` raises ``PermissionError`` for a directory that cannot be
    searched, on Python 3.11 and 3.12) failed the pass for all: no pause was cleared, and
    with ``pause_unsettled`` empty the ``--all`` line named no ``fleet resume``. It was that
    project's failure then. Since round 31, 5/7 the pass looks at no root: a pause is found
    by its name, and both are cleared."""
    from aisquare.core import orchestrator

    fleet_service.pause(project)
    fleet_service.pause(plain_project)
    real = orchestrator.directory_project

    def unsearchable(cwd: Path | None = None) -> ProjectInfo:
        if cwd is not None and cwd.resolve() == project.root.resolve():
            raise PermissionError(13, "Permission denied", str(cwd))
        return real(cwd)

    monkeypatch.setattr(orchestrator, "directory_project", unsearchable)
    report = fleet_service.ShutdownReport()

    fleet_service._clear_pause([project, plain_project], report)
    monkeypatch.setattr(orchestrator, "directory_project", real)

    names = [fleet_service._name(p) for p in (project, plain_project)]
    assert sorted(report.paused_cleared) == sorted(names)
    assert not _is_paused(plain_project) and not _is_paused(project)
    assert (report.pause_unsettled, report.pause_scan_failed) == ([], None)
    assert report.incomplete_projects == []


def test_the_pause_builds_no_row(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 26, 10/11: the boards a pause goes to were found from every row
    the project ever had, the ended ones too (``fleet shutdown``'s pass over every visible
    project). Rows are never deleted. Since round 29, 1/8, every board they recorded is
    asked of the store distinct, and no row is built."""
    for _ in range(3):
        fleet_service.stop(project, _coder(project).label, force=True)
    _coder(project)
    reads: list[bool] = []
    real = core_store.SqliteStore.fleet_agents

    def spied(self: Any, project_id: str, *, live_only: bool = False) -> list[FleetAgent]:
        reads.append(live_only)
        return real(self, project_id, live_only=live_only)

    monkeypatch.setattr(core_store.SqliteStore, "fleet_agents", spied)

    fleet_service.pause(project)
    assert _is_paused(project)
    fleet_service.resume(project)
    assert not _is_paused(project)
    report = fleet_service.ShutdownReport()
    fleet_service._clear_pause([project], report)
    assert reads == [], "no row is built"
    with store_session() as store:
        assert store.fleet_agent_boards(project.id) == [project.id], "one board, four rows"


def test_a_resume_writes_off_only_where_the_pause_reads_on(hubbed: _Hubbed) -> None:
    """Review of #259, round 30, 5/8: ``fleet resume`` wrote ``off`` on every board one of
    the project's rows ever recorded and both of its homes, and each write is a ``signal``
    event in the delta of every session on that board. ``fleet-paused.<id>: off`` reached
    every agent on every hub the project ever ran under, where it had never been paused.
    Off is written only where the pause reads on; on goes to every board, as before."""
    w = hubbed

    def signal_events() -> dict[str, int]:
        with store_session() as store:
            return {
                board.id: sum(e.kind == "signal" for e in store.recent_events(board.id, limit=500))
                for board in (w.hub, w.fleet)
            }

    before = signal_events()
    with pytest.raises(FleetError, match="unknown session"):
        fleet_service.resume(w.fleet, session_ref="ses_nobody")
    fleet_service.resume(w.fleet)
    assert signal_events() == before, "paused nowhere: nothing written"

    team_service.set_signal(
        harness.pause_signal(w.fleet.id, w.fleet.id), "on", project_id=w.fleet.id
    )
    assert _paused_on(w) == [w.fleet.id]
    before = signal_events()
    fleet_service.resume(w.fleet, session_ref=w.manager_sid)
    assert _paused_on(w) == [] and not _is_paused(w.fleet)
    assert signal_events() == {w.hub.id: before[w.hub.id], w.fleet.id: before[w.fleet.id] + 1}

    fleet_service.pause(w.fleet)
    assert sorted(_paused_on(w)) == sorted([w.hub.id, w.fleet.id]), "on: every board"


@pytest.mark.parametrize("live", [True, False], ids=["its manager live", "nothing live"])
def test_a_pause_goes_only_where_a_manager_of_the_project_reads_or_would_next(
    hubbed: _Hubbed, tmp_path: Path, live: bool
) -> None:
    """Review of #259, round 32, 2/9: ``fleet pause`` set the pause on every board one of
    the project's rows ever recorded, live or ended. Each write is a ``signal`` event in
    the delta of every session on that board, and ``fleet resume``, which clears it where it
    reads on, then wrote ``off`` on each again: a project that ran under another hub a month
    ago put both in the next prompt of every agent still on that hub. The pause goes where a
    manager of the project reads, or would next: its newest manager row's board, live or
    stopped (a restart hands it on), and its homes. With nothing live and a caller without
    the hub, the stopped manager's hub is still paused."""
    w = hubbed
    root = tmp_path / "old-hub"
    root.mkdir()
    month_ago = datetime.now(tz=UTC) - timedelta(days=30)
    with store_session() as store:
        old = store.onboard_project(ProjectInfo(id=project_id_for(root), root=root))
        for role in ("manager", "coder"):
            store.upsert_fleet_agent(
                FleetAgent(
                    id=f"agt_old_{role}", project_id=w.fleet.id, label=f"old-{role}",
                    role=role, pane_id=f"%old-{role}", cwd=w.fleet.root, created_at=month_ago,
                    ended_at=month_ago + timedelta(days=1), board=old.id,
                )
            )  # fmt: skip
        assert old.id in store.fleet_agent_boards(w.fleet.id)
    if not live:
        for label in (fleet_service.MANAGER_LABEL, w.coder.label):
            fleet_service.stop(w.fleet, label, force=True)
        _without_the_hub(w)

    def signals_on_the_old_hub() -> int:
        with store_session() as store:
            return sum(e.kind == "signal" for e in store.recent_events(old.id, limit=500))

    fleet_service.pause(w.fleet)
    assert sorted(_paused_on(w)) == sorted([w.hub.id, w.fleet.id])
    assert _is_paused(w.fleet)
    fleet_service.resume(w.fleet)
    assert _paused_on(w) == [] and not _is_paused(w.fleet)
    assert signals_on_the_old_hub() == 0, "neither the pause nor the resume reached it"


@pytest.mark.parametrize("manager", [False, True], ids=["no manager yet", "a stopped manager"])
def test_a_pause_goes_where_the_live_coders_read(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manager: bool,
) -> None:
    """Review of #259, round 33, 4/10: since round 32 the pause goes on the newest manager
    row's board and the homes. A project with coders spawned under a hub and no manager yet
    had no manager row, and a pause from a plain shell went on the checkout's board alone;
    the manager spawned next from ``asq``, under the coders' hub, read none there and
    staffed. The boards its live rows recorded are paused too; an ended row's are not.
    Round 33 asked them only with no manager row at all, and a manager stopped last week on
    the checkout's board left the coders' hub unpaused (round 34, 2/10)."""
    hub_root, old_root = tmp_path / "hub", tmp_path / "old-hub"
    hub_root.mkdir()
    old_root.mkdir()
    month_ago = datetime.now(tz=UTC) - timedelta(days=30)
    with store_session() as store:
        hub = store.onboard_project(team_project(hub_root))
        old = store.onboard_project(ProjectInfo(id=project_id_for(old_root), root=old_root))
        store.upsert_fleet_agent(
            FleetAgent(
                id="agt_old", project_id=project.id, label="old-coder", role="coder",
                pane_id="%old", cwd=project.root, created_at=month_ago,
                ended_at=month_ago + timedelta(days=1), board=old.id,
            )
        )  # fmt: skip
        if manager:  # last week's, from a plain shell: on the checkout's board
            store.upsert_fleet_agent(
                FleetAgent(
                    id="agt_mgr", project_id=project.id, label=fleet_service.MANAGER_LABEL,
                    role="manager", pane_id="%mgr", cwd=project.root, created_at=month_ago,
                    ended_at=month_ago + timedelta(days=1), board=project.id,
                )
            )  # fmt: skip
            assert store.fleet_manager_board(project.id) == project.id
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(hub_root))
    for _ in range(2):
        fleet_service.spawn(project, "coder", worktree=False)
    monkeypatch.delenv("AISQUARE_TEAM_HUB")  # a plain shell

    fleet_service.pause(project)

    with store_session() as store:
        paused = [
            board
            for board in (hub.id, project.id, old.id)
            for state in [
                team_service._read_signal(store, board, harness.pause_signal(project.id, board))
            ]
            if state is not None and state.value == "on"
        ]
    assert sorted(paused) == sorted([hub.id, project.id]), "the coders' hub, and the checkout"


def test_a_pause_or_resume_as_a_prefix_of_two_sessions_is_refused_as_ambiguous(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, runner: CliRunner
) -> None:
    """Review of #259, round 31, 2/7: ``--as`` is read as a session id prefix, and one that
    names two sessions raised ``AmbiguousIdError``, a ``LookupError`` and no ``KeyError``:
    out of ``resume``'s own check with nothing paused, and out of ``set_signal`` on the
    ``on`` path. The CLI catches only ``FleetError``, so ``fleet resume --as ses_aa`` ended
    in a traceback, and ``--json`` printed nothing. It is refused, as an ambiguous task is,
    and nothing is written."""
    now = datetime.now(tz=UTC)
    with store_session() as store:
        for sid in ("ses_aa1", "ses_aa2"):
            store.upsert_session(
                TeamSession(
                    id=sid, project_id=project.id, role="manager", started_at=now,
                    last_seen_at=now,
                )
            )  # fmt: skip
    ambiguous = "session 'ses_aa' is ambiguous — use more of the id"

    for verb in (fleet_service.pause, fleet_service.resume):
        with pytest.raises(FleetError) as refused:
            verb(project, session_ref="ses_aa")
        assert str(refused.value) == ambiguous
        assert not _is_paused(project)
    fleet_service.pause(project)
    with pytest.raises(FleetError, match="ambiguous"):
        fleet_service.resume(project, session_ref="ses_aa")
    assert _is_paused(project), "still paused"

    result = runner.invoke(app, ["--json", "fleet", "resume", "-P", project.id, "--as", "ses_aa"])
    assert result.exit_code == 1, result.exception
    assert json.loads(result.stdout) == {"error": "fleet_error", "detail": ambiguous}


@pytest.mark.parametrize("paused", [False, True], ids=["nothing on", "paused"])
def test_a_resume_looks_its_session_up_once(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    paused: bool,
) -> None:
    """Review of #259, round 35, 7/8: since round 34 ``team.set_signals_in`` resolves
    ``--as`` itself, with no signal to write too, and refuses an unknown or ambiguous one as
    ``resume``'s own check did; that check, kept from when nothing was written with nothing
    on, looked the session up a second time on every resume. Once, and still refused."""
    now = datetime.now(tz=UTC)
    with store_session() as store:
        store.upsert_session(
            TeamSession(
                id="ses_one", project_id=project.id, role="manager", started_at=now,
                last_seen_at=now,
            )
        )  # fmt: skip
    if paused:
        fleet_service.pause(project)
    asked: list[str] = []
    real = SqliteStore.get_session

    def counted(store: SqliteStore, ref: str) -> TeamSession | None:
        asked.append(ref)
        return real(store, ref)

    monkeypatch.setattr(SqliteStore, "get_session", counted)

    fleet_service.resume(project, session_ref="ses_one")

    assert asked == ["ses_one"], asked
    assert not _is_paused(project)
    with pytest.raises(FleetError, match="unknown session 'ses_none'"):
        fleet_service.resume(project, session_ref="ses_none")


def test_a_pause_is_read_and_cleared_by_its_name_on_any_board_with_no_git(
    hubbed: _Hubbed, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 31, 5/7: reading or clearing a pause worked out its boards from
    the project's rows and homes (``fleet._pause_homes``, a ``git rev-parse`` with a 5 s
    timeout), for ``fleet resume``, ``is_paused`` and each visible project of ``fleet
    shutdown --all``, every directory 0.8.0 captured included. A signal's name says whose
    pause it is (``harness.pause_signal``), so it is found by that name on whichever board
    holds it, one read of the store and no git: on a board no row of the project names too,
    and never another project's pause on the same board."""
    from aisquare.core import workspace

    w = hubbed
    root = tmp_path / "elsewhere"
    root.mkdir()
    with store_session() as store:
        elsewhere = store.onboard_project(ProjectInfo(id=project_id_for(root), root=root))
        assert elsewhere.id not in store.fleet_agent_boards(w.fleet.id)
    for board in (w.hub.id, elsewhere.id):
        team_service.set_signal(harness.pause_signal(w.fleet.id, board), "on", project_id=board)
    team_service.set_signal(harness.PAUSE_SIGNAL, "on", project_id=w.hub.id)  # the hub's own
    asked: list[Path] = []
    real_git = workspace.git_common_root

    def counted_git(start: Path) -> Path | None:
        asked.append(start)
        return real_git(start)

    monkeypatch.setattr(workspace, "git_common_root", counted_git)

    assert _is_paused(w.fleet)
    assert not _is_paused(elsewhere), "the fleet's pause there is not its own"
    fleet_service.resume(w.fleet)
    assert not _is_paused(w.fleet)
    with store_session() as store:
        name = harness.pause_signal(w.fleet.id, elsewhere.id)
        there = team_service._read_signal(store, elsewhere.id, name)
    assert there is not None and there.value == "off", "off on the unrecorded board too"
    for board in (w.hub.id, elsewhere.id):
        team_service.set_signal(harness.pause_signal(w.fleet.id, board), "on", project_id=board)
    report = fleet_service.ShutdownReport()
    fleet_service._clear_pause([w.fleet, elsewhere], report)

    assert asked == [], "no git"
    assert report.paused_cleared == [fleet_service._name(w.fleet)]
    assert not _is_paused(w.fleet)
    assert _is_paused(w.hub), "the hub's own pause, on the same board, is kept"


def test_shutdown_clears_the_fleet_paused_signal(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """`pause` then `shutdown` used to leave the signal set, so the next
    `fleet spawn manager` came up staffing nothing (the manager's instructions say to
    spawn nothing while it is set) with no output from either command naming it."""
    _coder(project)
    fleet_service.pause(project)
    assert _is_paused(project)

    report = fleet_service.shutdown(project, force=True)

    assert not _is_paused(project)
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

    def probe_then_refuse(*args: object, **kwargs: object) -> dict[fleet_service._Address, bool]:
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


@pytest.mark.parametrize(
    "blob",
    ["not json", "[1, 2]", '{"value": "on", "seq": "x"}', '{"value": "on", "updated_at": 7}'],
    ids=["not JSON", "not an object", "a bad seq", "a bad updated_at"],
)
def test_a_signal_that_does_not_parse_holds_up_only_the_project_whose_pause_it_is(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    tmp_path: Path,
    runner: CliRunner,
    blob: str,
) -> None:
    """Review of #259, round 32, 6/9: since round 31 every board's signals are read in one
    pass, and one blob that did not parse raised for every caller: ``fleet resume`` of any
    project ended in a traceback, and the shutdown pass put every visible project in
    ``pause_unsettled``, none confirmed down. Another project's blob that does not parse is
    left out; the project whose pause it is gets a refusal that names it."""
    root = tmp_path / "other"
    root.mkdir()
    with store_session() as store:
        other = store.onboard_project(ProjectInfo(id=project_id_for(root), root=root))
        theirs = harness.pause_signal(other.id, other.id)
        store.set_meta(f"signal/{other.id}/{theirs}", blob)
        store.set_meta("signal/prj_elsewhere/deploy", blob)  # nobody's pause
    fleet_service.pause(project)
    assert _is_paused(project) and not _is_paused(other)

    resumed = runner.invoke(app, ["--json", "fleet", "resume", "-P", project.id])

    assert resumed.exit_code == 0, resumed.output
    assert not _is_paused(project)
    with pytest.raises(FleetError) as refused:
        fleet_service.resume(other)
    assert str(refused.value) == f"cannot clear the pause: {theirs} on {other.id} could not be read"
    fleet_service.pause(project)
    report = fleet_service.ShutdownReport()
    fleet_service._clear_pause([project, other], report)
    assert report.paused_cleared == [fleet_service._name(project)], report
    assert [p.id for p in report.pause_unsettled] == [other.id]
    name = fleet_service._name(other)
    assert report.pause_scan_failed == f"{name}: could not be read ({theirs} on {other.id})"
    assert other.id in report.incomplete_projects and project.id not in report.incomplete_projects


@pytest.mark.parametrize("blob", ["not json", "[1, 2]", '{"value": "off", "seq": null}'])
def test_a_pause_whose_own_signal_does_not_parse_on_a_board_it_goes_to_sets_none(
    hubbed: _Hubbed, runner: CliRunner, blob: str
) -> None:
    """Review of #259, round 33, 1/10: round 32 refused a resume whose own pause did not
    parse, but ``fleet pause`` set each board in turn, and the write reads the value it
    replaces: the blob on the board sorted last raised a traceback there, ``--json`` printed
    nothing, and the boards before it were left paused. Every board is asked before any is
    written; a blob that does not parse on one is refused, naming it, and none is set."""
    w = hubbed
    last = max(w.hub.id, w.fleet.id)  # the boards are set in this order
    name = harness.pause_signal(w.fleet.id, last)
    with store_session() as store:
        store.set_meta(f"signal/{last}/{name}", blob)

    with pytest.raises(FleetError) as refused:
        fleet_service.pause(w.fleet)
    assert str(refused.value) == f"cannot set the pause: {name} on {last} could not be read"
    paused = runner.invoke(app, ["--json", "fleet", "pause", "-P", w.fleet.id])
    assert paused.exit_code != 0 and isinstance(paused.exception, SystemExit), paused.output
    assert "could not be read" in json.loads(paused.stdout)["detail"]
    first = min(w.hub.id, w.fleet.id)
    with store_session() as store:
        before = team_service._read_signal(store, first, harness.pause_signal(w.fleet.id, first))
    assert before is None, "nothing was set, on the board before it either"

    other = "prj_elsewhere"
    with store_session() as store:
        store.set_meta(f"signal/{last}/{name}", "{}")  # repaired by hand
        store.set_meta(f"signal/{other}/{harness.pause_signal(w.fleet.id, other)}", blob)
    fleet_service.pause(w.fleet)  # its own, on a board it is not set on: no concern
    assert sorted(_paused_on(w)) == sorted([w.hub.id, w.fleet.id])


class _RefusedOn:
    """A store's connection that refuses ``board``'s signal event as a lock does (or with
    ``message`` and SQLite's ``code``), in the transaction the boards sorted before it have
    already written in."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        board: str,
        message: str = "database is locked",
        code: int | None = None,
    ) -> None:
        self._conn, self._board, self._message, self._code = conn, board, message, code

    def execute(self, sql: str, params: Sequence[object] = ()) -> sqlite3.Cursor:
        if sql.startswith("INSERT INTO team_event") and params[1] == self._board:
            refused = sqlite3.OperationalError(self._message)
            if self._code is not None:
                refused.sqlite_errorcode = self._code
            raise refused
        return self._conn.execute(sql, params)

    def __enter__(self) -> sqlite3.Connection:
        return self._conn.__enter__()

    def __exit__(self, *exc: Any) -> bool:
        return bool(self._conn.__exit__(*exc))


@pytest.mark.parametrize("verb", ["pause", "resume"])
def test_a_pause_or_resume_the_store_refuses_on_a_later_board_writes_none(
    hubbed: _Hubbed, runner: CliRunner, verb: str
) -> None:
    """Review of #259, round 34, 5/10: round 33 made a pause all-or-nothing only when a
    signal blob did not parse. Each board's was still one ``set_signal`` in a store of its
    own, so a store error on the board sorted last (``database is locked`` while many
    agents' hooks write) left the board before it paused, and ``fleet pause`` ended in a
    traceback, ``--json`` printing nothing. Every board's is written in one transaction in
    the store the boards were worked out in: refused, none is, and it is said. The lock is
    an ``OperationalError``, as SQLite's is: only a lock is said as "try again" (round 35,
    1/8)."""
    w = hubbed
    if verb == "resume":
        fleet_service.pause(w.fleet)
    before = sorted(_paused_on(w))
    last = max(w.hub.id, w.fleet.id)  # the boards are written in this order
    real = SqliteStore.add_signal_events

    def locked_on_last(
        store: SqliteStore, writes: Sequence[tuple[TeamEvent, str, dict[str, Any]]]
    ) -> list[TeamEvent]:
        conn = store._conn
        store._conn = _RefusedOn(conn, last)  # type: ignore[assignment]
        try:
            return real(store, writes)
        finally:
            store._conn = conn

    w.monkeypatch.setattr(SqliteStore, "add_signal_events", locked_on_last)
    signals = {board.id: _events(board, "signal") for board in (w.hub, w.fleet)}

    with pytest.raises(FleetError) as refused:
        getattr(fleet_service, verb)(w.fleet)
    doing = "set" if verb == "pause" else "clear"
    assert str(refused.value).startswith(f"cannot {doing} the pause: the store refused it")
    assert "database is locked" in str(refused.value) and "nothing was written" in str(
        refused.value
    )
    told = runner.invoke(app, ["--json", "fleet", verb, "-P", w.fleet.id])
    assert told.exit_code != 0 and isinstance(told.exception, SystemExit), told.output
    assert "the store refused it" in json.loads(told.stdout)["detail"]
    assert sorted(_paused_on(w)) == before, "the board before it was not written either"
    assert {board.id: _events(board, "signal") for board in (w.hub, w.fleet)} == signals

    w.monkeypatch.setattr(SqliteStore, "add_signal_events", real)
    getattr(fleet_service, verb)(w.fleet)
    assert sorted(_paused_on(w)) == ([] if verb == "resume" else sorted([w.hub.id, w.fleet.id]))


@pytest.mark.parametrize("verb", ["pause", "resume"])
@pytest.mark.parametrize(
    ("message", "code"),
    [
        ("database or disk is full", sqlite3.SQLITE_FULL),
        ("attempt to write a readonly database", sqlite3.SQLITE_READONLY),
        ("disk I/O error", sqlite3.SQLITE_IOERR),
    ],
    ids=["full", "readonly", "io"],
)
def test_a_pause_or_resume_a_full_or_readonly_store_refuses_is_said_with_no_traceback(
    hubbed: _Hubbed, runner: CliRunner, verb: str, message: str, code: int
) -> None:
    """Review of #259, round 36, 2/9: round 35 re-raised every store error of a pause but a
    lock, for the CLI's root group to say how to recover from damage. That group maps damage
    alone: a full disk, a readonly store or an I/O error ended in a traceback, ``--json``
    printing nothing, as before round 31. Each is a ``fleet_error`` that names it, nothing
    written, with no "try again", which a lock alone is told."""
    w = hubbed
    if verb == "resume":
        fleet_service.pause(w.fleet)
    before = sorted(_paused_on(w))
    last = max(w.hub.id, w.fleet.id)
    real = SqliteStore.add_signal_events

    def refused_on_last(
        store: SqliteStore, writes: Sequence[tuple[TeamEvent, str, dict[str, Any]]]
    ) -> list[TeamEvent]:
        conn = store._conn
        store._conn = _RefusedOn(conn, last, message, code)  # type: ignore[assignment]
        try:
            return real(store, writes)
        finally:
            store._conn = conn

    w.monkeypatch.setattr(SqliteStore, "add_signal_events", refused_on_last)

    with pytest.raises(FleetError) as refused:
        getattr(fleet_service, verb)(w.fleet)
    said = str(refused.value)
    assert message in said and said.endswith("nothing was written"), said
    told = runner.invoke(app, ["--json", "fleet", verb, "-P", w.fleet.id])
    assert told.exit_code != 0 and isinstance(told.exception, SystemExit), told.output
    payload = json.loads(told.stdout)
    assert payload["error"] == "fleet_error" and message in payload["detail"], payload
    assert sorted(_paused_on(w)) == before, "nothing was written"


def _zero_the_table(table: str) -> None:
    """Zero the root page of ``table`` in the store, its header kept: the store opens, and
    a query that reaches the table finds ``database disk image is malformed``."""
    raw = sqlite3.connect(str(paths.db_path()))
    raw.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    size = raw.execute("PRAGMA page_size").fetchone()[0]
    (root,) = raw.execute("SELECT rootpage FROM sqlite_master WHERE name = ?", (table,)).fetchone()
    raw.close()
    with paths.db_path().open("r+b") as db:
        db.seek((root - 1) * size)
        db.write(bytes(size))


@pytest.mark.parametrize("verb", ["pause", "resume"])
@pytest.mark.parametrize("damage", ["at-query", "at-open"])
def test_a_pause_or_resume_on_a_damaged_store_is_not_told_to_try_again(
    hubbed: _Hubbed, runner: CliRunner, verb: str, damage: str
) -> None:
    """Review of #259, round 35, 1/8: round 34 said every store error of a pause as "the
    store refused it — try again", a ``FleetError`` the CLI prints as it is. A store found
    damaged is one too: ``database disk image is malformed`` from the signals' table, or a
    ``StoreUnopenable`` from the store the pause opens, and a retry of it never works. Only
    a lock is said so; damage reaches the CLI's root group, which says to move the store
    aside, as before."""
    w = hubbed
    if verb == "resume":
        fleet_service.pause(w.fleet)
    if damage == "at-query":
        _zero_the_table("team_meta")
    else:
        paths.db_path().write_bytes(b"this is not a sqlite database, and open_store must say so")

    with pytest.raises(sqlite3.DatabaseError) as found:
        getattr(fleet_service, verb)(w.fleet)
    assert not isinstance(found.value, FleetError)
    if damage == "at-open":
        assert isinstance(found.value, core_store.StoreUnopenable)
        return  # the CLI resolves -P in a store of its own first, and that one says it
    assert core_store.is_corrupt_error(found.value), found.value
    told = runner.invoke(app, ["--json", "fleet", verb, "-P", w.fleet.id])
    assert told.exit_code != 0 and isinstance(told.exception, SystemExit), told.output
    said = json.loads(told.stdout)
    assert said["error"] == "store_damaged", said
    assert said["hint"] == core_store.damaged_store_recovery()
    assert "try again" not in told.stdout


def test_where_a_pause_goes_is_said_with_the_live_rows_boards_wherever_it_is_said() -> None:
    """Review of #259, round 35, 8/8: since round 34 (2/10) a pause goes on the boards a
    project's live rows recorded, manager or not, but ``pause``, ``_pause_boards`` and the
    two store reads it asks still said the old rule: a coder's board is not one, the live
    rows' boards only while the project has no manager row, a pause besides its homes on the
    manager's board alone. Someone who trusted them expected ``fleet pause`` to leave a live
    coder's hub alone. Each says the live rows' boards, as docs/fleet.md does."""
    said = {
        name: " ".join((doc or "").split())
        for name, doc in (
            ("pause", fleet_service.pause.__doc__),
            ("_pause_boards", fleet_service._pause_boards.__doc__),
            ("fleet_agent_boards", SqliteStore.fleet_agent_boards.__doc__),
            ("fleet_manager_board", SqliteStore.fleet_manager_board.__doc__),
        )
    }
    assert "the boards its live rows recorded" in said["pause"], said["pause"]
    assert "a live coder's is" in said["_pause_boards"]
    assert "a coder's board is not one" not in said["_pause_boards"]
    assert "while the project has no manager row" not in said["fleet_agent_boards"]
    assert "besides its manager's and its homes" in said["fleet_agent_boards"]
    assert "besides its homes and the boards its live rows recorded" in said["fleet_manager_board"]
    docs = " ".join(
        (Path(__file__).parents[1] / "docs" / "fleet.md").read_text(encoding="utf-8").split()
    )
    assert "running or not, the boards its running agents recorded, and the ones a spawn" in docs


def test_shutdown_reports_a_pause_signal_it_could_not_read_or_clear(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #121, round 9 (P2): a blanket ``suppress(Exception)`` around the
    per-project pause read and write left the signal ON with ``paused_kept=[]``,
    ``pause_scan_failed=null`` and exit 0 — the next manager still under orders to
    spawn nothing, and nothing in the report saying so. Both failures are surfaced
    the way the visible-projects lookup already is, and the report is preserved.
    """
    real_read = team_service.board_signals_in
    real_clear = fleet_service._clear_pause_signal

    def refuse(*args: object, **kwargs: object) -> bool:
        raise sqlite3.OperationalError("database is locked (fake)")

    _coder(project)
    fleet_service.pause(project)
    name = fleet_service._name(project)

    monkeypatch.setattr(team_service, "board_signals_in", refuse)  # the READ fails
    report = fleet_service.shutdown(project, force=True)
    monkeypatch.setattr(team_service, "board_signals_in", real_read)

    assert len(report.stopped) == 1, "the report is preserved, not discarded"
    assert report.pause_scan_failed is not None and "locked" in report.pause_scan_failed
    assert report.pause_scan_failed.startswith(f"{name}: could not be read")
    assert report.paused_cleared == [], "nothing is cleared on a signal that could not be read"
    assert report.paused_kept == [], "and nothing is CLAIMED kept either — it was never read"
    assert project.id in report.incomplete_projects, "an unread pause is not a project down"
    assert _is_paused(project), "the pause is in fact still on"
    assert [p.id for p in report.pause_unsettled] == [project.id], "its resume is owed"

    monkeypatch.setattr(fleet_service, "_clear_pause_signal", refuse)  # now the WRITE fails
    second = fleet_service.shutdown(project, force=True)
    monkeypatch.setattr(fleet_service, "_clear_pause_signal", real_clear)

    assert second.pause_scan_failed is not None and "locked" in second.pause_scan_failed
    assert "could not be cleared" in second.pause_scan_failed, "the WRITE is the one that failed"
    assert second.paused_kept == [name], "read ON, not cleared: kept"
    assert second.paused_cleared == []
    assert project.id in second.incomplete_projects
    assert _is_paused(project), "the signal that could not be written stays on"
    assert [p.id for p in second.pause_unsettled] == [project.id]
    # Negative control: with the store answering, the same run clears it and exits clean.
    third = fleet_service.shutdown(project, force=True)
    assert third.pause_scan_failed is None and third.paused_cleared == [name]
    assert third.incomplete_projects == [] and not _is_paused(project)
    assert third.pause_unsettled == []


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
    assert _is_paused(project), "every pause is kept"
    assert report.pause_scan_failed is not None and "locked" in report.pause_scan_failed
    assert report.late_scan_failed is None, "the scan itself ran; only the pause lookup failed"


def test_shutdown_clears_the_pause_by_the_target_id_not_a_redirected_board(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #121, round 8 (P2): with ``AISQUARE_TEAM_HUB`` pointing at project
    A, shutting down explicit project B resolved its pause through cwd and cleared
    A's signal instead — B reported in ``paused_cleared`` while A's agent kept its
    standing order removed. The pause is read and written on the board B's agents
    recorded at spawn (review of #259, round 25, 2/10), so the caller's hub cannot
    redirect it: B's coder was spawned with no hub, and reads B's own board."""
    hub_root = project.root.parent / "hub-project"
    hub_root.mkdir()
    hub = team_project(hub_root)
    with store_session() as store:
        store.onboard_project(hub)
    fleet_service.pause(hub)
    _coder(project)
    fleet_service.pause(project)
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(hub_root))  # cwd resolution now points at A
    assert _is_paused(project) and _is_paused(hub)

    report = fleet_service.shutdown(project, force=True)

    assert report.paused_cleared == [project.root.name], "B, not the hub"
    assert not _is_paused(project), "B's own signal is cleared"
    assert _is_paused(hub), "the hub's pause is untouched"


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

    def probe_then_deny(*args: object, **kwargs: object) -> dict[fleet_service._Address, bool]:
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
    # The ended row stays on the live listing while tmux still holds its window (#138:
    # the 💤 exited row the UI hangs Restart on) and leaves the listing with it.
    listed = fleet_service.list_agents(project)
    assert [(s.agent.id, s.state) for s in listed] == [
        (manager.id, "waiting"),
        (coder.id, "exited"),
    ]
    tmux.vanish(coder.pane_id)
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
        (manager.pane_id, "paste", NUDGE_TEXT),
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
    # #138: that listing already RECORDED the exit — seen on the row's own socket — so
    # reap has nothing left to end; the live row is still not lost.
    with store_session() as store:
        recorded = store.get_fleet_agent(dead.id)
    assert recorded is not None and recorded.exit_status == 3, "seen on its own socket"
    report = fleet_service.reap(project)
    assert report.ended == [], "the listing recorded it; reap finds nothing new"
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

    branch = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=merged.cwd)

    report = fleet_service.reap(project)

    assert report.worktrees_removed == [merged.cwd]
    assert not merged.cwd.exists() and unmerged.cwd.exists()
    assert report.branches_deleted == [(project.root, branch)], "main holds its tip"
    assert _git("branch", "--list", branch, cwd=project.root) == ""
    assert fleet_service.reap(project).worktrees_removed == []


@pytest.mark.skipif(not can_deny_reads(), reason="mode 000 does not stop this user from reading")
def test_a_reap_of_every_project_goes_on_past_a_root_or_tree_it_may_not_look_at(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, tmp_path: Path
) -> None:
    """Review of #259, round 43, 1/7: the worktree pass asked ``Path.exists`` of each root's
    ``.git`` and of each ended agent's tree, which raised ``PermissionError`` for one under a
    folder this user may not search up to 3.13 (and from 3.14 said False, passing it over in
    silence). One such project ended ``reap --all`` in a traceback, the projects after it
    unreaped. Each is kept, and said so, and the sweep goes on."""
    ended: dict[str, FleetAgent] = {}
    for name in ("locked/root", "trees", "fine"):
        root = project.root if name == "fine" else tmp_path / name
        if name != "fine":
            root.mkdir(parents=True)
            _git("init", "-q", "-b", "main", cwd=root)
            _git("commit", "-q", "--allow-empty", "-m", "init", cwd=root)
        here = team_project(root)
        with store_session() as store:
            store.onboard_project(here)
        ended[name] = fleet_service.spawn(here, "coder", label="coder-1").agent
        fleet_service.stop(here, "coder-1", force=True)
    locked = [tmp_path / "locked", ended["trees"].cwd.parent]
    for folder in locked:
        folder.chmod(0)
    try:
        report = fleet_service.reap(None)
    finally:
        for folder in locked:
            folder.chmod(0o700)

    assert report.worktrees_removed == [ended["fine"].cwd], "the sweep went on"
    assert dict(report.worktrees_kept) == {
        (tmp_path / "locked" / "root").resolve(): "not looked at, nor any tree in it: "
        "Permission denied",
        ended["trees"].cwd: "not looked at: Permission denied",
    }
    assert ended["trees"].cwd.exists() and (tmp_path / "locked" / "root").exists()


def _remote(project: ProjectInfo, tmp_path: Path) -> Path:
    """A bare remote for the project's repo, ``origin``, with ``main`` pushed to it."""
    bare = tmp_path / "remote.git"
    _git("init", "-q", "--bare", str(bare), cwd=tmp_path)
    _git("remote", "add", "origin", str(bare), cwd=project.root)
    _git("push", "-q", "origin", "main", cwd=project.root)
    return bare


def _commit(tree: Path, name: str) -> None:
    (tree / name).write_text(f"{name}\n", encoding="utf-8")
    _git("add", name, cwd=tree)
    _git("commit", "-q", "-m", name, cwd=tree)


def test_reap_removes_a_worktree_pushed_to_any_remote_branch_and_keeps_its_fleet_branch(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, tmp_path: Path
) -> None:
    """A coder that pushes onto an integration branch (``push HEAD:feat/…``) never merges
    into the default branch, and reap kept every such tree: 15 worktrees and 1.2 GB on one
    night. Pushed anywhere, with nothing uncommitted, the tree holds nothing found only
    there — it goes. Its ``fleet/…`` branch stays, as main lacks its commits (review of
    #259, round 14)."""
    _remote(project, tmp_path)
    pushed = fleet_service.spawn(project, "coder", label="coder-pushed").agent
    _commit(pushed.cwd, "feature.txt")
    _git("push", "-q", "origin", "HEAD:feat/integration", cwd=pushed.cwd)
    branch = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=pushed.cwd)
    assert branch.startswith("fleet/"), branch
    fleet_service.stop(project, "coder-pushed", force=True)
    on_main = _git("branch", "--list", "--merged", "main", branch, cwd=project.root)
    assert on_main == "", "the premise: the commit is not on main"

    report = fleet_service.reap(project)

    assert report.worktrees_removed == [pushed.cwd] and not pushed.cwd.exists()
    assert report.branches_deleted == [] and report.branches_kept == [(project.root, branch)]
    assert _git("branch", "--list", branch, cwd=project.root), f"{branch} kept"
    assert report.worktrees_kept == []
    assert _git("rev-parse", "origin/feat/integration", cwd=project.root), "the work is safe"


def test_a_spawn_for_a_task_whose_tree_was_reaped_carries_on_from_its_branch(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, tmp_path: Path
) -> None:
    """Review of #259, round 14: reap deleted the ``fleet/…`` branch of a tree pushed to an
    integration branch, so a coder spawned again for the task — reopened by the reviewer —
    found no branch and started a new one from main, without its own pushed commits, and
    nothing said so. The branch is kept, and the respawn checks it out, commits and all."""
    _remote(project, tmp_path)
    task = _add_task(project, "Wire the auth flow")
    first = fleet_service.spawn(project, "coder", task_id=task.id).agent
    _commit(first.cwd, "feature.txt")
    _git("push", "-q", "origin", "HEAD:feat/integration", cwd=first.cwd)
    branch = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=first.cwd)
    tip = _git("rev-parse", "HEAD", cwd=first.cwd)
    fleet_service.stop(project, first.label, force=True)
    assert fleet_service.reap(project).worktrees_removed == [first.cwd]

    again = fleet_service.spawn(project, "coder", task_id=task.id)

    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=again.agent.cwd) == branch
    assert _git("rev-parse", "HEAD", cwd=again.agent.cwd) == tip, "its own commits"
    assert (again.agent.cwd / "feature.txt").exists()
    assert f"branch {branch} already existed — checked it out" in again.notes


#: A worktree and its ``fleet/…`` branch over their lifecycle (docs/fleet.md, "One rule for a
#: worktree and its branch"): what happened to them → what a restart of the agent does,
#: and what a new spawn for its task starts on. ``None`` for a restart is "made again, or
#: kept, on its branch at its tip"; a string is the refusal. A spawn ``"tip"`` lands on the
#: agent's last commit, ``"main"`` on main's.
_LIFECYCLE: dict[str, tuple[str | None, str]] = {
    "tree and branch there": (None, "tip"),
    "tree deleted by hand": (None, "tip"),
    "pushed, reaped: branch kept": (None, "tip"),
    "merged, reaped: branch deleted": ("and so is its branch", "tip"),
    "tree and branch deleted by hand": ("and so is its branch", "main"),
}


@pytest.mark.parametrize("then", ["restart", "spawn"])
@pytest.mark.parametrize("case", list(_LIFECYCLE))
def test_a_worktree_and_its_branch_over_their_lifecycle(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    tmp_path: Path,
    case: str,
    then: str,
) -> None:
    """Review of #259, rounds 13 to 15: one rule for an agent's tree and its branch. A
    tree that is gone is made again on the branch its row recorded (deleted by hand, or
    reaped with the branch kept), and a restart is refused only when that branch is gone,
    saying so — not blaming ``fleet reap``, which deletes a branch only once main holds it.
    A spawn for the task asks for the same branch and carries on from it. Refused, nothing
    is written: the 💤 row stays as it was, no tree is made at its path, and the repo's
    git config is untouched (round 15 left a note behind for a branch that was gone)."""
    _remote(project, tmp_path)
    task = _add_task(project, "Wire the auth flow")
    coder = fleet_service.spawn(project, "coder", task_id=task.id).agent
    _commit(coder.cwd, "feature.txt")
    branch = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=coder.cwd)
    tip = _git("rev-parse", "HEAD", cwd=coder.cwd)
    fleet_service.stop(project, coder.label, force=True)
    if case == "pushed, reaped: branch kept":
        _git("push", "-q", "origin", f"{branch}:feat/integration", cwd=project.root)
        assert fleet_service.reap(project).branches_kept == [(project.root, branch)]
    elif case == "merged, reaped: branch deleted":
        _git("merge", "-q", "--ff-only", branch, cwd=project.root)
        assert fleet_service.reap(project).branches_deleted == [(project.root, branch)]
    elif case != "tree and branch there":
        shutil.rmtree(coder.cwd)
        if case == "tree and branch deleted by hand":
            _git("update-ref", "-d", f"refs/heads/{branch}", cwd=project.root)
    refusal, spawned_on = _LIFECYCLE[case]

    if then == "spawn":
        cwd = fleet_service.spawn(project, "coder", task_id=task.id).agent.cwd
        assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd) == branch
        main = _git("rev-parse", "main", cwd=project.root)
        assert _git("rev-parse", "HEAD", cwd=cwd) == (tip if spawned_on == "tip" else main)
        return
    if refusal is not None:
        git_config = (project.root / ".git" / "config").read_bytes()
        with store_session() as store:
            [before] = store.fleet_agents(project.id)
        with pytest.raises(FleetError) as refused:
            fleet_service.restart(project, coder.label)
        said = str(refused.value)
        assert f"its worktree {coder.cwd} is gone" in said and refusal in said, said
        assert "fleet reap" not in said, "it says what it found"
        assert not coder.cwd.exists(), "no tree was made in its place"
        with store_session() as store:
            [row] = store.fleet_agents(project.id)
        assert row == before and row.id == coder.id and row.ended_at is not None
        assert (project.root / ".git" / "config").read_bytes() == git_config
        return
    receipt = fleet_service.restart(project, coder.label)
    assert receipt.started.cwd == coder.cwd
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=coder.cwd) == branch
    assert _git("rev-parse", "HEAD", cwd=coder.cwd) == tip, "every commit is there"
    assert receipt.started.branch == branch, "the replacement's row records it too"
    remade = f"its worktree {coder.cwd} was gone — made it again on its branch {branch}"
    assert (remade in receipt.notes) == (case != "tree and branch there"), receipt.notes


def _move_to_a_branch_of_its_own(tree: Path, name: str) -> str:
    """``git checkout -b <name>`` in ``tree``, a commit there, pushed to ``origin``: the tip."""
    _git("checkout", "-q", "-b", name, cwd=tree)
    _commit(tree, "fix.txt")
    _git("push", "-q", "origin", name, cwd=tree)
    return _git("rev-parse", "HEAD", cwd=tree)


@pytest.mark.parametrize(
    "case",
    [
        "on a branch of its own, stopped, its tree deleted by hand",
        "on a branch of its own, exited, reaped",
        "deleted by hand, then git's worktree list pruned",
        "its label's earlier tree was on another branch",
        "its label's earlier tree was on another branch, and it exited, reaped",
        "on a detached HEAD, stopped, its tree deleted by hand",
        "on a detached HEAD, exited, reaped",
    ],
)
def test_a_restart_makes_a_gone_tree_again_on_the_branch_its_row_recorded(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, tmp_path: Path, case: str
) -> None:
    """Review of #259, round 15: the branch of a gone tree was looked for in notes in the
    repo's git config, written only for a ``fleet/…`` branch and keyed by the tree's path,
    and in git's worktree list. So a coder that moved to ``fix/auth`` and was reaped could
    not be restarted (no note, and git had forgotten the tree); one whose tree was deleted
    by hand could not either once any spawn had pruned; and one whose label's earlier
    tree was reaped from ``fleet/…`` was rebuilt there, on the earlier agent's branch,
    without its own commits. The row records the branch at spawn, at stop and at reap,
    and a restart makes the tree again there, at its tip. Round 16: reap wrote the tree's
    branch to the OLDEST row at its path, and a restart acts on the newest, so the later
    agent, exited rather than stopped, was rebuilt on the earlier one's branch; and a
    detached HEAD at stop or reap wrote ``None`` over the branch recorded, so the restart
    was refused while that branch still held the agent's commits."""
    _remote(project, tmp_path)
    task = _add_task(project, "Wire the auth flow")
    coder = fleet_service.spawn(project, "coder", task_id=task.id).agent
    _commit(coder.cwd, "feature.txt")
    branch = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=coder.cwd)
    tip = _git("rev-parse", "HEAD", cwd=coder.cwd)
    if case == "on a branch of its own, stopped, its tree deleted by hand":
        branch, tip = "fix/auth", _move_to_a_branch_of_its_own(coder.cwd, "fix/auth")
        fleet_service.stop(project, coder.label, force=True)
        shutil.rmtree(coder.cwd)
    elif case == "on a branch of its own, exited, reaped":
        branch, tip = "fix/auth", _move_to_a_branch_of_its_own(coder.cwd, "fix/auth")
        tmux.die(coder.pane_id, 0)
        assert fleet_service.reap(project).worktrees_removed == [coder.cwd]
    elif case == "deleted by hand, then git's worktree list pruned":
        fleet_service.stop(project, coder.label, force=True)
        shutil.rmtree(coder.cwd)
        _git("worktree", "prune", cwd=project.root)  # as any spawn, or a role's cleanup
    elif case == "on a detached HEAD, stopped, its tree deleted by hand":
        _git("checkout", "-q", "--detach", cwd=coder.cwd)  # mid-rebase, say
        fleet_service.stop(project, coder.label, force=True)
        shutil.rmtree(coder.cwd)
    elif case == "on a detached HEAD, exited, reaped":
        _git("push", "-q", "origin", f"{branch}:feat/integration", cwd=coder.cwd)
        _git("checkout", "-q", "--detach", cwd=coder.cwd)
        tmux.die(coder.pane_id, 0)
        assert fleet_service.reap(project).worktrees_removed == [coder.cwd]
    else:
        _git("push", "-q", "origin", f"{branch}:feat/integration", cwd=coder.cwd)
        fleet_service.stop(project, coder.label, force=True)
        assert fleet_service.reap(project).branches_kept == [(project.root, branch)]
        coder = fleet_service.spawn(project, "coder", task_id=task.id).agent
        assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=coder.cwd) == branch
        branch, tip = "fix/auth", _move_to_a_branch_of_its_own(coder.cwd, "fix/auth")
        if case.endswith("exited, reaped"):
            tmux.die(coder.pane_id, 0)  # no stop: only the reap records its branch
        else:
            fleet_service.stop(project, coder.label, force=True)
        assert fleet_service.reap(project).worktrees_removed == [coder.cwd]
    assert not coder.cwd.exists()

    receipt = fleet_service.restart(project, coder.label)

    assert receipt.started.cwd == coder.cwd
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=coder.cwd) == branch
    assert _git("rev-parse", "HEAD", cwd=coder.cwd) == tip, "its own commits"
    assert f"made it again on its branch {branch}" in " ".join(receipt.notes), receipt.notes


@pytest.mark.parametrize("where", ["the main checkout", "another tree"])
def test_a_restart_refused_for_a_branch_out_elsewhere_says_how_to_free_it(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, tmp_path: Path, where: str
) -> None:
    """Review of #259, round 16: a restart refused because its gone tree's branch is out in
    another tree said "remove that tree first" — of the main checkout too, which cannot be
    removed, and where checking an ended agent's branch out to try its work is common.
    There it says to switch that checkout to another branch."""
    coder = fleet_service.spawn(project, "coder", label="coder-x").agent
    _commit(coder.cwd, "feature.txt")
    branch = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=coder.cwd)
    fleet_service.stop(project, "coder-x", force=True)
    shutil.rmtree(coder.cwd)
    _git("worktree", "prune", cwd=project.root)
    if where == "the main checkout":
        holder = project.root
        _git("checkout", "-q", branch, cwd=project.root)
    else:
        holder = tmp_path / "elsewhere"
        _git("worktree", "add", "-q", str(holder), branch, cwd=project.root)

    with pytest.raises(FleetError) as refused:
        fleet_service.restart(project, "coder-x")

    said = str(refused.value)
    assert f"{branch} is checked out in {holder.resolve()} now" in said, said
    way = "switch that checkout to another branch" if holder == project.root else "remove that tree"
    assert said.endswith(f"carry on there, or {way} first"), said


@pytest.mark.parametrize("merge", ["fast-forward", "squash"])
def test_reap_deletes_a_kept_fleet_branch_once_the_default_branch_has_its_work(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, tmp_path: Path, merge: str
) -> None:
    """Review of #259, round 15: a ``fleet/…`` branch reap kept when its tree went was
    never looked at again — the tree gone, its row was no candidate — so it stayed for good
    once merged, and with a squash or rebase merge its tip never reaches the default branch
    at all. Every reap now asks each branch no tree has out: deleted once the default
    branch holds its tip, or, pushed, once merging it would change nothing; the rest are
    named again, each reap, with how to drop them (the CLI)."""
    _remote(project, tmp_path)
    branches: dict[str, str] = {}
    for label in ("coder-merged", "coder-open"):
        agent = fleet_service.spawn(project, "coder", label=label).agent
        _commit(agent.cwd, f"{label}.txt")
        branches[label] = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=agent.cwd)
        _git("push", "-q", "origin", f"HEAD:feat/{label}", cwd=agent.cwd)
        fleet_service.stop(project, label, force=True)
    first = fleet_service.reap(project)
    assert sorted(first.branches_kept) == sorted((project.root, b) for b in branches.values())
    assert first.branches_deleted == []
    done, still = branches["coder-merged"], branches["coder-open"]
    if merge == "fast-forward":
        _git("merge", "-q", "--ff-only", done, cwd=project.root)
    else:
        _git("merge", "-q", "--squash", done, cwd=project.root)
        _git("commit", "-q", "-m", "Squash-merge the merged coder's work", cwd=project.root)
        assert _git("branch", "--list", "--merged", "main", done, cwd=project.root) == ""

    again = fleet_service.reap(project)

    assert again.worktrees_removed == [] and again.branches_deleted == [(project.root, done)]
    assert _git("branch", "--list", done, cwd=project.root) == ""
    assert again.branches_kept == [(project.root, still)], "still kept, and named again"
    assert _git("branch", "--list", still, cwd=project.root)


def test_a_squash_merged_branch_goes_where_a_top_level_path_is_named_like_the_default_branch(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, tmp_path: Path
) -> None:
    """Review of #259, round 17: ``git rev-list <tips> --not main --remotes`` had no ``--``,
    and in a checkout whose top level holds a ``main/`` git exits 128, "ambiguous argument
    'main': both revision and filename". Read as "nothing pushed", every squash-merged
    fleet branch was kept, and named again on every reap."""
    (project.root / "main").mkdir()
    (project.root / "main" / "README").write_text("a directory named like the branch\n")
    _git("add", "main", cwd=project.root)
    _git("commit", "-q", "-m", "a main/ directory", cwd=project.root)
    _remote(project, tmp_path)
    agent = fleet_service.spawn(project, "coder", label="coder-squash").agent
    _commit(agent.cwd, "feature.txt")
    branch = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=agent.cwd)
    _git("push", "-q", "origin", "HEAD:feat/squash", cwd=agent.cwd)
    fleet_service.stop(project, agent.label, force=True)
    _git("merge", "-q", "--squash", branch, cwd=project.root)
    _git("commit", "-q", "-m", "Squash-merge the coder's work", cwd=project.root)

    report = fleet_service.reap(project)

    assert report.worktrees_removed == [agent.cwd]
    assert report.branches_deleted == [(project.root, branch)] and report.branches_kept == []
    assert _git("branch", "--list", branch, cwd=project.root) == ""


def test_a_reap_with_every_recorded_branch_swept_asks_no_tree_about_branches(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #259, round 35, 5/8: round 34 asked every tree what it has out or is
    rebasing (``git worktree list``, up to four state files a tree) before the sweep's early
    return. Ended rows are kept for good, so once a project's merged branches were swept,
    every later reap asked them all for nothing, a live coder's own ``fleet/…`` branch
    keeping the listing from coming back empty. With no recorded branch left, the sweep asks
    neither ``--merged`` nor the trees."""
    _remote(project, tmp_path)
    agent = fleet_service.spawn(project, "coder", label="coder-done").agent
    _commit(agent.cwd, "done.txt")
    branch = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=agent.cwd)
    _git("push", "-q", "origin", "HEAD:feat/done", cwd=agent.cwd)
    fleet_service.stop(project, agent.label, force=True)
    _git("merge", "-q", "--ff-only", branch, cwd=project.root)
    assert fleet_service.reap(project).branches_deleted == [(project.root, branch)]
    fleet_service.spawn(project, "coder", label="coder-live")  # a fleet/… branch of its own
    asked: list[Path] = []
    real = fleet_service._branches_in_use

    def counted(root: Path, common: Path) -> set[str] | None:
        asked.append(root)
        return real(root, common)

    monkeypatch.setattr(fleet_service, "_branches_in_use", counted)
    gits: list[tuple[str, ...]] = []
    real_git = fleet_service._git

    def counted_git(cwd: Path, *args: str) -> object:
        gits.append(args)
        return real_git(cwd, *args)

    monkeypatch.setattr(fleet_service, "_git", counted_git)

    again = fleet_service.reap(project)

    assert again.branches_deleted == [] and again.branches_kept == []
    assert asked == [], "no recorded branch left to judge"
    assert not any(arg.startswith("--merged=") for args in gits for arg in args), gits


def test_a_kept_branch_costs_a_reap_one_git_call_pushed_and_none_unpushed(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #259, round 16: a kept branch stays until main has its work, perhaps for
    good, and every reap asked each one five git commands (``rev-parse``, ``merge-base``,
    ``for-each-ref --contains``, ``merge-tree``, ``rev-parse ^{tree}``). Whether each is
    pushed is one ``rev-list`` for them all now: a pushed one costs its ``merge-tree``,
    an unpushed one (its tree deleted by hand) nothing."""
    _remote(project, tmp_path)
    for label in ("coder-a", "coder-b", "coder-c"):
        agent = fleet_service.spawn(project, "coder", label=label).agent
        _commit(agent.cwd, f"{label}.txt")
        if label == "coder-c":
            fleet_service.stop(project, label, force=True)
            shutil.rmtree(agent.cwd)  # unpushed, so reap would have kept its tree
            _git("worktree", "prune", cwd=project.root)
        else:
            _git("push", "-q", "origin", f"HEAD:feat/{label}", cwd=agent.cwd)
            fleet_service.stop(project, label, force=True)
    assert len(fleet_service.reap(project).branches_kept) == 3
    real = fleet_service._git
    asked: list[tuple[str, ...]] = []

    def counted(cwd: Path, *args: str) -> object:
        asked.append(args)
        return real(cwd, *args)

    monkeypatch.setattr(fleet_service, "_git", counted)

    again = fleet_service.reap(project)

    assert len(again.branches_kept) == 3 and again.branches_deleted == []
    commands = [args[0] for args in asked]
    assert commands.count("merge-tree") == 2, "the two pushed ones"
    assert commands.count("rev-list") == 1, "one question for all three"
    assert "merge-base" not in commands and ("--contains" not in sum(asked, ())), asked


def test_reap_keeps_a_worktree_with_unpushed_commits_or_uncommitted_changes_and_says_why(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, tmp_path: Path
) -> None:
    _remote(project, tmp_path)
    unpushed = fleet_service.spawn(project, "coder", label="coder-unpushed").agent
    _commit(unpushed.cwd, "local.txt")
    dirty = fleet_service.spawn(project, "coder", label="coder-dirty").agent
    (dirty.cwd / "scratch.txt").write_text("wip\n", encoding="utf-8")  # untracked, not ignored
    branches = {
        agent.label: _git("rev-parse", "--abbrev-ref", "HEAD", cwd=agent.cwd)
        for agent in (unpushed, dirty)
    }
    for label in branches:
        fleet_service.stop(project, label, force=True)

    report = fleet_service.reap(project)

    assert report.worktrees_removed == [] and report.branches_deleted == []
    kept = dict(report.worktrees_kept)
    assert kept[unpushed.cwd] == "commits on no remote branch yet — push them, then reap again"
    assert kept[dirty.cwd] == "uncommitted changes"
    assert unpushed.cwd.exists() and dirty.cwd.exists()
    for branch in branches.values():
        assert _git("branch", "--list", branch, cwd=project.root), f"{branch} kept"

    # Pushed, the next reap takes it; the dirty one is still kept.
    _git("push", "-q", "origin", "HEAD:feat/later", cwd=unpushed.cwd)
    again = fleet_service.reap(project)
    assert again.worktrees_removed == [unpushed.cwd]
    assert again.branches_deleted == []
    assert again.branches_kept == [(project.root, branches["coder-unpushed"])]
    assert dict(again.worktrees_kept) == {dirty.cwd: "uncommitted changes"}


@pytest.mark.parametrize("where", ["repo config", "global config"])
def test_reap_keeps_an_untracked_file_that_status_show_untracked_files_hides(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    where: str,
) -> None:
    """``status.showUntrackedFiles=no`` (common in big repos and dotfile setups) hides an
    untracked file from a plain ``git status --porcelain``, and ``git worktree remove``
    honours it too: the tree read clean, was pushed, and reap deleted the file. Reap asks
    for untracked files whatever the config says."""
    _remote(project, tmp_path)
    if where == "repo config":
        _git("config", "status.showUntrackedFiles", "no", cwd=project.root)
    else:
        config = tmp_path / "gitconfig"
        config.write_text("[status]\n\tshowUntrackedFiles = no\n", encoding="utf-8")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    agent = fleet_service.spawn(project, "coder", label="coder-hidden").agent
    _commit(agent.cwd, "f.txt")
    _git("push", "-q", "origin", "HEAD:feat/x", cwd=agent.cwd)
    branch = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=agent.cwd)
    precious = agent.cwd / "precious.txt"
    precious.write_text("only here\n", encoding="utf-8")  # untracked, not ignored
    assert _git("status", "--porcelain", cwd=agent.cwd) == "", "the premise: git hides it"
    fleet_service.stop(project, "coder-hidden", force=True)

    report = fleet_service.reap(project)

    assert report.worktrees_removed == [] and report.branches_deleted == []
    assert dict(report.worktrees_kept) == {agent.cwd: "uncommitted changes"}
    assert precious.read_text(encoding="utf-8") == "only here\n"
    assert _git("branch", "--list", branch, cwd=project.root), f"{branch} kept"


#: Which branches a reap deletes (docs/fleet.md, ``fleet reap``): a ``fleet/…`` branch an
#: ended agent's row recorded, no tree has out, goes once main has its work and is named,
#: kept, until then. Every other branch is not the fleet's and is left as it is, merged or
#: not: case → ``"deleted"``, ``"kept"`` or ``"left"``.
_SWEEP: dict[str, str] = {
    "an ended agent's, merged": "deleted",
    "an ended agent's, pushed and not merged": "kept",
    "an ended agent's, merged, named by a live agent whose tree is gone": "left",
    "made by hand, merged, in a project no agent ran in": "left",
    "made by hand, merged, beside an ended agent's": "left",
    "spawned on, then left by its agent for a branch of its own": "left",
    "an ended agent's own, not fleet/…, merged": "left",
}


@pytest.mark.parametrize("case", list(_SWEEP))
def test_reap_deletes_only_a_fleet_branch_an_ended_agent_recorded(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, tmp_path: Path, case: str
) -> None:
    """Review of #259, round 16: every reap deleted ANY merged ``fleet/…`` branch no tree
    had out, in every project it walked — a user's ``git branch fleet/my-own-notes`` in a
    repo no agent ever ran in included, as ``fleet reap --all`` walks every captured
    directory. Only a branch an ended agent's row recorded is the fleet's to delete; a
    branch no row names, or one that is not ``fleet/…``, is never touched; and one a live
    agent's row names is not deleted under it, its tree gone or not."""
    _remote(project, tmp_path)
    if case == "made by hand, merged, in a project no agent ran in":
        branch = "fleet/my-own-notes"
        _git("branch", branch, cwd=project.root)
    else:
        agent = fleet_service.spawn(project, "coder", label="coder-x").agent
        branch = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=agent.cwd)
        if case == "an ended agent's, pushed and not merged":
            _commit(agent.cwd, "feature.txt")
            _git("push", "-q", "origin", "HEAD:feat/integration", cwd=agent.cwd)
        elif case == "made by hand, merged, beside an ended agent's":
            branch = "fleet/my-own-notes"
            _git("branch", branch, cwd=project.root)
        elif "my-own" in case or "not fleet/…" in case or "left by its agent" in case:
            _git("switch", "-q", "-c", "my-own", cwd=agent.cwd)
            _commit(agent.cwd, "mine.txt")
            _git("push", "-q", "origin", "HEAD:my-own", cwd=agent.cwd)
            if case == "an ended agent's own, not fleet/…, merged":
                _git("merge", "-q", "--ff-only", "my-own", cwd=project.root)
                branch = "my-own"
        fleet_service.stop(project, "coder-x", force=True)
        if case == "an ended agent's, merged, named by a live agent whose tree is gone":
            again = fleet_service.spawn(project, "coder", label="coder-x").agent
            assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=again.cwd) == branch
            shutil.rmtree(again.cwd)
            _git("worktree", "prune", cwd=project.root)  # out of every tree now
    assert branch in _git("branch", "--list", "--merged", "main", cwd=project.root) or (
        _SWEEP[case] == "kept"
    ), "the premise: main holds its tip, so only whose branch it is decides"

    report = fleet_service.reap(project)

    verdict = _SWEEP[case]
    assert ((project.root, branch) in report.branches_deleted) == (verdict == "deleted"), report
    assert ((project.root, branch) in report.branches_kept) == (verdict == "kept"), report
    assert bool(_git("branch", "--list", branch, cwd=project.root)) == (verdict != "deleted")


def test_a_reap_deletes_a_done_branch_only_at_the_tip_it_judged(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 33, 6/10: the sweep judged which branches were done (merged, or
    squashed in) before it took the lock, and under it ran ``git branch -D``, which deletes
    whatever the ref holds by then. A tip moved in between (a ``git fetch origin
    fleet/x:fleet/x``, a ``branch -f``) was deleted with commits nobody judged. The delete
    is made at the judged tip alone: a moved branch is kept, and judged again next reap.
    One deleted takes its ``branch.<name>`` config with it, as ``branch -D`` did."""
    task = _add_task(project, "Wire the auth flow")
    agent = fleet_service.spawn(project, "coder", task_id=task.id).agent
    branch = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=agent.cwd)
    fleet_service.stop(project, agent.label, force=True)
    _git("config", f"branch.{branch}.remote", "origin", cwd=project.root)  # as `push -u` sets
    real = fleet_service._squashed
    fetched: list[str] = []

    def a_fetch_meanwhile(root: Path, tips: Mapping[str, str], default: str) -> set[str]:
        if not fetched:  # after `--merged` read it done, before the lock
            tip = _git("commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "fetched", cwd=root)
            _git("branch", "-f", branch, tip, cwd=root)
            fetched.append(tip)
        return real(root, tips, default)

    monkeypatch.setattr(fleet_service, "_squashed", a_fetch_meanwhile)

    first = fleet_service.reap(project)

    assert first.branches_deleted == [], first
    assert _git("rev-parse", branch, cwd=project.root) == fetched[0], "kept, at its new tip"
    again = fleet_service.reap(project)
    assert again.branches_deleted == [] and again.branches_kept == [(project.root, branch)]
    _git("branch", "-f", branch, "HEAD", cwd=project.root)  # main holds its tip again
    assert fleet_service.reap(project).branches_deleted == [(project.root, branch)]
    assert _git("branch", "--list", branch, cwd=project.root) == ""
    left = subprocess.run(
        ["git", "config", "--get", f"branch.{branch}.remote"],
        cwd=project.root,
        capture_output=True,
        text=True,
    )
    assert (left.returncode, left.stdout) == (1, ""), "its config went with it"


def _git_in(tree: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    """``git <args>`` in ``tree`` with ``env`` added, whatever it exits with."""
    return subprocess.run(
        ["git", "-c", "user.email=fleet@test", "-c", "user.name=fleet", *args],
        cwd=tree,
        capture_output=True,
        text=True,
        env={**os.environ, **env},
    )


@pytest.mark.parametrize("state", ["rebasing", "rebasing, applying", "updating", "bisecting"])
def test_a_reap_never_deletes_a_branch_a_worktree_is_rebasing_or_bisecting(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, tmp_path: Path, state: str
) -> None:
    """Review of #259, round 34, 1/10: ``git branch -D`` refuses a branch a worktree has out,
    is rebasing or is bisecting from; the ``update-ref -d`` round 33 deleted with refuses
    none, and ``git worktree list`` says ``detached`` of a tree mid-rebase. A merged task
    branch a hand-made tree was rebasing was deleted, its ``rebase --continue`` failed on the
    ref, and the rebased commits were left on a detached HEAD. The sweep refuses what ``branch
    -D`` refuses: a rebase's branch (``head-name``, either backend), the refs its
    ``--update-refs`` moves, and a bisect's; deleted once the tree is done with it."""
    _commit(project.root, "a.txt")
    _commit(project.root, "b.txt")
    task = _add_task(project, "Wire the auth flow")
    agent = fleet_service.spawn(project, "coder", task_id=task.id).agent
    branch = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=agent.cwd)
    fleet_service.stop(project, agent.label, force=True)
    _git("worktree", "remove", str(agent.cwd), cwd=project.root)  # merged: main holds its tip
    tree = tmp_path / "wt"
    mine = ("-b", "mine") if state == "updating" else ()
    _git("worktree", "add", "-q", *mine, str(tree), branch, cwd=project.root)
    if state == "bisecting":
        started = _git_in(tree, "bisect", "start", "HEAD", "HEAD~2")  # checks out a.txt's
    else:  # stopped by a conflict: b.txt is theirs on the commit it is replayed onto
        _git("switch", "-q", "--detach", f"{branch}~1", cwd=tree)
        (tree / "b.txt").write_text("theirs\n", encoding="utf-8")
        _git("add", "b.txt", cwd=tree)
        _git("commit", "-q", "-m", "theirs", cwd=tree)
        onto = _git("rev-parse", "HEAD", cwd=tree)
        _git("switch", "-q", "-", cwd=tree)
        if state == "updating":  # another branch's rebase, which moves this one as it ends
            _commit(tree, "c.txt")
        how = {
            "rebasing": ("HEAD~1",),
            "rebasing, applying": ("--apply", "HEAD~1"),
            "updating": ("--update-refs", "HEAD~2"),
        }[state]
        started = _git_in(tree, "rebase", "--onto", onto, *how)
    listed = _git("worktree", "list", "--porcelain", cwd=project.root).split("\n\n")
    assert listed[1].endswith("detached"), f"the premise: {listed} ({started.stderr})"
    refused = _git_in(project.root, "branch", "-D", branch)
    assert refused.returncode != 0 and "used by worktree" in refused.stderr, "as git refuses"

    report = fleet_service.reap(project)

    assert report.branches_deleted == [], report
    assert _git("branch", "--list", branch, cwd=project.root)
    if state == "rebasing":
        (tree / "b.txt").write_text("ours\n", encoding="utf-8")
        _git("add", "b.txt", cwd=tree)
        ended = _git_in(tree, "rebase", "--continue", GIT_EDITOR="true")
        assert ended.returncode == 0, f"it ends cleanly: {ended.stderr}"
        assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=tree) == branch
        return
    if state == "bisecting":
        _git("bisect", "reset", cwd=tree)
    else:
        _git("rebase", "--abort", cwd=tree)
    _git("worktree", "remove", "--force", str(tree), cwd=project.root)
    assert fleet_service.reap(project).branches_deleted == [(project.root, branch)]


def test_a_reap_deletes_no_branch_when_git_will_not_list_the_worktrees(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 34, 1/10: a ``git worktree list`` that failed read as no tree
    having any branch out, and every done ``fleet/…`` branch was deleted, one checked out
    included. Nothing is known to be free then: none goes, and the next reap asks again."""
    task = _add_task(project, "Wire the auth flow")
    agent = fleet_service.spawn(project, "coder", task_id=task.id).agent
    branch = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=agent.cwd)
    fleet_service.stop(project, agent.label, force=True)
    _git("worktree", "remove", str(agent.cwd), cwd=project.root)
    real = fleet_service._git

    def no_list(root: Path, *args: str) -> fleet_service._GitResult:
        if args[:2] == ("worktree", "list"):
            return fleet_service._GitResult(128, "", "fatal: no")
        return real(root, *args)

    monkeypatch.setattr(fleet_service, "_git", no_list)
    assert fleet_service.reap(project).branches_deleted == []
    assert _git("branch", "--list", branch, cwd=project.root)
    monkeypatch.setattr(fleet_service, "_git", real)
    assert fleet_service.reap(project).branches_deleted == [(project.root, branch)]


def test_a_reap_inside_a_spawn_never_deletes_the_branch_the_spawn_is_making_its_tree_on(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 16: ``git worktree add -b`` makes the branch first and
    registers the tree after it, so a reap in between (the manager's, or ``fleet reap``)
    read a respawned task's branch — recorded by its ended agent, at main's tip — as merged
    and out of every tree, and deleted it under the spawn. A spawn makes its tree under
    the lock a reap deletes under: a reap that meets it deletes nothing, and the next
    one does."""
    task = _add_task(project, "Wire the auth flow")
    first = fleet_service.spawn(project, "coder", task_id=task.id).agent
    branch = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=first.cwd)
    fleet_service.stop(project, first.label, force=True)
    assert fleet_service.reap(project).branches_deleted == [(project.root, branch)], (
        "main holds its tip"
    )
    real = fleet_service._add_worktree
    meanwhile: list[fleet_service.ReapReport] = []

    def a_reap_in_between(root: Path, path: Path, name: str, *, new: bool = False) -> None:
        if new:  # `git worktree add -b`, in its two steps: the branch, then the tree
            _git("branch", name, cwd=root)
            meanwhile.append(fleet_service.reap(project))
        real(root, path, name)

    monkeypatch.setattr(fleet_service, "_add_worktree", a_reap_in_between)

    again = fleet_service.spawn(project, "coder", task_id=task.id).agent

    assert [report.branches_deleted for report in meanwhile] == [[]]
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=again.cwd) == branch
    fleet_service.stop(project, again.label, force=True)
    assert fleet_service.reap(project).branches_deleted == [(project.root, branch)], (
        "the next reap does"
    )


@pytest.mark.parametrize("via", ["spawn", "restart"])
def test_a_reap_never_removes_a_tree_a_spawn_reused_after_it_read_the_rows(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    via: str,
) -> None:
    """Review of #259, round 17: reap decided a tree was unused from the rows it read before
    its loop, and asks each tree a few git questions first. A spawn for the label — or a
    restart of the agent, which carries on in its tree as it stands — reused the tree
    meanwhile and wrote its live row; the reap then read the tree as an ended agent's, clean
    and pushed, and removed it under the new agent. It reads the live rows again, under the
    lock a spawn reuses trees under, before it removes one."""
    first = fleet_service.spawn(project, "coder", label="coder-auth").agent
    fleet_service.stop(project, "coder-auth", force=True)
    real = fleet_service._default_branch
    started: list[FleetAgent] = []

    def a_spawn_meanwhile(root: Path) -> str:
        """Between the reap's read of the rows and its first tree: every question about a
        tree is asked under the lock since round 18, which a spawn there would wait on."""
        if not started:
            if via == "spawn":
                started.append(fleet_service.spawn(project, "coder", label="coder-auth").agent)
            else:
                started.append(fleet_service.restart(project, "coder-auth").started)
        return real(root)

    monkeypatch.setattr(fleet_service, "_default_branch", a_spawn_meanwhile)

    report = fleet_service.reap(project)

    [again] = started
    assert again.cwd == first.cwd and again.ended_at is None
    assert first.cwd.is_dir(), "the new agent's tree is still there"
    assert report.worktrees_removed == [] and report.worktrees_kept == []


@pytest.mark.parametrize("then", ["working", "ended"])
def test_a_reap_records_no_branch_on_a_row_whose_tree_a_spawn_reused_after_it_read_the_rows(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    then: str,
) -> None:
    """Review of #259, round 18: reap recorded the tree's branch on the ended row it had read
    before its loop, outside the branch lock. A spawn for another task that reused the tree
    meanwhile had checked out its own branch there, which was written on the old row, and
    the old row's branch, named by no row then, was never swept. The branch is recorded under
    the lock, and only on a row no other has followed at the tree since the rows were read:
    one working there, or one that ended there since."""
    task_a, task_b = _add_task(project, "First job"), _add_task(project, "Second job")
    first = fleet_service.spawn(project, "coder", label="coder-auth", task_id=task_a.id).agent
    fleet_service.stop(project, "coder-auth", force=True)
    on_a = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=first.cwd)
    real = fleet_service._default_branch
    started: list[FleetAgent] = []

    def a_spawn_meanwhile(root: Path) -> str:
        if not started:
            receipt = fleet_service.spawn(project, "coder", label="coder-auth", task_id=task_b.id)
            started.append(receipt.agent)
            if then == "ended":
                fleet_service.stop(project, "coder-auth", force=True)
        return real(root)

    monkeypatch.setattr(fleet_service, "_default_branch", a_spawn_meanwhile)

    report = fleet_service.reap(project)

    [second] = started
    on_b = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=second.cwd)
    assert second.cwd == first.cwd and on_b != on_a, "the premise: task B's branch, in A's tree"
    with store_session() as store:
        old, new = store.get_fleet_agent(first.id), store.get_fleet_agent(second.id)
    assert old is not None and old.branch == on_a, "task A's branch, still"
    assert new is not None and new.branch == on_b
    assert report.worktrees_removed == [] and first.cwd.is_dir()


def test_a_reap_inside_a_spawn_never_removes_the_tree_it_reused_before_its_row(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the race above: a reap between a spawn's reuse of a tree and the row
    that says an agent works there — the window opening, say — found no live row at the path
    and removed the tree. A spawn holds the branch lock from the tree until its row, and a
    reap that meets the lock removes nothing, saying why; the next one asks again."""
    first = fleet_service.spawn(project, "coder", label="coder-auth").agent
    fleet_service.stop(project, "coder-auth", force=True)
    real = fleet_service.open_window
    meanwhile: list[fleet_service.ReapReport] = []

    def a_reap_first(*args: Any, **kwargs: Any) -> WindowInfo:
        meanwhile.append(fleet_service.reap(project))
        return real(*args, **kwargs)

    monkeypatch.setattr(fleet_service, "open_window", a_reap_first)

    again = fleet_service.spawn(project, "coder", label="coder-auth").agent

    [during] = meanwhile
    assert during.worktrees_removed == []
    assert [path for path, _why in during.worktrees_kept] == [first.cwd]
    assert "holds" in during.worktrees_kept[0][1]
    assert again.cwd == first.cwd and first.cwd.is_dir()
    monkeypatch.setattr(fleet_service, "open_window", real)
    assert fleet_service.reap(project).worktrees_removed == [], "a live agent's, from now on"


def test_a_reap_says_why_it_keeps_a_tree_while_a_spawn_holds_the_branch_lock(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 19: a reap took the branch lock before it asked what a tree
    held. While a spawn held it, a tree with uncommitted work was reported as "another
    aisquare holds … — try again", not as what to commit; and each reap held the lock
    through ``git status`` and the push check of every tree it kept, a spawn queued behind.
    What a tree holds is asked without the lock, and said whoever holds it.

    Round 20: said while the lock is busy, it is what the tree held when asked, beside "not
    removed now", not a settled reason to keep it. The holder may be a spawn reusing this
    very tree, mid-checkout: a reap during a respawn said the new agent's tree was kept for
    the ended one's uncommitted changes."""
    agent = fleet_service.spawn(project, "coder", label="coder-dirty").agent
    (agent.cwd / "work.txt").write_text("not committed\n", encoding="utf-8")
    fleet_service.stop(project, "coder-dirty", force=True)
    lock = fleet_service._git_common_dir(project.root) / "aisquare-fleet-branches.lock"

    with locking.holding(lock, wait_s=0):  # a spawn, mid `git worktree add`
        during = fleet_service.reap(project)

    real = fleet_service._why_kept
    free: list[bool] = []

    def asked(root: Path, tree: Path, default: str) -> str | None:
        try:
            with locking.holding(lock, wait_s=0):
                free.append(True)
        except TimeoutError:
            free.append(False)
        return real(root, tree, default)

    monkeypatch.setattr(fleet_service, "_why_kept", asked)
    after = fleet_service.reap(project)

    [(path, why)] = during.worktrees_kept
    assert path == agent.cwd
    assert why.startswith("not removed now: another aisquare (a spawn or a reap) holds "), why
    assert why.endswith("; when asked just before, it held: uncommitted changes"), why
    assert dict(after.worktrees_kept) == {agent.cwd: "uncommitted changes"}
    assert free == [True], "asked without the lock"
    assert agent.cwd.is_dir()


def test_a_reap_asks_what_a_tree_holds_without_writing_its_index(repo: Path) -> None:
    """Review of #259, round 20: a reap asks an ended agent's tree what it holds without the
    branch lock, and ``git status`` writes back the index it refreshed, holding the tree's
    ``index.lock`` while it does: a respawn's ``git checkout`` there failed on it (77 of 200
    overlapping checkouts). Asked with ``GIT_OPTIONAL_LOCKS=0``, it writes nothing."""
    (repo / "file.txt").write_text("committed\n", encoding="utf-8")
    _git("add", "file.txt", cwd=repo)
    _git("commit", "-q", "-m", "file", cwd=repo)
    index = repo / ".git" / "index"
    later = index.stat().st_mtime_ns + 5_000_000_000
    os.utime(repo / "file.txt", ns=(later, later))  # the same bytes, a stat to refresh
    before = index.stat()

    assert fleet_service._why_kept(repo, repo, "main") is None, "the premise: clean, merged"

    after = index.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_every_git_command_the_fleet_runs_takes_no_optional_lock(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 20: one seam, so every git command a spawn, a stop and a reap run
    (status, rev-parse, for-each-ref, worktree list and the rest) takes no optional lock,
    and a read never holds a lock a write in the same tree needs. But the board's own
    question, ``rev-parse --git-common-dir`` (``workspace.git_common_root``), which every
    hook asks too: it takes no lock (round 26, 11/11). Round 27, 4/7: by
    ``GIT_OPTIONAL_LOCKS=0``, not ``--no-optional-locks``, which a git before 2.15 refuses
    (exit 129), so every fleet git command failed there; that git ignores the variable.
    Round 28, 10/10: every git seam, that question's too, runs in the one git environment
    (``core.spawn.git_env``), so none is left out."""
    real = subprocess.run
    gits: list[tuple[list[str], str | None]] = []

    def before_2_15(argv: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(argv, list) and argv and argv[0] == "git":
            gits.append((argv, (kwargs.get("env") or {}).get("GIT_OPTIONAL_LOCKS")))
            if "--no-optional-locks" in argv:
                return subprocess.CompletedProcess(argv, 129, "", "unknown option")
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", before_2_15)
    agent = fleet_service.spawn(project, "coder", label="coder-1").agent
    (agent.cwd / "work.txt").write_text("not committed\n", encoding="utf-8")
    fleet_service.stop(project, "coder-1", force=True)
    report = fleet_service.reap(project)

    assert dict(report.worktrees_kept) == {agent.cwd: "uncommitted changes"}, "status ran"
    assert {argv[argv.index("-C") + 2] for argv, _ in gits} >= {"status", "worktree", "rev-parse"}
    assert any(argv[-2:] == ["rev-parse", "--git-common-dir"] for argv, _ in gits)
    assert [argv for argv, locks in gits if locks != "0"] == []


def test_a_spawn_asks_git_for_its_board_with_no_store_open(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 29, 5/8: since round 25 a spawn's board is ``_board_for``, which
    with no hub runs ``git rev-parse`` (a 5 s timeout), and every spawn ran it inside the
    store session that also held its one-manager and cap checks. It is asked before the
    store opens."""
    monkeypatch.delenv("AISQUARE_TEAM_HUB", raising=False)
    depth = 0

    @contextmanager
    def counted() -> Iterator[ContextStore]:
        nonlocal depth
        depth += 1
        try:
            with store_session() as store:
                yield store
        finally:
            depth -= 1

    asked: list[int] = []
    real = fleet_service._board_for

    def board_for(project: ProjectInfo) -> str:
        asked.append(depth)
        return real(project)

    monkeypatch.setattr(fleet_service, "store_session", counted)
    monkeypatch.setattr(fleet_service, "_board_for", board_for)

    fleet_service.spawn(project, "coder", worktree=False)

    assert asked == [0], "asked once, with no store open"


def test_a_reap_or_a_spawn_asks_git_for_the_branch_locks_directory_once(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 18: every taking of the branch lock asked ``git rev-parse
    --git-common-dir`` where the lock lives, and a reap takes it for each tree it removes,
    so it paid one more git process a tree; a spawn asked again to add its exclude line.
    Each asks once."""
    for label in ("coder-a", "coder-b", "coder-c"):
        fleet_service.spawn(project, "coder", label=label)
        fleet_service.stop(project, label, force=True)
    real = fleet_service._git
    asked: list[tuple[str, ...]] = []

    def counted(cwd: Path, *args: str) -> Any:
        asked.append(args)
        return real(cwd, *args)

    monkeypatch.setattr(fleet_service, "_git", counted)

    assert len(fleet_service.reap(project).worktrees_removed) == 3
    assert asked.count(("rev-parse", "--git-common-dir")) == 1
    asked.clear()
    fleet_service.spawn(project, "coder", label="coder-d")
    assert asked.count(("rev-parse", "--git-common-dir")) == 1


@pytest.mark.parametrize("moving", [True, False], ids=["moving", "silent"])
def test_a_waiter_on_the_branch_lock_waits_while_its_holder_moves_on(
    repo: Path, monkeypatch: pytest.MonkeyPatch, moving: bool
) -> None:
    """Review of #259, round 18: the wait on one holder was sized for "at most four git
    commands", and a spawn now holds the lock through its tree, its window and its row; on a
    large repo a holder still working went past it, and a restart, its agent stopped, was
    refused as behind a stuck one. A holder says it moved on as it works, and the waiter's
    patience is for one step. One that says nothing for the wait is still called stuck.

    Driven by events, not by sleeps against the waiter's patience (round 19): the holder
    says it moved on each time the waiter looks, and lets go once the waiter has looked
    more times than its patience lasts; saying nothing, it holds until the waiter gave up.
    A slow runner only makes each look later."""
    monkeypatch.setattr(fleet_service, "_HOLDER_LOOK", 0.01)
    common = fleet_service._git_common_dir(repo)
    lock = common / "aisquare-fleet-branches.lock"
    real_look = fleet_service._lock_holder
    looks: list[str] = []
    holding, let_go = threading.Event(), threading.Event()

    def look(at: Path) -> str:
        if moving and holding.is_set():
            # The holder's next step, said with a word of its own: a waiter that kept its
            # patience from the first word would be past it by the fifth look.
            fleet_service._holder_file(at).write_text(f"step {len(looks)}\n")
            if len(looks) >= 10:
                let_go.set()
        looks.append(real_look(at))
        return looks[-1]

    def a_holder() -> None:
        with locking.holding(lock, wait_s=5):
            fleet_service._announce_holder(common)
            holding.set()
            let_go.wait(30)

    monkeypatch.setattr(fleet_service, "_lock_holder", look)
    holder = threading.Thread(target=a_holder)
    holder.start()
    try:
        assert holding.wait(30)
        if moving:
            with fleet_service._branches_locked(common, wait=0.05):
                assert let_go.is_set(), "taken once the holder let go"
            assert len(looks) > 10, "waited through more looks than its patience lasts"
        else:
            with (
                pytest.raises(FleetError, match="may be stuck"),
                fleet_service._branches_locked(common, wait=0.05),
            ):
                pass
    finally:
        let_go.set()
        holder.join(30)


@pytest.mark.parametrize("stepping", [True, False], ids=["git-steps", "blocked"])
def test_a_holder_of_the_branch_lock_moves_on_only_as_its_git_steps_finish(
    repo: Path, monkeypatch: pytest.MonkeyPatch, stepping: bool
) -> None:
    """Review of #259, round 20: the hold said it still held from a thread of its own, every
    ``_HOLDER_LOOK`` for as long as it lasted (round 19). That shows the process alive, not
    moving: a holder blocked under the lock (a state-file ``flock`` on an NFS home whose lock
    manager is gone, a ``git worktree add`` that will not die on Windows) kept saying so, and
    every waiter waited on it with no limit. A holder moves on as each git command it runs
    under the lock finishes; blocked, it says nothing, and a waiter calls it stuck.

    Driven by events: stepping, the holder finishes one git command each time the waiter
    looks, ten in all, more looks than the waiter's patience lasts, then lets go; blocked, it
    holds until the waiter gives up, or for 5 s, after which round 19's waiter took the lock."""
    monkeypatch.setattr(fleet_service, "_HOLDER_LOOK", 0.01)
    common = fleet_service._git_common_dir(repo)
    real_look = fleet_service._lock_holder
    holding, step, stepped, let_go = (threading.Event() for _ in range(4))
    steps: list[str] = []
    looks: list[str] = []

    def look(at: Path) -> str:
        if stepping and holding.is_set() and len(steps) < 10:
            stepped.clear()
            step.set()
            assert stepped.wait(30)
        looks.append(real_look(at))
        return looks[-1]

    def a_holder() -> None:
        with fleet_service._branches_locked(common, wait=0):
            holding.set()
            if not stepping:
                let_go.wait(5)  # alive, and no step of it finishes
                return
            while len(steps) < 10:
                step.wait(30)
                step.clear()
                steps.append(fleet_service._git(repo, "rev-parse", "HEAD").stdout)
                stepped.set()

    monkeypatch.setattr(fleet_service, "_lock_holder", look)
    holder = threading.Thread(target=a_holder)
    holder.start()
    try:
        assert holding.wait(30)
        if stepping:
            with fleet_service._branches_locked(common, wait=0.05):
                assert len(steps) == 10, "taken once the holder let go"
            assert len(looks) > 10, "waited through more looks than its patience lasts"
        else:
            with (
                pytest.raises(FleetError, match="may be stuck"),
                fleet_service._branches_locked(common, wait=0.05),
            ):
                pytest.fail("taken: the blocked holder said it held until it let go")
    finally:
        let_go.set()
        holder.join(30)


def test_a_spawn_waits_through_a_queue_of_holders_of_the_branch_lock(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 17: the branch lock makes every worktree spawn in a repo wait
    its turn, and the wait was one 120 s for the lock, so the third of three spawns in a
    repo whose checkout is slow was refused behind two ``git worktree add``, though nothing
    was stuck. The wait is per holder now: each says who it is, and another holder than the
    last one seen starts the wait again.

    Each holder keeps it 0.6 s less than the wait and the queue 0.6 s more, so a loaded
    runner's late wake-up is not read as a stuck holder (a 0.15 s hold against a 0.2 s
    wait was, on CI), and one wait for the whole queue is still refused."""
    monkeypatch.setattr(fleet_service, "_BRANCHES_LOCK_WAIT", 1.0)
    monkeypatch.setattr(fleet_service, "_HOLDER_LOOK", 0.05)
    common = fleet_service._git_common_dir(repo)
    first_in = threading.Event()
    released = threading.Event()

    def a_queue() -> None:
        """Four holders, each for less than the wait and all of them for more: what a
        waiter that loses each hand-over sees of four spawns' `git worktree add`. One
        hold, and a new holder announced every 0.4 s, so no hand-over is the waiter's."""
        with locking.holding(common / "aisquare-fleet-branches.lock", wait_s=5):
            for _turn in range(4):
                fleet_service._announce_holder(common)
                first_in.set()
                time.sleep(0.4)
        released.set()

    queue = threading.Thread(target=a_queue)
    queue.start()
    try:
        first_in.wait(5)
        with fleet_service._branches_locked(common, wait=fleet_service._BRANCHES_LOCK_WAIT):
            assert released.is_set(), "taken once the queue let go, 1.6 s on"
    finally:
        queue.join()


def test_a_holder_of_the_branch_lock_that_never_lets_go_is_called_stuck(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fleet_service, "_HOLDER_LOOK", 0.05)
    common = fleet_service._git_common_dir(repo)
    with locking.holding(common / "aisquare-fleet-branches.lock", wait_s=0):
        fleet_service._announce_holder(common)
        started = time.monotonic()
        with (
            pytest.raises(FleetError, match=r"has held it for 0\.2 s without letting go"),
            fleet_service._branches_locked(common, wait=0.2),
        ):
            pytest.fail("taken while another held it")
        assert time.monotonic() - started >= 0.2
        with (
            pytest.raises(FleetError, match=r"holds .* — try again$"),
            fleet_service._branches_locked(common, wait=0),
        ):
            pytest.fail("taken while another held it")


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
        (manager.pane_id, "paste", NUDGE_TEXT),
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
        (manager.pane_id, "paste", NUDGE_TEXT),
        (manager.pane_id, "key", "Enter"),
    ]


def test_nudge_reads_the_panes_last_output_in_its_facts(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 5: the nudge ran `list-panes -a`, every pane on the server, for
    one pane's last output, which the facts it had just read carry (`PaneFacts.last_output`):
    one more tmux process per nudge, in a sub-agent's CLI process. It reads it there, and
    still tells a pane that printed just now (working) from a quiet one (waiting)."""
    manager = fleet_service.spawn(project, "manager").agent
    tmux.set_command(manager.pane_id, "claude")
    _stale_board_session(
        manager, "waiting", seen_ago=team_service._STALE_AFTER + timedelta(minutes=1)
    )
    ran: list[tuple[str, ...]] = []

    def run(*args: str, stdin: bytes | None = None) -> str:
        ran.append(args)
        return ""

    monkeypatch.setattr(tmux, "run", run)

    tmux.printed(manager.pane_id)  # mid-turn
    assert fleet_service.nudge_manager(project.id, reason="task_review") is False
    tmux.printed(manager.pane_id, ago=ACTIVITY_WINDOW + timedelta(seconds=2))
    assert fleet_service.nudge_manager(project.id, reason="task_review") is True

    assert ran == [], "no list-panes"


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
    assert not _is_paused(project)
    fleet_service.pause(project)
    assert _is_paused(project)
    assert _events(project, "signal") == ["fleet-paused: on"]
    fleet_service.resume(project)
    assert not _is_paused(project)
    assert _events(project, "signal")[-1] == "fleet-paused: off (was on)"


def test_pause_with_the_team_disabled_is_a_fleet_error(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_TEAM", "0")
    with pytest.raises(FleetError, match="pause signal"):
        fleet_service.pause(project)
    monkeypatch.delenv("AISQUARE_TEAM")
    assert not _is_paused(project), "nothing was written"


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


def test_attach_argv_asks_the_server_at_the_path_the_projects_rows_recorded(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 29 of #257: `fleet attach` found its server by the socket's name (`tmux -L`)
    while the Manager tab printed `tmux -S <path>`. From a shell under another `TMUX_TMPDIR`
    that is another server: with no session of the project's it refused, saying `spawn
    manager` starts one, which refuses for the live manager; with one of that name it
    attached there. It asks the server at the path the project's live rows recorded."""
    manager = fleet_service.spawn(project, "manager").agent
    manager, stranger = _behind_a_stranger(tmux, project, manager, monkeypatch)
    tmux.path = manager.socket_path  # the handle the stranger hands out for that path
    session = f"asq-{_codename(project)}"
    at_the_path = ["/fake/tmux", "-S", str(manager.socket_path), "attach-session", "-t"]

    assert fleet_service.attach_argv(project) == [*at_the_path, f"={session}"]
    stranger.sessions[session] = []  # a session of that name on the server `-L` reaches
    assert fleet_service.attach_argv(project) == [*at_the_path, f"={session}"]


def test_attach_argv_finds_an_ended_managers_lingering_window_where_its_row_recorded(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Follow-up of #257: with no live row, `fleet attach` fell back to `-L`, so an ended
    manager whose window lingers (`remain-on-exit`) on a server under another `TMUX_TMPDIR`
    was not found: "nothing to attach to", while the 💤 row's last screen stood there. The
    ended rows' recorded paths are asked too, the latest to end first, and `-L` last."""
    manager = fleet_service.spawn(project, "manager").agent
    manager, stranger = _behind_a_stranger(tmux, project, manager, monkeypatch)
    tmux.path = manager.socket_path  # the handle the stranger hands out for that path
    session = f"asq-{_codename(project)}"
    at_the_path = ["/fake/tmux", "-S", str(manager.socket_path), "attach-session", "-t"]
    tmux.die(manager.pane_id, 0)  # the manager exited; its window lingers on its server
    [listed] = fleet_service.list_agents(project)
    assert listed.agent.id == manager.id and listed.agent.ended_at is not None
    assert tmux.has_session(session) and not stranger.has_session(session)

    assert fleet_service.attach_argv(project) == [*at_the_path, f"={session}"]

    # A session of that name on the server `-L` reaches is asked last, not first.
    stranger.sessions[session] = []
    assert fleet_service.attach_argv(project) == [*at_the_path, f"={session}"]
    # And with the lingering window gone, the fallback is still where this shell looks.
    tmux.kill_window(manager.pane_id)
    assert fleet_service.attach_argv(project)[:3] == ["/fake/tmux", "-L", "asq-test"]


def test_attach_argv_asks_no_server_by_the_name_alone_when_a_live_row_recorded_its_own(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 1: the follow-up that asks the ended rows' servers asked this
    shell's server (`-L`) last whatever the live rows said. With the live manager's
    recorded server unreachable (its socket file taken), a shell under another
    `TMUX_TMPDIR` attached to the session of that name on its own server, which round 29
    of #257 had refused. An ended row that recorded no path is asked by the name too, so it
    is left out as well. With no live row, both are asked, as the follow-up meant."""
    manager = fleet_service.spawn(project, "manager").agent
    manager, stranger = _behind_a_stranger(tmux, project, manager, monkeypatch)
    session = f"asq-{_codename(project)}"
    with store_session() as store:
        store.upsert_fleet_agent(
            manager.model_copy(
                update={
                    "id": "agt_before_v25",
                    "label": "manager-0",
                    "socket_path": None,
                    "ended_at": manager.created_at,
                }
            )
        )
    tmux.running = False  # no tmux client reaches the manager's server any more
    stranger.sessions[session] = []  # a session of that name on the server `-L` reaches

    with pytest.raises(FleetError, match="nothing to attach to"):
        fleet_service.attach_argv(project)

    with store_session() as store:  # the manager ended: no live row says where to look
        store.end_fleet_agent(manager.id, exit_status=0)
    assert fleet_service.attach_argv(project)[:3] == ["/fake/tmux", "-L", "asq-test"]


def test_attach_argv_resolves_each_recorded_server_once_however_many_rows_ended_on_it(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 1: attach worked out where every row the project ever had is
    asked (`_address`, two paths resolved each), and rows are never deleted. Rows that
    recorded one server are one question, asked in the order the rows give."""
    manager = fleet_service.spawn(project, "manager").agent
    with store_session() as store:
        ended = store.end_fleet_agent(manager.id, exit_status=0)
        for n in range(40):
            store.upsert_fleet_agent(
                ended.model_copy(update={"id": f"agt_old_{n}", "label": f"coder-{n}"})
            )
        assert len(store.fleet_agents(project.id, live_only=False)) == 41
        assert store.fleet_agents(project.id, live_only=True) == []
    resolved: list[str] = []
    address = fleet_service._address

    def counted(agent: FleetAgent, config: FleetSettings | None = None) -> object:
        resolved.append(agent.id)
        return address(agent, config)

    monkeypatch.setattr(fleet_service, "_address", counted)

    assert fleet_service.attach_argv(project)[-1] == f"=asq-{_codename(project)}"
    assert len(resolved) == 1, resolved


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
        # The argv wraps at the pane's width: read it with the wrapped rows joined.
        said = "".join(real.capture(pane).lines)
        assert f"--session-id {receipt.agent.session_id}" in said, screen
        assert "--name coder-1" in said and "--permission-mode auto" in said, screen
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


def test_spawn_carries_this_shells_desktop_into_the_window(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#147: a window inherits the tmux SERVER's environment, frozen at its first
    start, so after a re-login every new agent had a stale display and SSH agent
    socket. The spawner's current values are set on the window (`new-window -e`)."""
    monkeypatch.setattr(
        tmux_core,
        "desktop_environment",
        lambda environ=None: {"WAYLAND_DISPLAY": "wayland-1", "SSH_AUTH_SOCK": "/run/u/1/ssh"},
    )
    _coder(project)
    env = tmux.spawned[-1]["env"]
    assert isinstance(env, dict)
    assert env["WAYLAND_DISPLAY"] == "wayland-1" and env["SSH_AUTH_SOCK"] == "/run/u/1/ssh"
    assert "AISQUARE_FLEET_AGENT" in env  # the fleet's own variables still travel


def test_spawn_resolves_the_default_account_and_records_the_slot_on_the_row(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """#145: the window gets the RESOLVED `--account <slot>`, the caller's environment travels
    with it, and the row says which account the agent draws on."""
    from aisquare.core import claude_accounts as accounts_core
    from aisquare.services import claude_accounts as accounts_service
    from aisquare.services import settings as settings_service

    accounts_core.create_account()  # slot 2, under the isolated AISQUARE_HOME

    # The control: nothing arranged → no flag, no carried environment, no slot on the row.
    plain = fleet_service.spawn(project, "coder")
    assert _flag(_command(tmux), "--account") is None
    assert plain.agent.account_slot is None
    plain_env = tmux.spawned[-1]["env"]
    assert isinstance(plain_env, dict) and "AISQUARE_HOME" not in plain_env

    accounts_service.set_default("2")
    chosen = fleet_service.spawn(project, "tester")
    command, env = _command(tmux), tmux.spawned[-1]["env"]
    assert isinstance(env, dict)
    assert _flag(command, "--account") == "2"  # the slot, never the alias or the email
    assert env["AISQUARE_HOME"] == str(Path(os.environ["AISQUARE_HOME"]).absolute())
    assert chosen.agent.account_slot == 2
    with store_session() as store:
        stored = store.get_fleet_agent(chosen.agent.id)
        assert stored is not None and stored.account_slot == 2

    # A role binding outranks the machine default, and reaches the window the same way.
    settings_service.bind_role("reviewer", account="1")
    bound = fleet_service.spawn(project, "reviewer", worktree=False)
    assert _flag(_command(tmux), "--account") == "1"
    assert bound.agent.account_slot == 1

    # A binding to an account this machine does not have refuses the spawn with the rung named,
    # instead of starting a window that dies on its first line.
    settings_service.bind_role("validator", account="9")
    with pytest.raises(fleet_service.FleetError, match="role binding for 'validator'"):
        fleet_service.spawn(project, "validator")


# --- fleet switch (#146): a hand-over to another account --------------------------------------


def _two_slots_with_usage(monkeypatch: pytest.MonkeyPatch, work: float, personal: float) -> None:
    """Slots 2 and 3 signed in (under the isolated home) with a scripted usage endpoint."""
    from tests.test_usage_aware_accounts import _payload, _slot, _Usage

    # The real clock runs here (no redirected-home clock), so the tokens must outlive it.
    _slot("work@example.com", "tok-work", expires_in=timedelta(days=3650))
    _slot("personal@example.com", "tok-personal", expires_in=timedelta(days=3650))
    from aisquare.services import claude_accounts as accounts_service

    monkeypatch.setattr(
        accounts_service,
        "_http_get",
        _Usage({"tok-work": _payload(work), "tok-personal": _payload(personal)}),
    )


def _with_transcript(agent: FleetAgent, transcript: Path | None) -> None:
    """The board row the agent's hooks would have written, naming its transcript."""
    assert agent.session_id is not None
    now = datetime.now(tz=UTC)
    with store_session() as store:
        store.upsert_session(
            TeamSession(
                id=agent.session_id,
                project_id=agent.project_id,
                role=agent.role,
                started_at=now,
                last_seen_at=now,
                transcript_path=str(transcript) if transcript is not None else None,
            )
        )


def _session_state(session_id: str) -> str:
    with store_session() as store:
        session = store.get_session(session_id)
    assert session is not None
    return session.state


def test_switch_moves_a_limited_agent_to_the_account_with_headroom_and_resumes_it(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _two_slots_with_usage(monkeypatch, work=95, personal=10)
    agent = fleet_service.spawn(project, "coder", worktree=False, account="2").agent
    transcript = tmp_path / f"{agent.session_id}.jsonl"
    transcript.write_text('{"type":"user"}\n', encoding="utf-8")
    _with_transcript(agent, transcript)
    now = datetime.now(tz=UTC)
    with store_session() as store:
        store.mark_limited(agent.session_id or "", now + timedelta(hours=2))
        task, _created = store.upsert_task(
            TeamTask(
                id="tsk_keep1",
                project_id=project.id,
                key="keep-it",
                title="Keep it",
                role="coder",
                created_at=now,
                updated_at=now,
            )
        )
    assert team_service.claim_task(task.id, session_ref=agent.session_id or "").status == "doing"
    [before] = fleet_service.list_agents(project)
    assert before.state == "limited" and before.detail is not None
    assert before.detail.startswith("limit resets in ")  # the one formatter: a distance
    # The old process's own SessionEnd lands during the grace, before the window goes: it
    # finds the session marked, and parks the claims as a /clear does, for a live row.
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    real_kill = tmux.kill_window
    marked_at_exit: list[str] = []

    def exit_then_kill(pane_id: str) -> None:
        if pane_id == agent.pane_id:
            marked_at_exit.append(_session_state(agent.session_id or ""))
            team_service.hook_session_end(
                agent.session_id or "", project.root, reason="prompt_input_exit"
            )
        real_kill(pane_id)

    monkeypatch.setattr(tmux, "kill_window", exit_then_kill)

    receipt = fleet_service.switch(project, agent.label, reason="session limit")

    assert (receipt.from_slot, receipt.to_slot, receipt.resumed) == (2, 3, True)
    command = _command(tmux)
    assert _flag(command, "--account") == "3"  # the account with room, never the one it left
    assert _flag(command, "--resume") == str(transcript)
    assert "--session-id" not in command  # resuming keeps the id; none is minted
    assert receipt.started.session_id == agent.session_id  # the same board session carries on
    assert receipt.started.label == agent.label and receipt.started.account_slot == 3
    assert receipt.started.pane_id != agent.pane_id
    assert agent.pane_id in tmux.killed  # the old window is gone…
    assert receipt.stopped.ended_at is not None  # …and its row ended
    assert tmux.typed[:2] == [(agent.pane_id, "literal", "/exit"), (agent.pane_id, "key", "Enter")]
    # The resumed conversation opens at an idle prompt: ONE line tells it to go on
    # (review of #205, finding 5) — one line, so it is typed even past the wait.
    new_pane = receipt.started.pane_id
    pasted = _first_lines(tmux, new_pane)
    assert len(pasted) == 1 and "\n" not in pasted[0]
    assert pasted[0].startswith(
        f"You are {agent.label}, moved to another Claude account after session limit"
    )
    assert "continue exactly where you left off" in pasted[0]
    assert (new_pane, "key", "Enter") not in tmux.typed, "on its command line: no Enter"
    live = fleet_service.list_agents(project)
    assert [status.agent.id for status in live] == [receipt.started.id]
    with store_session() as store:
        kinds = [(e.kind, e.text) for e in store.recent_events(project.id, limit=10)]
        kept = store.get_task(task.id)
        ended = store.get_session(agent.session_id or "")
    [switched] = [text for kind, text in kinds if kind == "switched"]
    assert "moved from account 2 (slot 2) to account 3 (slot 3) (session limit)" in switched
    assert switched.endswith("— resumed its session")
    assert any("headroom:" in note for note in receipt.notes)  # the pick is explained
    # A hand-over is not an exit (finding 6): the claim stays with the session that resumes,
    # nothing was released, no `agent_exited` woke the manager to respawn a coming-back agent.
    assert marked_at_exit == [team_service.HANDOVER_STATE]
    assert kept is not None and kept.status == "doing" and kept.claimed_by == agent.session_id
    assert not any(kind in {"agent_exited", "task_released"} for kind, _ in kinds)
    # The mark's one reader has run, so `switch` took the mark back (the #205 fold, round 2).
    assert ended is not None and ended.ended_at is not None and ended.state == "limited"
    # The resumed session's SessionStart, on the SAME id, finds the claims still its own.
    team_service.hook_session_start(agent.session_id or "", project.root, "resume")
    with store_session() as store:
        revived = store.get_session(agent.session_id or "")
        still = store.get_task(task.id)
    # A resumed session sits at its prompt until the resume line is typed (#123): `waiting`.
    assert revived is not None and revived.ended_at is None and revived.state == "waiting"
    assert still is not None and still.status == "doing" and still.claimed_by == agent.session_id


def test_the_receipts_are_immutable() -> None:
    """``SpawnReceipt`` lost ``frozen=True`` when its neighbours were inserted (review of #205,
    second round); every caller reads a receipt, none may rewrite one."""
    import dataclasses

    for receipt in (fleet_service.SpawnReceipt, fleet_service.SwitchReceipt):
        params = getattr(receipt, "__dataclass_params__", None)
        assert dataclasses.is_dataclass(receipt) and params is not None and params.frozen, receipt


def _third_slot_with_usage(
    monkeypatch: pytest.MonkeyPatch, *, work: float, personal: float, third: float
) -> None:
    """Slots 2, 3 and 4 signed in, with one scripted usage endpoint for all three."""
    from tests.test_usage_aware_accounts import _payload, _slot, _Usage

    _two_slots_with_usage(monkeypatch, work=work, personal=personal)
    _slot("third@example.com", "tok-third", expires_in=timedelta(days=3650))
    from aisquare.services import claude_accounts as accounts_service

    monkeypatch.setattr(
        accounts_service,
        "_http_get",
        _Usage(
            {
                "tok-work": _payload(work),
                "tok-personal": _payload(personal),
                "tok-third": _payload(third),
            }
        ),
    )


def test_a_switch_picks_by_headroom_before_the_binding_and_the_automatic_one_refuses_without_room(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #205, second round: the binding and project-default rungs sat above headroom
    inside ``choose(spread=True)``, so a bound role moved to its bound account however full;
    and with every account over the line the least-bad answer bounced an agent between two
    exhausted accounts for the whole window."""
    from aisquare.services import settings as settings_service

    _third_slot_with_usage(monkeypatch, work=95, personal=99, third=10)
    settings_service.bind_role("coder", account="3")  # bound to the 99 % account
    agent = fleet_service.spawn(project, "coder", worktree=False, account="2").agent
    _with_transcript(agent, None)

    receipt = fleet_service.switch(project, agent.label)

    assert receipt.to_slot == 4  # headroom, not the binding
    assert any(
        "headroom:" in note and "account 4 is first under the 85%-used line" in note
        for note in receipt.notes
    )
    assert "--to" not in _command(tmux) and _flag(_command(tmux), "--account") == "4"

    # Every account over the line: by hand the least-bad one still moves it (as before)…
    from aisquare.services import claude_accounts as accounts_service
    from tests.test_usage_aware_accounts import _payload, _Usage

    monkeypatch.setattr(  # the same three slots, the endpoint re-scripted
        accounts_service,
        "_http_get",
        _Usage({"tok-work": _payload(95), "tok-personal": _payload(99), "tok-third": _payload(90)}),
    )
    manual = fleet_service.switch(project, agent.label, to=None)
    assert manual.to_slot in {2, 3}  # it was on 4; the least-bad of the rest
    assert any("every account is over the 85%-used line" in note for note in manual.notes)
    # …but the automatic hand-over refuses, stops nothing, and says why.
    killed_before = list(tmux.killed)
    with pytest.raises(FleetError, match="no account under the line") as refused:
        fleet_service.switch(project, agent.label, automatic=True, reason="session limit")
    assert "every other account is over switch_at" in str(refused.value)
    assert tmux.killed == killed_before
    assert [status.agent.label for status in fleet_service.list_agents(project)] == [agent.label]


def test_a_manual_switch_falls_back_to_the_ladder_when_no_usage_can_be_read(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aisquare.services import claude_accounts as accounts_service
    from tests.test_usage_aware_accounts import _Usage

    _two_slots_with_usage(monkeypatch, work=95, personal=10)
    accounts_service.set_default("3")
    agent = fleet_service.spawn(project, "coder", worktree=False, account="2").agent
    _with_transcript(agent, None)
    monkeypatch.setattr(accounts_service, "_http_get", _Usage({}))  # every token rejected

    receipt = fleet_service.switch(project, agent.label)

    assert receipt.to_slot == 3  # the machine default, the account being left excluded
    assert any("no account's usage could be read" in note for note in receipt.notes)
    with pytest.raises(FleetError, match="no account under the line"):
        fleet_service.switch(project, agent.label, automatic=True)


def test_a_switch_that_finds_nothing_says_the_default_was_the_account_being_left(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``choose`` promises ``exclude`` is honoured on every rung WITH a note; the machine
    default's rung skipped the account being left in silence, and the refusal of a manual
    switch carried no notes at all — "no other account with headroom" and nothing on why
    (review of #205, fourth round)."""
    from aisquare.services import claude_accounts as accounts_service
    from tests.test_usage_aware_accounts import _Usage

    _two_slots_with_usage(monkeypatch, work=95, personal=10)
    accounts_service.set_default("2")
    agent = fleet_service.spawn(project, "coder", worktree=False, account="2").agent
    _with_transcript(agent, None)
    monkeypatch.setattr(accounts_service, "_http_get", _Usage({}))  # every token rejected

    with pytest.raises(FleetError, match="no other account with headroom") as refused:
        fleet_service.switch(project, agent.label)

    said = str(refused.value)
    assert "account 2 (machine default) is the account being left — skipped" in said
    assert "no account's usage could be read" in said
    assert agent.pane_id not in tmux.killed  # refused before anything was stopped


def test_a_switch_that_cannot_read_the_registry_says_so_automatic_or_not(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registry it cannot read picks nothing (the #205 fold, round 1), and the refusal is the
    only place the reason is said. The automatic one kept only the ``headroom:`` notes, so
    its board note blamed usage and ended in an empty ``()`` (the #205 fold, round 2)."""
    from aisquare.core import claude_accounts as accounts_core
    from aisquare.services import claude_accounts as accounts_service

    _two_slots_with_usage(monkeypatch, work=95, personal=10)
    agent = fleet_service.spawn(project, "coder", worktree=False, account="2").agent
    _with_transcript(agent, None)
    monkeypatch.setattr(
        accounts_service,
        "_read_arranged",
        lambda: (
            accounts_core.list_accounts(),
            "accounts registry unreadable (database is locked)",
        ),
    )

    for automatic in (True, False):
        with pytest.raises(FleetError) as refused:
            fleet_service.switch(project, agent.label, automatic=automatic)
        said = str(refused.value)
        assert "(accounts registry unreadable (database is locked))" in said, said
        assert "()" not in said
    assert agent.pane_id not in tmux.killed  # refused before anything was stopped


@pytest.mark.parametrize("left_at", [95, 10], ids=["five-hour-window-full", "weekly-limit"])
def test_an_automatic_hand_over_from_a_managed_slot_reads_and_launches_the_plain_claude(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    left_at: float,
) -> None:
    """Review of #205, sixth round. The automatic hand-over runs in the worker the limited
    agent's hook detached, so it runs under the agent's own slot, with ``CLAUDE_CONFIG_DIR``
    naming it, and slot 1 read from that variable WAS the slot. With the slot's five-hour
    window full, slot 1 read the slot's 95 % and nothing was under the line. With that
    window nearly empty (a weekly limit), slot 1 was picked, and the window got
    ``--account 1`` with the slot's variables still set: the limited agent relaunched on
    its limited account. Either way the slot's reading was recorded as slot 1's."""
    from aisquare.core import claude_accounts as accounts_core
    from aisquare.services import claude_accounts as accounts_service
    from tests.test_claude_accounts import _sign_in
    from tests.test_usage_aware_accounts import _payload, _slot, _Usage

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setattr(accounts_core, "_home", lambda: home)
    monkeypatch.setattr(accounts_core, "keychain_platform", lambda: False)
    plain = accounts_core.default_account()
    _sign_in(plain, "plain@example.com", expires_in=timedelta(days=3650))
    credentials = accounts_core.credentials_path(plain)
    signed_in = json.loads(credentials.read_text())
    signed_in["claudeAiOauth"]["accessToken"] = "tok-plain"
    credentials.write_text(json.dumps(signed_in))
    work = _slot("work@example.com", "tok-work", expires_in=timedelta(days=3650))
    fetch = _Usage({"tok-plain": _payload(5), "tok-work": _payload(left_at)})
    monkeypatch.setattr(accounts_service, "_http_get", fetch)
    agent = fleet_service.spawn(project, "coder", worktree=False, account="2").agent
    _with_transcript(agent, None)
    # The worker's environment: what `launch --account 2` gave the agent, from a shell
    # that set neither variable.
    for var, value in accounts_core.apply_launch_env({}, work, shell={}).items():
        monkeypatch.setenv(var, value)

    receipt = fleet_service.switch(project, agent.label, automatic=True, reason="session limit")

    assert (receipt.from_slot, receipt.to_slot) == (2, 1)
    assert "tok-plain" in fetch.calls  # slot 1 read with its own credentials
    command, env = _command(tmux), tmux.spawned[-1]["env"]
    assert isinstance(env, dict)
    assert _flag(command, "--account") == "1"
    # The window starts the plain claude: the slot's two variables unset, not carried.
    assert accounts_core.CONFIG_DIR_VAR not in env and accounts_core.TMPDIR_VAR not in env
    unset = {command[at + 1] for at, part in enumerate(command) if part == "-u"}
    assert {accounts_core.CONFIG_DIR_VAR, accounts_core.TMPDIR_VAR} <= unset
    with store_session() as store:
        [sample] = store.usage_samples(1, since=datetime.now(tz=UTC) - timedelta(hours=1))
    assert sample.session_percent == 5  # the plain claude's reading, not the slot's


@pytest.mark.parametrize("fresh", [False, True], ids=["resume", "fresh"])
def test_a_hand_over_that_does_not_complete_leaves_nothing_parked(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fresh: bool,
) -> None:
    """The two ways a switch can die after it has marked the session (review of #205, finding 6):
    a stop that raises leaves the session as it was; a spawn that raises releases the
    parked claims and records the exit the hand-over withheld.

    Resumed or fresh alike: since a fresh switch is a hand-over too, ``stop`` parks its
    claims as well, and the release after a failed spawn is the only thing that returns
    them. With the resume leg alone, that release could be limited to a resume and
    nothing failed (review of #205, fifth round)."""
    _two_slots_with_usage(monkeypatch, work=95, personal=10)
    agent = fleet_service.spawn(project, "coder", worktree=False, account="2").agent
    transcript = tmp_path / f"{agent.session_id}.jsonl"
    transcript.write_text('{"type":"user"}\n', encoding="utf-8")
    _with_transcript(agent, transcript)
    now = datetime.now(tz=UTC)
    with store_session() as store:
        store.mark_limited(agent.session_id or "", now + timedelta(hours=2))
        task, _created = store.upsert_task(
            TeamTask(
                id="tsk_keep2",
                project_id=project.id,
                key="keep-it-2",
                title="Keep it",
                role="coder",
                created_at=now,
                updated_at=now,
            )
        )
    assert team_service.claim_task(task.id, session_ref=agent.session_id or "").status == "doing"

    def stop_refuses(*args: Any, **kwargs: Any) -> fleet_service.StopReceipt:
        raise FleetError("tmux would not answer")

    real_stop = fleet_service.stop
    monkeypatch.setattr(fleet_service, "stop", stop_refuses)
    with pytest.raises(FleetError, match="would not answer"):
        fleet_service.switch(project, agent.label, fresh=fresh, reason="session limit")
    with store_session() as store:
        session = store.get_session(agent.session_id or "")
        held = store.get_task(task.id)
    assert session is not None and session.state == "limited"  # as it was, not `switching`
    assert held is not None and held.claimed_by == agent.session_id
    monkeypatch.setattr(fleet_service, "stop", real_stop)

    asked: list[dict[str, Any]] = []

    def spawn_refuses(*args: Any, **kwargs: Any) -> fleet_service.SpawnReceipt:
        asked.append(kwargs)
        raise FleetError("tmux could not start the window: boom")

    monkeypatch.setattr(fleet_service, "spawn", spawn_refuses)
    with pytest.raises(FleetError, match="boom"):
        fleet_service.switch(project, agent.label, fresh=fresh, reason="session limit")
    [spawned] = asked  # the leg this is: a fresh replacement takes over, a resumed one resumes
    if fresh:
        assert spawned["resume"] is None and spawned["takes_over"] == agent.session_id
    else:
        assert spawned["resume"] is not None and spawned["takes_over"] is None
    with store_session() as store:
        released = store.get_task(task.id)
        kinds = [(e.kind, e.text) for e in store.recent_events(project.id, limit=10)]
        rows = store.fleet_agents(project.id)
    assert released is not None and released.status == "todo" and released.claimed_by is None
    assert any(kind == "task_released" and "hand-over failed" in text for kind, text in kinds)
    exited = f"{agent.label} exited (0): {fleet_service.HANDOVER_FAILED}"
    assert ("agent_exited", exited) in kinds, "not the clean exit a stop announces"
    assert all(row.ended_at is not None for row in rows)  # the old row ended; no replacement


def test_a_hand_overs_mark_does_not_outlive_the_stop_it_was_set_for(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Review of the #205 fold, round 2. Every state writer keeps the ``switching`` mark
    (the store, since round 1), so nothing but ``switch`` and the session's own start takes
    it off — and ``switch`` did so only for a stop that raised an ``Exception``. A Ctrl-C in
    the grace is a ``KeyboardInterrupt`` and left a running agent marked; a completed resume
    left it to the replacement's start hook, and one that failed open left the agent
    ``switching`` through every later prompt: no ``limited`` recorded, no hand-over, no bell.
    The mark's one reader is the old process's ``SessionEnd``, which has run by the time
    ``stop`` returns (or never will), so ``switch`` takes the mark back there."""
    _two_slots_with_usage(monkeypatch, work=95, personal=10)
    agent = fleet_service.spawn(project, "coder", worktree=False, account="2").agent
    sid = agent.session_id or ""
    transcript = tmp_path / f"{sid}.jsonl"
    transcript.write_text('{"type":"user"}\n', encoding="utf-8")
    _with_transcript(agent, transcript)
    with store_session() as store:
        store.mark_limited(sid, datetime.now(tz=UTC) + timedelta(hours=2))
    task = _task(project, "keep it across the move")
    assert team_service.claim_task(task.id, session_ref=sid).status == "doing"

    def interrupted(*args: Any, **kwargs: Any) -> fleet_service.StopReceipt:
        raise KeyboardInterrupt

    real_stop = fleet_service.stop
    monkeypatch.setattr(fleet_service, "stop", interrupted)
    with pytest.raises(KeyboardInterrupt):
        fleet_service.switch(project, agent.label, reason="session limit")
    assert _session_state(sid) == "limited"  # as it was: the agent is still running
    monkeypatch.setattr(fleet_service, "stop", real_stop)

    # Completed: the old SessionEnd lands in the grace and parks the claims for the resume…
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    real_kill = tmux.kill_window

    def exit_then_kill(pane_id: str) -> None:
        if pane_id == agent.pane_id:
            team_service.hook_session_end(sid, project.root, reason="prompt_input_exit")
        real_kill(pane_id)

    monkeypatch.setattr(tmux, "kill_window", exit_then_kill)
    receipt = fleet_service.switch(project, agent.label, reason="session limit")
    assert receipt.resumed and receipt.started.session_id == sid
    # …the replacement's SessionStart never lands (a start hook that failed open), and the
    # heartbeat of the resume line it was typed repairs the row, as every prompt's always did.
    team_service.hook_prompt_heartbeat(sid, project.root)
    with store_session() as store:
        resumed = store.get_session(sid)
    assert resumed is not None and resumed.ended_at is None and resumed.state == "working"
    held = _task_now(task.id)
    assert held.status == "doing" and held.claimed_by == sid  # parked, never released
    # The same agent can be limited, and handed over, again.
    again = team_service.hook_stop_failure(
        sid, error="rate_limit", message="You've hit your session limit", details=None
    )
    assert again is not None and again.limited and not again.already_limited


def test_a_hand_overs_mark_that_could_not_be_taken_back_is_said_on_the_receipt(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The unmark is best effort, and it failed in silence: on a locked store the row kept
    ``switching`` with nothing on the receipt to say so — and a replacement whose start hook
    failed open too stayed there through every prompt (review of the #205 fold, round 3).
    The move still stands; the receipt says what was left behind."""
    _two_slots_with_usage(monkeypatch, work=95, personal=10)
    agent = fleet_service.spawn(project, "coder", worktree=False, account="2").agent
    sid = agent.session_id or ""
    transcript = tmp_path / f"{sid}.jsonl"
    transcript.write_text('{"type":"user"}\n', encoding="utf-8")
    _with_transcript(agent, transcript)
    with store_session() as store:
        store.mark_limited(sid, datetime.now(tz=UTC) + timedelta(hours=2))

    def locked(self: SqliteStore, session_id: str, state: str) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(SqliteStore, "unmark_handover", locked)

    receipt = fleet_service.switch(project, agent.label, reason="session limit")

    assert receipt.resumed and receipt.started.session_id == sid  # moved all the same
    assert _session_state(sid) == team_service.HANDOVER_STATE  # the control: it did stay on
    [said] = [note for note in receipt.notes if "hand-over mark" in note]
    assert "OperationalError: database is locked" in said and "`switching`" in said


def test_switch_starts_fresh_with_a_hand_off_prompt_when_asked_or_when_there_is_no_transcript(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _two_slots_with_usage(monkeypatch, work=95, personal=10)
    with store_session() as store:
        task, _created = store.upsert_task(
            TeamTask(
                id="tsk_switch1",
                project_id=project.id,
                key="ship-auth",
                title="Ship auth",
                detail="objective: make login work · acceptance: tests green",
                role="coder",
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            )
        )
    agent = fleet_service.spawn(
        project, "coder", worktree=False, account="2", task_id=task.id
    ).agent
    _with_transcript(agent, tmp_path / "missing.jsonl")  # named, not on disk
    with store_session() as store:
        team_service._emit(
            store, project.id, "note", "halfway through the router", session_id=agent.session_id
        )
        # A busy board: seventy newer entries from other agents used to push the
        # session's own out of the window the prompt was built from (third round).
        for n in range(70):
            team_service._emit(store, project.id, "note", f"chatter {n}", session_id="sess-other")
    # The new pane must look ready, or a multi-line prompt is (correctly) not typed.
    original_spawn = tmux.spawn_window

    def ready_spawn(*args: Any, **kwargs: Any) -> WindowInfo:
        window = original_spawn(*args, **kwargs)
        tmux.set_command(window.pane_id, "claude")
        return window

    monkeypatch.setattr(tmux, "spawn_window", ready_spawn)

    receipt = fleet_service.switch(project, agent.label, to="personal@example.com")

    assert receipt.resumed is False and receipt.to_slot == 3
    assert any("no transcript on disk" in note for note in receipt.notes)
    command = _command(tmux)
    assert "--resume" not in command and _flag(command, "--account") == "3"
    assert _flag(command, "--task") is None  # the task travels on the row, as spawn records it
    assert receipt.started.task_id == task.id
    pasted = _first_lines(tmux, receipt.started.pane_id)
    assert len(pasted) == 1
    prompt = pasted[0]
    assert prompt.startswith(f"You are {agent.label}, taking over")
    assert "Ship auth (tsk_switch1)" in prompt and "make login work" in prompt
    assert "- note: halfway through the router" in prompt
    assert f"aisquare task show {task.id}" in prompt and "git status" in prompt

    # --fresh on an agent WITH a transcript still starts over, by choice.
    transcript = tmp_path / "present.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    _with_transcript(receipt.started, transcript)
    again = fleet_service.switch(project, agent.label, to="2", fresh=True)
    assert again.resumed is False and again.to_slot == 2
    assert "--resume" not in _command(tmux)


def test_a_hand_over_stop_releases_nothing_and_announces_nothing(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The merge of the release train into #205. ``stop`` returns a ``StopReceipt``
    and REPORTS a release the store refused rather than raising it (review of the
    fold), and it gained ``handover`` for ``switch``. A hand-over's receipt names no
    release — the claims wait for the replacement — and no exit goes out for it."""
    _two_slots_with_usage(monkeypatch, work=95, personal=10)
    parked_task = _task(project, "parked across the hand-over")

    parked = fleet_service.spawn(project, "coder", worktree=False, account="2").agent
    _with_transcript(parked, None)
    team_service.claim_task(parked_task.id, session_ref=parked.session_id or "")
    receipt = fleet_service.stop(project, parked.label, handover=True)
    assert receipt.agent.ended_at is not None
    assert (receipt.released, receipt.release_failed) == ([], None)
    assert _task_now(parked_task.id).claimed_by == parked.session_id  # parked, not returned
    with store_session() as store:
        kinds = [e.kind for e in store.recent_events(project.id, limit=10)]
    assert "agent_exited" not in kinds and "task_released" not in kinds


@pytest.mark.parametrize("exits", [True, False], ids=["exits-in-the-grace", "killed"])
@pytest.mark.parametrize("fresh", [True, False], ids=["asked-fresh", "no-transcript"])
def test_a_fresh_switch_hands_the_claims_to_the_replacement_and_announces_no_exit(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fresh: bool,
    exits: bool,
) -> None:
    """Review of #205, fourth round. A switch with no transcript to resume — or with
    ``--fresh`` — stopped the agent as ``fleet stop`` does: its task went back to the
    pool and ``agent_exited`` woke the manager while ``spawn`` was still starting the
    replacement, so the manager staffed the task a second time. A fresh start is a
    hand-over too: the claims move onto the replacement's new session with its row,
    nothing is released or announced, and the replacement is briefed on its task as
    work already its own.

    ``killed`` is an agent that never answers the ``/exit``: no ``SessionEnd`` fires, so
    ``_take_over`` is the only thing that retires the old presence. With every leg
    exiting in the grace, the old session's own hook ended it and a ``_take_over`` that
    retired nothing passed (review of #205, fifth round)."""
    _two_slots_with_usage(monkeypatch, work=95, personal=10)
    task = _task(project, "keep it across the move")
    agent = fleet_service.spawn(
        project, "coder", worktree=False, account="2", task_id=task.id
    ).agent
    old = agent.session_id or ""
    transcript = tmp_path / f"{old}.jsonl"
    if fresh:
        transcript.write_text('{"type":"user"}\n', encoding="utf-8")  # there, and not used
    _with_transcript(agent, transcript)
    assert team_service.claim_task(task.id, session_ref=old).status == "doing"
    nudges: list[str] = []

    def nudge(project_id: str, *, reason: str, board: str | None = None) -> bool:
        nudges.append(reason)
        return True

    monkeypatch.setattr(fleet_service, "nudge_manager", nudge)
    if exits:
        # The old process's own SessionEnd lands during the grace, while its row is live.
        monkeypatch.setenv("AISQUARE_ROLE", "coder")
        real_kill = tmux.kill_window

        def exit_then_kill(pane_id: str) -> None:
            if pane_id == agent.pane_id:
                team_service.hook_session_end(old, project.root, reason="prompt_input_exit")
            real_kill(pane_id)

        monkeypatch.setattr(tmux, "kill_window", exit_then_kill)

    receipt = fleet_service.switch(project, agent.label, fresh=fresh, reason="session limit")

    new = receipt.started.session_id
    assert receipt.resumed is False and new is not None and new != old
    assert "--resume" not in _command(tmux)
    assert _flag(_command(tmux), "--session-id") == new  # the id the new process starts as
    held = _task_now(task.id)
    assert held.status == "doing" and held.claimed_by == new  # moved, never in the pool
    assert held.claim_expires_at is not None and held.claim_expires_at > datetime.now(tz=UTC)
    with store_session() as store:
        kinds = [e.kind for e in store.recent_events(project.id, limit=20)]
        retired = store.get_session(old)
    assert "switched" in kinds
    assert "agent_exited" not in kinds and "task_released" not in kinds
    assert nudges == []  # nothing woke the manager to staff the task again
    assert retired is not None and retired.ended_at is not None  # no ghost mid-switch
    # The replacement's first SessionStart: the task is its own work in flight.
    _become(receipt.started, tmux, monkeypatch, pid=PANE_PID, role="coder")
    board = team_service.hook_session_start(new, project.root, "startup")
    assert "You are the one working it" in _assignment_block(board)
    assert _task_now(task.id).claimed_by == new


def test_a_fresh_switch_whose_claims_cannot_be_moved_leaves_them_with_the_row(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_take_over`` fails open: the window runs and its row is recorded, so a store
    that refuses the move leaves the row on the old id WITH the claims — the state a
    ``/clear`` leaves between its hooks — the switch says so, and the replacement's
    own start hook makes the move (its pane's process adopts the row).

    That state includes the ended presence. The old session was retired only AFTER the
    move, so a refused move left it live as ``switching`` — a killed agent fires no
    ``SessionEnd`` — until the prune (review of #205, fifth round)."""
    _two_slots_with_usage(monkeypatch, work=95, personal=10)
    task = _task(project, "moved by the start hook instead")
    agent = fleet_service.spawn(
        project, "coder", worktree=False, account="2", task_id=task.id
    ).agent
    old = agent.session_id or ""
    _with_transcript(agent, None)
    team_service.claim_task(task.id, session_ref=old)
    original = SqliteStore.adopt_fleet_agent_session
    refusing = True

    def locked(self: SqliteStore, *args: Any, **kwargs: Any) -> bool:
        if refusing:
            raise sqlite3.OperationalError("database is locked (fake)")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(SqliteStore, "adopt_fleet_agent_session", locked)

    receipt = fleet_service.switch(project, agent.label, reason="session limit")

    assert any("claims were not moved" in note and "locked" in note for note in receipt.notes)
    assert receipt.started.session_id == old  # the row waits on the id holding the claims
    assert _task_now(task.id).claimed_by == old  # parked, not released
    with store_session() as store:
        retired = store.get_session(old)
        kinds = [e.kind for e in store.recent_events(project.id, limit=20)]
    assert retired is not None and retired.ended_at is not None  # no ghost mid-switch
    assert "agent_exited" not in kinds and "task_released" not in kinds
    refusing = False
    new = _flag(_command(tmux), "--session-id")
    assert new is not None and new != old
    _become(receipt.started, tmux, monkeypatch, pid=PANE_PID, role="coder")
    board = team_service.hook_session_start(new, project.root, "startup")
    assert "You are the one working it" in _assignment_block(board)
    assert _task_now(task.id).claimed_by == new and _row(receipt.started.id).session_id == new


def test_a_fresh_switch_whose_old_presence_cannot_be_retired_still_moves_the_claims(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #205, sixth round. Since the fifth, ``_take_over`` retires the old presence
    BEFORE the move, each in a commit of its own, inside one ``try``: a store that refused
    the retirement skipped the move too, and the claims waited on the old id for the start
    hook. Before, only the retirement was lost; now it is again. The loss is said, too:
    unexplained, the old id sat on the board as a live session until the prune (seventh
    round).

    The refusal is the COMMIT's, after the UPDATE ran, as ``database is locked`` refuses
    one: the transaction stays open with the retirement in it. Refused before any SQL,
    as this test first did, it could not see that the claim move's commit then carried
    the retirement, so the session WAS ended under a note saying it was not (review of
    the #205 fold, A1 and A3). The note and the store must agree, and the note's advice
    must be what a prune does (A2)."""
    _two_slots_with_usage(monkeypatch, work=95, personal=10)
    task = _task(project, "moved though the presence stayed")
    agent = fleet_service.spawn(
        project, "coder", worktree=False, account="2", task_id=task.id
    ).agent
    old = agent.session_id or ""
    _with_transcript(agent, None)
    team_service.claim_task(task.id, session_ref=old)
    original = SqliteStore.end_session
    refusing = True

    class RefusesTheCommit:
        """``sqlite3.Connection`` whose COMMIT is refused, leaving the transaction open."""

        def __init__(self, conn: sqlite3.Connection) -> None:
            self._conn = conn

        def commit(self) -> None:
            raise sqlite3.OperationalError("database is locked (fake)")

        def __getattr__(self, name: str) -> object:
            return getattr(self._conn, name)

    def locked(self: SqliteStore, session_id: str, **kwargs: Any) -> list[TeamTask]:
        if session_id != old or not refusing:
            return original(self, session_id, **kwargs)
        real = self._conn
        self._conn = RefusesTheCommit(real)  # type: ignore[assignment]
        try:
            return original(self, session_id, **kwargs)
        finally:
            self._conn = real

    monkeypatch.setattr(SqliteStore, "end_session", locked)

    receipt = fleet_service.switch(project, agent.label, reason="session limit")

    new = receipt.started.session_id
    assert new is not None and new != old
    assert not any("claims were not moved" in note for note in receipt.notes)
    assert _task_now(task.id).claimed_by == new and _row(receipt.started.id).session_id == new
    marked = f"{team_service.short_id(old)} was not marked ended"  # as the board names it
    assert any(marked in note for note in receipt.notes), receipt.notes
    with store_session() as store:
        kept = store.get_session(old)
    assert kept is not None and kept.ended_at is None, (
        "the switch said the old session was not marked ended, and the claim move's "
        "commit ended it anyway"
    )
    # The note's advice holds too. It said "until `aisquare team prune` retires it",
    # but the old id heartbeat up to the switch and a prune spares it until it has been
    # silent past the board's stale mark, so it names that wait (A2).
    stale = team_service._STALE_AFTER
    note = next(note for note in receipt.notes if marked in note)
    assert f"silent for {int(stale.total_seconds() // 60)} minutes" in note, note
    refusing = False
    assert old not in [p.id for p in team_service.prune_sessions(cwd=project.root).pruned]
    with store_session() as store:
        silent = datetime.now(tz=UTC) - stale - timedelta(minutes=1)
        store.upsert_session(kept.model_copy(update={"last_seen_at": silent}))
    assert old in [p.id for p in team_service.prune_sessions(cwd=project.root).pruned]


def test_a_switch_whose_courtesy_event_cannot_be_written_still_reports_the_move(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``with store_session() as store, contextlib.suppress(Exception)`` enters the store
    FIRST, so a store that could not be opened for the ``switched`` line raised out of a
    switch that had already moved the agent — ``fleet switch`` printed an error and the
    automatic path said "the limited agent was not moved" (review of #205, fourth round)."""
    _two_slots_with_usage(monkeypatch, work=95, personal=10)
    agent = fleet_service.spawn(project, "coder", worktree=False, account="2").agent
    _with_transcript(agent, None)
    real_spawn = fleet_service.spawn
    real_session = store_session
    wedged = False

    def spawn_then_wedge(*args: Any, **kwargs: Any) -> fleet_service.SpawnReceipt:
        nonlocal wedged
        receipt = real_spawn(*args, **kwargs)
        wedged = True  # every store open from here on refuses
        return receipt

    @contextmanager
    def session() -> Iterator[ContextStore]:
        if wedged:
            raise sqlite3.OperationalError("database is locked (fake)")
        with real_session() as store:
            yield store

    monkeypatch.setattr(fleet_service, "spawn", spawn_then_wedge)
    monkeypatch.setattr(fleet_service, "store_session", session)

    receipt = fleet_service.switch(project, agent.label, reason="session limit")

    assert receipt.to_slot == 3 and receipt.started.ended_at is None
    with real_session() as store:
        kinds = [e.kind for e in store.recent_events(project.id, limit=10)]
    assert "switched" not in kinds  # the courtesy is what was lost, and only that


def test_switch_refuses_without_headroom_the_same_account_or_a_dead_label(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    _two_slots_with_usage(monkeypatch, work=95, personal=99)
    agent = fleet_service.spawn(project, "coder", worktree=False, account="2").agent
    _with_transcript(agent, None)
    from aisquare.services import claude_accounts as accounts_service

    accounts_service.set_disabled("3", True)
    with pytest.raises(FleetError, match="no other account with headroom"):
        fleet_service.switch(project, agent.label)
    assert len(tmux.killed) == 0  # nothing was stopped before a target was found
    accounts_service.set_disabled("3", False)

    with pytest.raises(FleetError, match="already runs on"):
        fleet_service.switch(project, agent.label, to="2")
    with pytest.raises(FleetError, match="no Claude account in slot 9"):
        fleet_service.switch(project, agent.label, to="9")
    with pytest.raises(NoSuchAgent):
        fleet_service.switch(project, "nobody-here")
    assert len(tmux.killed) == 0

    # Every account over the line: the one with the most room still moves it (99 → 95? no —
    # the agent is ON 95; the only other one is 99, and that is what it gets, with a note).
    receipt = fleet_service.switch(project, agent.label)
    assert receipt.to_slot == 3
    assert any("every account is over the 85%-used line" in note for note in receipt.notes)


def test_fleet_switch_command_reports_the_move_and_its_json(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    _two_slots_with_usage(monkeypatch, work=95, personal=10)
    agent = fleet_service.spawn(project, "coder", worktree=False, account="2").agent
    _with_transcript(agent, None)

    result = runner.invoke(
        app, ["fleet", "switch", agent.label, "--project", project.id, "--reason", "limit"]
    )
    assert result.exit_code == 0, result.output
    assert (
        f"✓ {agent.label}: moved from account 2 (slot 2) to account 3 (slot 3) — started fresh"
        in result.stdout
    )

    second = runner.invoke(
        app, ["--json", "fleet", "switch", agent.label, "--project", project.id, "--to", "2"]
    )
    assert second.exit_code == 0, second.output
    payload = json.loads(second.stdout)
    assert (payload["from_slot"], payload["to_slot"], payload["resumed"]) == (3, 2, False)
    assert payload["started"]["label"] == agent.label and payload["started"]["account_slot"] == 2
    assert payload["stopped"]["ended_at"] is not None

    # A renamed account is that name in the line, as on the board and in `fleet ls`.
    from aisquare.services import claude_accounts as accounts_service

    accounts_service.set_alias("3", "Side project")
    third = runner.invoke(
        app, ["fleet", "switch", agent.label, "--project", project.id, "--to", "side project"]
    )
    assert third.exit_code == 0, third.output
    assert "moved from account 2 (slot 2) to Side project (slot 3)" in third.stdout

    refused = runner.invoke(app, ["--json", "fleet", "switch", "ghost", "--project", project.id])
    assert refused.exit_code == 1
    assert json.loads(refused.stdout)["error"] in ("no_such_agent", "fleet")


@pytest.mark.parametrize("command", ["restart", "switch"])
def test_restart_and_switch_say_in_their_headline_that_the_first_line_was_not_typed(
    command: str,
    tmux: FakeTmux,
    typed_first_lines: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    """The board's ``restarted`` and ``switched`` lines say whether the replacement's first
    line reached its pane (FLEET-5), but the commands' own headline still said "started
    fresh with a hand-off prompt", above a note saying the prompt was NOT typed (review of
    the side/ff-fleet fold). The headline is the board's words, and ``--json`` carries
    ``prompt_typed``, those words (``how``) and the note on why (``failures``), which the
    phone says them from (sweep 5 of #243)."""
    _two_slots_with_usage(monkeypatch, work=95, personal=10)
    agent = fleet_service.spawn(project, "coder", worktree=False, account="2").agent
    _with_transcript(agent, tmp_path / "missing.jsonl")  # named, not on disk: a fresh start
    # The fake's new pane never comes up, so the hand-off prompt is past its wait.

    result = runner.invoke(app, ["fleet", command, agent.label, "--project", project.id])

    assert result.exit_code == 0, result.output
    [headline] = [line for line in result.stdout.splitlines() if line.startswith("✓ ")]
    assert headline.split(" — ", 1)[1].startswith(
        "started fresh, but its hand-off prompt was NOT typed"
    ), headline
    assert "with a hand-off prompt" not in headline

    again = runner.invoke(app, ["--json", "fleet", command, agent.label, "--project", project.id])
    assert again.exit_code == 0, again.output
    payload = json.loads(again.stdout)
    assert (payload["resumed"], payload["prompt_typed"]) == (False, False)
    assert payload["how"] == "started fresh, but its hand-off prompt was NOT typed"
    (why,) = [note for note in payload["failures"] if "NOT typed" in note]
    assert why in payload["notes"]


@pytest.mark.parametrize(
    ("seen_ago", "printed_ago"),
    [
        # A prompt dismissed with Esc: no Stop follows, and the idle notice after it is quiet.
        (timedelta(minutes=10), timedelta(minutes=9)),
        # The redraw when the UI resizes the window on attach, or an arrow key in the dialog.
        (ACTIVITY_WINDOW + timedelta(seconds=10), ACTIVITY_WINDOW + timedelta(seconds=2)),
    ],
    ids=["dismissed", "redrawn"],
)
def test_one_burst_after_the_notice_is_not_an_answer(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    seen_ago: timedelta,
    printed_ago: timedelta,
) -> None:
    """#153: output after the notification reads as ``working`` only while the pane is
    still producing it. Once it is quiet again the prompt may well still be there, and
    the row must read 🔔 — not ▶ working for as long as it is fresh (``_STALE_AFTER``)."""
    agent = fleet_service.spawn(project, "coder", worktree=False).agent
    _stale_board_session(agent, "attention", seen_ago=seen_ago)  # still fresh, hooks quiet since
    tmux.printed(agent.pane_id, ago=printed_ago)  # after the notice, then nothing
    [quiet] = fleet_service.list_agents(project)
    assert quiet.state == "attention"

    tmux.printed(agent.pane_id)  # control: the same row, the pane moving now
    [moving] = fleet_service.list_agents(project)
    assert moving.state == "working"


def test_the_bell_clears_when_the_pane_prints_after_the_notice(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """#153: a granted permission (or a classifier verdict) shows as output AFTER the
    notification; the row must not stay 🔔 until a human types."""
    agent = fleet_service.spawn(project, "coder", worktree=False).agent
    _board_session(agent, "attention")
    [parked] = fleet_service.list_agents(project)
    assert parked.state == "attention"  # nothing printed since the notice: still needs someone

    tmux.printed(agent.pane_id, ago=timedelta(seconds=30))  # output BEFORE the notice
    [still] = fleet_service.list_agents(project)
    assert still.state == "attention"  # that was the prompt being drawn, not an answer

    # Output AFTER the notice. tmux reports `window_activity` in whole seconds and the
    # hook stamped `last_seen_at` with microseconds, so "after" means a LATER second —
    # exactly the production rule, which keeps the prompt being drawn (same second as
    # the hook) from reading as an answer. Two seconds on, then.
    tmux.printed(agent.pane_id, ago=timedelta(seconds=-2))
    [moved_on] = fleet_service.list_agents(project)
    assert moved_on.state == "working"


# --- window geometry (#149) ------------------------------------------------------------------


def test_spawn_passes_the_callers_size_and_otherwise_the_default_under_the_diff_panel_line(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """A window is born the size of the pane that will show it; headless, the safe default."""
    fleet_service.spawn(project, "coder", worktree=False, size=(97, 31))
    assert tmux.spawned[-1]["size"] == (97, 31)

    fleet_service.spawn(project, "tester", worktree=False)
    width, height = tmux.spawned[-1]["size"]
    assert (width, height) == (tmux_core.DEFAULT_WINDOW_WIDTH, tmux_core.DEFAULT_WINDOW_HEIGHT)
    # The numbers that matter, pinned where the default is consumed: under the 144
    # columns at which Claude Code's diff panel opens by itself, and not below the
    # 110 it needs to open the panel on demand — so `/diff` still works.
    assert 110 <= width < 144
    assert height >= 24


def test_a_window_tmux_would_not_size_still_runs_and_the_receipt_says_why(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """A refused resize after ``new-window`` costs geometry, never the agent — and is said.

    Headless, nothing corrects it: the coder runs at its session's size, which for
    a manager started from a wide pane is past 144 columns, the #149 failure. A
    receipt with nothing in ``notes`` left the diff panel as the only clue (review
    of #162, round 1).
    """
    fleet_service.spawn(project, "manager", worktree=False)
    tmux.refuse_resize = "no server running on /tmp/tmux-1000/asq"
    receipt = fleet_service.spawn(project, "coder", worktree=False)

    [recorded] = [a for a in fleet_service.list_agents(project) if a.agent.role == "coder"]
    assert recorded.agent.pane_id == receipt.agent.pane_id  # the row stands
    size = f"{tmux_core.DEFAULT_WINDOW_WIDTH}x{tmux_core.DEFAULT_WINDOW_HEIGHT}"
    [note] = [n for n in receipt.notes if "would not size" in n]
    assert size in note and "no server running on /tmp/tmux-1000/asq" in note
    assert "144 columns" in note

    tmux.refuse_resize = None
    quiet = fleet_service.spawn(project, "tester", worktree=False)
    assert not [n for n in quiet.notes if "would not size" in n]


# --- #138: a dead pane is recorded on every read; restart brings the agent back ----------


def test_a_listing_records_a_dead_pane_as_ended_and_keeps_the_row_while_its_window_lingers(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """#138. ctrl+c inside the manager's window left a row the store called live for a
    pane tmux called dead (``remain-on-exit`` keeps the window): 💤 on the sidebar
    forever, and ``fleet spawn manager`` refused. Every listing now records the
    exit the way ``reap`` does — status, board event, one nudge — and keeps the
    row on the live listing only while tmux still shows its window."""
    manager = fleet_service.spawn(project, "manager").agent
    _board_session(manager, "waiting")
    tmux.set_command(manager.pane_id, "claude")
    coder = _coder(project)
    tmux.die(coder.pane_id, 130)  # what ctrl+c leaves behind
    with store_session() as store:
        assert store.fleet_agent_by_label(project.id, "coder-1", live_only=True) is not None

    listed = {s.agent.label: s for s in fleet_service.list_agents(project)}

    seen = listed["coder-1"]
    assert seen.state == "exited" and seen.detail == "exit 130"
    assert seen.agent.ended_at is not None and seen.agent.exit_status == 130  # the ROW, too
    with store_session() as store:
        row = store.get_fleet_agent(coder.id)
    assert row is not None and row.ended_at is not None and row.exit_status == 130
    assert _events(project, "agent_exited") == ["coder-1 exited (130)"]
    assert tmux.typed == [
        (manager.pane_id, "paste", NUDGE_TEXT),
        (manager.pane_id, "key", "Enter"),
    ]
    # Idempotent: the next listing writes nothing and says nothing more.
    assert {s.agent.label: s.state for s in fleet_service.list_agents(project)} == {
        "manager": "waiting",
        "coder-1": "exited",
    }
    assert _events(project, "agent_exited") == ["coder-1 exited (130)"] and len(tmux.typed) == 2
    assert fleet_service.reap(project).ended == []  # nothing left for reap to record
    # The 💤 row leaves the live listing with its window — and stays in --all.
    tmux.vanish(coder.pane_id)
    assert [s.agent.label for s in fleet_service.list_agents(project)] == ["manager"]
    everything = fleet_service.list_agents(project, live_only=False)
    assert [(s.agent.label, s.state) for s in everything] == [
        ("manager", "waiting"),
        ("coder-1", "exited"),
    ]


def test_a_vanished_pane_is_not_recorded_by_a_listing_and_an_old_exit_leaves_the_live_list(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """The two negative controls of the rule above. Absence is not evidence (an
    empty answer from a server that did not speak looks the same), so a
    vanished pane stays a LIVE row reading ``lost`` until ``reap``; and an
    exit older than ``RECENTLY_ENDED`` is history even while its window lingers."""
    gone = _coder(project, label="coder-gone")
    tmux.vanish(gone.pane_id)
    [status] = fleet_service.list_agents(project)
    assert status.state == "lost" and status.agent.ended_at is None
    with store_session() as store:
        row = store.get_fleet_agent(gone.id)
    assert row is not None and row.ended_at is None, "absence is reap's call, not the listing's"
    assert _events(project, "agent_exited") == []
    fleet_service.reap(project)

    old = _coder(project, label="coder-old")
    tmux.die(old.pane_id, 1)
    [exited] = fleet_service.list_agents(project)
    assert exited.state == "exited"
    long_ago = datetime.now(tz=UTC) - fleet_service.RECENTLY_ENDED - timedelta(minutes=1)
    with store_session() as store:
        store.upsert_fleet_agent(exited.agent.model_copy(update={"ended_at": long_ago}))
    assert fleet_service.list_agents(project) == []  # the window is still there: too old
    assert old.pane_id in tmux.facts
    assert [s.agent.label for s in fleet_service.list_agents(project, live_only=False)] == [
        "coder-gone",
        "coder-old",
    ]


def test_spawn_manager_replaces_a_dead_manager_without_a_reap(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """The trap the issue reports: ``fleet spawn manager`` answered "already has a
    manager" for a manager everyone could see was dead, until a hand-run ``reap``."""
    first = fleet_service.spawn(project, "manager").agent
    tmux.die(first.pane_id, 130)
    # No listing and no reap in between: the store still says live.
    with store_session() as store:
        assert store.fleet_agent_by_label(project.id, "manager", live_only=True) is not None

    receipt = fleet_service.spawn(project, "manager")

    assert receipt.agent.id != first.id and receipt.agent.label == "manager"
    assert first.pane_id in tmux.killed, "the dead window goes with the replacement"
    with store_session() as store:
        old = store.get_fleet_agent(first.id)
    assert old is not None and old.ended_at is not None and old.exit_status == 130
    assert _events(project, "agent_exited") == ["manager exited (130)"]
    assert [s.agent.id for s in fleet_service.list_agents(project)] == [receipt.agent.id]
    # A LIVE manager is refused as before — the check did not get weaker.
    with pytest.raises(FleetError, match="already has a manager"):
        fleet_service.spawn(project, "manager")

    # A dead coder: the same label takes over its window; another label leaves the
    # 💤 row (and its readable last screen) alone and only records the exit.
    dead = _coder(project, label="coder-auth")
    other = _coder(project, label="coder-db")
    tmux.die(dead.pane_id, 1)
    tmux.die(other.pane_id, 2)
    replacement = fleet_service.spawn(project, "coder", label="coder-auth", worktree=False)
    assert replacement.agent.label == "coder-auth" and dead.pane_id in tmux.killed
    assert other.pane_id in tmux.facts and other.pane_id not in tmux.killed
    with store_session() as store:
        recorded = store.get_fleet_agent(other.id)
    assert recorded is not None and recorded.exit_status == 2
    assert sorted(_events(project, "agent_exited")) == sorted(
        ["manager exited (130)", "coder-auth exited (1)", "coder-db exited (2)"]
    )

    # The row was ended by a LISTING first (the UI's tick) and its window lingers:
    # a spawn under that label still removes the window it supersedes — measured
    # live before this rule: `fleet ls` showed two rows called manager.
    tmux.die(receipt.agent.pane_id, 130)
    [seen, *_rest] = [s for s in fleet_service.list_agents(project) if s.agent.label == "manager"]
    assert seen.state == "exited" and receipt.agent.pane_id not in tmux.killed
    third = fleet_service.spawn(project, "manager")
    assert receipt.agent.pane_id in tmux.killed
    managers = [s for s in fleet_service.list_agents(project) if s.agent.label == "manager"]
    assert [s.agent.id for s in managers] == [third.agent.id]
    assert other.pane_id in tmux.facts, "another label's 💤 row keeps its last screen"


def test_restart_brings_an_exited_agent_back_under_its_label_and_resumes_its_session(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The way back the issue asks for: the dead manager's row ends and ``manager``
    is spawned again — on the same account, and RESUMING the same session when
    its transcript is on disk, so it comes back knowing its intake and coders."""
    _two_slots_with_usage(monkeypatch, work=10, personal=10)
    agent = fleet_service.spawn(project, "manager", account="2").agent
    transcript = tmp_path / f"{agent.session_id}.jsonl"
    transcript.write_text('{"type":"user"}\n', encoding="utf-8")
    _with_transcript(agent, transcript)
    tmux.die(agent.pane_id, 130)
    [seen] = fleet_service.list_agents(project)  # the UI's tick: the row is 💤 exited now
    assert seen.state == "exited"

    receipt = fleet_service.restart(project, "manager")

    assert receipt.resumed is True and receipt.was_running is False
    assert receipt.replaced.id == agent.id and receipt.replaced.exit_status == 130
    assert receipt.started.label == "manager" and receipt.started.role == "manager"
    assert receipt.started.session_id == agent.session_id  # the same session carries on
    assert receipt.started.account_slot == 2  # the account it ran on, kept
    command = _command(tmux)
    assert _flag(command, "--resume") == str(transcript) and "--session-id" not in command
    assert _flag(command, "--account") == "2"
    assert agent.pane_id in tmux.killed, "the dead window leaves with the restart"
    assert [s.agent.id for s in fleet_service.list_agents(project)] == [receipt.started.id]
    manager = fleet_service.manager_of(project)
    assert manager is not None and manager.id == receipt.started.id
    assert _events(project, "restarted") == ["manager restarted — resumed its session"]
    assert receipt.tmux_session == f"asq-{_codename(project)}"

    # A LOST agent (pane vanished, row live) restarts the same way; nothing to kill.
    tmux.vanish(receipt.started.pane_id)
    again = fleet_service.restart(project, "manager")
    assert again.resumed is True and again.was_running is False
    assert again.replaced.id == receipt.started.id and again.replaced.exit_status is None


def test_restart_stops_a_running_agent_first_and_starts_fresh_when_asked_or_without_a_transcript(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    task = _add_task(project, "Ship auth")
    agent = _coder(project, task_id=task.id)
    _with_transcript(agent, tmp_path / "missing.jsonl")  # named, not on disk
    with store_session() as store:
        team_service._emit(
            store, project.id, "note", "halfway through the router", session_id=agent.session_id
        )
    # The new pane must look ready, or a multi-line prompt is (correctly) not typed.
    original_spawn = tmux.spawn_window

    def ready_spawn(*args: Any, **kwargs: Any) -> WindowInfo:
        window = original_spawn(*args, **kwargs)
        tmux.set_command(window.pane_id, "claude")
        return window

    monkeypatch.setattr(tmux, "spawn_window", ready_spawn)

    receipt = fleet_service.restart(project, agent.label)

    assert receipt.was_running is True and receipt.resumed is False
    assert tmux.typed[:2] == [(agent.pane_id, "literal", "/exit"), (agent.pane_id, "key", "Enter")]
    assert receipt.replaced.ended_at is not None and receipt.replaced.exit_status == 0
    assert any("no transcript on disk" in note for note in receipt.notes)
    assert receipt.started.task_id == task.id and receipt.started.label == agent.label
    assert receipt.started.session_id != agent.session_id  # fresh: a new session
    assert "--resume" not in _command(tmux)
    [prompt] = _first_lines(tmux, receipt.started.pane_id)
    assert prompt.startswith(
        f"You are {agent.label}, taking over from a previous session of this agent (restarted)."
    )
    assert "Ship auth" in prompt and "- note: halfway through the router" in prompt
    assert _events(project, "restarted") == [
        f"{agent.label} restarted — started fresh with a hand-off prompt"
    ]

    # --fresh on an agent WITH a transcript still starts over, by choice.
    transcript = tmp_path / "present.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    _with_transcript(receipt.started, transcript)
    again = fleet_service.restart(project, agent.label, fresh=True)
    assert again.resumed is False and "--resume" not in _command(tmux)
    assert not any("no transcript" in note for note in again.notes)  # asked for, not forced

    with pytest.raises(NoSuchAgent, match="no agent 'nobody-here'"):
        fleet_service.restart(project, "nobody-here")


def test_stop_on_an_exited_row_removes_the_lingering_window_and_the_row_leaves_the_listing(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    coder = _coder(project)
    tmux.die(coder.pane_id, 1)
    [seen] = fleet_service.list_agents(project)
    assert seen.state == "exited"  # ended by the listing; the window is still there

    receipt = fleet_service.stop(project, coder.label)

    ended = receipt.agent
    assert ended.id == coder.id and ended.exit_status == 1
    assert receipt.released == []  # its claims went with the end the listing recorded
    assert tmux.killed == [coder.pane_id] and tmux.typed == []  # nothing to /exit
    assert fleet_service.list_agents(project) == []
    # Without a window there is nothing to stop: the old answer stands.
    with pytest.raises(NoSuchAgent):
        fleet_service.stop(project, coder.label)


def test_fleet_restart_command_reports_the_restart_and_its_json(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, runner: CliRunner, tmp_path: Path
) -> None:
    agent = fleet_service.spawn(project, "manager").agent
    transcript = tmp_path / "manager.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    _with_transcript(agent, transcript)
    tmux.die(agent.pane_id, 130)

    result = runner.invoke(app, ["fleet", "restart", "manager", "--project", project.id])
    assert result.exit_code == 0, result.output
    assert "✓ manager: restarted — resumed its session" in result.stdout
    assert "stopped and" not in result.stdout  # it was not running

    second = runner.invoke(
        app, ["--json", "fleet", "restart", "manager", "--project", project.id, "--fresh"]
    )
    assert second.exit_code == 0, second.output
    payload = json.loads(second.stdout)
    assert payload["resumed"] is False and payload["was_running"] is True
    assert payload["started"]["label"] == "manager" and payload["replaced"]["ended_at"] is not None
    assert payload["replaced"]["id"] != payload["started"]["id"]
    assert isinstance(payload["notes"], list) and payload["tmux_session"].startswith("asq-")

    refused = runner.invoke(app, ["--json", "fleet", "restart", "ghost", "--project", project.id])
    assert refused.exit_code == 1
    assert json.loads(refused.stdout)["error"] == "no_such_agent"


def test_a_reused_pane_id_never_makes_an_ended_row_present_nor_kills_a_live_window(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """Pane ids are unique per SERVER lifetime: the fleet's server exits with its last
    window and the next numbers from %0 again (measured live while building #138:
    three ended rows and the running manager all at %0). An ended row's window is
    a DEAD pane under its id; a live pane under that id is another agent's — never
    listed as the row's, never killed for it."""
    old = _coder(project, label="coder-old")
    fleet_service.stop(project, "coder-old")  # its window is gone with it
    tmux._counter = 0  # the server restarted: numbering begins again
    new = _coder(project, label="coder-new")
    assert new.pane_id == old.pane_id  # the shape under test

    listed = fleet_service.list_agents(project)
    assert [(s.agent.id, s.state) for s in listed] == [(new.id, "waiting")]
    with pytest.raises(NoSuchAgent):
        fleet_service.stop(project, "coder-old")  # nothing of coder-old's is there to stop…
    assert new.pane_id in tmux.facts and tmux.killed == [old.pane_id]  # …coder-new keeps its window
    restarted = fleet_service.restart(project, "coder-old")
    assert restarted.replaced.id == old.id and restarted.started.label == "coder-old"
    assert new.pane_id in tmux.facts and tmux.killed == [old.pane_id]
    # A spawn under the old label after a listing: the same rule, from the supersede path.
    fleet_service.stop(project, "coder-old", force=True)
    tmux._counter = 0
    third = _coder(project, label="coder-third")
    assert third.pane_id == old.pane_id
    fleet_service.spawn(project, "coder", label="coder-old", worktree=False)
    assert third.pane_id in tmux.facts
    assert [s.agent.label for s in fleet_service.list_agents(project)] == [
        "coder-new",
        "coder-third",
        "coder-old",
    ]


# --- the review of #138 --------------------------------------------------------------------------


def test_a_refused_restart_leaves_the_exited_row_its_window_and_a_running_agent_alone(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``restart`` killed the 💤 row's dead window — or stopped a running agent — BEFORE
    ``spawn`` ran its refusals, so a refused restart took the row and its last screen
    and put nothing in their place; the commonest 💤 row is a coder whose task is
    done. The task is now checked before anything is touched, and the dead window
    goes only once the replacement is up and recorded (``_supersede``)."""
    task = _add_task(project, "Ship auth")
    coder = _coder(project, task_id=task.id)
    with store_session() as store:
        store.set_task_status(task.id, "done")
    tmux.die(coder.pane_id, 0)
    [seen] = fleet_service.list_agents(project)
    assert seen.state == "exited"

    with pytest.raises(FleetError, match=f"cannot restart '{coder.label}': task .* is done"):
        fleet_service.restart(project, coder.label)
    assert tmux.killed == [] and len(tmux.spawned) == 1
    listed = fleet_service.list_agents(project)
    assert [(s.agent.id, s.state) for s in listed] == [(coder.id, "exited")]

    # A running agent is refused the same way BEFORE it is stopped: nothing typed.
    billing = _add_task(project, "Ship billing")
    running = _coder(project, label="coder-billing", task_id=billing.id)
    with store_session() as store:
        store.set_task_status(billing.id, "dropped")
    with pytest.raises(FleetError, match="is dropped"):
        fleet_service.restart(project, running.label)
    assert tmux.typed == [] and tmux.killed == []
    with store_session() as store:
        assert store.fleet_agent_by_label(project.id, running.label) is not None  # still live

    # A refusal past every check — tmux will not start the window — keeps the 💤 row too.
    plain = _coder(project, label="coder-plain")
    tmux.die(plain.pane_id, 1)
    fleet_service.list_agents(project)

    def no_window(*args: Any, **kwargs: Any) -> WindowInfo:
        raise TmuxError("server exited unexpectedly")

    monkeypatch.setattr(tmux, "spawn_window", no_window)
    with pytest.raises(FleetError, match="tmux could not start the window"):
        fleet_service.restart(project, "coder-plain")
    assert tmux.killed == [] and plain.pane_id in tmux.facts
    states = {s.agent.label: s.state for s in fleet_service.list_agents(project)}
    assert states["coder-plain"] == "exited" and states[coder.label] == "exited"


def test_a_reused_pane_id_never_lets_stop_or_spawn_take_another_agents_last_screen(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """``stop`` on an ended row asked tmux about the id ALONE and killed any dead pane
    under it, and spawn's supersede walked every row the label ever had. Once the
    server restarted and a new agent died at the old id, both took the NEW agent's
    last screen for the old row's — and ``stop`` reported success. Both now go by
    the listing's rule (``_lingering_windows``): a dead pane is the window of a live
    row naming it, else of the LATEST row to end under it."""
    old = _coder(project, label="coder-old")
    fleet_service.stop(project, "coder-old")
    tmux._counter = 0  # the server restarted: numbering begins again
    new = _coder(project, label="coder-new")
    assert new.pane_id == old.pane_id  # the shape under test
    tmux.die(new.pane_id, 1)

    # Before any listing — coder-new's row is still live — and after it.
    with pytest.raises(NoSuchAgent):
        fleet_service.stop(project, "coder-old")
    listed = fleet_service.list_agents(project)
    assert [(s.agent.id, s.state) for s in listed] == [(new.id, "exited")]
    with pytest.raises(NoSuchAgent):
        fleet_service.stop(project, "coder-old")
    assert tmux.killed == [old.pane_id]  # the first stop's, nothing since

    replacement = fleet_service.spawn(project, "coder", label="coder-old", worktree=False)
    assert replacement.agent.label == "coder-old"
    assert new.pane_id in tmux.facts and tmux.killed == [old.pane_id]
    states = {s.agent.label: s.state for s in fleet_service.list_agents(project)}
    assert states == {"coder-new": "exited", "coder-old": "waiting"}

    # Its OWN window is still its own to remove.
    assert fleet_service.stop(project, "coder-new").agent.id == new.id
    assert tmux.killed == [old.pane_id, new.pane_id]


def test_restart_keeps_the_account_a_row_without_a_slot_ran_under(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A row with no ``account_slot`` (spawned before the column) restarted on the
    resolver's pick, and then resumed a transcript written under another config dir
    — the leg ``switch`` calls unverified. It reads the session's account, as
    ``switch`` does (``_account_slot_of``)."""
    from aisquare.core import claude_accounts as accounts_core

    _two_slots_with_usage(monkeypatch, work=10, personal=90)  # the pick would be slot 2
    agent = fleet_service.spawn(project, "manager", account="3").agent
    assert agent.session_id is not None
    transcript = tmp_path / "manager.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    now = datetime.now(tz=UTC)
    with store_session() as store:
        store.upsert_fleet_agent(agent.model_copy(update={"account_slot": None}))
        store.upsert_session(
            TeamSession(
                id=agent.session_id,
                project_id=project.id,
                role="manager",
                started_at=now,
                last_seen_at=now,
                transcript_path=str(transcript),
                account=str(accounts_core.accounts_root() / "3"),
            )
        )
    tmux.die(agent.pane_id, 130)

    receipt = fleet_service.restart(project, "manager")

    assert receipt.resumed is True
    assert _flag(_command(tmux), "--account") == "3" and receipt.started.account_slot == 3


def test_restart_replays_the_rows_persona_and_steps_down_when_it_no_longer_resolves(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restart is the same agent (#144), and the row is the record of how it ran
    (spawn-personas §3.8): the persona it was spawned with comes back with it; a row
    that had none replays with none, even once the role has adopted a default. A
    persona deleted since steps DOWN, and says so: to the role's current default when
    there is one that resolves, else to none — never a refusal, which would leave the
    agent stopped over a prompt it can run without (owner decision, 2026-09-24)."""
    from aisquare.core.config import FleetRoleSettings

    skill = project.root / ".aisquare" / "personas" / "tempo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: tempo\ndescription: A steady tempo, for this test.\n---\n"
        "Work at a steady tempo.\n",
        encoding="utf-8",
    )
    kept = fleet_service.spawn(project, "coder", label="coder-kept", persona="careful").agent
    plain = fleet_service.spawn(project, "coder", label="coder-plain").agent
    gone = fleet_service.spawn(project, "coder", label="coder-gone", persona="tempo").agent
    lone = fleet_service.spawn(project, "tester", label="tester-gone", persona="tempo").agent
    # A default adopted since the spawns, for coders only: a replay must not pick it
    # up over a persona the row has, nor over a row that ran with none.
    _settings(monkeypatch, roles={"coder": FleetRoleSettings(persona="minimalist")})
    shutil.rmtree(skill.parent)
    for agent in (kept, plain, gone, lone):
        tmux.die(agent.pane_id, 0)

    kept_again = fleet_service.restart(project, "coder-kept")
    plain_again = fleet_service.restart(project, "coder-plain")
    gone_again = fleet_service.restart(project, "coder-gone")
    lone_again = fleet_service.restart(project, "tester-gone")

    assert kept_again.started.persona == "careful"
    assert _flag(_command(tmux, -4), "--persona") == "careful"
    assert plain_again.started.persona is None and "--persona" not in _command(tmux, -3)
    # Gone, and the role has a default that resolves: the default, said.
    assert gone_again.started.persona == "minimalist"
    assert _flag(_command(tmux, -2), "--persona") == "minimalist"
    assert [n for n in gone_again.notes if "persona tempo no longer resolves" in n]
    assert [n for n in gone_again.notes if "started with the role's default, minimalist" in n]
    # Gone, and no default to step down to: none, said.
    assert lone_again.started.persona is None and "--persona" not in _command(tmux, -1)
    assert [n for n in lone_again.notes if "persona tempo no longer resolves" in n]
    assert [n for n in lone_again.notes if "started without a persona" in n]


def test_a_death_two_readers_see_is_announced_once(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """Every read records a dead pane now — the UI's tick, the manager's ``fleet ls``, a
    spawn — so two readers routinely hold the same live snapshot. The second to write
    found ``end_fleet_agent`` a no-op and still posted ``agent_exited`` and nudged."""
    coder = _coder(project)
    tmux.die(coder.pane_id, 1)
    views = fleet_service._observe_sockets([coder], f"asq-{_codename(project)}")

    first = fleet_service._end_dead_rows([coder], views)
    second = fleet_service._end_dead_rows([coder], views)  # the same, now stale, snapshot

    assert [row.id for row in first] == [row.id for row in second] == [coder.id]
    assert second[0].ended_at == first[0].ended_at and second[0].exit_status == 1
    assert _events(project, "agent_exited") == ["coder-1 exited (1)"]


def test_a_listing_still_lists_when_the_store_will_not_take_the_exit(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    """The reconcile made ``list_agents`` — ``fleet ls``, ``fleet status``, the UI's tick —
    write, and a store locked past its busy timeout raised ``OperationalError`` out of
    a READ (a traceback instead of the list). The write is best effort: the row waits
    for the next read and reads ``exited`` from its pane meanwhile."""
    coder = _coder(project)
    tmux.die(coder.pane_id, 3)

    def locked(
        self: SqliteStore, agent_id: str, *, exit_status: int | None = None
    ) -> FleetAgent | None:
        raise sqlite3.OperationalError("database is locked")

    with monkeypatch.context() as patched:
        patched.setattr(SqliteStore, "end_fleet_agent_if_live", locked)
        [status] = fleet_service.list_agents(project)
        assert status.state == "exited" and status.detail == "exit 3"
        result = runner.invoke(app, ["fleet", "ls", "--project", project.id])
        assert result.exit_code == 0, result.output
        assert "coder-1" in result.stdout
    with store_session() as store:
        row = store.get_fleet_agent(coder.id)
    assert row is not None and row.ended_at is None  # left for the next read
    assert _events(project, "agent_exited") == []

    fleet_service.list_agents(project)  # the next read records it
    assert _events(project, "agent_exited") == ["coder-1 exited (3)"]


def test_a_fresh_switch_still_tells_the_replacement_it_moved_to_another_account(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hand-off prompt became shared with ``restart`` and lost the one fact a fresh
    switched agent cannot read off the board: that it now runs under another account."""
    _two_slots_with_usage(monkeypatch, work=95, personal=10)
    agent = fleet_service.spawn(project, "coder", worktree=False, account="2").agent
    _with_transcript(agent, None)  # nothing to resume: a hand-off prompt
    original_spawn = tmux.spawn_window

    def ready_spawn(*args: Any, **kwargs: Any) -> WindowInfo:
        window = original_spawn(*args, **kwargs)
        tmux.set_command(window.pane_id, "claude")
        return window

    monkeypatch.setattr(tmux, "spawn_window", ready_spawn)

    fleet_service.switch(project, agent.label, reason="5h limit")

    [prompt] = _first_lines(tmux)
    assert prompt.startswith(
        f"You are {agent.label}, taking over from a previous session of this agent "
        "(it stopped on 5h limit under another Claude account)."
    )


# --- the review of #138, round 2 -----------------------------------------------------------------


def test_a_pinned_row_is_never_mistaken_for_the_agent_that_took_its_label(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """The agent view outlives its row (``_feed_open_views`` keeps a view whose row left
    the frame), and Stop is offered on the 💤 row. By label, a Stop pressed on a 💤 view
    after the manager had restarted that coder — woken by the very ``agent_exited`` the
    operator was reading — sent ``/exit`` to the NEW coder, killed it and said "stopped",
    and Restart stopped and restarted it. The view pins its row (``agent_id``): the
    replacement is refused, never touched; the row that IS there is acted on as before."""
    old = _coder(project, label="coder-1")
    tmux.die(old.pane_id, 1)
    [seen] = fleet_service.list_agents(project)  # the operator opens the 💤 row
    assert seen.state == "exited"
    new = fleet_service.restart(project, "coder-1").started  # the manager restarts it
    assert old.pane_id in tmux.killed  # superseded: the 💤 row has left the listing
    typed, killed, spawned = list(tmux.typed), list(tmux.killed), len(tmux.spawned)

    with pytest.raises(NoSuchAgent, match=rf"'coder-1' is another agent now \({new.id}\)"):
        fleet_service.stop(project, "coder-1", agent_id=old.id)
    with pytest.raises(NoSuchAgent, match=rf"'coder-1' is another agent now \({new.id}\)"):
        fleet_service.restart(project, "coder-1", agent_id=old.id)
    assert (tmux.typed, tmux.killed, len(tmux.spawned)) == (typed, killed, spawned)
    assert new.pane_id in tmux.facts and not tmux.facts[new.pane_id].dead
    with store_session() as store:
        live = store.fleet_agent_by_label(project.id, "coder-1")
    assert live is not None and live.id == new.id

    # Pinned to the row that is there, each acts as it always did: another 💤 row's
    # own window goes, and the live coder-1 is stopped.
    other = _coder(project, label="coder-2")
    tmux.die(other.pane_id, 0)
    fleet_service.list_agents(project)
    assert fleet_service.stop(project, "coder-2", agent_id=other.id).agent.id == other.id
    assert other.pane_id in tmux.killed
    assert fleet_service.stop(project, "coder-1", agent_id=new.id).agent.id == new.id
    assert (new.pane_id, "literal", "/exit") in tmux.typed


def test_a_pinned_switch_or_tell_never_reaches_the_agent_that_took_its_label(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """The phone's Switch and Tell, as a sweep of #243 found them (round 4). The manager's
    own ``fleet switch``, in another process, handed coder-1 on just after the phone had
    checked its pin. By label, the phone's switch then stopped the replacement and moved
    it to a third account, and its tell typed into it. Pinned (``agent_id``), as stop
    and restart were, both refuse the replacement before anything is looked up, typed or
    filed. Pinned to the row that is there, each goes on as before."""
    old = _coder(project, label="coder-1")
    tmux.die(old.pane_id, 1)
    fleet_service.list_agents(project)
    new = fleet_service.restart(project, "coder-1").started
    _board_session(new, "waiting")
    tmux.set_command(new.pane_id, "claude")
    typed, killed, spawned = list(tmux.typed), list(tmux.killed), len(tmux.spawned)

    replaced = rf"'coder-1' is another agent now \({new.id}\)"
    with pytest.raises(NoSuchAgent, match=replaced):
        fleet_service.switch(project, "coder-1", to="2", agent_id=old.id)
    with pytest.raises(NoSuchAgent, match=replaced):
        fleet_service.tell(project, "coder-1", "carry on", agent_id=old.id)
    assert (tmux.typed, tmux.killed, len(tmux.spawned)) == (typed, killed, spawned)
    assert _events(project, "note") == []

    with pytest.raises(FleetError, match="no Claude account in slot 2"):
        fleet_service.switch(project, "coder-1", to="2", agent_id=new.id)
    assert fleet_service.tell(project, "coder-1", "carry on", agent_id=new.id).delivered
    assert tmux.typed[len(typed) :] == [
        (new.pane_id, "paste", "carry on"),
        (new.pane_id, "key", "Enter"),
    ]


def test_a_switchs_last_check_comes_after_its_own_refusals_and_before_anything_is_stopped(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``before_stop`` is the caller's last word: the phone's dialog guard, whose Escape
    answers a prompt "No". Asked before the switch's own refusals, that Escape went to an
    agent the switch then would not move, for a ``to`` that named no account (sweep of
    #243, round 4). It is asked after them, once, before the session is marked or
    ``/exit`` typed, and a check that refuses leaves the agent as it was."""
    _two_slots_with_usage(monkeypatch, work=95, personal=10)
    agent = fleet_service.spawn(project, "coder", worktree=False, account="2").agent
    _with_transcript(agent, None)
    before = (len(tmux.typed), len(tmux.killed))
    asked: list[tuple[int, int]] = []

    def check() -> None:
        asked.append((len(tmux.typed), len(tmux.killed)))

    with pytest.raises(FleetError, match="no Claude account in slot 9"):
        fleet_service.switch(project, agent.label, to="9", before_stop=check)
    with pytest.raises(FleetError, match="already runs on"):
        fleet_service.switch(project, agent.label, to="2", before_stop=check)
    assert asked == [], "the switch's own refusals come first"

    def refuse() -> None:
        check()
        raise RuntimeError("the dialog guard said no")

    with pytest.raises(RuntimeError, match="the dialog guard said no"):
        fleet_service.switch(project, agent.label, before_stop=refuse)
    assert asked == [before] and (len(tmux.typed), len(tmux.killed)) == before
    assert _session_state(agent.session_id or "") != team_service.HANDOVER_STATE

    receipt = fleet_service.switch(project, agent.label, before_stop=check)
    assert asked == [before, before], "asked once, before /exit was typed"
    assert receipt.to_slot == 3 and agent.pane_id in tmux.killed


def test_a_restarts_last_check_comes_after_its_own_refusals_and_before_anything_is_stopped(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """As a switch's (sweep of #243, round 4): ``before_stop`` is the phone's dialog guard,
    whose Escape answers a prompt "No". It is asked after the restart's own refusals, a
    task closed meanwhile here, and before ``/exit`` is typed. A check that refuses leaves
    the running agent as it was, and one that passes is asked once."""
    task = _add_task(project, "Ship auth")
    closed = _coder(project, label="coder-closed", task_id=task.id)
    with store_session() as store:
        store.set_task_status(task.id, "done")
    agent = _coder(project)
    before = (len(tmux.typed), len(tmux.killed))
    asked: list[tuple[int, int]] = []

    def check() -> None:
        asked.append((len(tmux.typed), len(tmux.killed)))

    with pytest.raises(FleetError, match="is done"):
        fleet_service.restart(project, closed.label, before_stop=check)
    assert asked == [], "the restart's own refusals come first"

    def refuse() -> None:
        check()
        raise RuntimeError("the dialog guard said no")

    with pytest.raises(RuntimeError, match="the dialog guard said no"):
        fleet_service.restart(project, agent.label, before_stop=refuse)
    assert asked == [before] and (len(tmux.typed), len(tmux.killed)) == before
    with store_session() as store:
        assert store.fleet_agent_by_label(project.id, agent.label) is not None  # still live

    receipt = fleet_service.restart(project, agent.label, before_stop=check)
    assert asked == [before, before], "asked once, before /exit was typed"
    assert receipt.was_running and agent.pane_id in tmux.killed


def test_a_refused_restart_of_a_death_no_listing_has_recorded_keeps_the_window(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row still live whose pane had died — ``fleet restart`` run right after the death,
    with no listing between — went through ``stop``, which killed the dead window before
    ``spawn`` ran its refusals: a refused restart left neither the 💤 row nor its last
    screen. The death is recorded as a listing records it (announced once) and the
    window is left for ``_supersede``, once the replacement is up."""
    plain = _coder(project, label="coder-plain")
    tmux.die(plain.pane_id, 1)

    def no_window(*args: Any, **kwargs: Any) -> WindowInfo:
        raise TmuxError("server exited unexpectedly")

    with monkeypatch.context() as patched:
        patched.setattr(tmux, "spawn_window", no_window)
        with pytest.raises(FleetError, match="tmux could not start the window"):
            fleet_service.restart(project, "coder-plain")
    assert tmux.killed == [] and plain.pane_id in tmux.facts
    [status] = fleet_service.list_agents(project)
    assert (status.agent.id, status.state, status.agent.exit_status) == (plain.id, "exited", 1)
    assert _events(project, "agent_exited") == ["coder-plain exited (1)"]

    receipt = fleet_service.restart(project, "coder-plain")
    assert receipt.replaced.id == plain.id and receipt.was_running is False
    assert tmux.killed == [plain.pane_id]  # once the replacement was up
    assert [s.agent.id for s in fleet_service.list_agents(project)] == [receipt.started.id]


def test_a_running_agent_is_not_stopped_for_a_restart_its_account_or_binary_would_refuse(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``restart`` checked the task before stopping a running agent, and nothing else:
    an account no longer on this machine — reachable for a slotless row through its
    session's config dir, whose slot number outlives the slot — or a binary gone from
    PATH were refused inside ``spawn``, AFTER the stop, leaving no agent at all. Both
    are asked first now, as ``switch`` resolves its account before its stop, and so is
    the role, which ``spawn`` refuses before either."""
    from aisquare.core import claude_accounts as accounts_core

    agent = _coder(project)
    now = datetime.now(tz=UTC)
    with store_session() as store:
        store.upsert_fleet_agent(agent.model_copy(update={"account_slot": None}))
        store.upsert_session(
            TeamSession(
                id=agent.session_id or "",
                project_id=project.id,
                role="coder",
                started_at=now,
                last_seen_at=now,
                account=str(accounts_core.accounts_root() / "4"),  # a slot since removed
            )
        )

    with pytest.raises(FleetError, match="cannot restart 'coder-1': no Claude account in slot 4"):
        fleet_service.restart(project, agent.label)
    assert tmux.typed == [] and tmux.killed == []
    with store_session() as store:
        store.upsert_session(
            TeamSession(
                id=agent.session_id or "",
                project_id=project.id,
                role="coder",
                started_at=now,
                last_seen_at=now,
            )
        )
    with monkeypatch.context() as patched:
        patched.setenv("PATH", str(tmp_path / "nowhere"))
        # The binary asked about is the one the replay starts: the recorded one (#144).
        with pytest.raises(
            FleetError,
            match="cannot restart 'coder-1': 'claude', the binary it was launched with, is no "
            "longer on your PATH",
        ):
            fleet_service.restart(project, agent.label)
    assert tmux.typed == [] and tmux.killed == []
    # A role whose declaration left the config after the agent started: `spawn` refuses
    # it before anything else, and that refusal came after the stop (review of #163,
    # round 2).
    monkeypatch.setattr(fleet_service, "_role_ok", lambda role: False)
    with pytest.raises(FleetError, match="cannot restart 'coder-1': unknown role 'coder'"):
        fleet_service.restart(project, agent.label)
    assert tmux.typed == [] and tmux.killed == []
    with store_session() as store:
        row = store.fleet_agent_by_label(project.id, agent.label)
    assert row is not None and row.id == agent.id  # still live, still running
    assert not tmux.facts[agent.pane_id].dead


def test_one_death_two_writers_record_at_once_is_announced_once(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A death was "announced once" with two holes: ``_end_dead_rows`` re-read the row
    one statement before its write, and ``reap`` wrote and announced from a snapshot
    taken before its tmux calls. Another writer landing in either gap announced the
    same death twice. The end is a compare-and-set now, and only the writer whose
    UPDATE ended the row posts ``agent_exited``."""
    coder = _coder(project)
    tmux.die(coder.pane_id, 1)
    views = fleet_service._observe_sockets([coder], f"asq-{_codename(project)}")
    read = SqliteStore.get_fleet_agent
    interleaved: list[bool] = []

    def another_reader_lands_after(self: SqliteStore, ref: str) -> FleetAgent | None:
        row = read(self, ref)
        if not interleaved:
            interleaved.append(True)
            fleet_service._end_dead_rows([coder], views)
        return row

    with monkeypatch.context() as patched:
        patched.setattr(SqliteStore, "get_fleet_agent", another_reader_lands_after)
        [row] = fleet_service._end_dead_rows([coder], views)
    assert row.exit_status == 1
    assert _events(project, "agent_exited") == ["coder-1 exited (1)"]

    # reap, with a listing landing between its read of the live rows and its write.
    second = _coder(project, label="coder-2")
    tmux.die(second.pane_id, 2)
    observe = fleet_service._observe_sockets

    def a_listing_meanwhile(*args: Any, **kwargs: Any) -> Any:
        seen = observe(*args, **kwargs)
        monkeypatch.setattr(fleet_service, "_observe_sockets", observe)
        fleet_service.list_agents(project)  # the UI's tick
        return seen

    monkeypatch.setattr(fleet_service, "_observe_sockets", a_listing_meanwhile)
    report = fleet_service.reap(project)
    assert _events(project, "agent_exited") == ["coder-1 exited (1)", "coder-2 exited (2)"]
    assert report.ended == []  # the listing recorded it first


def test_a_spawn_under_a_store_that_will_not_take_the_exit_says_so(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reconcile's best effort is a LISTING's: a spawn that could not record the dead
    manager refused with "already has a manager — fleet stop manager first", which the
    store's own error explains and that advice does not."""
    manager = fleet_service.spawn(project, "manager").agent
    tmux.die(manager.pane_id, 130)

    def locked(self: SqliteStore, agent_id: str, **kwargs: object) -> FleetAgent | None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(SqliteStore, "end_fleet_agent_if_live", locked)
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        fleet_service.spawn(project, "manager")
    [status] = fleet_service.list_agents(project)  # a listing still lists
    assert status.agent.id == manager.id and status.state == "exited"
    assert len(tmux.spawned) == 1


# --- #138 over the release train and #205: every read reconciles, beside stop and switch -------


def test_a_listing_that_records_a_crash_returns_its_claims_as_reap_does(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``reap`` releases what a dead pane's session still holds — a crash fires no
    ``SessionEnd`` — and a listing now records that death first (the UI ticks every
    two seconds), after which ``reap`` never sees the row live again. Ended without
    the release, the crashed coder's task stayed claimed by a dead session until the
    four-hour orphan prune, and the manager saw its work held by the dead."""
    mine = _task(project, "the task this coder is for")
    agent, first = _spawned(project, "coder", mine.id, tmux, monkeypatch)
    team_service.hook_session_start(first, project.root, "startup")
    team_service.claim_task(mine.id, session_ref=first)
    tmux.die(agent.pane_id, 137)  # killed from outside: no hook ran

    [seen] = fleet_service.list_agents(project)

    assert seen.state == "exited" and seen.agent.ended_at is not None
    freed = _task_now(mine.id)
    assert freed.claimed_by is None and freed.status == "todo"
    assert any("agent exited" in text for text in _events(project, "task_released"))
    assert _events(project, "agent_exited") == [f"{agent.label} exited (137)"]
    assert fleet_service.reap(project).ended == []  # nothing left for reap to do


def test_a_listing_that_records_the_death_a_stop_caused_leaves_it_a_stop_that_worked(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stop ends the row in the store session that re-reads it, and reads a row
    ended meanwhile as one that went away on its own (``NoSuchAgent``). Since every
    read reconciles a dead pane, the UI's tick landing between the ``/exit`` and the
    kill recorded the death the stop itself caused — and ``fleet stop`` (or the Stop
    button) failed with "no longer live" on an agent it had just stopped."""
    coder = _coder(project)
    real_keys = tmux.send_keys
    listed: list[list[FleetAgentStatus]] = []

    def enter_then_a_listing(pane_id: str, *keys: str) -> None:
        real_keys(pane_id, *keys)
        if pane_id == coder.pane_id and not listed:
            listed.append(fleet_service.list_agents(project))  # the UI's tick

    monkeypatch.setattr(tmux, "send_keys", enter_then_a_listing)

    receipt = fleet_service.stop(project, coder.label)

    [[seen]] = listed
    assert seen.state == "exited" and seen.agent.ended_at is not None  # the listing wrote it
    assert receipt.agent.id == coder.id and receipt.agent.exit_status == 0
    assert coder.pane_id in tmux.killed
    assert _events(project, "agent_exited") == [f"{coder.label} exited (0)"]  # once
    assert fleet_service.list_agents(project) == []


def test_a_listing_that_lands_inside_a_switch_leaves_the_hand_over_to_it(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``switch`` marks the session ``HANDOVER_STATE`` so its stop releases and
    announces nothing (#205). A listing that recorded the hand-over's dead pane in
    the gap before the kill released the parked claims, posted ``agent_exited`` and
    woke the manager for an agent on its way back — and the switch's own stop then
    found its row ended and abandoned the replacement. A marked row is the switch's
    to end."""
    _two_slots_with_usage(monkeypatch, work=95, personal=10)
    task = _task(project, "keep it across the move")
    agent = fleet_service.spawn(
        project, "coder", worktree=False, account="2", task_id=task.id
    ).agent
    old = agent.session_id or ""
    _with_transcript(agent, None)
    assert team_service.claim_task(task.id, session_ref=old).status == "doing"
    nudges: list[str] = []

    def nudge(project_id: str, *, reason: str, board: str | None = None) -> bool:
        nudges.append(reason)
        return True

    monkeypatch.setattr(fleet_service, "nudge_manager", nudge)
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    real_keys = tmux.send_keys
    listed: list[list[FleetAgentStatus]] = []

    def exit_then_a_listing(pane_id: str, *keys: str) -> None:
        real_keys(pane_id, *keys)
        if pane_id == agent.pane_id and not listed:
            # The old process's own SessionEnd parks the claims, its pane dies, and
            # the UI's tick looks before the switch's stop kills the window.
            team_service.hook_session_end(old, project.root, reason="prompt_input_exit")
            listed.append(fleet_service.list_agents(project))

    monkeypatch.setattr(tmux, "send_keys", exit_then_a_listing)

    receipt = fleet_service.switch(project, agent.label, reason="session limit")

    [[seen]] = listed
    assert seen.agent.id == agent.id and seen.agent.ended_at is None  # left to the switch
    assert receipt.stopped.id == agent.id and receipt.started.ended_at is None
    new = receipt.started.session_id
    held = _task_now(task.id)
    assert held.status == "doing" and held.claimed_by == new  # moved, never in the pool
    with store_session() as store:
        kinds = [e.kind for e in store.recent_events(project.id, limit=20)]
    assert "switched" in kinds
    assert "agent_exited" not in kinds and "task_released" not in kinds
    assert nudges == []


def test_restart_hands_off_the_session_s_own_entries_on_a_busy_board(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``restart`` shares the hand-off prompt with ``switch`` (``_respawn``) and read
    its entries the way ``switch`` read them before #205's third round: the project's
    last sixty, filtered down to the session — on a busy board all of them another
    agent's, and the prompt lost "the previous session's last board entries"."""
    task = _add_task(project, "Ship auth")
    agent = _coder(project, task_id=task.id)
    _with_transcript(agent, tmp_path / "missing.jsonl")  # named, not on disk
    with store_session() as store:
        team_service._emit(
            store, project.id, "note", "halfway through the router", session_id=agent.session_id
        )
        for n in range(70):
            team_service._emit(store, project.id, "note", f"chatter {n}", session_id="sess-other")
    original_spawn = tmux.spawn_window

    def ready_spawn(*args: Any, **kwargs: Any) -> WindowInfo:
        window = original_spawn(*args, **kwargs)
        tmux.set_command(window.pane_id, "claude")
        return window

    monkeypatch.setattr(tmux, "spawn_window", ready_spawn)

    receipt = fleet_service.restart(project, agent.label)

    assert receipt.resumed is False
    [prompt] = _first_lines(tmux, receipt.started.pane_id)
    assert "- note: halfway through the router" in prompt
    assert "chatter" not in prompt


def test_a_restart_whose_courtesy_event_cannot_be_written_still_reports_the_restart(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``switch`` fix of #205's fourth round, for the ``restarted`` line: the store
    was entered BEFORE the suppression, so a store that could not be opened for the
    courtesy raised out of a restart whose replacement was already running."""
    coder = _coder(project)
    tmux.die(coder.pane_id, 130)
    fleet_service.list_agents(project)  # 💤 exited
    real_spawn = fleet_service.spawn
    real_session = store_session
    wedged = False

    def spawn_then_wedge(*args: Any, **kwargs: Any) -> fleet_service.SpawnReceipt:
        nonlocal wedged
        receipt = real_spawn(*args, **kwargs)
        wedged = True  # every store open from here on refuses
        return receipt

    @contextmanager
    def session() -> Iterator[ContextStore]:
        if wedged:
            raise sqlite3.OperationalError("database is locked (fake)")
        with real_session() as store:
            yield store

    monkeypatch.setattr(fleet_service, "spawn", spawn_then_wedge)
    monkeypatch.setattr(fleet_service, "store_session", session)

    receipt = fleet_service.restart(project, coder.label)

    assert receipt.replaced.id == coder.id and receipt.started.ended_at is None
    assert _events(project, "restarted") == []  # the courtesy is what was lost, and only that


def test_a_restart_says_a_release_its_stop_could_not_make(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``stop`` REPORTS a release the store refused on its receipt (the release train);
    ``fleet stop`` prints it and exits 1. ``restart`` ends a LOST agent's row through
    the same ``stop`` and dropped the receipt, so a claim left with the ended session —
    the replacement's own task, which it then cannot take back until the lease lapses —
    went unsaid. (A RUNNING agent's restart is a hand-over since round 1 of #163's
    review, and releases nothing for the store to refuse.)"""
    mine = _task(project, "the task this coder is for")
    agent, first = _spawned(project, "coder", mine.id, tmux, monkeypatch)
    team_service.hook_session_start(first, project.root, "startup")
    team_service.claim_task(mine.id, session_ref=first)
    tmux.vanish(agent.pane_id)  # lost: no hook ran, and the row is still live

    def refuse(*args: object, **kwargs: object) -> list[TeamTask]:
        raise sqlite3.OperationalError("database is locked (fake)")

    monkeypatch.setattr(team_service, "release_agent_claims", refuse)

    receipt = fleet_service.restart(project, agent.label)

    assert receipt.was_running is False and receipt.replaced.ended_at is not None
    assert receipt.started.ended_at is None
    [said] = [note for note in receipt.notes if note.startswith("claims:")]
    assert "could not be released" in said and "locked" in said
    assert receipt.failures[0] == said, "what did not happen, as a switch's receipt says it"
    assert _task_now(mine.id).claimed_by == first


# --- review of #163, round 1: a running agent's restart is a hand-over; a mark guards its row --


def _recorded_nudges(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """``nudge_manager`` replaced by a recorder: the reasons it was called with."""
    nudges: list[str] = []

    def nudge(project_id: str, *, reason: str, board: str | None = None) -> bool:
        nudges.append(reason)
        return True

    monkeypatch.setattr(fleet_service, "nudge_manager", nudge)
    return nudges


def test_restarting_a_running_agent_hands_its_claims_to_the_replacement_and_announces_no_exit(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``restart`` stopped a RUNNING agent as ``fleet stop`` stops it: its task went back
    to the pool, ``agent_exited`` went out and the manager was nudged, all before
    ``_respawn`` started the replacement on that same task. That is the second worker
    #205's fourth round shut out of ``switch``, and the running leg is a hand-over the
    same way now: the claims wait, move onto the replacement's session with its row,
    and the one event is ``restarted``. A replacement that never starts gives them back
    and announces the exit that was withheld (``_abandon_handover``)."""
    mine = _task(project, "the task this coder is for")
    agent, first = _spawned(project, "coder", mine.id, tmux, monkeypatch)
    team_service.hook_session_start(first, project.root, "startup")
    team_service.claim_task(mine.id, session_ref=first)
    nudges = _recorded_nudges(monkeypatch)
    real_spawn = fleet_service.spawn
    holder_at_spawn: list[str | None] = []

    def spawn_and_look(*args: Any, **kwargs: Any) -> fleet_service.SpawnReceipt:
        holder_at_spawn.append(_task_now(mine.id).claimed_by)
        return real_spawn(*args, **kwargs)

    monkeypatch.setattr(fleet_service, "spawn", spawn_and_look)

    receipt = fleet_service.restart(project, agent.label)

    assert receipt.was_running is True and receipt.replaced.ended_at is not None
    assert holder_at_spawn == [first]  # never in the pool while the replacement started
    new = receipt.started.session_id
    assert new is not None and new != first
    held = _task_now(mine.id)
    assert held.status == "doing" and held.claimed_by == new
    assert nudges == []
    assert _events(project, "agent_exited") == [] and _events(project, "task_released") == []
    # Its hand-off prompt rides its command line (P0-W1a): handed over with the launch,
    # whether or not the replacement has come up yet.
    assert _events(project, "restarted") == [
        f"{agent.label} restarted — started fresh with a hand-off prompt"
    ]

    # The replacement runs and holds the task. Restarted again, its spawn is refused
    # after the stop: nothing is coming for the parked claims, so they go back.
    _become(receipt.started, tmux, monkeypatch, pid=PANE_PID, role="coder")
    team_service.hook_session_start(new, project.root, "startup")

    def refused(*args: Any, **kwargs: Any) -> fleet_service.SpawnReceipt:
        raise FleetError("tmux refused the window (fake)")

    monkeypatch.setattr(fleet_service, "spawn", refused)
    with pytest.raises(FleetError, match="refused the window"):
        fleet_service.restart(project, agent.label)

    freed = _task_now(mine.id)
    assert freed.claimed_by is None and freed.status == "todo"
    assert _events(project, "agent_exited") == [
        f"{agent.label} exited (0): {fleet_service.HANDOVER_FAILED}"
    ]
    assert nudges == [f"{agent.label} exited"]


def test_restart_hands_over_a_running_agent_whose_server_will_not_say_when_it_started(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of the #203 final-round fixes, F5: ``_pane_alive`` asked the server when it
    started as a second, strict tmux call and read its ``TmuxError`` as "not running".
    One refusal there (a wedged server's timeout, an OS refusal) after the pane had
    answered alive sent the restart of a RUNNING agent down the other branch: stopped
    as ``fleet stop`` stops it, its task back in the pool and its exit announced, the
    replacement started with no hand-over. A start that cannot be read judges nothing:
    the pane answered alive, so the agent is handed over."""
    mine = _task(project, "the task this coder is for")
    agent, first = _spawned(project, "coder", mine.id, tmux, monkeypatch)
    team_service.hook_session_start(first, project.root, "startup")
    team_service.claim_task(mine.id, session_ref=first)
    nudges = _recorded_nudges(monkeypatch)
    real_started_at = tmux.started_at
    refusals = [TmuxError("display-message timed out (fake)")]

    def refused_once() -> datetime | None:
        if refusals:
            raise refusals.pop()
        return real_started_at()

    monkeypatch.setattr(tmux, "started_at", refused_once)

    receipt = fleet_service.restart(project, agent.label)

    assert refusals == []  # the refusal was met, and it was the first question
    assert receipt.was_running is True
    held = _task_now(mine.id)
    assert held.status == "doing" and held.claimed_by == receipt.started.session_id
    assert _events(project, "agent_exited") == [] and _events(project, "task_released") == []
    assert nudges == []


def test_a_resumed_replacement_that_dies_before_its_first_hook_is_ended_like_any_dead_row(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A listing leaves a row a hand-over is stopping to that hand-over, and the rule
    keyed on the SESSION's mark, which only the replacement's own ``SessionStart``
    clears. A resumed replacement shares the id, so one whose ``claude --resume`` died
    before its first hook (the failure ``switch`` itself names) was skipped by every
    listing and every ``spawn`` for good: no ``agent_exited``, and ``fleet spawn
    manager`` refused with "already has a manager", the #138 trap. The mark guards only
    a row older than it now, and only for ``HANDOVER_GRACE``: the old row of a switch
    killed on the way is ended like any dead row once that has passed."""
    _two_slots_with_usage(monkeypatch, work=95, personal=10)
    manager = fleet_service.spawn(project, "manager", account="2").agent
    transcript = tmp_path / f"{manager.session_id}.jsonl"
    transcript.write_text('{"type":"user"}\n', encoding="utf-8")
    _with_transcript(manager, transcript)
    moved = fleet_service.switch(project, "manager", reason="session limit")
    assert moved.resumed is True and moved.started.session_id == manager.session_id
    tmux.die(moved.started.pane_id, 1)  # `claude --resume` failed: no hook ever ran

    [seen] = fleet_service.list_agents(project)

    assert seen.agent.id == moved.started.id and seen.state == "exited"
    assert seen.agent.ended_at is not None and seen.agent.exit_status == 1
    assert _events(project, "agent_exited") == ["manager exited (1)"]
    again = fleet_service.spawn(project, "manager")  # not "already has a manager"
    assert again.agent.id != moved.started.id

    # A switch killed between its `/exit` and its kill: the old row's pane is dead and
    # its session marked. Inside the grace that is a hand-over in flight; after it, not.
    coder = _coder(project)
    _with_transcript(coder, None)
    with store_session() as store:
        store.touch_session(coder.session_id or "", state=team_service.HANDOVER_STATE)
    tmux.die(coder.pane_id, 0)
    [inside] = [s for s in fleet_service.list_agents(project) if s.agent.id == coder.id]
    assert inside.agent.ended_at is None  # left to the switch that is ending it
    later = datetime.now(tz=UTC) + fleet_service.HANDOVER_GRACE + timedelta(seconds=1)
    monkeypatch.setattr(fleet_service, "_now", lambda: later)
    [after] = [s for s in fleet_service.list_agents(project) if s.agent.id == coder.id]
    assert after.state == "exited" and after.agent.ended_at is not None
    assert f"{coder.label} exited (0)" in _events(project, "agent_exited")


@pytest.mark.parametrize("gap", ["dead", "gone"])
def test_a_reap_that_lands_inside_a_switch_leaves_the_hand_over_to_it(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    gap: str,
) -> None:
    """The listing's hand-over guard, for ``reap``. Run by hand between a switch's
    ``/exit`` and its kill (the pane reads dead), or between the kill and the stop's
    end (gone), it ended the moving row, released the parked claims and woke the
    manager, and the switch then failed on its own row with the agent already down."""
    _two_slots_with_usage(monkeypatch, work=95, personal=10)
    task = _task(project, "keep it across the move")
    agent = fleet_service.spawn(
        project, "coder", worktree=False, account="2", task_id=task.id
    ).agent
    _coder(project, label="coder-other")  # keeps the session, and the server, up
    old = agent.session_id or ""
    _with_transcript(agent, None)
    assert team_service.claim_task(task.id, session_ref=old).status == "doing"
    nudges = _recorded_nudges(monkeypatch)
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    reaped: list[fleet_service.ReapReport] = []
    if gap == "dead":
        real_keys = tmux.send_keys

        def exit_then_a_reap(pane_id: str, *keys: str) -> None:
            real_keys(pane_id, *keys)
            if pane_id == agent.pane_id and not reaped:
                team_service.hook_session_end(old, project.root, reason="prompt_input_exit")
                reaped.append(fleet_service.reap(project))

        monkeypatch.setattr(tmux, "send_keys", exit_then_a_reap)
    else:
        real_kill = tmux.kill_window

        def kill_then_a_reap(pane_id: str) -> None:
            real_kill(pane_id)
            if pane_id == agent.pane_id and not reaped:
                reaped.append(fleet_service.reap(project))

        monkeypatch.setattr(tmux, "kill_window", kill_then_a_reap)

    receipt = fleet_service.switch(project, agent.label, reason="session limit")

    [report] = reaped
    assert report.ended == [] and report.lost == []
    assert receipt.stopped.id == agent.id and receipt.started.ended_at is None
    held = _task_now(task.id)
    assert held.status == "doing" and held.claimed_by == receipt.started.session_id
    with store_session() as store:
        kinds = [e.kind for e in store.recent_events(project.id, limit=20)]
    assert "agent_exited" not in kinds and "task_released" not in kinds
    assert nudges == []


def test_a_switch_whose_label_passed_to_another_agent_meanwhile_leaves_that_agent_alone(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``switch`` reads its row, asks the accounts service for a target (a headroom
    lookup that reads every account's usage over the network), and then stopped the
    agent by LABEL. An agent that exited and was started again under that label in
    between was the one stopped: ``/exit`` typed into the newcomer, which was then
    replaced by a resume of the OLD agent's session. The stop is pinned to the row
    the switch read, as the agent view's Stop is."""
    from aisquare.services import claude_accounts as accounts_service

    _two_slots_with_usage(monkeypatch, work=95, personal=10)
    agent = fleet_service.spawn(project, "coder", worktree=False, account="2").agent
    _with_transcript(agent, None)
    real_choose = accounts_service.choose_for_handover
    newcomer: list[FleetAgent] = []

    def choose_while_the_label_moves(*args: Any, **kwargs: Any) -> Any:
        tmux.die(agent.pane_id, 0)
        fleet_service.list_agents(project)  # the UI's tick records the exit
        newcomer.append(_coder(project, label=agent.label))  # the manager starts it again
        return real_choose(*args, **kwargs)

    monkeypatch.setattr(accounts_service, "choose_for_handover", choose_while_the_label_moves)

    with pytest.raises(NoSuchAgent, match="another agent now"):
        fleet_service.switch(project, agent.label, reason="session limit")

    [new] = newcomer
    assert new.label == agent.label
    assert [typed for typed in tmux.typed if typed[0] == new.pane_id] == []
    assert new.pane_id not in tmux.killed and _row(new.id).ended_at is None
    assert len(tmux.spawned) == 2  # the original and the newcomer: no replacement


def test_a_listing_whose_write_lands_after_a_stop_read_its_row_is_not_announced_twice(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_stop_row`` read its row and then ended it: the read-then-write that
    ``end_fleet_agent_if_live`` was added to close for listings. A listing in another
    process that had seen the dead pane and wrote between the two ended and announced
    the death, and the stop announced it again: two ``agent_exited`` lines and two
    nudges for one stop. The stop ends the row by the same compare-and-set now."""
    coder = _coder(project)
    nudges = _recorded_nudges(monkeypatch)
    seen: list[Any] = []
    real_keys = tmux.send_keys

    def exit_then_a_look(pane_id: str, *keys: str) -> None:
        real_keys(pane_id, *keys)
        if pane_id == coder.pane_id and not seen:
            # A listing in another process observes the dead pane…
            seen.append(fleet_service._observe_sockets([coder], f"asq-{_codename(project)}"))

    real_get = SqliteStore.get_fleet_agent
    landed = False

    def read_then_the_listing_writes(self: SqliteStore, agent_id: str) -> FleetAgent | None:
        nonlocal landed
        row = real_get(self, agent_id)
        if agent_id == coder.id and coder.pane_id in tmux.killed and not landed:
            landed = True  # …and its write lands right after the stop reads the row
            fleet_service._end_dead_rows([coder], seen[0])
        return row

    monkeypatch.setattr(tmux, "send_keys", exit_then_a_look)
    monkeypatch.setattr(SqliteStore, "get_fleet_agent", read_then_the_listing_writes)

    receipt = fleet_service.stop(project, coder.label)

    assert landed and receipt.agent.id == coder.id and receipt.agent.ended_at is not None
    assert _events(project, "agent_exited") == [f"{coder.label} exited (0)"]  # once
    # A clean exit that left nothing open is news, not a wake (P1 WS-2, #279): no nudge.
    assert nudges == []


def test_a_stop_whose_death_a_listing_recorded_asks_again_for_the_release_it_could_not_make(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A listing that records the death a stop's ``/exit`` caused leaves the stop a stop
    that worked, and the stop returned that row with nothing released, without asking.
    A listing has no report to carry a release the store refused, so a claim the dead
    session still held stayed with it until the lease lapsed, and nothing said so. The
    stop asks again (a release is idempotent) and its receipt carries what that did."""
    mine = _task(project, "the task this coder is for")
    agent, first = _spawned(project, "coder", mine.id, tmux, monkeypatch)
    team_service.hook_session_start(first, project.root, "startup")
    team_service.claim_task(mine.id, session_ref=first)
    real_release = team_service.release_agent_claims
    refusals = 1

    def refuse_once(*args: Any, **kwargs: Any) -> Any:
        nonlocal refusals
        if refusals:
            refusals -= 1
            raise sqlite3.OperationalError("database is locked (fake)")
        return real_release(*args, **kwargs)

    monkeypatch.setattr(team_service, "release_agent_claims", refuse_once)
    real_keys = tmux.send_keys
    listed: list[list[FleetAgentStatus]] = []

    def enter_then_a_listing(pane_id: str, *keys: str) -> None:
        real_keys(pane_id, *keys)
        if pane_id == agent.pane_id and not listed:
            listed.append(fleet_service.list_agents(project))  # the UI's tick

    monkeypatch.setattr(tmux, "send_keys", enter_then_a_listing)

    receipt = fleet_service.stop(project, agent.label)

    [[recorded]] = listed
    assert recorded.agent.ended_at is not None and refusals == 0  # its release was refused
    assert [task.id for task in receipt.released] == [mine.id]
    assert receipt.release_failed is None
    assert _task_now(mine.id).claimed_by is None
    assert _events(project, "agent_exited") == [f"{agent.label} exited (0)"]  # still once


# --- review of #163, round 2: the mark outlives the agent's own hooks; one hand-over at a time --


@pytest.mark.parametrize("landing", ["stop", "attention", "wakeup"])
def test_a_turn_that_ends_inside_a_restart_s_grace_leaves_the_hand_over_its_mark(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    landing: str,
) -> None:
    """``restart`` hands a running agent over as ``switch`` does: the session is marked
    ``HANDOVER_STATE``, then ``/exit`` is typed. An agent in the middle of a turn finishes
    it first, and its own hooks wrote over the mark: ``hook_stop`` wrote ``waiting``, a
    manager's wake-up ``working``, a permission prompt ``attention``. The ``SessionEnd``
    that followed released the claims the replacement was to inherit, a listing ended the
    row under the hand-over, and the hand-over's stop raised after the kill: the agent
    gone, nothing started, the task back in the pool. A marked session's own hooks leave
    the mark to the hand-over now, and a manager on its way out is not kept going."""
    role = "manager" if landing == "wakeup" else "coder"
    mine = _task(project, "the task this agent holds")
    agent, first = _spawned(project, role, mine.id if role == "coder" else None, tmux, monkeypatch)
    team_service.hook_session_start(first, project.root, "startup")
    team_service.claim_task(mine.id, session_ref=first)
    if landing == "wakeup":
        with store_session() as store:  # a decision on the board since its cursor
            team_service._emit(store, project.id, "question", "which schema version?")
    nudges = _recorded_nudges(monkeypatch)
    real_keys = tmux.send_keys
    decisions: list[team_service.StopDecision | None] = []

    def enter_then_the_turn_ends(pane_id: str, *keys: str) -> None:
        real_keys(pane_id, *keys)
        if pane_id == agent.pane_id and not decisions:
            if landing == "attention":
                team_service.hook_notification(
                    first,
                    project.root,
                    "Claude needs your permission to use Bash",
                    notification_type="permission_prompt",
                )
            decisions.append(team_service.hook_stop(first, project.root))
            team_service.hook_session_end(first, project.root, reason="prompt_input_exit")
            fleet_service.list_agents(project)  # the UI's tick

    monkeypatch.setattr(tmux, "send_keys", enter_then_the_turn_ends)

    receipt = fleet_service.restart(project, agent.label)

    assert decisions == [None]  # no wake-up for an agent on its way out
    assert receipt.was_running is True and receipt.started.ended_at is None
    assert len(tmux.spawned) == 2
    held = _task_now(mine.id)
    assert held.status == "doing" and held.claimed_by == receipt.started.session_id
    assert _events(project, "agent_exited") == [] and _events(project, "task_released") == []
    assert _events(project, "attention") == []
    assert nudges == []


@pytest.mark.parametrize("starts", [True, False], ids=["replaced", "spawn-refused"])
@pytest.mark.parametrize("unguarded", ["no-session", "mark-lost"])
@pytest.mark.parametrize("command", ["restart", "switch"])
def test_a_hand_over_whose_row_a_listing_ended_after_its_exit_is_a_stop_that_worked(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    unguarded: str,
    starts: bool,
) -> None:
    """A listing leaves a hand-over's row alone only while its session carries the mark,
    and nothing marks a session that is not on the board yet (its ``SessionStart`` not
    in, or the orchestrator off), nor keeps a mark a start on the same id replaces (a
    compaction's ``SessionStart`` inside the grace). A listing then records the death the
    hand-over's own ``/exit`` caused, released and announced as any death is, and the
    hand-over's stop raised "no longer live" after the kill: the agent gone and nothing
    started. It is a stop that worked, as it always was for a plain one, and the
    replacement starts. What that listing announced is not announced again should the
    replacement never start."""
    if command == "switch":
        _two_slots_with_usage(monkeypatch, work=95, personal=10)
    mine = _task(project, "the task this coder is for")
    agent, first = _spawned(project, "coder", mine.id, tmux, monkeypatch)
    if unguarded == "mark-lost":
        team_service.hook_session_start(first, project.root, "startup")
        team_service.claim_task(mine.id, session_ref=first)
    nudges = _recorded_nudges(monkeypatch)
    if not starts:

        def refused(*args: Any, **kwargs: Any) -> fleet_service.SpawnReceipt:
            raise FleetError("tmux refused the window (fake)")

        monkeypatch.setattr(fleet_service, "spawn", refused)
    real_keys = tmux.send_keys
    listed: list[list[FleetAgentStatus]] = []

    def enter_then_a_listing(pane_id: str, *keys: str) -> None:
        real_keys(pane_id, *keys)
        if pane_id == agent.pane_id and not listed:
            if unguarded == "mark-lost":
                # Through the session's own start, the one writer besides the take-back
                # that replaces the mark: every other state write keeps it since the
                # #205 fold's round 1 (`SqliteStore.touch_session`), so the heartbeat
                # this leg used to lose it with no longer does.
                with store_session() as store:
                    session = store.get_session(first)
                    assert session is not None
                    store.upsert_session(session)
                assert _session_state(first) == "working"
            listed.append(fleet_service.list_agents(project))  # the UI's tick

    monkeypatch.setattr(tmux, "send_keys", enter_then_a_listing)

    def hand_over() -> tuple[FleetAgent, FleetAgent]:
        if command == "restart":
            restarted = fleet_service.restart(project, agent.label)
            assert restarted.was_running is True
            return restarted.replaced, restarted.started
        switched = fleet_service.switch(project, agent.label, reason="session limit")
        return switched.stopped, switched.started

    if starts:
        replaced, started = hand_over()
        assert replaced.id == agent.id and replaced.ended_at is not None
        assert started.ended_at is None and len(tmux.spawned) == 2
    else:
        with pytest.raises(FleetError, match="refused the window"):
            hand_over()
    [[recorded]] = listed
    assert recorded.agent.id == agent.id and recorded.agent.ended_at is not None
    assert _events(project, "agent_exited") == [f"{agent.label} exited (0)"]  # the listing's
    # Nudged only when the exit released a claim (P1 WS-2, #279): a clean exit with
    # nothing open is news the next wake carries.
    assert nudges == ([f"{agent.label} exited"] if unguarded == "mark-lost" else [])


@pytest.mark.parametrize("intruder", ["restart", "switch"])
def test_a_restart_or_a_switch_inside_a_switch_is_refused_and_leaves_the_hand_over_to_it(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    intruder: str,
) -> None:
    """The listing and ``reap`` leave a row a hand-over is stopping to it; ``restart`` read
    that same dead pane, found ``_end_dead_rows`` skipping it, and fell through to a plain
    ``stop``: the parked claims went back to the pool, ``agent_exited`` went out, the
    switch failed on its own row and the restart's replacement did not hold the task. A
    second ``switch`` (by hand, while the automatic one waits out its grace) stopped the
    row itself and started a replacement of its own. Both are refused while the hand-over
    holds the row, as the automatic path refuses a session already in flight."""
    _two_slots_with_usage(monkeypatch, work=95, personal=10)
    task = _task(project, "keep it across the move")
    agent = fleet_service.spawn(
        project, "coder", worktree=False, account="2", task_id=task.id
    ).agent
    _coder(project, label="coder-other")  # keeps the session, and the server, up
    old = agent.session_id or ""
    _with_transcript(agent, None)
    assert team_service.claim_task(task.id, session_ref=old).status == "doing"
    nudges = _recorded_nudges(monkeypatch)
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    refusals: list[str] = []
    real_keys = tmux.send_keys

    def exit_then_another_hand_over(pane_id: str, *keys: str) -> None:
        real_keys(pane_id, *keys)
        if pane_id == agent.pane_id and not refusals:
            team_service.hook_session_end(old, project.root, reason="prompt_input_exit")
            try:
                if intruder == "restart":
                    fleet_service.restart(project, agent.label)
                else:
                    fleet_service.switch(project, agent.label)
            except FleetError as exc:
                refusals.append(str(exc))
            else:
                refusals.append("not refused")

    monkeypatch.setattr(tmux, "send_keys", exit_then_another_hand_over)

    receipt = fleet_service.switch(project, agent.label, reason="session limit")

    [refusal] = refusals
    assert "a hand-over is already moving it" in refusal
    assert receipt.stopped.id == agent.id and receipt.started.ended_at is None
    assert len(tmux.spawned) == 3  # the two above, and the switch's replacement alone
    held = _task_now(task.id)
    assert held.status == "doing" and held.claimed_by == receipt.started.session_id
    with store_session() as store:
        kinds = [e.kind for e in store.recent_events(project.id, limit=20)]
    assert "agent_exited" not in kinds and "task_released" not in kinds
    assert nudges == []


@pytest.mark.parametrize(
    "pair", [("restart", "restart"), ("restart", "switch"), ("switch", "switch")]
)
def test_two_hand_overs_of_one_agent_at_once_start_one_replacement_and_refuse_the_other(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    pair: tuple[str, str],
) -> None:
    """Each hand-over reads the session and refuses one a hand-over holds, then marks it.
    The mark was an unconditional write, so two hand-overs that both read the session
    before either marked it (a restart racing a restart, a restart racing a switch, a
    switch by hand racing the automatic one) both marked it. The second typed its
    ``/exit``, the first ended the row and took its mark back, and the second's stop then
    read the ended row, with no mark on it, as a stop that worked: both started a
    replacement (review of #203). Driven in exactly that order here. The mark is a
    compare-and-set now, so the second is refused before it stops anything."""
    _two_slots_with_usage(monkeypatch, work=95, personal=10)
    task = _task(project, "keep it across the move")
    agent = fleet_service.spawn(
        project, "coder", worktree=False, account="2", task_id=task.id
    ).agent
    _coder(project, label="coder-other")  # keeps the session, and the server, up
    _with_transcript(agent, None)
    assert team_service.claim_task(task.id, session_ref=agent.session_id or "").status == "doing"
    _recorded_nudges(monkeypatch)
    monkeypatch.setenv("AISQUARE_ROLE", "coder")

    def me() -> str:
        return threading.current_thread().name

    both_read = threading.Barrier(2, timeout=30)  # both past their read, neither marked
    first_marked, second_moved, first_unmarked = (threading.Event() for _ in range(3))
    real_check = fleet_service._refuse_a_replay_that_cannot_start
    real_mark = fleet_service._mark_handing_over
    real_unmark = fleet_service._unmark_handing_over
    real_keys = tmux.send_keys

    def check_then_wait_for_the_other(*args: Any, **kwargs: Any) -> None:
        real_check(*args, **kwargs)
        both_read.wait()

    def mark_in_turn(*args: Any) -> None:
        if me() == "second":
            first_marked.wait(30)
            real_mark(*args)
            return
        real_mark(*args)
        first_marked.set()
        second_moved.wait(30)  # the second types its /exit (or is refused) first

    def unmark_then_say_so(*args: Any) -> str | None:
        try:
            return real_unmark(*args)
        finally:
            if me() == "first":
                first_unmarked.set()

    def keys_then_wait_for_the_first(pane_id: str, *keys: str) -> None:
        real_keys(pane_id, *keys)
        if me() == "second" and pane_id == agent.pane_id:
            second_moved.set()
            first_unmarked.wait(30)  # the first ends the row and takes its mark back

    monkeypatch.setattr(
        fleet_service, "_refuse_a_replay_that_cannot_start", check_then_wait_for_the_other
    )
    monkeypatch.setattr(fleet_service, "_mark_handing_over", mark_in_turn)
    monkeypatch.setattr(fleet_service, "_unmark_handing_over", unmark_then_say_so)
    monkeypatch.setattr(tmux, "send_keys", keys_then_wait_for_the_first)
    outcomes: dict[str, object] = {}

    def hand_over(command: str) -> None:
        try:
            if command == "restart":
                outcomes[me()] = fleet_service.restart(project, agent.label).started
            else:
                outcomes[me()] = fleet_service.switch(project, agent.label).started
        except Exception as exc:
            outcomes[me()] = exc
        finally:
            if me() == "second":
                second_moved.set()

    threads = [
        threading.Thread(target=hand_over, args=(command,), name=name)
        for name, command in zip(("first", "second"), pair, strict=True)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=90)

    started, refused = outcomes.get("first"), outcomes.get("second")
    assert isinstance(started, FleetAgent), outcomes
    assert isinstance(refused, FleetError), outcomes
    assert "a hand-over is already moving it" in str(refused)
    assert len(tmux.spawned) == 3  # the two above, and one replacement
    assert tmux.typed.count((agent.pane_id, "literal", "/exit")) == 1, "the refused one typed none"
    held = _task_now(task.id)
    assert held.status == "doing" and held.claimed_by == started.session_id


def test_a_hand_over_refused_after_its_agent_s_session_ended_leaves_that_session_ended(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hand-over whose stop is refused puts the session's state back, and did it with
    ``touch_session``: a heartbeat, which also clears ``ended_at``. Refused after the
    agent's own ``SessionEnd`` (the operator's ``fleet stop`` ended the row in the
    grace), that brought a dead session back to the board as a live teammate. Only the
    mark is taken back now, and only while it is still there."""
    mine = _task(project, "the task this coder is for")
    agent, first = _spawned(project, "coder", mine.id, tmux, monkeypatch)
    team_service.hook_session_start(first, project.root, "startup")
    team_service.claim_task(mine.id, session_ref=first)
    real_keys = tmux.send_keys
    stopped: list[fleet_service.StopReceipt] = []

    def exit_then_the_operator_stops_it(pane_id: str, *keys: str) -> None:
        real_keys(pane_id, *keys)
        if pane_id == agent.pane_id and not stopped:
            team_service.hook_session_end(first, project.root, reason="prompt_input_exit")
            stopped.append(fleet_service.stop(project, agent.label))  # another terminal

    monkeypatch.setattr(tmux, "send_keys", exit_then_the_operator_stops_it)

    with pytest.raises(NoSuchAgent, match="no longer live"):
        fleet_service.restart(project, agent.label)

    [operator] = stopped
    assert operator.agent.id == agent.id and len(tmux.spawned) == 1  # that stop stands
    with store_session() as store:
        session = store.get_session(first)
    assert session is not None and session.ended_at is not None
    assert session.state != team_service.HANDOVER_STATE


def test_a_resumed_restart_is_told_in_one_line_to_carry_on(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``claude --resume`` opens the conversation at an idle prompt, and ``switch`` types
    its replacement a line to continue for that reason (review of #205, finding 5).
    ``restart`` typed nothing, so a resumed agent sat idle — a running one's restart
    holding its task's claim the while — and a resumed manager, ``working`` from its
    start hook with no turn to end, was refused the board's nudges while that row
    stayed fresh."""
    mine = _task(project, "the task this coder is for")
    agent, first = _spawned(project, "coder", mine.id, tmux, monkeypatch)
    team_service.hook_session_start(first, project.root, "startup")
    team_service.claim_task(mine.id, session_ref=first)
    transcript = tmp_path / f"{first}.jsonl"
    transcript.write_text('{"type":"user"}\n', encoding="utf-8")
    with store_session() as store:
        store.upsert_session(
            TeamSession(
                id=first,
                project_id=project.id,
                role="coder",
                started_at=datetime.now(tz=UTC),
                last_seen_at=datetime.now(tz=UTC),
                transcript_path=str(transcript),
            )
        )

    receipt = fleet_service.restart(project, agent.label)

    assert receipt.resumed is True and receipt.started.session_id == first
    assert _task_now(mine.id).claimed_by == first  # the same session carries the claim on
    [line] = _first_lines(tmux, receipt.started.pane_id)
    assert "\n" not in line and line.startswith(f"You are {agent.label}, restarted")
    assert "aisquare board" in line and "continue" in line


def test_a_restart_s_mark_does_not_outlive_the_stop_it_was_set_for(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``switch``'s rule since the #205 fold's round 2, for the hand-over ``restart`` runs on
    a running agent (#163), which met it at the stack's fold. ``restart`` took the mark
    back only for a stop that raised an ``Exception``: a Ctrl-C in the grace is a
    ``KeyboardInterrupt`` and left the running agent marked, and a completed resume left
    the mark to the replacement's start hook — every state writer keeps it, so one that
    failed open left the agent ``switching`` through every later prompt: no ``limited``
    recorded, no hand-over, no bell. The mark's one reader is the old process's
    ``SessionEnd``, which has run by the time ``stop`` returns, or never will."""
    mine = _task(project, "the task this coder is for")
    agent, first = _spawned(project, "coder", mine.id, tmux, monkeypatch)
    team_service.hook_session_start(first, project.root, "startup")
    team_service.claim_task(mine.id, session_ref=first)
    transcript = tmp_path / f"{first}.jsonl"
    transcript.write_text('{"type":"user"}\n', encoding="utf-8")
    _with_transcript(agent, transcript)
    before = _session_state(first)
    assert before != team_service.HANDOVER_STATE

    def interrupted(*args: Any, **kwargs: Any) -> fleet_service.StopReceipt:
        raise KeyboardInterrupt

    real_stop = fleet_service.stop
    monkeypatch.setattr(fleet_service, "stop", interrupted)
    with pytest.raises(KeyboardInterrupt):
        fleet_service.restart(project, agent.label)
    assert _session_state(first) == before  # as it was: the agent is still running
    monkeypatch.setattr(fleet_service, "stop", real_stop)

    # Completed: the old SessionEnd lands in the grace and parks the claim for the resume…
    real_kill = tmux.kill_window

    def exit_then_kill(pane_id: str) -> None:
        if pane_id == agent.pane_id:
            team_service.hook_session_end(first, project.root, reason="prompt_input_exit")
        real_kill(pane_id)

    monkeypatch.setattr(tmux, "kill_window", exit_then_kill)
    receipt = fleet_service.restart(project, agent.label)
    assert receipt.was_running and receipt.resumed and receipt.started.session_id == first
    # …the replacement's SessionStart never lands (a start hook that failed open), and the
    # heartbeat of the line it was typed repairs the row, as every prompt's always did.
    team_service.hook_prompt_heartbeat(first, project.root)
    with store_session() as store:
        resumed = store.get_session(first)
    assert resumed is not None and resumed.ended_at is None and resumed.state == "working"
    held = _task_now(mine.id)
    assert held.status == "doing" and held.claimed_by == first  # parked, never released


def test_the_auto_mode_board_line_s_own_way_out_brings_the_agent_back_off_auto(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    runner: CliRunner,
) -> None:
    """The ``auto_mode_blocked`` line (#150) names its way out: a non-classifier mode for
    the role, then ``aisquare fleet restart <label> --permission-mode acceptEdits``,
    whose session resumes. Followed here as printed, from the project's directory: the
    replacement resumes the same transcript and starts in the mode that was set. A
    restart replays the mode the agent was launched with (#144), so the line used to
    name the role's step and a bare restart, and followed as printed that resumed the
    agent in ``auto``, refused as before — the loop the review of #164 (round 1) left
    this guard for. The restart's own flag is what reaches the agent, and its launch
    spec records it: the next restart keeps it without being told again."""
    agent, first = _spawned(project, "coder", None, tmux, monkeypatch)
    assert _flag(_command(tmux), "--permission-mode") == "auto"  # the built-in default
    refusal = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "content": "claude-opus-5[1m] is temporarily unavailable (server error), so "
                    "auto mode cannot determine the safety of Bash right now.",
                    "is_error": True,
                }
            ],
        },
    }
    transcript = tmp_path / f"{first}.jsonl"
    transcript.write_text("".join(json.dumps(refusal) + "\n" for _ in range(3)), encoding="utf-8")
    _with_transcript(agent, transcript)
    monkeypatch.setenv(PIPELINE_ID_ENV_VAR, "run-150")  # the agent was launched traced
    assert auto_mode.record_refusals(first) == 3
    monkeypatch.delenv(PIPELINE_ID_ENV_VAR)
    [line] = _events(project, auto_mode.EVENT_KIND)
    way_out = re.search(
        r"set a non-classifier mode \((aisquare config set [^)]+)\) and "
        r"`(aisquare fleet restart [^`]+)`",
        line,
    )
    assert way_out is not None, line
    monkeypatch.chdir(project.root)

    for step in way_out.groups():
        result = runner.invoke(app, step.split()[1:])
        assert result.exit_code == 0, (step, result.output)

    command = _command(tmux)
    assert _flag(command, "--resume") == str(transcript)
    assert _flag(command, "--permission-mode") == "acceptEdits"
    fleet_service.restart(project, agent.label)
    assert _flag(_command(tmux), "--permission-mode") == "acceptEdits", "the spec kept it"


def test_the_spawn_note_names_the_restart_under_the_label_the_agent_was_recorded_with(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #169, round 1: the auto-mode note on a spawn's receipt (#150) printed
    `aisquare fleet restart <label> --permission-mode acceptEdits` for an agent whose
    label `spawn` had just recorded, where the board line names the real one. It names
    the recorded label now, so the step runs as printed — on the receipt of a restart
    that replays `auto` too, which starts its replacement through `spawn`."""
    monkeypatch.setattr("aisquare.services.explainability.tracing_configured", lambda: True)
    refused = auto_mode.Sample(
        session_id="s-hit",
        role="coder",
        started_at=datetime.now(tz=UTC),
        tokens=80_000,
        refusals=auto_mode.REFUSAL_THRESHOLD,
    )
    monkeypatch.setattr(auto_mode, "measure_baseline", lambda: auto_mode.Baseline((refused,)))
    receipt = fleet_service.spawn(project, "coder", label="coder-auth", worktree=False)
    step = f"`{_fleet('restart', project, 'coder-auth', '--permission-mode', 'acceptEdits')}`"

    assert any(step in note for note in receipt.notes), receipt.notes
    tmux.die(receipt.agent.pane_id, 1)
    again = fleet_service.restart(project, "coder-auth")
    assert any(step in note for note in again.notes), again.notes


def test_the_auto_mode_evidence_is_read_before_the_window_starts(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #169, round 2: the baseline behind the auto-mode note (#150) was read
    after ``_record``, in the window in which the agent's first hook races ``_take_over``,
    and by then the agent's own new session could be the newest "recent" one and push
    the refused session out of the sample, so the warning was dropped. It is read before
    the window starts. The note is still worded under the label ``_record`` settled on,
    here one a parallel spawn made it re-pick (F10)."""
    monkeypatch.setattr("aisquare.services.explainability.tracing_configured", lambda: True)
    _coder(project, label="coder-auth")
    refused = auto_mode.Sample(
        session_id="s-hit",
        role="coder",
        started_at=datetime.now(tz=UTC),
        tokens=80_000,
        refusals=auto_mode.REFUSAL_THRESHOLD,
    )
    windows = len(tmux.spawned)

    def measured() -> auto_mode.Baseline:
        # Once the new window exists, its session is the newest and the refused one is gone.
        return auto_mode.Baseline(() if len(tmux.spawned) > windows else (refused,))

    monkeypatch.setattr(auto_mode, "measure_baseline", measured)
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
    step = f"`{_fleet('restart', project, 'coder-auth-2', '--permission-mode', 'acceptEdits')}`"
    assert any(step in note for note in receipt.notes), receipt.notes


def test_restarting_a_death_no_listing_recorded_announces_it_but_wakes_no_manager(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """``restart`` records a death no listing has seen yet the way a listing records it,
    and that nudged the manager: woken by "coder-1 exited" just as the replacement
    started, before ``restarted`` was on the board, it could run its own ``fleet restart
    coder-1`` — by label, onto the replacement. The death is still announced; the
    manager is not woken for an agent that is already coming back."""
    manager = _waiting_manager(tmux, project)
    coder = _coder(project, label="coder-1")
    tmux.die(coder.pane_id, 1)

    receipt = fleet_service.restart(project, "coder-1")

    assert receipt.replaced.id == coder.id and receipt.replaced.exit_status == 1
    assert _events(project, "agent_exited") == ["coder-1 exited (1)"]
    assert [typed for typed in tmux.typed if typed[0] == manager.pane_id] == []


def test_restarting_an_agent_whose_pane_vanished_announces_it_but_wakes_no_manager(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """The twin above, for a pane that is GONE rather than dead: ``_end_dead_rows``
    records only a pane it sees dead, so ``restart`` ends a vanished one's row through
    ``stop`` — which nudged the manager with "coder-1 exited" while the replacement
    was starting, the race the dead pane's ``nudge=False`` closed (review of the
    accounts stack's fold, round 1, F1)."""
    manager = _waiting_manager(tmux, project)
    coder = _coder(project, label="coder-1")
    tmux.vanish(coder.pane_id)

    receipt = fleet_service.restart(project, "coder-1")

    assert receipt.replaced.id == coder.id and receipt.replaced.ended_at is not None
    assert receipt.was_running is False and receipt.started.ended_at is None
    assert _events(project, "agent_exited") == ["coder-1 exited (?)"]
    assert [typed for typed in tmux.typed if typed[0] == manager.pane_id] == []


@pytest.mark.parametrize("gone", ["dead", "vanished"])
def test_a_restart_whose_replacement_never_starts_wakes_the_manager_it_held_back(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    gone: str,
) -> None:
    """The two twins above hold the manager's wake-up back because a replacement is
    coming. When ``_respawn`` then fails, none is: the exit stood announced, the claims
    were back in the pool, and nothing woke the manager waiting on the board — the
    running agent's hand-over wakes it through ``_abandon_handover``, these did not
    (review of the accounts stack's fold, round 2, F1). The restart still fails."""
    manager = _waiting_manager(tmux, project)
    coder = _coder(project, label="coder-1")
    if gone == "dead":
        tmux.die(coder.pane_id, 1)
    else:
        tmux.vanish(coder.pane_id)

    def refused(*args: Any, **kwargs: Any) -> fleet_service.SpawnReceipt:
        raise FleetError("tmux refused the window (fake)")

    monkeypatch.setattr(fleet_service, "spawn", refused)
    with pytest.raises(FleetError, match="refused the window"):
        fleet_service.restart(project, "coder-1")

    assert len(_events(project, "agent_exited")) == 1
    assert [typed for typed in tmux.typed if typed[0] == manager.pane_id] == [
        (manager.pane_id, "paste", NUDGE_TEXT),
        (manager.pane_id, "key", "Enter"),
    ]


def test_a_switch_whose_agent_a_restart_resumed_meanwhile_leaves_that_session_s_state_alone(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``switch`` refuses a row whose label passed to a newcomer during its headroom lookup
    (round 1), but it marked the session first and refused in the stop. A newcomer that
    is a ``fleet restart`` resuming the transcript shares the session id: the mark landed
    on its live session, and the take-back wrote the switch's snapshot (``limited``,
    read before the agent exited) over the state the newcomer's own start hook wrote.
    The refusal comes before the mark now."""
    from aisquare.services import claude_accounts as accounts_service

    _two_slots_with_usage(monkeypatch, work=95, personal=10)
    agent = fleet_service.spawn(project, "coder", worktree=False, account="2").agent
    sid = agent.session_id or ""
    transcript = tmp_path / f"{sid}.jsonl"
    transcript.write_text('{"type":"user"}\n', encoding="utf-8")
    _with_transcript(agent, transcript)
    with store_session() as store:
        store.mark_limited(sid, None)  # it hit its limit: the state the switch reads
    real_choose = accounts_service.choose_for_handover
    newcomer: list[FleetAgent] = []

    def choose_while_it_is_restarted(*args: Any, **kwargs: Any) -> Any:
        tmux.die(agent.pane_id, 0)
        newcomer.append(fleet_service.restart(project, agent.label).started)
        with store_session() as store:
            store.touch_session(sid, state="working")  # the resumed session's start hook
        return real_choose(*args, **kwargs)

    monkeypatch.setattr(accounts_service, "choose_for_handover", choose_while_it_is_restarted)

    with pytest.raises(NoSuchAgent, match="another agent now"):
        fleet_service.switch(project, agent.label, reason="session limit")

    [new] = newcomer
    assert new.session_id == sid and _row(new.id).ended_at is None
    with store_session() as store:
        session = store.get_session(sid)
    assert session is not None and session.state == "working"
    assert [typed for typed in tmux.typed if typed[0] == new.pane_id and typed[2] == "/exit"] == []


# --- the launch spec (#144) --------------------------------------------------------------------


def test_spawn_records_the_launch_spec_and_a_restart_replays_it_over_a_changed_config(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """#144: a restart re-read today's config, so a role's permission mode or extra
    arguments edited between runs silently changed what "the same agent" was."""
    _settings(
        monkeypatch,
        roles={"coder": FleetRoleSettings(permission_mode="auto", extra_args=["--effort", "high"])},
    )
    agent = fleet_service.spawn(project, "coder", worktree=False, agent_args=["--verbose"]).agent
    spec = agent.launch_spec
    assert spec is not None
    assert spec.binary == "claude" and spec.permission_mode == "auto" and spec.worktree is False
    assert spec.extra_args == ["--effort", "high", "--verbose"]
    assert spec.command == _command(tmux) and spec.account_slot == agent.account_slot
    with store_session() as store:
        stored = store.get_fleet_agent(agent.id)
    assert stored is not None and stored.launch_spec == spec

    # The config changes under the agent…
    _settings(
        monkeypatch,
        roles={"coder": FleetRoleSettings(permission_mode="acceptEdits", extra_args=["--quiet"])},
    )
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    _with_transcript(agent, transcript)
    tmux.die(agent.pane_id, 1)

    receipt = fleet_service.restart(project, agent.label)

    command = _command(tmux)
    assert _flag(command, "--permission-mode") == "auto", "the recorded mode, not today's"
    assert "--effort" in command and "--verbose" in command and "--quiet" not in command
    assert command.count("--effort") == 1, "the role's arguments are not doubled on replay"
    assert receipt.started.launch_spec is not None
    assert receipt.started.launch_spec.extra_args == ["--effort", "high", "--verbose"]
    assert any("launched as recorded" in note for note in receipt.notes)

    # A fresh spawn in the same role takes the NEW config: the spec is per agent.
    fresh = fleet_service.spawn(project, "coder", worktree=False).agent
    assert fresh.launch_spec is not None
    assert fresh.launch_spec.permission_mode == "acceptEdits"
    assert fresh.launch_spec.extra_args == ["--quiet"]


def test_a_launch_that_passed_no_permission_flag_is_restarted_without_one(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Review of #169: the spec recorded ``""`` (pass no flag) as ``None``, and the
    replay read ``None`` as "not recorded" — so the restart took the role's config
    mode, ``auto``: a permission the agent was never launched with."""
    _settings(monkeypatch, roles={"coder": FleetRoleSettings(permission_mode="auto")})
    agent = fleet_service.spawn(project, "coder", worktree=False, permission_mode="").agent
    assert "--permission-mode" not in _command(tmux)
    assert agent.launch_spec is not None and agent.launch_spec.permission_mode == ""
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    _with_transcript(agent, transcript)
    tmux.die(agent.pane_id, 1)

    receipt = fleet_service.restart(project, agent.label)

    assert "--permission-mode" not in _command(tmux), "no flag then, no flag now"
    assert receipt.started.launch_spec is not None
    assert receipt.started.launch_spec.permission_mode == ""

    # A spec recorded before the fix holds `None` for the same launch: the model
    # says that is "no flag was passed", and the replay honours it.
    legacy = receipt.started.launch_spec.model_copy(update={"permission_mode": None})
    with store_session() as store:
        store.upsert_fleet_agent(receipt.started.model_copy(update={"launch_spec": legacy}))
    tmux.die(receipt.started.pane_id, 1)
    fleet_service.restart(project, agent.label)
    assert "--permission-mode" not in _command(tmux)


def test_a_restart_given_a_permission_mode_changes_the_mode_alone_and_records_it(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The merge of #164 into #169: a restart replays the launch spec, so the role's
    config no longer reaches a running agent, and the way off ``auto`` that #150's
    board line names is ``fleet restart --permission-mode``. The explicit mode wins over
    the spec as it wins over the config — the binary and the arguments are still the
    recorded ones — and the replacement's spec records it. The next bare restart
    replays it, and its receipt does not say the mode was recorded at the agent's first
    spawn, which it no longer was (review of #169, round 1)."""
    _settings(
        monkeypatch,
        roles={"coder": FleetRoleSettings(permission_mode="auto", extra_args=["--effort", "high"])},
    )
    agent = fleet_service.spawn(project, "coder", worktree=False).agent
    _settings(monkeypatch, roles={"coder": FleetRoleSettings(extra_args=["--quiet"])})
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    _with_transcript(agent, transcript)
    tmux.die(agent.pane_id, 1)

    receipt = fleet_service.restart(project, agent.label, permission_mode="acceptEdits")

    command = _command(tmux)
    assert receipt.resumed is True and _flag(command, "--permission-mode") == "acceptEdits"
    assert command.count("--effort") == 1 and "--quiet" not in command, "the rest is replayed"
    assert receipt.started.launch_spec is not None
    assert receipt.started.launch_spec.permission_mode == "acceptEdits"
    assert any("binary and arguments come from the row" in note for note in receipt.notes)
    tmux.die(receipt.started.pane_id, 1)

    again = fleet_service.restart(project, agent.label)

    assert _flag(_command(tmux), "--permission-mode") == "acceptEdits", "the role says auto"
    assert (
        "launched as recorded — binary, permission mode and arguments come from the row, "
        "not from today's config (#144)" in again.notes
    )


def test_a_restart_refuses_a_permission_mode_claude_code_does_not_take_before_stopping_anything(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, runner: CliRunner
) -> None:
    """Review of #169, round 1: ``fleet restart --permission-mode`` took any string. A
    typo stopped the running agent for a replacement Claude Code refuses to start, and
    the replacement's spec recorded it, so every later restart, switch and automatic
    hand-over replayed it. It is refused first; the agent keeps running, on its spec."""
    agent = _coder(project)
    spawned = len(tmux.spawned)

    with pytest.raises(FleetError, match=r"'acceptEdit' is not a Claude Code permission mode"):
        fleet_service.restart(project, agent.label, permission_mode="acceptEdit")
    result = runner.invoke(
        app, ["fleet", "restart", agent.label, "--permission-mode", "Auto", "-P", project.id]
    )

    assert result.exit_code == 1 and "nothing was stopped" in result.output, result.output
    assert agent.pane_id not in tmux.killed and len(tmux.spawned) == spawned
    with store_session() as store:
        [live] = store.fleet_agents(project.id, live_only=True)
    assert live.id == agent.id and live.launch_spec is not None
    assert live.launch_spec.permission_mode == "auto"
    # Every mode the Settings page offers is taken, "" (no flag) included.
    receipt = fleet_service.restart(project, agent.label, permission_mode="")
    assert receipt.started.launch_spec is not None
    assert receipt.started.launch_spec.permission_mode == ""


def test_a_restart_asks_for_the_recorded_binary_not_the_one_the_role_names_today(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The merge of #164 into #169: ``restart`` refuses what ``spawn`` would refuse
    BEFORE it stops anything, and for the binary it asked the role's as it resolves
    today. A replay starts the RECORDED one, so an agent launched on a wrapper that is
    still installed was refused its restart because the role had since been bound to
    one that is not."""
    wrapper = _executable(tmp_path / "wrap" / "claude")
    monkeypatch.setenv("AISQUARE_BIN_CODER", str(wrapper))
    agent = fleet_service.spawn(project, "coder", worktree=False).agent
    assert agent.launch_spec is not None and agent.launch_spec.binary == str(wrapper)
    monkeypatch.setenv("AISQUARE_BIN_CODER", str(tmp_path / "gone" / "claude"))
    tmux.die(agent.pane_id, 1)

    receipt = fleet_service.restart(project, agent.label)

    assert receipt.started.binary == str(wrapper)
    assert _flag(_command(tmux), "--command") == str(wrapper)


def test_a_restart_whose_recorded_binary_left_the_path_resolves_it_again_and_says_so(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Review of #169: the spec's binary was replayed as an override, so once it
    was uninstalled `fleet restart`, `fleet switch` and Restart all refused with
    advice none of them can take (`--bin`) — where a restart before the spec
    re-resolved the binary and resumed."""
    # A wrapper named `claude` (`/opt/wrap/claude`, harness.is_default_agent), so
    # the agent has a session id to resume.
    wrapper = tmp_path / "wrap" / claude_on_path.name  # `claude.cmd` on Windows: PATHEXT
    wrapper.parent.mkdir()
    wrapper.write_text(claude_on_path.read_text(encoding="utf-8"), encoding="utf-8")
    wrapper.chmod(0o755)
    monkeypatch.setenv("AISQUARE_BIN_CODER", str(wrapper))
    agent = fleet_service.spawn(project, "coder", worktree=False).agent
    assert agent.launch_spec is not None and agent.launch_spec.binary == str(wrapper)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    _with_transcript(agent, transcript)
    tmux.die(agent.pane_id, 1)
    wrapper.unlink()
    monkeypatch.delenv("AISQUARE_BIN_CODER")

    receipt = fleet_service.restart(project, agent.label)

    assert receipt.resumed is True and receipt.started.binary == "claude"
    assert "--command" not in _command(tmux), "today's resolution: the default"
    assert any(
        f"{str(wrapper)!r}, the binary it was launched with, is no longer on your PATH" in note
        and "'claude' (chosen by: default)" in note
        for note in receipt.notes
    )
    # Not beside it "the binary comes from the row" (review of #169, F7): it did not.
    assert (
        "launched as recorded — permission mode and arguments come from the row, not from "
        "today's config (#144)" in receipt.notes
    ), receipt.notes
    assert receipt.started.launch_spec is not None
    assert receipt.started.launch_spec.binary == "claude", "the spec records what ran"


def test_session_flags_the_caller_passed_are_not_replayed_by_a_restart(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Review of #169: `--session-id` given to `fleet spawn` was recorded with the
    agent's arguments and replayed beside the restart's own `--resume` (which
    Claude Code refuses without `--fork-session`), or put `--fresh` on the old id."""
    _settings(monkeypatch, roles={"coder": FleetRoleSettings(extra_args=["--effort", "high"])})
    sid = "11111111-2222-3333-4444-555555555555"
    agent = fleet_service.spawn(
        project, "coder", worktree=False, agent_args=["--session-id", sid, "--verbose"]
    ).agent
    assert agent.session_id == sid and _flag(_command(tmux), "--session-id") == sid
    assert agent.launch_spec is not None
    assert agent.launch_spec.extra_args == ["--effort", "high", "--verbose"]
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    _with_transcript(agent, transcript)
    tmux.die(agent.pane_id, 1)

    resumed = fleet_service.restart(project, agent.label)

    command = _command(tmux)
    assert _flag(command, "--resume") == str(transcript) and "--session-id" not in command
    assert "--verbose" in command and command.count("--effort") == 1
    assert resumed.started.session_id == sid

    tmux.die(resumed.started.pane_id, 1)
    fresh = fleet_service.restart(project, agent.label, fresh=True)
    assert fresh.started.session_id not in (None, sid), "--fresh mints a new id"
    assert _flag(_command(tmux), "--session-id") == fresh.started.session_id


def test_the_launch_spec_drops_every_shape_of_a_session_choice() -> None:
    def drop(args: list[str]) -> list[str]:
        return fleet_service._without_session_choice("claude", args)

    assert drop(["--resume", "abc", "--verbose"]) == ["--verbose"]
    assert drop(["-r", "--verbose"]) == ["--verbose"], "a bare --resume (the picker)"
    assert drop(["--resume=abc", "--session-id=x", "--model", "opus"]) == ["--model", "opus"]
    assert drop(["--continue", "-c", "--fork-session", "--verbose"]) == ["--verbose"]
    assert drop(["--session-id"]) == []
    assert drop(["--effort", "high"]) == ["--effort", "high"]
    # Claude Code's flags, not every program's: aider's `-c` is its config file.
    assert fleet_service._without_session_choice("aider", ["-c", "a.yml"]) == ["-c", "a.yml"]
    # Claude Code by another name, known by a session its arguments NAME (the
    # planner joins the row to it), or by a board session its hook joined or a
    # transcript the launch resumes (`claude_code`).
    other = fleet_service._without_session_choice
    assert other("claude2", ["--session-id", "x", "--verbose"]) == ["--verbose"]
    assert other("claude2", ["-r", "abc", "-c"]) == [], "named: every session flag goes"
    assert other("claude2", ["-c", "--verbose"]) == ["-c", "--verbose"], "named nothing"
    assert other("claude2", ["-c", "--verbose"], claude_code=True) == ["--verbose"]


def _executable(path: Path) -> Path:
    """A stand-in agent binary at ``path`` — a script, never the real thing.

    Written by ``fakebin.executable_fake``, so it is ``path.cmd`` on Windows and the
    RETURNED path is the one that exists. Written as a bare ``#!/bin/sh`` file, it
    was no program to PATHEXT: ``shutil.which`` found nothing, and every test here
    failed in ``_launch_binary`` with "'claude2' is not on your PATH" on
    windows-latest. A bare name on PATH (``AISQUARE_BIN_CODER=claude2``) still
    finds it there, through PATHEXT; a full path must be this return value.
    """
    return fakebin.executable_fake(
        path.parent, path.name, windows="set /p line=", posix="read line"
    )


@pytest.mark.parametrize("flag", ["--session-id", "--resume"])
def test_a_session_the_caller_named_is_not_replayed_for_claude_code_by_another_name(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    flag: str,
) -> None:
    """Review of #169, round 2: session flags were left out of the spec only for a
    binary NAMED `claude`. The identity planner reads `--session-id` and `--resume
    <id>` whatever the binary is called, and a restart prepends its own
    `--resume` to any of them — so a `claude2` parallel install spawned on a
    session its caller chose replayed that choice beside the resume, and put
    `--fresh` back on the old id."""
    _executable(claude_on_path.with_name("claude2"))
    monkeypatch.setenv("AISQUARE_BIN_CODER", "claude2")
    sid = "11111111-2222-3333-4444-555555555555"
    agent = fleet_service.spawn(
        project, "coder", worktree=False, agent_args=[flag, sid, "--verbose"]
    ).agent
    assert agent.session_id == sid and _flag(_command(tmux), flag) == sid
    assert agent.launch_spec is not None and agent.launch_spec.extra_args == ["--verbose"]
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    _with_transcript(agent, transcript)
    tmux.die(agent.pane_id, 1)

    resumed = fleet_service.restart(project, agent.label)

    command = _command(tmux)
    assert _flag(command, "--command") == "claude2"
    assert command.count("--resume") == 1 and _flag(command, "--resume") == str(transcript)
    assert "--session-id" not in command and "--verbose" in command
    assert resumed.started.session_id == sid

    tmux.die(resumed.started.pane_id, 1)
    fresh = fleet_service.restart(project, agent.label, fresh=True)
    command = _command(tmux)
    assert "--session-id" not in command and "--resume" not in command
    assert fresh.started.session_id is None, "not the old id: its hook joins it on arrival"


def test_a_spec_that_holds_a_session_choice_does_not_replay_it(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Review of #169, round 2: session choices were filtered only when a spec was
    RECORDED, so a spec written before that — stores at v18 hold them — still
    replayed `--session-id` beside the restart's `--resume`, and one for a
    `claude2` whose session its hook joined replayed its `--continue` there."""
    sid = "11111111-2222-3333-4444-555555555555"
    agent = fleet_service.spawn(
        project, "coder", worktree=False, agent_args=["--session-id", sid, "--verbose"]
    ).agent
    assert agent.launch_spec is not None
    legacy = agent.launch_spec.model_copy(
        update={"extra_args": ["--session-id", sid, "--continue", "--verbose"]}
    )
    with store_session() as store:
        store.upsert_fleet_agent(agent.model_copy(update={"launch_spec": legacy}))
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    _with_transcript(agent, transcript)
    tmux.die(agent.pane_id, 1)

    resumed = fleet_service.restart(project, agent.label)

    command = _command(tmux)
    assert command.count("--resume") == 1 and "--session-id" not in command
    assert "--continue" not in command and "--verbose" in command
    assert resumed.started.launch_spec is not None
    assert resumed.started.launch_spec.extra_args == ["--verbose"]

    # `claude2 -c`: the planner joins nothing, so the spec keeps `-c` (it could
    # be aider's config file) — until the hook joins the row and a restart
    # resumes the transcript, which shows the binary to be Claude Code.
    _executable(claude_on_path.with_name("claude2"))
    monkeypatch.setenv("AISQUARE_BIN_CODER", "claude2")
    other = fleet_service.spawn(project, "coder", worktree=False, agent_args=["-c"]).agent
    assert other.session_id is None
    assert other.launch_spec is not None and other.launch_spec.extra_args == ["-c"]
    joined = "66666666-7777-8888-9999-000000000000"
    with store_session() as store:
        # The hook's join: an unbound row takes the session that first arrives.
        assert store.adopt_fleet_agent_session(other.id, None, joined, datetime.now(tz=UTC))
    _with_transcript(other.model_copy(update={"session_id": joined}), transcript)
    tmux.die(other.pane_id, 1)

    fleet_service.restart(project, other.label)

    command = _command(tmux)
    assert _flag(command, "--resume") == str(transcript)
    assert "-c" not in command and "--continue" not in command


def test_a_reviewer_restarted_from_a_spec_0_7_0_recorded_gets_its_shell_back(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Review of #257, round 18: 0.6.0 and 0.7.0 recorded the reviewer's built-in
    `--restricted` in its spec, and a restart replays the spec, not today's config, so
    every reviewer started before the upgrade came back with no shell, every time."""
    _settings(monkeypatch, roles={"reviewer": FleetRoleSettings(extra_args=["--restricted"])})
    agent = fleet_service.spawn(project, "reviewer", worktree=False).agent
    assert agent.launch_spec is not None and agent.launch_spec.recorded_by == __version__
    legacy = agent.launch_spec.model_copy(update={"recorded_by": None})  # as 0.7.0 wrote it
    with store_session() as store:
        store.upsert_fleet_agent(agent.model_copy(update={"launch_spec": legacy}))
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    _with_transcript(agent, transcript)
    tmux.die(agent.pane_id, 1)
    # The upgrade: loading the config replaced the old default (`config.py`).
    _settings(monkeypatch, roles={"reviewer": FleetRoleSettings(extra_args=list(REVIEWER_ARGS))})

    restarted = fleet_service.restart(project, agent.label).started

    command = _command(tmux)
    assert "--restricted" not in command
    assert REVIEWER_ARGS[0] in command, "today's reviewer arguments in its place"
    assert restarted.launch_spec is not None
    assert restarted.launch_spec.extra_args == list(REVIEWER_ARGS)
    assert restarted.launch_spec.recorded_by == __version__

    # Recorded since, the same list was chosen in today's config, and is replayed.
    _settings(monkeypatch, roles={"reviewer": FleetRoleSettings(extra_args=["--restricted"])})
    chosen = fleet_service.spawn(project, "reviewer", worktree=False).agent
    _with_transcript(chosen, transcript)
    tmux.die(chosen.pane_id, 1)

    fleet_service.restart(project, chosen.label)

    assert "--restricted" in _command(tmux)


@pytest.mark.parametrize(
    ("recorded_by", "replayed"),
    [
        ("0.8.2", ["--model", "opus"]),
        ("0.8.0", ["--model", "opus"]),
        ("0.8.3", [*REVIEWER_ARGS, "--model", "opus"]),
    ],
    ids=["0.8.2", "0.8.0", "since"],
)
def test_a_reviewer_spec_0_8_recorded_replays_without_its_default_deny(
    recorded_by: str, replayed: list[str]
) -> None:
    """Review of #311: 0.8.0 to 0.8.2 recorded the reviewer's default deny first in its
    spec. Replayed as typed, `allow_tools = ["Edit"]` could never hand Edit back; the deny
    is the role's own now, so `launch` still denies the editing tools without it."""
    spec = LaunchSpec(
        binary="claude", extra_args=[*REVIEWER_ARGS, "--model", "opus"], recorded_by=recorded_by
    )
    assert fleet_service._replayed_args("reviewer", spec, FleetSettings()) == replayed
    assert fleet_service._replayed_args("coder", spec, FleetSettings()) == spec.extra_args
    fleet = FleetSettings()
    fleet.roles["reviewer"] = fleet.roles["reviewer"].model_copy(update={"allow_tools": ["Edit"]})
    policy = harness.tool_policy("reviewer", binary="claude", args=replayed, fleet=fleet)
    assert ("Edit" in policy.denied) is (recorded_by == "0.8.3")
    assert {"Write", "NotebookEdit"} <= set(policy.denied)


@pytest.mark.parametrize(
    "today", [[], ["--model", "opus"]], ids=["every-tool", "model-chosen-since"]
)
@pytest.mark.parametrize("caller", [[], ["--add-dir", "/srv"]], ids=["alone", "then-callers"])
def test_a_0_7_0_reviewer_is_restarted_with_the_arguments_today_s_config_gives_the_role(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    today: list[str],
    caller: list[str],
) -> None:
    """Round 21 of #257: the `--restricted` a 0.7.0 spec leads with was replaced by the
    built-in `REVIEWER_ARGS` whatever today's config gave the role, so a reviewer its
    operator since gave `--model opus` came back without the model. Today's arguments are
    replayed in its place. Where they do not deny the editing tools the denial goes beside
    them, since the spec cannot tell 0.7.0's default from a `--restricted` its caller gave
    (round 22): that is all a restart adds to a fresh spawn's."""
    _settings(monkeypatch, roles={"reviewer": FleetRoleSettings(extra_args=["--restricted"])})
    agent = fleet_service.spawn(project, "reviewer", worktree=False, agent_args=caller).agent
    assert agent.launch_spec is not None
    legacy = agent.launch_spec.model_copy(update={"recorded_by": None})  # as 0.7.0 wrote it
    with store_session() as store:
        store.upsert_fleet_agent(agent.model_copy(update={"launch_spec": legacy}))
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    _with_transcript(agent, transcript)
    tmux.die(agent.pane_id, 1)
    _settings(monkeypatch, roles={"reviewer": FleetRoleSettings(extra_args=today)})

    restarted = fleet_service.restart(project, agent.label).started

    assert restarted.launch_spec is not None
    assert restarted.launch_spec.extra_args == [*today, *REVIEWER_ARGS, *caller]
    command = _command(tmux)
    assert "--restricted" not in command
    assert REVIEWER_ARGS[0] in command, "without the editing tools, whoever chose --restricted"
    fresh = fleet_service.spawn(project, "reviewer", worktree=False, agent_args=caller).agent
    assert fresh.launch_spec is not None
    assert fresh.launch_spec.extra_args == [*today, *caller], "a fresh spawn: today's alone"


def test_a_0_7_0_reviewer_its_caller_locked_down_is_never_restarted_with_every_tool(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Round 22 of #257: under 0.7.0, with `extra_args = []`, `fleet spawn reviewer --
    --restricted` recorded `["--restricted"]`, as the default did. Replaced by an empty
    `today` alone, that reviewer of someone else's PR came back with Bash, Edit and Write.
    The control: today's config denying the tools already, it is replayed as today's."""
    _settings(monkeypatch, roles={"reviewer": FleetRoleSettings(extra_args=[])})
    agent = fleet_service.spawn(
        project, "reviewer", worktree=False, agent_args=["--restricted"]
    ).agent
    assert agent.launch_spec is not None and agent.launch_spec.extra_args == ["--restricted"]
    legacy = agent.launch_spec.model_copy(update={"recorded_by": None})  # as 0.7.0 wrote it
    with store_session() as store:
        store.upsert_fleet_agent(agent.model_copy(update={"launch_spec": legacy}))
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    _with_transcript(agent, transcript)
    tmux.die(agent.pane_id, 1)

    restarted = fleet_service.restart(project, agent.label).started

    assert restarted.launch_spec is not None
    assert restarted.launch_spec.extra_args == list(REVIEWER_ARGS)
    assert REVIEWER_ARGS[0] in _command(tmux), "no Edit or Write"
    denied = [*REVIEWER_ARGS, "--model", "opus"]
    assert replacing_the_former_reviewer_default_first(["--restricted"], denied) == denied, (
        "control: no second denial beside today's"
    )


@pytest.mark.parametrize(
    ("today", "denies"),
    [
        (["--disallowedTools", "Edit Write NotebookEdit"], True),
        (["--disallowedTools", "Edit", "Write", "NotebookEdit", "--model", "opus"], True),
        (["--disallowedTools=Edit,Write,NotebookEdit"], True),
        (["--disallowed-tools", "NotebookEdit, Write ,Edit"], True),
        (["--disallowedTools=Edit Write NotebookEdit WebFetch"], True),
        (["--disallowedTools=Edit Write"], False),
        (["--disallowedTools", "Edit", "--model", "Write", "NotebookEdit"], False),
        (["--disallowedTools=Edit(src/**) Write NotebookEdit"], False),
        (["--allowedTools=Edit Write NotebookEdit"], False),
    ],
    ids=[
        "two-words",
        "words-then-option",
        "commas",
        "kebab-flag",
        "wider",
        "narrower",
        "words-end-at-an-option",
        "edit-in-part",
        "allowed-not-denied",
    ],
)
def test_a_0_7_0_reviewer_gets_one_denial_however_todays_config_spells_it(
    today: list[str], denies: bool
) -> None:
    """Round 23 of #257: today's config was taken to deny the editing tools only when it
    held `REVIEWER_ARGS`'s one token, so a list spelled another way, or one tool wider, got
    a second `--disallowedTools` beside it, and what the reviewer was denied rested on how
    Claude Code combines the two. Read off the tools denied instead; a list that leaves one
    of them out still gets the denial beside it."""
    replayed = replacing_the_former_reviewer_default_first(["--restricted", "--x"], today)

    assert replayed == ([*today, "--x"] if denies else [*today, *REVIEWER_ARGS, "--x"])


@pytest.mark.parametrize(
    ("role_args", "caller", "replayed"),
    [
        (["--restricted"], ["--model", "opus"], [*REVIEWER_ARGS, "--model", "opus"]),
        ([], ["--model", "opus", "--restricted"], ["--model", "opus", "--restricted"]),
    ],
    ids=["role-default-then-callers", "callers-own-restricted"],
)
def test_a_0_7_0_reviewer_spawned_with_arguments_of_its_own_gets_its_shell_back(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    role_args: list[str],
    caller: list[str],
    replayed: list[str],
) -> None:
    """Round 19 of #257: a spec records the role's arguments, then the caller's, so 0.7.0's
    `fleet spawn reviewer -- --model opus` recorded `--restricted --model opus`, and only a
    spec of exactly `--restricted` was given today's arguments: that reviewer still came
    back with no shell. The default leads the list; a `--restricted` the caller passed
    after other arguments of its own is theirs, and is kept (the control)."""
    _settings(monkeypatch, roles={"reviewer": FleetRoleSettings(extra_args=role_args)})
    agent = fleet_service.spawn(project, "reviewer", worktree=False, agent_args=caller).agent
    assert agent.launch_spec is not None
    assert agent.launch_spec.extra_args == [*role_args, *caller], "the role's, then the caller's"
    legacy = agent.launch_spec.model_copy(update={"recorded_by": None})  # as 0.7.0 wrote it
    with store_session() as store:
        store.upsert_fleet_agent(agent.model_copy(update={"launch_spec": legacy}))
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    _with_transcript(agent, transcript)
    tmux.die(agent.pane_id, 1)
    # The upgrade: loading the config replaced the old default (`config.py`).
    migrated = replacing_the_former_reviewer_default(role_args)
    _settings(monkeypatch, roles={"reviewer": FleetRoleSettings(extra_args=migrated)})

    restarted = fleet_service.restart(project, agent.label).started

    assert restarted.launch_spec is not None
    assert restarted.launch_spec.extra_args == replayed
    command = _command(tmux)
    assert _flag(command, "--model") == "opus", "the caller's own arguments are kept"
    assert ("--restricted" in command) == ("--restricted" in replayed)


@pytest.mark.parametrize(
    "caller", [[], ["--add-dir", "/srv"]], ids=["config-alone", "config-then-callers"]
)
def test_a_0_7_0_reviewer_whose_config_chose_restricted_is_restarted_without_a_shell(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caller: list[str],
) -> None:
    """Round 20 of #257: a 0.7.0 config of `["--restricted", "--model", "opus"]` was written
    by its operator, and loads as written, but the reviewer it spawned recorded the same
    list the default and the caller's own `--model opus` did. The restart replaced the
    leading `--restricted`, so that reviewer, kept from the shell by its config, got one
    back, while a fresh spawn from the same config did not. Today's config still leading
    with the flag says it was chosen."""
    chosen = ["--restricted", "--model", "opus"]
    assert replacing_the_former_reviewer_default(chosen) == chosen, "loading keeps it"
    _settings(monkeypatch, roles={"reviewer": FleetRoleSettings(extra_args=chosen)})
    agent = fleet_service.spawn(project, "reviewer", worktree=False, agent_args=caller).agent
    assert agent.launch_spec is not None
    legacy = agent.launch_spec.model_copy(update={"recorded_by": None})  # as 0.7.0 wrote it
    with store_session() as store:
        store.upsert_fleet_agent(agent.model_copy(update={"launch_spec": legacy}))
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    _with_transcript(agent, transcript)
    tmux.die(agent.pane_id, 1)

    restarted = fleet_service.restart(project, agent.label).started

    assert restarted.launch_spec is not None
    assert restarted.launch_spec.extra_args == [*chosen, *caller]
    command = _command(tmux)
    assert "--restricted" in command
    assert REVIEWER_ARGS[0] not in command
    fresh = fleet_service.spawn(project, "reviewer", worktree=False, agent_args=caller).agent
    assert fresh.launch_spec is not None
    assert fresh.launch_spec.extra_args == restarted.launch_spec.extra_args, "as a fresh spawn"


def test_a_replay_does_not_fall_back_onto_another_kind_of_program(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Review of #169, round 2: the fallback for a recorded binary that left the
    PATH took whatever the role resolves to today — and replayed the old
    program's arguments to it: aider's `-c a.yml` became Claude Code's
    `--continue` with the prompt `a.yml`, and a Claude session was `--resume`d
    on aider. `resolve_binary`: "the WRONG AGENT under the right role name —
    worse than not launching"."""
    aider = _executable(tmp_path / "abin" / "aider")
    monkeypatch.setenv("AISQUARE_BIN_CODER", str(aider))
    agent = fleet_service.spawn(project, "coder", worktree=False, agent_args=["-c", "a.yml"]).agent
    assert agent.launch_spec is not None and agent.launch_spec.extra_args == ["-c", "a.yml"]
    tmux.die(agent.pane_id, 1)
    aider.unlink()
    monkeypatch.delenv("AISQUARE_BIN_CODER")
    started = len(tmux.spawned)

    with pytest.raises(FleetError) as refused:
        fleet_service.restart(project, agent.label)

    message = str(refused.value)
    assert f"{str(aider)!r}, the binary it was launched with, is no longer on your PATH" in message
    assert "'claude' today (chosen by: default)" in message
    assert f"`{_fleet('spawn', project, 'coder')}`" in message and "--bin" not in message
    assert len(tmux.spawned) == started, "nothing was started"

    # The other way round: a Claude Code session is not resumed on aider.
    wrapper = _executable(tmp_path / "wrap" / "claude")
    monkeypatch.setenv("AISQUARE_BIN_CODER", str(wrapper))
    claude = fleet_service.spawn(project, "coder", worktree=False).agent
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    _with_transcript(claude, transcript)
    tmux.die(claude.pane_id, 1)
    wrapper.unlink()
    monkeypatch.setenv("AISQUARE_BIN_CODER", str(_executable(tmp_path / "abin" / "aider")))
    started = len(tmux.spawned)

    with pytest.raises(FleetError, match="not known to be the same kind of program"):
        fleet_service.restart(project, claude.label)
    assert len(tmux.spawned) == started


def test_a_removed_claude_code_by_another_name_comes_back_on_claude_only_when_known(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The fallback's other half: `claude2` is not Claude Code by name, but a
    board session its hook joined is — only Claude Code's hooks write one — so
    the round-1 case (a removed `AISQUARE_BIN_CODER=claude2`) still resumes on
    `claude`, and (review of #169, round 3) a `--fresh` restart of it starts on
    `claude` too. A `claude2` with nothing to show what it was is refused, with
    a way out, however it is restarted."""
    claude2 = _executable(claude_on_path.with_name("claude2"))
    monkeypatch.setenv("AISQUARE_BIN_CODER", "claude2")
    unknown = fleet_service.spawn(project, "coder", worktree=False).agent
    assert unknown.session_id is None, "no id is minted for a binary not named claude"
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    joined: list[FleetAgent] = []
    for sid in ("11111111-2222-3333-4444-555555555555", "66666666-7777-8888-9999-000000000000"):
        agent = fleet_service.spawn(
            project, "coder", worktree=False, agent_args=["--session-id", sid]
        ).agent
        _with_transcript(agent, transcript)
        joined.append(agent)
    for agent in (unknown, *joined):
        tmux.die(agent.pane_id, 1)
    claude2.unlink()
    monkeypatch.delenv("AISQUARE_BIN_CODER")
    started = len(tmux.spawned)

    for fresh in (True, False):
        with pytest.raises(FleetError, match=re.escape(f"`{_fleet('spawn', project, 'coder')}`")):
            fleet_service.restart(project, unknown.label, fresh=fresh)
    assert len(tmux.spawned) == started
    resumed = fleet_service.restart(project, joined[0].label)
    assert resumed.resumed is True and resumed.started.binary == "claude"
    assert resumed.started.session_id == joined[0].session_id
    renewed = fleet_service.restart(project, joined[1].label, fresh=True)
    assert renewed.resumed is False and renewed.started.binary == "claude"
    assert "--resume" not in _command(tmux)
    for receipt in (resumed, renewed):
        assert any("'claude2', the binary it was launched with" in note for note in receipt.notes)


def test_a_replay_whose_fallback_is_missing_too_names_a_way_out_a_restart_has(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Review of #169, round 2: with the fallback missing as well, the refusal
    still said "pass --bin" — which `fleet restart`, `fleet switch` and Restart
    do not take — and lost the binary the agent was launched with."""
    wrapper = _executable(tmp_path / "wrap" / "claude")
    monkeypatch.setenv("AISQUARE_BIN_CODER", str(wrapper))
    agent = fleet_service.spawn(project, "coder", worktree=False).agent
    tmux.die(agent.pane_id, 1)
    wrapper.unlink()
    elsewhere = tmp_path / "elsewhere" / "claude"
    monkeypatch.setenv("AISQUARE_BIN_CODER", str(elsewhere))

    with pytest.raises(FleetError) as refused:
        fleet_service.restart(project, agent.label)

    message = str(refused.value)
    assert f"{str(wrapper)!r}, the binary it was launched with, is no longer" in message
    assert f"neither is {str(elsewhere)!r}" in message and "(chosen by: env)" in message
    assert "--bin" not in message

    # Still bound to the binary that is gone: said as such, not as two binaries.
    monkeypatch.setenv("AISQUARE_BIN_CODER", str(wrapper))
    with pytest.raises(FleetError, match="the role still resolves to it") as refused:
        fleet_service.restart(project, agent.label)
    assert "--bin" not in str(refused.value)


def test_a_replay_falls_back_only_onto_the_same_program(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Review of #169, round 3: "the same kind of program" had two classes,
    Claude Code and everything else, so any two programs that are both not
    Claude Code passed as one — aider's `-c a.yml` was replayed to codex, and
    a joined `claude2`'s `--model opus` and a hand-off prompt went to aider.
    Two programs that are not both Claude Code are the same only by name."""
    aider = _executable(tmp_path / "abin" / "aider")
    monkeypatch.setenv("AISQUARE_BIN_CODER", str(aider))
    agent = fleet_service.spawn(project, "coder", worktree=False, agent_args=["-c", "a.yml"]).agent
    tmux.die(agent.pane_id, 1)
    aider.unlink()
    monkeypatch.setenv("AISQUARE_BIN_CODER", str(_executable(tmp_path / "cbin" / "codex")))
    started = len(tmux.spawned)

    with pytest.raises(FleetError, match="not known to be the same kind of program"):
        fleet_service.restart(project, agent.label)
    assert len(tmux.spawned) == started, "nothing was started"

    # The same program somewhere else is the same program, and gets its arguments.
    moved = _executable(tmp_path / "elsewhere" / "aider")
    monkeypatch.setenv("AISQUARE_BIN_CODER", str(moved))
    receipt = fleet_service.restart(project, agent.label)
    command = _command(tmux)
    assert _flag(command, "--command") == str(moved) and command[-2:] == ["-c", "a.yml"]
    assert any(f"{str(aider)!r}, the binary it was launched with" in n for n in receipt.notes)

    # A `claude2` its hook joined is Claude Code, which aider is not — `--fresh` or resumed.
    claude2 = _executable(claude_on_path.with_name("claude2"))
    monkeypatch.setenv("AISQUARE_BIN_CODER", "claude2")
    sid = "11111111-2222-3333-4444-555555555555"
    other = fleet_service.spawn(
        project, "coder", worktree=False, agent_args=["--session-id", sid, "--model", "opus"]
    ).agent
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    _with_transcript(other, transcript)
    tmux.die(other.pane_id, 1)
    claude2.unlink()
    monkeypatch.setenv("AISQUARE_BIN_CODER", str(moved))
    started = len(tmux.spawned)

    for fresh in (True, False):
        with pytest.raises(FleetError, match="not known to be the same kind of program"):
            fleet_service.restart(project, other.label, fresh=fresh)
    assert len(tmux.spawned) == started


def test_a_fresh_restart_of_claude_code_by_another_name_does_not_continue_its_session(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Review of #169, round 3: a `claude2` spawned with `-c` keeps it in its spec
    (nothing yet shows it to be Claude Code's `--continue`), and its hook joins
    the row to the session it continued. A `--fresh` restart replayed the `-c`,
    so the replacement continued the old conversation and the hand-off prompt
    was typed into it. The board session shows the binary to be Claude Code,
    resumed or not, and the `-c` goes."""
    _executable(claude_on_path.with_name("claude2"))
    monkeypatch.setenv("AISQUARE_BIN_CODER", "claude2")
    agent = fleet_service.spawn(
        project, "coder", worktree=False, agent_args=["-c", "--verbose"]
    ).agent
    assert agent.launch_spec is not None and agent.launch_spec.extra_args == ["-c", "--verbose"]
    joined = "66666666-7777-8888-9999-000000000000"
    with store_session() as store:
        # The hook's join: an unbound row takes the session that first arrives.
        assert store.adopt_fleet_agent_session(agent.id, None, joined, datetime.now(tz=UTC))
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    _with_transcript(agent.model_copy(update={"session_id": joined}), transcript)
    tmux.die(agent.pane_id, 1)

    receipt = fleet_service.restart(project, agent.label, fresh=True)

    command = _command(tmux)
    assert receipt.resumed is False and _flag(command, "--command") == "claude2"
    assert "-c" not in command and "--continue" not in command and "--resume" not in command
    assert "--verbose" in command
    assert receipt.started.launch_spec is not None
    assert receipt.started.launch_spec.extra_args == ["--verbose"]


def test_a_replay_that_cannot_start_leaves_a_running_agent_running(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Review of #169, round 3: `restart` and `switch` stop a running agent and
    only then spawn, so a replay the fallback refuses stopped the agent and
    started nothing — and the refusal did not say it had been stopped. It is
    refused before the stop now, and the agent keeps running."""
    _two_slots_with_usage(monkeypatch, work=95, personal=10)
    aider = _executable(tmp_path / "abin" / "aider")
    monkeypatch.setenv("AISQUARE_BIN_CODER", str(aider))
    agent = fleet_service.spawn(project, "coder", worktree=False, account="2").agent
    aider.unlink()
    monkeypatch.delenv("AISQUARE_BIN_CODER")
    started = len(tmux.spawned)

    with pytest.raises(FleetError, match="not known to be the same kind of program"):
        fleet_service.restart(project, agent.label)
    with pytest.raises(FleetError, match="not known to be the same kind of program"):
        fleet_service.switch(project, agent.label)

    assert tmux.typed == [] and tmux.killed == [], "no /exit typed, no window killed"
    assert len(tmux.spawned) == started
    with store_session() as store:
        live = store.fleet_agent_by_label(project.id, agent.label, live_only=True)
    assert live is not None and live.id == agent.id


def test_a_replay_refused_inside_the_replacements_spawn_names_no_bin_either(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Review of #169, round 2: the check before the stop gave ``bin_flag=False``, and
    the replacement's own ``spawn`` did not. A binary that left the PATH after that
    check (a relink during the stop's grace, a binding edited meanwhile) was refused
    there with "pass --bin", which neither ``restart`` nor ``switch`` takes."""
    agent = _coder(project)
    with store_session() as store:
        store.upsert_fleet_agent(agent.model_copy(update={"launch_spec": None}))
    tmux.die(agent.pane_id, 1)
    # The binary goes after the check before the stop has passed.
    monkeypatch.setattr(
        fleet_service, "_refuse_a_replay_that_cannot_start", lambda agent, session, project: None
    )
    monkeypatch.setenv("AISQUARE_BIN_CODER", str(tmp_path / "gone" / "claude"))

    with pytest.raises(FleetError, match="is not on your PATH") as refused:
        fleet_service.restart(project, agent.label)

    assert "--bin" not in str(refused.value), str(refused.value)
    assert "install it, or change the role's binding" in str(refused.value)


def test_a_row_spawned_before_the_launch_spec_is_refused_its_replay_before_the_stop(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Review of #169, round 1: a row spawned before v18 has no launch spec, so its
    replay starts the role's binary as the role resolves it today. With that binary
    gone, the check before the stop is the one thing between the operator and an
    agent stopped for a replacement that never starts — and no test asked it. It is
    `spawn`'s own choice (`_launch_binary`), asked by `restart` and by `switch`
    before either marks the session or types `/exit`, and its refusal names only
    ways out a restart has: neither takes `--bin`."""
    _two_slots_with_usage(monkeypatch, work=95, personal=10)
    agent = fleet_service.spawn(project, "coder", worktree=False, account="2").agent
    _with_transcript(agent, None)
    with store_session() as store:
        store.upsert_fleet_agent(agent.model_copy(update={"launch_spec": None}))
    gone = tmp_path / "gone" / "claude"
    monkeypatch.setenv("AISQUARE_BIN_CODER", str(gone))
    started = len(tmux.spawned)
    refusal = re.escape(
        f"{str(gone)!r} is not on your PATH (chosen by: env) — install it, or change the "
        "role's binding"
    )

    with pytest.raises(FleetError, match=f"^cannot restart {agent.label!r}: {refusal}$"):
        fleet_service.restart(project, agent.label)
    with pytest.raises(FleetError, match=f"^{refusal}$"):
        fleet_service.switch(project, agent.label)

    assert tmux.typed == [] and tmux.killed == [], "no /exit typed, no window killed"
    assert len(tmux.spawned) == started
    with store_session() as store:
        live = store.fleet_agent_by_label(project.id, agent.label, live_only=True)
        session = store.get_session(agent.session_id or "")
    assert live is not None and live.id == agent.id and live.launch_spec is None
    assert session is not None and session.state == "working", "the session is not marked"


# --- the final review of #203 ------------------------------------------------------------------


@pytest.mark.parametrize("act", ["stop", "shutdown --force", "restart"])
def test_a_row_that_outlived_its_server_never_acts_on_another_projects_pane(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    plain_project: ProjectInfo,
    act: str,
) -> None:
    """Review of #203, final round, FLEET-1: a pane id is unique for one SERVER's
    lifetime. A reboot or a hand-run `kill-server` leaves A's rows live, and B's first
    spawn starts a fresh server that numbers its panes from the start again, so B's
    manager took the id A's manager had. Asked about by id, B's pane answered for A's
    row: A's dead manager listed as `waiting`, a board write in A nudged B's manager,
    and stopping, shutting down or restarting A's manager typed `/exit` into B's and
    killed its window. A server that started after the row was written holds none of
    the row's panes: the row is lost, and ending it touches nothing of B's."""
    a_manager = fleet_service.spawn(project, "manager").agent
    _board_session(a_manager, "waiting")
    tmux.kill_server()
    tmux._counter = 0  # the next server numbers from the start again
    b_manager = fleet_service.spawn(plain_project, "manager").agent
    assert b_manager.pane_id == a_manager.pane_id  # the shape under test
    tmux.set_command(b_manager.pane_id, "claude")  # B's agent is up and reads input

    [listed] = fleet_service.list_agents(project)
    assert (listed.agent.id, listed.state, listed.detail) == (a_manager.id, "lost", "pane gone")
    assert fleet_service.nudge_manager(project.id, reason="a board write in A") is False

    if act == "stop":
        ended = fleet_service.stop(project, "manager").agent
        assert ended.id == a_manager.id and ended.ended_at is not None
    elif act == "shutdown --force":
        report = fleet_service.shutdown(project, force=True)
        assert [row.id for row in report.stopped] == [a_manager.id] and report.failed == []
    else:
        restarted = fleet_service.restart(project, "manager")
        assert restarted.replaced.id == a_manager.id and not restarted.was_running
        assert restarted.started.pane_id != b_manager.pane_id

    assert [typed for typed in tmux.typed if typed[0] == b_manager.pane_id] == []
    assert b_manager.pane_id not in tmux.killed and b_manager.pane_id in tmux.facts
    [b_listed] = fleet_service.list_agents(plain_project)
    assert (b_listed.agent.id, b_listed.state) == (b_manager.id, "waiting")


def test_a_row_that_outlived_its_server_is_not_its_own_projects_newer_agent(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """The same reuse inside ONE project reaches the row through its own session's
    window list, before any lookup by id alone: after the restart of the server, the
    project's next spawn (coder-2, as the stale coder-1 still holds its label) took the
    stale manager's id in the project's own session, and the manager's restart typed
    its `/exit` into coder-2. Neither name tmux keeps tells them apart — a relabelled
    window keeps its first name — the server's start does."""
    manager = fleet_service.spawn(project, "manager").agent
    coder = _coder(project)
    tmux.kill_server()
    tmux._counter = 0
    newer = _coder(project)
    assert newer.label == "coder-2" and newer.pane_id == manager.pane_id

    states = {s.agent.label: s.state for s in fleet_service.list_agents(project)}
    assert states == {"manager": "lost", "coder-1": "lost", "coder-2": "waiting"}

    restarted = fleet_service.restart(project, "manager")
    assert restarted.replaced.id == manager.id and not restarted.was_running
    # The replacement takes the stale coder-1's id in turn — and is not coder-1's either.
    assert restarted.started.pane_id == coder.pane_id
    fleet_service.stop(project, "coder-1")

    touched = {pane for pane, _, _ in tmux.typed} | set(tmux.killed)
    assert newer.pane_id not in touched, "coder-2 was never typed into nor killed"
    assert coder.pane_id not in tmux.killed, "the new manager was not stopped for coder-1"
    states = {s.agent.label: s.state for s in fleet_service.list_agents(project)}
    assert states == {"coder-2": "waiting", "manager": "waiting"}


@pytest.mark.parametrize(
    ("verb", "since"),
    [("restart", "its task closed"), ("switch", "its task closed"), ("restart", "fleet rename")],
)
def test_a_replacement_keeps_its_agents_worktree_as_it_stands(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    verb: str,
    since: str,
) -> None:
    """Review of #203, final round, FLEET-2: ``restart`` and ``switch`` rebuilt a
    worktree agent's branch from its row's task and the codename, and neither is what
    it was at the spawn: rule 3 forgets a task once it closes, and ``fleet rename``
    changes the codename. The replacement then asked for another branch — a tree
    holding uncommitted work was refused AFTER the agent had been stopped (lost, its
    claims released), and a clean one was moved to a branch the agent never worked
    on. The replacement is the same agent carrying on, in its tree as it stands."""
    _two_slots_with_usage(monkeypatch, work=95, personal=10)
    task = _add_task(project, "Wire the auth flow")
    coder = fleet_service.spawn(project, "coder", task_id=task.id, account="2").agent
    assert coder.worktree
    branch = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=coder.cwd)
    dirty = since == "its task closed"
    if dirty:
        with store_session() as store:
            store.set_task_status(task.id, "done")
            assert store.retire_fleet_assignments(task.id) == 1
        (coder.cwd / "wip.txt").write_text("half a change\n", encoding="utf-8")
    else:
        fleet_service.rename(project, "quiet-heron")

    if verb == "restart":
        started = fleet_service.restart(project, coder.label).started
    else:
        started = fleet_service.switch(project, coder.label).started

    assert started.label == coder.label and started.cwd == coder.cwd
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=coder.cwd) == branch
    assert (coder.cwd / "wip.txt").exists() is dirty, "the work is where the agent left it"
    live = fleet_service.list_agents(project)
    assert [status.agent.id for status in live] == [started.id]


def test_a_switch_whose_task_closes_during_the_stop_starts_the_replacement_without_it(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #203, final round, FLEET-3: ``switch`` started the replacement from the
    row it read before the headroom lookup and the stop's grace. A task closed in that
    window is forgotten by the live row (rule 3), but the snapshot still named it, so
    ``spawn`` refused it — "task … is dropped" — with the agent already stopped, and
    nothing started. The replacement comes from the row the stop ended, as ``restart``'s
    does, and its hand-off prompt no longer sets it on the closed task."""
    _two_slots_with_usage(monkeypatch, work=95, personal=10)
    task = _add_task(project, "Ship auth")
    agent = fleet_service.spawn(
        project, "coder", worktree=False, task_id=task.id, account="2"
    ).agent
    _with_transcript(agent, None)  # nothing to resume: a hand-off prompt
    original_spawn = tmux.spawn_window

    def ready_spawn(*args: Any, **kwargs: Any) -> WindowInfo:
        window = original_spawn(*args, **kwargs)
        tmux.set_command(window.pane_id, "claude")
        return window

    real_kill = tmux.kill_window

    def dropped_in_the_grace(pane_id: str) -> None:
        if pane_id == agent.pane_id:
            team_service.drop_task(task.id)  # the manager drops it meanwhile
        real_kill(pane_id)

    monkeypatch.setattr(tmux, "spawn_window", ready_spawn)
    monkeypatch.setattr(tmux, "kill_window", dropped_in_the_grace)

    receipt = fleet_service.switch(project, agent.label)

    assert receipt.stopped.task_id is None and receipt.started.task_id is None
    [prompt] = _first_lines(tmux, receipt.started.pane_id)
    assert "Ship auth" not in prompt and task.id not in prompt
    live = fleet_service.list_agents(project)
    assert [status.agent.id for status in live] == [receipt.started.id]


@pytest.mark.parametrize("refused", ["a closed task", "an unknown role"])
def test_a_switch_that_spawn_would_refuse_is_refused_before_the_stop(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    refused: str,
) -> None:
    """``restart`` asks spawn's refusals for the role and the task before it stops
    anything; ``switch`` asked neither, and ``spawn`` gave them after the stop, so the
    agent was stopped for a replacement that never started. A row spawned before rule
    3 names its closed task until its next briefing, so switching one always lost it
    (review of #203, final round, FLEET-3)."""
    _two_slots_with_usage(monkeypatch, work=95, personal=10)
    task = _add_task(project, "Ship auth")
    agent = fleet_service.spawn(
        project, "coder", worktree=False, task_id=task.id, account="2"
    ).agent
    _with_transcript(agent, None)
    if refused == "a closed task":
        with store_session() as store:
            store.set_task_status(task.id, "done")  # no rule 3: the row still names it
        why = f"task {task.id} is done"
    else:
        # A role whose declaration left the config after the agent started.
        monkeypatch.setattr(fleet_service, "_role_ok", lambda role: False)
        why = "unknown role 'coder'"

    with pytest.raises(FleetError, match=why):
        fleet_service.switch(project, agent.label)

    assert tmux.typed == [] and tmux.killed == [], "no /exit typed, no window killed"
    assert len(tmux.spawned) == 1
    with store_session() as store:
        live = store.fleet_agent_by_label(project.id, agent.label, live_only=True)
    assert live is not None and live.id == agent.id
    assert _session_state(agent.session_id or "") == "working", "the session is not marked"


@pytest.mark.parametrize("verb", ["restart", "switch"])
def test_a_restart_or_a_switch_is_refused_a_freed_socket_before_it_stops_the_agent(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    verb: str,
) -> None:
    """Review of #259, round 2: the freed-socket refusal ran only inside the spawn, after
    `stop(..., handover=True)` had typed `/exit`. With this shell's socket file taken from
    under the server coder-1 runs on, an agent whose own server listens at another path
    (another `TMUX_TMPDIR`) stopped as asked, and its replacement was refused: the agent
    was left down, and a switch announced that its replacement did not start. Refused
    now before anything is stopped, as the replay's other refusals are."""
    _two_slots_with_usage(monkeypatch, work=95, personal=10)
    monkeypatch.setattr(tmux_core, "boot_id", lambda: "b00t-1")
    stranded = _coder(project)  # on this shell's socket path
    apart = Path("/elsewhere/tmux-1000/asq-test")
    elsewhere = tmux.per_path[apart] = FakeTmux()
    window = elsewhere.spawn_window("asq-x", name="coder-2", cwd=project.root, command=["c"])
    elsewhere.set_command(window.pane_id, "claude")
    spawned = fleet_service.spawn(project, "coder", worktree=False, account="2").agent
    moved = {"socket_path": apart, "pane_id": window.pane_id}
    with store_session() as store:
        agent = store.upsert_fleet_agent(spawned.model_copy(update=moved))
    _with_transcript(agent, None)
    tmux.running = False  # no tmux client reaches coder-1's server…
    tmux.socket_file = False  # …because its socket file is gone, not because it exited
    started = len(tmux.spawned)

    with pytest.raises(FleetError, match=r"not started: .* lost its socket file"):
        getattr(fleet_service, verb)(project, agent.label)

    assert elsewhere.typed == [] and elsewhere.killed == [], "no /exit typed, nothing killed"
    assert len(tmux.spawned) == started
    with store_session() as store:
        live = {row.id for row in store.fleet_agents(project.id, live_only=True)}
    assert live == {stranded.id, agent.id}, "both still live"
    assert _session_state(agent.session_id or "") == "working", "the session is not marked"


@pytest.mark.parametrize("verb", ["switch", "restart"])
def test_a_replacement_whose_prompt_poll_tmux_will_not_answer_keeps_the_claims(
    tmux: FakeTmux,
    typed_first_lines: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    verb: str,
) -> None:
    """Review of #203, final round, FLEET-4: ``_type_prompt`` polls the new pane with
    ``pane_facts``, which raises on a wedged server's timeout, and it runs after the
    replacement's row is recorded. ``switch`` and ``restart`` read any raise out of
    ``spawn`` as "no replacement started": a resumed replacement that was up on the
    same session had its task put back in the pool, its exit announced and the manager
    woken for a second worker, and the TmuxError went on to the caller (a traceback on
    the CLI; the automatic worker died without a note). The poll is a note now, like
    the paste it guards, and the hand-over completes."""
    _two_slots_with_usage(monkeypatch, work=95, personal=10)
    agent = fleet_service.spawn(project, "coder", worktree=False, account="2").agent
    transcript = tmp_path / f"{agent.session_id}.jsonl"
    transcript.write_text('{"type":"user"}\n', encoding="utf-8")
    _with_transcript(agent, transcript)
    task = _add_task(project, "Keep it")
    assert team_service.claim_task(task.id, session_ref=agent.session_id or "").status == "doing"
    before = set(tmux.facts)
    real_facts = tmux.pane_facts

    def wedged_for_the_newcomer(pane_id: str) -> PaneFacts | None:
        if pane_id not in before:
            raise TmuxError("tmux did not answer within 30s: display-message -p -t …")
        return real_facts(pane_id)

    monkeypatch.setattr(tmux, "pane_facts", wedged_for_the_newcomer)
    if verb == "switch":
        receipt: Any = fleet_service.switch(project, agent.label)
    else:
        receipt = fleet_service.restart(project, agent.label)
    monkeypatch.setattr(tmux, "pane_facts", real_facts)

    assert receipt.resumed and receipt.started.session_id == agent.session_id
    assert any("could not be asked whether the agent is up" in note for note in receipt.notes)
    with store_session() as store:
        kept = store.get_task(task.id)
    assert kept is not None and (kept.status, kept.claimed_by) == ("doing", agent.session_id)
    assert _events(project, "task_released") == [] and _events(project, "agent_exited") == []
    live = fleet_service.list_agents(project)
    assert [status.agent.id for status in live] == [receipt.started.id]


def test_the_automatic_hand_over_puts_what_its_switch_did_not_do_on_the_board(
    tmux: FakeTmux,
    typed_first_lines: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Review of #203, final round, FLEET-5: ``hooks.hand_over`` threw away the
    ``SwitchReceipt``, and the worker has no terminal, so every note about what the
    switch did NOT do reached no one. And ``switched`` was worded from ``resumed``
    alone. A fresh replacement too slow to come up for its multi-line hand-off prompt
    sat idle at an empty prompt while the board said it "started fresh with a hand-off
    prompt". The line now says what reached the pane, and the notes go on the board."""
    from aisquare.services import hooks as hooks_service

    _two_slots_with_usage(monkeypatch, work=95, personal=10)
    agent = fleet_service.spawn(project, "coder", worktree=False, account="2").agent
    _with_transcript(agent, tmp_path / "missing.jsonl")  # named, not on disk: a fresh start
    # The fake's new pane never comes up, so the hand-off prompt is past its wait.

    hooks_service.hand_over(agent.session_id or "", reason="session limit")

    assert [text for _pane, kind, text in tmux.typed if kind == "paste"] == []
    [switched] = _events(project, "switched")
    assert switched.endswith(
        "(session limit) — started fresh, but its hand-off prompt was NOT typed"
    )
    [said] = [note for note in _events(project, "note") if note.startswith(f"{agent.label}: ")]
    assert said.startswith(f"{agent.label}: switched — ")
    assert "the prompt has several lines — NOT typed" in said
    # What every switch says is not on the board: only what this one could not do.
    assert "launched as recorded" not in said and "headroom" not in said


def test_a_clean_automatic_hand_over_puts_no_note_beside_switched(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Review of the #203 final-round fixes, F2: ``hand_over`` posted every note on the
    switch's receipt, and most are routine on every hand-over: the headroom each
    account had, "launched as recorded" (every row since v18), a one-line prompt typed
    past the wait. So nearly every automatic switch put a ``switched — …`` note on the
    feed, account advice the other agents read included. Only what the switch could
    not do goes on the board; a clean one is ``switched`` alone. The receipt still
    carries every note for the CLI, which is the control."""
    from aisquare.services import hooks as hooks_service

    _two_slots_with_usage(monkeypatch, work=95, personal=10)
    agent = fleet_service.spawn(project, "coder", worktree=False, account="2").agent
    transcript = tmp_path / f"{agent.session_id}.jsonl"
    transcript.write_text('{"type":"user"}\n', encoding="utf-8")
    _with_transcript(agent, transcript)  # resumable: its one line is typed past the wait
    real_switch = fleet_service.switch
    receipts: list[fleet_service.SwitchReceipt] = []

    def switch_and_keep(*args: Any, **kwargs: Any) -> fleet_service.SwitchReceipt:
        receipts.append(real_switch(*args, **kwargs))
        return receipts[-1]

    monkeypatch.setattr(fleet_service, "switch", switch_and_keep)

    hooks_service.hand_over(agent.session_id or "", reason="session limit")

    [switched] = _events(project, "switched")
    assert switched.endswith("(session limit) — resumed its session")
    assert [note for note in _events(project, "note") if note.startswith(f"{agent.label}: ")] == []
    [receipt] = receipts
    assert any(note.startswith("launched as recorded") for note in receipt.notes)
    assert receipt.failures == []


# --- a hand-off from a teammate, in the Spawn dialog (HANDOFF, tsk_01m3ns5a736s) ------------


def _claude_windows(tmux: FakeTmux, monkeypatch: pytest.MonkeyPatch) -> None:
    """New panes show Claude Code at its prompt, so a multi-line first message is typed."""
    original = tmux.spawn_window

    def ready(*args: Any, **kwargs: Any) -> WindowInfo:
        window = original(*args, **kwargs)
        tmux.set_command(window.pane_id, "claude")
        return window

    monkeypatch.setattr(tmux, "spawn_window", ready)


def _pasted(tmux: FakeTmux, pane_id: str) -> list[str]:
    return [text for pane, kind, text in tmux.typed if pane == pane_id and kind == "paste"]


def _on_disk(agent: FleetAgent, tmp_path: Path) -> Path:
    """The agent's transcript, written where its board session says it is."""
    transcript = tmp_path / f"{agent.session_id}.jsonl"
    transcript.write_text('{"type":"user"}\n', encoding="utf-8")
    _with_transcript(agent, transcript)
    return transcript


def test_the_hand_off_picker_lists_this_projects_open_teammates_and_no_other(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, plain_project: ProjectInfo
) -> None:
    """The owner's ask: "any current running or stopped but still open/attached teammate".
    Open is a row that has not ended and whose window is still there: running, waiting,
    asking, or exited with the dead window tmux keeps. A lost row (its window gone), an
    ended one and another project's are nobody to hand off from."""
    running = _coder(project)
    exited = _coder(project)
    lost = _coder(project)
    ended = _coder(project)
    elsewhere = fleet_service.spawn(plain_project, "coder", worktree=False).agent
    tmux.die(exited.pane_id, 0)
    tmux.vanish(lost.pane_id)
    fleet_service.stop(project, ended.label)

    listed = {status.agent.id: status.state for status in fleet_service.handoff_sources(project)}

    assert set(listed) == {running.id, exited.id}
    assert listed[exited.id] == "exited" and listed[running.id] != "lost"
    assert elsewhere.id not in listed


def test_a_fork_resumes_the_sources_session_as_a_new_one_and_shares_nothing(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, tmp_path: Path
) -> None:
    """Fork, the default. The source keeps running, untouched. The new agent resumes the
    source's transcript as a FORKED session: ``--resume <transcript> --fork-session``, its
    own id minted and passed as ``--session-id`` (Claude Code takes the two together only
    when it forks), so the row joins the new id and the source's is never shared. A new
    label, its own worktree, no claim; and the launch it records holds no session flag,
    so a restart of the fork resumes the fork, never the source."""
    task = _add_task(project, "Ship auth")
    source = _coder(project, task_id=task.id)
    old = source.session_id or ""
    transcript = _on_disk(source, tmp_path)
    team_service.claim_task(task.id, session_ref=old)

    receipt = fleet_service.hand_off(project, source.label)

    fork = receipt.started.agent
    command = _command(tmux)
    assert (receipt.mode, receipt.resumed, receipt.stopped) == ("fork", True, None)
    assert _flag(command, "--resume") == str(transcript) and "--fork-session" in command
    assert fork.session_id is not None and fork.session_id != old
    assert _flag(command, "--session-id") == fork.session_id
    assert fork.launch_spec is not None
    chosen = {"--resume", "--fork-session", "--session-id", "--continue"}
    assert not chosen & set(fork.launch_spec.extra_args), fork.launch_spec.extra_args
    assert fork.label != source.label and fork.task_id is None
    assert fork.worktree and fork.cwd != source.cwd
    with store_session() as store:
        kept = store.get_fleet_agent(source.id)
    assert kept is not None and kept.ended_at is None and kept.session_id == old
    held = _task_now(task.id)
    assert (held.status, held.claimed_by) == ("doing", old), "the source's claim never moved"
    [line] = _first_lines(tmux, fork.pane_id)
    assert "\n" not in line and f"a fork of {source.label}" in line
    [event] = _events(project, "forked")
    assert event.startswith(f"{fork.label} forked from {source.label}")


def test_a_forks_own_worktree_branches_from_the_commit_the_source_is_on(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """A fork's own worktree is a new branch, cut at the commit the source's tree is on, so the
    fork starts from the source's committed work and never works in the source's tree.
    What the source has not committed stays with the source."""
    task = _add_task(project, "Ship auth")
    source = fleet_service.spawn(project, "coder", task_id=task.id, worktree=True).agent
    tree = Path(source.cwd)
    (tree / "done.txt").write_text("committed\n", encoding="utf-8")
    _git("add", "done.txt", cwd=tree)
    _git("commit", "-q", "-m", "work in the source", cwd=tree)
    (tree / "wip.txt").write_text("not committed\n", encoding="utf-8")
    head = _git("rev-parse", "HEAD", cwd=tree)

    fork = fleet_service.hand_off(project, source.label, fresh=True).started.agent

    own = Path(fork.cwd)
    assert fork.worktree and own != tree
    assert _git("rev-parse", "HEAD", cwd=own) == head
    branch = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=own)
    assert branch != _git("rev-parse", "--abbrev-ref", "HEAD", cwd=tree)
    assert (own / "done.txt").is_file() and not (own / "wip.txt").exists()


@pytest.mark.parametrize("why", ["asked", "no transcript"])
def test_a_fork_starts_fresh_when_asked_or_without_a_transcript(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    why: str,
) -> None:
    """Start fresh checked, or no transcript on disk: no ``--resume``, an id minted as any
    spawn mints one, and a first message built from the board that names the source as
    where the fork came from and its task as the source's work, not the fork's."""
    _claude_windows(tmux, monkeypatch)
    task = _add_task(project, "Ship auth")
    source = _coder(project, task_id=task.id)
    if why == "asked":
        _on_disk(source, tmp_path)
    else:
        _with_transcript(source, tmp_path / "missing.jsonl")

    receipt = fleet_service.hand_off(project, source.label, fresh=why == "asked")

    fork = receipt.started.agent
    command = _command(tmux)
    assert receipt.resumed is False
    assert "--resume" not in command and "--fork-session" not in command
    assert fork.session_id is not None and fork.session_id != source.session_id
    assert _flag(command, "--session-id") == fork.session_id
    [message] = _first_lines(tmux, fork.pane_id)
    assert f"a fork of {source.label}" in message and task.id in message
    assert any("no transcript" in note for note in receipt.notes) is (why == "no transcript")


def test_take_over_is_the_sources_restart_with_the_dialogs_changes(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Take over is the source's own hand-over, which ``restart`` already is: a running
    agent stopped with its claims parked for the replacement (an exited one simply started
    again), under the same label, task and tree, resumed or fresh as asked. The dialog's
    changes ride along, and the row it read is the row restarted."""
    source = _coder(project)
    asked: list[dict[str, Any]] = []

    def restart(on: ProjectInfo, label: str, **kwargs: Any) -> fleet_service.RestartReceipt:
        asked.append({"project": on.id, "label": label, **kwargs})
        return fleet_service.RestartReceipt(
            replaced=source,
            started=source,
            resumed=True,
            was_running=True,
            tmux_session="asq-x",
            notes=["kept"],
        )

    monkeypatch.setattr(fleet_service, "restart", restart)

    receipt = fleet_service.hand_off(
        project, source.label, mode="take_over", fresh=True, role="tester", persona=""
    )

    assert asked == [
        {
            "project": project.id,
            "label": source.label,
            "fresh": True,
            "role": "tester",
            "account": None,
            "persona": "",
            "size": None,
            "spawned_by": "user",
            "agent_id": source.id,
        }
    ]
    assert (receipt.mode, receipt.resumed, receipt.stopped) == ("take_over", True, source)
    assert receipt.started.agent == source and receipt.notes == ["kept"]


@pytest.mark.parametrize("fresh", [False, True])
def test_restart_as_another_role_keeps_the_label_the_task_and_the_claims(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fresh: bool,
) -> None:
    """A take-over with the role and the persona changed in the dialog. The source stops as a
    hand-over; the replacement runs under its label and task, as the new role, with no
    persona. Resumed, it keeps the session id and with it the claims; fresh, the claims
    move onto its new id with the row, never through the pool."""
    _claude_windows(tmux, monkeypatch)
    task = _add_task(project, "Ship auth")
    source = _coder(project, task_id=task.id)
    old = source.session_id or ""
    _on_disk(source, tmp_path)
    team_service.claim_task(task.id, session_ref=old)

    receipt = fleet_service.restart(project, source.label, fresh=fresh, role="tester", persona="")

    started = receipt.started
    assert receipt.was_running and receipt.resumed is not fresh
    assert (started.label, started.role, started.task_id) == (source.label, "tester", task.id)
    assert not started.persona
    held = _task_now(task.id)
    assert held.status == "doing" and held.claimed_by == started.session_id
    assert (started.session_id == old) is not fresh


def test_restart_on_another_account_starts_the_replacement_there(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    _two_slots_with_usage(monkeypatch, work=10, personal=10)
    source = fleet_service.spawn(project, "coder", worktree=False, account="1").agent

    receipt = fleet_service.restart(project, source.label, account="2")

    assert receipt.started.account_slot == 2
    assert _flag(_command(tmux), "--account") == "2"


def test_a_role_the_replacement_cannot_run_as_is_refused_before_anything_stops(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    source = _coder(project)

    with pytest.raises(FleetError, match="nope"):
        fleet_service.restart(project, source.label, role="nope")

    assert source.pane_id not in tmux.killed
    with store_session() as store:
        kept = store.get_fleet_agent(source.id)
    assert kept is not None and kept.ended_at is None


def test_a_take_over_whose_replacement_cannot_start_gives_the_claims_back(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The source is stopped as a hand-over, so its claims wait for the replacement; a
    replacement that then fails to start must give them back, never leave them parked."""
    task = _add_task(project, "Ship auth")
    source = _coder(project, task_id=task.id)
    old = source.session_id or ""
    _on_disk(source, tmp_path)
    team_service.claim_task(task.id, session_ref=old)

    def spawn_refuses(*args: Any, **kwargs: Any) -> fleet_service.SpawnReceipt:
        raise FleetError("tmux could not start the window: boom")

    monkeypatch.setattr(fleet_service, "spawn", spawn_refuses)
    with pytest.raises(FleetError, match="boom"):
        fleet_service.restart(project, source.label, role="tester")

    held = _task_now(task.id)
    assert held.claimed_by != old or held.status != "doing", "left parked on the stopped session"


def test_a_fork_on_this_shells_account_runs_where_a_plain_spawn_runs(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """runner2-1's reopen of #234: Account edited to (this shell's) was dropped, because the
    dialog sent None, and None keeps the teammate's own slot. THIS_SHELL_ACCOUNT says it
    explicitly: no --account, the ladder a plain spawn runs, never the teammate's slot."""
    _two_slots_with_usage(monkeypatch, work=10, personal=10)
    # Slot 3: since 0.8.1 a plain spawn here lands on slot 2, the first one signed in.
    source = fleet_service.spawn(project, "coder", worktree=False, account="3").agent
    plain = fleet_service.spawn(project, "coder", worktree=False).agent
    plain_flag = _flag(_command(tmux), "--account")
    assert source.account_slot == 3 and plain.account_slot != 3, "the pin needs two answers"

    fork = fleet_service.hand_off(
        project, source.label, fresh=True, account=fleet_service.THIS_SHELL_ACCOUNT
    ).started.agent

    assert fork.account_slot == plain.account_slot
    assert _flag(_command(tmux), "--account") == plain_flag


def test_a_take_over_on_this_shells_account_runs_where_a_plain_spawn_runs(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same through Take over, which is the teammate's restart."""
    _two_slots_with_usage(monkeypatch, work=10, personal=10)
    # Slot 3: since 0.8.1 a plain spawn here lands on slot 2, the first one signed in.
    source = fleet_service.spawn(project, "coder", worktree=False, account="3").agent
    plain = fleet_service.spawn(project, "coder", worktree=False).agent
    plain_flag = _flag(_command(tmux), "--account")
    assert source.account_slot == 3 and plain.account_slot != 3, "the pin needs two answers"

    receipt = fleet_service.hand_off(
        project, source.label, mode="take_over", account=fleet_service.THIS_SHELL_ACCOUNT
    )

    assert receipt.started.agent.account_slot == plain.account_slot
    assert _flag(_command(tmux), "--account") == plain_flag


def test_a_role_whose_env_file_cannot_be_read_is_refused_before_a_window_or_a_stop(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, tmp_path: Path
) -> None:
    """Follow-up of #257: a reviewer bound to a `GH_TOKEN` file (`team bind --env-file`)
    whose file cannot be read is refused by the window's `aisquare launch`, rather than
    started on this shell's credentials. Asked by the spawn, before a window opens, and by
    a restart, before the running agent is stopped: the window would end as it began."""
    from aisquare.services import settings as settings_service

    token = tmp_path / "reviewer-gh-token"
    token.write_text("github_pat_x\n", encoding="utf-8")
    settings_service.bind_role("reviewer", env_files={"GH_TOKEN": str(token)})
    reviewer = fleet_service.spawn(project, "reviewer", worktree=False).agent
    token.unlink()
    started = len(tmux.spawned)
    refusal = f"reviewer reads GH_TOKEN from {token}, which cannot be read"

    with pytest.raises(FleetError, match=re.escape(refusal)):
        fleet_service.spawn(project, "reviewer", worktree=False, label="reviewer-2")
    with pytest.raises(FleetError, match=re.escape(refusal)):
        fleet_service.restart(project, reviewer.label)

    assert len(tmux.spawned) == started, "no window"
    assert tmux.typed == [] and tmux.killed == [], "the running reviewer is not stopped"
    with store_session() as store:
        live = store.fleet_agent_by_label(project.id, reviewer.label, live_only=True)
    assert live is not None and live.id == reviewer.id


# --- Claude Code's folder-trust question (core.claude_sharing) ------------------------

TRUST_SCREEN = (
    (Path(__file__).parent / "fixtures" / "claude_code" / "trust_prompt_2.1.299.txt")
    .read_text(encoding="utf-8")
    .splitlines()
)
"""The question as ``capture-pane -e`` showed it on Claude Code 2.1.299, escapes and all."""


def _signed_in(account: Any, email: str, *, trusted: tuple[str, ...] = ()) -> Path:
    path = accounts_core.claude_json_path(account)
    path.parent.mkdir(parents=True, exist_ok=True)
    projects = {key: {"hasTrustDialogAccepted": True} for key in trusted}
    body = {"oauthAccount": {"emailAddress": email}, "userID": email, "projects": projects}
    path.write_text(json.dumps(body, indent=2), encoding="utf-8")
    return path


def test_a_spawn_shares_the_folder_trust_into_the_account_it_picks_and_says_so(
    fake_home: Path, tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """Account 2 said yes to the repository; a coder spawned on account 3 in a worktree of it
    starts without the question, and the receipt — the manager's view — says why."""
    root = claude_sharing.project_key(Path(os.path.realpath(project.root)))
    _signed_in(accounts_core.default_account(), "one@x.dev")
    second, third = accounts_core.create_account(), accounts_core.create_account()
    _signed_in(second, "two@x.dev", trusted=(root,))
    _signed_in(third, "three@x.dev")

    receipt = fleet_service.spawn(project, "coder", worktree=True, account="3")

    assert receipt.agent.cwd != project.root  # its own worktree, covered by the repository
    data = json.loads(accounts_core.claude_json_path(third).read_text())
    assert data["projects"] == {root: {"hasTrustDialogAccepted": True}}
    assert data["oauthAccount"] == {"emailAddress": "three@x.dev"}
    shared = f"accounts: {root} is trusted on account 2 — now on account 3 too"
    assert shared in receipt.notes
    assert receipt.shared == [shared]  # done, not a warning: the receipt says it with a ✓
    # The plain claude was not the one launched: nothing written there.
    plain = json.loads(accounts_core.claude_json_path(accounts_core.default_account()).read_text())
    assert plain["projects"] == {}


def test_a_spawn_shares_the_trust_outside_the_branch_lock_and_tells_the_window(
    fake_home: Path,
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #259, round 21: a worktree spawn shared the trust while it held the
    repository's branch lock, which a reap and every other spawn there queue on, reading
    and parsing a .claude.json (tens of MB on a long-used account) for each account it
    asked; the window's `aisquare launch` then read them again. It shares before the lock,
    reads each file once, and tells the window, whose launch reads none."""
    root = claude_sharing.project_key(Path(os.path.realpath(project.root)))
    _signed_in(accounts_core.default_account(), "one@x.dev")
    second, third = accounts_core.create_account(), accounts_core.create_account()
    _signed_in(second, "two@x.dev", trusted=(root,))
    _signed_in(third, "three@x.dev")
    held: list[bool] = []
    reads: list[tuple[Path, bool]] = []
    real_lock, real_read = fleet_service._branches_locked, claude_sharing.read_claude_json

    @contextmanager
    def locked(common: Path, *, wait: float) -> Iterator[None]:
        with real_lock(common, wait=wait):
            held.append(True)
            try:
                yield
            finally:
                held.pop()

    def read(path: Path) -> dict[str, Any]:
        reads.append((path, bool(held)))
        return real_read(path)

    monkeypatch.setattr(fleet_service, "_branches_locked", locked)
    monkeypatch.setattr(claude_sharing, "read_claude_json", read)

    receipt = fleet_service.spawn(project, "coder", worktree=True, account="3")

    assert receipt.agent.cwd != project.root, "a worktree, made under the lock"
    assert receipt.shared == [f"accounts: {root} is trusted on account 2 — now on account 3 too"]
    assert reads and not any(under for _path, under in reads), "none under the branch lock"
    assert len(reads) == len({path for path, _under in reads}), "each file read once"
    told = tmux.spawned[-1]["env"]
    assert isinstance(told, dict) and told[accounts_service.SHARED_ENV] == "3"
    # A share it could not make is left to the window's launch to try again.
    accounts_core.claude_json_path(third).write_text("{not json", encoding="utf-8")
    again = fleet_service.spawn(project, "coder", worktree=True, account="3")
    assert again.shared == [] and any("could not be shared" in note for note in again.notes)
    untold = tmux.spawned[-1]["env"]
    assert isinstance(untold, dict) and accounts_service.SHARED_ENV not in untold


def test_a_worktree_spawn_shares_the_trust_its_tree_will_have_not_its_roots(
    fake_home: Path,
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    repo: Path,
    tmp_path: Path,
) -> None:
    """Review of #259, round 22, 8/9: round 21 moved the spawn's share before the branch lock,
    for the project's root, and told the window, whose launch then did not share for the
    tree it starts in. The two scopes differ. An entry on the tree itself (an older Claude
    Code's, or a hand edit; the fleet reuses one tree per label) was no longer found, and
    an entry on a root that is a sub-folder of its repository was copied and said shared,
    though it does not cover the tree. The spawn shares for the tree as it will be."""
    _signed_in(accounts_core.default_account(), "one@x.dev")
    second, third = accounts_core.create_account(), accounts_core.create_account()
    worktrees = fleet_service.settings().worktree_dir
    tree = claude_sharing.project_key(Path(os.path.realpath(project.root)) / worktrees / "coder-1")
    _signed_in(second, "two@x.dev", trusted=(tree,))
    _signed_in(third, "three@x.dev")

    receipt = fleet_service.spawn(project, "coder", worktree=True, account="3")

    assert claude_sharing.project_key(Path(os.path.realpath(receipt.agent.cwd))) == tree
    assert receipt.shared == [f"accounts: {tree} is trusted on account 2 — now on account 3 too"]
    data = json.loads(accounts_core.claude_json_path(third).read_text())
    assert data["projects"] == {tree: {"hasTrustDialogAccepted": True}}
    told = tmux.spawned[-1]["env"]
    assert isinstance(told, dict) and told[accounts_service.SHARED_ENV] == "3"

    # A root that is itself a worktree of another checkout: an entry on it covers it, and
    # not the trees the fleet makes under it, which are their own top levels.
    checkout = tmp_path / "checkout"
    _git("worktree", "add", "-q", "-b", "elsewhere", str(checkout), cwd=repo)
    nested = ProjectInfo(id=project_id_for(checkout), root=checkout, linked_repos=[])
    with store_session() as store:
        store.onboard_project(nested)
    on_root = claude_sharing.project_key(Path(os.path.realpath(checkout)))
    assert on_root in claude_sharing.trust_scope(checkout).covering, "the premise"
    _signed_in(second, "two@x.dev", trusted=(on_root,))
    _signed_in(third, "three@x.dev")

    inside = fleet_service.spawn(nested, "coder", worktree=True, account="3")

    assert inside.agent.cwd != checkout, "its own worktree, under that root"
    assert inside.shared == [], "the root's entry does not cover the tree"
    assert json.loads(accounts_core.claude_json_path(third).read_text())["projects"] == {}


def test_an_agent_at_the_trust_question_needs_the_human_until_it_is_answered(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    agent = _coder(project)
    tmux.set_command(agent.pane_id, "claude")
    tmux.screens[agent.pane_id] = TRUST_SCREEN

    (status,) = fleet_service.list_agents(project)

    assert status.state == "attention"
    assert status.detail is not None and status.detail.startswith(TRUST_PROMPT)
    trust = install_route.command_line(["aisquare", "accounts", "trust", str(agent.cwd)])
    assert trust in status.detail
    assert fleet_service.status_of(agent).detail == status.detail
    # `fleet tell` files a board note instead of typing: Enter there picks "No, exit".
    told = fleet_service.tell(project, agent.label, "start on the task")
    assert not told.delivered and told.how.startswith("it is attention") and tmux.typed == []
    # Answered, the screen moves on and so does the state.
    tmux.screens[agent.pane_id] = ["╭─ Claude Code ─╮", "\u276f "]
    assert fleet_service.list_agents(project)[0].state == "waiting"


def test_an_agent_whose_hooks_have_run_is_never_asked_for_its_screen(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """Claude Code runs no hook in a folder nobody trusted: one that has is past the
    question, and a healthy fleet pays no capture per agent per tick."""
    agent = _coder(project)
    tmux.set_command(agent.pane_id, "claude")
    tmux.screens[agent.pane_id] = TRUST_SCREEN  # what it showed before the yes
    _board_session(agent, "waiting")
    tmux.captured.clear()
    assert fleet_service.list_agents(project)[0].state == "waiting"
    assert tmux.captured == []
    # Control: a row no hook has spoken for yet is asked, and only it.
    fresh = _coder(project)
    tmux.set_command(fresh.pane_id, "claude")
    fleet_service.list_agents(project)
    assert tmux.captured == [fresh.pane_id]


def test_an_agent_that_is_not_claude_code_is_never_asked_for_its_screen(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """Review of #259, round 14: only Claude Code asks the trust question, and another
    harness's agent fires no hook, ever. Its pane was captured on every listing, and read
    as at the question when its screen held the words: ``fleet tell`` filed a note it
    never reads instead of typing, and Remote pushed a trust card for an agent at work."""
    agent = _coder(project, binary=sys.executable)  # another harness: no --session-id
    assert agent.session_id is None and not harness.is_default_agent(agent.binary)
    tmux.set_command(agent.pane_id, "aider")
    tmux.screens[agent.pane_id] = TRUST_SCREEN  # the question's words, as it reads them
    tmux.captured.clear()

    (status,) = fleet_service.list_agents(project)

    assert status.state != "attention" and tmux.captured == []
    # Control: a Claude Code row no hook has spoken for yet is asked, and read at it.
    claude = _coder(project)
    tmux.set_command(claude.pane_id, "claude")
    tmux.screens[claude.pane_id] = TRUST_SCREEN
    states = {status.agent.id: status.state for status in fleet_service.list_agents(project)}
    assert states[claude.id] == "attention" and tmux.captured == [claude.pane_id]


def test_a_row_pinned_to_a_session_is_asked_for_its_screen_whatever_its_binary_is_called(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, tmp_path: Path
) -> None:
    """Review of #259, round 15: a row with no session yet was asked about the trust
    question only when its binary was named ``claude``. Only Claude Code is started on a
    session id (minted for ``claude`` by name, or one its caller or a resume named), so
    the ``claude2`` parallel install spawned on a session its caller chose reads at the
    question as ``attention``, and ``fleet tell`` does not type into it, where Enter picks
    "No, exit"."""
    claude2 = _executable(tmp_path / "bin" / "claude2")
    pinned = "0b3c6a52-9f7e-4d0e-8a51-2f4c1d7e9b60"
    agent = _coder(project, binary=str(claude2), agent_args=["--session-id", pinned])
    assert agent.session_id == pinned and not harness.is_default_agent(agent.binary)
    tmux.set_command(agent.pane_id, "claude2")
    tmux.screens[agent.pane_id] = TRUST_SCREEN

    (status,) = fleet_service.list_agents(project)

    assert status.state == "attention", status
    assert status.detail is not None and status.detail.startswith(TRUST_PROMPT)
    told = fleet_service.tell(project, agent.label, "start on the task")
    assert not told.delivered and tmux.typed == []


def test_an_agent_whose_hooks_run_on_a_hubs_board_is_never_asked_for_its_screen(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under ``AISQUARE_TEAM_HUB`` every session is on the hub's board, and the listing
    joins its project's own: no row had one, so every listing captured every pane, and
    an agent whose screen showed the question's words read as at it (review of #259,
    round 13). Its session is found by its id, in the listing's one store session: a
    second one was opened for it on every listing, with the store's migration checks
    (round 16), and handed on to a caller that reads it too."""
    hub = ProjectInfo(id="prj_hub000000", root=tmp_path / "hub", linked_repos=[])
    agent = _coder(project)
    tmux.set_command(agent.pane_id, "claude")
    tmux.screens[agent.pane_id] = TRUST_SCREEN  # a reviewer reading claude_sharing's fixture
    now = datetime.now(tz=UTC)
    assert agent.session_id is not None
    with store_session() as store:
        store.onboard_project(hub)
        store.upsert_session(
            TeamSession(
                id=agent.session_id, project_id=hub.id, role=agent.role,
                started_at=now, last_seen_at=now,
            )
        )  # fmt: skip
    tmux.captured.clear()
    opens = 0
    real_open = core_store.open_store

    def counting_open() -> ContextStore:
        nonlocal opens
        opens += 1
        return real_open()

    monkeypatch.setattr(core_store, "open_store", counting_open)
    looked_up: dict[str, TeamSession | None] = {}

    (status,) = fleet_service.list_agents(project, looked_up=looked_up)

    assert status.session is None, "the premise: the listing joins its own board's sessions"
    assert status.state != "attention" and tmux.captured == []
    assert opens == 1, "one store session for the listing, its lookups included"
    found = looked_up[agent.session_id]
    assert found is not None and found.project_id == hub.id


def test_spawn_never_types_its_prompt_into_the_trust_question(
    tmux: FakeTmux,
    typed_first_lines: FakeTmux,
    clock: FakeClock,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = tmux.pane_facts

    def up_at_the_question(pane_id: str) -> PaneFacts | None:
        tmux.set_command(pane_id, "claude")
        tmux.screens[pane_id] = TRUST_SCREEN
        return original(pane_id)

    monkeypatch.setattr(tmux, "pane_facts", up_at_the_question)
    receipt = fleet_service.spawn(project, "coder", worktree=False, prompt="start on tsk_1")
    assert tmux.typed == []
    assert receipt.prompt_typed == "no"  # `Typed`: NOT typed, never "maybe" or "unsent"
    note = next(n for n in receipt.notes if "folder-trust question" in n)
    assert "NOT typed" in note
    trust = ["aisquare", "accounts", "trust", str(receipt.agent.cwd)]
    assert install_route.command_line(trust) in note
    assert clock.now >= PROMPT_TIMEOUT, "waited for a human to answer it first"


def test_spawn_types_its_prompt_once_the_trust_question_is_answered(
    tmux: FakeTmux,
    typed_first_lines: FakeTmux,
    clock: FakeClock,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_facts, original_capture = tmux.pane_facts, tmux.capture
    asked = 0

    def up_at_the_question(pane_id: str) -> PaneFacts | None:
        tmux.set_command(pane_id, "claude")
        return original_facts(pane_id)

    def answered_on_the_third_look(pane_id: str, **kwargs: Any) -> Capture:
        nonlocal asked
        asked += 1
        if asked < 3:
            tmux.screens[pane_id] = TRUST_SCREEN
        elif asked == 3:
            tmux.screens.pop(pane_id, None)  # answered: Claude Code draws its input box
        return original_capture(pane_id, **kwargs)

    monkeypatch.setattr(tmux, "pane_facts", up_at_the_question)
    monkeypatch.setattr(tmux, "capture", answered_on_the_third_look)
    receipt = fleet_service.spawn(project, "coder", worktree=False, prompt="start on tsk_1")
    pane = receipt.agent.pane_id
    assert tmux.typed == [(pane, "paste", "start on tsk_1"), (pane, "key", "Enter")]
    assert receipt.notes == [] and clock.now < PROMPT_TIMEOUT
    assert claude_sharing.shows_trust_prompt(TRUST_SCREEN)  # the fixture is the question


def test_a_switch_after_a_cross_account_resume_resumes_the_real_transcript(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """P0-W3b: the resumed session's SessionStart reports a transcript under the NEW account
    that Claude Code never creates (measured by P0-W0 on 2.1.309 and 2.1.223). Stored, it left
    the row naming no file, and the agent's next switch started fresh, its session lost."""
    _two_slots_with_usage(monkeypatch, work=95, personal=10)
    agent = fleet_service.spawn(project, "coder", worktree=False, account="2").agent
    real = tmp_path / "accounts" / "2" / "projects" / "-repo" / f"{agent.session_id}.jsonl"
    real.parent.mkdir(parents=True)
    real.write_text('{"type":"user"}\n', encoding="utf-8")
    _with_transcript(agent, real)
    missing = tmp_path / "accounts" / "3" / "projects" / "-repo" / f"{agent.session_id}.jsonl"
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    team_service.hook_session_start(
        agent.session_id or "", project.root, "resume", transcript_path=str(missing)
    )

    receipt = fleet_service.switch(project, agent.label, reason="session limit")

    assert receipt.resumed
    assert _flag(_command(tmux), "--resume") == str(real)


# --- one switch per limit (P0-W3, #277) -------------------------------------------------------


def _limit_handed_over_inline(
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    nudges: list[str] | None = None,
    message: str = "You've hit your session limit",
) -> tuple[FleetAgent, list[str]]:
    """An agent on slot 2 hits a limit with ``on_limit = "switch"`` (the default); the hook's
    detached worker is run inline. Returns the agent and every manager nudge (also collected
    into ``nudges`` when given, for a caller whose worker raises)."""
    from aisquare.services import hooks as hooks_service

    agent = fleet_service.spawn(project, "coder", worktree=False, account="2").agent
    transcript = tmp_path / f"{agent.session_id}.jsonl"
    transcript.write_text('{"type":"user"}\n', encoding="utf-8")
    _with_transcript(agent, transcript)
    nudges = [] if nudges is None else nudges
    monkeypatch.setattr(
        team_service, "_nudge_manager", lambda project_id, *, reason, board: nudges.append(reason)
    )

    def worker(argv: list[str]) -> None:
        session_id = argv[argv.index("hand-over") + 1]
        hooks_service.hand_over(session_id, reason=argv[argv.index("--reason") + 1])

    monkeypatch.setattr(hooks_service, "_detach", worker)
    hooks_service.turn_failed(
        session_id=agent.session_id,
        error="rate_limit",
        message=message,
    )
    return agent, nudges


def test_a_limit_with_on_limit_switch_moves_the_agent_once_and_wakes_no_manager(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _two_slots_with_usage(monkeypatch, work=95, personal=10)

    agent, nudges = _limit_handed_over_inline(project, monkeypatch, tmp_path)

    assert _events(project, "limited") == []  # its own kind, which wakes nobody
    [limited] = _events(project, team_service.LIMITED_HANDING_OVER)
    assert "moving it to another account automatically" in limited
    assert "fleet switch" not in limited  # the manager is not asked to do it too
    assert len(_events(project, "switched")) == 1
    assert nudges == []
    with store_session() as store:
        events = store.recent_events(project.id, limit=50)
    manager = TeamSession(
        id="mgr",
        project_id=project.id,
        role="manager",
        started_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
    )
    assert not any(team_service.wake_class(event, manager) == "wake" for event in events)

    # The manager's cycle used to switch the replacement again once the hand-over was done.
    with pytest.raises(fleet_service.FleetError, match="usage-limit hand-over; `--force`"):
        fleet_service.switch(project, agent.label)
    assert len(_events(project, "switched")) == 1


def test_force_moves_a_fresh_replacement_again(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _third_slot_with_usage(monkeypatch, work=95, personal=10, third=20)
    agent, _nudges = _limit_handed_over_inline(project, monkeypatch, tmp_path)
    [moved] = fleet_service.list_agents(project)
    assert moved.agent.account_slot == 3

    receipt = fleet_service.switch(project, agent.label, force=True)

    assert (receipt.from_slot, receipt.to_slot) == (3, 4)
    assert len(_events(project, "switched")) == 2


def test_a_refused_hand_over_is_handover_failed_and_wakes_the_manager(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _two_slots_with_usage(monkeypatch, work=95, personal=99)  # nowhere under the line

    _agent, nudges = _limit_handed_over_inline(
        project,
        monkeypatch,
        tmp_path,
        message="You've hit your weekly limit · resets Mon 12:00am (America/Toronto)",
    )

    assert _events(project, "switched") == []
    [failed] = _events(project, "handover_failed")
    assert "fleet switch" in failed
    assert "its own limit resets in " in failed  # the other answer: wait (reviewer-a)
    assert len(nudges) == 1


def test_a_hand_over_worker_that_crashes_posts_handover_failed_and_wakes_the_manager(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Review of #304, finding 1: with the hand-over coming, the `limited` line wakes nobody,
    so the worker is the only one left to say it could not move the agent. It said so for a
    `FleetError` alone; anything else (a locked store, a tmux error, a bug) went to the
    worker's DEVNULL stderr and the agent sat parked until the reset, days for a week."""
    _two_slots_with_usage(monkeypatch, work=95, personal=10)

    def crash(*args: Any, **kwargs: Any) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(fleet_service, "switch", crash)

    nudges: list[str] = []
    with pytest.raises(sqlite3.OperationalError):  # still the worker's to report as it did
        _limit_handed_over_inline(project, monkeypatch, tmp_path, nudges)

    [failed] = _events(project, "handover_failed")
    assert "hand-over worker failed (OperationalError: database is locked)" in failed
    assert "fleet switch" in failed
    assert len(nudges) == 1


# --- #123: an idle agent reads waiting, not working (P0-W1b) ---------------------------------


def _working_row(now: datetime, *, seen: timedelta) -> tuple[FleetAgent, TeamSession]:
    agent = FleetAgent(
        id="agt_idle1",
        project_id="prj_x",
        label="coder-1",
        role="coder",
        pane_id="%9",
        session_id="sess-idle",
        cwd=Path("/tmp"),
        created_at=now - timedelta(hours=1),
    )
    session = TeamSession(
        id="sess-idle",
        project_id="prj_x",
        role="coder",
        started_at=now - timedelta(hours=1),
        last_seen_at=now - seen,
        state="working",
    )
    return agent, session


def test_a_fresh_working_row_over_a_quiet_idle_pane_reads_waiting() -> None:
    """A turn that ended with Esc fires no Stop, and an agent whose start upsert said
    `working` before #123 stayed so: the row was trusted for `_STALE_AFTER` (30 min)
    whatever the pane showed. Past `busy_quiet_seconds` of a quiet pane, its screen
    decides — once, and only for a row in that state."""
    now = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
    agent, session = _working_row(now, seen=timedelta(minutes=5))
    quiet = fleet_service._PaneView(False, None, "claude", now - timedelta(seconds=45))
    asked: list[str] = []

    def idle() -> bool:
        asked.append("idle")
        return True

    state = fleet_service._derive(agent, session, quiet, observed=True, now=now, idle_box=idle)
    assert state == ("waiting", "idle (the board said working)")

    # Busy: the pane printed within the quiet window — trusted, and nothing is captured.
    asked.clear()
    busy = fleet_service._PaneView(False, None, "claude", now - timedelta(seconds=3))
    assert fleet_service._derive(agent, session, busy, observed=True, now=now, idle_box=idle) == (
        "working",
        None,
    )
    assert asked == []
    # A quiet pane whose box is not idle (a long tool call, a spinner) is still working.
    assert fleet_service._derive(
        agent, session, quiet, observed=True, now=now, idle_box=lambda: False
    ) == ("working", None)


def test_fleet_ls_reads_a_quiet_idle_agent_waiting_within_busy_quiet_seconds(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    from tests.captain_screens import REAL_IDLE, REAL_WORKING

    agent = fleet_service.spawn(project, "coder", worktree=False).agent
    now = datetime.now(tz=UTC)
    with store_session() as store:
        store.upsert_session(
            TeamSession(
                id=agent.session_id or "",
                project_id=project.id,
                role="coder",
                started_at=now,
                last_seen_at=now - timedelta(minutes=2),
                state="working",
            )
        )
    tmux.output_at[agent.pane_id] = now - timedelta(minutes=1)
    tmux.screens[agent.pane_id] = REAL_IDLE

    [idle] = fleet_service.list_agents(project)
    assert (idle.state, idle.detail) == ("waiting", "idle (the board said working)")

    tmux.screens[agent.pane_id] = REAL_WORKING  # mid-turn, a long tool call: still working
    [busy] = fleet_service.list_agents(project)
    assert busy.state == "working"


def test_tell_types_into_a_quiet_idle_agent_the_board_calls_working(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """#123's other half, through `fleet tell` (P0-FU, #51): a fresh `working` row whose pane
    has printed nothing for `busy_quiet_seconds` and shows Claude Code idle at its prompt is
    waiting, so the tell is TYPED, not filed as a note it would never read. The same row on a
    screen that is mid-turn still gets the note: never typed into a turn."""
    from tests.captain_screens import REAL_IDLE, REAL_WORKING

    agent = _coder(project)
    _board_session(agent, "working")
    tmux.set_command(agent.pane_id, "claude")  # the agent, not the launcher, is up
    tmux.output_at[agent.pane_id] = datetime.now(tz=UTC) - timedelta(minutes=1)
    tmux.screens[agent.pane_id] = REAL_WORKING

    busy = fleet_service.tell(project, "coder-1", "first")
    assert not busy.delivered and "board note" in busy.how
    assert tmux.typed == []

    tmux.screens[agent.pane_id] = REAL_IDLE
    idle = fleet_service.tell(project, "coder-1", "second")
    assert idle.delivered and "typed" in idle.how
    assert tmux.typed == [(agent.pane_id, "paste", "second"), (agent.pane_id, "key", "Enter")]
    assert [n for n in _events(project, "note")] == ["first"]
