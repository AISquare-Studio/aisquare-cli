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
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app as cli
from aisquare.core.config import AccountsSettings
from aisquare.core.paths import remote_audit_path, remote_needs_path
from aisquare.core.store import store_session
from aisquare.models import (
    FleetAgent,
    FleetAgentStatus,
    ProjectInfo,
    TeamEvent,
    TeamSession,
    TeamTask,
)
from aisquare.services import fleet as fleet_service
from aisquare.services import remote_needs
from aisquare.services.remote_needs import (
    AgentNow,
    NeedsItem,
    NeedsSources,
    QuickAnswer,
    RemoteNeedsWatcher,
    is_needs_board_event,
    load_needs_dismissals,
    looks_like_a_question,
    needs_agent_now,
    needs_at_input_prompt,
    needs_dialog_open,
    needs_from_agent,
    needs_from_board,
    needs_item_current,
    needs_item_id,
    needs_push_safe,
    needs_tool_pending,
    record_needs_dismissal,
    scan_needs_you,
)
from aisquare.services.remote_server import (
    READ_ONLY_REASON,
    Runtime,
    Sources,
    build_app,
    remote_agent_lock,
)
from aisquare.services.transcript import (
    TAIL_BUDGET,
    PendingTool,
    TranscriptTail,
    read_transcript_tail,
)
from tests.remote_kit_helpers import base, make_client, make_runtime, receive_within, unlock

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


def _in_a_sub_agent(
    seen: datetime, label: str = "coder-1"
) -> tuple[FleetAgentStatus, TranscriptTail]:
    """``label`` at a prompt of a sub-agent's, notified at ``seen``: the ``Task`` it runs in is
    the one tool pending in the agent's own transcript."""
    row = _row(label)
    session = _session(row, state="attention", seen=seen)
    task = _tool("toolu_task", "Task", at=BORN + timedelta(minutes=5), description="the cache")
    return _status(row, "attention", session), _tail(task, at=BORN + timedelta(minutes=5))


def test_every_prompt_of_a_sub_agent_is_a_new_item() -> None:
    """The ``Task`` is pending through all of its sub-agent's prompts. Named after it, each
    prompt after the first was the first again: never pushed, hidden by its dismissal, and
    answered by a card left from it, whose "1" approved what the sub-agent asked next. Every
    prompt is notified, and each notification moves ``last_seen_at``."""
    first = _one(_classify(*_in_a_sub_agent(NOW - timedelta(minutes=4))))
    second = _one(_classify(*_in_a_sub_agent(NOW - timedelta(seconds=30))))
    assert first.kind == second.kind == "permission" and first.id != second.id
    assert (first.since, second.since) == (NOW - timedelta(minutes=4), NOW - timedelta(seconds=30))
    assert second.push_after == second.since, "pushed, as the first was"
    assert second.reason == "coder-1 waits for a permission answer (in a sub-agent)"


def test_a_sub_agents_next_prompt_has_no_item_until_its_notice() -> None:
    """The pane goes quiet 5 s after the next prompt is drawn and its notice comes at 6 s: in
    between, ``last_seen_at`` still names the prompt before, whose id an item would carry."""
    seen = NOW - timedelta(minutes=1)
    status, tail = _in_a_sub_agent(seen)
    drawn = NOW - timedelta(seconds=6)
    assert _classify(status, tail, pane_output=lambda: drawn) == []
    noticed = _one(
        _classify(*_in_a_sub_agent(NOW - timedelta(seconds=1)), pane_output=lambda: drawn)
    )
    assert noticed.since == NOW - timedelta(seconds=1)
    redrawn = _one(_classify(status, tail, pane_output=lambda: NOW - timedelta(seconds=40)))
    assert redrawn.since == seen, "output long after the notice is a redraw, not a prompt"
    assert _one(_classify(status, tail, pane_output=lambda: None)).since == seen


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
    events = [_event(4, "attention", "Claude Code needs your approval", session=session, at=seen)]
    tail = _tail(newest="tool_result", at=seen - timedelta(seconds=8))
    item = _one(_classify(_status(row, "attention", session), tail, events))
    assert item.kind == "permission"
    assert item.id == needs_item_id(PROJECT.id, "permission", f"attention:4:{seen.isoformat()}")
    assert item.reason == "coder-1 shows a dialog that needs you"
    assert item.detail == {"text": "Claude Code needs your approval"}
    assert item.answers == ()
    assert item.since == seen


def test_a_dialog_after_the_agent_moved_on_is_not_named_by_the_notice_before_it() -> None:
    """``mark_attention`` flips a session once per turn: a turn's later dialogs leave no event
    and move ``last_seen_at`` alone. A usage-limit dialog after a Bash prompt that was
    granted read as a permission card quoting that prompt, pushed at once and with no
    Switch; and the reverse, a later dialog read as the usage limit with its words."""
    row = _row()
    first = NOW - timedelta(minutes=10)
    session = _session(row, state="attention", seen=NOW - timedelta(minutes=1))
    status = _status(row, "attention", session)
    moved_on = _tail(newest="assistant_text", at=first + timedelta(minutes=2), text="Ran it.")
    asked = "Claude needs your permission to use Bash"
    bash = [_event(4, "attention", asked, session=session, at=first)]
    paused = "Session paused — choose: continue on usage credits or switch models"
    limit = [_event(4, "attention", paused, session=session, at=first)]
    later = _one(_classify(status, moved_on, bash))
    assert (later.kind, later.excerpt, later.detail) == ("permission", "", {"text": ""})
    assert later.reason == "coder-1 shows a dialog that needs you"
    assert _one(_classify(status, moved_on, limit)).kind == "permission", "not the limit's"
    still = _tail(newest="assistant_text", at=first - timedelta(seconds=8), text="Running it.")
    assert _one(_classify(status, still, limit)).kind == "limited", "the dialog it named"
    assert _one(_classify(status, still, bash)).excerpt == bash[0].text


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
    silent: set[str] = field(default_factory=set)
    """Sockets whose tmux server does not answer."""
    probed: list[str] = field(default_factory=list)
    """Every socket the scan asked tmux about, once per question."""
    asked_events: list[tuple[str, str, datetime]] = field(default_factory=list)
    """Every session the scan asked the store for its newest event of a kind since a time."""
    output_at: datetime | None = None
    """When every pane last printed, as tmux tells it; ``None``: it would not say."""
    windows: int = 0
    """How many times the scan read the window of the newest events."""


