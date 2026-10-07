"""Needs-you (SPEC §4): what in the fleet needs the human, and answering it from a phone.

Classification is tested on the pure functions with hand-built rows, sessions,
board events and transcript tails: every rule of the per-agent table in its
order, the project kinds, the board's addressing and clearing. The watcher,
``needs_agent_now`` and the routes run over fake sources (``live_needs_sources``
monkeypatched) and a fake tmux that records what was typed. Nothing reads a real
store, transcript or tmux server.
"""

from __future__ import annotations

import json
import re
import stat
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app as cli
from aisquare.core.config import AccountsSettings
from aisquare.core.paths import remote_needs_path
from aisquare.models import FleetAgent, FleetAgentStatus, ProjectInfo, TeamEvent, TeamSession
from aisquare.services import fleet as fleet_service
from aisquare.services import remote_needs
from aisquare.services.remote_needs import (
    NeedsItem,
    NeedsSources,
    QuickAnswer,
    is_needs_board_event,
    load_needs_dismissals,
    looks_like_a_question,
    needs_from_agent,
    needs_from_board,
    needs_item_id,
    needs_push_safe,
    record_needs_dismissal,
    scan_needs_you,
)
from aisquare.services.transcript import PendingTool, TranscriptTail

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
BORN = NOW - timedelta(hours=1)
PROJECT = ProjectInfo(id="prj_alpha", root=Path("/work/alpha"))

QUESTION: dict[str, Any] = {
    "questions": [
        {
            "header": "Cache",
            "question": "Which store should the cache use?",
            "multiSelect": False,
            "options": [
                {"label": "Redis", "description": "shared, needs a server"},
                {"label": "SQLite", "description": "local file"},
                {"label": "none", "description": "no cache"},
            ],
        }
    ]
}


# --- building the facts -------------------------------------------------------------------


def _row(
    label: str = "coder-1",
    *,
    role: str = "coder",
    created: datetime = BORN,
    ended: datetime | None = None,
    exit_status: int | None = None,
    task_id: str | None = None,
    spawned_by: str | None = None,
    row_id: str | None = None,
) -> FleetAgent:
    return FleetAgent(
        id=row_id or f"agt_{label}",
        project_id=PROJECT.id,
        label=label,
        role=role,
        pane_id=f"%{len(label)}",
        session_id=f"ses_{row_id or label}",
        cwd=PROJECT.root,
        created_at=created,
        ended_at=ended,
        exit_status=exit_status,
        task_id=task_id,
        spawned_by=spawned_by,
    )


def _session(
    row: FleetAgent,
    *,
    state: str = "working",
    seen: datetime = NOW - timedelta(minutes=3),
    resets: datetime | None = None,
    label: str | None = None,
    role: str | None = None,
    ended: datetime | None = None,
) -> TeamSession:
    assert row.session_id is not None
    return TeamSession(
        id=row.session_id,
        project_id=PROJECT.id,
        role=role or row.role,
        label=label,
        started_at=row.created_at,
        last_seen_at=seen,
        ended_at=ended,
        state=state,
        limit_resets_at=resets,
        transcript_path=f"/transcripts/{row.label}.jsonl",
    )


def _status(
    row: FleetAgent, state: str, session: TeamSession | None = None, detail: str | None = None
) -> FleetAgentStatus:
    return FleetAgentStatus.model_validate(
        {"agent": row, "state": state, "detail": detail, "session": session}
    )


def _event(
    seq: int,
    kind: str,
    text: str = "",
    *,
    session: TeamSession | None = None,
    to: str | None = None,
    at: datetime = NOW - timedelta(minutes=5),
) -> TeamEvent:
    return TeamEvent(
        seq=seq,
        id=f"evt_{seq}",
        project_id=PROJECT.id,
        session_id=None if session is None else session.id,
        kind=kind,
        text=text,
        to_role=to,
        created_at=at,
    )


def _tool(
    tool_use_id: str,
    name: str = "Bash",
    *,
    at: datetime = NOW - timedelta(minutes=2),
    **payload: Any,
) -> PendingTool:
    detail = next((str(v) for v in payload.values() if isinstance(v, str)), "")
    summary = f"{name}({detail})" if detail else name
    return PendingTool(tool_use_id=tool_use_id, name=name, summary=summary, input=payload, at=at)


def _tail(
    *pending: PendingTool,
    newest: str = "assistant_tool",
    at: datetime = NOW - timedelta(minutes=2),
    text: str | None = None,
    key: str = "rec-1",
) -> TranscriptTail:
    return TranscriptTail(
        pending=pending,
        newest=newest,
        newest_at=at,
        last_text=text,
        last_text_at=at if text is not None else None,
        marker_key=key,
    )


def _classify(
    status: FleetAgentStatus,
    tail: TranscriptTail | None = None,
    events: list[TeamEvent] | None = None,
    **kw: Any,
) -> list[NeedsItem]:
    return needs_from_agent(status, tail, project=PROJECT, events=events or [], now=NOW, **kw)


def _one(items: list[NeedsItem]) -> NeedsItem:
    assert len(items) == 1, items
    return items[0]


# --- one agent: the table, in its order (SPEC §4.2) ---------------------------------------


def test_rule_1_a_gone_pane_is_lost_before_anything_its_transcript_says() -> None:
    row = _row()
    item = _one(_classify(_status(row, "lost", _session(row)), _tail(_tool("toolu_1"))))
    assert (item.kind, item.agent, item.agent_id) == ("lost", "coder-1", "agt_coder-1")
    assert item.reason == "coder-1's pane is gone"
    assert item.id == needs_item_id(PROJECT.id, "lost", "agt_coder-1")
    assert item.actions == ("restart", "stop", "dismiss")


