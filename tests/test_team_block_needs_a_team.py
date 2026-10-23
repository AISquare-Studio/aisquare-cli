"""A solo session gets no ``<aisquare-team>`` block and the one-line MCP instructions.

Solo is no role, no fleet row and nobody else on the board: no open task, no live fleet
agent, no other live session (decision seq 435 of #4). The board and protocol rode every
request of such a session for nothing. A session with a team gets exactly what it got
before, byte for byte, and a check that fails keeps today's behaviour.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import paths
from aisquare.core.store import store_session
from aisquare.core.workspace import current_project
from aisquare.services import mcp_server
from aisquare.services import team as team_service

pytestmark = pytest.mark.usefixtures("project_capture")

SOLO = "cccc3333-0000-0000-0000-000000000000"
OTHER = "dddd4444-0000-0000-0000-000000000000"


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    (work / ".git").mkdir()
    monkeypatch.chdir(work)
    monkeypatch.delenv("AISQUARE_ROLE", raising=False)
    monkeypatch.delenv("AISQUARE_FLEET_AGENT", raising=False)
    monkeypatch.delenv("AISQUARE_PERSONA", raising=False)
    return work


def _start(runner: CliRunner, session_id: str, work: Path) -> str:
    payload = json.dumps({"cwd": str(work), "session_id": session_id, "source": "startup"})
    result = runner.invoke(app, ["hook", "session-start"], input=payload)
    assert result.exit_code == 0, result.output
    return result.stdout


def _prompt(runner: CliRunner, session_id: str, work: Path) -> str:
    payload = json.dumps({"cwd": str(work), "session_id": session_id, "prompt": "go"})
    result = runner.invoke(app, ["hook", "user-prompt-submit"], input=payload)
    assert result.exit_code == 0, result.output
    return result.stdout


def _team_on(runner: CliRunner) -> None:
    assert runner.invoke(app, ["team", "on"]).exit_code == 0


def test_a_solo_session_gets_no_team_block_but_is_on_the_board(
    runner: CliRunner, work_dir: Path
) -> None:
    _team_on(runner)
    assert "<aisquare-team>" not in _start(runner, SOLO, work_dir)
    with store_session() as store:
        assert store.get_session(SOLO) is not None
    assert _prompt(runner, SOLO, work_dir) == ""


def test_the_second_session_has_a_team_and_the_first_is_briefed_once(
    runner: CliRunner, work_dir: Path
) -> None:
    _team_on(runner)
    _start(runner, SOLO, work_dir)
    assert "<aisquare-team>" in _start(runner, OTHER, work_dir)
    first = _prompt(runner, SOLO, work_dir)
    assert "<aisquare-team>" in first
    assert "You are team session cccc3333" in first
    assert _prompt(runner, SOLO, work_dir) == ""


def test_an_open_task_is_a_team(runner: CliRunner, work_dir: Path) -> None:
    _team_on(runner)
    assert runner.invoke(app, ["task", "add", "wire auth"]).exit_code == 0
    assert "<aisquare-team>" in _start(runner, SOLO, work_dir)


def test_a_failing_check_keeps_todays_board(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> bool:
        raise RuntimeError("store on fire")

    monkeypatch.setattr(team_service, "_has_team", broken)
    _team_on(runner)
    assert "<aisquare-team>" in _start(runner, SOLO, work_dir)
    monkeypatch.setattr(team_service, "board_has_team", broken)
    assert mcp_server.server_instructions() == mcp_server.TEAM_INSTRUCTIONS


def _snapshot_db() -> Path:
    saved = paths.db_path().with_suffix(".saved")
    shutil.copy2(paths.db_path(), saved)
    return saved


def _restore_db(saved: Path) -> None:
    for suffix in ("-wal", "-shm"):
        Path(f"{paths.db_path()}{suffix}").unlink(missing_ok=True)
    shutil.copy2(saved, paths.db_path())


@pytest.mark.parametrize("env", ["AISQUARE_ROLE", "AISQUARE_FLEET_AGENT"])
def test_a_team_member_gets_the_same_block_byte_for_byte(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch, env: str
) -> None:
    """Alone on the board, a role or a fleet row is a team member by being one: its
    block is the one the gate-free hook renders from the same store."""
    monkeypatch.setenv(env, "coder" if env == "AISQUARE_ROLE" else "agt_nosuchrow")
    _team_on(runner)
    saved = _snapshot_db()
    gated = _start(runner, SOLO, work_dir)
    _restore_db(saved)
    monkeypatch.setattr(team_service, "_solo", lambda *args, **kwargs: False)
    ungated = _start(runner, SOLO, work_dir)
    assert "<aisquare-team>" in gated
    assert gated == ungated
    monkeypatch.undo()
    monkeypatch.setenv(env, "coder")
    assert mcp_server.server_instructions() == mcp_server.TEAM_INSTRUCTIONS


def test_a_session_with_a_teammate_gets_the_same_block_byte_for_byte(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _team_on(runner)
    _start(runner, OTHER, work_dir)
    saved = _snapshot_db()
    gated = _start(runner, SOLO, work_dir)
    _restore_db(saved)
    monkeypatch.setattr(team_service, "_solo", lambda *args, **kwargs: False)
    assert "<aisquare-team>" in gated
    assert gated == _start(runner, SOLO, work_dir)


def test_serve_over_http_keeps_the_full_instructions() -> None:
    assert mcp_server.server_instructions(team=True) == mcp_server.TEAM_INSTRUCTIONS
    assert mcp_server.server_instructions(team=False) == mcp_server.SOLO_INSTRUCTIONS


# --- the acceptance figure: what a solo onboarded repo sends per request -------------


def _main_directive(snapshot_dir: Path) -> str:
    """The SessionStart directive origin/main printed for a ready pack with prompts in
    the log (``services.hooks._directive`` before this change), for the before figure."""
    pack = snapshot_dir / "pack.repomix.xml"
    return "\n".join(
        [
            "<aisquare-context>",
            "aisquare has a packed snapshot of this codebase — use it to understand the",
            "project fast and cheaply instead of grepping or listing files:",
            f"- Skeleton (structure + signatures, read this FIRST): {pack}",
            f"- Full pack (every file's contents, open on demand): {pack}",
            f"- Per-file index (char offsets + token counts): {snapshot_dir / 'index.json'}",
            "Orient from the skeleton; open the full pack only for implementation detail.",
            "Past user prompts here are captured — run `aisquare log` to see how the "
            "user tends to ask, and honour that intent.",
            "</aisquare-context>",
        ]
    )


def test_a_solo_onboarded_repo_sends_far_fewer_characters_per_request(
    runner: CliRunner,
    work_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: Any,
) -> None:
    """SessionStart output plus MCP instructions, before (today's gates off, main's
    directive) and after. The numbers go in the PR; the bound holds the cut."""
    claude_home = tmp_path / "claude-home"
    claude_home.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    (claude_home / "CLAUDE.md").write_text("# graphify\n- **graphify** - any input to a graph\n")
    for text in ("# graphify\n- **graphify** - any input to a graph", "prefer tabs"):
        runner.invoke(app, ["context", "add", text, "--user"])
    with store_session() as store:
        store.onboard_project(current_project(work_dir))
    _team_on(runner)  # what starting aisquare's MCP server does
    snapshot_dir = paths.project_data_dir(current_project(work_dir).id) / "snapshot"

    saved = _snapshot_db()
    after = _start(runner, SOLO, work_dir) + mcp_server.server_instructions()
    _restore_db(saved)
    monkeypatch.setattr(team_service, "_solo", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        "aisquare.services.hooks.without_loaded_preferences", lambda entries, _files: entries
    )
    before_start = _main_directive(snapshot_dir) + "\n\n" + _start(runner, SOLO, work_dir)
    before = before_start + mcp_server.TEAM_INSTRUCTIONS

    with capsys.disabled():
        print(f"\nsolo onboarded repo, chars per request: before {len(before)}, after {len(after)}")
    assert "<aisquare-team>" in before and "<aisquare-team>" not in after
    assert "graphify" in before and "graphify" not in after
    assert "prefer tabs" in after
    assert len(after) < len(before) / 4