def _sources(fleet: Fleet, *, accounts: AccountsSettings | None = None) -> NeedsSources:
    def list_agents(project: ProjectInfo) -> list[FleetAgentStatus]:
        fleet.listed += 1
        if fleet.listing_fails:
            raise fleet_service.FleetUnavailable("tmux is not installed")
        return list(fleet.agents)

    def board_events(pid: str, limit: int) -> list[TeamEvent]:
        fleet.windows += 1
        return fleet.events[-limit:]

    def board_since(pid: str, since: datetime) -> list[TeamEvent]:
        """The store's ``team_events_since`` over the kinds the live source asks for."""
        return [
            event
            for event in fleet.events
            if event.created_at >= since
            and (
                event.kind in (*remote_needs._NEEDS_ASKED, "agent_exited")
                or (event.session_id is None and event.kind in remote_needs._NEEDS_REPLIED)
            )
        ]

    def tmux_answers(socket: str) -> bool:
        fleet.probed.append(socket)
        return socket not in fleet.silent

    def session_event(pid: str, session_id: str, kind: str, since: datetime) -> TeamEvent | None:
        fleet.asked_events.append((session_id, kind, since))
        own = [
            event
            for event in fleet.events
            if event.session_id == session_id and event.kind == kind and event.created_at >= since
        ]
        return max(own, key=lambda event: event.seq, default=None)

    return NeedsSources(
        list_projects=lambda: [PROJECT],
        list_agents=list_agents,
        ended_agents=lambda pid, since: [
            row for row in fleet.ended if row.ended_at is not None and row.ended_at >= since
        ],
        board_events=board_events,
        board_since=board_since,
        board_sessions=lambda pid, since, ids: [
            session
            for session in fleet.sessions
            if session.last_seen_at >= since or session.id in ids
        ],
        task_status=lambda ref: fleet.tasks.get(ref),
        transcript_tail=lambda path: fleet.tails.get(path),
        accounts=lambda: accounts or AccountsSettings(),
        has_live_agents=lambda pid: any(status.agent.ended_at is None for status in fleet.agents),
        tmux_answers=tmux_answers,
        session_event=session_event,
        pane_output=lambda agent: fleet.output_at,
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


def test_a_managers_last_word_is_its_own_not_the_exit_the_fleet_announced_for_it() -> None:
    """``fleet stop`` announces the exit under the manager's own session (``agent_exited``),
    so the session's newest event was that announcement after every stop, never the result
    before it: a manager stopped once it reported read as one stopped mid-work. Its last
    word is what it posted itself, a question, a result or a decision."""
    manager = _row("manager", role="manager", ended=NOW - timedelta(minutes=5))
    managing = _session(manager, ended=NOW - timedelta(minutes=5))
    coder = _row()
    stopped = NOW - timedelta(minutes=5)
    fleet = Fleet(
        ended=[manager],
        agents=[_status(coder, "working", _session(coder))],
        sessions=[managing],
        events=[
            _event(5, "result", "Shipped.", session=managing, at=stopped - timedelta(minutes=1)),
            _event(6, "note", "Signing off.", session=managing, at=stopped - timedelta(minutes=1)),
            _event(7, "agent_exited", "manager exited (?)", session=managing, at=stopped),
        ],
    )
    assert [item.kind for item in _scan(fleet)] == ["board_result"], "it reported, then stopped"
    asked = _event(6, "question", "Anything else?", session=managing, at=stopped)
    fleet.events[1] = asked
    assert [item.kind for item in _scan(fleet)] == ["board_question", "manager_down"]


def test_a_manager_whose_hand_over_never_started_its_replacement_is_down() -> None:
    """A switch or a restart stops the manager with its own ``/exit``, status 0, and then
    starts the replacement. When that start fails nothing replaces it, and "manager exited
    (0)" read as a manager whose job was done: no card while the crew worked on unmanaged.
    The exit announced for a hand-over that failed says so."""
    from aisquare.services.fleet import HANDOVER_FAILED

    manager = _row("manager", role="manager", ended=NOW - timedelta(minutes=5), exit_status=0)
    managing = _session(manager, ended=NOW - timedelta(minutes=5))
    coder = _row()
    failed = _event(7, "agent_exited", f"manager exited (0): {HANDOVER_FAILED}", session=managing)
    fleet = Fleet(
        ended=[manager],
        agents=[_status(coder, "working", _session(coder))],
        sessions=[managing],
        events=[failed],
    )
    down = _one(_scan(fleet))
    assert (down.kind, down.reason) == (
        "manager_down",
        "the manager stopped while 1 agent still works",
    )
    assert down.detail == {"exit_status": 0, "task_id": None}
    fleet.events = [_event(7, "agent_exited", "manager exited (0)", session=managing)]
    assert _scan(fleet) == [], "a stop that meant it: its job was done"
    fleet.events = [failed]
    fleet.agents = [_status(coder, "waiting", _session(coder, state="waiting"))]
    assert _scan(fleet) == [], "and with nobody at work, nothing needs a manager"


def test_a_coder_whose_hand_over_never_started_its_replacement_is_reported() -> None:
    """A switch (by hand, or on its usage limit with ``on_limit = "switch"``) or a restart
    stops the coder with its own ``/exit``, status 0, and then starts the replacement. When
    that start failed, nothing took its place, and with no manager live nobody was told: its
    limit card went with its row, and a clean exit is no crash. The exit announced for a
    hand-over that failed says so, and it reads as a crash that a restart answers."""
    from aisquare.services.fleet import HANDOVER_FAILED

    coder = _row(ended=NOW - timedelta(minutes=10), exit_status=0, task_id="tsk_1")
    coding = _session(coder, ended=NOW - timedelta(minutes=10))
    failed = _event(7, "agent_exited", f"coder-1 exited (0): {HANDOVER_FAILED}", session=coding)
    fleet = Fleet(ended=[coder], events=[failed], tasks={"tsk_1": "todo"})
    item = _one(_scan(fleet))
    assert (item.kind, item.agent) == ("crashed", "coder-1")
    assert item.reason == "coder-1 stopped, and its replacement did not start"
    assert item.detail == {"exit_status": 0, "task_id": "tsk_1"}
    assert item.actions == ("restart", "dismiss")
    fleet.events = [_event(7, "agent_exited", "coder-1 exited (0)", session=coding)]
    assert _scan(fleet) == [], "an /exit that meant it"
    fleet.events = [failed]
    assert _scan(_with_live_manager(fleet)) == [], "a live manager was nudged on it"


def test_a_new_manager_ends_manager_down() -> None:
    old = _row("manager", role="manager", ended=NOW - timedelta(minutes=5), exit_status=3)
    new = _row("manager", role="manager", row_id="agt_new", created=NOW - timedelta(minutes=1))
    fleet = Fleet(ended=[old], agents=[_status(new, "working", _session(new))])
    assert _scan(fleet) == []


def _crew_and_manager(manager: FleetAgentStatus) -> Fleet:
    """coder-1 asks the manager on the board, coder-2 crashed with its task open."""
    asker = _row("coder-1")
    asking = _session(asker)
    crashed = _row("coder-2", ended=NOW - timedelta(minutes=10), exit_status=1, task_id="tsk_1")
    return Fleet(
        agents=[manager, _status(asker, "working", asking)],
        ended=[crashed],
        sessions=[asking] + ([manager.session] if manager.session is not None else []),
        events=[_event(5, "question", "Which branch?", session=asking, to="manager")],
        tasks={"tsk_1": "doing"},
    )


def test_a_manager_parked_on_its_usage_limit_is_no_manager_to_leave_work_to() -> None:
    """Parked, a manager fires no hook and takes no nudge until its reset, hours away maybe.
    Counted live, it hid a coder's crash and a coder's question to it for the whole limit,
    while nobody acted on either; and its own limit's push waited 90 s, for itself."""
    manager = _row("manager", role="manager")
    parked = _session(manager, state="limited", resets=NOW + timedelta(hours=4))
    fleet = _crew_and_manager(_status(manager, "limited", parked, "limit resets in 4h"))
    items = _scan(fleet)
    assert [(item.kind, item.agent) for item in items] == [
        ("board_question", "coder-1"),
        ("crashed", "coder-2"),
        ("limited", "manager"),
    ]
    assert items[-1].push_after == items[-1].since, "nobody else is on the manager's own limit"
    working = _crew_and_manager(_status(manager, "working", _session(manager)))
    assert _scan(working) == [], "a manager at work has both"


def test_a_manager_tmux_cannot_reach_is_no_manager_to_leave_work_to() -> None:
    manager = _row("manager", role="manager").model_copy(update={"tmux_socket": "elsewhere"})
    fleet = _crew_and_manager(_status(manager, "unknown"))
    assert [item.kind for item in _scan(fleet)] == ["board_question", "crashed"]


def test_a_manager_session_parked_on_its_limit_is_not_live_without_its_row_either() -> None:
    """The session counts for a manager started outside the fleet, which has no row; a
    session whose row the scan read is that row's to decide, whatever the session says."""
    question = _event(10, "question", "Which branch?", session=CODING, to="manager")
    parked = _session(MANAGER, state="limited", seen=NOW - timedelta(minutes=2))
    assert len(_board([question], sessions=(parked, CODING), manager_live=None)) == 1
    outside = _row("manager", role="manager", row_id="outside")
    waiting = _session(outside, state="limited", seen=NOW - timedelta(minutes=2))
    rows = (CODER,)
    assert len(_board([question], sessions=(waiting, CODING), rows=rows, manager_live=None)) == 1
    fleet = _crew_and_manager(_status(_row("manager", role="manager"), "lost"))
    fleet.sessions.append(_session(_row("manager", role="manager")))
    assert "crashed" in [item.kind for item in _scan(fleet)], "its row says it is gone"


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
    fleet.agents = [_status(row, "waiting", _session(row, state="waiting")) for row in (one, two)]
    assert _scan(fleet, now=NOW + timedelta(seconds=6), first_seen=memory) == []
    assert memory == {}, "a cleared condition is forgotten"
    fleet.agents[0] = _status(one, "unknown")
    back = _one(_scan(fleet, now=NOW + timedelta(minutes=1), first_seen=memory))
    assert back.id != first.id, "it came back: a new item, a new push"


def test_one_row_tmux_answers_for_is_not_fleet_down() -> None:
    """Rows on two servers: the one that answered keeps its agent's own items."""
    one, two = _row("coder-1"), _row("coder-2").model_copy(update={"tmux_socket": "other"})
    attention = _session(two, state="attention")
    fleet = Fleet(agents=[_status(one, "unknown"), _status(two, "attention", attention)])
    assert [(item.kind, item.agent) for item in _scan(fleet)] == [("permission", "coder-2")]
    assert fleet.probed == ["other"], "the unknown row said its own server did not answer"


def _on_a_dead_tmux() -> Fleet:
    """Three rows the real ``fleet._derive`` reads from the board with tmux not asked at all
    (``observed=False``): two sessions seen seconds ago, and one parked on a limit."""
    rows = [_row("coder-1"), _row("coder-2"), _row("coder-3")]
    sessions = [
        _session(rows[0], state="attention", seen=NOW - timedelta(seconds=30)),
        _session(rows[1], state="working", seen=NOW - timedelta(seconds=30)),
        _session(rows[2], state="limited", resets=NOW + timedelta(hours=3)),
    ]
    return Fleet(
        agents=[
            fleet_service._status(row, session, None, None, NOW)
            for row, session in zip(rows, sessions, strict=True)
        ]
    )


def test_a_tmux_that_is_gone_is_fleet_down_at_once_not_when_its_rows_go_stale() -> None:
    """``fleet._derive`` takes a fresh board row over a silent tmux, for 30 minutes, and a
    parked one until its reset: every live row read ``unknown`` only then, so tmux down was
    reported half an hour late (hours, with a row on a limit), under cards for agents tmux
    took with it. The server is asked once the rows cannot tell."""
    fleet = _on_a_dead_tmux()
    assert [status.state for status in fleet.agents] == ["attention", "working", "limited"]
    fleet.silent.add("asq")
    (down,) = _scan(fleet)
    assert (down.kind, down.since, down.push_after) == (
        "fleet_down",
        NOW,
        NOW + timedelta(minutes=1),
    )
    assert fleet.probed == ["asq"]
    fleet.silent.clear()
    assert [item.kind for item in _scan(fleet)] == ["permission", "limited"], "it answers again"


def test_a_scan_asks_each_tmux_server_once_whatever_the_projects() -> None:
    fleet = _on_a_dead_tmux()
    other = ProjectInfo(id="prj_beta", root=Path("/work/beta"))
    sources = replace(_sources(fleet), list_projects=lambda: [PROJECT, other])
    scan_needs_you(sources, now=NOW, dismissed=())
    assert fleet.probed == ["asq"]


def test_a_listing_that_fails_does_not_forget_when_tmux_went_down() -> None:
    """``fleet_down``'s id is its first sighting. A scan whose listing failed dropped that
    date, and the next one minted a new item: pushed again, the dismissal of the first lost."""
    fleet = Fleet(agents=[_status(_row("coder-1"), "unknown")])
    memory: dict[str, datetime] = {}
    first = _one(_scan(fleet, first_seen=memory))
    fleet.listing_fails = True
    assert _scan(fleet, now=NOW + timedelta(seconds=3), first_seen=memory) == []
    fleet.listing_fails = False
    again = _one(_scan(fleet, now=NOW + timedelta(seconds=6), first_seen=memory))
    assert (again.id, again.since) == (first.id, NOW)


def _parked_and_asking() -> Fleet:
    """coder-1 parked on its limit, coder-2 at an MCP form, coder-3 at the usage-limit dialog:
    each named by an event the team writes once, when it starts."""
    rows = [_row("coder-1"), _row("coder-2"), _row("coder-3")]
    at = NOW - timedelta(minutes=30)
    parked = _session(rows[0], state="limited", resets=NOW + timedelta(hours=4))
    form = _session(rows[1], state="attention", seen=at)
    dialog = _session(rows[2], state="attention", seen=at)
    paused = "Session paused — choose: continue on usage credits or switch models"
    return Fleet(
        agents=[
            _status(rows[0], "limited", parked, "limit resets in 4h"),
            _status(rows[1], "attention", form),
            _status(rows[2], "attention", dialog),
        ],
        events=[
            _event(
                1, "limited", "coder-1 hit its limit", session=parked, at=at - timedelta(minutes=20)
            ),
            _event(2, "attention", "Claude Code needs your input", session=form, at=at),
            _event(3, "attention", paused, session=dialog, at=at),
        ],
    )


def _board_traffic(fleet: Fleet, count: int = remote_needs.NEEDS_BOARD_EVENTS) -> None:
    """``count`` newer events of nobody's: every one the scan's window can hold."""
    start = max(event.seq for event in fleet.events) + 1
    fleet.events += [
        _event(seq, "note", "fyi", at=NOW - timedelta(minutes=1))
        for seq in range(start, start + count)
    ]


def test_an_item_keeps_its_id_however_many_board_events_follow_its_own() -> None:
    """The scan reads the project's newest 300 events, and the team writes ``limited`` once a
    park and ``attention`` once a turn. While an agent stayed parked, or a dialog stayed up,
    300 events later its item became another: a new id pushed again, its dismissal lost, the
    usage-limit dialog read as a plain one. The store is asked for that one session's."""
    fleet = _parked_and_asking()
    before = _scan(fleet)
    assert [(item.kind, item.agent) for item in before] == [
        ("permission", "coder-2"),
        ("limited", "coder-1"),
        ("limited", "coder-3"),
    ]
    assert fleet.asked_events == [], "the window holds them: nothing else is read"
    _board_traffic(fleet)
    dismissed = before[1].id
    after = _scan(fleet, dismissed=(dismissed,))
    assert [(item.id, item.kind) for item in after] == [
        (before[0].id, "permission"),
        (before[2].id, "limited"),
    ], "the same items, coder-1's still dismissed"
    assert sorted(fleet.asked_events) == [
        ("ses_coder-1", "limited", BORN),
        ("ses_coder-2", "attention", BORN),
        ("ses_coder-3", "attention", BORN),
    ], "since the row was created: no further back than its own process"


def test_an_event_older_than_its_row_names_nothing_in_the_window_or_out_of_it() -> None:
    """The store is asked for an agent's event only since its row was created, so the walk
    stops there for a session that has none. The window keeps to the same bound, or an item
    named by an older event while the window held it became another once it left."""
    row = _row()
    session = _session(row, state="attention", seen=NOW - timedelta(minutes=1))
    asked = "Claude needs your permission to use Bash"
    older = _event(1, "attention", asked, session=session, at=BORN - timedelta(minutes=5))
    fleet = Fleet(agents=[_status(row, "attention", session)], events=[older])
    (before,) = _scan(fleet)
    assert (before.kind, before.excerpt) == ("permission", ""), "the dialog form, not its words"
    _board_traffic(fleet)
    (after,) = _scan(fleet)
    assert after.id == before.id


def test_a_managers_last_word_is_read_however_long_ago_it_was() -> None:
    """A manager stopped after its ``result`` finished its job, however busy the board has
    been since: 300 newer events made its last word unknown, and it read as down. It is
    read with the board's day, and nothing is asked of the store for it."""
    manager = _row("manager", role="manager", ended=NOW - timedelta(minutes=5))
    managing = _session(manager, ended=NOW - timedelta(minutes=5))
    coder = _row()
    fleet = Fleet(
        ended=[manager],
        agents=[_status(coder, "working", _session(coder))],
        sessions=[managing],
        events=[_event(5, "result", "Shipped.", session=managing, at=NOW - timedelta(hours=2))],
    )
    _board_traffic(fleet)
    exited = _event(
        fleet.events[-1].seq + 1, "agent_exited", "manager exited (?)", session=managing
    )
    fleet.events.append(exited)
    assert "manager_down" not in [item.kind for item in _scan(fleet)]
    assert fleet.asked_events == []


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
    coder = _row("coder-2")
    fleet = Fleet(
        agents=[_status(coder, "working", _session(coder))],
        listing_fails=True,
        sessions=[managing],
        events=[_event(5, "question", "Ship on Friday?", session=managing)],
        ended=[_row("coder-1", ended=NOW - timedelta(minutes=1), exit_status=1)],
    )
    assert [item.kind for item in _scan(fleet)] == ["board_question"]
    assert fleet.listed == 1, "it has a live row, so it was listed, and the listing failed"


def test_a_project_with_no_live_row_is_not_listed() -> None:
    """``fleet.list_agents`` reads every row and session the project ever had, and the scan
    runs every few seconds over every project. With no live row there is no pane to ask
    about and no death to record: its ended rows and its board say all there is."""
    fleet = _crash(exit_status=1)
    assert [item.kind for item in _scan(fleet)] == ["crashed"]
    assert fleet.listed == 0
    coder = _row("coder-2")
    fleet.agents.append(_status(coder, "working", _session(coder)))
    _scan(fleet)
    assert fleet.listed == 1


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


def test_a_board_question_stays_open_however_busy_the_board_gets() -> None:
    """The scan read the board's items from the project's newest 300 events, and a busy fleet
    writes that many in a few hours: a question asked in the morning was gone by the
    afternoon, neither answered nor dismissed. An item is read for its whole day, and what
    answers it does so however many events came between the two."""
    asked = _event(
        5, "question", "Ship it on Friday?", session=MANAGING, at=NOW - timedelta(hours=3)
    )
    fleet = Fleet(
        agents=[_status(MANAGER, "working", MANAGING)], sessions=[MANAGING], events=[asked]
    )
    (item,) = _scan(fleet)
    _board_traffic(fleet)
    assert [found.id for found in _scan(fleet)] == [item.id], "300 events later, still open"
    later = NOW - timedelta(minutes=1)
    fleet.events.append(_event(fleet.events[-1].seq + 1, "note", "Friday.", to="manager", at=later))
    _board_traffic(fleet)
    assert _scan(fleet) == [], "answered by a reply 300 events before the scan"


def test_the_window_of_newest_events_is_read_only_for_an_agent_that_needs_it() -> None:
    """The board's items are read by time; the window is for the events of an agent at a
    dialog or parked on its limit. A scan of agents doing neither reads none of it, and one
    with several such agents reads it once."""
    coder, other = _row("coder-1"), _row("coder-2")
    fleet = Fleet(agents=[_status(coder, "working", _session(coder))])
    _scan(fleet)
    assert fleet.windows == 0
    fleet.agents = [
        _status(row, "attention", _session(row, state="attention", seen=NOW - timedelta(minutes=n)))
        for n, row in enumerate((coder, other), start=1)
    ]
    assert [item.agent for item in _scan(fleet)] == ["coder-2", "coder-1"]
    assert fleet.windows == 1


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


# --- one agent, now: the predicates actions rely on ---------------------------------------


class FakeTmux:
    """The agent's tmux server: what it says about the pane, and what was typed into it."""

    def __init__(
        self,
        *,
        reference: datetime | None = None,
        command: str = "claude",
        quiet_for: float = 60.0,
        gone: bool = False,
        started: datetime | None = None,
    ) -> None:
        self.reference = reference
        self.command = command
        self.quiet_for = quiet_for
        self.gone = gone
        self.started = started
        """When the server started; ``None`` is a start tmux does not report."""
        self.fail = False
        self.asked: list[str] = []
        """Every pane asked about, once per question."""
        self.typed: list[tuple[str, ...]] = []

    def pane_facts(self, pane_id: str) -> SimpleNamespace | None:
        self.asked.append(pane_id)
        if self.gone:
            return None
        return SimpleNamespace(dead=False, dead_status=None, current_command=self.command)

    def started_at(self) -> datetime | None:
        return self.started

    def answers(self) -> bool:
        return True

    def run(self, *args: str, stdin: bytes | None = None) -> str:
        if args[:2] == ("list-panes", "-a"):
            return ""  # the fleet listing's output times: none, so the board's state decides
        assert args[:3] == ("display-message", "-p", "-t") and args[-1] == "#{window_activity}"
        self.asked.append(args[3])
        now = self.reference.timestamp() if self.reference else time.time()
        return str(int(now - self.quiet_for))

    def send_keys(self, pane_id: str, *keys: str) -> None:
        if self.fail:
            from aisquare.core.tmux import TmuxError

            raise TmuxError("no server running on /tmp/tmux-1000/asq")
        self.typed.append(("keys", pane_id, *keys))

    def send_literal(self, pane_id: str, text: str) -> None:
        self.typed.append(("text", pane_id, text))


def _now_of(
    fleet: Fleet, tmux: FakeTmux, monkeypatch: pytest.MonkeyPatch, label: str = "coder-1"
) -> AgentNow:
    monkeypatch.setattr(remote_needs, "live_needs_sources", lambda: _sources(fleet))
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: tmux)
    return needs_agent_now(PROJECT, label, now=NOW)