def test_rule_2_a_limited_row_is_limited_about_its_newest_limit_event() -> None:
    row = _row()
    session = _session(row, state="limited", resets=NOW + timedelta(hours=2))
    events = [
        _event(3, "limited", "an older limit", session=session, at=NOW - timedelta(hours=3)),
        _event(7, "limited", "coder-1 hit its five-hour limit · resets 2pm", session=session),
    ]
    status = _status(row, "limited", session, detail="limit resets in 2h (14:00)")
    item = _one(_classify(status, _tail(_tool("toolu_q", "AskUserQuestion", **QUESTION)), events))
    assert item.kind == "limited"
    assert item.id == needs_item_id(PROJECT.id, "limited", "7")
    assert item.reason == "coder-1 hit its usage limit · limit resets in 2h (14:00)"
    assert item.detail == {"text": "coder-1 hit its five-hour limit · resets 2pm"}
    assert item.since == events[1].created_at
    assert item.actions == ("switch", "open", "dismiss")


def test_rule_3_a_pending_question_is_a_question_while_the_row_reads_working() -> None:
    row = _row()
    tail = _tail(
        _tool("toolu_b", command="ls"),
        _tool("toolu_q", "AskUserQuestion", at=NOW - timedelta(minutes=1), **QUESTION),
    )
    item = _one(_classify(_status(row, "working", _session(row)), tail))
    assert item.kind == "question"
    assert item.id == needs_item_id(PROJECT.id, "question", "toolu_q")
    assert item.reason == "coder-1 asks you a question"
    assert item.excerpt == "Which store should the cache use? — Redis · SQLite · none"
    assert item.detail == {"questions": QUESTION["questions"]}
    assert item.answers == (
        QuickAnswer("Redis", ("1",)),
        QuickAnswer("SQLite", ("2",)),
        QuickAnswer("none", ("3",)),
        QuickAnswer("Cancel", ("Escape",)),
    )
    assert item.since == item.push_after == NOW - timedelta(minutes=1)
    assert item.actions == ("answer", "open", "dismiss")


def test_rule_4_a_pending_plan_is_a_plan() -> None:
    row = _row()
    plan = "# Cache plan\n\n1. Add SQLite\n2. Wire it in"
    item = _one(
        _classify(
            _status(row, "waiting", _session(row)),
            _tail(_tool("toolu_p", "ExitPlanMode", plan=plan)),
        )
    )
    assert item.kind == "plan" and item.reason == "coder-1 asks you to approve a plan"
    assert item.excerpt == "Cache plan"
    assert item.detail == {"plan": plan}
    assert [answer.label for answer in item.answers] == ["1", "2", "3", "Keep planning"]
    assert item.answers[-1].keys == ("Escape",)


def test_rule_5_a_pending_tool_with_attention_is_a_prompt_about_the_oldest_tool() -> None:
    row = _row()
    session = _session(row, state="attention")
    tail = _tail(
        _tool("toolu_a", command="git push --force", description="push it"),
        _tool("toolu_b", command="ls", at=NOW - timedelta(minutes=1)),
    )
    item = _one(_classify(_status(row, "attention", session), tail))
    assert item.kind == "permission"
    assert item.id == needs_item_id(PROJECT.id, "permission", "toolu_a")
    assert item.reason == "coder-1 waits for a permission answer to use Bash"
    assert item.excerpt == "Bash(git push --force)"
    assert item.detail == {
        "tool": "Bash",
        "input": {"command": "git push --force", "description": "push it"},
    }
    assert item.answers == (
        QuickAnswer("1", ("1",)),
        QuickAnswer("2", ("2",)),
        QuickAnswer("No", ("Escape",)),
    )


def test_a_pending_tool_without_attention_is_a_tool_at_work() -> None:
    row = _row()
    assert _classify(_status(row, "working", _session(row)), _tail(_tool("toolu_a"))) == []


def test_the_second_prompt_of_one_turn_is_a_new_item() -> None:
    """Main writes no second ``attention`` event, so the subject is the oldest pending tool."""
    row = _row()
    session = _session(row, state="attention")
    events = [_event(4, "attention", "Claude needs your permission to use Bash", session=session)]
    status = _status(row, "attention", session)
    first = _one(_classify(status, _tail(_tool("toolu_a")), events))
    second = _one(_classify(status, _tail(_tool("toolu_b")), events))
    assert first.kind == second.kind == "permission"
    assert first.id != second.id


def test_rule_6_a_prompt_dismissed_with_esc_is_interrupted_not_a_prompt() -> None:
    """Esc fires no Stop: the row keeps reading ``attention`` for its whole stale window."""
    row = _row()
    session = _session(row, state="attention", seen=NOW - timedelta(minutes=3))
    events = [_event(4, "attention", "Claude needs your permission to use Bash", session=session)]
    tail = _tail(
        newest="interrupted",
        at=NOW - timedelta(minutes=1),
        text="Pushing now.\n\nI was about to run the migration.",
        key="esc-1",
    )
    item = _one(_classify(_status(row, "attention", session), tail, events))
    assert item.kind == "interrupted"
    assert item.id == needs_item_id(PROJECT.id, "interrupted", "esc-1")
    assert item.reason == "coder-1 was interrupted and waits for you"
    assert item.excerpt == "I was about to run the migration."
    assert item.push_after == item.since + timedelta(minutes=10)
    assert item.actions == ("tell", "open", "dismiss")


def test_rule_6_an_interrupted_turn_is_interrupted_while_the_row_reads_working() -> None:
    row = _row()
    tail = _tail(newest="interrupted", at=NOW - timedelta(minutes=1))
    item = _one(_classify(_status(row, "working", _session(row)), tail))
    assert item.kind == "interrupted"


def test_an_interruption_from_before_the_last_hook_is_history() -> None:
    row = _row()
    session = _session(row, state="attention", seen=NOW - timedelta(minutes=1))
    tail = _tail(newest="interrupted", at=NOW - timedelta(minutes=4))
    item = _one(_classify(_status(row, "attention", session), tail))
    assert item.kind == "permission" and item.answers == ()


