"""The captain's audit lines never push real events out of a briefing or a board.

Review of #240, finding 11. The captain writes one ``captain_action`` line on the TARGET
board for every tool call, reads included, each carrying the tool, its arguments and the
owner's words. ``team.HUMAN_BOARD_KINDS`` kept those out of deltas and wake-ups, but only
after the read: the briefings, ``aisquare board`` and the MCP ``team_board`` read the newest
five events unfiltered, so after the owner had asked the captain about a board a few times
the next coder to start there was briefed with five audit lines and none of the decisions
and results behind them. And ``events_since`` dropped the kinds after its 31-row LIMIT, so
a run of 62 audit lines left a waiting manager's wake-up reading nothing but audit lines,
at every Stop, with the coder's result behind them.

Now each reader leaves the kinds out IN its query, before the LIMIT: the agent-facing ones
(both briefings, the MCP board, the delta, the wake-up) every human-board kind; the human
board (``aisquare board``, its watch) the audit alone, the bell and the notices being its
own. The audit is read with ``aisquare captain log``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli import watch as watch_ui
from aisquare.cli.app import app
from aisquare.core.orchestrator import team_project
from aisquare.core.store import store_session
from aisquare.models import ProjectInfo
from aisquare.services import mcp_server
from aisquare.services import team as team_service
from aisquare.services.captain import actions
from aisquare.services.captain import state as captain_state
from tests import test_manager_loop as loop_suite
from tests.test_manager_loop import (
    BLOCK,
    CODER,
    MANAGER,
    _decision,
    _fleet,
    _latest_seq,
    _session,
    _stop,
)

# The manager-loop suite's fixtures, bound here so pytest finds them for this module's tests.
work_dir = loop_suite.work_dir
no_nudge = loop_suite.no_nudge

AUDIT = "captain_action"
OWNER_ASKED = "what is coder-1 doing over there?"
NEWCOMER = "cccc3333-0000-0000-0000-000000000000"
LATECOMER = "dddd4444-0000-0000-0000-000000000000"
BELL = "Claude needs your permission to use Bash"
NOTICE = "Signed in to the second account"
RUN_OF_AUDITS = 70
"""More than two whole pages of the delta's read (31 rows each): what a busy captain leaves."""


def _the_owner_asks_the_captain(project: ProjectInfo, times: int) -> None:
    """``times`` calls of the captain's own ``board`` tool, a read: one audit line each on
    the board it reads, with the tool, its arguments and the owner's words."""
    for _ in range(times):
        answer = json.loads(actions.board(project.id, utterance=OWNER_ASKED))
        assert answer["action_seq"], answer


def _audit_lines(project: ProjectInfo, count: int) -> None:
    """``count`` audit lines written as ``actions._audit`` writes them: a ``captain_action``
    note from the captain's own session on the target board."""
    captain = captain_state.ensure_session(project)
    for n in range(count):
        record = {"v": 1, "tool": "board", "project": project.id, "utterance": f"again ({n})"}
        team_service.add_note(json.dumps(record), session_ref=captain, kind=AUDIT)


def _real_news() -> None:
    team_service.add_note("JWT it is", kind="decision", session_ref=CODER)
    team_service.add_note("the auth suite is green", kind="result", session_ref=CODER)


def _a_bell_and_a_notice(work: Path) -> None:
    """The two kinds that are the human board's own: a permission prompt's bell and a
    notification's feed line, written by the real Notification hook."""
    team_service.hook_notification(CODER, work, BELL, notification_type="permission_prompt")
    team_service.hook_notification(CODER, work, NOTICE, notification_type="auth_success")


def _a_board_the_owner_asked_the_captain_about(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work: Path
) -> ProjectInfo:
    """A manager and a coder, a decision and a result, a bell and a notice — and then the
    owner asking the captain about the board six times."""
    _fleet(runner, monkeypatch, work)
    _real_news()
    _a_bell_and_a_notice(work)
    project = team_project(work)
    _the_owner_asks_the_captain(project, 6)
    return project


# --- the store's readers ----------------------------------------------------------------