def _working(tail: TranscriptTail | None, *, state: str = "working") -> Fleet:
    row = _row()
    session = _session(row, state="attention" if state == "attention" else "working")
    fleet = Fleet(agents=[_status(row, state, session)])
    if tail is not None:
        fleet.tails[f"/transcripts/{row.label}.jsonl"] = tail
    return fleet


def test_a_pending_tool_in_a_quiet_pane_is_a_dialog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Claude Code animates while a tool runs; a quiet pane with a tool pending is waiting."""
    snap = _now_of(_working(_tail(_tool("toolu_a"))), FakeTmux(reference=NOW), monkeypatch)
    assert snap.pane_is_agent and snap.pane_quiet is True
    assert snap.status is not None and snap.status.agent.label == "coder-1"
    assert needs_dialog_open(snap) and not needs_at_input_prompt(snap)
    assert snap.items == (), "no item before the notification: the dialog is open all the same"


def test_a_pending_tool_in_a_busy_pane_is_a_tool_at_work(monkeypatch: pytest.MonkeyPatch) -> None:
    tmux = FakeTmux(reference=NOW, quiet_for=1)
    snap = _now_of(_working(_tail(_tool("toolu_a"))), tmux, monkeypatch)
    assert snap.pane_quiet is False
    assert not needs_dialog_open(snap) and not needs_at_input_prompt(snap)


@pytest.mark.parametrize("seconds", [0, 1, 3, 5])
def test_a_prompt_too_new_to_see_is_still_a_pending_tool(
    seconds: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Bash prompt Claude Code drew ``seconds`` ago: the pane is not quiet yet (5 s without
    output) and its notification comes at 6 s, so needs-you reads a tool at work. The tool is
    pending all the same, and that is what a stop's ``/exit`` and Enter would answer "Yes"."""
    drawn = NOW - timedelta(seconds=seconds)
    pending = _working(_tail(_tool("toolu_a", at=drawn, command="git push --force"), at=drawn))
    snap = _now_of(pending, FakeTmux(reference=NOW, quiet_for=seconds), monkeypatch)
    assert snap.items == () and not needs_dialog_open(snap)
    assert needs_tool_pending(snap)


