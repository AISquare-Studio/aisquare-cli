"""The cliXR transport end to end: static assets, token auth, frames, the CLI.

Driven through ``starlette.testclient``, which runs the real ASGI app — real
routing, a real websocket handshake, the real poll task — in a thread. The
board is mutated from the test thread through its own store handle, which is
exactly what a Claude Code hook does from its own process, so a delta here
proves the same path a headset sees.
"""

from __future__ import annotations

import contextlib
import json
import socket
import sqlite3
import threading
import time
from array import array
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core.orchestrator import team_project
from aisquare.core.paths import HOME_ENV_VAR
from aisquare.core.store import store_session
from aisquare.models import FleetAgent, ProjectInfo, TeamSession

pytest.importorskip("starlette.testclient", reason="the [xr] extra is not installed")

from starlette.testclient import TestClient

from aisquare.services import fleet as fleet_service
from aisquare.services import mcp_server
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


def test_auth_rejects_a_missing_a_wrong_and_a_foreign_token(
    work_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One answer for every way of not being authorised.

    The third case is the one worth having: a token is per ``AISQUARE_HOME``,
    so a client holding one minted against a different home — another machine,
    another checkout, an old install — is refused exactly like a guess.
    """
    project = _seed(work_dir)
    mine = mcp_server.serve_token()

    monkeypatch.setenv(HOME_ENV_VAR, str(tmp_path / "other-home"))
    foreign = mcp_server.serve_token()
    monkeypatch.setenv(HOME_ENV_VAR, str(tmp_path / "aisquare-home"))
    assert foreign != mine, "two homes must not share a token"

    with TestClient(xr_server.build_app(project, token=mine)) as http:
        for frame in (
            None,  # a first frame that is not an auth at all
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


def _speak(connection: Any, *, frames: int = FRAMES_PER_INTERIM, session: str = CODER) -> None:
    """A push-to-talk burst: header, ``frames`` binary frames, ``audioEnd``."""
    connection.send_text(json.dumps({"t": "audio", "session": session, "seq": 0}))
    for _ in range(frames):
        connection.send_bytes(FRAME)
    connection.send_text(json.dumps({"t": "audioEnd", "session": session}))


def test_the_default_factory_is_the_real_backend_and_is_replaceable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seam, at rest: a live server transcribes for real, a test does not.

    Asserted by identity and never called — invoking it here would load a
    whisper model on whatever machine is running the suite.
    """
    assert xr_server.transcriber_factory() is speech.transcriber

    def fake() -> Transcriber:
        return FakeTranscriber(CANNED)

    monkeypatch.setattr(xr_server, "_factory", speech.transcriber)
    xr_server.set_transcriber_factory(fake)
    assert xr_server.transcriber_factory() is fake
    xr_server.set_transcriber_factory(None)
    assert xr_server.transcriber_factory() is speech.transcriber, "None restores the real one"


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
    assert bytes(built[0].fed) == FRAME * FRAMES_PER_INTERIM, "every frame arrived, in order"
    assert built[0].finished


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
            with store_session() as store:
                store.mark_attention(CODER)
            frames = _drain_until(connection, "delta")

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
            final = json.loads(_until(connection, "stt"))
            while final["final"] is False:
                final = json.loads(_until(connection, "stt"))
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
    assert built[0].fed == b"", "the abandoned burst never fed the model it was holding"


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
            with store_session() as store:
                store.mark_attention(CODER)
            frames = _drain_until(connection, "delta")

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
    entered = threading.Event()
    release = threading.Event()

    class SlowTranscriber:
        def feed(self, pcm: bytes) -> str | None:
            entered.set()
            release.wait(RECEIVE_TIMEOUT_S)
            return None

        def finish(self) -> str:
            return "held"

    try:
        for http, _project, token in _voice(work_dir, SlowTranscriber):
            with _authed(http, token) as connection:
                connection.send_text(json.dumps({"t": "audio", "session": CODER, "seq": 0}))
                connection.send_bytes(FRAME)
                assert entered.wait(RECEIVE_TIMEOUT_S), "the frame never reached the transcriber"
                with store_session() as store:
                    store.mark_attention(CODER)
                delta = json.loads(_until(connection, "delta"))
                assert not release.is_set(), "the decode is still running"
    finally:
        # Always, even on a failure: an un-released worker would be joined at
        # interpreter exit and turn one failing test into a wedged run.
        release.set()
    assert [session["state"] for session in delta["changed"]] == ["needs_you"]


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

A stand-in for "the backend threw mid-utterance", which used to be reachable
with an odd-length frame and no longer is — see
``test_an_odd_frame_anywhere_is_transcribed_not_failed``. The failure it
provokes is the real one, not a patched method: ``BufferedTranscriber`` calls
``_reset()`` only AFTER ``_decode`` returns, so a decode that raises leaves the
poisoned bytes in the buffer and every later decode on that object raises too.
That is what makes dropping the object the fix and reusing it a permanent loss
of voice.
"""


def _strict_decode(pcm: bytes) -> str:
    """Production's decode shape: reject a half sample, and reject the poison.

    ``speech.py``'s real decoder opens with ``numpy.frombuffer(pcm,
    dtype=numpy.int16)``, which raises on a length that is not a multiple of
    two. numpy ships in the optional ``xr`` extra, so it is used when it is
    installed — which is what CI's ``[dev,xr]`` job runs — and ``array``, which
    raises on exactly the same lengths for exactly the same reason, stands in
    under ``[dev]``. The property is pinned in both, rather than skipped in one.
    """
    try:
        import numpy
    except ImportError:
        array("h").frombytes(pcm)
    else:
        numpy.frombuffer(pcm, dtype=numpy.int16).astype(numpy.float32) / 32768.0
    if _POISON in pcm:
        raise ValueError("the backend refuses this audio")
    return "real words"


def _real_voice(
    work_dir: Path,
) -> Iterator[tuple[TestClient, ProjectInfo, str, list[BufferedTranscriber]]]:
    """A socket whose transcriber is the REAL buffering one over :func:`_strict_decode`.

    The list is the only way to see the claim these tests are about: nothing on
    the wire says whether the connection built a new transcriber or reused the
    one it already had.
    """
    built: list[BufferedTranscriber] = []

    def factory() -> BufferedTranscriber:
        made = BufferedTranscriber(_strict_decode)
        built.append(made)
        return made

    for http, project, token in _voice(work_dir, factory):
        yield http, project, token, built


def _burst(connection: Any, frames: list[bytes], *, session: str = CODER) -> None:
    """A push-to-talk burst of exactly these frames, in this order."""
    connection.send_text(json.dumps({"t": "audio", "session": session, "seq": 0}))
    for frame in frames:
        connection.send_bytes(frame)
    connection.send_text(json.dumps({"t": "audioEnd", "session": session}))


def _tone_frames(*, odd_at: int | None = None, poison_at: int | None = None) -> list[bytes]:
    """Enough loud frames to earn an interim, with one frame spoiled at ``at``."""
    frames = [TONE] * (FRAMES_PER_INTERIM + 1)
    if odd_at is not None:
        frames[odd_at] = TONE + b"\x01"
    if poison_at is not None:
        frames[poison_at] = _POISON + TONE[len(_POISON) :]
    return frames


def _final_stt(connection: Any) -> dict[str, Any]:
    """Drain interims and return the ``stt`` frame with ``final: true``."""
    frame = json.loads(_until(connection, "stt"))
    while frame["final"] is False:
        frame = json.loads(_until(connection, "stt"))
    return dict(frame)


@pytest.mark.parametrize("where", [0, FRAMES_PER_INTERIM // 2, FRAMES_PER_INTERIM])
def test_an_odd_frame_anywhere_is_transcribed_not_failed(work_dir: Path, where: int) -> None:
    """A frame that ends mid-sample costs nothing — not the sentence, not the next one.

    ``_on_audio_frame``'s docstring tells a client author the frame size is
    their business and any size will do. It is true only because ``feed``
    carries the trailing odd byte into the next chunk; concatenated raw, one
    639-byte frame flips the buffer's parity permanently and every decode after
    it raises, which reaches the operator as a bare ``stt_failed`` for a
    sentence they now have to say again with no clue why.

    First frame, mid-utterance and the last frame before ``audioEnd`` are all
    tested because they fail differently: the first is the one the gate sees,
    and the last is the only one that can reach ``finish`` without an interim
    having decoded first.
    """
    for http, _project, token, built in _real_voice(work_dir):
        with _authed(http, token) as connection:
            _burst(connection, _tone_frames(odd_at=where))
            final = _final_stt(connection)
            assert final["text"] == "real words", f"the odd byte cost the sentence: {final}"
            connection.send_text(json.dumps({"t": "prompt", "session": CODER, "text": "after"}))
            assert json.loads(_until(connection, "ack"))["ok"] is True
        assert len(built) == 1, "a clean utterance keeps the model it loaded"


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
    ``_forget_transcriber`` that did nothing on any day the poison happened to
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
        final = json.loads(_until(first, "stt"))
        while final["final"] is False:
            final = json.loads(_until(first, "stt"))
    assert final["text"] == CANNED
    assert len(built) == 1, "the stray-frame client never needed a transcriber"


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
