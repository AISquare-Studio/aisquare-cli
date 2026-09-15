"""The voice path over a REAL uvicorn socket: the two properties ``TestClient`` cannot see.

``tests/test_xr_server.py`` drives the ASGI app through ``starlette.testclient``,
which is the right instrument for frames and routing and the wrong one for two
findings, because it is not a network transport:

- its ``send`` after the client has gone does not raise, so "a client leaving
  mid-decode raises ``WebSocketDisconnect`` out of the ASGI app and the
  sentence is never routed" (measured under uvicorn 0.52/0.53 as ``ERROR:
  Exception in ASGI application`` followed by ``Cannot call send once a close
  message has been sent``) is invisible to it; mutating ``_send_frame`` to
  raise on a dead client passes that whole file;
- it sends no keepalive pings, so "a model load inline in the read loop stops
  the socket being read, the pongs go unread and the keepalive closes it with
  1011" (measured: 4 of 5 clients at 40 s with the 20 s/20 s defaults) is
  invisible to it too.

So these run uvicorn in a thread on a loopback ephemeral port and connect with
the ``websockets`` client, which answers pings on its own. Both dependencies are
in the ``[dev]`` extra. The ping interval is shortened to make the keepalive
case take a second rather than forty; the mechanism is the same one.
"""

from __future__ import annotations

import contextlib
import json
import logging
import socket
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from aisquare.core.orchestrator import team_project
from aisquare.core.store import store_session
from aisquare.models import ProjectInfo, TeamSession

pytest.importorskip("uvicorn", reason="the [xr] extra is not installed")
pytest.importorskip("websockets", reason="the [xr] extra is not installed")

import uvicorn
from websockets.sync.client import ClientConnection, connect

from aisquare.services import mcp_server
from aisquare.services import team as team_service
from aisquare.services.xr import server as xr_server
from aisquare.services.xr.speech import FakeTranscriber

CODER = "bbbb2222-0000-0000-0000-000000000000"
WAIT_S = 15.0
"""A bound that turns a hang into a failure; nothing here takes more than a second."""


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("AISQUARE_XR_POLL_MS", "20")
    return work


def _seed(work: Path) -> ProjectInfo:
    project = team_project(work)
    now = datetime.now(tz=UTC)
    with store_session() as store:
        store.ensure_project(project)
        store.upsert_session(
            TeamSession(
                id=CODER, project_id=project.id, role="coder", started_at=now, last_seen_at=now
            )
        )
    team_service.activate(project.root)
    return project


class _Live:
    """One uvicorn server on a loopback port, stopped on exit."""

    def __init__(
        self,
        project: ProjectInfo,
        token: str,
        factory: Any,
        *,
        ws_ping_interval: float | None = None,
        ws_ping_timeout: float | None = None,
    ) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = int(probe.getsockname()[1])
        app = xr_server.build_app(project, token=token, transcriber_factory=factory)
        # log_config=None: uvicorn's own config would give its loggers handlers
        # and stop propagation, and the assertion these tests make is on what
        # reaches the ROOT logger — an "Exception in ASGI application" record.
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
            log_config=None,
            ws_ping_interval=ws_ping_interval,
            ws_ping_timeout=ws_ping_timeout,
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, name="xr-live", daemon=True)
        self.token = token

    def __enter__(self) -> _Live:
        self.thread.start()
        deadline = time.monotonic() + WAIT_S
        while not self.server.started:
            if time.monotonic() > deadline or not self.thread.is_alive():
                raise AssertionError("uvicorn never started")
            time.sleep(0.01)
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(WAIT_S)
        assert not self.thread.is_alive(), "uvicorn did not stop"

    @contextlib.contextmanager
    def authed(self) -> Iterator[ClientConnection]:
        with connect(f"ws://127.0.0.1:{self.port}/ws", open_timeout=WAIT_S) as ws:
            ws.send(json.dumps({"t": "auth", "token": self.token}))
            assert json.loads(ws.recv(timeout=WAIT_S))["t"] == "hello"
            assert json.loads(ws.recv(timeout=WAIT_S))["t"] == "snapshot"
            yield ws