def test_a_tool_older_than_the_row_or_in_no_agents_pane_is_not_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resumed session's old tool belongs to the process before it, and a pane that is no
    longer the agent's (a crash mid-tool) answers nothing typed into it."""
    leftover = _tail(_tool("toolu_old", at=BORN - timedelta(minutes=1)))
    assert not needs_tool_pending(_now_of(_working(leftover), FakeTmux(reference=NOW), monkeypatch))
    crashed = FakeTmux(reference=NOW, command="zsh")
    snap = _now_of(_working(_tail(_tool("toolu_a"))), crashed, monkeypatch)
    assert not snap.pane_is_agent and not needs_tool_pending(snap)
    assert not needs_tool_pending(_now_of(_working(_tail()), FakeTmux(reference=NOW), monkeypatch))


def test_a_current_question_is_a_dialog_however_busy_the_pane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tail = _tail(_tool("toolu_q", "AskUserQuestion", **QUESTION))
    snap = _now_of(_working(tail), FakeTmux(reference=NOW, quiet_for=0), monkeypatch)
    (item,) = snap.items
    assert item.kind == "question" and needs_item_current(snap, item.id)
    assert needs_dialog_open(snap)


def test_attention_is_a_dialog_until_an_interruption_follows_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snap = _now_of(_working(None, state="attention"), FakeTmux(reference=NOW), monkeypatch)
    assert needs_dialog_open(snap)
    escaped = _tail(newest="interrupted", at=NOW - timedelta(minutes=1), text="Stopping.")
    after = _now_of(_working(escaped, state="attention"), FakeTmux(reference=NOW), monkeypatch)
    assert [item.kind for item in after.items] == ["interrupted"]
    assert not needs_dialog_open(after)
    assert needs_at_input_prompt(after), (
        "Esc fired no Stop: the row says attention, the pane a prompt"
    )


def _printed_since_the_notice(tail: TranscriptTail | None, *, printed: datetime) -> Fleet:
    """coder-1 at a dialog with no tool behind it, notified a minute ago, its row as the real
    ``fleet._derive`` reads it once the pane printed at ``printed``; ``tail`` ``None`` is a
    transcript that cannot be read."""
    row = _row()
    session = _session(row, state="attention", seen=NOW - timedelta(minutes=1))
    view = fleet_service._PaneView(False, None, "claude", printed)
    fleet = Fleet(agents=[fleet_service._status(row, session, {row.id: view}, None, NOW)])
    if tail is not None:
        fleet.tails["/transcripts/coder-1.jsonl"] = tail
    return fleet


def test_a_dialog_whose_pane_just_printed_is_still_a_dialog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``fleet._derive`` reads output after the notice as the dialog answered, for 5 s. A key
    that moves the dialog's highlight prints too: a stop, a restart or a switch in those
    seconds typed ``/exit`` and Enter into the usage-limit dialog and picked what was
    highlighted, and the dialog's card went and came back, its Switch answered stale."""
    from aisquare.services.remote_actions import action_may_answer

    before = _tail(newest="assistant_text", text="Hit the limit.", at=NOW - timedelta(minutes=2))
    still = _printed_since_the_notice(before, printed=NOW - timedelta(seconds=30))
    quiet = _now_of(still, FakeTmux(reference=NOW), monkeypatch)
    assert quiet.status is not None and quiet.status.state == "attention"
    (card,) = quiet.items
    moved = _printed_since_the_notice(before, printed=NOW - timedelta(seconds=1))
    pressed = _now_of(moved, FakeTmux(reference=NOW, quiet_for=1), monkeypatch)
    assert pressed.status is not None and pressed.status.state == "working"
    assert needs_dialog_open(pressed) and action_may_answer(pressed)
    assert needs_item_current(pressed, card.id), "the card stays, and its Switch is not stale"
    replied = _tail(newest="assistant_text", text="Switched.", at=NOW - timedelta(seconds=2))
    answered = _printed_since_the_notice(replied, printed=NOW - timedelta(seconds=1))
    gone = _now_of(answered, FakeTmux(reference=NOW, quiet_for=1), monkeypatch)
    assert not needs_dialog_open(gone) and gone.items == (), "it wrote since: answered"


def test_a_granted_tool_at_work_is_not_its_own_prompt_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A granted tool prints while it runs and writes nothing until it ends: the board's
    ``attention`` is no reason to read it as a prompt, which a stale card's "1" would answer."""
    running = _tail(_tool("toolu_a", at=NOW - timedelta(minutes=2)), at=NOW - timedelta(minutes=2))
    granted = _printed_since_the_notice(running, printed=NOW - timedelta(seconds=1))
    snap = _now_of(granted, FakeTmux(reference=NOW, quiet_for=1), monkeypatch)
    assert snap.items == () and not needs_dialog_open(snap)
    assert needs_tool_pending(snap), "a stop still refuses for it, as it always did"


def test_a_row_at_work_since_its_notice_with_no_transcript_to_read_is_no_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a transcript to read nothing tells a dialog whose pane just printed from a
    granted tool at work, which prints until the turn's Stop: the feed kept the notice's
    card for the rest of the turn. Now it shows none it cannot vouch for, while the guard
    still refuses, a refusal being its cheap mistake. An empty transcript is no such doubt:
    nothing at all was written since the notice."""
    from aisquare.services.remote_actions import action_may_answer

    unread = _printed_since_the_notice(None, printed=NOW - timedelta(seconds=1))
    snap = _now_of(unread, FakeTmux(reference=NOW, quiet_for=1), monkeypatch)
    assert snap.status is not None and snap.status.state == "working"
    assert snap.items == (), "no card for what may be a tool at work"
    assert needs_dialog_open(snap) and action_may_answer(snap), "but no Enter either"
    nothing = TranscriptTail(
        pending=(), newest="none", newest_at=None, last_text=None, last_text_at=None,
        marker_key=None,
    )  # fmt: skip
    empty = _printed_since_the_notice(nothing, printed=NOW - timedelta(seconds=1))
    snap = _now_of(empty, FakeTmux(reference=NOW, quiet_for=1), monkeypatch)
    assert [item.kind for item in snap.items] == ["permission"] and needs_dialog_open(snap)


@pytest.mark.parametrize(
    "tmux",
    [
        pytest.param(FakeTmux(reference=NOW, command="zsh"), id="a shell after an exit"),
        pytest.param(FakeTmux(reference=NOW, command="python3.13"), id="the launcher"),
        pytest.param(FakeTmux(reference=NOW, gone=True), id="no pane"),
    ],
)
def test_a_pane_that_is_not_the_agent_shows_no_dialog(
    tmux: FakeTmux, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never a dialog, even with attention and a pending tool: a crash mid-tool leaves both."""
    snap = _now_of(_working(_tail(_tool("toolu_a")), state="attention"), tmux, monkeypatch)
    assert not snap.pane_is_agent
    assert not needs_dialog_open(snap) and not needs_at_input_prompt(snap)


