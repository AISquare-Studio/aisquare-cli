"""The cliXR transport end to end: static assets, token auth, frames, the CLI.

Driven through ``starlette.testclient``, which runs the real ASGI app — real
routing, a real websocket handshake, the real poll task — in a thread. The
board is mutated from the test thread through its own store handle, which is
exactly what a Claude Code hook does from its own process, so a delta here
proves the same path a headset sees.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import socket
import sqlite3
import threading
import time
from array import array
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core.orchestrator import team_project
from aisquare.core.paths import HOME_ENV_VAR
from aisquare.core.store import store_session
from aisquare.models import FleetAgent, ProjectInfo, TeamSession

pytest.importorskip("starlette.testclient", reason="the [xr] extra is not installed")

from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from aisquare.services import fleet as fleet_service
from aisquare.services import mcp_server
from aisquare.services.xr import protocol as wire
from aisquare.services.xr import server as xr_server
from aisquare.services.xr import speech
from aisquare.services.xr.speech import (
    BufferedTranscriber,
    FakeTranscriber,
    SpeechUnavailable,
    Transcriber,
)

CODER = "bbbb2222-0000-0000-0000-000000000000"
PLANNER = "aaaa1111-0000-0000-0000-000000000000"


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    # A board poll fast enough that a test waits on the event, not the clock.
    monkeypatch.setenv("AISQUARE_XR_POLL_MS", "20")
    return work


def _seed(work: Path, *, session_id: str = CODER, transcript: str | None = None) -> ProjectInfo:
    """A project with one live session, the minimum a ring needs."""
    project = team_project(work)
    now = datetime.now(tz=UTC)
    with store_session() as store:
        store.ensure_project(project)
        store.upsert_session(
            TeamSession(
                id=session_id,
                project_id=project.id,
                role="coder",
                started_at=now,
                last_seen_at=now,
                transcript_path=transcript,
            )
        )
    return project


@pytest.fixture
def client(work_dir: Path) -> Iterator[tuple[TestClient, ProjectInfo, str]]:
    project = _seed(work_dir)
    token = mcp_server.serve_token()
    with TestClient(xr_server.build_app(project, token=token)) as http:
        yield http, project, token


@contextlib.contextmanager
def _authed(http: TestClient, token: str) -> Iterator[Any]:
    """An open ``/ws``, authenticated, with the hello + snapshot pair drained."""
    with http.websocket_connect("/ws") as connection:
        connection.send_text(json.dumps({"t": "auth", "token": token}))
        hello = json.loads(_text(connection))
        snapshot = json.loads(_text(connection))
        assert hello["t"] == "hello"
        assert snapshot["t"] == "snapshot"
        yield connection


RECEIVE_TIMEOUT_S = 15.0
"""How long a test waits for a frame before calling it a failure.

Generous — the poll interval here is 20 ms — because this number exists to turn
a HANG into a failure, not to measure anything. ``receive_text`` blocks on a
queue with no timeout of its own, so a server that stops sending would otherwise
wedge the whole run rather than fail one test, and a wedged CI job is the one
failure mode nobody can read.
"""


def _text(connection: Any) -> str:
    """The next text frame, or an assertion failure. Never an unbounded wait.

    A bare DAEMON thread, not a ``ThreadPoolExecutor``: the executor's context
    manager exits through ``shutdown(wait=True)``, which joins the very thread
    that is stuck in ``receive_text`` — so the timeout fires, the assertion is
    raised, and the process then hangs anyway on the way out. Measured while
    proving the busy-store test below could fail: the mutated server sent
    nothing, and the run wedged instead of reporting. A daemon thread left
    blocked costs a strand in a test that is failing regardless, and never
    holds the interpreter.
    """
    received: list[str] = []

    def read() -> None:
        received.append(connection.receive_text())

    worker = threading.Thread(target=read, daemon=True)
    worker.start()
    worker.join(RECEIVE_TIMEOUT_S)
    if not received:
        raise AssertionError(
            f"no frame arrived within {RECEIVE_TIMEOUT_S:.0f}s — the server stopped sending"
        )
    return received[0]


# --- static assets --------------------------------------------------------------


def test_the_client_and_its_schema_are_served_from_package_data(
    client: tuple[TestClient, ProjectInfo, str],
) -> None:
    """Both are the files the wheel ships, resolved through importlib."""
    http, _project, _token = client
    index = http.get("/")
    assert index.status_code == 200
    assert "<div id=" in index.text
    assert index.headers["content-type"].startswith("text/html")

    schema = http.get("/protocol.schema.json")
    assert schema.status_code == 200
    assert schema.json()["protocol"] == 1


def test_a_path_outside_the_web_root_is_a_404_not_a_file(
    client: tuple[TestClient, ProjectInfo, str],
) -> None:
    http, _project, _token = client
    assert http.get("/../../../etc/passwd").status_code == 404
    assert http.get("/nothing-here.js").status_code == 404


# --- auth -----------------------------------------------------------------------


def _rejected(http: TestClient, first_frame: str | None) -> dict[str, Any]:
    """Send a bad opening frame; return the error the server answers with."""
    with http.websocket_connect("/ws") as connection:
        if first_frame is not None:
            connection.send_text(first_frame)
        else:
            connection.send_text(json.dumps({"t": "subscribe", "session": None}))
        return dict(json.loads(_text(connection)))


def test_a_rejected_token_is_auth_failed_however_it_is_wrong(
    work_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One answer — ``auth_failed`` — for every way a TOKEN can be wrong.

    The foreign case is the one worth having: a token is per ``AISQUARE_HOME``,
    so a client holding one minted against a different home — another machine,
    another checkout, an old install — is refused exactly like a guess. All of
    these are a token that was checked and found wrong, which is the terminal
    ``auth_failed`` case, told apart below from a handshake that never checked a
    token at all.
    """
    project = _seed(work_dir)
    mine = mcp_server.serve_token()

    monkeypatch.setenv(HOME_ENV_VAR, str(tmp_path / "other-home"))
    foreign = mcp_server.serve_token()
    monkeypatch.setenv(HOME_ENV_VAR, str(tmp_path / "aisquare-home"))
    assert foreign != mine, "two homes must not share a token"

    with TestClient(xr_server.build_app(project, token=mine)) as http:
        for frame in (
            json.dumps({"t": "auth", "token": ""}),
            json.dumps({"t": "auth", "token": "not-the-token"}),
            json.dumps({"t": "auth", "token": foreign}),
            # Non-ASCII: str-mode compare_digest raises TypeError on this, which
            # would leave _authenticate as an unhandled exception rather than a
            # refusal. The same trap `mcp_server._BearerGuard` documents.
            json.dumps({"t": "auth", "token": "tökèn-wíth-ümlauts-🔑"}),
        ):
            answer = _rejected(http, frame)
            assert answer["t"] == "error"
            assert answer["code"] == "auth_failed", frame


def test_a_pre_auth_failure_that_checked_no_token_is_retryable(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stalled or malformed handshake is a RETRYABLE close, not a rejected token.

    The old code answered every pre-auth failure with ``auth_failed`` + close
    4401 ``retry:false`` — including a first frame that was not an auth at all,
    and (below) a valid token that simply arrived after the timeout. A client
    following the contract then gave up permanently on a transient stall. These
    checked no token, so they are ``auth_timeout``/``auth_invalid`` and close
    :data:`CLOSE_AUTH_TIMEOUT` (4408), where reconnecting is right.
    """
    project = _seed(work_dir)
    token = mcp_server.serve_token()
    monkeypatch.setattr(xr_server, "AUTH_TIMEOUT_S", 0.05)

    def close_of(first_frame: str | None) -> tuple[str, int]:
        with (
            TestClient(xr_server.build_app(project, token=token)) as http,
            http.websocket_connect("/ws") as connection,
        ):
            if first_frame is not None:
                connection.send_text(first_frame)
            answer = json.loads(_text(connection))
            closing = connection.receive()
            assert closing["type"] == "websocket.close", closing
            return answer["code"], int(closing["code"])

    # A first frame that is not an auth message: no token was checked.
    code, close = close_of(json.dumps({"t": "subscribe", "session": None}))
    assert code == "auth_invalid" and close == wire.CLOSE_AUTH_TIMEOUT, (code, close)
    # Not even JSON.
    code, close = close_of("{not json")
    assert code == "auth_invalid" and close == wire.CLOSE_AUTH_TIMEOUT, (code, close)
    # Silence past the timeout — the transient stall the finding is about.
    code, close = close_of(None)
    assert code == "auth_timeout" and close == wire.CLOSE_AUTH_TIMEOUT, (code, close)


def test_a_wrong_token_still_closes_terminally(work_dir: Path) -> None:
    """The rejected-token close stays 4401 (``retry:false``): retrying is hopeless."""
    project = _seed(work_dir)
    with (
        TestClient(xr_server.build_app(project, token=mcp_server.serve_token())) as http,
        http.websocket_connect("/ws") as connection,
    ):
        connection.send_text(json.dumps({"t": "auth", "token": "not-the-token"}))
        answer = json.loads(_text(connection))
        closing = connection.receive()
    assert answer["code"] == "auth_failed"
    assert closing["type"] == "websocket.close", closing
    assert int(closing["code"]) == wire.CLOSE_AUTH_FAILED, (
        "a rejected token is the one terminal close"
    )


def test_a_valid_token_gets_hello_then_the_board(
    client: tuple[TestClient, ProjectInfo, str],
) -> None:
    http, project, token = client
    with http.websocket_connect("/ws") as connection:
        connection.send_text(json.dumps({"t": "auth", "token": token}))
        hello = json.loads(_text(connection))
        snapshot = json.loads(_text(connection))
    assert hello == {
        "t": "hello",
        "protocol": 1,
        "hub": project.id,
        "serverTime": hello["serverTime"],
    }
    assert [session["id"] for session in snapshot["sessions"]] == [CODER]
    assert snapshot["groups"] == []


# --- deltas ---------------------------------------------------------------------


def test_attention_on_the_board_becomes_a_needs_you_delta(
    client: tuple[TestClient, ProjectInfo, str],
) -> None:
    """The alert path: a hook marks attention, the ring learns within a poll."""
    http, _project, token = client
    with _authed(http, token) as connection:
        with store_session() as store:
            assert store.mark_attention(CODER)
        delta = json.loads(_text(connection))
    assert delta["t"] == "delta"
    assert [(s["id"], s["state"]) for s in delta["changed"]] == [(CODER, "needs_you")]
    assert delta["removed"] == []


def test_a_session_that_ends_is_removed_once(
    client: tuple[TestClient, ProjectInfo, str],
) -> None:
    http, project, token = client
    with _authed(http, token) as connection:
        now = datetime.now(tz=UTC)
        with store_session() as store:
            store.upsert_session(
                TeamSession(
                    id=PLANNER,
                    project_id=project.id,
                    role="planner",
                    started_at=now,
                    last_seen_at=now,
                )
            )
        added = json.loads(_text(connection))
        assert [s["id"] for s in added["changed"]] == [PLANNER]

        with store_session() as store:
            store.end_session(PLANNER)
        removed = json.loads(_text(connection))
    assert removed["removed"] == [PLANNER]
    assert removed["changed"] == []


# --- transcripts ----------------------------------------------------------------


def test_subscribe_streams_the_transcript_of_that_session_only(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backlog first, then records as they land — user and assistant text only."""
    transcript = work_dir / "session.jsonl"
    transcript.write_text(
        "\n".join(
            json.dumps(record)
            for record in (
                {"type": "user", "message": {"content": [{"type": "text", "text": "first"}]}},
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                            {"type": "text", "text": "second"},
                        ]
                    },
                },
                {"type": "system", "message": {"content": "not a conversation turn"}},
            )
        )
        + "\n",
        encoding="utf-8",
    )
    project = _seed(work_dir, transcript=str(transcript))
    token = mcp_server.serve_token()
    with (
        TestClient(xr_server.build_app(project, token=token)) as http,
        _authed(http, token) as connection,
    ):
        connection.send_text(json.dumps({"t": "subscribe", "session": CODER}))
        backlog = [json.loads(_text(connection)) for _ in range(2)]
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "third"}]},
                    }
                )
                + "\n"
            )
        fresh = json.loads(_text(connection))

    assert [frame["text"] for frame in backlog] == ["first", "second"]
    assert all(frame["session"] == CODER and frame["final"] for frame in backlog)
    assert [frame["seq"] for frame in backlog] == [1, 2]
    assert fresh["text"] == "third", "a record written after subscribe arrives too"
    assert fresh["seq"] == 3


