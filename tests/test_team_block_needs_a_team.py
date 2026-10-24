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
    # Its MCP server starts with its client, before SessionStart writes its row.
    assert mcp_server.server_instructions() == mcp_server.SOLO_INSTRUCTIONS
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


def test_a_session_joining_one_live_teammate_sees_the_team_in_both_places(
    runner: CliRunner, work_dir: Path
) -> None:
    """Review of #346: B's MCP server starts before B's SessionStart row exists, with
    teammate A already live. It counted one live session, took it for B's own, and
    gave B the solo line while B's hook rendered the team block."""
    _team_on(runner)
    _start(runner, OTHER, work_dir)  # A, live
    assert mcp_server.server_instructions() == mcp_server.TEAM_INSTRUCTIONS  # B's server
    assert "<aisquare-team>" in _start(runner, SOLO, work_dir)  # B's SessionStart
    assert mcp_server.server_instructions() == mcp_server.TEAM_INSTRUCTIONS  # and after


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
    # The MCP server asks before the session's own row is written, as it starts.
    instructions = mcp_server.server_instructions()
    after = _start(runner, SOLO, work_dir) + instructions
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


# --- #46: the owner's decision on a hand-run plain agent, and #45's m1 and n1 ----------


def _agent_row(project_id: str) -> None:
    """A live fleet agent on the board, as a spawn records one."""
    from datetime import UTC, datetime

    from aisquare.models import FleetAgent

    with store_session() as store:
        store.upsert_fleet_agent(
            FleetAgent(
                id="agt_teammate",
                project_id=project_id,
                label="coder-1",
                role="coder",
                pane_id="%9",
                cwd=Path.cwd(),
                created_at=datetime.now(tz=UTC),
            )
        )