@pytest.mark.parametrize("state", ["lost", "exited", "unknown"])
def test_a_pane_the_listing_did_not_vouch_for_is_never_asked_about(
    state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row that outlived its tmux server reads ``lost``, and the next server, started by a
    spawn in any project, gave its pane id to another agent. That pane answers as ``claude``
    and quiet, and an Escape sent on the strength of it would interrupt the other agent's
    turn, or answer its prompt (FLEET-1, through the remote)."""
    row = _row()
    fleet = Fleet(agents=[_status(row, state, _session(row, state="attention"))])
    tmux = FakeTmux(reference=NOW)
    snap = _now_of(fleet, tmux, monkeypatch)
    assert tmux.asked == [], "not one question about the pane under the row's id"
    assert not snap.pane_is_agent and snap.pane_quiet is None
    assert not needs_dialog_open(snap) and not needs_at_input_prompt(snap)


def test_a_pane_on_a_server_younger_than_the_row_is_another_agents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh board row derives its state without tmux, so the listing vouches for nothing
    when tmux would not answer it. The server that answers next is asked when it started:
    one started after the row was written numbers its panes from ``%0`` again."""
    pending = _working(_tail(_tool("toolu_a")), state="attention")
    younger = FakeTmux(reference=NOW, started=BORN + timedelta(minutes=5))
    snap = _now_of(pending, younger, monkeypatch)
    assert not snap.pane_is_agent and snap.pane_quiet is None
    assert not needs_dialog_open(snap) and not needs_at_input_prompt(snap)
    older = FakeTmux(reference=NOW, started=BORN - timedelta(minutes=5))
    assert _now_of(pending, older, monkeypatch).pane_is_agent, "the row's own server"


def test_at_the_prompt_takes_a_quiet_pane_tmux_vouches_for(monkeypatch: pytest.MonkeyPatch) -> None:
    waiting = _working(None, state="waiting")
    assert needs_at_input_prompt(_now_of(waiting, FakeTmux(reference=NOW), monkeypatch))
    busy = FakeTmux(reference=NOW, quiet_for=0)
    assert not needs_at_input_prompt(_now_of(waiting, busy, monkeypatch))
    said = _tail(newest="assistant_text", text="Done.")
    assert needs_at_input_prompt(_now_of(_working(said), FakeTmux(reference=NOW), monkeypatch))
    mid_turn = _tail(newest="tool_result")
    assert not needs_at_input_prompt(
        _now_of(_working(mid_turn), FakeTmux(reference=NOW), monkeypatch)
    )


def test_quiet_is_unknown_when_tmux_will_not_say(monkeypatch: pytest.MonkeyPatch) -> None:
    class Silent(FakeTmux):
        def run(self, *args: str, stdin: bytes | None = None) -> str:
            from aisquare.core.tmux import TmuxError

            raise TmuxError("no server")

    snap = _now_of(_working(None, state="waiting"), Silent(reference=NOW), monkeypatch)
    assert snap.pane_quiet is None
    assert not needs_at_input_prompt(snap), "only a pane tmux says is quiet is a prompt"
    pending = _now_of(_working(_tail(_tool("toolu_a"))), Silent(reference=NOW), monkeypatch)
    assert needs_dialog_open(pending), "unknown counts as quiet for a dialog: a refusal is cheap"


def test_the_agents_items_include_its_project_kinds(monkeypatch: pytest.MonkeyPatch) -> None:
    lost = _row()
    fleet = Fleet(agents=[_status(lost, "lost", _session(lost))])
    snap = _now_of(fleet, FakeTmux(reference=NOW, gone=True), monkeypatch)
    assert [item.kind for item in snap.items] == ["lost"]
    crashed = _row("coder-2", ended=NOW - timedelta(minutes=5), exit_status=1)
    fleet = Fleet(ended=[crashed])
    snap = _now_of(fleet, FakeTmux(reference=NOW), monkeypatch, label="coder-2")
    assert snap.status is None, "its newest row ended and has no window left"
    (item,) = snap.items
    assert item.kind == "crashed" and needs_item_current(snap, item.id)
    assert not snap.pane_is_agent and not needs_dialog_open(snap)


def test_a_dismissed_prompt_is_still_the_agents(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dismissal hides the card; the dialog is still up, and an Enter would still answer it."""
    fleet = _working(_tail(_tool("toolu_a")), state="attention")
    (item,) = _scan(fleet)
    record_needs_dismissal(item.id)
    snap = _now_of(fleet, FakeTmux(reference=NOW), monkeypatch)
    assert needs_item_current(snap, item.id) and needs_dialog_open(snap)


def test_a_label_with_no_row_is_no_such_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(fleet_service.NoSuchAgent):
        _now_of(_working(None), FakeTmux(reference=NOW), monkeypatch, label="ghost")


def test_a_listing_that_fails_is_not_an_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """A snapshot without the listing would claim the pane shows no dialog."""
    fleet = _working(None)
    fleet.listing_fails = True
    with pytest.raises(fleet_service.FleetUnavailable):
        _now_of(fleet, FakeTmux(reference=NOW), monkeypatch)


# --- the live sources, over a real store --------------------------------------------------


def _asking_record(at: datetime) -> dict[str, Any]:
    """A transcript's assistant record with a pending ``AskUserQuestion``."""
    question = {"type": "tool_use", "id": "toolu_q", "name": "AskUserQuestion", "input": QUESTION}
    return {
        "type": "assistant",
        "uuid": "a1",
        "timestamp": at.isoformat(),
        "message": {"id": "m1", "role": "assistant", "content": [question]},
    }


def test_the_live_sources_scan_the_store_the_fleet_and_the_transcripts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every other test hands the scan fake sources. These are the real ones, over a seeded
    store and the fleet's own listing on a fake tmux server, so what is tested is the wiring:
    ended rows since ``RECENTLY_ENDED`` and none older, the board's events and sessions, a
    task's status, and the transcript a session names."""
    now = datetime.now(UTC)
    root = tmp_path / "alpha"
    transcript = tmp_path / "coder-1.jsonl"
    record = _asking_record(now - timedelta(minutes=2))
    transcript.write_text(json.dumps(record) + "\n", encoding="utf-8")
    hour_ago, crashed_at = now - timedelta(hours=1), now - timedelta(minutes=5)
    with store_session() as store:
        project = store.onboard_project(ProjectInfo(id="prj_alpha", root=root))
        store.upsert_session(
            TeamSession(
                id="ses_1",
                project_id=project.id,
                role="coder",
                label="coder-1",
                started_at=hour_ago,
                last_seen_at=now - timedelta(seconds=30),
                transcript_path=str(transcript),
            )
        )
        store.upsert_fleet_agent(
            FleetAgent(
                id="agt_1",
                project_id=project.id,
                label="coder-1",
                role="coder",
                pane_id="%1",
                session_id="ses_1",
                cwd=root,
                created_at=hour_ago,
            )
        )
        for label, task_id, task_status in (
            ("coder-2", "tsk_open", "doing"),
            ("coder-3", "tsk_done", "done"),
        ):
            store.upsert_task(
                TeamTask(
                    id=task_id,
                    project_id=project.id,
                    key=task_id,
                    title=f"{label}'s work",
                    status=task_status,
                    created_at=hour_ago,
                    updated_at=hour_ago,
                )
            )
            store.upsert_fleet_agent(
                FleetAgent(
                    id=f"agt_{label}",
                    project_id=project.id,
                    label=label,
                    role="coder",
                    pane_id="%9",
                    cwd=root,
                    task_id=task_id,
                    created_at=hour_ago,
                    ended_at=crashed_at,
                    exit_status=1,
                )
            )
        # Two days gone: past RECENTLY_ENDED, so no `manager_down` for it.
        store.upsert_fleet_agent(
            FleetAgent(
                id="agt_old",
                project_id=project.id,
                label="manager",
                role="manager",
                pane_id="%8",
                cwd=root,
                created_at=now - timedelta(days=3),
                ended_at=now - timedelta(days=2),
                exit_status=3,
            )
        )
        store.upsert_session(
            TeamSession(
                id="ses_m",
                project_id=project.id,
                role="manager",
                started_at=now - timedelta(hours=3),
                last_seen_at=now - timedelta(hours=2),
                state="waiting",
            )
        )
        store.add_team_event(
            TeamEvent(
                id="evt_q",
                project_id=project.id,
                session_id="ses_m",
                kind="question",
                text="Ship on Friday?",
                created_at=now - timedelta(minutes=1),
            )
        )
    tmux = FakeTmux()
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: tmux)
    items = scan_needs_you(remote_needs.live_needs_sources(), now=now, dismissed=())
    assert [(item.kind, item.agent) for item in items] == [
        ("question", "coder-1"),
        ("board_question", None),
        ("crashed", "coder-2"),
    ]
    assert "%1" in tmux.asked, "the fleet's own listing asked tmux about the live pane"


def test_the_live_sources_read_an_unchanged_transcript_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The watcher scans every few seconds: a transcript that did not change costs a
    ``stat()``, and one that grew is read again."""
    reads: list[str] = []
    real = read_transcript_tail

    def counted(path: Path | str | None, *, budget: int = TAIL_BUDGET) -> TranscriptTail | None:
        reads.append(str(path))
        return real(path, budget=budget)

    monkeypatch.setattr(remote_needs, "read_transcript_tail", counted)
    monkeypatch.setattr(remote_needs, "_tails", {})
    transcript = tmp_path / "coder-1.jsonl"
    transcript.write_text(json.dumps(_asking_record(NOW)) + "\n", encoding="utf-8")
    tail_of = remote_needs.live_needs_sources().transcript_tail
    first = tail_of(str(transcript))
    assert first is not None and [tool.tool_use_id for tool in first.pending] == ["toolu_q"]
    assert tail_of(str(transcript)) is first
    assert reads == [str(transcript)], "unchanged: read once"
    answer = {
        "type": "user",
        "uuid": "r1",
        "timestamp": NOW.isoformat(),
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_q"}]},
    }
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(answer) + "\n")
    again = tail_of(str(transcript))
    assert len(reads) == 2 and again is not None and again.pending == (), "it grew: read again"


def test_a_scan_builds_none_of_a_projects_history_it_cannot_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every spawn, restart and switch leaves a fleet row, and every Claude Code start a
    session, that is never deleted. Each scan read all of both, for every project, every 3 s,
    after ``fleet.list_agents`` had read them already (review of #243, round 3, 11/13). Now a
    project with no live row is not listed, and the scan's own reads keep to the day's
    endings, the sessions seen in the last half hour and the authors of open questions."""
    from aisquare.core import store as store_module

    now = datetime.now(UTC)
    old = now - timedelta(days=3)
    dormant = ProjectInfo(id="prj_dormant", root=tmp_path / "dormant")
    active = ProjectInfo(id="prj_active", root=tmp_path / "active")
    history = 60
    with store_session() as store:
        for project in (dormant, active):
            store.onboard_project(project)
            for n in range(history):
                sid = f"ses_{project.id}_{n}"
                store.upsert_session(
                    TeamSession(
                        id=sid, project_id=project.id, role="coder", started_at=old,
                        last_seen_at=old, ended_at=old,
                    )
                )  # fmt: skip
                store.upsert_fleet_agent(
                    FleetAgent(
                        id=f"agt_{project.id}_{n}", project_id=project.id, label=f"coder-{n}",
                        role="coder", pane_id=f"%{n}", session_id=sid, cwd=project.root,
                        created_at=old, ended_at=old, exit_status=0,
                    )
                )  # fmt: skip
        store.upsert_session(
            TeamSession(
                id="ses_live", project_id=active.id, role="coder", label="coder-live",
                started_at=now - timedelta(hours=1), last_seen_at=now - timedelta(minutes=1),
            )
        )  # fmt: skip
        store.upsert_fleet_agent(
            FleetAgent(
                id="agt_live", project_id=active.id, label="coder-live", role="coder",
                pane_id="%99", session_id="ses_live", cwd=active.root,
                created_at=now - timedelta(hours=1),
            )
        )  # fmt: skip
        store.add_team_event(
            TeamEvent(
                id="evt_q", project_id=active.id, session_id=f"ses_{active.id}_7",
                kind="question", text="Which cache?", created_at=now - timedelta(hours=1),
            )
        )  # fmt: skip
    built: dict[str, list[str]] = {"rows": [], "sessions": []}
    rows, sessions = store_module._row_to_fleet_agent, store_module._row_to_session

    def row_built(row: Any) -> FleetAgent:
        built["rows"].append(row["project_id"])
        return rows(row)

    def session_built(row: Any) -> TeamSession:
        built["sessions"].append(row["project_id"])
        return sessions(row)

    monkeypatch.setattr(store_module, "_row_to_fleet_agent", row_built)
    monkeypatch.setattr(store_module, "_row_to_session", session_built)
    tmux = FakeTmux()
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: tmux)
    items = scan_needs_you(remote_needs.live_needs_sources(), now=now, dismissed=())
    assert [(item.kind, item.reason) for item in items] == [
        ("board_question", "coder asks on the board")
    ], "the question's author was read, however long ago it was seen"
    assert dormant.id not in built["rows"] + built["sessions"], "no live row: never listed"
    listing = history + 1  # fleet.list_agents' own read of every row and session, once
    assert built["rows"].count(active.id) <= listing + 1, "and the live row the scan counts"
    assert built["sessions"].count(active.id) <= listing + 2, "the live one and the author"


