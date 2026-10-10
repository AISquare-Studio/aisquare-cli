"""A phone that stops reading its socket holds neither its stream nor Remote's turning off.

A sleeping phone, or one whose network dropped, takes nothing more from its socket, and
once the kernel's buffers are full the server's sends wait for it. The stream's send and
close waited with no end, so its auto-off, revoke and sign-out checks stopped with it; and
uvicorn's stop closes a connection and waits for it to go, which a close does only once
what was written drains. So ``serve`` never exited after its auto-off, and the R panel's
next start was refused as winding down for as long as the phone's TCP lived (sweep 5 of
#243). These run the real server on loopback, with a client that never reads.
"""

from __future__ import annotations

import base64
import contextlib
import itertools
import json
import os
import socket
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from aisquare.services import remote_server
from aisquare.services.remote_server import Sources
from tests.remote_kit_helpers import PASSWORD

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="how much Windows' loopback buffers for a client that never reads"
)

ROWS = ["x" * 200] * 400
"""A screen of 80 KB: eight of them a tick fill loopback's buffers in a moment."""


def _wait_for(done: Callable[[], bool], seconds: float = 15.0) -> None:
    deadline = time.monotonic() + seconds
    while not done():
        assert time.monotonic() < deadline, f"not done in {seconds} s"
        time.sleep(0.02)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _sources() -> Sources:
    """Panes whose every capture is a new screen, so each tick sends every one again."""
    captures = itertools.count()
    return Sources(
        projects=lambda: [],
        fleet=lambda project: {"agents": []},
        board=lambda project: {},
        tasks=lambda project: [],
        memory=lambda project: [],
        panes=lambda agent, project, history: {
            "rows": [str(next(captures)), *ROWS],
            "width": 200,
            "height": len(ROWS) + 1,
        },
        explainability=lambda agent, project: {"available": False},
    )


@pytest.fixture
def remote(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """This module's Remote, over panes that change every 20 ms, its stops cut short."""
    monkeypatch.setattr(remote_server, "_runtime", None)
    monkeypatch.setattr(remote_server, "_server", None)
    monkeypatch.setattr(remote_server, "_winding_down", [])
    monkeypatch.setattr(remote_server, "REMOTE_WINDING_DOWN_SECONDS", 0.2)
    build = remote_server.build_remote_app

    def built(state: remote_server.Runtime, *, dist_dir: Path | None = None) -> Any:
        return build(state, sources=_sources(), dist_dir=dist_dir, tick=0.02)

    monkeypatch.setattr(remote_server, "build_remote_app", built)
    state = remote_server.runtime()
    state._state.password = PASSWORD
    state._save_state()
    yield
    remote_server.stop_remote_server()


def _unlocked(port: int, token: str) -> tuple[str, str]:
    """A device's id and its cookie, unlocked over HTTP."""
    origin = f"http://127.0.0.1:{port}"
    with httpx.Client(trust_env=False, headers={"origin": origin}) as phone:
        answer = phone.post(f"{origin}/r/{token}/api/unlock", json={"password": PASSWORD})
        assert answer.status_code == 200, answer.text
        return answer.json()["device"]["id"], phone.cookies[remote_server.COOKIE]


def _client_frame(text: str) -> bytes:
    """One masked text frame, as a browser sends it."""
    payload = text.encode()
    assert len(payload) < 126
    mask = os.urandom(4)
    return (
        bytes([0x81, 0x80 | len(payload)])
        + mask
        + bytes(byte ^ mask[n % 4] for n, byte in enumerate(payload))
    )


@contextlib.contextmanager
def _phone_that_stopped_reading(port: int, token: str, cookie: str) -> Iterator[socket.socket]:
    """A socket subscribed to eight panes that reads nothing past its handshake."""
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    sock.connect(("127.0.0.1", port))
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall(
        (
            f"GET /r/{token}/ws HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
            f"Origin: http://127.0.0.1:{port}\r\nCookie: {remote_server.COOKIE}={cookie}\r\n\r\n"
        ).encode()
    )
    head = b""
    while not head.endswith(b"\r\n\r\n"):
        head += sock.recv(1)  # one byte at a time: no frame is read past the handshake
    assert head.startswith(b"HTTP/1.1 101"), head
    for label in (f"coder-{n}" for n in range(8)):
        sock.sendall(_client_frame(json.dumps({"subscribe": label})))
    try:
        yield sock
    finally:
        sock.close()


def _held_up(server: Any) -> bool:
    """Whether one of ``server``'s connections holds bytes its peer has not taken."""
    return any(
        connection.transport.get_write_buffer_size() > 0
        for connection in list(server.server_state.connections)
    )


def test_a_phone_that_stops_reading_ends_its_stream_instead_of_holding_it(
    remote: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its send waited for good, and with it the checks that close the socket (4401 once
    the device goes, 4410 at auto-off) and the needs watcher's count of who is watching."""
    monkeypatch.setattr(remote_server, "WS_SEND_SECONDS", 0.5)
    port = _free_port()
    info = remote_server.start_remote_server(port=port)
    device, cookie = _unlocked(port, info.token)
    server = remote_server._server
    assert server is not None
    app = server._server.config.app
    assert isinstance(app, remote_server._TokenGate)
    kit = app.kit
    with _phone_that_stopped_reading(port, info.token, cookie):
        _wait_for(lambda: device in kit.sockets)
        _wait_for(lambda: device not in kit.sockets)


@pytest.mark.parametrize("stopped_by", ["the R panel", "serve's auto-off"])
def test_a_phone_that_stopped_reading_lets_remote_go_off_and_on_again(
    remote: None, stopped_by: str
) -> None:
    """Stopped while the phone holds its socket and reads nothing: the R panel's server winds
    down and the next start is let in, and ``serve`` returns."""
    port = _free_port()
    if stopped_by == "the R panel":
        info = remote_server.start_remote_server(port=port)
        token, serving = info.token, remote_server._server
        assert serving is not None
        uvicorn_server = serving._server
    else:
        token = remote_server.runtime().token
        ended: list[bool] = []
        foreground = threading.Thread(
            target=lambda: ended.append(remote_server.run_foreground(port=port)), daemon=True
        )
        foreground.start()
        _wait_for(lambda: remote_server._foreground is not None)
        serving_now = remote_server._foreground
        assert serving_now is not None
        uvicorn_server = serving_now
        _wait_for(lambda: uvicorn_server.started)
    _device, cookie = _unlocked(port, token)
    with _phone_that_stopped_reading(port, token, cookie):
        _wait_for(lambda: _held_up(uvicorn_server))
        if stopped_by == "the R panel":
            remote_server.stop_remote_server()
            _wait_for(
                lambda: not any(stopped.winding_down for stopped in remote_server._winding_down)
            )
            again = remote_server.start_remote_server(port=_free_port())
            assert again.token == token, "on again, its phone still holding the old socket"
        else:
            uvicorn_server.should_exit = True  # what auto-off does (_remote_serve_off)
            foreground.join(15)
            assert not foreground.is_alive(), "serve never returned"
            assert ended == [False]
