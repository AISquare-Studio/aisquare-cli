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
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