def test_a_hand_run_plain_agent_alone_on_the_board_is_solo(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner's decision (2026-10-24): ``aisquare launch agent`` run by hand, alone on its
    board, is solo like a session with no role: no team block, the SOLO MCP line, and the
    board on its next prompt once a team forms."""
    monkeypatch.setenv("AISQUARE_ROLE", "agent")
    _team_on(runner)
    assert mcp_server.server_instructions() == mcp_server.SOLO_INSTRUCTIONS
    assert "<aisquare-team>" not in _start(runner, SOLO, work_dir)
    with store_session() as store:
        assert store.get_meta(team_service._unbriefed(SOLO)) is not None
    assert _prompt(runner, SOLO, work_dir) == ""

    monkeypatch.delenv("AISQUARE_ROLE")
    _start(runner, OTHER, work_dir)  # a teammate comes
    assert "<aisquare-team>" in _prompt(runner, SOLO, work_dir)


@pytest.mark.parametrize("team", ["a teammate", "an open task", "a live fleet agent"])
def test_a_hand_run_plain_agent_with_a_team_gets_the_board(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch, team: str
) -> None:
    _team_on(runner)
    if team == "a teammate":
        _start(runner, OTHER, work_dir)
    elif team == "an open task":
        assert runner.invoke(app, ["task", "add", "wire auth"]).exit_code == 0
    else:
        _agent_row(current_project().id)
    monkeypatch.setenv("AISQUARE_ROLE", "agent")
    assert mcp_server.server_instructions() == mcp_server.TEAM_INSTRUCTIONS
    assert "<aisquare-team>" in _start(runner, SOLO, work_dir)


def test_a_plain_agent_with_a_persona_is_a_member_in_both_places(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a persona it stays a member. And a persona alone is one to the MCP server too:
    it counted a role or a fleet row only, and gave the hooks' member the SOLO line (#45,
    n1)."""
    _team_on(runner)
    monkeypatch.setenv("AISQUARE_PERSONA", "skeptic")
    assert mcp_server.server_instructions() == mcp_server.TEAM_INSTRUCTIONS
    monkeypatch.setenv("AISQUARE_ROLE", "agent")
    assert mcp_server.server_instructions() == mcp_server.TEAM_INSTRUCTIONS
    assert "<aisquare-team>" in _start(runner, SOLO, work_dir)


def test_the_mcp_servers_own_row_is_no_teammate(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#45, m1, the reviewer's repro: a solo session calls ``team_board()``, which writes its
    own MCP server's row. Read as a live teammate, it brought the session the board on its
    next prompt, a new role-less session read a team, and the server's instructions turned
    TEAM, for ``_STALE_AFTER``. Its server is the one Claude Code started for it, which says
    so (``CLAUDE_CODE_SESSION_ID``, #48)."""
    _team_on(runner)
    assert "<aisquare-team>" not in _start(runner, SOLO, work_dir)
    monkeypatch.setenv(team_service.HOST_SESSION_ENV, SOLO)  # SOLO's own MCP server
    mcp_server.team_board()
    monkeypatch.delenv(team_service.HOST_SESSION_ENV)
    with store_session() as store:
        ids = [s.id for s in store.team_sessions(current_project().id)]
    assert any(team_service.hosting_session(i) == SOLO for i in ids), ids

    assert _prompt(runner, SOLO, work_dir) == ""
    # The session ends; the server's row stays live. The next client's server, and the next
    # session's start, alone on the board, are solo. (While SOLO is live a new server counts
    # SOLO itself: #346's known limitation, not this.)
    ended = json.dumps({"cwd": str(work_dir), "session_id": SOLO, "reason": "exit"})
    assert runner.invoke(app, ["hook", "session-end"], input=ended).exit_code == 0
    assert mcp_server.server_instructions() == mcp_server.SOLO_INSTRUCTIONS
    assert "<aisquare-team>" not in _start(runner, OTHER, work_dir)


def test_the_gate_asks_for_an_open_task_and_never_reads_the_board_s_tasks(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#45, n7: the gate loaded every task the board ever had, done and dropped included, on
    each role-less start and each unbriefed prompt. It asks one EXISTS on the open ones: a
    board whose only task is done is no team, and the task list is never read."""
    from aisquare.core.store import SqliteStore

    _team_on(runner)
    assert runner.invoke(app, ["task", "add", "shipped"]).exit_code == 0
    with store_session() as store:
        [task] = store.team_tasks(current_project().id)
        store.set_task_status(task.id, "done")

    def every_task(*args: object, **kwargs: object) -> Any:
        raise AssertionError("the gate read every task")

    monkeypatch.setattr(SqliteStore, "team_tasks", every_task)
    assert "<aisquare-team>" not in _start(runner, SOLO, work_dir)
    with store_session() as store:
        assert not team_service._has_team(store, current_project().id, SOLO)
        assert store.has_team_task_in(current_project().id, {"done"})


def test_a_pruned_solo_session_leaves_no_unbriefed_mark(runner: CliRunner, work_dir: Path) -> None:
    """#45, n6: ``team prune`` ends a crashed or ghost session, which fires no SessionEnd, so
    the mark that hook takes away outlived the row. The prune takes it too."""
    _team_on(runner)
    _start(runner, SOLO, work_dir)
    with store_session() as store:
        assert store.get_meta(team_service._unbriefed(SOLO)) is not None
    report = team_service.prune_sessions(older_than_minutes=0)
    assert [pruned.id for pruned in report.pruned] == [SOLO]
    with store_session() as store:
        assert store.get_meta(team_service._unbriefed(SOLO)) is None


# --- #48: a remote teammate over `aisquare serve` is a member (final review MAJOR-1) ----


@pytest.mark.parametrize(
    "client",
    [
        {"AISQUARE_SERVE_CLIENT": "desktop", "AISQUARE_SERVE_ROLE": "browser-debugger"},
        {"AISQUARE_SERVE_ROLE": "browser-debugger"},
        {},  # Claude Desktop over `serve --stdio`, no name set: `mcp:remote:<project>`
        {team_service.HOST_SESSION_ENV: "eeee5555-0000-0000-0000-000000000000"},
    ],
    ids=["named client", "a role set", "nameless desktop", "a claude code session, no hooks"],
)
def test_a_remote_teammate_over_serve_makes_the_board_a_team(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch, client: dict[str, str]
) -> None:
    """The final review's MAJOR-1: #46 skipped every ``mcp:`` row, and a remote teammate's
    row is one (docs/orchestration.md, *Remote agents over MCP*). A hand-run session beside
    it read solo: no block, the SOLO line, nothing on its prompts. Only a Claude Code
    session's own server row, whose session is on the board, is skipped; a hosting session
    with no row of its own (aisquare's hooks not installed) has only that row, and counts."""
    _team_on(runner)
    assert "<aisquare-team>" not in _start(runner, SOLO, work_dir)  # alone, before it

    for name, value in client.items():
        monkeypatch.setenv(name, value)
    mcp_server.note_add("checking the login page")  # the remote teammate works
    for name in client:
        monkeypatch.delenv(name)

    assert "<aisquare-team>" in _prompt(runner, SOLO, work_dir), "the board, once a team forms"
    # SOLO ends: the remote teammate's row alone is on the board, and it is a team for the
    # next client's server and the next hand-run session.
    ended = json.dumps({"cwd": str(work_dir), "session_id": SOLO, "reason": "exit"})
    assert runner.invoke(app, ["hook", "session-end"], input=ended).exit_code == 0
    assert mcp_server.server_instructions() == mcp_server.TEAM_INSTRUCTIONS
    assert "<aisquare-team>" in _start(runner, OTHER, work_dir)