def test_the_stores_readers_leave_kinds_out_before_the_limit_and_nothing_out_by_default(
    work_dir: Path,
) -> None:
    project = team_project(work_dir)
    with store_session() as store:
        store.ensure_project(project)
        for text in ("one", "two", "three"):
            team_service._emit(store, project.id, "note", text)
        for n in range(40):
            team_service._emit(store, project.id, AUDIT, f"audit {n}")
        team_service._emit(store, project.id, "result", "four")
        newest = store.recent_events(project.id, exclude_kinds=(AUDIT,), limit=3)
        assert [event.text for event in newest] == ["two", "three", "four"]
        paged = store.events_since(project.id, 0, exclude_kinds=(AUDIT, "result"), limit=5)
        assert [event.text for event in paged] == ["one", "two", "three"]
        # The default is every kind, as it was: no other caller changes.
        assert [event.kind for event in store.recent_events(project.id, limit=3)] == [
            AUDIT,
            AUDIT,
            "result",
        ]
        unfiltered = store.events_since(project.id, 0, limit=5)
        assert [event.kind for event in unfiltered] == ["note", "note", "note", AUDIT, AUDIT]


def test_the_kinds_the_boards_leave_out_are_the_captains_and_the_humans() -> None:
    assert {actions.AUDIT_KIND} == team_service.CAPTAIN_AUDIT_KINDS
    assert team_service.CAPTAIN_AUDIT_KINDS < team_service.HUMAN_BOARD_KINDS
    assert {"attention", "notice"} < team_service.HUMAN_BOARD_KINDS


# --- the agent-facing readers -----------------------------------------------------------


def _what_hides_the_real_news(text: str) -> list[str]:
    """What is wrong with a briefing or a board's recent updates; empty when they show
    the real events alone. Each test asserts on it in its own body, so none of them is
    incapable of failing (tests/test_every_test_can_fail.py)."""
    wrong = []
    if "recent updates:" not in text:
        wrong.append("no recent updates")
    if "JWT it is" not in text or "the auth suite is green" not in text:
        wrong.append("the real events are missing")
    if AUDIT in text or OWNER_ASKED in text:
        wrong.append("the owner's audit is shown")
    if BELL in text or NOTICE in text:
        wrong.append("the human board's own lines are shown")
    return wrong


def test_a_coder_that_starts_after_the_owner_asked_the_captain_is_briefed_on_the_real_events(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work_dir: Path
) -> None:
    """The finding's repro: it was briefed with five lines of ``captain_action: {'tool':
    'board', …, 'utterance': '<the owner's words>'}`` and no decision, no result."""
    _a_board_the_owner_asked_the_captain_about(runner, monkeypatch, work_dir)
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    for source in ("startup", "clear"):
        briefing = team_service.hook_session_start(NEWCOMER, work_dir, source)
        assert _what_hides_the_real_news(briefing) == [], source


def test_a_session_that_joins_late_is_briefed_on_the_real_events(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work_dir: Path
) -> None:
    """A session the board has never seen, prompting in an active project: the same board."""
    _a_board_the_owner_asked_the_captain_about(runner, monkeypatch, work_dir)
    assert _what_hides_the_real_news(team_service.hook_prompt_heartbeat(LATECOMER, work_dir)) == []


def test_the_mcp_board_shows_a_remote_agent_the_real_events(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work_dir: Path
) -> None:
    _a_board_the_owner_asked_the_captain_about(runner, monkeypatch, work_dir)
    assert _what_hides_the_real_news(mcp_server.team_board()) == []


# --- the delta and the manager's wake-up ------------------------------------------------


def test_a_result_behind_a_run_of_audit_lines_still_wakes_the_manager(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work_dir: Path
) -> None:
    """62 audit lines or more, then a coder's result. The wake-up read 31 rows, dropped
    them all, and did so again at every Stop: it never moves the cursor when it has
    nothing to say, so the result stayed behind the run until something else was written."""
    _fleet(runner, monkeypatch, work_dir)
    project = team_project(work_dir)
    _audit_lines(project, RUN_OF_AUDITS)
    team_service.add_note("the migration ran clean", kind="result", session_ref=CODER)

    decision = _decision(_stop(runner, MANAGER, work_dir))

    assert decision["decision"] == BLOCK
    assert "the migration ran clean" in decision["reason"] and AUDIT not in decision["reason"]
    after = _session(MANAGER)
    assert after.cursor == _latest_seq(project.id), "past the whole run, at the result"
    again = _stop(runner, MANAGER, work_dir)
    assert again.stdout == "", "and the same result never wakes it twice"


