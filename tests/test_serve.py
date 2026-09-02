"""The MCP ingress: remote clients act as attributed virtual sessions."""

from __future__ import annotations

import json
import logging
import stat
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app

pytest.importorskip("mcp", reason="the [serve] extra is not installed")

import anyio
from mcp.client import Client
from mcp.types import CallToolResult, TextContent

from aisquare.services import mcp_server
from aisquare.services import team as team_service
from aisquare.services.team import ClaimLostError


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


def call_remote(tool: str, arguments: dict[str, Any] | None = None) -> CallToolResult:
    """Call one tool end-to-end through an in-memory MCP client session.

    ``Client(server, mode="legacy")`` is what replaced the removed
    ``create_connected_server_and_client_session`` in mcp 2, and takes the same
    path it did: a real client session over memory streams with JSON-RPC
    request/response envelopes and an initialize handshake, the result
    JSON-dumped, sieved at the negotiated 2025-11-25 surface and re-parsed on
    the client — what a handshake-era remote agent gets. (The streams carry
    the envelope objects rather than their text; that last encoding step is
    the stdio transport's alone.) The default ``mode="auto"`` is a
    ``DirectDispatcher`` pair — 2026-07-28, no handshake, no envelopes, the
    same JSON dump — and reaches the same registered ``tools/call`` handler;
    ``test_the_modern_in_process_path_reaches_the_same_handler`` proves that
    once, and every other assertion here takes the handshake-era path.
    """

    async def go() -> CallToolResult:
        async with Client(mcp_server.build_server(), mode="legacy") as client:
            return await client.call_tool(tool, arguments or {})

    return anyio.run(go)


def error_text(result: CallToolResult) -> str:
    """The message of an MCP error result (asserting ``is_error`` on the way)."""
    assert result.is_error, f"expected an MCP error result, got success: {result.content!r}"
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def test_serve_token_is_created_once_with_tight_permissions(isolated_home: Path) -> None:
    first = mcp_server.serve_token()
    second = mcp_server.serve_token()
    assert first == second and len(first) > 30
    creds = isolated_home / "credentials"
    assert stat.S_IMODE(creds.stat().st_mode) == 0o600


def test_remote_tools_act_as_an_attributed_virtual_session(
    runner: CliRunner, work_dir: Path
) -> None:
    team_service.activate()
    added = mcp_server.task_add("bug: dropdown flickers", role="coder")
    assert added.startswith("created:")
    again = mcp_server.task_add("bug: dropdown flickers")
    assert "idempotent" in again
    board = mcp_server.team_board()
    assert "mcp:remo" in board and "dropdown flickers" in board

    # The remote's activity reaches local sessions' deltas, attributed.
    payload = json.dumps(
        {"cwd": str(work_dir), "session_id": "cccc3333-0000-0000-0000-000000000000"}
    )
    runner.invoke(app, ["hook", "session-start"], input=payload)
    note = mcp_server.note_add("repro: happens on Windows Chrome only", kind="result")
    assert note.startswith("shared (result)")
    from aisquare.core.orchestrator import team_project

    assert f"on board {team_project(work_dir).id}" in note  # the receipt names the board
    delta = runner.invoke(
        app,
        ["hook", "user-prompt-submit"],
        input=json.dumps(
            {
                "cwd": str(work_dir),
                "session_id": "cccc3333-0000-0000-0000-000000000000",
                "prompt": "go",
            }
        ),
    )
    assert "Windows Chrome" in delta.stdout and "mcp:remo" in delta.stdout


def test_remote_task_lifecycle_and_guards(work_dir: Path) -> None:
    team_service.activate()
    mcp_server.task_add("verify login flow", role="debugger")
    picked = json.loads(mcp_server.task_next(role="debugger", claim=True))
    assert picked["status"] == "doing" and picked["claimed_by"].startswith("mcp:remote:")
    reopen = call_remote("task_update", {"ref": picked["id"], "action": "reopen"})
    assert error_text(reopen) == "error: reopen requires a note (the feedback)"
    block = call_remote("task_update", {"ref": picked["id"], "action": "block"})
    assert error_text(block) == "error: block requires a note (the reason)"
    moved = mcp_server.task_update(picked["id"], "review", note="ready to verify")
    assert moved.endswith("is now review")
    bogus = call_remote("task_update", {"ref": picked["id"], "action": "bogus"})
    assert error_text(bogus) == "error: action must be done, review, block, reopen, release or drop"
    log = json.loads(mcp_server.team_log())
    assert log["latest_seq"] > 0
    kinds = {event["kind"] for event in log["events"]}
    assert "team.task_claimed" in kinds and "team.task_review" in kinds