def test_subscribing_to_a_session_that_is_not_here_is_an_error_not_a_close(
    client: tuple[TestClient, ProjectInfo, str],
) -> None:
    http, _project, token = client
    with _authed(http, token) as connection:
        connection.send_text(json.dumps({"t": "subscribe", "session": "ses_nope"}))
        answer = json.loads(_text(connection))
        # Still alive: a bad ask must not cost the operator their ring.
        connection.send_text(json.dumps({"t": "subscribe", "session": None}))
    assert answer["code"] == "no_such_session"


# --- prompts and audio ----------------------------------------------------------


def test_a_prompt_to_a_session_with_no_pane_is_filed_as_a_board_note(
    client: tuple[TestClient, ProjectInfo, str],
) -> None:
    """No fleet agent means nothing to type into, so the board carries it.

    ``ack.detail`` is what makes the two delivery paths distinguishable to the
    operator, which is the whole reason that message exists.
    """
    http, project, token = client
    from aisquare.services import team as team_service

    team_service.activate(project.root)
    with _authed(http, token) as connection:
        connection.send_text(json.dumps({"t": "prompt", "session": CODER, "text": "run the suite"}))
        ack = json.loads(_until(connection, "ack"))
    assert ack["for"] == "prompt"
    assert ack["ok"] is True
    assert "board note" in ack["detail"]

    with store_session() as store:
        texts = [event.text for event in store.recent_events(project.id, limit=10)]
    assert any("run the suite" in text for text in texts)


def test_an_empty_prompt_and_an_unknown_session_are_refused_in_the_ack(
    client: tuple[TestClient, ProjectInfo, str],
) -> None:
    http, _project, token = client
    with _authed(http, token) as connection:
        connection.send_text(json.dumps({"t": "prompt", "session": CODER, "text": "   "}))
        empty = json.loads(_until(connection, "ack"))
        connection.send_text(json.dumps({"t": "prompt", "session": "ses_nope", "text": "hi"}))
        unknown = json.loads(_until(connection, "ack"))
    assert empty["ok"] is False and "empty" in empty["detail"]
    assert unknown["ok"] is False and "no such session" in unknown["detail"]