def test_rule_7_the_session_paused_dialog_reads_as_limited() -> None:
    row = _row()
    session = _session(row, state="attention")
    text = "Session paused — choose: continue on usage credits or switch models"
    events = [_event(9, "attention", text, session=session)]
    item = _one(_classify(_status(row, "attention", session), None, events))
    assert item.kind == "limited"
    assert item.id == needs_item_id(PROJECT.id, "limited", "attention:9")
    assert item.reason == "coder-1 hit its usage limit (Claude Code is asking what to do)"
    assert item.detail == {"text": text}
    assert item.answers == ()


def test_rule_8_attention_without_a_tool_is_a_dialog() -> None:
    """An MCP elicitation is a form: a digit would be typed into a field, so no buttons."""
    row = _row()
    seen = NOW - timedelta(minutes=2)
    session = _session(row, state="attention", seen=seen)
    events = [_event(4, "attention", "Claude Code needs your approval", session=session)]
    item = _one(_classify(_status(row, "attention", session), _tail(newest="tool_result"), events))
    assert item.kind == "permission"
    assert item.id == needs_item_id(PROJECT.id, "permission", f"attention:4:{seen.isoformat()}")
    assert item.reason == "coder-1 shows a dialog that needs you"
    assert item.detail == {"text": "Claude Code needs your approval"}
    assert item.answers == ()
    assert item.since == seen


def test_stale_attention_still_counts_as_attention() -> None:
    """Past ``_STALE_AFTER`` the row derives ``waiting`` while its dialog may still be up."""
    row = _row()
    session = _session(row, state="attention", seen=NOW - timedelta(hours=2))
    item = _one(_classify(_status(row, "waiting", session), _tail(_tool("toolu_a"))))
    assert item.kind == "permission"
    assert item.id == needs_item_id(PROJECT.id, "permission", "toolu_a")


def test_rule_9_a_turn_that_ends_on_a_question_is_asked() -> None:
    row = _row()
    text = "Two ways to do it.\n\nWhich approach?\n1. A cache table\n2. A file per key"
    tail = _tail(newest="assistant_text", text=text, key="txt-1")
    item = _one(_classify(_status(row, "waiting", _session(row)), tail))
    assert item.kind == "asked"
    assert item.id == needs_item_id(PROJECT.id, "asked", "txt-1")
    assert item.reason == "coder-1 ended its turn with a question"
    assert item.excerpt == "Which approach? 1. A cache table 2. A file per key"
    assert item.detail == {"text": text}
    assert item.actions == ("tell", "open", "dismiss")
    done = _tail(newest="assistant_text", text="Done. All tests pass.")
    assert _classify(_status(row, "waiting", _session(row)), done) == []


def test_a_question_is_asked_only_while_the_agent_waits() -> None:
    row = _row()
    tail = _tail(newest="assistant_text", text="Shall I go on?")
    assert _classify(_status(row, "working", _session(row)), tail) == []


@pytest.mark.parametrize("state", ["exited", "unknown"])
def test_an_exited_or_unknown_agent_needs_nothing_of_its_own(state: str) -> None:
    row = _row()
    session = _session(row, state="attention")
    tail = _tail(_tool("toolu_q", "AskUserQuestion", **QUESTION))
    assert _classify(_status(row, state, session), tail) == []


def test_records_older_than_the_row_are_ignored() -> None:
    """A resumed session's old pending tool or question belongs to the process before it."""
    row = _row()
    old = BORN - timedelta(minutes=1)
    attention = _session(row, state="attention", seen=NOW - timedelta(minutes=1))
    stale_tool = _tail(_tool("toolu_old", "AskUserQuestion", at=old, **QUESTION))
    item = _one(_classify(_status(row, "attention", attention), stale_tool))
    assert item.kind == "permission" and item.answers == (), "the dialog form, not the old question"
    waiting = _session(row, state="waiting", seen=old - timedelta(minutes=1))
    old_marker = _tail(newest="interrupted", at=old)
    assert _classify(_status(row, "working", waiting), old_marker) == []
    old_question = _tail(newest="assistant_text", text="Shall I?", at=old)
    assert _classify(_status(row, "waiting", waiting), old_question) == []


def test_without_a_tail_only_the_dialog_forms_remain() -> None:
    row = _row()
    attention = _session(row, state="attention")
    assert _one(_classify(_status(row, "attention", attention), None)).kind == "permission"
    assert _classify(_status(row, "working", _session(row)), None) == []
    assert _classify(_status(row, "waiting", _session(row, state="waiting")), None) == []


# --- a project, over fake sources ---------------------------------------------------------


@dataclass
class Fleet:
    """One project's facts, as fake sources hand them to the scan."""

    agents: list[FleetAgentStatus] = field(default_factory=list)
    ended: list[FleetAgent] = field(default_factory=list)
    events: list[TeamEvent] = field(default_factory=list)
    sessions: list[TeamSession] = field(default_factory=list)
    tasks: dict[str, str] = field(default_factory=dict)
    tails: dict[str, TranscriptTail] = field(default_factory=dict)
    listing_fails: bool = False
    listed: int = 0


def _sources(fleet: Fleet, *, accounts: AccountsSettings | None = None) -> NeedsSources:
    def list_agents(project: ProjectInfo) -> list[FleetAgentStatus]:
        fleet.listed += 1
        if fleet.listing_fails:
            raise fleet_service.FleetUnavailable("tmux is not installed")
        return list(fleet.agents)

    return NeedsSources(
        list_projects=lambda: [PROJECT],
        list_agents=list_agents,
        ended_agents=lambda pid, since: [
            row for row in fleet.ended if row.ended_at is not None and row.ended_at >= since
        ],
        board_events=lambda pid, limit: fleet.events[-limit:],
        board_sessions=lambda pid: list(fleet.sessions),
        task_status=lambda ref: fleet.tasks.get(ref),
        transcript_tail=lambda path: fleet.tails.get(path),
        accounts=lambda: accounts or AccountsSettings(),
    )