def test_server_exposes_exactly_the_nine_tools() -> None:
    server = mcp_server.build_server()

    tools = anyio.run(server.list_tools)
    assert {tool.name for tool in tools} == {
        "team_board",
        "task_add",
        "task_next",
        "task_update",
        "note_add",
        "team_log",
        "verify",
        "signal",
        "recall",
    }


def test_show_token_cli(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--json", "serve", "--show-token"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["url"].endswith(":8747/mcp") and payload["token"]


def test_remote_calls_never_activate_an_unopted_project(work_dir: Path) -> None:
    # No activate(): one read-only call must not flip the repo's orchestrator on.
    result = call_remote("team_board")
    text = error_text(result)
    assert text.startswith("error:") and "not active" in text
    from aisquare.core.orchestrator import team_project
    from aisquare.core.store import store_session

    with store_session() as store:
        assert not store.team_active(team_project(work_dir).id)


def test_remote_calls_respect_master_switch(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every tool's failure must be an MCP error result with the exact wording.
    team_service.activate()
    monkeypatch.setenv("AISQUARE_TEAM", "0")
    calls: list[tuple[str, dict[str, Any]]] = [
        ("team_board", {}),
        ("task_add", {"title": "x"}),
        ("task_next", {}),
        ("task_update", {"ref": "tsk_x", "action": "done"}),
        ("note_add", {"text": "x"}),
        ("team_log", {}),
        ("recall", {"query": "x"}),
    ]
    for tool, arguments in calls:
        result = call_remote(tool, arguments)
        assert (
            error_text(result)
            == "error: the agent orchestrator is disabled on the host (AISQUARE_TEAM=0)"
        ), tool


def test_remote_success_results_stay_plain_tool_results(work_dir: Path) -> None:
    # The wording-preserving handler must leave the success path untouched.
    team_service.activate()
    result = call_remote("task_add", {"title": "wire auth flow"})
    assert not result.is_error
    block = result.content[0]
    assert isinstance(block, TextContent)
    assert block.text.startswith("created: tsk_")


def test_virtual_session_is_scoped_per_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aisquare.core.orchestrator import team_project
    from aisquare.core.store import store_session

    ids = []
    for name in ("repo-a", "repo-b"):
        repo = tmp_path / name
        repo.mkdir()
        monkeypatch.chdir(repo)
        team_service.activate()
        mcp_server.note_add(f"hello from {name}")
        ids.append(mcp_server.client_session_id(team_project(repo).id))
    assert ids[0] != ids[1]  # one row per project — no phantom pinning
    with store_session() as store:
        for session_id, repo_name in zip(ids, ("repo-a", "repo-b"), strict=True):
            session = store.get_session(session_id)
            assert session is not None, repo_name


def test_task_next_rejects_bad_status(work_dir: Path) -> None:
    team_service.activate()
    result = call_remote("task_next", {"status": "reviwe"})
    assert error_text(result) == (
        "error: status must be one of: todo, doing, review, blocked, done, dropped"
    )


def test_guard_reports_ambiguous_refs_cleanly(work_dir: Path) -> None:
    team_service.activate()
    mcp_server.task_add("alpha")
    mcp_server.task_add("beta")
    result = call_remote("task_update", {"ref": "tsk_", "action": "done"})
    text = error_text(result)
    assert text.startswith("error:") and "ambiguous" in text


def test_stdio_serve_refuses_markerless_directories(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bare = tmp_path / "no-markers-here"
    bare.mkdir()
    monkeypatch.chdir(bare)
    monkeypatch.delenv("AISQUARE_TEAM_HUB", raising=False)
    result = runner.invoke(app, ["--json", "serve", "--stdio"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"] == "not_a_project"


def test_serve_respects_master_switch(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_TEAM", "0")
    result = runner.invoke(app, ["--json", "serve", "--stdio"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"] in ("team_disabled", "not_a_project")


def test_bearer_guard_rejects_non_ascii_header_with_401() -> None:
    import asyncio

    from aisquare.services.mcp_server import _BearerGuard

    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return {"type": "http.request"}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    async def inner(scope: dict[str, object], receive: object, send: object) -> None:
        sent.append({"type": "PASSED_THROUGH"})

    guard = _BearerGuard(inner, "the-real-token")
    scope: dict[str, object] = {
        "type": "http",
        "headers": [(b"authorization", b"Bearer caf\xe9")],
    }
    asyncio.run(guard(scope, receive, send))  # non-ASCII byte, used to 500

    starts = [m for m in sent if m.get("type") == "http.response.start"]
    assert starts and starts[0]["status"] == 401  # clean 401, not a TypeError 500
    assert not any(m.get("type") == "PASSED_THROUGH" for m in sent)


def _http_app_for(bind: str, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any, dict[str, Any]]:
    """``run_http(bind=...)`` up to, not including, the listen: the app it hands uvicorn.

    ``uvicorn.run`` is replaced so nothing binds a port, and ``build_server`` is
    wrapped so the ``MCPServer`` is reachable: its session manager has to be
    running for the app to answer a real request, which uvicorn does through
    the Starlette lifespan and ``_post_status`` does by hand. Returns the ASGI
    app (``_BearerGuard`` and all), the server, and uvicorn's keyword arguments.
    """
    import uvicorn

    captured: dict[str, Any] = {}
    real_build = mcp_server.build_server

    def build_and_remember() -> Any:
        server = real_build()
        captured["server"] = server
        return server

    def do_not_listen(app: Any, **kwargs: Any) -> None:
        captured["app"] = app
        captured["uvicorn"] = kwargs

    monkeypatch.setattr(mcp_server, "build_server", build_and_remember)
    monkeypatch.setattr(uvicorn, "run", do_not_listen)
    mcp_server.run_http(bind=bind, port=8747)
    return captured["app"], captured["server"], captured["uvicorn"]


_INITIALIZE = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "probe", "version": "0"},
        },
    }
).encode()


def _post_status(app: Any, server: Any, *, host: str, headers: dict[str, str]) -> int:
    """The status of one ``POST /mcp`` to ``app`` — the first response line, no more.

    A raw ASGI call rather than an HTTP client, so that nothing but the SDK's
    own dependencies are needed (Starlette's ``TestClient`` wants ``httpx``,
    which mcp 2 no longer pulls in). The transport answers a successful
    initialize with an SSE stream that stays open, so the call is cancelled the
    moment the status is known; a 401 or a 421 comes back whole before the
    transport is reached at all.
    """
    wire = [
        (b"host", host.encode()),
        (b"accept", b"application/json, text/event-stream"),
        (b"content-type", b"application/json"),
        *((name.lower().encode(), value.encode()) for name, value in headers.items()),
    ]
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "root_path": "",
        "query_string": b"",
        "headers": wire,
        "client": ("192.168.1.9", 50000),
        "server": (host.rsplit(":", 1)[0], 8747),
    }

    async def go() -> int:
        body_sent = False
        started = anyio.Event()
        status: list[int] = []

        async def receive() -> dict[str, Any]:
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": _INITIALIZE, "more_body": False}
            await anyio.sleep_forever()  # the client neither sends more nor hangs up
            raise AssertionError("unreachable")

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status.append(int(message["status"]))
                started.set()

        async with server.session_manager.run(), anyio.create_task_group() as tg:
            tg.start_soon(app, scope, receive, send)
            with anyio.fail_after(10):
                await started.wait()
            tg.cancel_scope.cancel()
        return status[0]

    return anyio.run(go)


@pytest.mark.parametrize(("bind", "expected"), [("0.0.0.0", 200), ("127.0.0.1", 421)])
def test_http_host_validation_follows_the_bind(
    bind: str, expected: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A LAN ``Host`` header: accepted on ``--bind 0.0.0.0``, 421 on a loopback bind.

    The one HTTP behaviour the mcp 2 port changed, pinned in both directions.
    mcp 2 decides DNS-rebinding protection from the ``host`` handed to
    ``streamable_http_app`` — a bind spelled 127.0.0.1, localhost or ::1 gets a
    loopback-only Host allowlist, anything else gets no Host/Origin check — so
    the first case is what makes a LAN client possible at all, and the second
    is what keeps a loopback bind protected. Nothing exercised ``run_http``
    before this: dropping the ``host`` argument left every test green while
    ``--bind 0.0.0.0`` reverted to answering every LAN client with 421.
    """
    app, server, uvicorn_kwargs = _http_app_for(bind, monkeypatch)
    assert uvicorn_kwargs == {"host": bind, "port": 8747, "log_level": "warning"}
    token = mcp_server.serve_token()  # the same one run_http just read
    status = _post_status(
        app, server, host="192.168.1.5:8747", headers={"Authorization": f"Bearer {token}"}
    )
    assert status == expected


def test_http_bearer_guard_answers_before_the_host_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """No token, bad Host, loopback bind: 401, not 421 — the guard is outermost.

    That ordering is what makes the bearer token the sole gate on a
    non-loopback bind acceptable: nothing in the app runs before it.
    """
    app, server, _uvicorn_kwargs = _http_app_for("127.0.0.1", monkeypatch)
    assert _post_status(app, server, host="192.168.1.5:8747", headers={}) == 401


def test_unknown_ref_keeps_nothing_matches_wording(work_dir: Path) -> None:
    team_service.activate()
    result = call_remote("task_update", {"ref": "tsk_typo", "action": "done"})
    assert error_text(result) == "error: nothing matches 'tsk_typo'"  # not a naked ref


def test_guard_maps_the_write_path_failure_types(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The #20 failure types must reach remote agents as MCP error results
    # with the error contract's wording, never as raw tool exceptions
    # (PR #26 review must-fix 2), proven end-to-end through the client.
    import sqlite3

    from aisquare.core.store import SqliteStore

    team_service.activate()
    monkeypatch.setattr(SqliteStore, "get_event", lambda self, event_id: None)
    unconfirmed = call_remote("note_add", {"text": "vanishing write"})
    text = error_text(unconfirmed)
    assert text.startswith("error:") and "was not confirmed" in text

    def wedged(self: SqliteStore, event: object) -> object:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(SqliteStore, "add_team_event", wedged)
    locked = call_remote("note_add", {"text": "wedged write"})
    assert error_text(locked).startswith("error: context store busy")

    def broken(self: SqliteStore, event: object) -> object:
        raise sqlite3.OperationalError("no such table: team_event")

    monkeypatch.setattr(SqliteStore, "add_team_event", broken)
    hard = call_remote("note_add", {"text": "broken store"})
    text = error_text(hard)
    assert text.startswith("error: context store error") and "no such table" in text


def test_team_log_by_session_me_reads_back_own_writes(work_dir: Path) -> None:
    team_service.activate()
    mcp_server.note_add("remote receipt")
    team_service.add_note("local noise")  # unattributed (cli) — the filter must drop it
    log = json.loads(mcp_server.team_log(by_session="me"))
    texts = [event["payload"]["text"] for event in log["events"]]
    assert "remote receipt" in texts and "local noise" not in texts


def test_verify_tool_round_trip_and_not_found(work_dir: Path) -> None:
    team_service.activate()
    note = mcp_server.note_add("prove the remote write")
    seq = note.rsplit("seq ", 1)[1].split(" ", 1)[0]
    verified = mcp_server.verify(seq)
    assert verified.startswith("delivered") and "prove the remote write" in verified
    missing = call_remote("verify", {"receipt": "987654"})
    text = error_text(missing)
    assert text.startswith("error: no event matches receipt")


def test_signal_tool_set_read_and_errors(work_dir: Path) -> None:
    team_service.activate()
    set_result = mcp_server.signal("fold-ready", "on")
    assert set_result.startswith("signal fold-ready: on")
    read_result = mcp_server.signal("fold-ready")
    assert "fold-ready = on" in read_result
    missing = call_remote("signal", {"name": "never-set"})
    assert error_text(missing).startswith("error: no signal named")
    invalid = call_remote("signal", {"name": "Bad Name", "value": "x"})
    assert error_text(invalid).startswith("error: signal name")


def test_the_server_identifies_itself_with_the_cli_version() -> None:
    """``serverInfo`` names this distribution and ITS version, not the SDK's.

    mcp 1.x filled an omitted version with the SDK's own package version
    ("1.29.1"), and 2.x fills it with nothing; either way a connector list
    would show a number that says nothing about aisquare. The legacy
    connection is used because ``InitializeResult.serverInfo`` is mandatory
    there, so an absent identity fails loudly rather than reading as ``None``.
    """
    from aisquare.core.version import __version__

    async def go() -> tuple[str, str] | None:
        async with Client(mcp_server.build_server(), mode="legacy") as client:
            info = client.server_info
            return None if info is None else (info.name, info.version)

    assert anyio.run(go) == ("aisquare-team", __version__)


def test_the_modern_in_process_path_reaches_the_same_handler(work_dir: Path) -> None:
    """``mode="auto"`` — 2026-07-28, no handshake, no envelopes — still hits our handler.

    A replaced protocol handler that only took effect on the handshake era
    would leave modern clients with the SDK's prefixed wording. Same call,
    same exact text, on the path ``call_remote`` deliberately does not use.
    """
    team_service.activate()

    async def go() -> CallToolResult:
        async with Client(mcp_server.build_server()) as client:  # the default mode
            return await client.call_tool("task_update", {"ref": "tsk_x", "action": "bogus"})

    result = anyio.run(go)
    assert error_text(result) == (
        "error: action must be done, review, block, reopen, release or drop"
    )


def test_a_lost_claim_keeps_the_error_wording(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one ``_guard`` mapping nothing else here provokes: ``ClaimLostError``.

    No tool can raise it today: ``next_task`` moves on to the next todo when a
    claim is lost rather than raising, and no tool calls ``claim_task``, the
    one site that does raise. So the service is made to raise it, and what is
    under test is the mapping — kept for the day a tool claims by ref.
    """
    team_service.activate()
    task, _created = team_service.add_task("contested")

    def lost(**kwargs: object) -> object:
        raise ClaimLostError(task)

    monkeypatch.setattr(team_service, "next_task", lost)
    result = call_remote("task_next", {"claim": True})
    assert error_text(result) == f"error: task {task.id} is already claimed by another session"


def test_a_crashed_tool_is_an_error_result_logged_server_side(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A bug in a tool: an ``isError`` result naming the tool, the detail in the log.

    mcp 2.1 keeps a crash's detail off the wire — the agent sees ``Error
    executing tool <name>`` and nothing else — where 1.x sent the exception's
    text in the result. That text was the only place a crash was ever visible,
    so the wording-preserving handler logs it server-side instead. Both halves
    are asserted: the wire carries the tool's name and NOT the exception, and
    the log carries the exception, with its traceback.

    The other two kinds of failure must NOT be logged as crashes: a deliberate
    ``error: …`` (every other test in this file) and a rejected argument set,
    which is the caller's mistake, reported to the caller.
    """
    team_service.activate()
    ours = "aisquare.services.mcp_server"

    def fell_over() -> object:
        raise RuntimeError("the board renderer fell over")

    monkeypatch.setattr(team_service, "board_data", fell_over)
    with caplog.at_level(logging.ERROR, logger=ours):
        crashed = call_remote("team_board")
    text = error_text(crashed)
    assert text == "Error executing tool team_board"  # the SDK's text, unprefixed by us
    assert "fell over" not in text  # ...and nothing of the exception itself
    crashes = [record for record in caplog.records if record.name == ours]
    assert len(crashes) == 1 and crashes[0].exc_info is not None, caplog.text
    assert "the board renderer fell over" in caplog.text

    caplog.clear()
    with caplog.at_level(logging.ERROR, logger=ours):
        deliberate = call_remote("task_update", {"ref": "tsk_x", "action": "bogus"})
        rejected = call_remote("task_add", {})  # `title` is required
    assert error_text(deliberate).startswith("error: action must be")
    assert error_text(rejected).startswith("Error executing tool task_add")
    assert "title" in error_text(rejected)  # the caller learns which argument
    assert not [record for record in caplog.records if record.name == ours], caplog.text
