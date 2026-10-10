"""Quitting while a phone's write still runs says so and waits for it, and Ctrl-C quits at once.

A write runs in a worker thread, and Python waits for every worker thread at exit
(``concurrent.futures`` joins them all, daemon or not). Quitting the fleet UI while a
phone's restart ran left the process alive after the UI was gone, silently, for as
long as the restart took (up to 40 s), and a Ctrl-C there printed a traceback and cut
the restart wherever it was. ``serve``'s first Ctrl-C waited as silently, and its
second printed a traceback and changed nothing (review of #243, the sweep after
round 3). The wait itself is right: a restart cut between its ``/exit`` and its
spawn leaves the agent down. So it is said, and a Ctrl-C ends it at once.
"""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import IO, Any

import httpx
import pytest

from aisquare.cli.ui import app as app_module
from aisquare.services import remote_server
from aisquare.services.remote_server import Sources, Writes, build_app
from tests.remote_kit_helpers import PASSWORD, base, make_client, make_runtime, unlock


def _wait_for(done: Callable[[], bool], seconds: float = 10.0) -> None:
    deadline = time.monotonic() + seconds
    while not done():
        assert time.monotonic() < deadline, f"not done in {seconds} s"
        time.sleep(0.02)


def _sources() -> Sources:
    return Sources(
        projects=lambda: [],
        fleet=lambda project: {"agents": []},
        board=lambda project: {},
        tasks=lambda project: [],
        memory=lambda project: [],
        panes=lambda agent, project, history: {"rows": [], "width": 0, "height": 0},
        explainability=lambda agent, project: {"available": False},
    )


@pytest.fixture
def said(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """What Remote says on stderr, line by line."""
    lines: list[str] = []
    monkeypatch.setattr(remote_server, "_remote_say", lines.append)
    return lines


@pytest.fixture
def quit_now(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[int]]:
    """``os._exit``, recorded instead and raised as ``SystemExit`` so the test goes on."""
    codes: list[int] = []

    def recorded(code: int) -> None:
        codes.append(code)
        raise SystemExit(code)

    monkeypatch.setattr(os, "_exit", recorded)
    monkeypatch.setattr(remote_server, "_quitting", False)
    yield codes


# --- which writes are running ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("agent", "named"),
    [("coder-1", "agent/restart for coder-1"), ("\x1b]0;owned\x07", "agent/restart")],
    ids=["a label", "a control character, never printed to the terminal"],
)
def test_a_write_is_running_for_as_long_as_its_thread_runs_it(
    isolated_home: Path, tmp_path: Path, agent: str, named: str
) -> None:
    started, release = threading.Event(), threading.Event()

    def restart(body: dict[str, Any]) -> tuple[dict[str, object], str]:
        started.set()
        release.wait(timeout=10)
        return {"restarted": True}, "agent/restart"

    runtime = make_runtime()
    runtime.set_allow_write(True)
    writes = Writes({"agent/restart": restart})
    app = build_app(runtime, sources=_sources(), writes=writes, dist_dir=tmp_path)
    client = make_client(app)
    assert unlock(client, runtime).status_code == 200
    url = f"{base(runtime)}/api/agent/restart"
    with ThreadPoolExecutor(max_workers=1) as pool:
        posted = pool.submit(client.post, url, json={"agent": agent})
        try:
            assert started.wait(timeout=5)
            assert remote_server.remote_writes_running() == [named]
        finally:
            release.set()
        assert posted.result(timeout=5).status_code == 200
    assert remote_server.remote_writes_running() == []


# --- the fleet UI's quit -----------------------------------------------------------------------


def test_a_quit_with_no_write_running_waits_for_nothing_and_says_nothing(said: list[str]) -> None:
    began = time.monotonic()
    remote_server.remote_wait_for_writes()
    assert said == [] and time.monotonic() - began < 0.5


def test_a_quit_says_which_write_it_waits_for_and_waits_for_it(said: list[str]) -> None:
    release = threading.Event()

    def restart() -> None:
        with remote_server._remote_write_running("agent/restart for coder-1"):
            release.wait(timeout=10)

    writer = threading.Thread(target=restart)
    writer.start()
    _wait_for(lambda: remote_server.remote_writes_running() != [])
    threading.Timer(0.3, release.set).start()
    remote_server.remote_wait_for_writes()
    assert release.is_set(), "it returned before the write was done"
    writer.join(5)
    assert said == [
        "waiting for agent/restart for coder-1 to finish (a restart or switch can take 40 s); "
        "Ctrl-C quits now and leaves it unfinished"
    ]