def test_a_result_behind_a_run_of_audit_lines_is_in_the_very_next_delta(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work_dir: Path
) -> None:
    """Every agent's prompt reads the same page: it took three prompts to reach the result."""
    _fleet(runner, monkeypatch, work_dir)
    project = team_project(work_dir)
    _audit_lines(project, RUN_OF_AUDITS)
    team_service.add_note("the migration ran clean", kind="result", session_ref=CODER)
    delta = team_service.hook_prompt_heartbeat(MANAGER, work_dir)
    assert "the migration ran clean" in delta and "1 teammate update" in delta
    assert AUDIT not in delta


def test_a_prompt_with_only_audit_lines_ahead_moves_the_cursor_past_all_of_them(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work_dir: Path
) -> None:
    """Nothing to deliver is still something read: the cursor does not stay in front of the
    run for every later prompt and Stop to scan again — and what is written after it is
    still delivered."""
    _fleet(runner, monkeypatch, work_dir)
    project = team_project(work_dir)
    before = _session(MANAGER)
    _audit_lines(project, RUN_OF_AUDITS)
    assert team_service.hook_prompt_heartbeat(MANAGER, work_dir) == ""
    after = _session(MANAGER)
    assert after.cursor == _latest_seq(project.id) > before.cursor
    team_service.add_note("and then the deploy", kind="result", session_ref=CODER)
    assert "and then the deploy" in team_service.hook_prompt_heartbeat(MANAGER, work_dir)


# --- the human board --------------------------------------------------------------------


def test_aisquare_board_lists_the_bell_and_the_notice_and_not_the_captains_audit(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work_dir: Path
) -> None:
    """The bell and the notice are the human board's own lines and stay; the audit, which
    filled its five recent updates, is left out before they are counted."""
    _a_board_the_owner_asked_the_captain_about(runner, monkeypatch, work_dir)
    shown = runner.invoke(app, ["board"])
    assert shown.exit_code == 0, shown.output
    for line in ("JWT it is", "the auth suite is green", BELL, NOTICE):
        assert line in shown.stdout, line
    assert AUDIT not in shown.stdout and OWNER_ASKED not in shown.stdout
    as_json = json.loads(runner.invoke(app, ["--json", "board"]).stdout)
    kinds = [event["kind"] for event in as_json["events"]]
    assert kinds[-4:] == ["team.decision", "team.result", "team.attention", "team.notice"], kinds
    assert f"team.{AUDIT}" not in kinds


def test_the_watch_fallback_frame_leaves_the_audit_out_too(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work_dir: Path
) -> None:
    _a_board_the_owner_asked_the_captain_about(runner, monkeypatch, work_dir)
    frame = watch_ui.board_frame(40, 120).plain
    assert "JWT it is" in frame and BELL in frame and NOTICE in frame
    assert AUDIT not in frame and OWNER_ASKED not in frame


def test_the_watch_board_feed_leaves_the_audit_out_on_its_first_read_and_on_every_tick(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work_dir: Path
) -> None:
    """``aisquare board --watch`` with the tui extra is ``cli.ui.board.BoardPanel``: its first
    read is the newest 400, every tick after it a page past the last seq it showed."""
    pytest.importorskip("textual", reason="the [tui] extra is not installed")
    project = _a_board_the_owner_asked_the_captain_about(runner, monkeypatch, work_dir)

    async def drive() -> tuple[list[str], list[str]]:
        app_cls = watch_ui._build_app_class(interval=60.0)
        async with app_cls().run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            board = pilot.app.board
            first = [event.kind for event in board._events_by_id.values()]
            _the_owner_asks_the_captain(project, 2)
            team_service.add_note("late news", kind="note", session_ref=CODER)
            board.refresh_data()
            await pilot.pause()
            return first, [event.kind for event in board._events_by_id.values()]

    first, later = asyncio.run(drive())
    assert first[-4:] == ["decision", "result", "attention", "notice"], first
    assert later == [*first, "note"], "the tick appended the note and neither audit line"
    assert AUDIT not in later


def test_the_audit_is_still_read_with_aisquare_captain_log(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work_dir: Path
) -> None:
    """The control: left out of the boards, every line is still in the audit's own command."""
    project = _a_board_the_owner_asked_the_captain_about(runner, monkeypatch, work_dir)
    listed = runner.invoke(app, ["--json", "captain", "log", project.id])
    assert listed.exit_code == 0, listed.output
    rows = json.loads(listed.stdout)["events"]
    assert [(row["tool"], row["utterance"]) for row in rows] == [("board", OWNER_ASKED)] * 6
    with store_session() as store:
        on_the_board = store.filtered_events(project.id, kind=AUDIT, limit=50)
    assert len(on_the_board) == 7, "the six reads, and the log's own line"
