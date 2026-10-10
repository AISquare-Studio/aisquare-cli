"""``serve`` ended by a SIGTERM or a SIGHUP takes its way out, and a missed auto-off is run late.

uvicorn put back the handlers it found and raised each signal it caught again, so a
SIGTERM killed ``serve`` inside ``run``, before its way out, and a SIGHUP, which uvicorn does
not catch, killed it at once: a deadline that had passed revoked no device, the deadline
stayed in ``remote.json``, and the ngrok reminder was never said. The fleet UI dies of both
at once too. A deadline that passed with no Remote to keep it now signs every phone out when
Remote next comes on (sweep 5 of #243).
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO

import httpx
import pytest

from aisquare.core.paths import remote_state_path
from aisquare.services import remote_server
from tests.remote_kit_helpers import PASSWORD, make_runtime


def _wait_for(done: Callable[[], bool], seconds: float = 20.0) -> None:
    deadline = time.monotonic() + seconds
    while not done():
        assert time.monotonic() < deadline, f"not done in {seconds} s"
        time.sleep(0.05)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _lines_of(stream: IO[str]) -> list[str]:
    lines: list[str] = []

    def read() -> None:
        for line in stream:
            lines.append(line.rstrip("\n"))

    threading.Thread(target=read, daemon=True).start()
    return lines


def _on_disk() -> dict[str, object]:
    state = json.loads(remote_state_path().read_bytes())
    return {"devices": [row["id"] for row in state["devices"]], "auto_off_at": state["auto_off_at"]}


@pytest.mark.skipif(os.name == "nt", reason="SIGTERM and SIGHUP sent to a child are POSIX's")
@pytest.mark.parametrize("ended_by", ["SIGTERM", "SIGHUP"])
@pytest.mark.parametrize("deadline", ["passed", "to come"])
def test_serve_ended_by_a_signal_takes_its_way_out_and_exits_as_the_signal_says(
    isolated_home: Path, ended_by: str, deadline: str
) -> None:
    """The real ``serve``, a phone unlocked, its deadline moved into the past as a machine
    that slept past it finds it, and the signal sent before its 30 s check: auto-off signs
    the phone out. With the deadline to come, the phone stays signed in, as after a
    Ctrl-C. Either way the deadline is cleared and the ngrok reminder said."""
    runtime = make_runtime()
    port = _free_port()
    child = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "aisquare",
            "remote",
            "serve",
            "--port",
            str(port),
            "--auto-off",
            "30",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None and child.stderr is not None
    _lines_of(child.stdout)
    err = _lines_of(child.stderr)
    try:
        _wait_for(lambda: any(line.startswith("Remote Control on") for line in err))
        origin = f"http://127.0.0.1:{port}"
        with httpx.Client(trust_env=False, headers={"origin": origin}) as phone:
            unlocked = phone.post(
                f"{origin}/r/{runtime.token}/api/unlock", json={"password": PASSWORD}
            )
            assert unlocked.status_code == 200, unlocked.text
            device = unlocked.json()["device"]["id"]
        if deadline == "passed":
            runtime.set_auto_off(datetime.now(UTC) - timedelta(minutes=1))
        child.send_signal(getattr(signal, ended_by))
        code = child.wait(timeout=30)
    finally:
        child.kill()
        child.wait()
    assert code == 128 + getattr(signal, ended_by), err
    kept = [] if deadline == "passed" else [device]
    assert _on_disk() == {"devices": kept, "auto_off_at": None}
    assert any(line.startswith("Remote is off — stop the ngrok") for line in err), err
    ran_out = any("the auto-off timer ran out" in line for line in err)
    assert ran_out is (deadline == "passed"), err


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="a hangup is POSIX's")
def test_serve_under_nohup_outlives_the_hangup_it_was_started_to_outlive(
    isolated_home: Path,
) -> None:
    """``nohup`` ignores SIGHUP so that closing the terminal leaves the process running; a
    handler for it, installed whatever ``serve`` found, stopped Remote at that very close.
    Ignored when ``serve`` started, a hangup changes nothing, and a SIGTERM still ends it."""
    runtime = make_runtime()
    port = _free_port()
    child = subprocess.Popen(
        [sys.executable, "-m", "aisquare", "remote", "serve", "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_IGN),  # what nohup does
    )
    assert child.stdout is not None and child.stderr is not None
    _lines_of(child.stdout)
    err = _lines_of(child.stderr)
    try:
        _wait_for(lambda: any(line.startswith("Remote Control on") for line in err))
        child.send_signal(signal.SIGHUP)
        time.sleep(1.0)
        assert child.poll() is None, err
        origin = f"http://127.0.0.1:{port}"
        with httpx.Client(trust_env=False, headers={"origin": origin}) as phone:
            unlocked = phone.post(
                f"{origin}/r/{runtime.token}/api/unlock", json={"password": PASSWORD}
            )
        assert unlocked.status_code == 200, unlocked.text
        child.send_signal(signal.SIGTERM)
        code = child.wait(timeout=30)
    finally:
        child.kill()
        child.wait()
    assert code == 128 + signal.SIGTERM, err


def test_a_deadline_that_passed_with_no_remote_to_keep_it_signs_every_phone_out_at_the_next_start(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A Remote killed, or a fleet UI ended by a SIGTERM or a SIGHUP, took no way out: its
    deadline stayed, past now, and its phones signed in. The next start set a deadline of its
    own and let them all in, for the 7 days a device lives."""
    monkeypatch.setattr(remote_server, "_runtime", None)
    monkeypatch.setattr(remote_server, "_server", None)
    runtime = make_runtime()
    assert runtime.unlock_device(PASSWORD, "phone") is not None
    runtime.set_auto_off(datetime.now(UTC) - timedelta(minutes=5))
    with caplog.at_level("WARNING", logger="aisquare.services.remote_server"):
        remote_server.start_remote_server(port=_free_port())
    try:
        assert _on_disk() == {"devices": [], "auto_off_at": None}
        assert any("auto-off time passed" in record.getMessage() for record in caplog.records)
    finally:
        remote_server.stop_remote_server()


def test_a_deadline_still_to_come_at_a_start_keeps_every_phone(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Remote that went before its deadline keeps its phones, as a Ctrl-C does."""
    monkeypatch.setattr(remote_server, "_runtime", None)
    monkeypatch.setattr(remote_server, "_server", None)
    runtime = make_runtime()
    made = runtime.unlock_device(PASSWORD, "phone")
    assert made is not None
    later = datetime.now(UTC) + timedelta(minutes=5)
    runtime.set_auto_off(later)
    remote_server.start_remote_server(port=_free_port())
    try:
        on_disk = _on_disk()
        assert on_disk["devices"] == [made[1].id] and on_disk["auto_off_at"] is not None
    finally:
        remote_server.stop_remote_server()
