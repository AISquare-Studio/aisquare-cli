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
import json
import socket
import sqlite3
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core.orchestrator import team_project
from aisquare.core.paths import HOME_ENV_VAR
from aisquare.core.store import store_session
from aisquare.models import ProjectInfo, TeamSession

pytest.importorskip("starlette.testclient", reason="the [xr] extra is not installed")

from starlette.testclient import TestClient

from aisquare.services import mcp_server
from aisquare.services.xr import protocol as wire
from aisquare.services.xr import server as xr_server

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


def test_audio_without_a_speech_backend_says_so(
    client: tuple[TestClient, ProjectInfo, str],
) -> None:
    """``TRANSCRIBE`` is ``None`` in this tree; a later task wires it in."""
    http, _project, token = client
    assert xr_server.TRANSCRIBE is None
    with _authed(http, token) as connection:
        connection.send_text(json.dumps({"t": "audio", "session": CODER, "seq": 0}))
        connection.send_bytes(b"\x00\x01" * 16)
        connection.send_text(json.dumps({"t": "audioEnd", "session": CODER}))
        answer = json.loads(_until(connection, "error"))
    assert answer["code"] == "stt_unavailable"


def test_a_buffered_burst_reaches_the_transcribe_hook(
    client: tuple[TestClient, ProjectInfo, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seam the speech task plugs into: bytes in, one ``stt`` frame out."""
    http, _project, token = client
    seen: list[bytes] = []

    def transcribe(payload: bytes) -> str:
        seen.append(payload)
        return "open the ring"

    monkeypatch.setattr(xr_server, "TRANSCRIBE", transcribe)
    with _authed(http, token) as connection:
        connection.send_text(json.dumps({"t": "audio", "session": CODER, "seq": 0}))
        connection.send_bytes(b"abc")
        connection.send_bytes(b"def")
        connection.send_text(json.dumps({"t": "audioEnd", "session": CODER}))
        answer = json.loads(_until(connection, "stt"))
    assert seen == [b"abcdef"], "binary frames are concatenated in order"
    assert answer == {"t": "stt", "text": "open the ring", "final": True}


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
    connection = xr_server._Connection(cast(Any, recorder), project=project, token="unused")

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
    connection = xr_server._Connection(cast(Any, recorder), project=project, token="unused")

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
    client: tuple[TestClient, ProjectInfo, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``audio`` owns the burst; a mismatched ``audioEnd`` is logged, not refused.

    The header's ``session`` opened the microphone and the samples were recorded
    for it, so it wins. Refusing instead (the old ``session_mismatch``) discarded
    the operator's sentence over a client bookkeeping bug — and worse, a header
    addressed by PREFIX with an ``audioEnd`` carrying the FULL id (the ids the
    server's own frames use) disagreed as raw strings and so was refused every
    single time, though both name the same session. Now the burst is transcribed
    and the disagreement is logged.
    """
    import logging

    http, _project, token = client
    monkeypatch.setattr(xr_server, "TRANSCRIBE", lambda payload: "open the ring")
    with (
        caplog.at_level(logging.INFO, logger=xr_server.__name__),
        _authed(http, token) as connection,
    ):
        connection.send_text(json.dumps({"t": "audio", "session": CODER, "seq": 0}))
        connection.send_bytes(b"\x00\x01" * 160)
        connection.send_text(json.dumps({"t": "audioEnd", "session": SECOND}))
        stt = json.loads(_until(connection, "stt"))

    assert stt == {"t": "stt", "text": "open the ring", "final": True}, (
        "the header owns the burst, so the speech is transcribed rather than dropped"
    )
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert CODER in logged and SECOND in logged, (
        "the disagreement is not answered, so the log is the only record of it"
    )


def test_a_burst_past_the_cap_is_answered_once_and_not_called_empty(
    client: tuple[TestClient, ProjectInfo, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A burst over :data:`MAX_AUDIO_BYTES` gets one ``audio_too_long``, not ``stt_empty``.

    The old path dropped an over-cap burst with no frame, silently discarded
    every later frame, and then answered ``audioEnd`` with ``stt_empty`` "no
    audio arrived" although megabytes had — telling the operator "no audio" on
    every retry of a burst that was simply too long. Now the cap is answered once
    and the eventual ``audioEnd`` says nothing more.
    """
    http, _project, token = client
    seen: list[bytes] = []

    def transcribe(payload: bytes) -> str:
        seen.append(payload)
        return "heard"

    monkeypatch.setattr(xr_server, "TRANSCRIBE", transcribe)
    monkeypatch.setattr(xr_server, "MAX_AUDIO_BYTES", 1000)
    with _authed(http, token) as connection:
        connection.send_text(json.dumps({"t": "audio", "session": CODER, "seq": 0}))
        for _ in range(3):
            connection.send_bytes(b"\x00" * 400)  # 1200 B > 1000 B cap
        for _ in range(4):
            connection.send_bytes(b"\x00" * 400)  # later frames: silently dropped
        connection.send_text(json.dumps({"t": "audioEnd", "session": CODER}))
        first = json.loads(_until(connection, "error"))
        # A second subscribe/None round-trips a frame, proving the socket lives
        # and that audioEnd produced no stt_empty and no stt behind the error.
        connection.send_text(json.dumps({"t": "subscribe", "session": None}))
    assert first["code"] == "audio_too_long", first
    assert str(1000) in first["message"], "the message must state the cap it hit"
    assert seen == [], "an over-cap burst never reaches the transcriber"