def _scan(
    fleet: Fleet,
    *,
    now: datetime = NOW,
    dismissed: tuple[str, ...] = (),
    first_seen: dict[str, datetime] | None = None,
) -> list[NeedsItem]:
    return scan_needs_you(_sources(fleet), now=now, dismissed=dismissed, first_seen=first_seen)


def _asking(now: datetime, tool: str = "toolu_push") -> tuple[FleetAgentStatus, TranscriptTail]:
    """coder-1 at a permission prompt for ``git push``, as of ``now``."""
    row = _row(created=now - timedelta(hours=1))
    session = _session(row, state="attention", seen=now - timedelta(seconds=30))
    tail = _tail(
        _tool(tool, command="git push", at=now - timedelta(minutes=1)),
        at=now - timedelta(minutes=1),
    )
    return _status(row, "attention", session), tail


def _crash(**kw: Any) -> Fleet:
    row = _row(ended=kw.pop("ended", NOW - timedelta(minutes=10)), **kw)
    return Fleet(ended=[row], tasks={"tsk_1": "doing", "tsk_done": "done"})


@pytest.mark.parametrize("task_id", ["tsk_1", None, "tsk_gone"])
def test_a_crash_with_work_left_and_nobody_on_it_is_reported(task_id: str | None) -> None:
    item = _one(_scan(_crash(exit_status=1, task_id=task_id)))
    assert item.kind == "crashed" and item.agent == "coder-1"
    assert item.reason == "coder-1 exited unexpectedly (exit 1)"
    assert item.detail == {"exit_status": 1, "task_id": task_id}
    assert item.since == NOW - timedelta(minutes=10)
    assert item.push_after == item.since + timedelta(seconds=30)
    assert item.actions == ("restart", "dismiss")


def _with_live_manager(fleet: Fleet) -> Fleet:
    manager = _row("manager", role="manager")
    fleet.agents.append(_status(manager, "working", _session(manager)))
    return fleet


def _restarted(fleet: Fleet) -> Fleet:
    again = _row(row_id="agt_again", created=NOW - timedelta(minutes=5))
    fleet.agents.append(_status(again, "working", _session(again)))
    return fleet


@pytest.mark.parametrize(
    "fleet",
    [
        pytest.param(_crash(exit_status=0), id="a clean /exit"),
        pytest.param(_crash(exit_status=None), id="a forced stop"),
        pytest.param(_crash(exit_status=1, task_id="tsk_done"), id="its task is closed"),
        pytest.param(_crash(exit_status=1, ended=NOW - timedelta(hours=2)), id="over an hour ago"),
        pytest.param(_with_live_manager(_crash(exit_status=1)), id="a live manager has it"),
        pytest.param(_restarted(_crash(exit_status=1)), id="restarted since"),
    ],
)
def test_what_is_not_a_crash(fleet: Fleet) -> None:
    assert [item.kind for item in _scan(fleet)] == []


def test_a_manager_that_crashed_is_reported_with_its_exit_code() -> None:
    manager = _row("manager", role="manager", ended=NOW - timedelta(minutes=5), exit_status=3)
    item = _one(_scan(Fleet(ended=[manager])))
    assert item.kind == "manager_down"
    assert (item.agent, item.agent_id) == ("manager", "agt_manager")
    assert item.reason == "the manager exited unexpectedly (exit 3)"
    assert item.detail == {"exit_status": 3, "task_id": None}
    assert item.push_after == item.since + timedelta(seconds=60)


def test_a_manager_stopped_while_its_crew_works_is_reported() -> None:
    manager = _row("manager", role="manager", ended=NOW - timedelta(minutes=5))
    one, two = _row("coder-1"), _row("coder-2")
    fleet = Fleet(ended=[manager], agents=[_status(one, "working", _session(one))])
    assert _one(_scan(fleet)).reason == "the manager stopped while 1 agent still works"
    fleet.agents.append(_status(two, "attention", _session(two, state="attention")))
    kinds = [item.kind for item in _scan(fleet)]
    assert kinds == ["permission", "manager_down"]
    down = _scan(fleet)[1]
    assert down.reason == "the manager stopped while 2 agents still work"


def test_a_stopped_manager_whose_work_is_done_is_not_down() -> None:
    manager = _row("manager", role="manager", ended=NOW - timedelta(minutes=5))
    managing = _session(manager, ended=NOW - timedelta(minutes=5))
    coder = _row()
    waiting = Fleet(ended=[manager], agents=[_status(coder, "waiting", _session(coder))])
    assert _scan(waiting) == [], "nobody works: stopping the manager was the end"
    working = [_status(coder, "working", _session(coder))]
    reported = Fleet(
        ended=[manager],
        agents=working,
        sessions=[managing],
        events=[_event(5, "result", "Shipped.", session=managing)],
    )
    assert [item.kind for item in _scan(reported)] == ["board_result"], "its last word: a result"
    clean = _row("manager", role="manager", ended=NOW - timedelta(minutes=5), exit_status=0)
    assert _scan(Fleet(ended=[clean], agents=working)) == []


def test_a_new_manager_ends_manager_down() -> None:
    old = _row("manager", role="manager", ended=NOW - timedelta(minutes=5), exit_status=3)
    new = _row("manager", role="manager", row_id="agt_new", created=NOW - timedelta(minutes=1))
    fleet = Fleet(ended=[old], agents=[_status(new, "working", _session(new))])
    assert _scan(fleet) == []