def test_the_live_sources_keep_a_board_question_however_busy_the_board_gets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the store: a manager's question three hours old, under 300 newer notes, is
    still open, and a reply to the manager under 300 more still answers it."""
    now = datetime.now(UTC)
    project = ProjectInfo(id="prj_busy", root=tmp_path / "busy")

    def written(
        kind: str, at: datetime, *, session_id: str | None = None, to: str | None = None
    ) -> TeamEvent:
        return TeamEvent(
            id=f"evt_{kind}_{at.timestamp()}_{session_id}_{to}", project_id=project.id,
            session_id=session_id, kind=kind, text=kind, to_role=to, created_at=at,
        )  # fmt: skip

    def traffic(store: Any, start: datetime) -> None:
        for n in range(remote_needs.NEEDS_BOARD_EVENTS):
            note = written("note", start + timedelta(seconds=n), session_id="ses_c")
            store.add_team_event(note.model_copy(update={"id": f"evt_{start}_{n}"}))

    with store_session() as store:
        store.onboard_project(project)
        store.upsert_session(
            TeamSession(
                id="ses_m", project_id=project.id, role="manager", label="manager",
                started_at=now - timedelta(hours=4), last_seen_at=now - timedelta(hours=3),
            )
        )  # fmt: skip
        store.add_team_event(written("question", now - timedelta(hours=3), session_id="ses_m"))
        traffic(store, now - timedelta(hours=2))
    sources = remote_needs.live_needs_sources()
    (item,) = scan_needs_you(sources, now=now, dismissed=())
    assert (item.kind, item.reason) == ("board_question", "manager asks on the board")
    with store_session() as store:
        store.add_team_event(written("note", now - timedelta(hours=1), to="manager"))
        traffic(store, now - timedelta(minutes=50))
    assert scan_needs_you(sources, now=now, dismissed=()) == []


def test_the_live_sources_take_a_manager_stopped_after_its_result_for_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the store, with the exit announced as ``fleet stop`` announces it, under the
    manager's own session (``fleet._emit_exit``): a manager that reported and was then
    stopped, while a coder still works, finished its job."""
    now = datetime.now(UTC)
    hour_ago = now - timedelta(hours=1)
    root = tmp_path / "alpha"
    with store_session() as store:
        project = store.onboard_project(ProjectInfo(id="prj_alpha", root=root))
        for session_id, role in (("ses_m", "manager"), ("ses_c", "coder")):
            store.upsert_session(
                TeamSession(
                    id=session_id, project_id=project.id, role=role, started_at=hour_ago,
                    last_seen_at=now - timedelta(seconds=30),
                )
            )  # fmt: skip
        store.upsert_fleet_agent(
            FleetAgent(
                id="agt_c", project_id=project.id, label="coder-1", role="coder", pane_id="%1",
                session_id="ses_c", cwd=root, created_at=hour_ago,
            )
        )  # fmt: skip
        manager = FleetAgent(
            id="agt_m", project_id=project.id, label="manager", role="manager", pane_id="%2",
            session_id="ses_m", cwd=root, created_at=hour_ago, ended_at=now - timedelta(minutes=5),
        )  # fmt: skip
        store.upsert_fleet_agent(manager)
        store.add_team_event(
            TeamEvent(
                id="evt_r", project_id=project.id, session_id="ses_m", kind="result",
                text="Shipped.", created_at=now - timedelta(minutes=6),
            )
        )  # fmt: skip
        fleet_service._emit_exit(store, manager)
    tmux = FakeTmux()
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: tmux)
    items = scan_needs_you(remote_needs.live_needs_sources(), now=now, dismissed=())
    assert [item.kind for item in items] == ["board_result"]


# --- the watcher --------------------------------------------------------------------------


def _server_sources() -> Sources:
    return Sources(
        projects=lambda: [],
        fleet=lambda project: {},
        board=lambda project: {},
        tasks=lambda project: [],
        memory=lambda project: [],
        panes=lambda agent, project, history: {"rows": [], "width": 0, "height": 0},
        explainability=lambda agent, project: {"available": False},
    )


@pytest.fixture
def runtime() -> Runtime:
    return make_runtime()


def _until_true(check: Callable[[], bool], seconds: float = 5.0) -> None:
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, "it never happened"
        time.sleep(0.01)


def test_the_watcher_scans_only_while_a_device_exists(runtime: Runtime, tmp_path: Path) -> None:
    """Nobody to show it to, no scan: no store reads, no tmux spawns."""
    app = build_app(runtime, sources=_server_sources(), dist_dir=tmp_path)
    made: list[NeedsSources] = []

    def counted() -> NeedsSources:
        made.append(_sources(Fleet()))
        return made[-1]

    watcher = RemoteNeedsWatcher(app.kit, sources=counted, interval=0.01)
    watcher.start_watching()
    try:
        threading.Event().wait(0.2)
        assert made == [] and watcher.needs_scanned_at() is None
        assert unlock(make_client(app), runtime).status_code == 200
        _until_true(lambda: watcher.needs_scanned_at() is not None)
        assert any(t.name == "asq-remote-needs" for t in threading.enumerate())
    finally:
        watcher.stop_watching()
    assert not watcher.needs_watching()


