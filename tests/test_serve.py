"""The MCP ingress: remote clients act as attributed virtual sessions."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app

pytest.importorskip("mcp", reason="the [serve] extra is not installed")

from aisquare.services import mcp_server
from aisquare.services import team as team_service


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


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
    assert (
        mcp_server.task_update(picked["id"], "reopen")
        == "error: reopen requires a note (the feedback)"
    )
    moved = mcp_server.task_update(picked["id"], "review", note="ready to verify")
    assert moved.endswith("is now review")
    assert mcp_server.task_update(picked["id"], "bogus").startswith("error: action must be")
    log = json.loads(mcp_server.team_log())
    assert log["latest_seq"] > 0
    kinds = {event["kind"] for event in log["events"]}
    assert "team.task_claimed" in kinds and "team.task_review" in kinds


def test_server_exposes_exactly_the_seven_tools() -> None:
    server = mcp_server.build_server()
    import anyio

    tools = anyio.run(server.list_tools)
    assert {tool.name for tool in tools} == {
        "team_board",
        "task_add",
        "task_next",
        "task_update",
        "note_add",
        "team_log",
        "recall",
    }


def test_show_token_cli(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--json", "serve", "--show-token"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["url"].endswith(":8747/mcp") and payload["token"]


def test_remote_calls_never_activate_an_unopted_project(work_dir: Path) -> None:
    # No activate(): one read-only call must not flip the repo's bus on.
    result = mcp_server.team_board()
    assert result.startswith("error:") and "not active" in result
    from aisquare.core.store import store_session
    from aisquare.core.teambus import team_project

    with store_session() as store:
        assert not store.team_active(team_project(work_dir).id)


def test_remote_calls_respect_master_switch(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    team_service.activate()
    monkeypatch.setenv("AISQUARE_TEAM", "0")
    assert "disabled" in mcp_server.task_add("x")


def test_virtual_session_is_scoped_per_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aisquare.core.store import store_session
    from aisquare.core.teambus import team_project

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
    assert mcp_server.task_next(status="reviwe").startswith("error: status must be")


def test_guard_reports_ambiguous_refs_cleanly(work_dir: Path) -> None:
    team_service.activate()
    mcp_server.task_add("alpha")
    mcp_server.task_add("beta")
    result = mcp_server.task_update("tsk_", "done")
    assert result.startswith("error:") and "ambiguous" in result


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