def test_tmux_not_answering_is_one_fleet_down_item_until_it_clears() -> None:
    one, two = _row("coder-1"), _row("coder-2")
    fleet = Fleet(agents=[_status(one, "unknown"), _status(two, "unknown")])
    memory: dict[str, datetime] = {}
    first = _one(_scan(fleet, first_seen=memory))
    assert first.kind == "fleet_down" and first.agent is None
    assert first.reason == "tmux is not answering for alpha"
    again = _one(_scan(fleet, now=NOW + timedelta(seconds=3), first_seen=memory))
    assert again.id == first.id and again.since == NOW
    assert again.push_after == NOW + timedelta(seconds=60)
    fleet.agents[0] = _status(one, "waiting", _session(one, state="waiting"))
    assert _scan(fleet, now=NOW + timedelta(seconds=6), first_seen=memory) == []
    assert memory == {}, "a cleared condition is forgotten"
    fleet.agents[0] = _status(one, "unknown")
    back = _one(_scan(fleet, now=NOW + timedelta(minutes=1), first_seen=memory))
    assert back.id != first.id, "it came back: a new item, a new push"


def test_one_row_tmux_answers_for_is_not_fleet_down() -> None:
    one, two = _row("coder-1"), _row("coder-2")
    fleet = Fleet(agents=[_status(one, "unknown"), _status(two, "waiting", _session(two))])
    assert _scan(fleet) == []


def test_a_lost_pane_is_dated_from_the_first_scan_that_saw_it() -> None:
    """It flashes between a restart's kill and the row's end: the push waits from then."""
    row = _row()
    fleet = Fleet(agents=[_status(row, "lost", _session(row))])
    memory: dict[str, datetime] = {}
    first = _one(_scan(fleet, first_seen=memory))
    later = _one(_scan(fleet, now=NOW + timedelta(seconds=30), first_seen=memory))
    assert later.id == first.id
    assert (later.since, later.push_after) == (NOW, NOW + timedelta(seconds=60))


def test_a_project_whose_listing_fails_still_shows_its_board() -> None:
    manager = _row("manager", role="manager")
    managing = _session(manager)
    fleet = Fleet(
        listing_fails=True,
        sessions=[managing],
        events=[_event(5, "question", "Ship on Friday?", session=managing)],
        ended=[_row("coder-1", ended=NOW - timedelta(minutes=1), exit_status=1)],
    )
    assert [item.kind for item in _scan(fleet)] == ["board_question"]


def test_a_dismissed_item_leaves_the_feed() -> None:
    row = _row()
    session = _session(row)
    fleet = Fleet(agents=[_status(row, "working", session)])
    fleet.tails[f"/transcripts/{row.label}.jsonl"] = _tail(
        _tool("toolu_q", "AskUserQuestion", **QUESTION)
    )
    (item,) = _scan(fleet)
    assert _scan(fleet, dismissed=(item.id,)) == []


def test_the_feed_is_ranked_by_kind_then_age_and_ids_hold_across_scans() -> None:
    manager = _row("manager", role="manager")
    managing = _session(manager)
    asker, older, crashed = _row("coder-1"), _row("coder-2"), _row("coder-3")
    fleet = Fleet(
        agents=[
            _status(manager, "working", managing),
            _status(asker, "waiting", _session(asker)),
            _status(older, "waiting", _session(older)),
        ],
        sessions=[managing],
        events=[_event(5, "result", "Shipped.", session=managing)],
        ended=[_row("coder-9", ended=NOW - timedelta(minutes=1), exit_status=1)],
    )
    fleet.tails["/transcripts/coder-1.jsonl"] = _tail(
        _tool("toolu_q", "AskUserQuestion", at=NOW - timedelta(minutes=1), **QUESTION)
    )
    fleet.tails["/transcripts/coder-2.jsonl"] = _tail(
        _tool("toolu_p", "AskUserQuestion", at=NOW - timedelta(minutes=4), **QUESTION)
    )
    first = _scan(fleet)
    assert [(item.kind, item.agent) for item in first] == [
        ("question", "coder-2"),
        ("question", "coder-1"),
        ("board_result", "manager"),
    ]
    assert crashed.label not in [item.agent for item in first]
    assert all(re.fullmatch(r"ny_[0-9a-f]{16}", item.id) for item in first)
    assert [item.id for item in _scan(fleet, now=NOW + timedelta(seconds=3))] == [
        item.id for item in first
    ]


# --- the board (SPEC §4.4) ----------------------------------------------------------------


MANAGER = _row("manager", role="manager")
MANAGING = _session(MANAGER)
CODER = _row("coder-1")
CODING = _session(CODER)


def _board(
    events: list[TeamEvent],
    *,
    sessions: tuple[TeamSession, ...] = (MANAGING, CODING),
    rows: tuple[FleetAgent, ...] = (MANAGER, CODER),
    manager_live: bool | None = False,
    now: datetime = NOW,
) -> list[NeedsItem]:
    return needs_from_board(
        events, list(sessions), list(rows), project=PROJECT, now=now, manager_live=manager_live
    )


def test_a_managers_question_with_no_to_is_for_the_human() -> None:
    question = _event(10, "question", "Ship it on Friday?", session=MANAGING)
    item = _one(_board([question], manager_live=True))
    assert item.kind == "board_question"
    assert (item.agent, item.agent_id) == ("manager", "agt_manager")
    assert item.reason == "manager asks on the board"
    assert item.excerpt == "Ship it on Friday?"
    assert item.detail == {"text": "Ship it on Friday?", "author": "manager", "seq": 10}
    assert item.id == needs_item_id(PROJECT.id, "board_question", "10")
    assert item.since == item.push_after == question.created_at
    assert item.actions == ("reply", "dismiss")


def test_a_coders_question_to_the_manager_is_the_managers_while_one_is_live() -> None:
    question = _event(10, "question", "Which branch?", session=CODING, to="manager")
    assert _board([question], manager_live=True) == []
    item = _one(_board([question], manager_live=False))
    assert item.reason == "coder-1 asks on the board"


