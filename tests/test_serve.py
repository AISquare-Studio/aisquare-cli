"""The MCP ingress: remote clients act as attributed virtual sessions."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app

pytest.importorskip("mcp", reason="the [serve] extra is not installed")

import anyio
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult, TextContent

from aisquare.services import mcp_server
from aisquare.services import team as team_service
from tests import winacl


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


def call_remote(tool: str, arguments: dict[str, Any] | None = None) -> CallToolResult:
    """Call one tool end-to-end through an in-memory MCP client session."""

    async def go() -> CallToolResult:
        async with create_connected_server_and_client_session(
            mcp_server.build_server()._mcp_server
        ) as client:
            return await client.call_tool(tool, arguments or {})

    return anyio.run(go)


def error_text(result: CallToolResult) -> str:
    """The message of an MCP error result (asserting ``isError`` on the way)."""
    assert result.isError, f"expected an MCP error result, got success: {result.content!r}"
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def test_serve_token_is_created_once_with_tight_permissions(isolated_home: Path) -> None:
    first = mcp_server.serve_token()
    second = mcp_server.serve_token()
    assert first == second and len(first) > 30
    creds = isolated_home / "credentials"
    if sys.platform == "win32":
        # Mode bits are advisory on NTFS -- S_IMODE reports 0o666 however the
        # file is really protected -- so assert the ACL that was actually
        # applied. Compare SIDs, not the names icacls prints: those contain
        # spaces ("OWNER RIGHTS", "NT AUTHORITY\\SYSTEM") so they do not parse
        # on whitespace, and they are localized, so an English-only assertion
        # would fail on a German runner for no real reason.
        granted = winacl.ace_sids(creds)
        assert granted, "no ACEs read back from the credentials file"
        # The invariant is "no ORDINARY account other than me" -- not "nobody
        # else". The privileged SIDs below are the machine's own plumbing, and
        # an admin can take ownership regardless; that is the same deal POSIX
        # offers, where 0600 never excluded root. What must not appear is
        # Users, Everyone or Authenticated Users.
        me = winacl.current_user_sid()
        assert me in granted, granted
        assert not (granted - winacl.PRIVILEGED_SIDS - {me}), granted
    else:
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
    assert not result.isError
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