def test_every_scan_reaches_every_listener_and_a_failing_one_costs_nothing(
    runtime: Runtime, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    app = build_app(runtime, sources=_server_sources(), dist_dir=tmp_path)
    fleet = _working(_tail(_tool("toolu_q", "AskUserQuestion", **QUESTION)))
    clock = iter([NOW, NOW + timedelta(seconds=3)])
    watcher = RemoteNeedsWatcher(
        app.kit, sources=lambda: _sources(fleet), clock=lambda: next(clock)
    )
    heard: list[tuple[list[NeedsItem], datetime]] = []

    def broken(items: list[NeedsItem], scanned_at: datetime) -> None:
        raise RuntimeError("a listener's bug")

    app.kit.needs_listeners.extend([broken, lambda items, at: heard.append((items, at))])
    first = watcher.scan_needs_now()
    second = watcher.scan_needs_now()
    assert [at for _items, at in heard] == [NOW, NOW + timedelta(seconds=3)]
    assert [items for items, _at in heard] == [first, second]
    assert [item.kind for item in first] == ["question"] and first == second
    assert sum("needs listener failed" in r.getMessage() for r in caplog.records) == 2
    assert watcher.needs_items_now() == second
    assert watcher.needs_scanned_at() == NOW + timedelta(seconds=3)


def test_a_card_dismissed_while_a_scan_runs_stays_dismissed(
    runtime: Runtime, tmp_path: Path
) -> None:
    """The scan reads the dismissals before it starts and publishes its snapshot whole when
    it ends. A dismissal in between was dropped from the snapshot of the moment, and the scan
    then put the card back on every phone and before the push sender, which pushed it."""
    app = build_app(runtime, sources=_server_sources(), dist_dir=tmp_path)
    fleet = _working(_tail(_tool("toolu_q", "AskUserQuestion", **QUESTION)))
    listing, held = threading.Event(), threading.Event()

    def slow_listing(project: ProjectInfo) -> list[FleetAgentStatus]:
        if fleet.listed:  # every scan after the first is held while the card is dismissed
            listing.set()
            assert held.wait(5.0)
        fleet.listed += 1
        return list(fleet.agents)

    watcher = RemoteNeedsWatcher(
        app.kit, sources=lambda: replace(_sources(fleet), list_agents=slow_listing)
    )
    heard: list[list[str]] = []
    app.kit.needs_listeners.append(lambda items, at: heard.append([item.id for item in items]))
    (card,) = watcher.scan_needs_now()
    scan = threading.Thread(target=watcher.scan_needs_now)
    scan.start()
    try:
        assert listing.wait(5.0), "the second scan read the dismissals and is listing"
        record_needs_dismissal(card.id)  # what POST api/needs/dismiss does, in its order
        watcher.needs_forget(card.id)
        assert watcher.needs_items_now() == []
    finally:
        held.set()
        scan.join(5.0)
    assert watcher.needs_items_now() == [] and watcher.needs_items_json() == []
    assert heard == [[card.id], []], "the push sender heard it gone too"
    assert watcher.scan_needs_now() == [] and heard[-1] == [], "and every scan after"


def test_the_stream_and_the_heartbeat_read_the_watchers_snapshot(
    runtime: Runtime, tmp_path: Path
) -> None:
    app = build_app(runtime, sources=_server_sources(), dist_dir=tmp_path)
    assert remote_needs.needs_ws_frames(app.kit) == []
    assert remote_needs.needs_scanned_iso(app.kit) is None
    fleet = _working(_tail(_tool("toolu_q", "AskUserQuestion", **QUESTION)))
    watcher = RemoteNeedsWatcher(app.kit, sources=lambda: _sources(fleet), clock=lambda: NOW)
    app.kit.lane_state["needs"] = watcher
    assert remote_needs.needs_ws_frames(app.kit) == [], "nothing until the first scan"
    (item,) = watcher.scan_needs_now()
    assert remote_needs.needs_ws_frames(app.kit) == [
        ("needs_you", {"items": [item.needs_item_json()]})
    ]
    assert remote_needs.needs_scanned_iso(app.kit) == "2026-10-07T12:00:00+00:00"
    assert watcher.needs_payload_now() == {
        "items": [item.needs_item_json()],
        "scanned_at": "2026-10-07T12:00:00+00:00",
    }


# --- the routes (SPEC §4.6) ---------------------------------------------------------------


@dataclass
class Live:
    """A served app over fake needs sources and a fake tmux, one device unlocked."""

    app: Any
    client: Any
    runtime: Runtime
    fleet: Fleet
    tmux: FakeTmux

    def url(self, path: str) -> str:
        return f"{base(self.runtime)}/api/{path}"

    def feed(self) -> list[dict[str, Any]]:
        response = self.client.get(self.url("needs"))
        assert response.status_code == 200, response.text
        items: list[dict[str, Any]] = response.json()["items"]
        return items

    def card(self, kind: str) -> dict[str, Any]:
        return next(item for item in self.feed() if item["kind"] == kind)

    def audit(self) -> list[str]:
        path = remote_audit_path()
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


@pytest.fixture
def live(runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Live:
    now = datetime.now(UTC)
    status, tail = _asking(now)
    manager = _row("manager", role="manager", created=now - timedelta(hours=2))
    managing = _session(manager, seen=now - timedelta(minutes=1))
    fleet = Fleet(
        agents=[status, _status(manager, "working", managing)],
        sessions=[managing],
        events=[_event(5, "question", "Ship on Friday?", session=managing, at=now)],
    )
    fleet.tails["/transcripts/coder-1.jsonl"] = tail
    tmux = FakeTmux()
    monkeypatch.setattr(remote_needs, "live_needs_sources", lambda: _sources(fleet))
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: tmux)
    monkeypatch.setattr(remote_needs, "NEEDS_RESCAN_AFTER_ANSWER", 0.0)
    app = build_app(runtime, sources=_server_sources(), dist_dir=tmp_path)
    client = make_client(app)
    assert unlock(client, runtime).status_code == 200
    return Live(app, client, runtime, fleet, tmux)


def test_the_feed_is_one_scan_away_when_no_watcher_runs(live: Live) -> None:
    response = live.client.get(live.url("needs"))
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"items", "scanned_at"} and body["scanned_at"] is not None
    assert [item["kind"] for item in body["items"]] == ["permission", "board_question"]
    card = body["items"][0]
    assert set(card) == {
        "id", "kind", "project", "agent", "agent_id", "reason", "excerpt", "detail",
        "answers", "since", "actions",
    }  # fmt: skip
    assert card["project"] == {"id": "prj_alpha", "name": "alpha"}
    assert card["detail"] == {"tool": "Bash", "input": {"command": "git push"}}
    assert "push_after" not in card


def test_a_dismissal_hides_a_card_for_good_and_is_audited(live: Live) -> None:
    assert live.runtime.allow_write is False, "not write-gated: it changes what is shown"
    card = live.card("permission")
    response = live.client.post(live.url("needs/dismiss"), json={"id": card["id"]})
    assert response.status_code == 200 and response.json() == {"dismissed": card["id"]}
    assert [item["kind"] for item in live.feed()] == ["board_question"]
    assert card["id"] in load_needs_dismissals()
    assert live.audit()[-1].endswith(f"needs/dismiss {card['id']} permission coder-1@prj_alpha")


@pytest.mark.parametrize(
    ("body", "status", "error"),
    [
        ({"id": "ny_0000000000000000"}, 404, "not_found"),
        ({}, 400, "invalid"),
        ({"id": 7}, 400, "invalid"),
        ({"id": "ny_" + "f" * 62}, 413, "too_large"),
    ],
)
def test_a_dismissal_of_nothing_is_refused(
    live: Live, body: dict[str, Any], status: int, error: str
) -> None:
    response = live.client.post(live.url("needs/dismiss"), json=body)
    assert (response.status_code, response.json()["error"]) == (status, error)
    # The trail may hold other lines (the unlock is audited, SPEC §1.3); not a dismissal.
    assert not any(" needs/dismiss " in line for line in live.audit())


def test_an_answer_is_refused_while_writes_are_off(live: Live) -> None:
    card = live.card("permission")
    response = live.client.post(live.url("needs/answer"), json={"id": card["id"], "keys": ["1"]})
    assert response.status_code == 403
    assert response.json() == {"error": "read_only", "message": READ_ONLY_REASON}
    assert live.tmux.typed == []


def test_an_answer_types_into_the_agent_is_audited_and_clears_its_card(live: Live) -> None:
    live.runtime.set_allow_write(True)
    card = live.card("permission")
    watcher = live.app.kit.lane_state["needs"]
    scanned = watcher.needs_scanned_at()
    response = live.client.post(live.url("needs/answer"), json={"id": card["id"], "keys": ["1"]})
    assert response.status_code == 200, response.text
    assert response.json() == {
        "answered": card["id"],
        "agent": "coder-1",
        "project": "prj_alpha",
        "sent": True,
    }
    assert live.tmux.typed == [("keys", "%7", "1")]
    assert live.audit()[-1].endswith(
        f"needs/answer answer {card['id']} permission coder-1@prj_alpha keys=[1] text=0ch "
        "enter=False"
    )
    _until_true(lambda: watcher.needs_scanned_at() != scanned)