@pytest.mark.parametrize("to", [None, "", "user", "Human", " owner ", "everyone"])
def test_a_question_to_the_human_or_to_nobody_is_for_the_human(to: str | None) -> None:
    question = _event(10, "question", "Which branch?", session=CODING, to=to)
    assert _one(_board([question], manager_live=True)).kind == "board_question"


def test_results_are_the_managers_or_a_coders_with_no_manager_to_read_them() -> None:
    reported = _one(_board([_event(10, "result", "Shipped.", session=MANAGING)]))
    assert (reported.kind, reported.reason) == ("board_result", "manager reports a result")
    to_manager = _event(11, "result", "Fixed the cache.", session=CODING, to="manager")
    assert _board([to_manager], manager_live=True) == []
    assert _one(_board([to_manager], manager_live=False)).reason == "coder-1 reports a result"
    assert _board([_event(12, "result", "a note to myself", session=CODING)]) == []


def test_the_humans_own_writes_are_never_items() -> None:
    assert _board([_event(10, "question", "for the agents", session=None)]) == []


def test_a_note_addressed_to_the_author_answers_its_question() -> None:
    question = _event(10, "question", "Ship it?", session=MANAGING)
    for to in ("manager", " Manager "):
        reply = _event(11, "note", "yes", session=None, to=to)
        assert _board([question, reply]) == [], to
    decided = _event(11, "decision", "ship", session=None, to="manager")
    assert _board([question, decided]) == []
    coder_asks = _event(20, "question", "Which branch?", session=CODING)
    for to, cleared in (("coder-1", True), ("coder", True), ("coder-2", False)):
        reply = _event(21, "note", "main", session=None, to=to)
        assert (_board([coder_asks, reply]) == []) is cleared, to


def test_an_unaddressed_human_note_answers_nothing() -> None:
    question = _event(10, "question", "Ship it?", session=MANAGING)
    news = _event(11, "note", "fyi: CI is red", session=None)
    assert _one(_board([question, news])).kind == "board_question"


def test_a_reply_written_before_the_question_answers_nothing() -> None:
    reply = _event(9, "note", "earlier", session=None, to="manager")
    question = _event(10, "question", "Ship it?", session=MANAGING)
    assert _one(_board([reply, question])).id == needs_item_id(PROJECT.id, "board_question", "10")


def test_the_authors_next_post_moves_on_from_its_question() -> None:
    first = _event(10, "question", "Ship it?", session=MANAGING)
    second = _event(12, "question", "Ship it Monday instead?", session=MANAGING)
    assert [item.excerpt for item in _board([first, second])] == ["Ship it Monday instead?"]
    decided = _event(11, "decision", "shipping Monday", session=MANAGING)
    assert _board([first, decided]) == []
    others = _event(11, "question", "unrelated", session=CODING, to="manager")
    assert len(_board([first, others], manager_live=True)) == 1, "another agent's post is not"


def test_a_question_older_than_a_day_needs_nobody() -> None:
    old = _event(10, "question", "Ship it?", session=MANAGING, at=NOW - timedelta(hours=25))
    assert _board([old]) == []


def test_whether_a_manager_is_live_is_read_from_its_rows_and_sessions() -> None:
    question = _event(10, "question", "Which branch?", session=CODING, to="manager")
    assert _board([question], manager_live=None) == [], "a live manager row"
    gone = _row("manager", role="manager", ended=NOW - timedelta(minutes=2), exit_status=1)
    crashed = _session(gone, seen=NOW - timedelta(minutes=2))
    assert (
        len(_board([question], sessions=(crashed, CODING), rows=(gone, CODER), manager_live=None))
        == 1
    ), "its session never saw a SessionEnd, but its row ended"
    outside = _row("manager", role="manager", row_id="outside")
    fresh = _session(outside, seen=NOW - timedelta(minutes=5))
    stale = _session(outside, seen=NOW - timedelta(minutes=40))
    assert _board([question], sessions=(fresh, CODING), rows=(CODER,), manager_live=None) == []
    assert len(_board([question], sessions=(stale, CODING), rows=(CODER,), manager_live=None)) == 1


@pytest.mark.parametrize(
    ("kind", "role", "to", "manager_live", "expected"),
    [
        ("question", "manager", None, True, True),
        ("question", "manager1", "coder-2", True, True),
        ("question", "coder", None, True, True),
        ("question", "coder", "all", True, True),
        ("question", "coder", "manager", True, False),
        ("question", "coder", "manager", False, True),
        ("question", "coder", "coder-2", False, False),
        ("question", None, None, True, True),
        ("result", "manager", None, True, True),
        ("result", "coder", None, False, False),
        ("result", "coder", "manager", False, True),
        ("note", "manager", None, False, False),
        ("decision", "manager", None, False, False),
    ],
)
def test_which_board_events_are_for_the_human(
    kind: str, role: str | None, to: str | None, manager_live: bool, expected: bool
) -> None:
    event = _event(1, kind, "…", session=CODING, to=to)
    assert is_needs_board_event(event, author_role=role, manager_live=manager_live) is expected
    human = _event(1, kind, "…", session=None, to=to)
    assert is_needs_board_event(human, author_role=role, manager_live=manager_live) is False


# --- what a lock screen may show, and what a card shows -----------------------------------


def test_a_hostile_role_reaches_a_reason_as_at_most_40_printable_characters() -> None:
    role = "evil\nrole\u202e" + "\x1b[31m" + "x" * 300
    author = _session(_row("x", row_id="nobody"), role=role)
    question = _event(10, "question", "Ship it?", session=author)
    item = _one(_board([question], sessions=(author,), rows=()))
    name = item.reason.removesuffix(" asks on the board")
    assert name != item.reason
    assert len(name) <= 40 and name.isprintable(), name
    assert name.startswith("evil role") and name.endswith("…")
    assert "\u202e" not in item.reason and "\x1b" not in item.reason
    assert item.detail["author"] == role, "the card says who, as written: it is never pushed"


