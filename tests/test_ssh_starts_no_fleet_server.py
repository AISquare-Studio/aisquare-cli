"""#20: an SSH login on macOS never starts the fleet's tmux server.

A tmux server runs in the macOS security session of the process that started it, and so does
every pane it opens. An SSH asq that started the fleet's server opened every agent in a session
that cannot read the Keychain ("login keychain is locked"). Spawning into a server the Mac
started is unchanged.

The premise was probed on the owner's Mac (macOS 27.2) with throwaway servers on their own
``-L`` sockets, quoted in PR #464: from an SSH login, a pane of a server it started could not
read the Claude Code login (rc=36, session ``0x5020 [tty REMOTE authenticated]``), and a new
window of a server started at the Mac could (rc=0, ``0x6030 [graphic tty console
authenticated]``).
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path
from typing import Any

import pytest

from aisquare.core import login_session
from aisquare.services import fleet as fleet_service
from tests import test_fleet_service as fleet_suite

# The fleet suite's fixtures, bound here so pytest finds them for this module's tests.
tmux = fleet_suite.tmux
clock = fleet_suite.clock
claude_on_path = fleet_suite.claude_on_path
repo = fleet_suite.repo
project = fleet_suite.project

_REAL_REMOTE_SESSION = login_session.remote_session
"""The real one: ``conftest.at_the_machine`` replaces it in every test."""


class _Server:
    """The fleet's server as ``_before_a_server_starts`` and ``open_window`` ask it: what its
    socket answers, and the windows opened on it."""

    socket = "asq-test"
    path = None

    def __init__(self, answer: str | None) -> None:
        self.answer = answer
        self.opened: list[str] = []

    def socket_path(self) -> Path:
        return Path("/tmp/tmux-test/asq")

    def listener(self, recorded: Path | None = None) -> str | None:
        return self.answer

    def spawn_window(self, session: str, **_: Any) -> str:
        self.opened.append(session)
        return "window"


def _open(srv: _Server) -> object:
    return fleet_service.open_window(
        srv,  # type: ignore[arg-type]
        "asq-test",
        name="coder",
        cwd=Path.cwd(),
        command=["true"],
        config=fleet_service.settings(),
    )


@pytest.mark.parametrize("answer", [None, "gone"], ids=["stale-file", "no-file"])
def test_an_ssh_login_does_not_start_the_fleets_server(
    monkeypatch: pytest.MonkeyPatch, answer: str | None
) -> None:
    monkeypatch.setattr(login_session, "remote_session", lambda: True)
    srv = _Server(answer)

    with pytest.raises(fleet_service.FleetError) as refused:
        _open(srv)

    assert fleet_service.SSH_START_REFUSAL in str(refused.value)
    assert str(refused.value).startswith(
        "not started: start asq on the Mac first: agents started from an SSH login can't "
        "read the Keychain"
    )
    assert srv.opened == [], "a window opened, and with it a server"


def test_a_remote_session_joins_a_running_server_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe's step 5, end to end through the session read: an SSH login's session (its
    measured bits, REMOTE among them) opens a window in a server the Mac started, and is refused
    only where it would start one."""
    monkeypatch.setattr(login_session, "remote_session", _REAL_REMOTE_SESSION)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(ctypes, "CDLL", lambda path: _Security(0, 0x5020))
    running = _Server("answers")
    assert _open(running) == "window"
    assert running.opened == ["asq-test"]

    none_yet = _Server(None)
    with pytest.raises(fleet_service.FleetError, match="start asq on the Mac first"):
        _open(none_yet)
    assert none_yet.opened == []


