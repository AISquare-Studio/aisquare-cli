"""The suite reaches no real fleet: a private tmux per test, a stand-in for ``claude`` (13220).

A T3 test once spawned a REAL captain on the owner's main tmux socket: ``asq`` was
the fleet's default, ``isolated_home`` clears ``TMUX_TMPDIR``, and the window
exec'd the real binary. ``tests/conftest.py::no_real_fleet`` closes both doors for
every test; these pin that the doors are shut, and that an escape is said.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from aisquare.core import tmux as tmux_core
from aisquare.core.tmux import TmuxServer
from tests.conftest import _OWNER_TMUX_DIRS, RealFleetGuard, _kill_private_servers

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="tmux and /bin/sh are POSIX")


def test_the_fleets_default_socket_is_a_server_of_the_tests_own(
    no_real_fleet: RealFleetGuard,
) -> None:
    socket = TmuxServer("asq").socket_path()
    assert socket.parent.resolve() not in _OWNER_TMUX_DIRS
    assert no_real_fleet.private in socket.parents


@pytest.mark.parametrize("how", ["-L", "-S"])
def test_a_command_that_would_reach_the_owners_server_is_refused_and_recorded(
    no_real_fleet: RealFleetGuard, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """A test that clears ``TMUX_TMPDIR`` (or names a socket path) and runs tmux for real
    is answered as a failure without running, and recorded for the teardown to say."""
    if how == "-L":
        monkeypatch.delenv("TMUX_TMPDIR")
        argv = ["tmux", "-L", "asq", "list-sessions"]
    else:
        owners = sorted(_OWNER_TMUX_DIRS)[0]
        argv = ["tmux", "-S", str(owners / "asq"), "list-sessions"]
    result = tmux_core._tmux(argv, None)
    assert result.returncode == 1 and "owner's tmux server" in result.stderr
    assert no_real_fleet.reached == [tuple(argv)]
    assert "owner's tmux server" in (no_real_fleet.verdict() or "")
    no_real_fleet.forgive()  # this escape was the test's own
    assert no_real_fleet.verdict() is None


def test_claude_on_path_is_the_suites_stand_in_and_a_launch_is_said(
    no_real_fleet: RealFleetGuard,
) -> None:
    found = shutil.which("claude")
    assert found is not None and Path(found).parent == no_real_fleet.private / "bin"
    run = subprocess.run([found, "--name", "captain"], capture_output=True, text=True, check=False)
    assert run.returncode == 97 and "refused" in run.stderr
    assert no_real_fleet.launches() == ["--name captain"]
    assert "launched claude" in (no_real_fleet.verdict() or "")
    no_real_fleet.forgive()
    assert no_real_fleet.verdict() is None


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
def test_a_server_a_test_starts_is_its_own_and_teardown_ends_it(
    no_real_fleet: RealFleetGuard,
) -> None:
    server = TmuxServer("asq")
    result = tmux_core._tmux(
        [server.binary(), "-L", "asq", "-f", os.devnull, "new-session", "-d", "-s", "t"], None
    )
    assert result.returncode == 0, result.stderr
    assert server.socket_path().exists()
    assert no_real_fleet.private in server.socket_path().parents
    _kill_private_servers(no_real_fleet.private, no_real_fleet.uid, no_real_fleet.tmux)
    alive = tmux_core._tmux([server.binary(), "-L", "asq", "has-session", "-t", "t"], None)
    assert alive.returncode != 0, "the private server is gone, and every pane with it"
    assert no_real_fleet.verdict() is None