def _until(ws: ClientConnection, kind: str) -> dict[str, Any]:
    for _ in range(40):
        frame = dict(json.loads(ws.recv(timeout=WAIT_S)))
        if frame.get("t") == kind:
            return frame
    raise AssertionError(f"no {kind} frame arrived")


class _HeldFinish:
    """A transcriber whose ``finish`` blocks until the test releases it."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def feed(self, pcm: bytes) -> str | None:
        return None

    def finish(self) -> str:
        self.entered.set()
        self.release.wait(WAIT_S)
        return "routed after the headset left"

    def discard(self) -> None:
        return None


def test_a_client_that_leaves_during_the_final_decode_is_routed_and_logs_no_error(
    work_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Over a real socket: the headset closes while ``finish`` runs; no traceback, one note.

    Before the worker routed first and sends went quiet, this exact sequence
    produced uvicorn's "Exception in ASGI application" (``WebSocketDisconnect``
    from the final ``stt`` send), a second traceback from the error frame the
    read loop answered it with, and no board note — the operator's committed
    sentence was lost while a typed prompt in the same situation was delivered.
    """
    project = _seed(work_dir)
    held = _HeldFinish()
    with (
        caplog.at_level(logging.INFO),
        _Live(project, mcp_server.serve_token(), lambda: held) as live,
    ):
        with live.authed() as ws:
            ws.send(json.dumps({"t": "audio", "session": CODER, "seq": 0}))
            ws.send(b"\x00" * 640)
            ws.send(json.dumps({"t": "audioEnd", "session": CODER}))
            assert held.entered.wait(WAIT_S), "audioEnd never reached finish"
        # The socket is closed here, while finish() is still held.
        held.release.set()
        deadline = time.monotonic() + WAIT_S
        while time.monotonic() < deadline:
            with store_session() as store:
                texts = [event.text for event in store.recent_events(project.id, limit=10)]
            if "routed after the headset left" in texts:
                break
            time.sleep(0.02)
        assert "routed after the headset left" in texts, "the committed sentence was never routed"

    errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert not errors, [record.getMessage() for record in errors]
    quiet = [r for r in caplog.records if r.name == "aisquare.services.xr.server"]
    assert all(r.levelno <= logging.INFO for r in quiet), [r.getMessage() for r in quiet]


def test_the_socket_survives_a_model_load_longer_than_the_keepalive(work_dir: Path) -> None:
    """The read loop keeps reading — and answering pings — while the factory runs.

    ``ws_ping_interval``/``ws_ping_timeout`` are 0.1 s here against a 1 s
    factory, which is the 20 s/20 s defaults against a 45 s model download at
    a two-hundredth of the wall time. With the factory awaited inline in the
    read loop this closed with 1011 "keepalive ping timeout" at two ping
    intervals; the burst is answered instead, on a socket that is still open.
    """
    project = _seed(work_dir)
    entered = threading.Event()

    def slow_factory() -> FakeTranscriber:
        entered.set()
        time.sleep(1.0)
        return FakeTranscriber("heard after the load")

    with (
        _Live(
            project,
            mcp_server.serve_token(),
            slow_factory,
            ws_ping_interval=0.1,
            ws_ping_timeout=0.1,
        ) as live,
        live.authed() as ws,
    ):
        ws.send(json.dumps({"t": "audio", "session": CODER, "seq": 0}))
        ws.send(b"\x00" * 640)
        assert entered.wait(WAIT_S), "the header never reached the factory"
        time.sleep(1.2)  # well past ping interval + timeout, with the load in flight
        ws.send(json.dumps({"t": "audioEnd", "session": CODER}))
        final = _until(ws, "stt")
        while final["final"] is False:
            final = _until(ws, "stt")
        ack = _until(ws, "ack")

    assert final["text"] == "heard after the load"
    assert ack["ok"] is True, ack
