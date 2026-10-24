"""A solo session gets no ``<aisquare-team>`` block and the one-line MCP instructions.

Solo is no role, no fleet row and nobody else on the board: no open task, no live fleet
agent, no other live session (decision seq 435 of #4). The board and protocol rode every
request of such a session for nothing. A session with a team gets exactly what it got
before, byte for byte, and a check that fails keeps today's behaviour.
"""

from __future__ import annotations

import json
import shutil
from datetime import timedelta
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
THIRD = "ffff6666-0000-0000-0000-000000000000"


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


def test_known_limitation_a_solo_sessions_own_board_tool_reads_as_a_team_until_it_goes_stale(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELIBERATE, a pinned known limitation (#45 m1; owner decision 2026-10-24, option A).
    A solo session that calls one of aisquare's board tools writes its own MCP server's row,
    ``mcp:remote:<project>``, and every live ``mcp:`` row counts as a teammate: no row tells
    a session's own server from a remote teammate's for sure. Two attempts to skip it read a
    real teammate as solo (#46's blanket skip, #47 MAJOR-1; #48's ``CLAUDE_CODE_SESSION_ID``,
    which Claude Code exports to every shell, #49 MAJOR-1). So the session reads as a team,
    and gets the board, until that row goes stale (``_STALE_AFTER``): tokens, never a
    hidden teammate. Precise detection is option B, planned in draft PR #397
    (https://github.com/AISquare-Studio/aisquare-cli/pull/397, docs/plans/mcp-own-row.md
    there). If this test fails because the session stays solo, that is option B landing:
    update it there."""
    _team_on(runner)
    assert "<aisquare-team>" not in _start(runner, SOLO, work_dir)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SOLO)  # as Claude Code starts its server
    mcp_server.team_board()
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")

    assert "<aisquare-team>" in _prompt(runner, SOLO, work_dir), "the board comes back"
    assert mcp_server.server_instructions() == mcp_server.TEAM_INSTRUCTIONS
    assert "<aisquare-team>" in _start(runner, OTHER, work_dir)

    later = team_service._now() + team_service._STALE_AFTER + timedelta(minutes=1)
    monkeypatch.setattr(team_service, "_now", lambda: later)
    assert mcp_server.server_instructions() == mcp_server.SOLO_INSTRUCTIONS, "stale: solo again"
    assert "<aisquare-team>" not in _start(runner, THIRD, work_dir)


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


def test_a_solo_session_pruned_while_alive_is_still_briefed_when_a_team_forms(
    runner: CliRunner, work_dir: Path
) -> None:
    """Final delta review, m2: ``team prune`` ends a session by idleness, so it may be alive.
    #46 took its unbriefed mark with the row, and its next turn (which revives the row)
    took the delta path for good: never the board or the protocol once a team formed. The
    mark is kept, and the board comes once."""
    _team_on(runner)
    _start(runner, SOLO, work_dir)
    report = team_service.prune_sessions(older_than_minutes=0)
    assert [pruned.id for pruned in report.pruned] == [SOLO]
    with store_session() as store:
        assert store.get_meta(team_service._unbriefed(SOLO)) is not None

    _start(runner, OTHER, work_dir)  # a team forms; SOLO was alive all along
    first = _prompt(runner, SOLO, work_dir)
    assert "<aisquare-team>" in first and "You are team session cccc3333" in first
    assert "<aisquare-team>" not in _prompt(runner, SOLO, work_dir), "once"


def test_a_solo_session_idle_overnight_and_pruned_is_still_briefed_when_a_team_forms(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The closing review's m2, its repro: #50 n7 deleted the unbriefed mark of a session
    pruned once it was silent past the claim clock (four hours). A pruned session may be
    alive at any idle age (#48 m2): a solo session idle overnight, pruned, then a team
    forms, and its next prompt got a one-line delta, never the board. The mark is kept,
    whatever the age; a crashed session's mark stays until a forget or a purge (accepted)."""
    _team_on(runner)
    _start(runner, SOLO, work_dir)
    now = team_service._now
    later = now() + timedelta(hours=5)
    monkeypatch.setattr(team_service, "_now", lambda: later)
    report = team_service.prune_sessions(older_than_minutes=0)
    monkeypatch.setattr(team_service, "_now", now)
    assert [pruned.id for pruned in report.pruned] == [SOLO]
    with store_session() as store:
        assert store.get_meta(team_service._unbriefed(SOLO)) is not None

    _start(runner, OTHER, work_dir)  # a team forms; SOLO was alive all along
    first = _prompt(runner, SOLO, work_dir)
    assert "<aisquare-team>" in first and "You are team session cccc3333" in first


def test_a_prune_keeps_the_mark_of_a_session_silent_under_the_claim_clock(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#48 m2: an ended session keeps its mark through every later prune, so it is briefed
    once a team forms."""
    from datetime import timedelta

    _team_on(runner)
    _start(runner, SOLO, work_dir)
    team_service.prune_sessions(older_than_minutes=0)
    now = team_service._now
    later = now() + team_service._CLAIM_ORPHAN_AFTER - timedelta(minutes=1)
    monkeypatch.setattr(team_service, "_now", lambda: later)
    team_service.prune_sessions(older_than_minutes=0)
    monkeypatch.setattr(team_service, "_now", now)
    with store_session() as store:
        assert store.get_meta(team_service._unbriefed(SOLO)) is not None

    _start(runner, OTHER, work_dir)
    assert "<aisquare-team>" in _prompt(runner, SOLO, work_dir)


# --- #52 (option A): #49's repros, a team member never read as solo --------------------


def test_a_serve_started_from_a_claude_code_shell_still_shows_its_clients_as_a_team(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#49 MAJOR-1: Claude Code exports ``CLAUDE_CODE_SESSION_ID`` to every shell it runs,
    so an ``aisquare serve`` started from session S's Bash tool inherits S's id. #48 named
    every client's row after S (``mcp:cc-S``) and skipped it: S beside a live Desktop
    teammate read as solo. Every row counts now."""
    _team_on(runner)
    assert "<aisquare-team>" not in _start(runner, SOLO, work_dir)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SOLO)  # inherited by the serve process
    mcp_server.note_add("checking the login page")  # a nameless Desktop client, through it
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")

    assert "<aisquare-team>" in _prompt(runner, SOLO, work_dir)
    ended = json.dumps({"cwd": str(work_dir), "session_id": SOLO, "reason": "exit"})
    assert runner.invoke(app, ["hook", "session-end"], input=ended).exit_code == 0
    assert mcp_server.server_instructions() == mcp_server.TEAM_INSTRUCTIONS
    assert "<aisquare-team>" in _start(runner, OTHER, work_dir)


def test_a_session_on_another_board_never_hides_its_server_row_on_this_one(
    runner: CliRunner, work_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#49 m1: R's hooks put its row on board B, and R mounts A's ``serve --stdio``
    (docs/orchestration.md) and posts on A. #48 asked for R's row on any board, found B's,
    and skipped R's server row on A: a hand-run session on A read solo beside R."""
    other = tmp_path / "other"
    other.mkdir()
    (other / ".git").mkdir()
    monkeypatch.chdir(other)
    _team_on(runner)
    assert (
        runner.invoke(
            app,
            ["hook", "session-start"],
            input=json.dumps({"cwd": str(other), "session_id": THIRD, "source": "startup"}),
        ).exit_code
        == 0
    )
    monkeypatch.chdir(work_dir)
    _team_on(runner)

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", THIRD)  # R's server, mounted on A
    mcp_server.note_add("from board B's session")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")

    assert mcp_server.server_instructions() == mcp_server.TEAM_INSTRUCTIONS
    assert "<aisquare-team>" in _start(runner, SOLO, work_dir)


# --- #48: a remote teammate over `aisquare serve` is a member (final review MAJOR-1) ----


@pytest.mark.parametrize(
    "client",
    [
        {"AISQUARE_SERVE_CLIENT": "desktop", "AISQUARE_SERVE_ROLE": "browser-debugger"},
        {"AISQUARE_SERVE_ROLE": "browser-debugger"},
        {},  # Claude Desktop over `serve --stdio`, no name set: `mcp:remote:<project>`
        {"CLAUDE_CODE_SESSION_ID": "eeee5555-0000-0000-0000-000000000000"},
    ],
    ids=["named client", "a role set", "nameless desktop", "a claude code session, no hooks"],
)
def test_a_remote_teammate_over_serve_makes_the_board_a_team(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch, client: dict[str, str]
) -> None:
    """The final review's MAJOR-1: #46 skipped every ``mcp:`` row, and a remote teammate's
    row is one (docs/orchestration.md, *Remote agents over MCP*). A hand-run session beside
    it read solo: no block, the SOLO line, nothing on its prompts. Every live ``mcp:`` row
    counts (option A), whoever started its server."""
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