def test_a_prompt_addressed_by_prefix_reaches_the_live_pane(
    client: tuple[TestClient, ProjectInfo, str],
    work_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prompt by id PREFIX is typed into the session's pane, not filed as a note.

    The prefix fix landed only in ``_subscribe``: ``_prompt`` matched the live
    fleet agent with ``candidate.session_id == message.session`` — the raw
    client string — and a prefix never equals the full stored id, so every
    prefix-addressed prompt found no agent and became a board note, with the ack
    naming the prefix instead of the session. The prompt path now resolves once
    and uses ``row.id`` everywhere.
    """
    from types import SimpleNamespace

    from aisquare.models import FleetAgent
    from aisquare.services import fleet as fleet_service

    http, project, token = client
    now = datetime.now(tz=UTC)
    with store_session() as store:
        store.upsert_fleet_agent(
            FleetAgent(
                id="flt_1",
                project_id=project.id,
                label="coder-1",
                role="coder",
                pane_id="%1",
                session_id=CODER,
                cwd=work_dir,
                created_at=now,
            )
        )
    told: list[tuple[str, str]] = []

    def fake_tell(_project: ProjectInfo, label: str, text: str) -> Any:
        told.append((label, text))
        return SimpleNamespace(delivered=True, how="typed into the waiting pane")

    monkeypatch.setattr(fleet_service, "tell", fake_tell)
    with _authed(http, token) as connection:
        connection.send_text(
            json.dumps({"t": "prompt", "session": CODER[:8], "text": "run the suite"})
        )
        ack = json.loads(_until(connection, "ack"))
    assert told == [("coder-1", "run the suite")], (
        f"a prefix must resolve to the pane's full session id; fleet.tell saw {told}"
    )
    assert ack["session"] == CODER, "the ack names the RESOLVED session, not the typed prefix"
    assert ack["ok"] is True and "pane" in ack["detail"]


def test_an_ambiguous_prompt_prefix_is_a_refused_ack_not_internal(work_dir: Path) -> None:
    """An ambiguous prompt prefix is answered in the ack, not as a server fault.

    ``AmbiguousIdError`` escaped ``_prompt`` to ``_read_loop``'s catch-all and
    reached the client as ``internal`` (and no ack at all), while the same prefix
    on ``subscribe`` was answered ``ambiguous_session``. The prompt path answers
    on its own channel — one ack per prompt — with ``ok: false`` and the reason.
    """
    project = _seed(work_dir)  # CODER = bbbb2222-...
    _join(project, SECOND, role="coder")  # bbbb3333-..., so `bbbb` ties on this board
    token = mcp_server.serve_token()
    with (
        TestClient(xr_server.build_app(project, token=token)) as http,
        _authed(http, token) as connection,
    ):
        connection.send_text(json.dumps({"t": "prompt", "session": "bbbb", "text": "hi"}))
        ack = json.loads(_until(connection, "ack"))
    assert ack["ok"] is False, ack
    assert "ambiguous" in ack["detail"] and "bbbb" in ack["detail"], ack["detail"]


# --- voice ----------------------------------------------------------------------

CANNED = "open the ring"
"""What the fake hears. Short, like the commands this path is actually for."""

FRAME = b"\x00" * speech.FRAME_BYTES
"""One 20 ms frame of 16 kHz mono PCM16LE — the size the client's worklet emits.

Zeroes, which a REAL transcriber's silence gate would refuse; that gate is
``tests/test_xr_speech.py``'s subject. What is measured here is the path the
bytes take, and it must be measured with bytes the client would really send.
"""

FRAMES_PER_INTERIM = speech.INTERIM_BYTES // speech.FRAME_BYTES
"""How many frames earn one interim decode: 50, i.e. one second of audio.

Derived, not typed: it is the same arithmetic
``services.xr.speech.BufferedTranscriber`` does, so a change to the frame size
or the interim cadence moves this suite with it instead of silently making the
interim assertions untestable.
"""


def _voice(
    work_dir: Path, factory: xr_server.TranscriberFactory
) -> Iterator[tuple[TestClient, ProjectInfo, str]]:
    project = _seed(work_dir)
    token = mcp_server.serve_token()
    with TestClient(xr_server.build_app(project, token=token, transcriber_factory=factory)) as http:
        yield http, project, token


@pytest.fixture
def voice(
    work_dir: Path,
) -> Iterator[tuple[TestClient, ProjectInfo, str, list[FakeTranscriber]]]:
    """A board and a socket whose transcriber is a fake, recording every one built.

    The list is the point of the fixture: "one transcriber per connection,
    reused across utterances" is a claim about how many times the factory ran,
    and nothing on the wire can show that.
    """
    built: list[FakeTranscriber] = []

    def factory() -> FakeTranscriber:
        made = FakeTranscriber(CANNED)
        built.append(made)
        return made

    for http, project, token in _voice(work_dir, factory):
        yield http, project, token, built


def _drain_until(connection: Any, kind: str) -> list[dict[str, Any]]:
    """Every frame up to and INCLUDING the first of ``kind``.

    ``_until`` answers "did this arrive"; this one answers "and what else did,
    on the way". Half the voice failure modes are specified as *exactly one*
    of something, which is a claim about the frames nobody asked for.
    """
    seen: list[dict[str, Any]] = []
    for _ in range(60):
        frame = dict(json.loads(_text(connection)))
        seen.append(frame)
        if frame.get("t") == kind:
            return seen
    raise AssertionError(f"no {kind} frame arrived; saw {[frame.get('t') for frame in seen]}")


def _burst(connection: Any, frames: list[bytes], *, session: str = CODER, seq: int = 0) -> None:
    """A push-to-talk burst of exactly these frames, in this order: header, frames, ``audioEnd``."""
    connection.send_text(json.dumps({"t": "audio", "session": session, "seq": seq}))
    for frame in frames:
        connection.send_bytes(frame)
    connection.send_text(json.dumps({"t": "audioEnd", "session": session}))


def _voice_frames(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``frames`` without the poller's deltas.

    The poll task runs on its own clock and may put a ``delta`` anywhere in
    a drained sequence — it did, between an interim and a final, on a py3.11
    CI runner — so an assertion about the ORDER of voice frames drops them
    first. What is asserted is what the voice worker sent, in the order it
    sent it; the deltas are a different task's traffic.
    """
    return [frame for frame in frames if frame.get("t") != "delta"]


def _speak(connection: Any, *, frames: int = FRAMES_PER_INTERIM, session: str = CODER) -> None:
    """A burst of ``frames`` identical silent frames — the common shape, spelled once."""
    _burst(connection, [FRAME] * frames, session=session)


def _final_stt(connection: Any) -> dict[str, Any]:
    """Drain interims and return the ``stt`` frame with ``final: true``."""
    frame = json.loads(_until(connection, "stt"))
    while frame["final"] is False:
        frame = json.loads(_until(connection, "stt"))
    return dict(frame)


def _note_filed(project_id: str, text: str) -> bool:
    """Whether a board event with exactly ``text`` exists — what a routed prompt leaves behind."""
    with store_session() as store:
        return any(event.text == text for event in store.recent_events(project_id, limit=10))


def _eventually(condition: Callable[[], bool], *, within: float = RECEIVE_TIMEOUT_S) -> bool:
    """Poll ``condition`` until it holds or ``within`` seconds pass.

    For a fact that lives on the server's side of the socket — a transcriber
    built by the worker task, a note the worker filed after the client left —
    which no frame the test receives can synchronise with.
    """
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def test_the_real_backend_is_the_default_and_app_state_is_the_one_seam(work_dir: Path) -> None:
    """The seam, at rest and swapped: a live server transcribes for real, a test does not.

    The default is asserted by identity and never called — invoking it here
    would load a whisper model on whatever machine is running the suite. The
    swap is exercised on a RUNNING app, because ``app.state`` is the one
    documented place to inject a transcriber and a value captured at build
    time would make that swap silently do nothing. There used to be a second,
    process-wide seam beside this one; two ways to inject the same object are
    two places to check to learn which transcriber a socket will get.
    """
    project = _seed(work_dir)
    token = mcp_server.serve_token()
    from aisquare.services import team as team_service

    team_service.activate(project.root)
    app = xr_server.build_app(project, token=token)
    assert app.state.transcriber_factory is speech.transcriber

    built: list[str] = []

    def first() -> Transcriber:
        built.append("first")
        return FakeTranscriber("first")

    def second() -> Transcriber:
        built.append("second")
        return FakeTranscriber("second")

    app.state.transcriber_factory = first
    with TestClient(app) as http:
        with _authed(http, token) as connection:
            _speak(connection)
            assert _final_stt(connection)["text"] == "first"
        app.state.transcriber_factory = second
        with _authed(http, token) as connection:
            _speak(connection)
            assert _final_stt(connection)["text"] == "second", (
                "the swap was not read per connection"
            )
    assert built == ["first", "second"]


def test_a_spoken_burst_is_interim_stt_then_one_final_then_a_routed_prompt(
    voice: tuple[TestClient, ProjectInfo, str, list[FakeTranscriber]],
) -> None:
    """The whole M6 server half in one pass, on the board-note delivery route.

    The client sends no ``prompt`` of its own: releasing the trigger IS the
    commit, and the ``ack`` is how the operator learns where their words went.
    """
    http, project, token, built = voice
    from aisquare.services import team as team_service

    team_service.activate(project.root)
    with _authed(http, token) as connection:
        connection.send_text(json.dumps({"t": "audio", "session": CODER, "seq": 0}))
        for _ in range(FRAMES_PER_INTERIM):
            connection.send_bytes(FRAME)
        interim = json.loads(_until(connection, "stt"))
        connection.send_text(json.dumps({"t": "audioEnd", "session": CODER}))
        final = json.loads(_until(connection, "stt"))
        ack = json.loads(_until(connection, "ack"))

    assert interim == {"t": "stt", "text": CANNED, "final": False}
    assert final == {"t": "stt", "text": CANNED, "final": True}
    assert ack["for"] == "prompt"
    assert ack["ok"] is True
    assert "board note" in ack["detail"], ack

    # The transcript reached the agent by the same route a typed prompt takes.
    with store_session() as store:
        texts = [event.text for event in store.recent_events(project.id, limit=10)]
    assert any(CANNED in text for text in texts)

    assert len(built) == 1, "one transcriber for the connection"
    assert bytes(built[0].fed) == FRAME * FRAMES_PER_INTERIM, "every frame arrived"
    assert built[0].finished


def _distinct_frames(count: int) -> list[bytes]:
    """``count`` frames whose payloads differ, so a reordering cannot hide in the join."""
    return [index.to_bytes(2, "little") * (speech.FRAME_BYTES // 2) for index in range(count)]


def test_frames_reach_the_transcriber_in_wire_order_with_distinct_payloads(
    voice: tuple[TestClient, ProjectInfo, str, list[FakeTranscriber]],
) -> None:
    """Order, pinned with frames that can tell each other apart.

    Every other voice test sends identical frames — all-zero ``FRAME`` or a
    repeated tone — so "every frame arrived, in order" could not detect a
    reordering: swapping pairs in the server's frame path passed the whole
    suite. Speech scrambled that way is noise, and a per-frame task dispatch,
    the obvious way to get decodes off the read loop, would produce exactly
    that. So these payloads differ, and the join is asserted whole.
    """
    http, _project, token, built = voice
    frames = _distinct_frames(FRAMES_PER_INTERIM)
    with _authed(http, token) as connection:
        _burst(connection, frames)
        assert _final_stt(connection)["text"] == CANNED
    assert bytes(built[0].fed) == b"".join(frames), "the frames were reordered on the way"


def test_frames_that_arrive_during_a_slow_decode_are_fed_as_one_chunk_in_order(
    work_dir: Path,
) -> None:
    """Catching up costs one ``feed``, and it preserves the order it caught up on.

    While the worker is parked in a decode the read loop keeps accepting
    frames; when the decode returns, everything that arrived meanwhile is
    handed over as ONE chunk. That is what lets a worker that fell a decode
    behind catch up, rather than paying one decode per frame it missed for
    the rest of the press — and it is the one place a reordering could slip
    in, so the payloads differ and the join is checked.
    """
    held = _HeldTranscriber()
    frames = _distinct_frames(FRAMES_PER_INTERIM)
    for http, _project, token in _voice(work_dir, lambda: held):
        with _authed(http, token) as connection:
            try:
                connection.send_text(json.dumps({"t": "audio", "session": CODER, "seq": 0}))
                connection.send_bytes(frames[0])
                assert held.entered.wait(RECEIVE_TIMEOUT_S), "the first frame never reached feed"
                for frame in frames[1:]:
                    connection.send_bytes(frame)
                connection.send_text(json.dumps({"t": "audioEnd", "session": CODER}))
            finally:
                held.release.set()
            assert _final_stt(connection)["text"] == "held"
    assert b"".join(held.feeds) == b"".join(frames), "the frames were reordered or lost"
    assert held.feeds[0] == frames[0]
    assert len(held.feeds) < len(frames), (
        f"{len(held.feeds)} feeds for {len(frames)} frames: the backlog was not batched"
    )


def test_a_second_burst_on_one_connection_reuses_the_transcriber(
    voice: tuple[TestClient, ProjectInfo, str, list[FakeTranscriber]],
) -> None:
    """The model load is paid once per headset, not once per sentence.

    That is the only reason the transcriber is cached at all, so it is worth a
    test of its own: an operator says three things in a row and waits for a
    whisper load on none of them.
    """
    http, project, token, built = voice
    from aisquare.services import team as team_service

    team_service.activate(project.root)
    with _authed(http, token) as connection:
        for _ in range(2):
            _speak(connection)
            assert json.loads(_until(connection, "stt"))["final"] is False
            assert json.loads(_until(connection, "stt"))["final"] is True
            assert json.loads(_until(connection, "ack"))["ok"] is True
    assert len(built) == 1, "the second utterance built no second transcriber"


def test_a_spoken_prompt_reaches_a_fleet_pane_by_that_agents_label(
    voice: tuple[TestClient, ProjectInfo, str, list[FakeTranscriber]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other delivery route: a session with a live pane is typed into.

    ``fleet.tell`` is patched rather than run because the real one needs tmux,
    and what this asserts is the ROUTING — the label the voice path resolved
    and the text it handed over — not tmux's ability to type.
    """
    http, project, token, _built = voice
    told: list[tuple[str, str]] = []

    def tell(
        project_arg: ProjectInfo, label: str, text: str, *, sender: str | None = None
    ) -> fleet_service.TellResult:
        told.append((label, text))
        return fleet_service.TellResult(True, "typed into the waiting pane")

    monkeypatch.setattr(fleet_service, "tell", tell)
    with store_session() as store:
        store.upsert_fleet_agent(
            FleetAgent(
                id="flt_voice",
                project_id=project.id,
                label="coder-xr-voice",
                role="coder",
                pane_id="%7",
                session_id=CODER,
                cwd=project.root,
                created_at=datetime.now(tz=UTC),
            )
        )
    with _authed(http, token) as connection:
        _speak(connection)
        ack = json.loads(_until(connection, "ack"))

    assert told == [("coder-xr-voice", CANNED)], "the agent's label, and the words as heard"
    assert ack["ok"] is True
    assert ack["detail"] == "typed into the waiting pane"
    assert ack["session"] == CODER


def test_a_machine_with_no_speech_backend_says_so_once_and_keeps_the_ring(
    work_dir: Path,
) -> None:
    """Fail open, in the shape an operator meets it in.

    One error carrying the FIX, the frames already in flight swallowed rather
    than refused one by one, and — the part that matters — the board still
    arriving underneath. A voice failure that took the ring with it would cost
    the operator the thing they put the headset on for.
    """

    def missing() -> Transcriber:
        raise SpeechUnavailable("faster-whisper is not installed", "pip install 'aisquare-cli[xr]'")

    for http, _project, token in _voice(work_dir, missing):
        with _authed(http, token) as connection:
            _speak(connection, frames=10)
            # The error is the worker's answer, on its own clock; wait for it
            # before provoking the delta, or a fast poller ends the drain first.
            answered = _drain_until(connection, "error")
            with store_session() as store:
                store.mark_attention(CODER)
            frames = answered + _drain_until(connection, "delta")

    errors = [frame for frame in frames if frame["t"] == "error"]
    assert len(errors) == 1, [frame["t"] for frame in frames]
    assert errors[0]["code"] == "stt_unavailable"
    assert "faster-whisper is not installed" in errors[0]["message"]
    assert "pip install" in errors[0]["message"], "the fix travels with the complaint"
    assert not [frame for frame in frames if frame["t"] in ("stt", "ack")]
    assert [session["state"] for session in frames[-1]["changed"]] == ["needs_you"]


def test_the_backend_is_retried_on_the_next_press_not_written_off(
    work_dir: Path,
) -> None:
    """An operator who installs the extra mid-session gets voice on press two.

    Caching the FAILURE would be the easy read of "one error per utterance",
    and it would mean the fix this server just printed cannot be applied
    without a reconnect.
    """
    attempts: list[int] = []

    def flaky() -> Transcriber:
        attempts.append(1)
        if len(attempts) == 1:
            raise SpeechUnavailable("not installed yet", "pip install 'aisquare-cli[xr]'")
        return FakeTranscriber(CANNED)

    for http, project, token in _voice(work_dir, flaky):
        from aisquare.services import team as team_service

        team_service.activate(project.root)
        with _authed(http, token) as connection:
            _speak(connection, frames=5)
            first = json.loads(_until(connection, "error"))
            _speak(connection)
            final = _final_stt(connection)
    assert first["code"] == "stt_unavailable"
    assert final == {"t": "stt", "text": CANNED, "final": True}
    assert len(attempts) == 2


def test_a_stray_binary_frame_is_one_error_and_nothing_else(
    voice: tuple[TestClient, ProjectInfo, str, list[FakeTranscriber]],
) -> None:
    """Audio with no header names no session, so there is nothing to do with it.

    "Nothing else" is measured rather than assumed: the delta is provoked
    AFTER the stray frame, so every frame the server chose to send in between
    is in the list being asserted on.
    """
    http, _project, token, built = voice
    with _authed(http, token) as connection:
        connection.send_bytes(FRAME)
        with store_session() as store:
            store.mark_attention(CODER)
        frames = _drain_until(connection, "delta")

    assert [frame["t"] for frame in frames] == ["error", "delta"]
    assert frames[0]["code"] == "audio_unexpected"
    assert built == [], "a stray frame must not load a model"


def test_a_flood_of_stray_frames_is_still_exactly_one_error(
    voice: tuple[TestClient, ProjectInfo, str, list[FakeTranscriber]],
) -> None:
    """A client whose header was lost sends the whole burst before it can react.

    Answering each frame would bury the one message the operator needs under a
    flood the server generated itself.
    """
    http, _project, token, _built = voice
    with _authed(http, token) as connection:
        for _ in range(20):
            connection.send_bytes(FRAME)
        with store_session() as store:
            store.mark_attention(CODER)
        frames = _drain_until(connection, "delta")
    assert [frame["t"] for frame in frames].count("error") == 1


def test_the_stray_latch_lifts_so_the_next_real_mistake_is_reported(
    voice: tuple[TestClient, ProjectInfo, str, list[FakeTranscriber]],
) -> None:
    """Quietening a flood must not quieten the channel permanently."""
    http, _project, token, _built = voice
    with _authed(http, token) as connection:
        connection.send_bytes(FRAME)
        assert json.loads(_until(connection, "error"))["code"] == "audio_unexpected"
        _speak(connection, frames=2)
        assert json.loads(_until(connection, "stt"))["final"] is True
        connection.send_bytes(FRAME)
        assert json.loads(_until(connection, "error"))["code"] == "audio_unexpected"


@pytest.mark.parametrize("cap", ["MAX_UTTERANCE_S", "MAX_AUDIO_BYTES"])
def test_an_utterance_past_either_cap_is_dropped_with_one_error(
    voice: tuple[TestClient, ProjectInfo, str, list[FakeTranscriber]],
    monkeypatch: pytest.MonkeyPatch,
    cap: str,
) -> None:
    """Both caps exist and both must end the burst the same way.

    The clock catches a stuck trigger; the byte count catches a client pushing
    a file through faster than real time, where the clock would never fire and
    the buffer is the thing that grows. Dropping is the whole response: no
    transcript, no prompt, and a socket that is still there afterwards.
    """
    http, _project, token, _built = voice
    monkeypatch.setattr(xr_server, cap, 0)
    with _authed(http, token) as connection:
        _speak(connection, frames=5)
        with store_session() as store:
            store.mark_attention(CODER)
        frames = _drain_until(connection, "delta")

    errors = [frame for frame in frames if frame["t"] == "error"]
    assert len(errors) == 1, [frame["t"] for frame in frames]
    assert errors[0]["code"] == "audio_too_long"
    assert not [frame for frame in frames if frame["t"] in ("stt", "ack")], "dropped, not decoded"


def test_an_utterance_opened_and_abandoned_is_capped_by_the_clock(
    voice: tuple[TestClient, ProjectInfo, str, list[FakeTranscriber]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The headset put down mid-word, which is the case the cap's docstring names.

    A burst that is opened and then simply stops sending frames is the one the
    wall-clock cap exists for, and it is the one an arrival-triggered check can
    never see: no frame arrives, so nothing re-evaluates, and the loaded model
    is held for the life of the socket. The poller is the connection's only
    clock, so the cap is re-checked there.

    No frames at all after the header — not even one — because a single frame
    would let the arrival path take the credit and leave the timer untested.
    """
    http, _project, token, built = voice
    monkeypatch.setattr(xr_server, "MAX_UTTERANCE_S", 0.0)
    with _authed(http, token) as connection:
        connection.send_text(json.dumps({"t": "audio", "session": CODER, "seq": 0}))
        answer = json.loads(_until(connection, "error"))
        connection.send_text(json.dumps({"t": "prompt", "session": CODER, "text": "still here"}))
        assert json.loads(_until(connection, "ack"))["ok"] is True, "fail-open: the socket lives"

    assert answer["code"] == "audio_too_long"
    assert _eventually(lambda: len(built) == 1), "the header asked the worker for a transcriber"
    assert built[0].fed == b"", "the abandoned burst never fed the model it was holding"
    assert built[0].discarded == 0, "nothing was held, so there was nothing to discard"


def test_an_utterance_that_transcribes_to_nothing_is_not_a_prompt(
    work_dir: Path,
) -> None:
    """Silence is answered, and it is answered with silence.

    The final ``stt`` still goes out — the panel must stop showing a live mic —
    but nothing is routed. Filing an empty board note against an operator who
    simply did not speak would be a write this server had no reason to make.
    """
    for http, _project, token in _voice(work_dir, lambda: FakeTranscriber("")):
        with _authed(http, token) as connection:
            _speak(connection)
            # The final is the worker's answer, on its own clock; wait for it
            # before provoking the delta, or a fast poller ends the drain first.
            answered = _drain_until(connection, "stt")
            with store_session() as store:
                store.mark_attention(CODER)
            frames = answered + _drain_until(connection, "delta")

    stt = [frame for frame in frames if frame["t"] == "stt"]
    assert stt == [{"t": "stt", "text": "", "final": True}]
    assert not [frame for frame in frames if frame["t"] == "ack"], "nothing to route"


def test_a_slow_decode_does_not_stall_the_board_poll(work_dir: Path) -> None:
    """The reason ``feed`` runs in a thread, measured instead of asserted by eye.

    The transcriber is held inside ``feed`` while the board is mutated from
    another thread. The delta that arrives WHILE it is still held is the whole
    proof: on the event loop there would be no poll tick to send it, and this
    test would time out rather than fail on a value.
    """
    held = _HeldTranscriber()
    for http, _project, token in _voice(work_dir, lambda: held):
        with _authed(http, token) as connection:
            try:
                connection.send_text(json.dumps({"t": "audio", "session": CODER, "seq": 0}))
                connection.send_bytes(FRAME)
                assert held.entered.wait(RECEIVE_TIMEOUT_S), (
                    "the frame never reached the transcriber"
                )
                with store_session() as store:
                    store.mark_attention(CODER)
                delta = json.loads(_until(connection, "delta"))
                assert not held.release.is_set(), "the decode is still running"
            finally:
                # INSIDE the socket's block, and always, even on a failure. The
                # release used to sit in an outer finally, after TestClient had
                # exited — and TestClient's exit joins the worker still parked
                # in the held decode, so every run of this test spent the whole
                # RECEIVE_TIMEOUT_S in teardown: 15 s, most of the XR suite's
                # time, protecting nothing on either the passing or the failing
                # path. Releasing here takes 0.03 s and still unwedges a failure.
                held.release.set()
    assert [session["state"] for session in delta["changed"]] == ["needs_you"]


class _HeldTranscriber:
    """A transcriber whose first ``feed`` (or ``finish``) blocks until the test says so.

    ``entered`` is set when the call begins — the test's proof that the
    server is inside the backend — and ``release`` is what lets it return.
    Later calls do not block, so a burst can be finished after the hold.
    """

    def __init__(
        self, *, hold: str = "feed", interim: str | None = None, final: str = "held"
    ) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.hold = hold
        self.interim = interim
        self.final = final
        self.feeds: list[bytes] = []
        self.discarded = 0

    def _block(self, name: str) -> None:
        if name == self.hold and not self.entered.is_set():
            self.entered.set()
            self.release.wait(RECEIVE_TIMEOUT_S)

    def feed(self, pcm: bytes) -> str | None:
        self.feeds.append(pcm)
        self._block("feed")
        return self.interim

    def finish(self) -> str:
        self._block("finish")
        return self.final

    def discard(self) -> None:
        self.discarded += 1


def test_a_typed_prompt_is_answered_while_a_decode_is_still_running(work_dir: Path) -> None:
    """The read loop never waits on the backend, so typing is not queued behind speech.

    Awaiting each decode inline in the socket's only read loop made every
    typed prompt, subscribe and ``audioEnd`` wait behind it, and — since the
    utterance cap is wall-clock time since the header — let intake fall
    behind real time until the poller dropped a press that was in contract
    (measured on the real stack: a 46.6 s press got ``audio_too_long`` at
    60.3 s, 14 s after release). The worker is what separates the two, and
    this is the observable half: an ``ack`` for a typed prompt arrives while
    the transcriber is provably still inside ``feed``.
    """
    held = _HeldTranscriber()
    for http, project, token in _voice(work_dir, lambda: held):
        from aisquare.services import team as team_service

        team_service.activate(project.root)
        with _authed(http, token) as connection:
            try:
                connection.send_text(json.dumps({"t": "audio", "session": CODER, "seq": 0}))
                connection.send_bytes(FRAME)
                assert held.entered.wait(RECEIVE_TIMEOUT_S), (
                    "the frame never reached the transcriber"
                )
                connection.send_text(
                    json.dumps({"t": "prompt", "session": CODER, "text": "typed meanwhile"})
                )
                ack = json.loads(_until(connection, "ack"))
                assert not held.release.is_set(), "the decode is still running"
            finally:
                held.release.set()
    assert ack["ok"] is True and "board note" in ack["detail"], ack


TONE = b"\x40\x1f\xc0\xe0" * (speech.FRAME_BYTES // 4)
"""One 20 ms frame a REAL transcriber will accept as speech, not room tone.

:data:`FRAME` is zeroes, which is all the fake path needs and all it should
use. The tests below drive a real
:class:`~aisquare.services.xr.speech.BufferedTranscriber`, whose gate is
one-way: until a frame is loud enough nothing is buffered at all, so an
all-zero frame would make every assertion below vacuously pass. +/-8000 as a
square wave, the same shape ``tests/test_xr_speech.py`` uses, and deliberately
free of :data:`_POISON`'s byte pair.
"""

_POISON = b"\xad\xde"
"""One sample (0xdead) a :func:`_strict_decode` refuses to transcribe.

A stand-in for "the backend threw mid-utterance". The failure it provokes is
the real one, not a patched method: ``BufferedTranscriber`` calls ``_reset()``
only AFTER ``_decode`` returns, so a decode that raises leaves the poisoned
bytes in the buffer and every later decode on that object raises too. That is
what makes dropping the object the fix and reusing it a permanent loss of
voice — and what makes it different from an odd frame, which never reaches
the decoder at all (see the ``audio_misaligned`` test below).
"""


def _strict_decode(pcm: bytes) -> str:
    """Production's decode shape: reject a half sample, and reject the poison.

    ``speech.py``'s real decoder opens with ``numpy.frombuffer(pcm,
    dtype=numpy.int16)``, which raises on a length that is not a multiple of
    two. numpy ships in the optional ``xr`` extra, so it is used when it is
    installed — which is what CI's ``[dev,xr]`` job runs — and ``array``, which
    raises on exactly the same lengths for exactly the same reason, stands in
    under ``[dev]``. The property is pinned in both, rather than skipped in one.
    Every chunk it was handed is recorded in :data:`_DECODED`, so a test can
    also assert what was NOT decoded.
    """
    _DECODED.append(pcm)
    try:
        import numpy
    except ImportError:
        array("h").frombytes(pcm)
    else:
        numpy.frombuffer(pcm, dtype=numpy.int16).astype(numpy.float32) / 32768.0
    if _POISON in pcm:
        raise ValueError("the backend refuses this audio")
    return "real words"


_DECODED: list[bytes] = []
"""Every chunk :func:`_strict_decode` was handed, cleared by :func:`_real_voice`."""


def _real_voice(
    work_dir: Path,
) -> Iterator[tuple[TestClient, ProjectInfo, str, list[BufferedTranscriber]]]:
    """A socket whose transcriber is the REAL buffering one over :func:`_strict_decode`.

    The list is the only way to see the claim these tests are about: nothing on
    the wire says whether the connection built a new transcriber or reused the
    one it already had.
    """
    built: list[BufferedTranscriber] = []
    _DECODED.clear()

    def factory() -> BufferedTranscriber:
        made = BufferedTranscriber(_strict_decode)
        built.append(made)
        return made

    for http, project, token in _voice(work_dir, factory):
        yield http, project, token, built


def _tone_frames(*, odd_at: int | None = None, poison_at: int | None = None) -> list[bytes]:
    """Enough loud frames to earn an interim, with one frame spoiled at ``at``."""
    frames = [TONE] * (FRAMES_PER_INTERIM + 1)
    if odd_at is not None:
        frames[odd_at] = TONE + b"\x01"
    if poison_at is not None:
        frames[poison_at] = _POISON + TONE[len(_POISON) :]
    return frames


@pytest.mark.parametrize("where", [0, FRAMES_PER_INTERIM // 2, FRAMES_PER_INTERIM])
def test_an_odd_frame_anywhere_is_answered_once_and_costs_the_burst_not_the_model(
    work_dir: Path, where: int
) -> None:
    """A frame that is not a whole number of samples is a protocol error: one ``audio_misaligned``.

    A websocket delivers whole messages, so an odd-length frame is a client
    that lost or added a byte, and every sample after it is the wrong pair.
    The server briefly carried that byte into the next frame instead; a
    stand-in decoder that checked alignment then measured "8000/16319
    samples mis-paired" in a final transcript that was filed as a board note
    with ``ok``. So the burst is dropped, once, like ``audio_unexpected`` —
    and unlike a backend crash it costs the loaded model nothing, because
    the bad frame never reached the decoder.

    First frame, mid-utterance and the last frame before ``audioEnd`` are all
    tested because they differ: the first is refused before anything was
    buffered, the middle one after an interim may already have gone out, and
    the last is the only one that could otherwise reach ``finish``.
    """
    for http, project, token, built in _real_voice(work_dir):
        from aisquare.services import team as team_service

        team_service.activate(project.root)
        with _authed(http, token) as connection:
            _burst(connection, _tone_frames(odd_at=where))
            with store_session() as store:
                store.mark_attention(CODER)
            frames = _drain_until(connection, "delta")
            errors = [frame for frame in frames if frame["t"] == "error"]
            assert [error["code"] for error in errors] == ["audio_misaligned"], frames
            assert f"{speech.FRAME_BYTES + 1}-byte" in errors[0]["message"]
            after_error = frames[frames.index(errors[0]) + 1 :]
            assert not [f for f in after_error if f["t"] in ("stt", "ack")], (
                f"the dropped burst was still transcribed or routed: {after_error}"
            )

            # The socket still works, and it is the TYPED path that proves it:
            # its ack is read by the text it filed, not by being the next ack.
            connection.send_text(json.dumps({"t": "prompt", "session": CODER, "text": "after"}))
            ack = json.loads(_until(connection, "ack"))
            assert ack["ok"] is True, ack
            with store_session() as store:
                texts = [event.text for event in store.recent_events(project.id, limit=10)]
            assert "after" in texts, "the typed prompt after the bad burst was not filed"

            # And the next burst is clean, on the SAME transcriber: the dropped
            # burst's audio was discarded, not decoded and not prepended.
            _burst(connection, _tone_frames())
            assert _final_stt(connection)["text"] == "real words"
        assert len(built) == 1, f"the misaligned frame cost a model reload: built={len(built)}"
        assert len(_DECODED[-1]) == len(TONE) * (FRAMES_PER_INTERIM + 1), (
            "the final decode included audio from the dropped burst"
        )


@pytest.mark.parametrize("where", [0, FRAMES_PER_INTERIM // 2, FRAMES_PER_INTERIM])
def test_a_backend_failure_anywhere_does_not_poison_the_next_utterance(
    work_dir: Path, where: int
) -> None:
    """A transcriber that raised is thrown away, because it may never work again.

    ``BufferedTranscriber`` resets in ``finish()`` and only after the decode
    returns, so an object that raised still holds the audio that made it raise:
    reusing it turns one lost sentence into a socket that looks live and
    transcribes nothing for as long as the operator keeps it open.

    The final assertion is the one that actually pins it. The wire cannot show
    whether the connection reused an object or built a new one, so a test that
    only checked the second transcript would pass against a
    ``_fail_burst`` that kept the object on any day the poison happened to
    fall outside what the second decode was handed.
    """
    for http, _project, token, built in _real_voice(work_dir):
        with _authed(http, token) as connection:
            _burst(connection, _tone_frames(poison_at=where))
            failure = _drain_until(connection, "error")[-1]
            assert failure["code"] == "stt_failed", failure

            _burst(connection, _tone_frames())
            final = _final_stt(connection)
            assert final["text"] == "real words", f"the next utterance was poisoned: {final}"
        assert len(built) >= 2, f"the poisoned transcriber was REUSED: built={len(built)}"


def test_two_clients_speaking_at_once_never_hear_each_other(work_dir: Path) -> None:
    """Two headsets, both mid-utterance, frames interleaved on the wire.

    The existing two-client test has one client speaking and one sending a
    stray frame, so it builds a single transcriber and says nothing about the
    case this one is for: two open utterances at the same time, which is the
    shape a second headset in the room actually produces.
    """
    built: list[FakeTranscriber] = []

    def factory() -> FakeTranscriber:
        made = FakeTranscriber(f"speaker {len(built) + 1}")
        built.append(made)
        return made

    for http, _project, token in _voice(work_dir, factory):
        with _authed(http, token) as first, _authed(http, token) as second:
            first.send_text(json.dumps({"t": "audio", "session": CODER, "seq": 0}))
            second.send_text(json.dumps({"t": "audio", "session": CODER, "seq": 0}))
            for _ in range(FRAMES_PER_INTERIM):
                first.send_bytes(FRAME)
                second.send_bytes(FRAME)
            first.send_text(json.dumps({"t": "audioEnd", "session": CODER}))
            second.send_text(json.dumps({"t": "audioEnd", "session": CODER}))
            heard = (_final_stt(first)["text"], _final_stt(second)["text"])

    assert len(built) == 2, "each connection loaded its own model"
    assert set(heard) == {"speaker 1", "speaker 2"}, f"one client heard the other: {heard}"
    assert [len(made.fed) for made in built] == [speech.INTERIM_BYTES] * 2, (
        "each transcriber got its own client's frames and only those"
    )


def test_two_clients_keep_their_own_transcriber_and_their_own_utterance(
    voice: tuple[TestClient, ProjectInfo, str, list[FakeTranscriber]],
) -> None:
    """Two headsets on one board share the SQLite file and nothing else.

    One is mid-utterance while the other sends audio it never announced; each
    gets its own answer and neither sees the other's.
    """
    http, _project, token, built = voice
    with _authed(http, token) as first, _authed(http, token) as second:
        _speak(first)
        second.send_bytes(FRAME)
        assert json.loads(_until(second, "error"))["code"] == "audio_unexpected"
        final = _final_stt(first)
    assert final["text"] == CANNED
    assert len(built) == 1, "the stray-frame client never needed a transcriber"


def test_a_binary_first_frame_is_auth_invalid_and_closed_4408(
    client: tuple[TestClient, ProjectInfo, str],
) -> None:
    """A first frame that is not an auth at all — not even text — is refused, retryably.

    ``receive_text`` raises ``KeyError`` for a binary message, and when the
    auth read and the parse shared one ``try``, that ``KeyError`` was caught
    as "the frame did not parse". Splitting them left it on the receive, where
    it fell into the "client vanished" branch: no frame, no close code, the
    transport dropped. The shipped client reads that as a transient and
    reconnects forever instead of reporting the failure.

    Refused the way every other non-auth first frame is: no token was checked,
    so the answer is ``auth_invalid`` and close 4408 (``retry:true``) — not
    ``auth_failed`` + 4401, which the client treats as a rejected token and
    stops reconnecting on. A headset whose first frame was audio has a client
    bug or a lost auth frame, not a bad credential.
    """
    http, _project, _token = client
    with http.websocket_connect("/ws") as connection:
        connection.send_bytes(FRAME)
        answer = json.loads(_text(connection))
        with pytest.raises(WebSocketDisconnect) as closed:
            connection.receive_text()
    assert answer == {
        "t": "error",
        "code": "auth_invalid",
        "message": "the first frame must be an auth message, not binary audio",
    }
    assert closed.value.code == wire.CLOSE_AUTH_TIMEOUT


def test_a_client_that_never_authenticates_is_closed_with_auth_timeout(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silence is not a wrong guess, and is not answered as one.

    ``auth_failed`` would send the operator looking for a token problem they do
    not have; a socket that opened and said nothing is a transport problem —
    an ``adb reverse`` that is not up, a client pointed at the wrong port.
    """
    project = _seed(work_dir)
    token = mcp_server.serve_token()
    monkeypatch.setattr(xr_server, "AUTH_TIMEOUT_S", 0.05)
    with (
        TestClient(xr_server.build_app(project, token=token)) as http,
        http.websocket_connect("/ws") as connection,
    ):
        answer = json.loads(_text(connection))
    assert answer["t"] == "error"
    assert answer["code"] == "auth_timeout"


def test_hello_server_time_is_an_absolute_instant_a_client_can_parse(
    client: tuple[TestClient, ProjectInfo, str],
) -> None:
    """ISO8601 with an offset, so a headset in another timezone lands on the same moment.

    A naive timestamp here is the bug that costs an hour in a demo and looks
    like a clock problem in the room.
    """
    http, _project, token = client
    with http.websocket_connect("/ws") as connection:
        connection.send_text(json.dumps({"t": "auth", "token": token}))
        hello = json.loads(_text(connection))
    moment = datetime.fromisoformat(hello["serverTime"])
    assert moment.tzinfo is not None, hello["serverTime"]
    assert moment.utcoffset() == timedelta(0), "UTC, as a Z or a +00:00"


def test_a_malformed_frame_is_answered_not_fatal(
    client: tuple[TestClient, ProjectInfo, str],
) -> None:
    http, _project, token = client
    with _authed(http, token) as connection:
        connection.send_text("{not json")
        answer = json.loads(_until(connection, "error"))
        connection.send_text(json.dumps({"t": "subscribe", "session": None}))
    assert answer["code"] == "bad_message"


def _until(connection: Any, kind: str) -> str:
    """The next frame of ``kind``, skipping the deltas the poll loop emits."""
    for _ in range(40):
        raw = _text(connection)
        if json.loads(raw).get("t") == kind:
            return str(raw)
    raise AssertionError(f"no {kind} frame arrived")


def test_a_burst_with_no_audio_frames_is_stt_empty_not_a_quiet_press(
    voice: tuple[TestClient, ProjectInfo, str, list[FakeTranscriber]],
) -> None:
    """A header followed by ``audioEnd`` with nothing between is a dead microphone, and is named.

    The shipped client sends the header before its first frame and the
    ``audioEnd`` after a 250 ms drain, so a capture graph that rendered
    nothing — a suspended ``AudioContext``, a tap released before the
    worklet's first render — produces exactly this wire. Answering it with
    the same empty final ``stt`` a silent room gets left the operator
    re-pressing into the same dead graph with nothing anywhere saying why.
    ``audio_bytes == 0`` is known at close, so it is answered as the fault it
    is: one ``stt_empty`` error, no ``stt`` frame, nothing routed.
    """
    http, _project, token, built = voice
    with _authed(http, token) as connection:
        _burst(connection, [])
        answered = _voice_frames(_drain_until(connection, "error"))
        with store_session() as store:
            store.mark_attention(CODER)
        after = _drain_until(connection, "delta")

    assert [frame["t"] for frame in answered] == ["error"], answered
    assert answered[0]["code"] == "stt_empty"
    assert "no audio arrived" in answered[0]["message"]
    assert [frame["t"] for frame in after] == ["delta"], (
        f"the error was not the whole answer: {after}"
    )
    assert _eventually(lambda: len(built) == 1)
    assert not built[0].finished, "nothing was buffered, so nothing was decoded"


def test_a_header_during_an_open_burst_ends_it_like_audioend_and_keeps_the_model(
    voice: tuple[TestClient, ProjectInfo, str, list[FakeTranscriber]],
) -> None:
    """Trigger bounce: ``audio`` seq 0, frames, ``audio`` seq 1, ``audioEnd``. Nothing is lost.

    The shipped client produces this wire on a quick re-press, because
    release clears its capturing flag before the worklet's flush has sent
    ``audioEnd``. The server used to discard the open burst silently (one
    log line nothing prints) and drop the cached transcriber, forcing a
    model rebuild on the read loop; a replay then yielded an interim and an
    empty final with no ack. Now the second header ends burst #1 exactly as
    its own ``audioEnd`` would have — final ``stt``, routed, acked — and the
    late ``audioEnd`` closes burst #2, which had no frames and says so.
    """
    http, project, token, built = voice
    from aisquare.services import team as team_service

    team_service.activate(project.root)
    with _authed(http, token) as connection:
        connection.send_text(json.dumps({"t": "audio", "session": CODER, "seq": 0}))
        for _ in range(FRAMES_PER_INTERIM):
            connection.send_bytes(FRAME)
        connection.send_text(json.dumps({"t": "audio", "session": CODER, "seq": 1}))
        connection.send_text(json.dumps({"t": "audioEnd", "session": CODER}))
        # Up to burst #2's answer, which is the LAST voice frame: the worker
        # answers bursts in wire order, so everything #1 produced is before it.
        # Deltas are the poller's, on its own clock, and are not part of the
        # order being asserted — one landed between the interim and the final
        # on a py3.11 runner and ended a drain-until-delta early.
        frames = _voice_frames(_drain_until(connection, "error"))

    kinds = [(frame["t"], frame.get("code") or frame.get("final")) for frame in frames]
    assert kinds == [
        ("stt", False),
        ("stt", True),
        ("ack", None),
        ("error", "stt_empty"),
    ], kinds
    assert frames[1]["text"] == CANNED, "burst #1's sentence was lost"
    assert frames[2]["ok"] is True
    assert len(built) == 1, "the re-press cost a model reload"
    assert bytes(built[0].fed) == FRAME * FRAMES_PER_INTERIM


def test_a_cap_that_trips_during_a_decode_is_still_answered_exactly_once(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The poller drops the burst while ``feed`` is in flight: no stale interim, no second error.

    The poller is the connection's only clock, and it can trip the cap while
    the worker is parked in a decode. When that decode returns, its interim
    belongs to a burst the client has already been told to abandon — the
    shipped client repaints on any ``stt`` and aborts a NEW press on any
    ``stt_failed`` that arrives late — so the worker re-reads the burst's
    state after every thread call and sends nothing for a dropped one.
    Reproduced at production cadence before the check: ``audio_too_long``
    followed by an interim in 11 of 12 runs.
    """
    held = _HeldTranscriber(interim="stale words from a dropped burst")
    for http, _project, token in _voice(work_dir, lambda: held):
        with _authed(http, token) as connection:
            try:
                connection.send_text(json.dumps({"t": "audio", "session": CODER, "seq": 0}))
                connection.send_bytes(FRAME)
                assert held.entered.wait(RECEIVE_TIMEOUT_S), "the frame never reached feed"
                # The cap falls to zero only now, with the decode held: no
                # frame arrives after this, so it is the POLLER's clock that
                # trips it, against a worker parked inside `feed`.
                monkeypatch.setattr(xr_server, "MAX_UTTERANCE_S", 0.0)
                dropped = json.loads(_until(connection, "error"))
            finally:
                held.release.set()
            with store_session() as store:
                store.mark_attention(CODER)
            frames = _drain_until(connection, "delta")

    assert dropped["code"] == "audio_too_long"
    assert [frame["t"] for frame in frames] == ["delta"], (
        f"a dropped burst still produced frames after its error: {frames}"
    )
    assert held.discarded == 1, "the audio the decode was holding was not discarded"


def test_a_committed_burst_is_routed_even_if_the_client_leaves_during_the_final_decode(
    work_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Releasing the trigger is the commit; a headset that then vanishes has not un-said it.

    With the final ``stt`` sent before the routing, a client gone during
    ``finish`` raised ``WebSocketDisconnect`` out of the ASGI app — uvicorn's
    "Exception in ASGI application" traceback, then a second one from the
    error frame the read loop tried to answer it with — and the sentence
    never reached the agent, while a typed prompt in the same situation was
    delivered because it routes before it acks. Now the worker routes first
    and every send after a disconnect is quiet; the note is on the board and
    the log has one line at info.
    """
    held = _HeldTranscriber(hold="finish", final="route me anyway")
    with caplog.at_level(logging.INFO, logger="aisquare.services.xr.server"):
        for http, project, token in _voice(work_dir, lambda: held):
            from aisquare.services import team as team_service

            team_service.activate(project.root)
            with _authed(http, token) as connection:
                _burst(connection, [FRAME])
                assert held.entered.wait(RECEIVE_TIMEOUT_S), "audioEnd never reached finish"
            # The socket is closed while finish() is still held; then let it return.
            held.release.set()
            assert _eventually(functools.partial(_note_filed, project.id, "route me anyway")), (
                "the committed sentence was never routed"
            )

    assert not [record for record in caplog.records if record.levelno > logging.INFO], (
        f"a client leaving is not an error: {[r.getMessage() for r in caplog.records]}"
    )


def test_a_store_that_cannot_be_read_at_connect_is_an_error_frame_and_a_clean_close(
    client: tuple[TestClient, ProjectInfo, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A locked or damaged context.db during hello/snapshot: say so, then close 1013.

    Left to escape, the store error was a traceback out of the ASGI app and
    a transport dropped with no close frame, which a client can only read as
    a network fault. ``board_unavailable`` says what happened and 1013 ("try
    again later") says what to do — reconnect with backoff, unlike 4401.
    """
    http, _project, token = client

    @contextlib.contextmanager
    def locked() -> Iterator[Any]:
        raise sqlite3.OperationalError("database is locked")
        yield  # pragma: no cover - the generator must be a generator

    monkeypatch.setattr(xr_server, "_store", locked)
    with http.websocket_connect("/ws") as connection:
        connection.send_text(json.dumps({"t": "auth", "token": token}))
        answer = json.loads(_text(connection))
        with pytest.raises(WebSocketDisconnect) as closed:
            connection.receive_text()
    assert answer["t"] == "error"
    assert answer["code"] == "board_unavailable"
    assert "database is locked" in answer["message"]
    assert closed.value.code == xr_server.CLOSE_TRY_AGAIN_LATER


def test_a_prompt_the_fleet_filed_as_a_note_is_acked_ok_and_a_delivery_that_raised_is_not(
    voice: tuple[TestClient, ProjectInfo, str, list[FakeTranscriber]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ack.ok`` means the text reached the agent by EITHER route, as the Ack docstring says.

    ``fleet.tell`` reports ``delivered=False`` whenever it filed a board note
    instead of typing — the agent was working, or its pane was not the agent
    yet — and passing that through as ``ok`` told the operator a prompt that
    was safely on the board was "not sent", while the no-pane path reported
    the identical outcome as ok. Only a delivery that raised is a failure.
    """
    http, project, token, _built = voice
    outcomes: list[fleet_service.TellResult | Exception] = [
        fleet_service.TellResult(False, "it is working — filed as board note #4 to coder-xr"),
        fleet_service.TellResult(True, "typed into its pane (it was waiting)"),
        fleet_service.FleetError("tmux is not running"),
    ]

    def tell(
        project_arg: ProjectInfo, label: str, text: str, *, sender: str | None = None
    ) -> fleet_service.TellResult:
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(fleet_service, "tell", tell)
    with store_session() as store:
        store.upsert_fleet_agent(
            FleetAgent(
                id="flt_ack",
                project_id=project.id,
                label="coder-xr",
                role="coder",
                pane_id="%9",
                session_id=CODER,
                cwd=project.root,
                created_at=datetime.now(tz=UTC),
            )
        )
    acks: list[dict[str, Any]] = []
    with _authed(http, token) as connection:
        for _ in range(3):
            connection.send_text(json.dumps({"t": "prompt", "session": CODER, "text": "go"}))
            acks.append(json.loads(_until(connection, "ack")))

    assert [(ack["ok"], ack["detail"]) for ack in acks] == [
        (True, "it is working — filed as board note #4 to coder-xr"),
        (True, "typed into its pane (it was waiting)"),
        (False, "tmux is not running"),
    ], acks


# --- the command ----------------------------------------------------------------


def test_show_token_prints_the_url_the_token_and_the_adb_line(runner: CliRunner) -> None:
    result = runner.invoke(app, ["xr", "--show-token"])
    assert result.exit_code == 0, result.output
    token = mcp_server.serve_token()
    assert f"http://localhost:8748/#token={token}" in result.output
    assert "adb reverse tcp:8748 tcp:8748" in result.output


def test_show_token_under_json_is_four_keys(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--json", "xr", "--show-token"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert set(payload) == {"url", "token", "bind", "adb_reverse"}
    assert payload["url"].endswith(payload["token"])
    assert payload["bind"] == "127.0.0.1"


def test_an_occupied_port_is_a_sentence_not_a_traceback(runner: CliRunner) -> None:
    """The likeliest failure of this command: one is already running."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        port = held.getsockname()[1]
        result = runner.invoke(app, ["xr", "--port", str(port)])
        as_json = runner.invoke(app, ["--json", "xr", "--port", str(port)])
    assert result.exit_code != 0
    assert str(port) in result.output and "in use" in result.output
    assert "Traceback" not in result.output
    assert json.loads(as_json.stdout)["error"] == "xr_port_busy"


def test_a_missing_extra_is_the_error_contract(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aisquare.cli import xr as xr_cli

    monkeypatch.setattr(xr_cli, "_find_spec", lambda name: None)
    result = runner.invoke(app, ["--json", "xr", "--show-token"])
    assert result.exit_code != 0
    payload = json.loads(result.stdout)
    assert payload["error"] == "xr_not_installed"


def test_xr_and_serve_share_one_token(runner: CliRunner) -> None:
    """Two servers, one credential — an operator wires up auth once."""
    xr_out = json.loads(runner.invoke(app, ["--json", "xr", "--show-token"]).stdout)
    serve_out = json.loads(runner.invoke(app, ["--json", "serve", "--show-token"]).stdout)
    assert xr_out["token"] == serve_out["token"]


def test_a_busy_store_does_not_end_the_ring(
    client: tuple[TestClient, ProjectInfo, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A locked read is transient; the next tick must still deliver.

    Ten agents on one SQLite file contend, and a connection that gave up on the
    first ``database is locked`` would be a ring that goes dark under exactly
    the load it exists for.
    """
    from aisquare.services.xr import projector

    real = projector.sessions
    failures = {"left": 3}

    def flaky(*args: Any, **kwargs: Any) -> Any:
        if failures["left"] > 0:
            failures["left"] -= 1
            raise sqlite3.OperationalError("database is locked")
        return real(*args, **kwargs)

    http, _project, token = client
    with _authed(http, token) as connection:
        monkeypatch.setattr(projector, "sessions", flaky)
        with store_session() as store:
            assert store.mark_attention(CODER)
        delta = json.loads(_text(connection))
    assert failures["left"] == 0, "the reads really did fail"
    assert [s["state"] for s in delta["changed"]] == ["needs_you"]


def test_a_record_written_in_two_writes_is_not_lost(work_dir: Path) -> None:
    """A poll that lands mid-write must not swallow the record.

    A transcript record is one conversation turn — kilobytes — so a tick
    landing inside one is ordinary, not exotic. Consuming the fragment and
    advancing past it parses nothing and then never sees the rest: the panel
    silently skips a turn, which is the worst kind of missing, because the
    stream looks healthy.
    """
    transcript = work_dir / "session.jsonl"
    transcript.write_text(
        json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "first"}]}})
        + "\n",
        encoding="utf-8",
    )
    project = _seed(work_dir, transcript=str(transcript))
    token = mcp_server.serve_token()
    record = (
        json.dumps(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "split turn"}]}}
        )
        + "\n"
    )
    head, tail = record[: len(record) // 2], record[len(record) // 2 :]

    with (
        TestClient(xr_server.build_app(project, token=token)) as http,
        _authed(http, token) as connection,
    ):
        connection.send_text(json.dumps({"t": "subscribe", "session": CODER}))
        assert json.loads(_text(connection))["text"] == "first"

        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(head)
            handle.flush()
        # Several polls pass over the fragment before the rest lands.
        time.sleep(xr_server.poll_interval() * 5)
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(tail)
            handle.flush()
        frame = json.loads(_text(connection))

    assert frame["text"] == "split turn"
    assert frame["seq"] == 2, "one record, delivered once"


def test_show_token_warns_about_a_non_loopback_bind(runner: CliRunner) -> None:
    """`serve --show-token` says this; the path an operator actually reads.

    A warning printed only when serving is a warning they meet after the
    decision, not before it.
    """
    open_bind = runner.invoke(app, ["xr", "--show-token", "--bind", "0.0.0.0"])
    loopback = runner.invoke(app, ["xr", "--show-token"])
    assert open_bind.exit_code == 0, open_bind.output
    assert "only gate" in open_bind.output and "trusted network" in open_bind.output
    assert "only gate" not in loopback.output, "loopback gives nothing up; stay quiet"


def test_xr_is_registered_as_a_command_the_sweeps_must_not_invoke() -> None:
    """`xr` binds a port and blocks, exactly as `serve` does.

    Three repo-wide sweeps invoke every leaf in the command tree
    (`test_no_traceback_on_a_damaged_store`, `test_no_traceback_in_a_configured_home`,
    `test_json_stdout_is_machine_readable`). A command that never returns hangs
    all three — not a failure, a WEDGE, which in CI is a job that burns its
    whole budget and reports nothing.

    This was not hypothetical. It was hidden for several full-suite runs because
    a teammate's server happened to be holding 8748, so `aisquare --json xr`
    exited immediately through `xr_port_busy` and the sweeps were green. They
    stopped their server; the next run hung at 51%. A guard that depends on
    somebody else's process is not a guard, so the entry is asserted here.
    """
    from tests.test_no_traceback_on_a_damaged_store import UNINVOKED

    assert "xr" in UNINVOKED, (
        "`aisquare xr` starts a blocking server, so the command-tree sweeps must "
        "not invoke it — add it to UNINVOKED beside `serve`"
    )
    assert "blocks" in UNINVOKED["xr"], "the reason must say why, as every entry does"


# --- unread badges --------------------------------------------------------------

SECOND = "bbbb3333-0000-0000-0000-000000000000"
"""Shares a four-character prefix with :data:`CODER`, which is the point."""


def _join(project: ProjectInfo, session_id: str, *, role: str = "runner") -> None:
    """Register a session on a board that is already being watched.

    Committed on its own, before any event for it, because that is the order
    the real thing happens in — a session registers and then works — and it is
    the order the poll loop's seeding is written against.
    """
    now = datetime.now(tz=UTC)
    with store_session() as store:
        store.upsert_session(
            TeamSession(
                id=session_id,
                project_id=project.id,
                role=role,
                started_at=now,
                last_seen_at=now,
            )
        )


def _say(project: ProjectInfo, session_id: str, count: int) -> None:
    """``count`` board events from one session: the thing a badge counts."""
    from aisquare.core.ids import new_event_id
    from aisquare.models import TeamEvent

    with store_session() as store:
        for index in range(count):
            store.add_team_event(
                TeamEvent(
                    id=new_event_id(),
                    project_id=project.id,
                    session_id=session_id,
                    kind="note",
                    text=f"working {index}",
                    created_at=datetime.now(tz=UTC),
                )
            )


def _badges_until(connection: Any, session_id: str, *, want: int) -> list[int]:
    """Every badge ``session_id`` reported, up to and including ``want``.

    Returns the trail instead of asserting on it, so the assertion stays where
    a reader can see it: ``test_every_test_can_fail.py`` is right that one
    buried in a helper is invisible, and it caught these four tests when they
    were written that way.

    The trail rather than the last value, because "the badge never moved" and
    "the badge moved and moved back" are different bugs and a bare timeout
    names neither. A stuck badge emits no delta at all, so the wait ends in
    :data:`RECEIVE_TIMEOUT_S` and the trail so far is the answer.
    """
    seen: list[int] = []
    for _ in range(60):
        try:
            frame = json.loads(_text(connection))
        except AssertionError:
            break  # nothing more is coming; the trail is what there is to report
        if frame.get("t") != "delta":
            continue
        for session in frame["changed"]:
            if session["id"] != session_id:
                continue
            seen.append(session["unread"])
            if session["unread"] == want:
                return seen
    return seen


def test_a_session_that_joins_after_the_client_connects_gets_a_badge(
    client: tuple[TestClient, ProjectInfo, str],
) -> None:
    """A late joiner counts from when the client learned of it, not from never.

    ``_seed_watermarks`` runs once, at connect, over the sessions that exist
    then. A session spawned while the operator is wearing the headset is not in
    that map, so :func:`projector.sessions` counts it from ``unread_floor`` — the
    board position this connection started at — which is where the events that
    announced it and everything it has said since all land. (Before, an
    un-watermarked session was counted by nobody and read 0 forever, and the
    poll loop re-seeded watermarks every tick to work around it; the floor
    default replaced that.)

    That is precisely the case the badge exists for: a newly spawned agent
    announcing it is working is a fresh agent shouting for attention with a
    blank badge, and spawning agents mid-session is what an operator does
    during the demo this is built for.
    """
    http, project, token = client
    with _authed(http, token) as connection:
        _join(project, SECOND)
        _say(project, SECOND, 3)
        badges = _badges_until(connection, SECOND, want=3)

    assert badges[-1:] == [3], (
        "a session that joined after this client connected reported "
        f"{badges or 'no badge at all'} — it must count from the connection floor"
    )


def test_a_session_present_at_connect_still_counts_from_the_connection(
    client: tuple[TestClient, ProjectInfo, str],
) -> None:
    """The path that already worked, pinned so the late-joiner seeding cannot bend it.

    A session the client could see at connect is watermarked at the board's
    position then, so its badge counts what happened while the operator was
    wearing the headset and nothing that happened before.
    """
    http, project, token = client
    _say(project, CODER, 5)  # before the socket exists: already read, by definition
    with _authed(http, token) as connection:
        _say(project, CODER, 3)
        badges = _badges_until(connection, CODER, want=3)

    assert badges[-1:] == [3], f"the five before the socket are not unread; got {badges}"


def test_subscribing_clears_the_badge_and_counting_resumes_from_there(
    work_dir: Path,
) -> None:
    """Focusing a panel marks it read, and the next event starts a new count."""
    transcript = work_dir / "session.jsonl"
    transcript.write_text(_record("first"), encoding="utf-8")
    project = _seed(work_dir, transcript=str(transcript))
    token = mcp_server.serve_token()
    with (
        TestClient(xr_server.build_app(project, token=token)) as http,
        _authed(http, token) as connection,
    ):
        _say(project, CODER, 3)
        counted = _badges_until(connection, CODER, want=3)
        connection.send_text(json.dumps({"t": "subscribe", "session": CODER}))
        cleared = _badges_until(connection, CODER, want=0)
        _say(project, CODER, 2)
        resumed = _badges_until(connection, CODER, want=2)

    assert counted[-1:] == [3], f"three events, three unread; got {counted}"
    assert cleared[-1:] == [0], f"focusing the panel marks it read; got {cleared}"
    assert resumed[-1:] == [2], f"and the count starts again from there; got {resumed}"


def test_subscribing_by_id_prefix_clears_the_badge_it_was_aimed_at(
    work_dir: Path,
) -> None:
    """The watermark belongs to the session, so it is keyed on the session's own id.

    ``store.get_session`` resolves PREFIXES — short ids work everywhere else in
    this repo — and every frame that goes back out carries ``row.id``. Keying
    the watermark on the string the CLIENT typed therefore wrote a watermark
    that nothing ever reads: the badge stayed where it was for the life of the
    socket, focusing the panel never cleared it, and the short string sat in
    the map counting nothing — the exact outcome the comment above that line
    says it is there to prevent.
    """
    transcript = work_dir / "session.jsonl"
    transcript.write_text(_record("first"), encoding="utf-8")
    project = _seed(work_dir, transcript=str(transcript))
    token = mcp_server.serve_token()
    with (
        TestClient(xr_server.build_app(project, token=token)) as http,
        _authed(http, token) as connection,
    ):
        _say(project, CODER, 4)
        counted = _badges_until(connection, CODER, want=4)
        connection.send_text(json.dumps({"t": "subscribe", "session": CODER[:8]}))
        cleared = _badges_until(connection, CODER, want=0)

    assert counted[-1:] == [4], f"four events, four unread; got {counted}"
    assert cleared[-1:] == [0], (
        "subscribing by prefix must clear the badge of the session it resolved to, "
        f"not of the string the client typed; got {cleared or 'no badge at all'}"
    )


def test_an_ambiguous_id_prefix_is_the_clients_error_not_an_internal_one(
    work_dir: Path,
) -> None:
    """A prefix matching two sessions is bad input, and must be named as such.

    ``AmbiguousIdError`` escaping ``_subscribe`` reaches ``_read_loop``'s
    catch-all, which reports every escape as ``internal`` — a server fault.
    The client is then told the server broke, when what happened is that it
    typed four characters where it needed five.
    """
    project = _seed(work_dir)
    _join(project, SECOND, role="coder")
    token = mcp_server.serve_token()
    with (
        TestClient(xr_server.build_app(project, token=token)) as http,
        _authed(http, token) as connection,
    ):
        connection.send_text(json.dumps({"t": "subscribe", "session": "bbbb"}))
        answer = json.loads(_until(connection, "error"))
        # Still alive: a fixable typo must not cost the operator their ring.
        connection.send_text(json.dumps({"t": "subscribe", "session": None}))

    assert answer["code"] == "ambiguous_session", (
        "a two-way prefix tie is the client's input, not a server fault"
    )
    assert "bbbb" in answer["message"], "and it must say which string was ambiguous"


def test_an_empty_or_glob_subscribe_is_a_bad_message_not_a_wildcard(
    client: tuple[TestClient, ProjectInfo, str],
) -> None:
    """An empty or glob-metacharacter subscribe id is refused, not resolved to '*'.

    ``Subscribe.session`` used to be a bare ``str | None``, so ``""`` (or a lone
    ``*``) passed validation and reached ``get_session``, whose prefix resolver
    strips those characters and turns them into ``GLOB '*'`` over the whole
    store — marking a real, arbitrary session read (or, with more than one
    session, answering ambiguous after the tail was already cancelled). The
    validator now rejects it at the wire boundary as ``bad_message``, and the
    socket stays open.
    """
    http, _project, token = client
    with _authed(http, token) as connection:
        connection.send_text(json.dumps({"t": "subscribe", "session": ""}))
        empty = json.loads(_until(connection, "error"))
        connection.send_text(json.dumps({"t": "subscribe", "session": "*"}))
        glob = json.loads(_until(connection, "error"))
        # Still alive: a bad ask must not cost the operator their ring.
        connection.send_text(json.dumps({"t": "subscribe", "session": None}))
    assert empty["code"] == "bad_message", empty
    assert glob["code"] == "bad_message", glob


def test_a_prefix_unique_on_this_board_resolves_despite_another_board(
    work_dir: Path,
) -> None:
    """A prefix unique here is not answered ambiguous because another board matches.

    ``_subscribe`` resolved through ``get_session``, whose ``GLOB`` fallback spans
    every project in the shared store, so a prefix unique on this board came back
    ``ambiguous_session`` because an unrelated project the operator cannot see had
    a session starting the same way — after the tail was already cancelled.
    Resolution is now scoped to this project.
    """
    transcript = work_dir / "session.jsonl"
    transcript.write_text(_record("hello from this board"), encoding="utf-8")
    project = _seed(work_dir, transcript=str(transcript))  # CODER = bbbb2222-...
    other = team_project(work_dir / "other-board")
    now = datetime.now(tz=UTC)
    with store_session() as store:
        store.ensure_project(other)
        store.upsert_session(
            TeamSession(
                id=SECOND,  # bbbb3333-..., shares the `bbbb` prefix, on ANOTHER board
                project_id=other.id,
                role="coder",
                started_at=now,
                last_seen_at=now,
            )
        )
    token = mcp_server.serve_token()
    with (
        TestClient(xr_server.build_app(project, token=token)) as http,
        _authed(http, token) as connection,
    ):
        connection.send_text(json.dumps({"t": "subscribe", "session": "bbbb"}))
        frame = json.loads(_until(connection, "transcript"))
    assert frame["text"] == "hello from this board", (
        "the prefix is unique on this board, so it must resolve rather than tie "
        "with a session on a board the operator cannot see"
    )


def test_a_failed_resubscribe_leaves_the_current_tail_running(work_dir: Path) -> None:
    """A subscribe that cannot resolve must not cancel the transcript already open.

    ``_subscribe`` cancelled the current tail FIRST and resolved second, so an
    ambiguous prefix (or an id from another board) left the operator with no tail
    AND an error. Resolution now happens first; the old tail is replaced only once
    a real session on this board is in hand.
    """
    transcript = work_dir / "session.jsonl"
    transcript.write_text(_record("first line"), encoding="utf-8")
    project = _seed(work_dir, transcript=str(transcript))  # CODER = bbbb2222-...
    _join(project, SECOND, role="coder")  # bbbb3333-..., so `bbbb` is ambiguous ON THIS BOARD
    token = mcp_server.serve_token()
    with (
        TestClient(xr_server.build_app(project, token=token)) as http,
        _authed(http, token) as connection,
    ):
        connection.send_text(json.dumps({"t": "subscribe", "session": CODER}))
        assert json.loads(_until(connection, "transcript"))["text"] == "first line"
        # A re-subscribe that cannot resolve: it must be an error, and it must
        # NOT kill the tail that is following CODER.
        connection.send_text(json.dumps({"t": "subscribe", "session": "bbbb"}))
        assert json.loads(_until(connection, "error"))["code"] == "ambiguous_session"
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(_record("second line"))
        followed = json.loads(_until(connection, "transcript"))
    assert followed["text"] == "second line", (
        "the CODER tail survived the failed re-subscribe and followed the new record"
    )


def test_a_poll_tick_needs_no_separate_seeding_read(work_dir: Path) -> None:
    """Seeding a late joiner adds NO per-tick store reads.

    The poll loop used to call ``_seed_late_joiners``, which re-read
    ``latest_seq`` and the entire session table every tick just to hand new
    sessions a watermark — taking a tick from five reads to seven. That pass is
    gone: :func:`projector.sessions` defaults an un-watermarked session to
    ``unread_floor``, so the only reads a tick makes are the projection's own.
    """
    from aisquare.services.xr import projector

    project = _seed(work_dir)
    _say(project, CODER, 2)
    with store_session() as store:
        floor = store.latest_seq(project.id)
        statements: list[str] = []
        conn = cast(Any, store)._conn
        conn.set_trace_callback(lambda stmt: statements.append(str(stmt)))
        projector.sessions(store, project.id, unread_since={CODER: floor}, unread_floor=floor)
        conn.set_trace_callback(None)
    selects = [s for s in statements if s.lstrip().upper().startswith(("SELECT", "WITH"))]
    assert len(selects) <= 5, (
        f"a tick reads at most the projection's own selects; got {len(selects)}"
    )
    assert not hasattr(xr_server._Connection, "_seed_late_joiners"), (
        "the per-tick late-joiner re-seed pass is gone; the floor default replaces it"
    )


# --- transcript tails -----------------------------------------------------------


def _record(*texts: str) -> str:
    """JSONL conversation turns, the shape ``_record_text`` reads."""
    return (
        "\n".join(
            json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": text}]}})
            for text in texts
        )
        + "\n"
    )


class _Recorder:
    """A websocket that only remembers, for driving one tail without a client."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [frame for frame in self.sent if frame.get("t") == kind]


async def _until_frames(recorder: _Recorder, kind: str, count: int, what: str) -> None:
    for _ in range(500):
        if len(recorder.of(kind)) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"only {len(recorder.of(kind))} {kind} frames arrived — {what}")


def test_the_transcript_tail_resyncs_when_the_file_shrinks(work_dir: Path) -> None:
    """A transcript that is replaced must not kill the panel for good.

    The tail seeks to a monotonically increasing offset and nothing compared it
    to the file's size, so the moment the file became SHORTER than what had
    already been read — a compaction, a ``/clear`` onto the same path, a log
    rotation, a restarted session handed the same ``transcript_path`` — every
    later read returned ``b""`` and was skipped by ``if not fresh: continue``.
    No frame, no error, no close, forever: the board deltas keep flowing on the
    same socket, so the operator sees a live ring beside a conversation that
    has quietly stopped and nothing anywhere says why. Re-subscribing is the
    only way back and nothing tells them to.

    The docstring on this method reasons carefully about the opposite case — a
    poll landing mid-write, where dropping a fragment costs one turn. This cost
    every turn from then on.
    """
    transcript = work_dir / "session.jsonl"
    transcript.write_text(_record("turn one", "turn two", "turn three"), encoding="utf-8")
    project = _seed(work_dir, transcript=str(transcript))
    with store_session() as store:
        row = store.get_session(CODER)
    assert row is not None

    recorder = _Recorder()
    # A factory is required to build a connection, but this one never opens a
    # burst: the subject is `_stream_transcript`, driven directly. The fake is
    # here to satisfy the constructor, not to be called.
    connection = xr_server._Connection(
        cast(Any, recorder),
        project=project,
        token="unused",
        transcriber_factory=lambda: FakeTranscriber(CANNED),
    )

    async def drive() -> list[str]:
        tail = asyncio.create_task(connection._stream_transcript(row))
        try:
            await _until_frames(recorder, "transcript", 3, "the backlog never replayed")
            before = [frame["text"] for frame in recorder.of("transcript")]
            assert before == ["turn one", "turn two", "turn three"]
            # The file is REPLACED by a shorter one, in place: this is what a
            # compaction or a reused transcript_path looks like from here.
            transcript.write_text(_record("after the compaction"), encoding="utf-8")
            await _until_frames(
                recorder,
                "transcript",
                4,
                "the tail is seeking past the end of a file that got shorter and "
                "will never produce another frame",
            )
            return [frame["text"] for frame in recorder.of("transcript")]
        finally:
            tail.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await tail

    delivered = asyncio.run(drive())

    assert delivered[-1] == "after the compaction", "the stream follows the file that is there now"
    assert recorder.of("error") == [], "a resync is recovery, not something to report"


def _drive_tail(
    project: ProjectInfo,
    row: TeamSession,
    body: Any,
) -> _Recorder:
    """Run one ``_stream_transcript`` against ``recorder`` while ``body`` mutates the file.

    ``body(recorder)`` is an async callable that drives the scenario and returns
    when it is done; the tail is always cancelled afterwards.
    """
    recorder = _Recorder()
    # A factory is required to build a connection, but these tails never open
    # a burst: the subject is `_stream_transcript`, driven directly. The fake
    # satisfies the constructor and is never called.
    connection = xr_server._Connection(
        cast(Any, recorder),
        project=project,
        token="unused",
        transcriber_factory=lambda: FakeTranscriber(CANNED),
    )

    async def run() -> None:
        tail = asyncio.create_task(connection._stream_transcript(row))
        try:
            await body(recorder)
        finally:
            tail.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await tail

    asyncio.run(run())
    return recorder


def test_a_transcript_replaced_by_a_longer_file_is_reread_from_the_start(work_dir: Path) -> None:
    """A replacement at least as long as the read offset is not read from a stale offset.

    Detection by ``size < offset`` alone misses this: the new file is longer, so
    the shrink check never fires, and the tail seeks to the old offset — landing
    inside the new file — and skips every record before it. The inode changes on
    ``os.replace``, so identity catches the swap and the replacement is read from
    its first record, with ``reset`` set so the client clears first.
    """
    transcript = work_dir / "session.jsonl"
    transcript.write_text(_record("short one", "short two"), encoding="utf-8")
    project = _seed(work_dir, transcript=str(transcript))
    with store_session() as store:
        row = store.get_session(CODER)
    assert row is not None

    async def body(recorder: _Recorder) -> None:
        await _until_frames(recorder, "transcript", 2, "the backlog never replayed")
        # A DIFFERENT, LONGER file atomically replaces it (new inode).
        replacement = work_dir / "session.jsonl.new"
        replacement.write_text(
            _record("brand new first", "brand new second", "brand new third"), encoding="utf-8"
        )
        replacement.replace(transcript)
        await _until_frames(
            recorder, "transcript", 5, "the replacement's leading records were skipped"
        )

    recorder = _drive_tail(project, row, body)
    texts = [frame["text"] for frame in recorder.of("transcript")]
    assert texts[:2] == ["short one", "short two"]
    assert texts[2:] == ["brand new first", "brand new second", "brand new third"], (
        "every record of the replacement must arrive, including the ones before the old offset"
    )
    resets = [frame for frame in recorder.of("transcript") if frame["reset"]]
    assert [frame["text"] for frame in resets] == ["brand new first"], (
        "exactly the first frame of the new file carries reset, so the client clears once"
    )
    assert recorder.of("transcript")[2]["seq"] == 1, "the sequence restarts with the new file"


def test_a_replacement_replays_a_bounded_backlog_not_the_whole_file(work_dir: Path) -> None:
    """A resync applies the same backlog cap as a fresh subscribe, and flags a reset.

    When the file shrank the old code set ``offset = 0`` and replayed the ENTIRE
    replacement through one unbounded read — thousands of frames on a large
    rewrite, seq still climbing, no reset — so the client appended a whole
    conversation it already had. The resync now reads only the bounded backlog
    and marks it a restart.
    """
    transcript = work_dir / "session.jsonl"
    transcript.write_text(_record("original"), encoding="utf-8")
    project = _seed(work_dir, transcript=str(transcript))
    with store_session() as store:
        row = store.get_session(CODER)
    assert row is not None

    # A replacement of many small records, well past TRANSCRIPT_BACKLOG_BYTES.
    many = _record(*[f"line {index}" for index in range(4000)])
    assert len(many.encode()) > xr_server.TRANSCRIPT_BACKLOG_BYTES * 4

    async def body(recorder: _Recorder) -> None:
        await _until_frames(recorder, "transcript", 1, "the backlog never replayed")
        # Atomically replace it (new inode) with the large file: this is the
        # resync trigger, and the old code would then replay all 4000 records.
        replacement = work_dir / "session.jsonl.new"
        replacement.write_text(many, encoding="utf-8")
        replacement.replace(transcript)
        await _until_frames(recorder, "transcript", 2, "the replacement never replayed")
        # Let a few more ticks pass so an unbounded replay would show itself.
        await asyncio.sleep(xr_server.poll_interval() * 5)

    recorder = _drive_tail(project, row, body)
    after = recorder.of("transcript")[1:]
    assert 0 < len(after) < 4000, (
        f"the replay must be bounded, not the whole file; got {len(after)}"
    )
    assert after[0]["reset"] is True and after[0]["seq"] == 1, "the restart is flagged and reseq'd"


def test_the_tail_follows_a_row_repointed_at_a_new_transcript(work_dir: Path) -> None:
    """A resumed session re-pointed at a new file is followed there, not on the old one.

    The tail copied the path from the row at subscribe and never looked again, so
    an ``ordinary resume`` that rewrote ``transcript_path`` left the panel reading
    the old, now-static file forever. Re-reading the row each tick switches files.
    """
    first = work_dir / "conv1.jsonl"
    first.write_text(_record("from the first file"), encoding="utf-8")
    project = _seed(work_dir, transcript=str(first))
    with store_session() as store:
        row = store.get_session(CODER)
    assert row is not None
    second = work_dir / "conv2.jsonl"

    async def body(recorder: _Recorder) -> None:
        await _until_frames(recorder, "transcript", 1, "the first file never replayed")
        second.write_text(_record("from the second file"), encoding="utf-8")
        now = datetime.now(tz=UTC)
        with store_session() as store:
            store.upsert_session(
                TeamSession(
                    id=CODER,
                    project_id=project.id,
                    role="coder",
                    started_at=now,
                    last_seen_at=now,
                    transcript_path=str(second),
                )
            )
        await _until_frames(recorder, "transcript", 2, "the tail never switched to the new file")

    recorder = _drive_tail(project, row, body)
    texts = [frame["text"] for frame in recorder.of("transcript")]
    assert texts == ["from the first file", "from the second file"], texts
    assert recorder.of("transcript")[1]["reset"] is True, "switching files clears the panel first"


def test_a_rotated_transcript_recovers_rather_than_dying(work_dir: Path) -> None:
    """A rename-then-create rotation is a gap to wait out, not the end of the tail.

    The path is briefly missing between the rename and the recreate. Ending the
    tail on the first ``FileNotFoundError`` turned that ordinary gap into a dead
    panel for the life of the connection; the tail now retries and picks up the
    recreated file.
    """
    transcript = work_dir / "session.jsonl"
    transcript.write_text(_record("before rotation"), encoding="utf-8")
    project = _seed(work_dir, transcript=str(transcript))
    with store_session() as store:
        row = store.get_session(CODER)
    assert row is not None

    async def body(recorder: _Recorder) -> None:
        await _until_frames(recorder, "transcript", 1, "the pre-rotation record never replayed")
        transcript.rename(work_dir / "session.jsonl.1")  # the path is now missing
        await asyncio.sleep(xr_server.poll_interval() * 2)  # a tick or two with no file
        transcript.write_text(_record("after rotation"), encoding="utf-8")  # recreated
        await _until_frames(recorder, "transcript", 2, "the tail never recovered the new file")

    recorder = _drive_tail(project, row, body)
    texts = [frame["text"] for frame in recorder.of("transcript")]
    assert texts == ["before rotation", "after rotation"], texts
    assert recorder.of("error") == [], "a rotation that comes back is recovery, not an error"


def test_a_transcript_that_never_comes_back_is_surfaced(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A loss that outlasts the grace period is reported, not swallowed forever.

    The old loop returned on the first ``OSError`` with no frame at all, so a
    permanently-lost transcript looked exactly like a live-but-quiet one. Now the
    client gets ``transcript_gone`` once the file has been missing longer than
    :data:`TRANSCRIPT_MISSING_GRACE_S`.
    """
    monkeypatch.setattr(xr_server, "TRANSCRIPT_MISSING_GRACE_S", 0.05)
    transcript = work_dir / "session.jsonl"
    transcript.write_text(_record("last words"), encoding="utf-8")
    project = _seed(work_dir, transcript=str(transcript))
    with store_session() as store:
        row = store.get_session(CODER)
    assert row is not None

    async def body(recorder: _Recorder) -> None:
        await _until_frames(recorder, "transcript", 1, "the record never replayed")
        transcript.unlink()  # gone for good
        await _until_frames(recorder, "error", 1, "a permanent loss was never surfaced")

    recorder = _drive_tail(project, row, body)
    assert [frame["code"] for frame in recorder.of("error")] == ["transcript_gone"]


def test_the_backlog_replays_a_final_record_larger_than_the_window(work_dir: Path) -> None:
    """The backlog always includes the last complete record, even past the budget.

    Reading the last :data:`TRANSCRIPT_BACKLOG_BYTES` and dropping the leading
    fragment replays NOTHING when the final record fills the whole window — an
    8 KB tool_result, a long answer — because the window holds only that record's
    tail and its terminating newline, and dropping the fragment before that
    newline drops the record. The panel then sits on "waiting for transcript…"
    until a new record is written. The backlog now walks back far enough to carry
    the whole last record.
    """
    big = "x" * (xr_server.TRANSCRIPT_BACKLOG_BYTES + 1000)
    transcript = work_dir / "session.jsonl"
    transcript.write_text(_record("small first", big), encoding="utf-8")
    project = _seed(work_dir, transcript=str(transcript))
    with store_session() as store:
        row = store.get_session(CODER)
    assert row is not None

    async def body(recorder: _Recorder) -> None:
        await _until_frames(
            recorder, "transcript", 1, "a final record larger than the window vanished"
        )

    recorder = _drive_tail(project, row, body)
    texts = [frame["text"] for frame in recorder.of("transcript")]
    assert big in texts, (
        "the last record must replay even though it is larger than the backlog budget"
    )


# --- push-to-talk bursts --------------------------------------------------------


def test_a_burst_that_ends_on_a_different_session_keeps_the_headers_owner(
    voice: tuple[TestClient, ProjectInfo, str, list[FakeTranscriber]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``audio`` owns the burst; a mismatched ``audioEnd`` is logged, not refused.

    The header's ``session`` used to be dropped on the floor and the burst
    attributed to whatever ``audioEnd`` carried, so ``audio(session=A)``
    followed by ``audioEnd(session=B)`` transcribed A's microphone into B's
    panel, silently — and the schema could not warn anyone, because it required
    ``session`` on both frames and documented no relationship between them.

    The header wins rather than the burst being refused. Both close the defect,
    but only this one keeps the operator's sentence: the microphone was opened
    for A and the samples were recorded for A, so A is where they belong.
    Discarding a spoken sentence over a client's bookkeeping bug would spend
    the operator's words on our problem. The disagreement is logged instead,
    because a client whose two frames disagree is still worth finding.
    """
    http, project, token, _built = voice
    from aisquare.services import team as team_service

    team_service.activate(project.root)
    with (
        caplog.at_level(logging.INFO, logger=xr_server.__name__),
        _authed(http, token) as connection,
    ):
        connection.send_text(json.dumps({"t": "audio", "session": CODER, "seq": 0}))
        for _ in range(FRAMES_PER_INTERIM):
            connection.send_bytes(FRAME)
        interim = json.loads(_until(connection, "stt"))
        connection.send_text(json.dumps({"t": "audioEnd", "session": SECOND}))
        final = json.loads(_until(connection, "stt"))
        ack = json.loads(_until(connection, "ack"))

    assert interim["final"] is False, "one second of audio earns an interim decode first"
    assert final == {"t": "stt", "text": CANNED, "final": True}
    assert ack["session"] == CODER, "the words go to the panel whose microphone was opened"
    assert ack["ok"] is True, "a client bookkeeping bug must not cost the operator a sentence"

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert CODER in logged and SECOND in logged, (
        "the disagreement is not answered, so the log is the only place it is recorded"
    )
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert CODER in logged and SECOND in logged, (
        "the disagreement is not answered, so the log is the only record of it"
    )