def test_a_ctrl_c_while_a_quit_waits_quits_at_once_and_says_what_it_left(
    said: list[str], quit_now: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Python's own exit would have waited for the write anyway, and printed a traceback for
    the Ctrl-C: only ``os._exit`` leaves at once."""
    release = threading.Event()

    def interrupted(seconds: float) -> None:
        raise KeyboardInterrupt

    def restart() -> None:
        with remote_server._remote_write_running("agent/switch for coder-2"):
            release.wait(timeout=10)

    writer = threading.Thread(target=restart)
    writer.start()
    try:
        _wait_for(lambda: remote_server.remote_writes_running() != [])
        # The wait's own sleep, and no one else's, is where the Ctrl-C lands.
        monkeypatch.setattr(
            remote_server, "time", SimpleNamespace(sleep=interrupted, monotonic=time.monotonic)
        )
        with pytest.raises(SystemExit):
            remote_server.remote_wait_for_writes()
    finally:
        release.set()
        writer.join(5)
    assert quit_now == [130]
    assert said[-1] == (
        "Remote quit with agent/switch for coder-2 unfinished: "
        "`aisquare fleet ls` shows where the agent is"
    )


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_quitting_the_fleet_ui_mid_restart_says_so_and_the_phone_still_gets_its_answer(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, said: list[str]
) -> None:
    """The real server, stopped as quit stops it, a phone's restart still running: the stop
    returns after its 5 s, and the rest is the wait ``run_ui`` makes."""
    monkeypatch.setattr(remote_server, "_runtime", None)
    started, release = threading.Event(), threading.Event()

    def restart(body: dict[str, Any]) -> tuple[dict[str, object], str]:
        started.set()
        release.wait(timeout=20)
        return {"restarted": True}, "agent/restart coder-1"

    monkeypatch.setattr(remote_server, "live_writes", lambda: Writes({"agent/restart": restart}))
    stop_serving = remote_server._Server.stop_serving
    monkeypatch.setattr(  # its 5 s, cut to a fraction: the write outlasts it either way
        remote_server._Server, "stop_serving", lambda self, timeout=5.0: stop_serving(self, 0.2)
    )
    state = remote_server.runtime()
    state._state.password = PASSWORD
    state._save_state()
    state.set_allow_write(True)
    port = _free_port()
    info = remote_server.start_remote_server(port=port)
    origin = f"http://127.0.0.1:{port}"
    url = info.url_local.rstrip("/")
    answers: list[httpx.Response] = []
    with httpx.Client(trust_env=False, headers={"origin": origin}, timeout=30) as phone:
        assert phone.post(f"{url}/api/unlock", json={"password": PASSWORD}).status_code == 200

        def ask() -> None:
            answers.append(phone.post(f"{url}/api/agent/restart", json={"agent": "coder-1"}))

        asking = threading.Thread(target=ask)
        asking.start()
        try:
            assert started.wait(timeout=10)
            remote_server.stop_remote_server()
            assert remote_server.remote_writes_running() == ["agent/restart for coder-1"]
            threading.Timer(0.5, release.set).start()
            remote_server.remote_wait_for_writes()
        finally:
            release.set()
            asking.join(10)
    assert said and said[0].startswith("waiting for agent/restart for coder-1 to finish")
    assert [answer.status_code for answer in answers] == [200], "the answer never went out"
    assert remote_server._winding_down == []


def test_run_ui_waits_for_the_writes_once_the_fleet_ui_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    class Quit:
        """A fleet UI that quits at once, its Remote off already."""

        def __init__(self, **options: object) -> None:
            self.unsaved: list[str] = []
            self.remote = SimpleNamespace(wait_until_off=lambda timeout=None: True)

        def run(self) -> None:
            order.append("the UI is gone")

    monkeypatch.setattr(app_module, "FleetApp", Quit)
    monkeypatch.setattr(remote_server, "remote_wait_for_writes", lambda: order.append("writes"))
    app_module.run_ui()
    assert order == ["the UI is gone", "writes"]


def test_a_ctrl_c_while_quit_waits_for_remote_to_stop_quits_at_once_and_stops_ngrok_first(
    said: list[str],
    quit_now: list[int],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Quit waits for Remote's server and ngrok to stop before it waits for the writes, and a
    phone's write still running is what holds that stop up: uvicorn waits for its request. A
    Ctrl-C there raised out of run_ui, Click said "Aborted!", and Python's exit then waited
    for the write all the same, silently. It quits at once, as it does once the stop is done:
    what the quit could not save is said first, and the ngrok the stopping thread had not
    reached yet is stopped before that thread ends with the process."""
    from aisquare.cli.ui.remote_control import RemoteController
    from tests.test_remote_control import FakeTunnel, SlowServer, fake_tunnel_factory

    server = SlowServer(patience=10.0)
    remote = RemoteController(server=server, tunnel_factory=fake_tunnel_factory(url="x"))
    remote.turn_on()
    tunnel = remote.tunnel
    assert isinstance(tunnel, FakeTunnel)
    waited = remote.wait_until_off

    def ctrl_c_in_the_wait(timeout: float | None = None) -> bool:
        if timeout is None:  # the wait with no end, where the Ctrl-C lands
            raise KeyboardInterrupt
        return waited(timeout)

    monkeypatch.setattr(remote, "wait_until_off", ctrl_c_in_the_wait)

    class Quit:
        """A fleet UI whose Remote is still stopping as it quits, a phone's write running."""

        def __init__(self, **options: object) -> None:
            self.unsaved = ["the theme was not saved: state.json.lock is held by another process"]
            self.remote = remote

        def run(self) -> None:
            self.remote.shutdown_for_exit(wait=False)  # what on_unmount does

    monkeypatch.setattr(app_module, "FleetApp", Quit)
    try:
        with remote_server._remote_write_running("agent/restart for coder-1"):
            try:
                app_module.run_ui()
            except KeyboardInterrupt:
                pytest.fail("the Ctrl-C raised out of run_ui: Aborted!, then a silent wait")
            except SystemExit:
                pass
        assert quit_now == [130]
        assert server.stopping.is_set() and not server.release.is_set(), "still stopping"
        assert tunnel.stopped, "ngrok is stopped before the process ends with its stopper"
        assert said[-1] == (
            "Remote quit with agent/restart for coder-1 unfinished: "
            "`aisquare fleet ls` shows where the agent is"
        )
        err = capsys.readouterr().err
        assert "⚠ the theme was not saved: state.json.lock is held by another process" in err
    finally:
        server.release.set()
        assert waited(5)


# --- serve's Ctrl-C ----------------------------------------------------------------------------


def test_serves_ctrl_c_says_what_it_waits_for_and_a_second_one_quits_only_mid_write(
    said: list[str], quit_now: list[int]
) -> None:
    """With no write running, the second Ctrl-C stays uvicorn's own, so the way out still
    clears the deadline and saves ``last_seen``; mid-write it quits at once."""
    import uvicorn

    async def unserved(scope: Any, receive: Any, send: Any) -> None:
        """An app for the config: these servers are signalled, never run."""

    def serve() -> Any:
        return remote_server._remote_serve_server(uvicorn.Config(app=unserved))

    idle = serve()
    idle.handle_exit(signal.SIGINT, None)
    idle.handle_exit(signal.SIGINT, None)
    assert (idle.should_exit, idle.force_exit) == (True, True)
    assert said == [] and quit_now == []

    busy = serve()
    with remote_server._remote_write_running("agent/restart for coder-1"):
        busy.handle_exit(signal.SIGINT, None)
        assert said == [
            "waiting for agent/restart for coder-1 to finish (a restart or switch can take "
            "40 s); Ctrl-C again quits now and leaves it unfinished"
        ]
        with pytest.raises(SystemExit):
            busy.handle_exit(signal.SIGINT, None)
    assert quit_now == [130]
    assert said[-1].startswith("Remote quit with agent/restart for coder-1 unfinished")


def test_a_second_ctrl_c_landing_while_the_first_reads_the_writes_does_not_hang(
    said: list[str], quit_now: list[int]
) -> None:
    """Python runs a signal handler in the main thread between two bytecodes, so the second
    Ctrl-C's handler can run while the first one's holds the writes' lock: a lock that is
    not re-entrant would hang ``serve`` there for good."""
    import uvicorn

    async def unserved(scope: Any, receive: Any, send: Any) -> None:
        """An app for the config: this server is signalled, never run."""

    server = remote_server._remote_serve_server(uvicorn.Config(app=unserved))
    with remote_server._writes_lock:  # the first handler, interrupted holding it
        assert remote_server._writes_lock.acquire(timeout=1), "the second handler would hang"
        remote_server._writes_lock.release()
        server.handle_exit(signal.SIGINT, None)
    assert server.should_exit is True and quit_now == []


def test_serves_auto_off_says_which_write_its_way_out_waits_for(
    said: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Told to stop by the timer, the server takes the next Ctrl-C as its second, which
    quits at once mid-write: said before it can be pressed."""
    monkeypatch.setattr(remote_server, "revoke_every_remote_device", lambda reason: None)
    state = SimpleNamespace(set_auto_off=lambda deadline: None)
    idle, busy = SimpleNamespace(should_exit=False), SimpleNamespace(should_exit=False)
    remote_server._remote_serve_off(state, idle)  # type: ignore[arg-type]
    assert idle.should_exit is True and said == []
    with remote_server._remote_write_running("agent/switch for coder-2"):
        remote_server._remote_serve_off(state, busy)  # type: ignore[arg-type]
    assert busy.should_exit is True
    assert said == [
        "waiting for agent/switch for coder-2 to finish (a restart or switch can take 40 s); "
        "Ctrl-C quits now and leaves it unfinished"
    ]


def test_a_line_said_on_stderr_is_written_whole_and_a_stderr_that_is_gone_loses_only_it(
    capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    remote_server._remote_say("waiting for send-keys for coder-1 to finish")
    assert capfd.readouterr().err == "waiting for send-keys for coder-1 to finish\n"

    def gone(fd: int, data: bytes) -> int:
        raise OSError(9, "Bad file descriptor")

    monkeypatch.setattr(os, "write", gone)
    remote_server._remote_say("said to no one")


def test_a_line_is_said_in_the_encoding_the_terminal_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Written past ``sys.stderr``, the line was UTF-8 whatever the terminal read: a Windows
    console on its legacy code page showed an agent's ``é`` as two other characters."""
    written: list[bytes] = []

    def write(fd: int, data: bytes) -> int:
        written.append(data)
        return len(data)

    monkeypatch.setattr(os, "device_encoding", lambda fd: "cp437" if fd == 2 else None)
    monkeypatch.setattr(os, "write", write)
    remote_server._remote_say("waiting for agent/restart for écrivain to finish")
    assert written == ["waiting for agent/restart for écrivain to finish\n".encode("cp437")]


def test_what_quitting_says_is_plain_text_that_any_console_shows_as_it_is(
    said: list[str], quit_now: list[int]
) -> None:
    """Its em dashes are in no legacy console's code page but one."""
    with remote_server._remote_write_running("agent/restart for coder-1"):
        remote_server._remote_writes_announced("Ctrl-C")
        with pytest.raises(SystemExit):
            remote_server._remote_quit_now()
    assert len(said) == 2 and all(line.isascii() for line in said)


@pytest.fixture
def push_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[[float], threading.Event]]:
    """Put a one-shot push in flight that takes ``seconds`` to arrive; the event says it did."""
    from aisquare.services import remote_push

    flying: set[threading.Thread] = set()
    monkeypatch.setattr(remote_push, "_push_in_flight", flying)
    over = threading.Event()

    def send_for(seconds: float) -> threading.Event:
        arrived = threading.Event()

        def send() -> None:
            if not over.wait(seconds):
                arrived.set()

        thread = threading.Thread(target=send, name="asq-remote-push-now", daemon=True)
        flying.add(thread)
        thread.start()
        return arrived

    yield send_for
    over.set()


def test_a_quit_at_once_still_lets_a_farewell_in_flight_arrive(
    said: list[str], quit_now: list[int], push_in_flight: Callable[[float], threading.Event]
) -> None:
    """``os._exit`` skips the exit handler that waits for one (``push_drain``): ``serve``'s
    auto-off queued its farewell, said it waits for a write, and the Ctrl-C that followed
    cut the farewell off in its TLS handshake."""
    arrived = push_in_flight(0.3)
    with pytest.raises(SystemExit):
        remote_server._remote_quit_now()
    assert arrived.is_set() and quit_now == [130]


def test_a_push_that_does_not_arrive_holds_a_quit_at_once_for_moments_at_most(
    said: list[str],
    quit_now: list[int],
    push_in_flight: Callable[[float], threading.Event],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(remote_server, "REMOTE_QUIT_PUSH_SECONDS", 0.3)
    arrived = push_in_flight(60)
    began = time.monotonic()
    with pytest.raises(SystemExit):
        remote_server._remote_quit_now()
    assert 0.2 <= time.monotonic() - began < 3 and not arrived.is_set()


def test_a_ctrl_c_while_the_fleet_uis_quit_waits_for_a_push_ends_the_wait(
    said: list[str], quit_now: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the UI is gone, a Ctrl-C is a KeyboardInterrupt wherever it lands."""
    from aisquare.services import remote_push

    def interrupted(timeout: float) -> bool:
        raise KeyboardInterrupt

    monkeypatch.setattr(remote_push, "push_drain", interrupted)
    with pytest.raises(SystemExit):
        remote_server._remote_quit_now()
    assert quit_now == [130]


def test_a_ctrl_c_while_serves_quit_waits_for_a_push_ends_the_wait_and_says_nothing_again(
    said: list[str], quit_now: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``serve``'s next Ctrl-C runs its handler, which calls the quit again: that one leaves
    at once, where it would have said its line again and waited again."""
    from aisquare.services import remote_push

    def handled_again(timeout: float) -> bool:
        remote_server._remote_quit_now()
        raise AssertionError("the second quit returned")

    monkeypatch.setattr(remote_push, "push_drain", handled_again)
    with (
        remote_server._remote_write_running("agent/restart for coder-1"),
        pytest.raises(SystemExit),
    ):
        remote_server._remote_quit_now()
    assert quit_now[0] == 130
    assert said == [
        "Remote quit with agent/restart for coder-1 unfinished: "
        "`aisquare fleet ls` shows where the agent is"
    ]


CHILD = r"""
import sys, time
from aisquare.services import remote_server
from aisquare.services.remote_server import Writes

def restart(body):
    print("restarting", flush=True)
    time.sleep(60)
    return {"restarted": True}, "agent/restart coder-1"

remote_server.live_writes = lambda: Writes({"agent/restart": restart})
state = remote_server.runtime()
state._state.password = sys.argv[2]
state._save_state()
state.set_allow_write(True)
print("token", state.token, flush=True)
remote_server.run_foreground(port=int(sys.argv[1]), ready=lambda: print("ready", flush=True))
"""


def _lines_of(stream: IO[str]) -> list[str]:
    """Every line ``stream`` gives, as it gives it, read on a thread of its own."""
    lines: list[str] = []

    def read() -> None:
        for line in stream:
            lines.append(line.rstrip("\n"))

    threading.Thread(target=read, daemon=True).start()
    return lines


@pytest.mark.skipif(os.name == "nt", reason="a Ctrl-C sent to a child is a POSIX signal")
def test_serve_says_what_its_ctrl_c_waits_for_and_a_second_ctrl_c_quits_at_once(
    isolated_home: Path,
) -> None:
    port = _free_port()
    child = subprocess.Popen(
        [sys.executable, "-c", CHILD, str(port), PASSWORD],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None and child.stderr is not None
    out, err = _lines_of(child.stdout), _lines_of(child.stderr)
    try:
        _wait_for(lambda: "ready" in out)
        token = next(line.split()[1] for line in out if line.startswith("token "))
        url = f"http://127.0.0.1:{port}/r/{token}"
        with httpx.Client(trust_env=False, headers={"origin": f"http://127.0.0.1:{port}"}) as phone:
            assert phone.post(f"{url}/api/unlock", json={"password": PASSWORD}).status_code == 200
            cookies = dict(phone.cookies)

        def ask() -> None:
            with (
                contextlib.suppress(httpx.TransportError),  # cut off: the child quit mid-write
                httpx.Client(
                    trust_env=False, headers={"origin": f"http://127.0.0.1:{port}"}, cookies=cookies
                ) as other,
            ):
                other.post(f"{url}/api/agent/restart", json={"agent": "coder-1"}, timeout=90)

        threading.Thread(target=ask, daemon=True).start()
        _wait_for(lambda: "restarting" in out)
        child.send_signal(signal.SIGINT)
        waiting = "waiting for agent/restart for coder-1 to finish"
        _wait_for(lambda: any(waiting in line for line in err))
        again = time.monotonic()
        child.send_signal(signal.SIGINT)
        assert child.wait(timeout=20) == 130
        assert time.monotonic() - again < 10, "the second Ctrl-C waited for the write"
        assert any("quit with agent/restart for coder-1 unfinished" in line for line in err)
    finally:
        child.kill()
        child.wait()