def test_an_ssh_login_spawns_into_a_running_server_as_ever(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(login_session, "remote_session", lambda: True)
    srv = _Server("answers")
    assert _open(srv) == "window"
    assert srv.opened == ["asq-test"]


def test_a_socket_that_could_not_be_asked_is_not_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A denied socket or a connect that timed out says nothing of whether a server runs:
    nothing is refused on a guess."""
    monkeypatch.setattr(login_session, "remote_session", lambda: True)
    srv = _Server("unasked")
    assert _open(srv) == "window"


@pytest.mark.parametrize("remote", [False, None], ids=["at-the-mac", "not-macos"])
def test_a_login_at_the_machine_starts_the_server_as_ever(
    monkeypatch: pytest.MonkeyPatch, remote: bool | None
) -> None:
    monkeypatch.setattr(login_session, "remote_session", lambda: remote)
    calls: list[object] = []

    def forget(*args: object) -> list[object]:
        calls.append(args)
        return []

    monkeypatch.setattr(fleet_service, "_forget_an_exited_server", forget)
    srv = _Server(None)
    assert _open(srv) == "window"
    assert srv.opened == ["asq-test"] and len(calls) == 1


def test_a_spawn_from_ssh_is_refused_before_anything_is_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The spawn's own early check (before the project is captured or a worktree made) is the
    same one, so the refusal comes first."""
    monkeypatch.setattr(login_session, "remote_session", lambda: True)
    srv = _Server(None)
    forgot: list[object] = []
    monkeypatch.setattr(fleet_service, "_forget_an_exited_server", lambda *a: forgot.append(a))
    with pytest.raises(fleet_service.FleetError, match="start asq on the Mac first"):
        fleet_service._before_a_server_starts(srv, fleet_service.settings())  # type: ignore[arg-type]
    assert forgot == [], "a row's server start was forgotten for a server never started"


class _Security:
    """``Security.framework`` as ``SessionGetInfo`` answers: ``status`` and the attribute bits."""

    def __init__(self, status: int, bits: int) -> None:
        self.status = status
        self.bits = bits

    def SessionGetInfo(self, caller: Any, session: Any, attributes: Any) -> int:
        assert caller.value == login_session.CALLER_SECURITY_SESSION
        attributes._obj.value = self.bits
        return self.status


@pytest.mark.parametrize(
    ("status", "bits", "expected"),
    [
        # Both measured by the probe on the owner's Mac (macOS 27.2), quoted in PR #464.
        (0, 0x6030, False),  # graphic tty console authenticated: at the Mac, Keychain rc=0
        (0, 0x5020, True),  # tty REMOTE authenticated: an SSH login, Keychain rc=36
        # The REMOTE bit alone decides: the SSH login without it would not be refused, and
        # a session without graphic access is not refused for that.
        (0, 0x5020 & ~login_session.SESSION_IS_REMOTE, False),
        (-60500, 0x5020, None),  # SessionGetInfo failed: no answer, no guess
    ],
    ids=["at-the-mac", "ssh", "ssh-without-the-remote-bit", "unanswered"],
)
def test_the_session_says_whether_it_was_reached_over_the_network(
    monkeypatch: pytest.MonkeyPatch, status: int, bits: int, expected: bool | None
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(ctypes, "CDLL", lambda path: _Security(status, bits))
    assert _REAL_REMOTE_SESSION() is expected


def test_no_security_framework_is_no_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(path: str) -> object:
        raise OSError(f"dlopen({path}) failed")

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(ctypes, "CDLL", missing)
    assert _REAL_REMOTE_SESSION() is None


def test_off_macos_there_is_no_session_to_ask(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert _REAL_REMOTE_SESSION() is None


@pytest.mark.skipif(sys.platform != "darwin", reason="the real Security.framework")
def test_the_real_session_answers_on_macos() -> None:
    assert _REAL_REMOTE_SESSION() in (True, False)


def test_a_restart_from_ssh_over_a_stale_socket_refuses_before_it_records_the_dead_row(
    tmux: Any, claude_on_path: Path, project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``restart`` refuses all that does not depend on the stop BEFORE anything is stopped or
    recorded (its contract). From SSH over a stale socket it recorded the dead row, and only
    its replacement's spawn refused to start the server (review of #464, finding 1)."""
    from aisquare.core.store import store_session

    agent = fleet_service.spawn(project, "coder").agent
    tmux.kill_server()  # the Mac's server exited: its socket file is stale, no server listens
    monkeypatch.setattr(login_session, "remote_session", lambda: True)  # and this is SSH

    def row() -> tuple[object, object]:
        with store_session() as store:
            [now] = [r for r in store.fleet_agents(project.id) if r.id == agent.id]
        return now.ended_at, now.ended_by

    before = (row(), list(tmux.typed), len(tmux.spawned))

    with pytest.raises(fleet_service.FleetError, match="start asq on the Mac first") as refused:
        fleet_service.restart(project, agent.label)

    assert row() == (None, None), "the dead row is left for a listing, as a refusal leaves it"
    assert (row(), list(tmux.typed), len(tmux.spawned)) == before, "nothing recorded or started"
    assert fleet_suite._events(project, "agent_exited") == []
    assert f"cannot restart {agent.label!r}" in str(refused.value), "restart's own refusal"