@pytest.mark.parametrize(
    ("raw", "safe"),
    [
        ("coder-1", "coder-1"),
        ("a\tb\nc\r\nd", "a b c d"),
        ("\u202eevil\u200b\u2066", "evil"),
        ("line\u2028separator", "line separator"),
        ("\x1b[1;31mred\x1b[0m", "red"),
        ("\x1b]8;;https://evil.example\x07click\x1b]8;;\x07", "click"),
        ("\x1b]0;unterminated title", ""),
        ("x" * 100, "x" * 39 + "…"),
        ("   ", ""),
    ],
)
def test_needs_push_safe(raw: str, safe: str) -> None:
    assert needs_push_safe(raw) == safe


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("Bash", "coder-1 waits for a permission answer to use Bash"),
        (
            "mcp__github__create_pr",
            "coder-1 waits for a permission answer to use mcp__github__create_pr",
        ),
        ("Task", "coder-1 waits for a permission answer (in a sub-agent)"),
        ("Agent", "coder-1 waits for a permission answer (in a sub-agent)"),
        ("two words", "coder-1 waits for a permission answer"),
        ("evil\nname", "coder-1 waits for a permission answer"),
        ("x" * 41, "coder-1 waits for a permission answer"),
    ],
)
def test_a_tool_name_is_said_only_when_a_lock_screen_can_show_it(name: str, reason: str) -> None:
    row = _row()
    session = _session(row, state="attention")
    item = _one(_classify(_status(row, "attention", session), _tail(_tool("toolu_a", name))))
    assert item.reason == reason


def _size(detail: object) -> int:
    return len(json.dumps(detail, ensure_ascii=False, separators=(",", ":")).encode())


def test_every_detail_fits_its_cap() -> None:
    row = _row()
    attention = _status(row, "attention", _session(row, state="attention"))
    big = _tool(
        "toolu_a",
        "Edit",
        command="c" * 20_000,
        old_string="o" * 5_000,
        new_string="n\u00e9" * 3_000,
        content="z" * 3_000,
        extra="never shown",
        timeout=120,
        query=3,
        pattern=float("nan"),
    )
    tool = _one(_classify(attention, _tail(big))).detail
    assert _size(tool) <= 4_096
    shown = tool["input"]
    assert isinstance(shown, dict)
    assert "extra" not in shown and "timeout" not in shown, "only what the tool would do"
    assert shown["query"] == 3 and "pattern" not in shown, "a NaN is not JSON a browser reads"
    texts = [value for value in shown.values() if isinstance(value, str)]
    assert len(texts) == 4 and all(value.endswith("…") for value in texts)
    questions: dict[str, Any] = {
        "questions": [
            {
                "header": f"Q{n}",
                "question": "q" * 1_000,
                "multiSelect": False,
                "options": [{"label": f"o{m}", "description": "d" * 3_000} for m in range(4)],
            }
            for n in range(4)
        ]
    }
    asking = _status(row, "working", _session(row))
    question = _one(_classify(asking, _tail(_tool("toolu_q", "AskUserQuestion", **questions))))
    assert _size(question.detail) <= 8_192
    assert question.excerpt.endswith("(+3 more)") or len(question.excerpt) == 280
    plan = _one(_classify(asking, _tail(_tool("toolu_p", "ExitPlanMode", plan="p" * 40_000))))
    assert _size(plan.detail) <= 16_384 and str(plan.detail["plan"]).endswith("…")
    waiting = _status(row, "waiting", _session(row, state="waiting"))
    long = "word " * 4_000 + "\n\nShall I go on?"
    asked = _one(_classify(waiting, _tail(newest="assistant_text", text=long)))
    assert _size(asked.detail) <= 8_192
    assert asked.excerpt == "Shall I go on?"


def test_an_excerpt_is_one_plain_line_of_at_most_280_characters() -> None:
    row = _row()
    text = "\x1b[1mBold\x1b[0m\x00 and\tmore\n\n" + "y" * 400 + "?"
    asked = _one(
        _classify(_status(row, "waiting", _session(row)), _tail(newest="assistant_text", text=text))
    )
    assert len(asked.excerpt) == 280 and asked.excerpt.endswith("…")
    assert asked.excerpt.isprintable()


@pytest.mark.parametrize(
    ("questions", "answers"),
    [
        pytest.param(
            QUESTION["questions"], ["Redis", "SQLite", "none", "Cancel"], id="one, 3 options"
        ),
        pytest.param(
            [{**QUESTION["questions"][0], "multiSelect": True}], [], id="multi-select: the pad"
        ),
        pytest.param(
            [{**QUESTION["questions"][0], "options": [{"label": str(n)} for n in range(10)]}],
            [],
            id="10 options: no digit for the 10th",
        ),
        pytest.param(
            [{**QUESTION["questions"][0], "options": [{"label": str(n)} for n in range(9)]}],
            [*(str(n) for n in range(9)), "Cancel"],
            id="9 options",
        ),
        pytest.param(QUESTION["questions"] * 2, [], id="two questions"),
        pytest.param(
            [{**QUESTION["questions"][0], "options": [{"label": "evil\n\u202ename"}]}],
            ["evil name", "Cancel"],
            id="a label is shown clean",
        ),
    ],
)
def test_a_question_offers_digits_only_for_one_simple_choice(
    questions: list[dict[str, Any]], answers: list[str]
) -> None:
    row = _row()
    tail = _tail(_tool("toolu_q", "AskUserQuestion", questions=questions))
    item = _one(_classify(_status(row, "working", _session(row)), tail))
    assert [answer.label for answer in item.answers] == answers
    for number, answer in enumerate(item.answers[:-1], start=1):
        assert answer.keys == (str(number),)


# --- when a push may go out (C decides; D sends) ------------------------------------------


def _limited(resets: datetime | None) -> FleetAgentStatus:
    row = _row()
    return _status(row, "limited", _session(row, state="limited", resets=resets), "usage limit")