def test_an_answer_in_words_is_typed_then_entered(live: Live) -> None:
    live.runtime.set_allow_write(True)
    card = live.card("permission")
    body = {"id": card["id"], "text": "use the staging remote", "enter": True}
    assert live.client.post(live.url("needs/answer"), json=body).status_code == 200
    assert live.tmux.typed == [("text", "%7", "use the staging remote"), ("keys", "%7", "Enter")]
    assert live.audit()[-1].endswith("keys=0 text=22ch enter=True")


def test_a_card_that_changed_under_the_phone_is_stale(live: Live) -> None:
    """A stale card's ``1`` must never approve the prompt that replaced it."""
    live.runtime.set_allow_write(True)
    gone = live.client.post(
        live.url("needs/answer"), json={"id": "ny_0000000000000000", "keys": ["1"]}
    )
    assert gone.status_code == 409
    assert gone.json() == {
        "error": "stale",
        "message": "that card no longer needs you",
        "current": [],
    }
    card = live.card("permission")
    status, tail = _asking(datetime.now(UTC), tool="toolu_next")
    live.fleet.agents[0] = status
    live.fleet.tails["/transcripts/coder-1.jsonl"] = tail
    stale = live.client.post(live.url("needs/answer"), json={"id": card["id"], "keys": ["1"]})
    assert stale.status_code == 409
    body = stale.json()
    assert body["error"] == "stale" and body["message"] == "coder-1 no longer shows that permission"
    assert [item["id"] for item in body["current"]] == [
        needs_item_id(PROJECT.id, "permission", "toolu_next")
    ]
    assert live.tmux.typed == []


def test_a_sub_agents_card_is_stale_once_it_asks_again(live: Live) -> None:
    """The ``Task`` is pending through every prompt of its sub-agent's, so the card for the
    first prompt still matched the second, and its "1" approved whatever that one asked."""
    live.runtime.set_allow_write(True)
    now = datetime.now(UTC)
    status, tail = _in_a_sub_agent(now - timedelta(minutes=2))
    live.fleet.agents[0], live.fleet.tails["/transcripts/coder-1.jsonl"] = status, tail
    card = live.card("permission")
    asks_again, _same = _in_a_sub_agent(now - timedelta(seconds=1))
    live.fleet.agents[0] = asks_again  # answered at the machine; the sub-agent asked again
    stale = live.client.post(live.url("needs/answer"), json={"id": card["id"], "keys": ["1"]})
    assert stale.status_code == 409 and stale.json()["error"] == "stale"
    (current,) = stale.json()["current"]
    assert current["kind"] == "permission" and current["id"] != card["id"]
    assert live.tmux.typed == []


def test_a_board_card_is_answered_on_the_board(live: Live) -> None:
    live.runtime.set_allow_write(True)
    card = live.card("board_question")
    response = live.client.post(live.url("needs/answer"), json={"id": card["id"], "text": "yes"})
    assert response.status_code == 400
    assert response.json() == {"error": "not_answerable", "message": "reply on the board instead"}


@pytest.mark.parametrize(
    "keys", [["C-c"], ["C-d"], ["F1"], ["1", "kill-server"], ["Enter;"], ["-l"], [7]]
)
def test_an_answer_is_only_the_keys_an_answer_needs(live: Live, keys: list[Any]) -> None:
    live.runtime.set_allow_write(True)
    card = live.card("permission")
    response = live.client.post(live.url("needs/answer"), json={"id": card["id"], "keys": keys})
    assert response.status_code == 400 and response.json()["error"] == "invalid_key"
    assert live.tmux.typed == []


@pytest.mark.parametrize(
    ("extra", "status", "error"),
    [
        ({"keys": ["1"], "text": "and words"}, 400, "text_and_keys"),
        ({}, 400, "invalid"),
        ({"keys": []}, 400, "invalid"),
        ({"text": ""}, 400, "invalid"),
        ({"text": 7}, 400, "invalid"),
        ({"text": "x" * 2_049}, 413, "too_large"),
    ],
)
def test_an_answer_is_keys_or_words_never_both(
    live: Live, extra: dict[str, Any], status: int, error: str
) -> None:
    live.runtime.set_allow_write(True)
    card = live.card("permission")
    response = live.client.post(live.url("needs/answer"), json={"id": card["id"], **extra})
    assert (response.status_code, response.json()["error"]) == (status, error)
    assert live.tmux.typed == []


@pytest.mark.parametrize("text", ["\x03", "yes\x1b[201~", "no\x7f", "ok\x04"])
def test_an_answer_in_words_carries_no_control_character(live: Live, text: str) -> None:
    """Words reach the pane byte for byte, so a control in them would be a keystroke
    that no allowlist saw: a Ctrl-C past send-keys' double-press guard, written to the
    trail as ``text=1ch``. The same boundary as send-keys' text refuses it."""
    live.runtime.set_allow_write(True)
    card = live.card("permission")
    response = live.client.post(live.url("needs/answer"), json={"id": card["id"], "text": text})
    assert (response.status_code, response.json()["error"]) == (400, "invalid")
    assert "control character" in response.json()["message"]
    assert live.tmux.typed == []


def test_an_answer_waits_for_no_other_action_on_the_agent(live: Live) -> None:
    live.runtime.set_allow_write(True)
    card = live.card("permission")
    held = remote_agent_lock(PROJECT.id, "coder-1")
    assert held.acquire(blocking=False)
    try:
        response = live.client.post(
            live.url("needs/answer"), json={"id": card["id"], "keys": ["1"]}
        )
    finally:
        held.release()
    assert response.status_code == 409
    assert response.json() == {
        "error": "busy",
        "message": "another action on coder-1 is still running",
    }
    assert live.tmux.typed == []


def test_a_pane_that_is_not_the_agent_is_not_typed_into(live: Live) -> None:
    live.runtime.set_allow_write(True)
    card = live.card("permission")
    live.tmux.command = "zsh"
    response = live.client.post(live.url("needs/answer"), json={"id": card["id"], "keys": ["1"]})
    assert response.status_code == 409 and response.json()["error"] == "not_agent"
    assert live.tmux.typed == []


def test_an_answer_needs_the_listing_it_rechecks_against(live: Live) -> None:
    live.runtime.set_allow_write(True)
    card = live.card("permission")
    live.fleet.listing_fails = True
    response = live.client.post(live.url("needs/answer"), json={"id": card["id"], "keys": ["1"]})
    assert response.status_code == 503
    assert response.json() == {"error": "fleet_unavailable", "message": "tmux is not installed"}
    assert live.tmux.typed == []
    assert not remote_agent_lock(PROJECT.id, "coder-1").locked(), "the lock is let go"


def test_tmux_failing_mid_answer_is_said_and_still_on_the_trail(live: Live) -> None:
    live.runtime.set_allow_write(True)
    card = live.card("permission")
    live.tmux.fail = True
    response = live.client.post(live.url("needs/answer"), json={"id": card["id"], "keys": ["1"]})
    assert response.status_code == 503 and response.json()["error"] == "fleet_unavailable"
    assert live.audit()[-1].endswith("enter=False failed"), "part of it may have reached the pane"


def test_a_retried_answer_is_typed_once_with_the_servers_own_ledger(live: Live) -> None:
    live.runtime.set_allow_write(True)
    card = live.card("permission")
    body = {"id": card["id"], "keys": ["1"], "request_id": "c0ffee"}
    first = live.client.post(live.url("needs/answer"), json=body)
    again = live.client.post(live.url("needs/answer"), json=body)
    assert first.status_code == again.status_code == 200
    assert first.json() == again.json(), "the retry is answered from the ledger"
    assert live.tmux.typed == [("keys", "%7", "1")]


def test_an_answer_of_more_keys_than_any_pad_sends_is_refused(live: Live) -> None:
    live.runtime.set_allow_write(True)
    card = live.card("permission")
    keys = ["Down"] * 33
    response = live.client.post(live.url("needs/answer"), json={"id": card["id"], "keys": keys})
    assert (response.status_code, response.json()["error"]) == (413, "too_large"), (
        "refused as send-keys refuses it: check_remote_key_names caps keys at 32"
    )
    assert live.tmux.typed == []


# --- the stream ---------------------------------------------------------------------------


def test_the_stream_sends_the_feed_once_the_watcher_has_scanned(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, tail = _asking(datetime.now(UTC))
    fleet = Fleet(agents=[status])
    fleet.tails["/transcripts/coder-1.jsonl"] = tail
    monkeypatch.setattr(remote_needs, "live_needs_sources", lambda: _sources(fleet))
    monkeypatch.setattr(remote_needs, "NEEDS_SCAN_SECONDS", 0.02)
    app = build_app(runtime, sources=_server_sources(), dist_dir=tmp_path, tick=0.02)
    with make_client(app) as client:
        assert unlock(client, runtime).status_code == 200
        with client.websocket_connect(f"{base(runtime)}/ws") as ws:
            for _ in range(500):
                message = receive_within(ws)
                assert message["type"] == "websocket.send", message
                frame = json.loads(message["text"])
                if frame["type"] == "needs_you":
                    break
            else:
                raise AssertionError("no needs_you frame")
        (item,) = frame["payload"]["items"]
        assert item["kind"] == "permission" and item["agent"] == "coder-1"
        assert set(frame["payload"]) == {"items"}, "no scan time: it goes out on a change only"
        watcher = app.kit.lane_state["needs"]
        assert isinstance(watcher, RemoteNeedsWatcher) and watcher.needs_watching()
    assert not watcher.needs_watching(), "the lifespan stopped it"


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
