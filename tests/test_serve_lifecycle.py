"""The stdio daemon's idle deadline (#19): abandoned daemons exit themselves.

Every test drives the REAL daemon (``python -m aisquare serve --stdio``) the
way the field failure did: clients killed mid-handshake with the pipe write
end held open, so stdin never sees EOF. Process checks live here — in tests —
and nowhere in the production code (the daemon minds only its own clock).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="the [serve] extra is not installed")

_WAIT = 15  # generous CI margin for "exits by its 1s deadline"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A directory serve accepts as a project root (marker required)."""
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


def _spawn(
    project: Path, *, env_extra: dict[str, str], args: list[str] | None = None
) -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    env.pop("AISQUARE_SERVE_CLOSE_AFTER", None)
    env.update(env_extra)
    return subprocess.Popen(
        [sys.executable, "-m", "aisquare", "serve", "--stdio", *(args or [])],
        cwd=project,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _stderr_of(daemon: subprocess.Popen[bytes]) -> bytes:
    assert daemon.stderr is not None
    data: bytes = daemon.stderr.read()
    return data


def test_abandoned_daemons_exit_at_the_idle_deadline(project: Path) -> None:
    # The #19 trap, looped: each client dies mid-handshake (a partial line,
    # no newline) and its pipe write end stays open — EOF never arrives.
    daemons: list[subprocess.Popen[bytes]] = []
    for _ in range(2):
        daemon = _spawn(project, env_extra={"AISQUARE_SERVE_CLOSE_AFTER": "1"})
        assert daemon.stdin is not None
        daemon.stdin.write(b'{"jsonrpc":"2.0","id":1,"method":"initi')
        daemon.stdin.flush()
        daemons.append(daemon)  # keeping the handle open IS the no-EOF trap
    for daemon in daemons:
        assert daemon.wait(timeout=_WAIT) == 0  # exits itself — nobody reaps it
    # Zero daemons remain (every wait() above returned), and the exit says why.
    assert b"idle deadline" in _stderr_of(daemons[0])


def test_an_active_client_outlives_many_idle_windows(project: Path) -> None:
    # Deadline 2 (not 1): pings flow every 0.3s, so the margin only breaks if
    # the TEST process stalls >1.7s between writes — CI-stall headroom per
    # review, while 4.5s of traffic still spans several deadline windows.
    daemon = _spawn(project, env_extra={"AISQUARE_SERVE_CLOSE_AFTER": "2"})
    assert daemon.stdin is not None
    until = time.monotonic() + 4.5
    request_id = 0
    try:
        while time.monotonic() < until:
            request_id += 1
            daemon.stdin.write(b'{"jsonrpc":"2.0","id":%d,"method":"ping"}\n' % request_id)
            daemon.stdin.flush()
            time.sleep(0.3)
            assert daemon.poll() is None  # any traffic keeps resetting the clock
    except BrokenPipeError:  # pragma: no cover - diagnostic path
        pytest.fail(f"daemon died mid-conversation: {_stderr_of(daemon).decode()!r}")
    daemon.stdin.close()
    assert daemon.wait(timeout=_WAIT) == 0  # EOF still ends it promptly


def test_close_after_zero_runs_forever(project: Path) -> None:
    daemon = _spawn(project, env_extra={"AISQUARE_SERVE_CLOSE_AFTER": "0"})
    assert daemon.stdin is not None
    time.sleep(3)  # total silence, well past the 1s windows the other tests use
    assert daemon.poll() is None  # the persistent-client opt-out holds
    daemon.stdin.close()
    assert daemon.wait(timeout=_WAIT) == 0


def test_close_after_flag_beats_env(project: Path) -> None:
    daemon = _spawn(
        project,
        env_extra={"AISQUARE_SERVE_CLOSE_AFTER": "0"},
        args=["--close-after", "1"],
    )
    assert daemon.wait(timeout=_WAIT) == 0  # the flag's 1s deadline fired despite env=0


def test_eof_still_exits_immediately(project: Path) -> None:
    daemon = _spawn(project, env_extra={})  # default 300s deadline
    assert daemon.stdin is not None
    daemon.stdin.close()
    start = time.monotonic()
    assert daemon.wait(timeout=_WAIT) == 0
    assert time.monotonic() - start < 10  # EOF path, nowhere near the deadline