@pytest.mark.parametrize(
    ("resets", "accounts", "manager_live", "delay"),
    [
        (NOW + timedelta(minutes=10), AccountsSettings(), False, None),
        (NOW - timedelta(minutes=2), AccountsSettings(), False, None),
        (NOW + timedelta(minutes=10), AccountsSettings(wait_if_reset_within_minutes=5), False, 0),
        (NOW + timedelta(hours=3), AccountsSettings(), False, 0),
        (None, AccountsSettings(), False, 0),
        (NOW + timedelta(hours=3), AccountsSettings(on_limit="switch"), False, 90),
        (NOW + timedelta(hours=3), AccountsSettings(), True, 90),
    ],
)
def test_a_limit_pushes_never_near_its_reset_and_later_when_someone_is_on_it(
    resets: datetime | None, accounts: AccountsSettings, manager_live: bool, delay: int | None
) -> None:
    item = _one(_classify(_limited(resets), manager_live=manager_live, accounts=accounts))
    expected = None if delay is None else item.since + timedelta(seconds=delay)
    assert item.push_after == expected
    assert item.reason == "coder-1 hit its usage limit"


@pytest.mark.parametrize(
    ("role", "spawned_by", "manager_live", "minutes"),
    [
        ("coder", "ses_manager", True, 5),
        ("coder", "ses_manager", False, 0),
        ("coder", "user", True, 0),
        ("manager", None, True, 0),
    ],
)
def test_a_closing_question_waits_for_the_manager_to_answer_it_first(
    role: str, spawned_by: str | None, manager_live: bool, minutes: int
) -> None:
    row = _row(role=role, spawned_by=spawned_by)
    tail = _tail(newest="assistant_text", text="Want me to commit this?")
    item = _one(_classify(_status(row, "waiting", _session(row)), tail, manager_live=manager_live))
    assert item.push_after == item.since + timedelta(minutes=minutes)


@pytest.mark.parametrize(
    "text",
    [
        "Which approach?\n1. A table\n2. A file\n3. Neither",
        "Done with the refactor.\n\nWant me to commit this?",
        "**Should I push?**",
        '> "Is this the right file?"',
        "Shall I open the PR?\n\n- it adds the cache\n- it adds the tests",
        "All green.\n\nOne thing:\n\nship it now or wait for review?",
    ],
)
def test_text_that_asks(text: str) -> None:
    assert looks_like_a_question(text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Done.",
        "Why did it fail? The cache was cold.\n\nFixed and pushed.",
        "Is the cache warm?\n\n" + "\n".join(f"step {n} done" for n in range(13)),
        "Is the cache warm?\n\n" + "x" * 700,
    ],
)
def test_text_that_does_not(text: str) -> None:
    assert not looks_like_a_question(text)


# --- dismissals ---------------------------------------------------------------------------


def test_a_dismissal_is_written_owner_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from aisquare.core import paths

    restricted: list[int] = []
    real = paths.restrict_to_owner

    def spy(path: Path) -> bool:
        restricted.append(path.stat().st_size)
        return real(path)

    monkeypatch.setattr(paths, "restrict_to_owner", spy)
    record_needs_dismissal("ny_0123456789abcdef")
    assert set(load_needs_dismissals()) == {"ny_0123456789abcdef"}
    assert restricted == [0], "restricted while still empty, before it held anything"
    if sys.platform != "win32":  # POSIX file modes; the spy above is the NTFS half
        assert stat.S_IMODE(remote_needs_path().stat().st_mode) == 0o600


def test_old_dismissals_are_pruned_and_at_most_500_kept() -> None:
    now = datetime.now(UTC)
    seeded = {f"ny_old{n}": (now - timedelta(days=8)).isoformat() for n in range(3)}
    seeded |= {f"ny_{n:016x}": (now - timedelta(minutes=n)).isoformat() for n in range(600)}
    remote_needs_path().parent.mkdir(parents=True, exist_ok=True)
    remote_needs_path().write_text(json.dumps({"dismissed": seeded}), encoding="utf-8")
    record_needs_dismissal("ny_newest")
    kept = load_needs_dismissals()
    assert len(kept) == 500 and "ny_newest" in kept
    assert not any(key.startswith("ny_old") for key in kept)
    assert f"ny_{599:016x}" not in kept and f"ny_{0:016x}" in kept, "the oldest go first"


@pytest.mark.parametrize("body", ["not json", "[1, 2]", '{"dismissed": ["ny_1"]}'])
def test_an_unreadable_dismissals_file_dismisses_nothing(body: str) -> None:
    remote_needs_path().parent.mkdir(parents=True, exist_ok=True)
    remote_needs_path().write_text(body, encoding="utf-8")
    assert load_needs_dismissals() == {}


# --- asq remote needs ---------------------------------------------------------------------


def test_asq_remote_needs_scans_the_fleet(monkeypatch: pytest.MonkeyPatch) -> None:
    status, tail = _asking(datetime.now(UTC) - timedelta(minutes=2))
    fleet = Fleet(agents=[status])
    fleet.tails["/transcripts/coder-1.jsonl"] = tail
    monkeypatch.setattr(remote_needs, "live_needs_sources", lambda: _sources(fleet))
    as_json = CliRunner().invoke(cli, ["--json", "remote", "needs"])
    assert as_json.exit_code == 0, as_json.output
    payload = json.loads(as_json.stdout)
    assert [item["kind"] for item in payload["items"]] == ["permission"]
    assert payload["scanned_at"]
    human = CliRunner().invoke(cli, ["remote", "needs"])
    assert human.exit_code == 0, human.output
    assert human.stdout.splitlines() == [
        "⚑ permission · alpha · coder-1 — coder-1 waits for a permission answer to use Bash (3m)"
    ]
    fleet.agents.clear()
    assert CliRunner().invoke(cli, ["remote", "needs"]).stdout.strip() == "nothing needs you"
