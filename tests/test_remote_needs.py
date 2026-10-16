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
import logging
import re
import shlex
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app as cli
from aisquare.core.claude_accounts import LimitNotice, format_reset
from aisquare.core.config import AccountsSettings, AppConfig, FleetSettings, save_config
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
    needs_single_agent_now,
    needs_tool_pending,
    needs_unseen,
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

_LIVE_NEEDS_SOURCES = remote_needs.live_needs_sources
"""The live sources as the module defines them, which the ``live`` fixture replaces."""

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
    row: FleetAgent,
    state: str,
    session: TeamSession | None = None,
    detail: str | None = None,
    *,
    socket_at: Path | None = None,
) -> FleetAgentStatus:
    return FleetAgentStatus.model_validate(
        {"agent": row, "state": state, "detail": detail, "session": session, "socket_at": socket_at}
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


# --- what a pane shows: Claude Code's screens, as ``capture-pane -e`` gives their rows --------

_RULE = "\x1b[2m" + "─" * 78 + "\x1b[0m"
_POINTER = "\u276f"
"""The heavy angle quote Claude Code draws before a dialog's highlighted option."""
_ASK_AGAIN = ("Yes", "Yes, and don't ask again for this command", "No, and tell Claude (esc)")


def _dialog(*words: str, options: tuple[str, ...], above: tuple[str, ...] = ()) -> list[str]:
    """A screen at a dialog: the conversation ``above``, the dialog's top rule, its ``words``
    and its numbered ``options``, the first highlighted. The dialog replaces the input box."""
    choices = [f" {_POINTER if n == 1 else ' '} {n}. {o}" for n, o in enumerate(options, start=1)]
    return [*above, "", _RULE, *(f" {line}" for line in words), "", *choices]


def _bash_screen(command: str, description: str = "", *, above: tuple[str, ...] = ()) -> list[str]:
    """Bash's permission prompt for ``command``, its title and command styled."""
    return _dialog(
        "\x1b[1mBash command\x1b[0m",
        "",
        f"  \x1b[36m{command}\x1b[0m",
        f"  {description}",
        "",
        "Do you want to proceed?",
        options=_ASK_AGAIN,
        above=above,
    )


def _question_screen(*, above: tuple[str, ...] = ()) -> list[str]:
    """AskUserQuestion's dialog for :data:`QUESTION`."""
    return _dialog(
        "☐ Cache",
        "",
        "Which store should the cache use?",
        options=("Redis", "SQLite", "none", "Type something."),
        above=above,
    )


def _fetch_screen(url: str, *, above: tuple[str, ...] = ()) -> list[str]:
    return _dialog(
        "Fetch", "", f"  {url}", "", "Do you want to allow Claude to fetch this content?",
        options=_ASK_AGAIN, above=above,
    )  # fmt: skip


def _at_work(*above: str) -> list[str]:
    """A screen with no dialog up: the conversation, then the input box between its rules."""
    return [*above, "", _RULE, "> ", _RULE, "  ? for shortcuts"]


# --- one agent: the table, in its order (SPEC §4.2) ---------------------------------------


def test_rule_1_a_gone_pane_is_lost_before_anything_its_transcript_says() -> None:
    row = _row()
    item = _one(_classify(_status(row, "lost", _session(row)), _tail(_tool("toolu_1"))))
    assert (item.kind, item.agent, item.agent_id) == ("lost", "coder-1", "agt_coder-1")
    assert item.reason == "coder-1's pane is gone"
    assert item.id == needs_item_id(PROJECT.id, "lost", "agt_coder-1")
    assert item.actions == ("restart", "stop", "dismiss")


def test_rule_1_a_held_row_is_one_lost_card_saying_why_whatever_its_board_says() -> None:
    """Review of #259, round 2: a row held as another tmux server's got rules 3 to 10 from
    its board while that was fresh: permission and question cards, pushed, whose every
    answer was a 409 `not_agent` (its pane id is another agent's on the server at its
    socket). Then, the board stale, nothing at all: an `unknown` row needs nothing, and
    `fleet_down` leaves held rows out. Before 0.8.1 the same row read `lost` and had that
    card. It is one `lost` card of its own reason, the same item while it lasts."""
    row = _row()
    pending = _tail(_tool("toolu_1"))
    fresh = _session(row, state="attention")
    stale = _session(row, state="attention", seen=NOW - timedelta(hours=1))
    held = [
        fleet_service._status(
            row, session, {row.id: fleet_service.HELD}, None, NOW, FleetSettings()
        )
        for session in (fresh, stale)
    ]
    assert all(status.held for status in held)
    # And a status that reads its board's state, as one from before the derivation held it.
    held.append(_status(row, "attention", fresh).model_copy(update={"held": True}))

    cards = [_one(_classify(status, pending)) for status in held]

    for item in cards:
        assert (item.kind, item.agent_id) == ("lost", row.id), item
        assert item.reason == "coder-1 is out of reach: another tmux server answers on its socket"
        assert item.actions == ("restart", "stop", "dismiss")
        assert item.push_after == NOW + timedelta(seconds=60)
    assert len({item.id for item in cards}) == 1, "one item while it lasts"
    # Negative control: on its own server the same row is at its permission prompt.
    assert _one(_classify(_status(row, "attention", fresh), pending)).kind == "permission"


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
    assert item.reason == "coder-1 hit its usage limit · limit resets in 2h"
    assert item.detail == {
        "text": "coder-1 hit its five-hour limit · resets 2pm",
        "resets_at": "2026-10-07T14:00:00+00:00",
    }
    assert item.since == events[1].created_at
    assert item.actions == ("switch", "open", "dismiss")


def test_rule_3_a_pending_question_is_a_question_while_the_row_reads_working() -> None:
    row = _row()
    tail = _tail(
        _tool("toolu_b", command="ls"),
        _tool("toolu_q", "AskUserQuestion", at=NOW - timedelta(minutes=1), **QUESTION),
    )
    on_screen = _question_screen(above=("● Bash(ls)", "  ⎿  a.py b.py"))
    item = _one(
        _classify(_status(row, "working", _session(row)), tail, pane_screen=lambda: on_screen)
    )
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


def test_rule_5_a_pending_tool_with_attention_is_a_prompt_about_the_call_up() -> None:
    row = _row()
    session = _session(row, state="attention")
    tail = _tail(
        _tool("toolu_a", command="git push --force", description="push it"),
        _tool("toolu_b", command="ls", at=NOW - timedelta(minutes=1)),
    )
    up = _bash_screen("git push --force", "push it", above=("● Bash(git push --force)",))
    item = _one(_classify(_status(row, "attention", session), tail, pane_screen=lambda: up))
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


def test_an_agent_at_the_folder_trust_question_is_one_card_for_its_row() -> None:
    """``fleet`` reads an agent at Claude Code's trust question ``attention`` with no session
    (no hook runs before the yes). It is a card of its own: one id for the row whatever the
    scan's clock, no quick answer (Enter there picks "No, exit"), pushed after a grace."""
    row = _row()
    detail = fleet_service.trust_prompt_detail(PROJECT.root)
    status = _status(row, "attention", detail=detail)
    item = _one(_classify(status))
    later = _one(
        needs_from_agent(status, None, project=PROJECT, events=[], now=NOW + timedelta(minutes=9))
    )
    assert item.kind == "permission" and item.id == later.id
    assert item.reason == "coder-1 is waiting for folder trust (Claude Code asks before it starts)"
    assert item.excerpt == detail
    assert f"aisquare accounts trust {shlex.quote(str(PROJECT.root))}" in detail
    assert item.answers == ()
    assert item.since == BORN and item.push_after == BORN + timedelta(seconds=30)
    # Control: the same state with any other detail is rule 8's dialog card, as before.
    other = _one(_classify(_status(row, "attention")))
    assert other.reason == "coder-1 shows a dialog that needs you" and other.id != item.id


def test_a_permission_card_shows_the_call_not_eleven_keys_of_it() -> None:
    """Only the scalar values of eleven known keys were kept, and nothing said the rest were
    gone: an MCP merge's owner, repo and pull request showed as ``input: {}``, a
    ``MultiEdit`` as its file without a single edit, each card reading as the whole call
    beside the "1" that approves it (review of #243, round 6). Every field shows now, the
    known keys first; a list or an object as its JSON."""
    row = _row()
    attention = _status(row, "attention", _session(row, state="attention"))
    merge = _tool(
        "toolu_m",
        "mcp__github__merge_pull_request",
        owner="acme",
        repo="prod",
        pull_number=42,
        merge_method="squash",
    )
    assert _one(_classify(attention, _tail(merge))).detail == {
        "tool": "mcp__github__merge_pull_request",
        "input": {"owner": "acme", "repo": "prod", "pull_number": 42, "merge_method": "squash"},
    }
    edits = [{"old_string": "a", "new_string": "b"}, {"old_string": "c", "replace_all": True}]
    multi = _tool("toolu_e", "MultiEdit", edits=edits, file_path="/etc/hosts", dry_run=None)
    assert _one(_classify(attention, _tail(multi))).detail["input"] == {
        "file_path": "/etc/hosts",
        "edits": '[{"old_string":"a","new_string":"b"},{"old_string":"c","replace_all":true}]',
        "dry_run": "null",
    }


def test_a_list_or_an_object_cut_at_2000_characters_is_marked_cut_as_a_string_is() -> None:
    """A list or an object shows as its JSON, cut at 2 000 characters as a string is (round 6).
    ``cut`` is counted on what the card would show whole, its JSON: counted on the call's
    own input, where ``edits`` is a list, a MultiEdit's 3 035 characters of edits showed
    their first 2 000 with no "Not all of it", reading as the whole of what the "1" beside
    them approves. Nothing tested it (review of #243, sweep 5)."""
    row = _row()
    attention = _status(row, "attention", _session(row, state="attention"))
    edits = [{"old_string": "a" * 1500, "new_string": "b" * 1500}]
    query = {"ids": list(range(600)), "table": "accounts"}
    calls = {
        "edits": _tool("toolu_e", "MultiEdit", edits=edits, file_path="/etc/hosts"),
        "where": _tool("toolu_q", "mcp__db__delete_rows", where=query),
    }
    for key, call in calls.items():
        whole = json.dumps(call.input[key], ensure_ascii=False, separators=(",", ":"))
        detail = _one(_classify(attention, _tail(call))).detail
        fields = detail["input"]
        assert isinstance(fields, dict)
        shown = fields[key]
        assert len(whole) > 2000 and len(shown) == 2000, key
        assert shown == whole[:1999] + "…", key
        assert detail["cut"] == {key: len(whole)}, key
    assert len(json.dumps(edits, separators=(",", ":"))) == 3035


def test_a_call_of_more_fields_than_a_card_holds_says_how_many_it_leaves_out() -> None:
    """The page draws twenty fields, and a name it could not hold to the card's size is
    none of them: each one left out is counted, so the card says so."""
    row = _row()
    attention = _status(row, "attention", _session(row, state="attention"))
    fields: dict[str, Any] = {f"f{n:02}": n for n in range(25)}
    fields["x" * 65] = "a name longer than a card holds"
    fields["bad\nname"] = 1
    call = _tool("toolu_w", "mcp__wide__call", **fields)
    detail = _one(_classify(attention, _tail(call))).detail
    shown = detail["input"]
    assert isinstance(shown, dict) and list(shown) == [f"f{n:02}" for n in range(20)]
    assert detail["omitted"] == 7
    narrow: dict[str, Any] = {f"f{n:02}": n for n in range(20)}
    whole = _tool("toolu_n", "mcp__narrow__call", **narrow)
    assert "omitted" not in _one(_classify(attention, _tail(whole))).detail


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


def test_a_sub_agents_prompt_card_says_the_call_it_answers_is_not_on_it() -> None:
    """The ``Task`` is the one tool the agent's own transcript holds, so its description and
    prompt were the card's detail, with nothing to say that the "1" beside them approves
    whatever the sub-agent asked: a ``git push --force`` approved from a card that never
    named it (review of #243, round 6). The detail is still the task, marked ``subagent``,
    which the page says in words."""
    item = _one(_classify(*_in_a_sub_agent(NOW - timedelta(seconds=30))))
    assert item.detail == {"tool": "Task", "input": {"description": "the cache"}, "subagent": True}
    row = _row()
    direct = _one(
        _classify(
            _status(row, "attention", _session(row, state="attention")),
            _tail(_tool("toolu_b", command="git push --force")),
        )
    )
    assert "subagent" not in direct.detail


def test_a_sub_agents_next_prompt_has_no_item_until_its_notice() -> None:
    """The pane goes quiet 5 s after the next prompt is drawn and its notice comes at 6 s: in
    between, ``last_seen_at`` still names the prompt before, whose id an item would carry.
    A notice whose hook waited out the store's lock comes some seconds later still."""
    seen = NOW - timedelta(minutes=1)
    status, tail = _in_a_sub_agent(seen)
    drawn = NOW - timedelta(seconds=6)
    assert _classify(status, tail, pane_output=lambda: drawn) == []
    slow = NOW - timedelta(seconds=13)
    assert _classify(status, tail, pane_output=lambda: slow) == [], "a hook slowed by the lock"
    noticed = _one(
        _classify(*_in_a_sub_agent(NOW - timedelta(seconds=1)), pane_output=lambda: drawn)
    )
    assert noticed.since == NOW - timedelta(seconds=1)
    redrawn = _one(_classify(status, tail, pane_output=lambda: NOW - timedelta(seconds=40)))
    assert redrawn.since == seen, "output long after the notice is a redraw, not a prompt"
    assert _one(_classify(status, tail, pane_output=lambda: None)).since == seen


def test_a_call_beside_one_answered_has_its_own_card_never_the_answered_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #259, remote focus, 1/12, and round 8, 1/9: one message calls two tools that
    each need a permission. The first's prompt is answered, which writes nothing until it
    ends, and the second's is drawn: the same pending calls, the same ``last_seen_at``.
    Rule 5's card was the first call's again, its id and its URL, and a tap of "1" on it
    re-derived as current and approved the other call. 1/12 hid the card while the pane
    printed after the newest notice, which rested on when Claude Code draws a call's prompt
    beside another's. The card is the call the screen shows, however late it printed.

    Round 9, 4/8: and it stays that call's card once the call's own notice lands, seconds
    after its prompt is drawn. A notice later than the turn's first made it rule 8's card,
    under another id and without answers, and both could be pushed for the one prompt."""
    row = _row()
    asked = NOW - timedelta(seconds=40)
    called = asked - timedelta(seconds=6)
    session = _session(row, state="attention", seen=asked)
    said = "Claude needs your permission to use WebFetch"
    events = [_event(4, "attention", said, session=session, at=asked)]
    first = _tool("toolu_a", "WebFetch", at=called, url="https://a.example/")
    second = _tool("toolu_b", "WebFetch", at=called, url="https://b.example/")
    tail = _tail(first, second, at=called)
    status = _status(row, "attention", session)
    above = ("● Fetch(https://a.example/)", "● Fetch(https://b.example/)")
    a_up = _fetch_screen("https://a.example/", above=above)
    b_up = _fetch_screen("https://b.example/", above=above)
    card = _one(_classify(status, tail, events, pane_screen=lambda: a_up))
    assert card.id == needs_item_id(PROJECT.id, "permission", "toolu_a")
    assert card.detail["input"] == {"url": "https://a.example/"}
    for drawn in (NOW - timedelta(seconds=6), NOW - timedelta(seconds=25), None):
        b_card = _one(
            _classify(
                status, tail, events, pane_output=lambda at=drawn: at, pane_screen=lambda: b_up
            )
        )
        assert b_card.id == needs_item_id(PROJECT.id, "permission", "toolu_b"), drawn
        assert b_card.detail["input"] == {"url": "https://b.example/"}
    fleet = Fleet(agents=[status], events=events, screen=b_up)
    fleet.tails[f"/transcripts/{row.label}.jsonl"] = tail
    snap = _now_of(fleet, FakeTmux(reference=NOW), monkeypatch)
    assert not needs_item_current(snap, card.id), "a tap on the first call's card is stale"
    assert needs_dialog_open(snap), "the second prompt is up all the same"
    noticed = _status(row, "attention", _session(row, state="attention", seen=NOW))
    landed = _one(_classify(noticed, tail, events, pane_screen=lambda: b_up))
    assert landed.id == b_card.id, "its own notice landed: one card, one push, for its prompt"
    assert landed.answers == b_card.answers != ()
    # A screen that names neither is the plain dialog's card, its answers the key pad's.
    for unread in (None, _at_work(*above)):
        dialog = _one(_classify(status, tail, events, pane_screen=lambda rows=unread: rows))
        assert dialog.kind == "permission" and dialog.answers == () and dialog.id != card.id
    # Negative control: with one call pending its prompt can only be its own, screen or not.
    assert _one(_classify(status, _tail(first, at=called), events)).id == card.id


_PUSH = _tool("toolu_push", command="git push --force origin main", description="Publish it")
_RM = _tool("toolu_rm", command="rm -rf dist", description="Clear the build")
_TESTS = _tool("toolu_tests", command="npm test -- --watch=false", description="Run the tests")
_LS = _tool("toolu_ls", command="ls", description="List the files")
_ASK = _tool("toolu_q", "AskUserQuestion", **QUESTION)
_TASK = _tool("toolu_task", "Task", description="the cache", prompt="Find the cache")
_PLAN = _tool("toolu_p", "ExitPlanMode", plan="# Cache plan\n\n1. Add SQLite")
_SUB_AGENTS = PendingTool("toolu_sub", "Bash", "Bash(cat .env)", {"command": "cat .env"}, None)
"""A call of a sub-agent's: its prompt is on the agent's screen, its record in its own."""


def _screen_of(up: PendingTool | None, pending: tuple[PendingTool, ...]) -> list[str]:
    """The agent's screen with ``up``'s prompt drawn under the conversation, which shows a
    line for every call pending; with ``up`` ``None``, a tool at work, a numbered list in the
    conversation above the input box (no dialog)."""
    above = tuple(f"● {tool.summary}" for tool in pending)
    if up is None:
        return _at_work(*above, "  1. git push --force origin main", "  2. rm -rf dist")
    if up.name == "AskUserQuestion":
        return _question_screen(above=above)
    return _bash_screen(str(up.input["command"]), str(up.input.get("description", "")), above=above)


_ORDERINGS = [
    # (what is pending, oldest first; whose prompt the screen may show)
    ("a question after a permission", (_PUSH, _ASK), (_PUSH, _ASK, None)),
    ("a question before a permission", (_ASK, _PUSH), (_ASK, _PUSH, None)),
    ("two permissions", (_PUSH, _RM), (_PUSH, _RM, None)),
    ("a call running beside a prompt", (_TESTS, _RM), (_RM, None)),
    ("a sub-agent beside a call", (_TASK, _RM), (_SUB_AGENTS, _RM, None)),
    ("a plan beside a call", (_RM, _PLAN), (_RM, None)),
    ("a call too short to name", (_LS, _ASK), (_LS, _ASK, None)),
]


@pytest.mark.parametrize("state", ["attention", "working"])
@pytest.mark.parametrize(
    ("pending", "up"),
    [
        pytest.param(pending, up, id=f"{name}, {'nothing' if up is None else up.tool_use_id} up")
        for name, pending, ups in _ORDERINGS
        for up in ups
    ],
)
def test_a_card_with_digits_is_only_ever_the_call_whose_prompt_is_up(
    pending: tuple[PendingTool, ...], up: PendingTool | None, state: str
) -> None:
    """Review of #259, round 8, 1/9 (major): rule 5 took the first ``AskUserQuestion``
    anywhere among the calls pending, then the first ``ExitPlanMode``, then the oldest call.
    One message calls ``git push --force`` and asks a question: Claude Code runs them in
    order, so the push's prompt is up and the question not drawn, and the card was the
    question's, whose "1" approved the push. Every ordering, every prompt it may show: a
    card whose answers type a digit names the call up, and a screen that shows none of the
    agent's own calls (a sub-agent's, a tool at work) has no such card."""
    row = _row()
    session = _session(row, state=state, seen=NOW - timedelta(seconds=30))
    said = "Claude needs your permission to use Bash"
    events = [_event(4, "attention", said, session=session, at=NOW - timedelta(seconds=30))]
    screen = _screen_of(up, pending)
    items = _classify(
        _status(row, state, session), _tail(*pending), events, pane_screen=lambda: screen
    )
    digits = [item for item in items if any(key.isdigit() for a in item.answers for key in a.keys)]
    if up is None or up not in pending:
        assert digits == [], items
        return
    named = needs_item_id(PROJECT.id, digits[0].kind if digits else "-", up.tool_use_id)
    assert [item.id for item in digits] in ([], [named]), items
    # And the card is there wherever the screen names the call up among calls it can tell apart.
    told = up is not _LS and _TASK not in pending and _PLAN not in pending
    if told and (state == "attention" or up is _ASK):
        assert [item.id for item in digits] == [named], items


def test_each_dialog_drawn_while_a_question_waits_is_a_card_of_its_own() -> None:
    """Review of #259, round 11, 1/13: named ``dialog:{question}``, every dialog drawn while a
    question waited behind a sub-agent was one card. Only the sub-agent's first prompt was
    pushed (a pushed id is kept a week), and one dismissal hid every later one, the
    question's own dialog too. Each dialog is a card of its own; one whose highlight moved,
    or whose notice landed, is the same card."""
    row = _row()
    session = _session(row, state="working", seen=NOW - timedelta(minutes=2))
    status, tail = _status(row, "working", session), _tail(_TASK, _ASK)
    above = (f"● {_TASK.summary}",)
    first = _bash_screen("npm test", above=above)
    second = _bash_screen("git push origin main", above=above)
    one = _one(_classify(status, tail, pane_screen=lambda: first))
    two = _one(_classify(status, tail, pane_screen=lambda: second))
    asked = _one(_classify(status, tail, pane_screen=lambda: _question_screen(above=above)))
    assert len({one.id, two.id, asked.id}) == 3, "three dialogs, three cards"
    for card in (one, two, asked):
        assert card.reason == "coder-1 shows a dialog that needs you" and card.answers == ()
    pointer = [line.replace(_POINTER, " ") if "1." in line else line for line in second]
    moved = [line.replace("  2.", f"{_POINTER} 2.") for line in pointer]
    assert moved != second
    assert _one(_classify(status, tail, pane_screen=lambda: moved)).id == two.id
    noticed = _session(row, state="attention", seen=NOW)
    events = [_event(9, "attention", "Claude needs your permission to use Bash", session=noticed)]
    landed = _classify(_status(row, "attention", noticed), tail, events, pane_screen=lambda: second)
    assert _one(landed).id == two.id, "its notice landing names it the same: one push"


def test_a_dialog_taller_than_the_screen_is_one_card_whatever_rows_the_screen_shows() -> None:
    """Review of #259, round 12, 6/9: with no rule above its first option (a long Edit diff, a
    plan whose top scrolled off), the card was named by every row the screen showed above
    ``1.``, and a resize changed them (asq attaching to the pane adopts its size): a new
    card, pushed again, its dismissal lost. It is named after the question alone then; a
    dialog its rule bounds is still a card of its own."""
    row = _row()
    session = _session(row, state="working", seen=NOW - timedelta(minutes=2))
    status, tail = _status(row, "working", session), _tail(_TASK, _ASK)
    diff = [f"  {n} + the line numbered {n}" for n in range(60)]
    tall = _dialog("Edit file", *diff, "Do you want to make this edit?", options=_ASK_AGAIN)
    shown, resized = tall[-24:], tall[-40:]
    assert not any("─" in line for line in (*shown, *resized)), "its top rule scrolled off"
    one = _one(_classify(status, tail, pane_screen=lambda: shown))
    assert one.reason == "coder-1 shows a dialog that needs you"
    assert _one(_classify(status, tail, pane_screen=lambda: resized)).id == one.id
    bounded = _bash_screen("npm test", above=(f"● {_TASK.summary}",))
    assert _one(_classify(status, tail, pane_screen=lambda: bounded)).id != one.id


def test_a_dialog_drawn_while_a_question_waits_is_dated_by_the_scan_that_first_saw_it() -> None:
    """Review of #259, round 11, 8/13: the card was dated when the question was asked, so a
    sub-agent's prompt drawn 25 minutes after it was a card 25 minutes old, in the feed and
    in ``asq remote needs``. It is dated by the first scan that saw it, and keeps that date."""
    row = _row()
    asked_at = NOW - timedelta(minutes=25)
    session = _session(row, state="working", seen=asked_at)
    task, ask = replace(_TASK, at=asked_at), replace(_ASK, at=asked_at)
    fleet = Fleet(
        agents=[_status(row, "working", session)],
        screen=_bash_screen("npm test", above=(f"● {_TASK.summary}",)),
    )
    fleet.tails[f"/transcripts/{row.label}.jsonl"] = _tail(task, ask, at=asked_at)
    memory: dict[str, datetime] = {}
    (first,) = scan_needs_you(_sources(fleet), now=NOW, dismissed=(), first_seen=memory)
    assert first.reason == "coder-1 shows a dialog that needs you"
    assert (first.since, first.push_after) == (NOW, NOW)
    later = NOW + timedelta(minutes=5)
    (again,) = scan_needs_you(_sources(fleet), now=later, dismissed=(), first_seen=memory)
    assert (again.id, again.since, again.push_after) == (first.id, NOW, NOW)


def test_the_same_dialog_drawn_again_while_a_question_waits_is_a_card_of_its_own(
    live: Live,
) -> None:
    """Review of #259, round 12, 5/9: named after the question and what the dialog shows, the
    same command asked for twice drew the same dialog, one id: the second was never pushed (a
    pushed id is kept a week), and the first one's dismissal hid it. Its id names the scan
    that first saw it since one last did not: still drawn, the same card; drawn again, a card
    of its own, which the first one's dismissal does not hide. An action reads it by the id
    the feed gave it (``needs_agent_now``, from the sightings the watcher kept)."""
    row = _row()
    session = _session(row, state="working", seen=NOW - timedelta(minutes=2))
    above = (f"● {_TASK.summary}",)
    fleet = Fleet(agents=[_status(row, "working", session)], screen=_bash_screen("npm test"))
    fleet.tails[f"/transcripts/{row.label}.jsonl"] = _tail(_TASK, _ASK)
    memory: dict[str, datetime] = {}

    def scan(at: timedelta, dismissed: tuple[str, ...] = ()) -> list[NeedsItem]:
        return scan_needs_you(_sources(fleet), now=NOW + at, dismissed=dismissed, first_seen=memory)

    (first,) = scan(timedelta(0))
    assert first.reason == "coder-1 shows a dialog that needs you"
    assert [item.id for item in scan(timedelta(seconds=3))] == [first.id], "still drawn"
    fleet.screen = _at_work(*above)  # answered: the sub-agent runs it
    assert scan(timedelta(seconds=6)) == []
    fleet.screen = _bash_screen("npm test")  # and asks for it again
    (again,) = scan(timedelta(seconds=9), dismissed=(first.id,))
    assert again.id != first.id and again.since == NOW + timedelta(seconds=9)

    # Through the watcher: the feed's id is the one an action re-derives.
    called = datetime.now(UTC) - timedelta(minutes=1)
    task, ask = replace(_TASK, at=called), replace(_ASK, at=called)
    live.fleet.tails["/transcripts/coder-1.jsonl"] = _tail(task, ask, at=called)
    live.fleet.screen = _bash_screen("npm test", above=above)
    card = live.card("permission")
    assert card["reason"] == "coder-1 shows a dialog that needs you"
    later = datetime.now(UTC) + timedelta(seconds=5)
    assert needs_item_current(needs_agent_now(PROJECT, "coder-1", now=later), card["id"])


def test_a_scan_that_cannot_read_a_screen_keeps_its_last_cards_and_makes_none_of_its_own() -> None:
    """Review of #259, round 11, 5/13: a question queued behind a sub-agent at work, whose
    screen could not be captured, was "shows a dialog that needs you", pushed while nothing
    was drawn, under the id its real dialog needed later. Unread, the screen decides
    nothing: the scan keeps its last cards, and makes none of its own."""
    from aisquare.core.tmux import TmuxError

    row = _row()
    session = _session(row, state="working", seen=NOW - timedelta(minutes=2))
    fleet = Fleet(
        agents=[_status(row, "working", session)],
        screen=_question_screen(above=(f"● {_TASK.summary}",)),
    )
    fleet.tails[f"/transcripts/{row.label}.jsonl"] = _tail(_TASK, _ASK)
    first = scan_needs_you(_sources(fleet), now=NOW, dismissed=())
    assert [item.reason for item in first] == ["coder-1 shows a dialog that needs you"]

    def unreadable(status: FleetAgentStatus) -> list[str]:
        raise TmuxError("tmux did not answer within 30s: capture-pane")

    blind = replace(_sources(fleet), pane_screen=unreadable)
    later = NOW + timedelta(seconds=3)
    assert scan_needs_you(blind, now=later, dismissed=(), kept=first) == first
    assert scan_needs_you(blind, now=later, dismissed=()) == []
    fleet.screen = _at_work(f"● {_TASK.summary}")
    assert scan_needs_you(_sources(fleet), now=later, dismissed=(), kept=first) == []


def test_one_agents_screen_not_captured_is_that_agents_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #259, round 12, 4/9: a capture that failed was a failed read of the whole
    project's scan. Every agent's cards froze at their last, and every action on every agent
    of the project was a 503, for one pane that closed between the listing and its capture.
    It is that agent's alone: its last card stands and its actions are a 503; the other
    agent's card is derived anew, and its actions go on."""
    from aisquare.core.tmux import TmuxError
    from aisquare.services.remote_server import RequestError

    calls = {
        "coder-1": (_PUSH, _RM),
        "coder-2": (replace(_PUSH, tool_use_id="toolu_push2"), replace(_RM, tool_use_id="x")),
    }
    fleet = Fleet()
    for label, pending in calls.items():
        row = _row(label)
        fleet.agents.append(_status(row, "attention", _session(row, state="attention")))
        fleet.tails[f"/transcripts/{label}.jsonl"] = _tail(*pending)
    screens: dict[str, list[str] | None] = {
        label: _screen_of(pending[0], pending) for label, pending in calls.items()
    }

    def screen(status: FleetAgentStatus) -> list[str]:
        shown = screens[status.agent.label]
        if shown is None:
            raise TmuxError("can't find pane: %7")
        return shown

    def card(pending: PendingTool) -> str:
        return needs_item_id(PROJECT.id, "permission", pending.tool_use_id)

    sources = replace(_sources(fleet), pane_screen=screen)
    first = scan_needs_you(sources, now=NOW, dismissed=())
    two_push = card(calls["coder-2"][0])
    assert {(item.agent, item.id) for item in first} == {
        ("coder-1", card(_PUSH)),
        ("coder-2", two_push),
    }

    screens["coder-1"] = _screen_of(_RM, calls["coder-1"])  # its push answered, its rm asks
    screens["coder-2"] = None
    later = scan_needs_you(sources, now=NOW + timedelta(seconds=3), dismissed=(), kept=first)
    assert {(item.agent, item.id) for item in later} == {
        ("coder-1", card(_RM)),
        ("coder-2", two_push),
    }

    monkeypatch.setattr(remote_needs, "live_needs_sources", lambda: sources)
    assert [item.id for item in needs_agent_now(PROJECT, "coder-1", now=NOW).items] == [card(_RM)]
    with pytest.raises(RequestError) as refused:
        needs_agent_now(PROJECT, "coder-2", now=NOW)
    assert (refused.value.status, refused.value.error) == (503, "unavailable")
    assert "coder-2's screen could not be read: can't find pane" in refused.value.message


@pytest.mark.parametrize("asking", [_ASK, _PLAN], ids=["question", "plan"])
@pytest.mark.parametrize("beside", [_TASK, _tool("toolu_read", "Read", file_path="/a.py")])
def test_a_question_or_plan_beside_a_call_it_cannot_name_has_a_card_before_any_notice(
    asking: PendingTool, beside: PendingTool
) -> None:
    """Review of #259, round 9, 5/8: beside a call with no mark, the screen names no call,
    and with no ``attention`` there was no card at all, where rules 3 and 4 make one with no
    notice. Its dialog drawn, it is rule 8's card, without answers; not while nothing is
    drawn (a question behind a call at work).

    Round 10, 2/9: it was named after the notices before it, and one that landed later (the
    idle notice Claude Code sends after a minute) named the same dialog anew, a second push
    whose first link was a card gone. It is named after the question or the plan, at once
    and after any notice."""
    row = _row()
    session = _session(row, state="working", seen=NOW - timedelta(minutes=2))
    status, tail = _status(row, "working", session), _tail(beside, asking)
    drawn = _question_screen(above=(f"● {beside.summary}",))
    card = _one(_classify(status, tail, pane_screen=lambda: drawn))
    assert (card.kind, card.answers) == ("permission", ())
    assert card.reason == "coder-1 shows a dialog that needs you"
    shows = remote_needs._needs_prompt_region(drawn)
    assert card.id == needs_item_id(
        PROJECT.id, "permission", f"dialog:{asking.tool_use_id}:{shows}"
    )
    # Round 11, 5/13: a screen nobody could read is unknown, and makes no card: it was one,
    # pushed while nothing was drawn, under the id the dialog drawn later then needed.
    unread = _classify(status, tail, pane_screen=lambda: None)
    assert unread == [], "a screen nobody could read is not a dialog drawn"
    just = _classify(
        status, tail, pane_screen=lambda: drawn, pane_output=lambda: NOW - timedelta(seconds=3)
    )
    assert [item.id for item in just] == [card.id], "drawn a moment ago: no notice to wait for"
    for late in (timedelta(seconds=30), timedelta(minutes=1, seconds=2)):
        noticed = _session(row, state="attention", seen=NOW - timedelta(minutes=2) + late)
        said = "Claude is waiting for your input"
        events = [_event(9, "attention", said, session=noticed, at=noticed.last_seen_at)]
        landed = _one(
            _classify(_status(row, "attention", noticed), tail, events, pane_screen=lambda: drawn)
        )
        assert landed.id == card.id, f"a notice {late} later names the same dialog: one push"
    at_work = _at_work(f"● {beside.summary}")
    behind = _classify(status, tail, pane_screen=lambda: at_work, pane_output=lambda: None)
    assert behind == [], "nothing is drawn yet"


def test_a_description_that_quotes_the_call_beside_it_names_neither() -> None:
    """Both marks above the options, the screen names neither call, and the card is the plain
    dialog's: the key pad beside the live pane answers what it shows."""
    row = _row()
    session = _session(row, state="attention")
    events = [_event(4, "attention", "Claude needs your permission to use Bash", session=session)]
    screen = _bash_screen("rm -rf dist", "Clear it, then git push --force origin main")
    item = _one(
        _classify(
            _status(row, "attention", session),
            _tail(_PUSH, _RM),
            events,
            pane_screen=lambda: screen,
        )
    )
    assert item.answers == () and item.reason == "coder-1 shows a dialog that needs you"


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


def _turned_down(path: Path, said: str) -> TranscriptTail:
    """coder-1's transcript: a Bash call it asked to run, turned down with ``said``, and its
    next message begun, thinking only. Read as the scan reads it."""

    def record(kind: str, uuid: str, seconds: int, content: object, **message: str) -> str:
        at = (NOW - timedelta(seconds=seconds)).isoformat()
        body = {"role": kind, "content": content, **message}
        return json.dumps({"type": kind, "uuid": uuid, "timestamp": at, "message": body}) + "\n"

    call = {"type": "tool_use", "id": "toolu_rm", "name": "Bash", "input": {"command": "rm -rf x"}}
    result = {"type": "tool_result", "tool_use_id": "toolu_rm", "content": said, "is_error": True}
    thinking = {"type": "thinking", "thinking": "Tests, then."}
    path.write_text(
        record("user", "u1", 300, "go")
        + record("assistant", "a1", 240, [call], id="m1")
        + record("user", "r1", 60, [result])
        + record("assistant", "a2", 50, [thinking], id="m2"),
        encoding="utf-8",
    )
    tail = read_transcript_tail(path)
    assert tail is not None
    return tail


def test_a_prompt_turned_down_with_words_for_the_agent_is_no_interruption(tmp_path: Path) -> None:
    """Turned down with "No, and tell Claude what to do differently", Claude Code does not stop
    the turn, and the agent works on the words. Its card said it "was interrupted and waits
    for you" until its next message wrote more than thinking, and the card's Tell offered
    Interrupt & tell, whose Esc would cut short the very work the words had started. Turned
    down and left at that, it is the interruption it always was."""
    row = _row()
    status = _status(
        row, "working", _session(row, state="attention", seen=NOW - timedelta(minutes=3))
    )
    stop = (
        "The user doesn't want to proceed with this tool use. The tool use was rejected (eg. if "
        "it was a file edit, the new_string was NOT written to the file). STOP what you are "
        "doing and wait for the user to tell you how to proceed."
    )
    words = stop.split(" STOP ")[0] + " To tell you how to proceed, the user said:\nrun the tests"
    assert _classify(status, _turned_down(tmp_path / "words.jsonl", words)) == []
    item = _one(_classify(status, _turned_down(tmp_path / "stop.jsonl", stop)))
    assert (item.kind, item.since) == ("interrupted", NOW - timedelta(seconds=60))


def test_rule_7_the_session_paused_dialog_reads_as_limited() -> None:
    row = _row()
    session = _session(row, state="attention")
    text = "Session paused — choose: continue on usage credits or switch models"
    events = [_event(9, "attention", text, session=session, at=session.last_seen_at)]
    item = _one(_classify(_status(row, "attention", session), None, events))
    assert item.kind == "limited"
    assert item.id == needs_item_id(PROJECT.id, "limited", "attention:9")
    assert item.reason == "coder-1 hit its usage limit (Claude Code is asking what to do)"
    assert item.detail == {"text": text}
    assert item.answers == ()


@pytest.mark.parametrize(
    ("accounts", "manager_live"),
    [
        (AccountsSettings(), False),
        (AccountsSettings(), True),
        (AccountsSettings(on_limit="switch"), False),
        (AccountsSettings(on_limit="switch"), True),
    ],
)
def test_the_session_paused_dialog_is_pushed_at_once_since_nobody_else_acts_on_it(
    accounts: AccountsSettings, manager_live: bool
) -> None:
    """The dialog's card waited 90 s with ``on_limit = "switch"`` or a live manager, for the
    hand-over or the manager to move the agent first. The dialog is a notification: the
    hand-over starts only on a turn that failed on its limit (``StopFailure``), and an
    ``attention`` event wakes no manager, so the agent waited 90 s on a human nobody told
    (review of #243, sweep 5)."""
    row = _row()
    seen = NOW - timedelta(minutes=1)
    session = _session(row, state="attention", seen=seen)
    text = "Session paused — choose: continue on usage credits or switch models"
    events = [_event(9, "attention", text, session=session, at=seen)]
    status = _status(row, "attention", session)
    item = _one(_classify(status, None, events, manager_live=manager_live, accounts=accounts))
    assert (item.kind, item.since, item.push_after) == ("limited", seen, seen)


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


@pytest.mark.parametrize(
    "said",
    ["Claude Code needs your input", "An MCP server needs your input", "tester needs your input"],
)
def test_an_elicitation_asked_mid_call_is_a_dialog_not_the_calls_permission(said: str) -> None:
    """An MCP server asks for input while its tool call is still pending, so the tool use
    has no result and rule 5 took it first: "waits for a permission answer to use
    mcp__deploy__release", its notice's words dropped, and quick answers whose "1" typed a
    digit into the form's field, the toast saying it was sent and the agent still blocked
    (SPEC §4.2 gives a form no buttons; sweep 4 of #243). Its notice says it asks for
    input, as a permission prompt's does not: the dialog form, as rule 8 makes it."""
    row = _row()
    seen = NOW - timedelta(seconds=30)
    session = _session(row, state="attention", seen=seen)
    call = _tool("toolu_mcp", "mcp__deploy__release", at=seen - timedelta(seconds=8), env="prod")
    tail = _tail(call, at=seen - timedelta(seconds=8))
    status = _status(row, "attention", session)
    events = [_event(4, "attention", said, session=session, at=seen)]
    item = _one(_classify(status, tail, events))
    assert item.kind == "permission"
    assert item.id == needs_item_id(PROJECT.id, "permission", f"attention:4:{seen.isoformat()}")
    assert item.reason == "coder-1 shows a dialog that needs you"
    assert (item.detail, item.excerpt, item.answers) == ({"text": said}, said, ())
    permission = "Claude needs your permission to use mcp__deploy__release"
    prompt = _one(
        _classify(status, tail, [_event(4, "attention", permission, session=session, at=seen)])
    )
    assert prompt.reason == "coder-1 waits for a permission answer to use mcp__deploy__release"
    assert [answer.label for answer in prompt.answers] == ["1", "2", "No"]
    assert [answer.label for answer in _one(_classify(status, tail)).answers] == ["1", "2", "No"]


def test_a_form_asked_after_its_calls_own_prompt_is_a_dialog_not_that_prompt_again() -> None:
    """An MCP tool not allowed yet asks its permission first, and that notice is the turn's
    first, the one the board keeps in words. The human grants it, the call runs, and its
    server asks for a form. A grant writes nothing to the transcript and a later notice no
    event, only ``last_seen_at``: the card was the call's permission again, under the
    answered card's id, so nothing was pushed, and its "1" typed a digit into the form
    (verifier of sweep 4's item 18 on #243). A notice since the turn's first with nothing
    written between is a dialog the board has no words for: rule 8's form, its own id."""
    row = _row()
    asked = NOW - timedelta(seconds=40)
    called = asked - timedelta(seconds=6)
    first = _session(row, state="attention", seen=asked)
    permission = "Claude needs your permission to use mcp__deploy__release"
    events = [_event(4, "attention", permission, session=first, at=asked)]
    call = _tool("toolu_mcp", "mcp__deploy__release", at=called, env="prod")
    tail = _tail(call, at=called)
    prompt = _one(_classify(_status(row, "attention", first), tail, events))
    assert prompt.id == needs_item_id(PROJECT.id, "permission", "toolu_mcp")
    assert [answer.label for answer in prompt.answers] == ["1", "2", "No"]
    seen = NOW - timedelta(seconds=5)
    later = _status(row, "attention", _session(row, state="attention", seen=seen))
    form = _one(_classify(later, tail, events))
    assert form.id == needs_item_id(PROJECT.id, "permission", f"attention:4:{seen.isoformat()}")
    assert form.reason == "coder-1 shows a dialog that needs you"
    assert (form.excerpt, form.detail, form.answers) == ("", {"text": ""}, ())
    assert form.push_after == form.since == seen
    beside = _tool("toolu_beside", "mcp__deploy__status", at=called)
    both = _one(_classify(later, _tail(call, beside, at=called), events))
    assert (both.id, both.answers) == (form.id, ()), "not the granted call's card again"


def test_the_next_prompt_of_a_turn_keeps_its_answers_after_a_notice_since_the_first() -> None:
    """``last_seen_at`` past the turn's first notice is any later notice, the next prompt's
    too. A prompt's tool use is written before it, so the agent wrote after the event: the
    call it asks about is the pending one, with its digits."""
    row = _row()
    asked = NOW - timedelta(minutes=2)
    first = _session(row, state="attention", seen=asked)
    bash = "Claude needs your permission to use Bash"
    events = [_event(4, "attention", bash, session=first, at=asked)]
    seen = NOW - timedelta(seconds=5)
    later = _status(row, "attention", _session(row, state="attention", seen=seen))
    called = seen - timedelta(seconds=6)
    call = _tool("toolu_next", "mcp__deploy__release", at=called)
    item = _one(_classify(later, _tail(call, at=called), events))
    assert item.id == needs_item_id(PROJECT.id, "permission", "toolu_next")
    assert [answer.label for answer in item.answers] == ["1", "2", "No"]


def test_a_sub_agents_prompt_after_its_form_is_a_prompt_not_that_form_again() -> None:
    """A sub-agent's records are not its agent's, so every one of its prompts comes with
    nothing written here. Its form's notice, the turn's first, read as every prompt after
    it: dialogs without digits. Only while it is the newest notice is it the form's."""
    seen = NOW - timedelta(seconds=30)
    status, tail = _in_a_sub_agent(seen)
    assert status.session is not None
    said = "Claude Code needs your input"
    form = [_event(4, "attention", said, session=status.session, at=seen)]
    assert _one(_classify(status, tail, form)).answers == ()
    asked = _one(_classify(*_in_a_sub_agent(NOW - timedelta(seconds=5)), form))
    assert asked.reason == "coder-1 waits for a permission answer (in a sub-agent)"
    assert [answer.label for answer in asked.answers] == ["1", "2", "No"]


def test_the_hooks_own_writes_tell_a_form_from_the_prompt_before_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Over the real store, by ``team.hook_notification``: the first notice of a turn leaves
    ``last_seen_at`` before its event, which is what names the call's prompt; the form's
    notice after it writes no event and moves ``last_seen_at`` past it."""
    from aisquare.services import team as team_service

    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("AISQUARE_TEAM", "1")
    project = team_service.activate(work)
    called = datetime.now(UTC)
    row = _row(created=called - timedelta(minutes=1))
    session_id = str(row.session_id)
    team_service.hook_session_start(session_id, work, "startup")
    tail = _tail(_tool("toolu_mcp", "mcp__deploy__release", at=called, env="prod"), at=called)

    def card() -> NeedsItem:
        with store_session() as store:
            session = store.get_session(session_id)
            events = store.recent_events(project.id, limit=20)
        status = _status(row, "attention", session)
        now = datetime.now(UTC)
        return _one(needs_from_agent(status, tail, project=project, events=events, now=now))

    said = "Claude needs your permission to use mcp__deploy__release"
    team_service.hook_notification(session_id, work, said, notification_type="permission_prompt")
    prompt = card()
    assert prompt.id == needs_item_id(project.id, "permission", "toolu_mcp")
    assert [answer.label for answer in prompt.answers] == ["1", "2", "No"]
    team_service.hook_notification(
        session_id, work, "Claude Code needs your input", notification_type="elicitation_dialog"
    )
    form = card()
    assert (form.reason, form.answers) == ("coder-1 shows a dialog that needs you", ())
    assert form.id != prompt.id


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
    named = _status(row, "attention", _session(row, state="attention", seen=first))
    assert _one(_classify(named, still, limit)).kind == "limited", "the dialog it named"
    assert _one(_classify(named, still, bash)).excerpt == bash[0].text
    after = _one(_classify(status, still, limit))
    assert (after.kind, after.detail) == ("permission", {"text": ""}), "a notice since: another"


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


def test_a_question_a_local_command_follows_is_still_asked_under_its_own_id() -> None:
    """The class of sweep 5 of #243's interruption: a ``/model`` or a ``!`` command typed at
    the agent's prompt runs no turn, and its records became the newest. The agent's closing
    question was no longer its newest record, and the card went, the question unanswered.
    It is the same card, its id and time the question's."""
    row = _row()
    text = "Two ways to do it.\n\nWhich approach?"
    asked_at = NOW - timedelta(minutes=2)
    said = _tail(newest="assistant_text", text=text, key="txt-1", at=asked_at)
    behind = replace(
        _tail(newest="own", text=text, key="own-1", at=NOW - timedelta(seconds=30)),
        under_own="assistant_text",
        under_own_at=asked_at,
        under_own_key="txt-1",
    )
    waiting = _status(row, "waiting", _session(row))
    first, again = (_one(_classify(waiting, tail)) for tail in (said, behind))
    assert (again.kind, again.id, again.since) == (first.kind, first.id, first.since)
    assert first.kind == "asked" and first.since == asked_at
    turned = replace(behind, under_own="user_prompt")
    assert _classify(waiting, turned) == [], "a prompt since: the question was answered"


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


LOGIN_EXPIRED = "authentication_failed: Login expired · Please run /login"


def _turn_failed(
    *,
    row: FleetAgent | None = None,
    state: str = "waiting",
    marked: str = "waiting",
    seen: datetime = NOW - timedelta(minutes=2),
    at: datetime = NOW - timedelta(minutes=2),
    text: str = LOGIN_EXPIRED,
) -> tuple[FleetAgentStatus, TranscriptTail, list[TeamEvent]]:
    """coder-1 after ``team.hook_stop_failure``: its session marked ``waiting`` at ``seen``,
    the ``turn_failed`` event written at ``at``, its transcript ending on Claude Code's own
    ``API Error`` text."""
    row = row or _row()
    session = _session(row, state=marked, seen=seen)
    tail = _tail(newest="assistant_text", at=at, text="API Error: 401 · Please run /login")
    events = [_event(12, "turn_failed", text, session=session, at=at)]
    return _status(row, state, session), tail, events


def test_rule_10_a_turn_that_ended_on_an_api_error_is_failed() -> None:
    """A login that expired, credit run out, the API overloaded past Claude Code's retries:
    ``StopFailure`` marks the session ``waiting``, as a Stop would, and writes
    ``turn_failed``. The row read idle, no manager was nudged, nothing retried, and the feed
    stayed empty while the agent sat at its prompt holding its claim."""
    item = _one(_classify(*_turn_failed()))
    assert (item.kind, item.agent) == ("failed", "coder-1")
    assert item.id == needs_item_id(PROJECT.id, "failed", "12")
    assert item.reason == "coder-1's turn failed (authentication_failed)"
    assert (item.excerpt, item.detail) == (LOGIN_EXPIRED, {"text": LOGIN_EXPIRED})
    assert item.since == item.push_after == NOW - timedelta(minutes=2)
    assert item.actions == ("tell", "switch", "open", "dismiss")


@pytest.mark.parametrize(
    "facts",
    [
        pytest.param({"seen": NOW - timedelta(seconds=30)}, id="a hook fired since"),
        pytest.param({"state": "working"}, id="at work again"),
        pytest.param({"marked": "switching"}, id="a hand-over's own"),
        pytest.param({"row": _row(created=NOW - timedelta(minutes=1))}, id="the process before"),
    ],
)
def test_a_failed_turn_needs_nobody_once_the_agent_moved_on(facts: dict[str, Any]) -> None:
    assert _classify(*_turn_failed(**facts)) == []


def test_a_failed_turns_reason_names_an_error_only_a_lock_screen_can_show() -> None:
    item = _one(_classify(*_turn_failed(text="\x1b[31mred alert\x1b[0m: the API said no")))
    assert item.reason == "coder-1's turn failed"


def test_a_failed_turn_is_read_from_the_boards_day() -> None:
    status, tail, events = _turn_failed()
    fleet = Fleet(agents=[status], events=events, tails={"/transcripts/coder-1.jsonl": tail})
    assert [(item.kind, item.agent) for item in _scan(fleet)] == [("failed", "coder-1")]
    assert fleet.windows == 0, "the day is read every scan anyway: no window for it"


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
    """Servers whose tmux does not answer: a socket's name, or the path a row's spawn
    recorded (``socket_at``) where it is asked there."""
    probed: list[str] = field(default_factory=list)
    """Every server the scan asked tmux about, named as in ``silent``, once per question."""
    asked_events: list[tuple[str, str, datetime]] = field(default_factory=list)
    """Every session the scan asked the store for its newest event of a kind since a time."""
    output_at: datetime | None = None
    """When every pane last printed, as tmux tells it; ``None``: it would not say."""
    screen: list[str] | None = None
    """What every pane shows, as tmux captures it; ``None``: it would not say."""
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
                event.kind in remote_needs._NEEDS_DAY_KINDS
                or (event.session_id is None and event.kind in remote_needs._NEEDS_REPLIED)
            )
        ]

    def board_posts(pid: str, since: datetime, sessions: Collection[str]) -> list[TeamEvent]:
        """The store's ``team_events_since`` over the kinds and sessions the live source asks
        for."""
        return [
            event
            for event in fleet.events
            if event.created_at >= since
            and (
                event.kind in remote_needs._NEEDS_FLEET_KINDS
                or (event.session_id in sessions and event.kind in remote_needs._NEEDS_ASKED)
                or (event.session_id is None and event.kind in remote_needs._NEEDS_REPLIED)
            )
        ]

    def tmux_answers(socket: str, path: Path | None) -> bool:
        server = str(path or socket)
        fleet.probed.append(server)
        return server not in fleet.silent

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
        board_posts=board_posts,
        board_sessions=lambda pid, ids: [
            session for session in fleet.sessions if session.id in ids
        ],
        task_status=lambda ref: fleet.tasks.get(ref),
        transcript_tail=lambda path: fleet.tails.get(path),
        accounts=lambda: accounts or AccountsSettings(),
        has_live_agents=lambda pid: any(status.agent.ended_at is None for status in fleet.agents),
        tmux_answers=tmux_answers,
        session_event=session_event,
        pane_output=lambda agent: fleet.output_at,
        pane_screen=lambda agent: fleet.screen,
    )


def _scan(
    fleet: Fleet,
    *,
    now: datetime = NOW,
    dismissed: tuple[str, ...] = (),
    first_seen: dict[str, datetime] | None = None,
    accounts: AccountsSettings | None = None,
) -> list[NeedsItem]:
    return scan_needs_you(
        _sources(fleet, accounts=accounts), now=now, dismissed=dismissed, first_seen=first_seen
    )


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


def test_a_manager_stopped_with_no_status_is_down_whatever_its_crew_does() -> None:
    """A kill or a pane gone leaves no exit status. Reported only while an agent of its crew
    worked, and not after its result, the card went with the coder that reported to it and
    went idle, before its push: the result stayed on the board, read by nobody (review of
    #243, round 25, 2/4). Only a clean exit is taken to be done."""
    manager = _row("manager", role="manager", ended=NOW - timedelta(minutes=5))
    managing = _session(manager, ended=NOW - timedelta(minutes=5))
    coder = _row()
    fleet = Fleet(ended=[manager], agents=[_status(coder, "working", _session(coder))])
    down = _one(_scan(fleet))
    assert (down.kind, down.reason) == ("manager_down", "the manager stopped")
    fleet.agents = [_status(coder, "waiting", _session(coder, state="waiting"))]
    assert _scan(fleet) == [down], "its crew idle"
    fleet.agents = []
    assert _scan(fleet) == [down], "no crew"
    fleet.sessions = [managing]
    fleet.events = [_event(5, "result", "Shipped.", session=managing)]
    assert [item.kind for item in _scan(fleet)] == ["manager_down", "board_result"]
    clean = _row("manager", role="manager", ended=NOW - timedelta(minutes=5), exit_status=0)
    fleet.ended = [clean]
    assert [item.kind for item in _scan(fleet)] == ["board_result"], "a clean exit: done"


def test_a_manager_whose_hand_over_never_started_its_replacement_is_down() -> None:
    """A switch or a restart stops the manager with its own ``/exit``, status 0, and then
    starts the replacement. When that start fails nothing replaces it, and "manager exited
    (0)" read as a manager whose job was done: no card while the crew worked on unmanaged.
    The exit announced for a hand-over that failed says so. Nobody chose that stop: it is
    down with its crew all waiting too, in a coder's words (review of #243, round 23, 3/7
    and 6/7)."""
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
        "the manager stopped, and its replacement did not start",
    )
    assert down.detail == {"exit_status": 0, "task_id": None}
    fleet.events = [_event(7, "agent_exited", "manager exited (0)", session=managing)]
    assert _scan(fleet) == [], "a stop that meant it: its job was done"
    fleet.events = [failed]
    fleet.agents = [_status(coder, "waiting", _session(coder, state="waiting"))]
    assert _one(_scan(fleet)).reason == down.reason, "nobody chose it, whatever its crew does"


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
    killed = coder.model_copy(update={"exit_status": None})
    fleet.ended = [killed]
    fleet.events = [
        _event(7, "agent_exited", f"coder-1 exited (?): {HANDOVER_FAILED}", session=coding)
    ]
    assert _one(_scan(fleet)).reason == item.reason, "its /exit not in time, so it was killed"
    assert _scan(_with_live_manager(fleet)) == [], "a live manager was nudged on it"


def test_an_exit_is_its_own_rows_not_another_sessions_announced_later() -> None:
    """``_needs_exit_of`` reads the exit announced under the row's own session: the manager
    stopped cleanly, and a coder's hand-over failed after, is no manager down (its newest
    exit on the board is the coder's), and the coder's card is the coder's alone. Read off
    any session, the manager's clean stop read as a failed hand-over, and the reverse order
    hid the coder's real card under the manager's newer exit."""
    from aisquare.services.fleet import HANDOVER_FAILED

    stopped, failed = NOW - timedelta(minutes=20), NOW - timedelta(minutes=10)
    manager = _row("manager", role="manager", ended=stopped, exit_status=0)
    coder = _row("coder-1", ended=failed, exit_status=0)
    working = _row("coder-2")
    fleet = Fleet(
        ended=[manager, coder],
        agents=[_status(working, "working", _session(working))],
        sessions=[_session(manager, ended=stopped), _session(coder, ended=failed)],
    )
    managing, coding = fleet.sessions
    fleet.events = [
        _event(7, "agent_exited", "manager exited (0)", session=managing, at=stopped),
        _event(
            9, "agent_exited", f"coder-1 exited (0): {HANDOVER_FAILED}", session=coding, at=failed
        ),
    ]
    assert [(item.kind, item.agent) for item in _scan(fleet)] == [("crashed", "coder-1")]
    fleet.events = [
        _event(
            7, "agent_exited", f"coder-1 exited (0): {HANDOVER_FAILED}", session=coding, at=stopped
        ),
        _event(9, "agent_exited", "manager exited (0)", session=managing, at=failed),
    ]
    fleet.ended = [
        manager.model_copy(update={"ended_at": failed}),
        coder.model_copy(update={"ended_at": stopped}),
    ]
    assert [(item.kind, item.agent) for item in _scan(fleet)] == [("crashed", "coder-1")]


def test_a_clean_exit_is_no_failed_hand_over_another_agent_announced_later() -> None:
    from aisquare.services.fleet import HANDOVER_FAILED

    done, failed = NOW - timedelta(minutes=20), NOW - timedelta(minutes=10)
    one = _row("coder-1", ended=done, exit_status=0)
    two = _row("coder-2", ended=failed, exit_status=0)
    fleet = Fleet(
        ended=[one, two],
        events=[
            _event(7, "agent_exited", "coder-1 exited (0)", session=_session(one), at=done),
            _event(
                9,
                "agent_exited",
                f"coder-2 exited (0): {HANDOVER_FAILED}",
                session=_session(two),
                at=failed,
            ),
        ],
    )
    assert [(item.kind, item.agent) for item in _scan(fleet)] == [("crashed", "coder-2")]


def test_a_failed_hand_over_is_not_the_exit_of_a_later_row_of_its_session() -> None:
    """A restart resumes the session, so a later row of the label has the session id of the
    one whose hand-over failed. Reaped as lost, that row has no exit status and announces
    nothing: the earlier row's failed exit, announced before it was created, is not its."""
    from aisquare.services.fleet import HANDOVER_FAILED

    first = _row(
        "coder-1",
        created=NOW - timedelta(minutes=55),
        ended=NOW - timedelta(minutes=50),
        exit_status=0,
    )
    again = first.model_copy(
        update={
            "id": "agt_again",
            "created_at": NOW - timedelta(minutes=40),
            "ended_at": NOW - timedelta(minutes=10),
            "exit_status": None,
        }
    )
    failed = _event(
        7,
        "agent_exited",
        f"coder-1 exited (0): {HANDOVER_FAILED}",
        session=_session(first),
        at=NOW - timedelta(minutes=50),
    )
    assert _scan(Fleet(ended=[first, again], events=[failed])) == []


def test_another_sessions_events_say_nothing_of_this_agent() -> None:
    """``needs_from_agent`` reads its own session's events alone: another agent's usage-limit
    notice or failed turn, handed in with them, names nothing of this one's."""
    row = _row()
    other = _session(_row("coder-2"), state="attention")
    paused = "Session paused — choose: continue on usage credits or switch models"
    events = [
        _event(9, "attention", paused, session=other),
        _event(10, "turn_failed", LOGIN_EXPIRED, session=other, at=NOW - timedelta(minutes=1)),
    ]
    at_a_dialog = _status(row, "attention", _session(row, state="attention"))
    assert [(item.kind, item.reason) for item in _classify(at_a_dialog, None, events)] == [
        ("permission", "coder-1 shows a dialog that needs you")
    ]
    waiting = _status(row, "waiting", _session(row, state="waiting"))
    assert _classify(waiting, None, events) == []


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
    # Waiting on a limit, so no hand-over is on it either.
    items = _scan(fleet, accounts=AccountsSettings(on_limit="wait"))
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


def test_a_manager_at_the_usage_limit_dialog_does_not_wait_for_itself() -> None:
    """At the usage-limit dialog a manager reads attention, so it counted as the live manager
    its own limit's push waited 90 s for: itself. A coder's dialog waited 90 s for it too,
    but no manager hears of a dialog (an ``attention`` event wakes none), and no hand-over
    starts on one: both go at once (review of #243, sweep 5)."""
    paused = "Session paused — choose: continue on usage credits or switch models"
    at = NOW - timedelta(minutes=1)
    manager, coder = _row("manager", role="manager"), _row()
    managing = _session(manager, state="attention", seen=at)
    coding = _session(coder, state="attention", seen=at)
    fleet = Fleet(
        agents=[_status(manager, "attention", managing), _status(coder, "attention", coding)],
        sessions=[managing],
        events=[
            _event(4, "attention", paused, session=managing, at=at),
            _event(5, "attention", paused, session=coding, at=at),
        ],
    )
    pushes = {item.agent: item.push_after for item in _scan(fleet) if item.kind == "limited"}
    assert pushes == {"manager": at, "coder-1": at}


def test_only_the_manager_the_fleets_nudge_wakes_keeps_work_off_the_phone() -> None:
    """``fleet.nudge_manager`` wakes the project's live ``manager`` row and nobody else, for a
    coder's board write and for an exit alike. A manager session no row holds and a
    numbered seat were counted live, so a coder's question to the manager and a crash were
    no card while nothing woke anyone for them (review of #243, round 21, 5/8). A row that
    reads gone is no manager either, whatever its session says."""

    def seat(label: str, state: str = "working") -> FleetAgentStatus:
        row = _row(label, role="manager")
        return _status(row, state, _session(row))

    assert _scan(_crew_and_manager(seat("manager"))) == []
    unheld = _crew_and_manager(seat("manager"))
    del unheld.agents[0]  # its session on the board, and no row of it
    for fleet, why in (
        (_crew_and_manager(seat("manager-2")), "a numbered seat"),
        (unheld, "a manager session no row holds"),
        (_crew_and_manager(seat("manager", "lost")), "a manager row whose pane is gone"),
    ):
        kinds = [item.kind for item in _scan(fleet) if item.agent != "manager"]
        assert kinds == ["board_question", "crashed"], why


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


def test_two_servers_on_one_socket_name_are_asked_apart() -> None:
    """A row records where its server listened (``socket_at``, ``tmux -S``, schema v25):
    two rows on the socket name ``asq``, spawned under two ``TMUX_TMPDIR``s, are on two
    servers. Grouped by the name alone, one row's ``unknown`` made the other's server silent
    too, and its permission prompt read ``unknown`` under a ``fleet_down``. Each server is
    asked where its rows say it listens, and the one that answers keeps its agent's items."""
    elsewhere = Path("/run/elsewhere/tmux-1000/asq")
    one = _row("coder-1")
    two = _row("coder-2").model_copy(update={"socket_path": elsewhere})
    attention = _session(two, state="attention")
    fleet = Fleet(
        agents=[
            _status(one, "unknown"),
            _status(two, "attention", attention, socket_at=elsewhere),
        ]
    )
    assert [(item.kind, item.agent) for item in _scan(fleet)] == [("permission", "coder-2")]
    assert fleet.probed == [str(elsewhere)], "asked at the path coder-2's spawn recorded"


def test_a_row_held_as_another_servers_does_not_silence_the_server_that_answered() -> None:
    """Review of #259, round 1: after a `kill-server` and a respawn, the old row recorded
    another server's start and is held (`fleet.ANOTHER_SERVER`), `unknown` once its board
    goes stale, on the same socket as the new server's healthy rows. Read by its state
    word it said that server did not answer: every row there turned `unknown / tmux
    unavailable`, coder-2's permission prompt left the phone, and `fleet_down` said tmux
    was not answering. A held row's server answered; on its own it is no outage either."""
    old, new = _row("coder-1"), _row("coder-2")
    stale = _session(old, state="working", seen=NOW - timedelta(hours=1))
    held = fleet_service._status(
        old, stale, {old.id: fleet_service.HELD}, None, NOW, FleetSettings()
    )
    assert (held.state, held.detail, held.held) == ("unknown", fleet_service.ANOTHER_SERVER, True)
    fleet = Fleet(agents=[held, _status(new, "attention", _session(new, state="attention"))])

    assert [(item.kind, item.agent) for item in _scan(fleet)] == [
        ("permission", "coder-2"),
        ("lost", "coder-1"),  # its own card, out of reach (round 2)
    ]
    assert fleet.probed == [], "the held row says its server answered"
    fleet.agents = [held]
    said = [item.kind for item in _scan(fleet)]
    assert said == ["lost"], "no `tmux is not answering` for a server that answered"
    assert fleet.probed == []
    # The control: the same row unknown because its server could not be asked.
    unasked = fleet_service._status(old, stale, None, None, NOW, FleetSettings())
    fleet.agents = [unasked, _status(new, "attention", _session(new, state="attention"))]
    assert [item.kind for item in _scan(fleet)] == ["fleet_down"]


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
            fleet_service._status(row, session, None, None, NOW, FleetSettings())
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


def test_a_row_dated_after_the_scan_does_not_start_tmux_down_over_every_scan() -> None:
    """A sighting from before a live row was made is another outage's. A row dated ahead of
    the clock (the clock set back, a VM restored from a snapshot) is after every sighting,
    and each scan started the outage again: a new id every 3 s, no dismissal held and no
    push streak built (merge of round 5 of #243)."""
    ahead = _row("coder-1", created=NOW + timedelta(hours=1))
    fleet = Fleet(agents=[_status(ahead, "unknown")])
    memory: dict[str, datetime] = {}
    first = _one(_scan(fleet, first_seen=memory))
    for later in (3, 6, 9):
        again = _one(_scan(fleet, now=NOW + timedelta(seconds=later), first_seen=memory))
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


def test_a_lost_pane_is_dated_from_the_first_scan_that_saw_it() -> None:
    """It flashes between a restart's kill and the row's end: the push waits from then."""
    row = _row()
    fleet = Fleet(agents=[_status(row, "lost", _session(row))])
    memory: dict[str, datetime] = {}
    first = _one(_scan(fleet, first_seen=memory))
    later = _one(_scan(fleet, now=NOW + timedelta(seconds=30), first_seen=memory))
    assert later.id == first.id
    assert (later.since, later.push_after) == (NOW, NOW + timedelta(seconds=60))


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
    manager_live: bool = False,
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
        extra="shown too",
        timeout=120,
        query=3,
        pattern=float("nan"),
    )
    tool = _one(_classify(attention, _tail(big))).detail
    assert _size(tool) <= 4_096
    shown = tool["input"]
    assert isinstance(shown, dict)
    assert shown["extra"] == "shown too" and shown["timeout"] == 120, "the rest of the call"
    assert shown["query"] == 3 and shown["pattern"] == "NaN", "a NaN as text a browser reads"
    texts = [shown[key] for key in ("command", "old_string", "new_string", "content")]
    assert all(isinstance(value, str) and value.endswith("…") for value in texts)
    wide: dict[str, Any] = {f"{n:02}" + "k" * 62: "v" * 3_000 for n in range(30)}
    fields = _one(_classify(attention, _tail(_tool("toolu_w", "mcp__wide__call", **wide)))).detail
    assert _size(fields) <= 4_096, "twenty names as long as a card holds, each value cut"
    cut, given = fields["cut"], fields["input"]
    assert isinstance(cut, dict) and isinstance(given, dict)
    assert (len(given), fields["omitted"], len(cut)) == (20, 10, 20)
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
    assert question.detail["cut"] == {"questions": 4 * (2 + 1_000) + 16 * (2 + 3_000)}, (
        "options cut to fit beside the digits that pick them, and the card says so"
    )
    assert question.excerpt.endswith("(+3 more)") or len(question.excerpt) == 280
    plan = _one(_classify(asking, _tail(_tool("toolu_p", "ExitPlanMode", plan="p" * 40_000))))
    assert _size(plan.detail) <= 16_384 and str(plan.detail["plan"]).endswith("…")
    assert plan.detail["cut"] == {"plan": 40_000}, "the card says the plan goes on"
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


def test_the_guide_gives_quick_answers_only_to_the_cards_that_carry_them() -> None:
    """docs/remote.md gave ``1``, ``2`` and No to every permission and a button an option to
    every single question: one of Claude Code's own dialogs carries none, nor does a question
    with several answers to pick, or more than nine options. Each is the key pad's."""
    guide = Path(__file__).resolve().parents[1] / "docs" / "remote.md"
    prose = " ".join(guide.read_text(encoding="utf-8").split())
    assert "`1`, `2` and No for a tool's permission" in prose
    assert "for a single question with one answer to pick from at most nine" in prose
    assert (
        "(one of Claude Code's own dialogs, a form an MCP server asks you to fill in, a"
        " question of several answers)"
    ) in prose
    assert (
        "When the agent already went on past an earlier prompt in the same turn, the form"
        " reads as its tool's permission card"
    ) in prose
    row = _row()
    dialog = _one(_classify(_status(row, "attention", _session(row, state="attention")), None))
    assert (dialog.kind, dialog.answers) == ("permission", ())
    tool = _tail(_tool("toolu_b", "Bash", command="make check"))
    allow = _one(_classify(_status(row, "attention", _session(row, state="attention")), tool))
    assert [answer.label for answer in allow.answers] == ["1", "2", "No"]
    several = [{**QUESTION["questions"][0], "multiSelect": True}]
    asking = _tail(_tool("toolu_q", "AskUserQuestion", questions=several))
    assert _one(_classify(_status(row, "working", _session(row)), asking)).answers == ()


# --- when a push may go out (C decides; D sends) ------------------------------------------


def _limited(resets: datetime | None) -> FleetAgentStatus:
    row = _row()
    return _status(row, "limited", _session(row, state="limited", resets=resets), "usage limit")


@pytest.mark.parametrize(
    ("resets", "accounts", "manager_live", "delay"),
    [
        (NOW + timedelta(minutes=10), AccountsSettings(), False, None),
        (NOW - timedelta(minutes=2), AccountsSettings(), False, None),
        (
            NOW + timedelta(minutes=10),
            AccountsSettings(on_limit="wait", wait_if_reset_within_minutes=5),
            False,
            0,
        ),
        (NOW + timedelta(hours=3), AccountsSettings(on_limit="wait"), False, 0),
        (None, AccountsSettings(on_limit="wait"), False, 0),
        (NOW + timedelta(hours=3), AccountsSettings(), False, 90),  # on_limit: switch
        (NOW + timedelta(hours=3), AccountsSettings(on_limit="wait"), True, 90),
    ],
)
def test_a_limit_pushes_never_near_its_reset_and_later_when_someone_is_on_it(
    resets: datetime | None, accounts: AccountsSettings, manager_live: bool, delay: int | None
) -> None:
    item = _one(_classify(_limited(resets), manager_live=manager_live, accounts=accounts))
    expected = None if delay is None else item.since + timedelta(seconds=delay)
    assert item.push_after == expected
    lifts = (
        "" if resets is None else f" · limit resets {format_reset(resets, now=NOW, clock=False)}"
    )
    assert item.reason == "coder-1 hit its usage limit" + lifts


def test_a_card_and_an_actions_refusal_name_a_project_as_the_project_list_does() -> None:
    """Remote's project list sends a nicknamed project's nickname, and its cards, their
    pushes and the errors said the directory's name: "Billing" in the list, "api-v2" on the
    card and the lock screen (review of #259, round 13)."""
    from aisquare.services import remote_server

    billing = PROJECT.model_copy(update={"nickname": "Billing", "root": Path("/work/api-v2")})
    item = _one(needs_from_agent(_limited(None), None, project=billing, events=[], now=NOW))
    assert item.project_name == "Billing" and item.needs_item_json()["project"] == {
        "id": "prj_alpha",
        "name": "Billing",
    }
    assert "'coder-9' in Billing" in str(remote_server._no_live_agent(billing, "coder-9"))
    stale = remote_server.remote_stale(billing, "coder-9", None, done="nothing was sent")
    assert "in Billing now" in stale.message
    assert remote_needs._needs_project_name(PROJECT) == "alpha"  # no nickname: its directory


def test_naming_a_project_on_a_card_does_not_load_the_remote_server() -> None:
    """Review of #259, round 16: ``_needs_item`` named its project through
    ``remote_server.remote_project_name``, a one-line wrapper of the project service, and
    so loaded all of ``remote_server`` in processes that need nothing else of it
    (``aisquare remote needs``, ``needs_single_agent_now``). Asked in a fresh interpreter,
    as an import is once per process."""
    probe = (
        "import sys\n"
        "from pathlib import Path\n"
        "from aisquare.models import ProjectInfo\n"
        "from aisquare.services import remote_needs\n"
        "named = remote_needs._needs_project_name(ProjectInfo(id='prj_a', root=Path('alpha')))\n"
        "print(named, 'aisquare.services.remote_server' in sys.modules)\n"
    )
    ran = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True, timeout=60
    )
    assert ran.stdout.split() == ["alpha", "False"], ran.stdout + ran.stderr


def test_a_limit_with_no_account_to_switch_to_is_pushed_at_once(isolated_home: Path) -> None:
    """``on_limit = "switch"`` is the default on a machine with one account too, where no
    hand-over can start: the limit's push waited 90 s for one (review of #259, round 13).
    The scan asks the hook's own question, whether there is an account to go to: none."""
    sources = remote_needs.live_needs_sources()
    assert sources.accounts().on_limit == "switch"  # as configured
    item = _one(
        _classify(
            _limited(NOW + timedelta(hours=3)),
            accounts=sources.accounts(),
            hands_over=sources.hands_over,
        )
    )
    assert item.push_after == item.since


@pytest.mark.parametrize(("somewhere", "delay"), [(True, 90), (False, 0)])
def test_a_limit_is_held_by_the_hand_over_question_asked_with_the_agents_own_slot(
    somewhere: bool, delay: int
) -> None:
    """Review of #259, round 14: the scan asked ``somewhere_to_hand_over()`` with no slot,
    "two usable accounts", and the hook asks it with the agent's slot, "one other than
    this". On slot 2, disabled since, with slot 3 left, the hook handed the agent over to 3
    while Remote pushed the limit at once. Both ask with the row's slot now; and only for
    a row no reset or manager settles first."""
    asked: list[int | None] = []

    def hands_over(leaving: int | None) -> bool:
        asked.append(leaving)
        return somewhere

    status = _limited(NOW + timedelta(hours=3))
    status.agent.account_slot = 2
    item = _one(_classify(status, accounts=AccountsSettings(), hands_over=hands_over))
    assert asked == [2]
    assert item.push_after == item.since + timedelta(seconds=delay)

    asked.clear()
    _one(_classify(status, accounts=AccountsSettings(), hands_over=hands_over, manager_live=True))
    _one(_classify(_limited(NOW + timedelta(minutes=5)), hands_over=hands_over))
    _one(_classify(status, accounts=AccountsSettings(on_limit="wait"), hands_over=hands_over))
    assert asked == [], "settled before it: a manager, a near reset, or `wait`"


def test_the_hand_over_question_is_asked_once_a_scan_and_only_of_a_limited_row() -> None:
    """Review of #259, round 14: the scan read ``limit_settings`` every 3 s whatever the rows
    said — a store open, the registry's reconcile, every account's ``.claude.json`` parsed —
    for an answer only a limited row uses. Asked of a limited row alone, once a scan for
    its slot, however many rows are on it."""
    asked: list[int | None] = []

    def hands_over(leaving: int | None) -> bool:
        asked.append(leaving)
        return True

    working = _row("coder-1")
    fleet = Fleet(agents=[_status(working, "working", _session(working))])
    sources = replace(_sources(fleet), hands_over=hands_over)
    scan_needs_you(sources, now=NOW, dismissed=())
    assert asked == [], "no limited row: never asked"

    rows = [_row(label).model_copy(update={"account_slot": 2}) for label in ("coder-1", "coder-2")]
    resets = NOW + timedelta(hours=3)
    limited = [
        _status(row, "limited", _session(row, state="limited", resets=resets)) for row in rows
    ]
    sources = replace(_sources(Fleet(agents=limited)), hands_over=hands_over)
    items = scan_needs_you(sources, now=NOW, dismissed=())
    assert [item.kind for item in items] == ["limited", "limited"]
    assert asked == [2], "one question for the slot, in one scan"


def test_the_live_hand_over_question_is_kept_a_minute_and_a_raise_is_a_yes(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 14: the question could raise where ``accounts_settings`` never
    could — an ``OSError`` listing the slot directories — and a raise there was the
    project's failed read: a 503 on every action. It is a yes, as an unreadable registry
    is, and kept a minute, as an agent no hand-over can move stays limited for hours."""
    from aisquare.services import claude_accounts as claude_accounts_service

    calls: list[int | None] = []

    def unreadable(leaving: int | None) -> bool:
        calls.append(leaving)
        raise OSError("[Errno 13] Permission denied: 'claude-accounts'")

    clock = [1000.0]
    monkeypatch.setattr(remote_needs, "_hands_over", {})
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(claude_accounts_service, "somewhere_to_hand_over", unreadable)
    hands_over = remote_needs.live_needs_sources().hands_over

    assert hands_over(2) is True and hands_over(2) is True
    assert calls == [2], "kept"
    clock[0] += remote_needs._NEEDS_HANDS_OVER_KEPT
    assert hands_over(2) is True and calls == [2, 2], "asked again a minute on"

    monkeypatch.undo()
    monkeypatch.setattr(
        claude_accounts_service,
        "_read_arranged",
        lambda: (_ for _ in ()).throw(OSError("[Errno 13] Permission denied")),
    )
    assert claude_accounts_service.somewhere_to_hand_over(2), "as an unreadable registry is"


def test_a_limits_reset_reaches_the_phone_as_an_instant_never_the_machines_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The card's reason, its push and the board's line in its detail said the reset by the
    machine's clock: "(18:40)" from a machine in India, which a phone in UTC-7 read as its
    own, for a reset at 06:10 there. The reason and the push say how far it is; the instant
    goes to the page as ``resets_at``; the board's line comes without its reset, which it
    said as of when the hook ran."""
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset is POSIX-only: the process zone cannot be switched for the test")
    from aisquare.services import remote_push
    from aisquare.services import team as team_service

    now = datetime(2026, 10, 7, 10, 0, 7, tzinfo=UTC)
    resets = datetime(2026, 10, 7, 13, 10, tzinfo=UTC)
    with monkeypatch.context() as local:
        local.setenv("TZ", "Asia/Kolkata")
        time.tzset()
        try:
            monkeypatch.setattr(team_service, "_now", lambda: now - timedelta(minutes=1))
            line = team_service._limited_text(
                "coder-1", "raw", LimitNotice("five-hour", resets), project=PROJECT
            )
            row = _row()
            session = _session(row, state="limited", resets=resets)
            event = _event(7, "limited", line, session=session, at=now - timedelta(minutes=1))
            status = _status(row, "limited", session, fleet_service._limit_detail(resets, now))
            item = _one(needs_from_agent(status, None, project=PROJECT, events=[event], now=now))
            push = remote_push.push_needs_message([item], total=1, base_url=None)
        finally:
            local.undo()
            time.tzset()
    assert "(18:40)" in line and "(18:40)" in str(status.detail), (
        "the control: the machine's own clock"
    )
    assert item.reason == "coder-1 hit its usage limit · limit resets in 3h 09m"
    assert push["body"] == item.reason
    assert item.detail == {
        "text": "coder-1 hit its five-hour limit — `aisquare fleet switch --project prj_alpha "
        "coder-1` moves it to the account with the most headroom (or wait for the reset)",
        "resets_at": "2026-10-07T13:10:00+00:00",
    }
    assert item.excerpt == item.detail["text"]
    clock = re.compile(r"\b\d{1,2}:\d{2}\b")
    assert not clock.search(item.reason + item.excerpt + str(item.detail["text"]) + push["body"])


@pytest.mark.parametrize(
    ("role", "manager_live"), [("coder", True), ("coder", False), ("manager", True)]
)
def test_a_closing_question_is_pushed_at_once_since_no_manager_hears_of_it(
    role: str, manager_live: bool
) -> None:
    """A crew agent's closing question waited five minutes for a live manager to answer it
    first, but nothing wakes the manager for it: a crew agent's Stop marks its session
    waiting and nudges nobody (``team.hook_stop``), and nothing is on the board. The agent
    sat idle with no push and nobody on it (review of #243, round 22, 6/7)."""
    row = _row(role=role, spawned_by="ses_manager" if role == "coder" else None)
    tail = _tail(newest="assistant_text", text="Want me to commit this?")
    item = _one(_classify(_status(row, "waiting", _session(row)), tail, manager_live=manager_live))
    assert item.push_after == item.since


def test_the_guide_says_when_each_kind_is_pushed_as_the_scan_decides_it() -> None:
    """docs/remote.md gave the delays of a crash and a lost pane only, and said every other
    item went out after two scans: a usage limit near its reset never does, an interruption
    waits ten minutes. Each delay the guide gives is the scan's own."""
    guide = Path(__file__).resolve().parents[1] / "docs" / "remote.md"
    prose = " ".join(guide.read_text(encoding="utf-8").split())
    delays = remote_needs._NEEDS_PUSH_DELAY
    said: list[tuple[str, object, object]] = [
        ("a crash: after 30 seconds", delays["crashed"], timedelta(seconds=30)),
        ("a lost pane, a stopped manager, or tmux not answering: after a minute",
         {delays["lost"], delays["manager_down"], delays["fleet_down"]}, {timedelta(minutes=1)}),
        ("a usage limit: never when it lifts within `[accounts] wait_if_reset_within_minutes`"
         " (15 by default)", AccountsSettings().wait_if_reset_within_minutes, 15),
        ('after 90 seconds when `on_limit = "switch"` and another account can take the agent,'
         " or a live manager is on it",
         remote_needs._LIMITED_PUSH_DELAY, timedelta(seconds=90)),
        ("an interruption: after 10 minutes", delays["interrupted"], timedelta(minutes=10)),
    ]  # fmt: skip
    assert [sentence for sentence, _code, _guide in said if sentence not in prose] == []
    assert [sentence for sentence, code, guide in said if code != guide] == []


@pytest.mark.parametrize(
    "text",
    [
        "Which approach?\n1. A table\n2. A file\n3. Neither",
        "Done with the refactor.\n\nWant me to commit this?",
        "**Should I push?**",
        '> "Is this the right file?"',
        "Shall I open the PR?\n\n- it adds the cache\n- it adds the tests",
        "All green.\n\nOne thing:\n\nship it now or wait for review?",
        "```swift\nvar email: String?\n```\n\nShould I make `Account.email` optional too?",
        "Should I run `make check`?",
        "Is `String?` the right type for `email`?",
        "Which of these should the cache use?\n```\nRedis\n\nSQLite\n```",
    ],
)
def test_text_that_asks(text: str) -> None:
    assert looks_like_a_question(text)


@pytest.mark.parametrize(
    "text",
    [
        "The branch is green.\n\nShould I go ahead and merge it? (y/n)",
        "Want me to also update the README? (It still mentions the old flag.)",
        "Proceed with the migration? [y/N]",
        "Merge it now? (y/n) [default: no]",
        "Shall I deploy to staging? 🚀",
        "Ready to merge? 👍🏽",
        "Ship it? ❤️",
        "**Should I push? (it is a force-push)**",
        "要我现在提交吗\uff1f",
        "缓存已经改好了。\n\n你想用哪种方案\uff1f\n1. Redis\n2. SQLite\n3. 不用缓存",
        "この変更をコミットしてもよろしいですか\uff1f",
        "هل تريد أن أدفع التغييرات الآن؟",
    ],
)
def test_text_that_asks_after_its_question_mark_or_with_another_one(text: str) -> None:
    """``?`` alone, then quotes and brackets alone, missed a question followed by an aside or
    an emoji, and every question asked in Chinese, Japanese or Arabic, in which Claude answers
    its human: no card and no push for an agent waiting on an answer (review of #243, sweep
    3). The card shows the question it found."""
    assert looks_like_a_question(text)
    row = _row()
    tail = _tail(newest="assistant_text", text=text)
    item = _one(_classify(_status(row, "waiting", _session(row, state="waiting")), tail))
    assert item.kind == "asked" and item.excerpt


@pytest.mark.parametrize(
    "text",
    [
        "Fixed the parser (see below).",
        "Updated the docs [skip ci]",
        "Merged (was it the cache?) and pushed.",
        "Done 🚀",
        "缓存已经改好了。",
        "Fixed in [#123](https://example.com/pull/123)",
    ],
)
def test_an_aside_or_an_emoji_alone_asks_nothing(text: str) -> None:
    assert not looks_like_a_question(text)


@pytest.mark.parametrize(
    "text",
    [
        "Tests pass? ✅",
        "- Lint clean? ✔️",
        "* Types check? ✔",
        "Migrations reversible? ❌ (the drop is not)",
    ],
)
def test_a_checklist_line_ticked_after_its_question_reports_and_asks_nothing(text: str) -> None:
    """An emoji may follow a question ("Shall I deploy? 🚀"), but a check or cross mark after
    one is a closing checklist's result, not a question to the human (review of #243, round
    5)."""
    assert not looks_like_a_question(text)


@pytest.mark.parametrize(
    ("text", "asks"),
    [
        ("Merge it now? " + "(a) " * 8_000, True),
        (" ".join(f"[#{n}](https://example.com/pull/{n})" for n in range(1_000)), False),
    ],
    ids=["asides", "links"],
)
def test_a_long_line_of_asides_or_links_is_read_in_one_pass(text: str, asks: bool) -> None:
    """The question test runs every scan, and every quarter second of an interrupt, on an
    assistant's last text, up to a 256 KiB record. Searching the line for its last aside again
    after each one took seconds on a line of 1 000 links, and minutes on a 256 KiB one of
    ``(a)``s (review of #243, round 5). Each aside is read once now: milliseconds."""
    started = time.perf_counter()
    assert looks_like_a_question(text) is asks
    assert time.perf_counter() - started < 0.5


def test_a_long_line_of_backtick_runs_is_read_in_one_pass() -> None:
    """Pairing each run of backticks against the rest of its line took over a second on a
    256 KiB line of runs of different lengths, none closed (review of #243, round 5). Each run
    is paired with the next of its length now, in one pass."""
    text = " ".join("`" * size for size in range(1, 720)) + " — keep these?"
    assert len(text) > 250_000
    started = time.perf_counter()
    assert looks_like_a_question(text)
    assert time.perf_counter() - started < 0.5


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


@pytest.mark.parametrize(
    "text",
    [
        "Added the field.\n\n```swift\nstruct User {\n    var email: String?\n}\n```\n\n"
        "All 42 tests pass; committed as a1b2c3d.",
        "The repository is now:\n\n```kotlin\ninterface Users {\n    fun find(id: Long): User?\n}"
        "\n```",
        "Done:\n\n```ruby\ndef publishable?\n  !draft? && approved?\nend\n```",
        "Fixed:\n\n```rust\nOk(toml::from_str(&fs::read_to_string(path)?)?)\n```",
        "The query binds the address:\n\n```sql\nSELECT * FROM users WHERE email = ?\n```",
        "Done.\n\n~~~python\nwho = user.name if user else None  # was it set?\n~~~",
        "Here it is:\n\n```sh\nmake check\nstatus=$?",
        "- the tag parser now uses `<(.*?)>`",
        "- `User.email` is now `String?`",
    ],
)
def test_code_that_ends_in_a_question_mark_asks_nothing(text: str) -> None:
    """A closing summary that showed code ending in ``?`` was an ``asked`` card, "coder-1
    ended its turn with a question", pushed again every turn it did so: a Swift optional, a
    Kotlin return type, a Ruby predicate, Rust's ``?``, a SQL placeholder, a comment in a
    block, a lazy regex in an inline span. Only prose asks: no line inside a fenced block
    (one never closed runs to the end), and no ``?`` inside an inline span."""
    assert not looks_like_a_question(text)
    row = _row()
    tail = _tail(newest="assistant_text", text=text)
    assert _classify(_status(row, "waiting", _session(row, state="waiting")), tail) == []


def test_the_question_a_card_shows_is_the_last_one_in_prose() -> None:
    text = "Should I run `make check` before the push?\n\n```sh\nmake check\nstatus=$?\n```"
    row = _row()
    tail = _tail(newest="assistant_text", text=text)
    item = _one(_classify(_status(row, "waiting", _session(row, state="waiting")), tail))
    assert item.kind == "asked"
    assert item.excerpt.startswith("Should I run `make check` before the push?"), item.excerpt


@pytest.mark.parametrize(
    "text",
    [
        "The query binds the address:\n\n    SELECT * FROM users WHERE email = ?",
        "Done:\n\n\tdef publishable?\n\t  !draft? && approved?\n\tend",
        "    var email: String?\n\nThe field is optional now.",
        "- added the predicate:\n\n        def publishable?",
        "1. the binding:\n\n       WHERE email = ?\n\n2. the tests pass.",
        "The model:\n\n```swift\nstruct User {}\n```\n    var email: String?",
    ],
)
def test_an_indented_code_block_that_ends_in_a_question_mark_asks_nothing(text: str) -> None:
    """Markdown's other code block, four columns deeper than its list item or the margin,
    opened by the text, a blank line or a fence's close: ``    WHERE id = ?`` was still a
    closing question once fenced blocks were not."""
    assert not looks_like_a_question(text)
    row = _row()
    tail = _tail(newest="assistant_text", text=text)
    assert _classify(_status(row, "waiting", _session(row, state="waiting")), tail) == []


@pytest.mark.parametrize(
    "text",
    [
        "1. Keep the cache.\n\n    Or should I drop it?",
        "- the cache stays\n\n  - and the tests\n\n      should they move too?",
        "Here is the plan, and one thing to settle first:\n    should I start with the cache?",
        "    make check\n\nShould I push now?",
    ],
)
def test_an_indented_line_that_is_no_code_block_still_asks(text: str) -> None:
    """A list item's continuation, indented to its content, and a paragraph's next line,
    which no code block can interrupt, are prose as markdown reads them."""
    assert looks_like_a_question(text)


def test_the_question_a_card_shows_is_not_an_indented_code_line() -> None:
    text = "Should I bind the address like this?\n\n    SELECT * FROM users WHERE email = ?"
    row = _row()
    tail = _tail(newest="assistant_text", text=text)
    item = _one(_classify(_status(row, "waiting", _session(row, state="waiting")), tail))
    assert item.kind == "asked"
    assert item.excerpt.startswith("Should I bind the address like this?"), item.excerpt


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


def _seed_dismissals(seeded: dict[str, str]) -> None:
    remote_needs_path().parent.mkdir(parents=True, exist_ok=True)
    remote_needs_path().write_text(json.dumps({"dismissed": seeded}), encoding="utf-8")


def test_a_dismissal_older_than_a_week_is_pruned() -> None:
    """With fewer than 500 on file, the 7-day rule alone can drop one: seeded with 600, the
    cap dropped the old ones by itself, and a test of the age held without the rule."""
    now = datetime.now(UTC)
    seeded = {f"ny_old{n}": (now - timedelta(days=8)).isoformat() for n in range(3)}
    seeded |= {f"ny_{n:016x}": (now - timedelta(days=6, minutes=n)).isoformat() for n in range(10)}
    _seed_dismissals(seeded)
    record_needs_dismissal("ny_newest")
    assert set(load_needs_dismissals()) == {"ny_newest", *(f"ny_{n:016x}" for n in range(10))}


def test_at_most_the_newest_500_dismissals_are_kept() -> None:
    now = datetime.now(UTC)
    _seed_dismissals({f"ny_{n:016x}": (now - timedelta(minutes=n)).isoformat() for n in range(600)})
    record_needs_dismissal("ny_newest")
    kept = load_needs_dismissals()
    assert len(kept) == 500 and "ny_newest" in kept
    assert f"ny_{599:016x}" not in kept and f"ny_{0:016x}" in kept, "the oldest go first"


_TOO_DEEP = "[" * 100_000 + "]" * 100_000
"""JSON nested past the parser's depth: ``json.loads`` raises ``RecursionError`` on it, which
escaped every reader and the writer (review of #259, round 6, 4/10)."""


@pytest.mark.parametrize(
    "body", ["not json", "[1, 2]", '{"dismissed": ["ny_1"]}', _TOO_DEEP], ids=lambda b: b[:16]
)
def test_an_unreadable_dismissals_file_dismisses_nothing(body: str) -> None:
    remote_needs_path().parent.mkdir(parents=True, exist_ok=True)
    remote_needs_path().write_text(body, encoding="utf-8")
    assert load_needs_dismissals() == {}


_KEPT_AT = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="seconds")
_KEPT_DISMISSED = {f"ny_{n:016x}": _KEPT_AT for n in range(3)}
_KEPT_BEFORE = {
    "version": 1,
    "dismissed": _KEPT_DISMISSED,
    "first_seen": {"fleet_down:prj_alpha": _KEPT_AT},
}
"""What three dismissals and a sighting leave on file, each an hour ago (within the week)."""


@pytest.mark.parametrize(
    "body",
    [
        json.dumps(_KEPT_BEFORE).encode() + b"\x00",  # one stray byte
        b"\xff\xfe not UTF-8",
        b"[1, 2]",
        b'{"dismissed": ["ny_1"]}',
        b'{"dismissed": {"ny_1": {"at": "2026-10-07T11:00:00+00:00"}}}',  # a later shape
        _TOO_DEEP.encode(),
        b'{"version": "2", "dismissed": {}}',
        b'{"version": 0, "dismissed": {}}',
    ],
    ids=lambda b: b[:16].decode(errors="replace"),
)
def test_a_needs_file_a_write_cannot_read_whole_is_kept_aside_not_overwritten(
    body: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    """Review of #259, remote focus, 3/12: a file that could not be read was read as empty,
    and the next dismissal, re-dating or first sighting replaced it with that one entry:
    every earlier dismissal and first sighting gone for good. It is moved to
    ``remote-needs.json.bad`` as it was, said in the log, and a new one started."""
    path = remote_needs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    with caplog.at_level(logging.WARNING, logger=remote_needs.log.name):
        record_needs_dismissal("ny_newest")
    assert path.with_name("remote-needs.json.bad").read_bytes() == body
    assert set(load_needs_dismissals()) == {"ny_newest"}
    assert "remote-needs.json.bad" in caplog.text


def test_each_needs_file_kept_aside_is_kept_under_a_name_of_its_own(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Review of #259, round 8, 8/9: ``os.replace(path, aside)`` put the second file a write
    could not read over the first one's ``.bad``, and the dismissals and first sightings
    the first held, the copy kept for the human, were gone. Each goes to the first free
    ``.bad``, ``.bad.1``…"""
    path = remote_needs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    bodies = [b"[1]", b"[2]", b"[3]"]
    for body in bodies:
        path.write_bytes(body)
        with caplog.at_level(logging.WARNING, logger=remote_needs.log.name):
            record_needs_dismissal("ny_newest")
    kept = [
        path.with_name(name)
        for name in ("remote-needs.json.bad", *(f"remote-needs.json.bad.{n}" for n in (1, 2)))
    ]
    assert [aside.read_bytes() for aside in kept] == bodies
    assert "remote-needs.json.bad.2" in caplog.text
    assert set(load_needs_dismissals()) == {"ny_newest"}


def test_a_later_versions_needs_file_is_never_written_over() -> None:
    """Review of #259, round 8, 5/9: the reader looked at neither ``version`` nor any part it
    does not know, so a later release's file whose two known parts kept this shape was read
    as whole and rewritten as ``version: 1``, what that release added there gone, with no
    copy and no word. A later version's file is the write's ``OSError`` (a dismissal's 503,
    the watcher's streak), left as it is and not moved aside; its dismissals still hide."""
    path = remote_needs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    later = {**_KEPT_BEFORE, "version": 2, "snoozed": {"ny_9": _KEPT_AT}}
    body = json.dumps(later).encode()
    path.write_bytes(body)
    with pytest.raises(remote_needs.LaterNeedsFile, match="version 2, written by a later"):
        record_needs_dismissal("ny_newest")
    assert isinstance(remote_needs.LaterNeedsFile("x"), OSError), "said as a failed write"
    assert path.read_bytes() == body
    assert not path.with_name("remote-needs.json.bad").exists()
    assert set(load_needs_dismissals()) == set(_KEPT_DISMISSED)


def test_a_key_the_needs_file_holds_that_this_version_does_not_know_is_written_back() -> None:
    """A key of its own version's file that no part here reads is carried through the write,
    as it was."""
    path = remote_needs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**_KEPT_BEFORE, "pinned": {"ny_9": [1, 2]}}), encoding="utf-8")
    record_needs_dismissal("ny_newest")
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["pinned"] == {"ny_9": [1, 2]} and written["version"] == 1
    assert set(written["dismissed"]) == {*_KEPT_DISMISSED, "ny_newest"}


def test_a_needs_file_that_could_not_be_read_is_never_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read that failed (EIO, EACCES) says nothing of what is in the file: the write is the
    ``OSError`` (a dismissal's 503, the watcher's streak), the file left as it was."""
    path = remote_needs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_KEPT_BEFORE), encoding="utf-8")
    real = Path.read_text

    def failing(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == path:
            raise OSError(5, "Input/output error")
        return real(self, *args, **kwargs)

    with monkeypatch.context() as patched, pytest.raises(OSError, match="Input/output"):
        patched.setattr(Path, "read_text", failing)
        record_needs_dismissal("ny_newest")
    assert json.loads(path.read_text(encoding="utf-8")) == _KEPT_BEFORE
    assert not path.with_name("remote-needs.json.bad").exists()
    record_needs_dismissal("ny_newest")  # the control: read whole, it is built on
    assert set(load_needs_dismissals()) == {*_KEPT_DISMISSED, "ny_newest"}


def test_a_dismissals_stamp_and_asq_remote_needs_age_are_read_as_the_server_reads_a_stamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both read an ISO stamp with copies of the rule ``remote_server._remote_instant`` is the
    one reader of: with an offset as it says, without one as UTC. A copy that drifted read
    stamps unlike the server that wrote them, as the push sender's once read a device's
    expiry as local time (review of #243, round 3): the next change to the rule is made in
    one place, and both read through it."""
    from aisquare.cli.remote import _needs_age
    from aisquare.services import remote_server

    read: list[object] = []
    real = remote_server._remote_instant

    def reading(text: object, *, naive_is_local: bool = False) -> datetime | None:
        read.append(text)
        return real(text, naive_is_local=naive_is_local)

    monkeypatch.setattr(remote_server, "_remote_instant", reading)
    naive = (datetime.now(UTC) - timedelta(days=6)).replace(tzinfo=None).isoformat()
    _seed_dismissals({"ny_naive": naive, "ny_garbled": "last tuesday"})
    record_needs_dismissal("ny_newest")
    assert set(load_needs_dismissals()) == {"ny_naive", "ny_newest"}, "naive is UTC: 6 days"
    assert naive in read and "last tuesday" in read
    assert _needs_age("2026-10-07T10:55:00", NOW) == "1h05m"
    assert _needs_age("2026-10-07T13:55:00+02:00", NOW) == "5m"
    assert _needs_age(None, NOW) == _needs_age("soon", NOW) == "?"
    assert {"2026-10-07T10:55:00", "2026-10-07T13:55:00+02:00", None, "soon"} <= set(read)


def test_asq_remote_needs_writes_an_age_by_the_boards_one_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_needs_age`` promised the age "as the board says it" with a copy of the board's
    ``_age`` of its own, as ``asq team prune`` had one: a change to how the board writes an
    age (days past 24 h, say) reached one of the three (review of #243, round 7, 4/4). The
    rule the board learns here, ``asq remote needs`` writes too."""
    from aisquare.cli.remote import _needs_age
    from aisquare.services import team as team_service

    assert _needs_age("2026-10-07T09:55:00Z", NOW) == "2h05m"
    monkeypatch.setattr(team_service, "minutes_text", lambda minutes: f"<{minutes} min>")
    assert _needs_age("2026-10-07T09:55:00Z", NOW) == "<125 min>"
    assert _needs_age("2026-10-07T12:30:00Z", NOW) == "<0 min>", "a stamp ahead is no age"
    assert team_service.age_text(NOW - timedelta(minutes=125), NOW) == "<125 min>"


# --- one agent, now: the predicates actions rely on ---------------------------------------


class FakeTmux:
    """The agent's tmux server: what it says about the pane, and what was typed into it."""

    def at(self, path: Path | None) -> FakeTmux:
        """Asked where the row's spawn recorded its server (``TmuxServer.at``): this one."""
        return self

    def __init__(
        self,
        *,
        reference: datetime | None = None,
        command: str = "claude",
        quiet_for: float | None = 60.0,
        gone: bool = False,
        started: datetime | None = None,
    ) -> None:
        self.reference = reference
        self.command = command
        self.quiet_for = quiet_for
        """How long the pane has printed nothing; ``None`` is a time tmux does not say."""
        self.gone = gone
        self.started = started
        """When the server started; ``None`` is a start tmux does not report."""
        self.fail = False
        self.timing_out = False
        """``send_keys`` times out: a server that did not answer, which may still type them."""
        self.asked: list[str] = []
        """Every pane asked about, once per question."""
        self.typed: list[tuple[str, ...]] = []
        self.screen: list[str] | None = None
        """What the pane shows when it is captured (rule 8's screen, ``fleet``'s trust check);
        ``None`` is a pane tmux cannot capture."""

    def output_epoch(self) -> str:
        """``#{window_activity}`` as tmux prints it: whole seconds, or nothing."""
        if self.quiet_for is None:
            return ""
        now = self.reference.timestamp() if self.reference else time.time()
        return str(int(now - self.quiet_for))

    def pane_facts(self, pane_id: str) -> SimpleNamespace | None:
        self.asked.append(pane_id)
        if self.gone:
            return None
        epoch = self.output_epoch()
        return SimpleNamespace(
            dead=False,
            dead_status=None,
            current_command=self.command,
            server_started=self.started,
            last_output=datetime.fromtimestamp(int(epoch), tz=UTC) if epoch else None,
        )

    def started_at(self) -> datetime | None:
        return self.started

    def capture(self, pane_id: str, **_: object) -> SimpleNamespace:
        if self.screen is None:
            from aisquare.core.tmux import TmuxError

            raise TmuxError(f"can't find pane: {pane_id}")
        return SimpleNamespace(lines=list(self.screen))

    def answers(self) -> bool:
        return True

    def run(self, *args: str, stdin: bytes | None = None) -> str:
        # Every fact about the pane is asked through `pane_facts`, the one format `PaneFacts`
        # owns: a question of its own here is the copy of it needs-you once kept.
        assert args[:2] == ("list-panes", "-a"), f"only the fleet's listing runs tmux: {args}"
        return ""  # the fleet listing's output times: none, so the board's state decides

    def send_keys(self, pane_id: str, *keys: str) -> None:
        if self.fail:
            from aisquare.core.tmux import TmuxError

            raise TmuxError("no server running on /tmp/tmux-1000/asq")
        if self.timing_out:
            from aisquare.core.tmux import TmuxTimedOut

            raise TmuxTimedOut("tmux did not answer within 30s: send-keys")
        self.typed.append(("keys", pane_id, *keys))

    def send_literal(self, pane_id: str, text: str) -> None:
        self.typed.append(("text", pane_id, text))

    def paste(self, pane_id: str, text: str) -> None:
        self.typed.append(("paste", pane_id, text))


def _now_of(
    fleet: Fleet, tmux: FakeTmux, monkeypatch: pytest.MonkeyPatch, label: str = "coder-1"
) -> AgentNow:
    monkeypatch.setattr(remote_needs, "live_needs_sources", lambda: _sources(fleet))
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: tmux)
    return needs_agent_now(PROJECT, label, now=NOW)


def test_the_pane_reads_go_to_the_server_the_listing_found_without_reading_config_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Review of #259, round 9, 7/8: the screen (every scan, for every agent with more than one
    call pending, and again for each quick answer under the agent's lock) and the pane's last
    output were asked of ``fleet.server_of``, which read ``config.toml`` to work out an address
    the listed row already carries (``socket_at``). Round 10, 7/9: and a quick answer's keys,
    typed into the server whose screen was checked."""
    tmux = FakeTmux(reference=NOW)
    tmux.screen = ["the screen"]
    asked: list[str] = []

    def server(config: Any = None) -> FakeTmux:
        asked.append(config.tmux_socket)
        return tmux

    monkeypatch.setattr(fleet_service, "server", server)
    monkeypatch.setattr(fleet_service, "settings", lambda: pytest.fail("config.toml read again"))
    row = _row()
    status = _status(row, "attention", _session(row), socket_at=tmp_path / "asq")
    assert remote_needs._needs_screen_now(status) == ["the screen"]
    assert remote_needs.live_needs_sources().pane_output(status) is not None
    snap = remote_needs._needs_snapshot(PROJECT, status, None, (), NOW)
    assert snap.pane_is_agent
    remote_needs._needs_send(status, ["1"], "", enter=True)
    assert tmux.typed == [("keys", row.pane_id, "1"), ("keys", row.pane_id, "Enter")]
    assert asked == [row.tmux_socket] * 4


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


def test_a_tail_cut_short_of_the_newest_message_counts_as_a_tool_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #243, round 7: a walk that ran out inside a 700 KB screenshot result never
    reached the message whose Bash waits on its permission prompt, and read nothing
    pending. A stop's ``/exit`` and Enter answered "1. Yes" once its pane was quiet. What
    it did not read may hold a pending tool: one at work while the pane prints, a dialog
    once it is quiet, never a prompt to type at. Not when the cut is older than the row
    (a resumed session's), and it makes no card: it names no tool to show."""
    cut = replace(_tail(newest="tool_result"), cut_at=NOW - timedelta(seconds=2))
    busy = _now_of(_working(cut), FakeTmux(reference=NOW, quiet_for=2), monkeypatch)
    assert busy.items == () and not needs_dialog_open(busy)
    assert needs_tool_pending(busy)
    quiet = _now_of(_working(cut, state="waiting"), FakeTmux(reference=NOW), monkeypatch)
    assert needs_dialog_open(quiet) and not needs_at_input_prompt(quiet)
    assert quiet.items == ()
    older = replace(cut, cut_at=BORN - timedelta(minutes=1))
    resumed = _now_of(_working(older, state="waiting"), FakeTmux(reference=NOW), monkeypatch)
    assert not needs_tool_pending(resumed) and not needs_dialog_open(resumed)
    assert needs_at_input_prompt(resumed), "the control"


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


@pytest.mark.parametrize("state", ["attention", "waiting"])
def test_an_interruption_behind_a_local_command_is_still_where_the_agent_sits(
    state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sweep 5 of #243: a prompt answered with Esc fires no Stop, so the session stays
    ``attention``, and a ``/model`` or a ``!`` command typed at the prompt after it fires
    no hook either. Its records were the newest, the interruption under them unseen: the
    idle agent was a "shows a dialog" card, pushed at once, and Prompt, Tell, Send, Stop,
    Restart and Switch were each refused ``dialog_open`` until someone typed at the
    machine. It is the interruption's card still, under the same id, and at its prompt.
    ``waiting`` is the row 30 minutes on, the session still marked ``attention``."""
    row = _row()
    escaped_at = NOW - timedelta(minutes=1)
    plain = _tail(newest="interrupted", at=escaped_at, text="Pushing now.", key="rec-esc")
    behind = replace(
        _tail(newest="own", at=NOW - timedelta(seconds=20), text="Pushing now.", key="rec-model"),
        under_own="interrupted",
        under_own_at=escaped_at,
        under_own_key="rec-esc",
    )
    cards = []
    for tail in (plain, behind):
        fleet = Fleet(agents=[_status(row, state, _session(row, state="attention"))])
        fleet.tails[f"/transcripts/{row.label}.jsonl"] = tail
        snap = _now_of(fleet, FakeTmux(reference=NOW), monkeypatch)
        assert not needs_dialog_open(snap) and needs_at_input_prompt(snap)
        cards.append([(item.kind, item.id, item.since) for item in snap.items])
    assert cards[0] == cards[1] and cards[0][0][0] == "interrupted"
    assert cards[0][0][2] == escaped_at
    unmarked = replace(behind, under_own="none", under_own_at=None, under_own_key=None)
    fleet = Fleet(agents=[_status(row, state, _session(row, state="attention"))])
    fleet.tails[f"/transcripts/{row.label}.jsonl"] = unmarked
    snap = _now_of(fleet, FakeTmux(reference=NOW), monkeypatch)
    assert needs_dialog_open(snap), "the control: no interruption, a dialog may be up"


def _printed_since_the_notice(tail: TranscriptTail | None, *, printed: datetime) -> Fleet:
    """coder-1 at a dialog with no tool behind it, notified a minute ago, its row as the real
    ``fleet._derive`` reads it once the pane printed at ``printed``; ``tail`` ``None`` is a
    transcript that cannot be read."""
    row = _row()
    session = _session(row, state="attention", seen=NOW - timedelta(minutes=1))
    view = fleet_service._PaneView(False, None, "claude", printed)
    fleet = Fleet(
        agents=[fleet_service._status(row, session, {row.id: view}, None, NOW, FleetSettings())]
    )
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
        marker_key=None, empty=True,
    )  # fmt: skip
    empty = _printed_since_the_notice(nothing, printed=NOW - timedelta(seconds=1))
    snap = _now_of(empty, FakeTmux(reference=NOW, quiet_for=1), monkeypatch)
    assert [item.kind for item in snap.items] == ["permission"] and needs_dialog_open(snap)


def test_a_transcript_whose_tail_holds_no_conversation_is_no_card_either(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A tail's ``newest`` is ``none`` for an empty file, and also for a walk that met no
    conversation record within its budget: a sub-agent's run, or other kinds of record,
    filling the end of the file. That one was read as "nothing written at all", and a row
    at work while its session still said ``attention`` got a dialog card in the feed. It
    says nothing of what was written, so it is the doubt a transcript that cannot be read
    is: no card, and the guard still refuses (verification of a8a6db0f)."""
    from aisquare.services.remote_actions import action_may_answer

    path = tmp_path / "coder-1.jsonl"
    side = {"type": "assistant", "isSidechain": True, "timestamp": "2026-10-07T11:59:50Z"}
    record = {**side, "message": {"id": "msg_s", "content": [{"type": "text", "text": "x"}]}}
    path.write_text("\n".join(json.dumps(record) for _ in range(5)) + "\n", encoding="utf-8")
    walked = read_transcript_tail(path)
    assert walked is not None and walked.newest == "none" and not walked.empty
    fleet = _printed_since_the_notice(walked, printed=NOW - timedelta(seconds=1))
    snap = _now_of(fleet, FakeTmux(reference=NOW, quiet_for=1), monkeypatch)
    assert snap.status is not None and snap.status.state == "working"
    assert snap.items == (), "a card the transcript cannot vouch for"
    assert needs_dialog_open(snap) and action_may_answer(snap), "and no Enter either"
    path.write_text("", encoding="utf-8")
    nothing = read_transcript_tail(path)
    assert nothing is not None and nothing.empty, "an empty file is nothing written"


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
    waiting = _working(_tail(newest="tool_result"), state="waiting")
    assert needs_at_input_prompt(_now_of(waiting, FakeTmux(reference=NOW), monkeypatch))
    busy = FakeTmux(reference=NOW, quiet_for=0)
    assert not needs_at_input_prompt(_now_of(waiting, busy, monkeypatch))
    said = _tail(newest="assistant_text", text="Done.")
    assert needs_at_input_prompt(_now_of(_working(said), FakeTmux(reference=NOW), monkeypatch))
    mid_turn = _tail(newest="tool_result")
    assert not needs_at_input_prompt(
        _now_of(_working(mid_turn), FakeTmux(reference=NOW), monkeypatch)
    )


def test_an_agent_nothing_here_reads_is_never_at_its_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #243, round 7: a row with no board session (``fleet spawn --bin``, a Claude
    Code started without aisquare's hooks) has no transcript read, so a permission prompt
    there is no pending tool and no ``attention``, and ``fleet._derive`` reads its pane,
    quiet for 5 s, as ``waiting``. That was all ``needs_at_input_prompt`` had, and a
    prompt-mode tell pasted and pressed Enter into the prompt. A session whose transcript
    cannot be read hides a tool use's first seconds the same way. Neither shows a dialog
    here either: nothing says one is up, and the actions take the unseen for one."""
    hookless = _row().model_copy(update={"session_id": None})
    snap = _now_of(
        Fleet(agents=[_status(hookless, "waiting")]), FakeTmux(reference=NOW), monkeypatch
    )
    assert snap.pane_is_agent and snap.pane_quiet is True and snap.tail is None
    assert needs_unseen(snap)
    assert not needs_at_input_prompt(snap) and not needs_dialog_open(snap)
    unread = _now_of(_working(None, state="waiting"), FakeTmux(reference=NOW), monkeypatch)
    assert unread.status is not None and unread.status.session is not None
    assert needs_unseen(unread) and not needs_at_input_prompt(unread)
    read = _working(_tail(newest="tool_result"), state="waiting")
    snap = _now_of(read, FakeTmux(reference=NOW), monkeypatch)
    assert not needs_unseen(snap) and needs_at_input_prompt(snap), "the control"
    gone = _now_of(Fleet(agents=[_status(hookless, "waiting")]), FakeTmux(gone=True), monkeypatch)
    assert not needs_unseen(gone), "no pane of its own: not_agent says so"


_NOTHING_WRITTEN = TranscriptTail(
    pending=(), newest="none", newest_at=None, last_text=None, last_text_at=None,
    marker_key=None, empty=True,
)  # fmt: skip


@pytest.mark.parametrize(
    ("tail", "quiet_for", "at_prompt"),
    [
        pytest.param(_NOTHING_WRITTEN, 60.0, True, id="nothing written yet"),
        pytest.param(
            _tail(newest="tool_result", at=BORN - timedelta(minutes=1)),
            60.0,
            True,
            id="only a resumed session's records",
        ),
        pytest.param(_NOTHING_WRITTEN, 1.0, False, id="a pane at work"),
        pytest.param(_tail(newest="user_prompt"), 60.0, False, id="its first prompt written"),
        pytest.param(None, 60.0, False, id="a transcript that cannot be read"),
    ],
)
def test_a_session_at_its_fresh_prompt_is_at_its_prompt_though_the_board_says_working(
    tail: TranscriptTail | None, quiet_for: float, at_prompt: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session starts ``working`` on the board, which is trusted for 30 minutes: an agent
    spawned with no prompt, or after a ``/clear``, read as busy at its fresh prompt, and
    neither prompt mode nor Interrupt & tell could reach it (review of #243, sweep 3). A
    turn writes the human's prompt before anything else, and its pane animates while it
    runs: a quiet pane over a transcript this process has written nothing in is a prompt.
    A transcript that cannot be read says nothing of what was written."""
    snap = _now_of(_working(tail), FakeTmux(reference=NOW, quiet_for=quiet_for), monkeypatch)
    assert snap.status is not None and snap.status.state == "working"
    assert needs_at_input_prompt(snap) is at_prompt
    noticed = _now_of(_working(tail, state="attention"), FakeTmux(reference=NOW), monkeypatch)
    assert not needs_at_input_prompt(noticed), "a dialog at its start is still a dialog"


def test_quiet_is_unknown_when_tmux_will_not_say(monkeypatch: pytest.MonkeyPatch) -> None:
    silent = FakeTmux(reference=NOW, quiet_for=None)
    snap = _now_of(_working(None, state="waiting"), silent, monkeypatch)
    assert snap.pane_quiet is None
    assert not needs_at_input_prompt(snap), "only a pane tmux says is quiet is a prompt"
    pending = _now_of(_working(_tail(_tool("toolu_a"))), silent, monkeypatch)
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


# --- the agent alone: what an action reads while its Escape lands -------------------------


def _never(*_args: object, **_kwargs: object) -> Any:
    raise AssertionError("a read of one agent went through its whole project")


class _TailOnly:
    """The live sources as a read of one agent may use them: the tail of its transcript, and
    nothing of its project. Asking for any other source fails the test."""

    def __init__(self, read: Callable[[str], TranscriptTail | None]) -> None:
        self.transcript_tail = read

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"a read of one agent asked its project's sources for {name}")


def _alone_of(
    fleet: Fleet,
    tmux: FakeTmux,
    monkeypatch: pytest.MonkeyPatch,
    label: str = "coder-1",
    read: Callable[[str], TranscriptTail | None] | None = None,
) -> AgentNow:
    """``needs_single_agent_now`` over the facts :func:`_now_of` scans: the rows in the store,
    each derived as the fake fleet lists it, the tails as it holds them (or as ``read`` reads
    them). Listing the project, or asking the live sources for anything but a tail, fails the
    test."""
    statuses = {status.agent.id: status for status in fleet.agents}
    with store_session() as store:
        for status in fleet.agents:
            store.upsert_fleet_agent(status.agent)
    monkeypatch.setattr(fleet_service, "status_of", lambda row: statuses[row.id])
    monkeypatch.setattr(fleet_service, "list_agents", _never)
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: tmux)
    tails = _TailOnly(read or fleet.tails.get)
    monkeypatch.setattr(remote_needs, "live_needs_sources", lambda: tails)
    return needs_single_agent_now(PROJECT, label, now=NOW)


def _showing(fleet: Fleet, screen: list[str]) -> Fleet:
    """``fleet``, its agent's pane showing ``screen`` to the scan."""
    fleet.screen = screen
    return fleet


def _attention_answered() -> Fleet:
    escaped = _tail(newest="interrupted", at=NOW - timedelta(minutes=1), text="Stopping.")
    return _working(escaped, state="attention")


def _lost() -> Fleet:
    row = _row()
    return Fleet(agents=[_status(row, "lost", _session(row, state="attention"))])


@pytest.mark.parametrize(
    ("situation", "tmux"),
    [
        pytest.param(
            lambda: _working(_tail(_tool("toolu_a"))),
            FakeTmux(reference=NOW),
            id="a tool pending in a quiet pane",
        ),
        pytest.param(
            lambda: _working(_tail(_tool("toolu_a"))),
            FakeTmux(reference=NOW, quiet_for=1),
            id="a tool at work",
        ),
        pytest.param(
            lambda: _working(_tail(_tool("toolu_q", "AskUserQuestion", **QUESTION))),
            FakeTmux(reference=NOW, quiet_for=0),
            id="a question in a busy pane",
        ),
        pytest.param(
            lambda: _working(None, state="attention"),
            FakeTmux(reference=NOW),
            id="attention",
        ),
        pytest.param(_attention_answered, FakeTmux(reference=NOW), id="attention escaped"),
        pytest.param(
            lambda: _working(None, state="waiting"),
            FakeTmux(reference=NOW),
            id="waiting at its prompt",
        ),
        pytest.param(
            lambda: _working(_tail(_tool("toolu_a")), state="attention"),
            FakeTmux(reference=NOW, command="zsh"),
            id="a shell in its pane",
        ),
        pytest.param(_lost, FakeTmux(reference=NOW), id="lost"),
        pytest.param(
            lambda: _showing(_working(_tail(_TASK, _ASK)), _at_work(f"● {_TASK.summary}")),
            FakeTmux(reference=NOW, quiet_for=1),
            id="a question queued behind a sub-agent at work",
        ),
    ],
)
def test_the_agent_alone_answers_what_an_action_asks_as_its_projects_scan_does(
    situation: Callable[[], Fleet], tmux: FakeTmux, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every poll after an action's Escape read the project's whole scan, up to 32 of them
    for one Interrupt & tell (review of #243, round 3, 4/13). The agent's own facts are
    enough for what a poll asks: the dialog, the pending tool, the prompt.

    Review of #259, round 10, 9/9: without a screen, a question beside a sub-agent at work
    was rule 8's dialog card here and none in the scan, whose screen showed the work, so
    the guards said "is showing a prompt" of a tool pending."""
    scanned = _now_of(situation(), tmux, monkeypatch)
    alone = _alone_of(situation(), tmux, monkeypatch)
    for predicate in (needs_dialog_open, needs_tool_pending, needs_at_input_prompt):
        assert predicate(alone) == predicate(scanned), predicate.__name__
    assert (alone.pane_is_agent, alone.pane_quiet) == (scanned.pane_is_agent, scanned.pane_quiet)
    assert alone.status == scanned.status and alone.tail == scanned.tail
    assert alone.items == scanned.items, "no board here, so even the ids are the scan's"


def test_without_the_board_a_dialogs_item_is_still_a_dialog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The board names the usage-limit dialog (its notification's text). Without it the item
    is the plain dialog's, with another id: the kind a predicate reads, never a card's id,
    which an action matches before its Escape, on the project's scan."""
    fleet = _working(None, state="attention")
    session = fleet.agents[0].session
    assert session is not None
    paused = "Session paused: usage limit"
    fleet.events = [_event(9, "attention", paused, session=session, at=session.last_seen_at)]
    scanned = _now_of(fleet, FakeTmux(reference=NOW), monkeypatch)
    alone = _alone_of(fleet, FakeTmux(reference=NOW), monkeypatch)
    assert [item.kind for item in scanned.items] == ["limited"]
    assert [item.kind for item in alone.items] == ["permission"]
    assert needs_dialog_open(scanned) and needs_dialog_open(alone)


def test_the_agent_alone_whose_tail_cannot_be_read_is_a_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown is not absent, here as in the project's scan: the read went through a
    ``_NeedsReads`` nobody asked, and a tail it could not read was none (review of #243,
    round 28, 9/10)."""
    from aisquare.services.remote_server import RequestError

    def locked(path: str) -> TranscriptTail | None:
        raise RuntimeError("database is locked")

    fleet = _working(_tail(_tool("toolu_a")), state="attention")
    with pytest.raises(RequestError) as refused:
        _alone_of(fleet, FakeTmux(reference=NOW), monkeypatch, read=locked)
    assert (refused.value.status, refused.value.error) == (503, "unavailable")
    assert refused.value.message == "coder-1 could not be read: database is locked"


def test_the_agent_alone_is_derived_on_its_own_server_and_its_transcript_alone_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``fleet.status_of`` for real, over the store: coder-1's row and session derive it
    ``attention``, only its pane is asked about and only its transcript read. The reviewer's
    pane and transcript, and the project's listing, are never touched."""
    one, other = _row(), _row("reviewer", role="reviewer")
    with store_session() as store:
        for row in (one, other):
            store.upsert_fleet_agent(row)
            store.upsert_session(_session(row, state="attention", seen=NOW - timedelta(seconds=30)))
    read: list[str] = []

    def cached_tail(path: str) -> TranscriptTail:
        read.append(path)
        return _tail(_tool("toolu_a"))

    tmux = FakeTmux(reference=NOW)
    monkeypatch.setattr(fleet_service, "_now", lambda: NOW)
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: tmux)
    monkeypatch.setattr(fleet_service, "list_agents", _never)
    monkeypatch.setattr(remote_needs, "live_needs_sources", lambda: _TailOnly(cached_tail))
    snap = needs_single_agent_now(PROJECT, "coder-1", now=NOW)
    assert snap.status is not None
    assert (snap.status.agent.id, snap.status.state) == (one.id, "attention")
    assert set(tmux.asked) == {one.pane_id}, "the reviewer's pane is never asked about"
    assert read == ["/transcripts/coder-1.jsonl"]
    assert snap.pane_is_agent and snap.pane_quiet is True
    assert [item.kind for item in snap.items] == ["permission"] and needs_dialog_open(snap)


def test_the_agent_alone_is_the_newest_row_holding_the_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restart since the Escape: the label reads as the newcomer, whom the action's pin
    then refuses. A label no row holds is no agent."""
    old = _row(ended=NOW - timedelta(seconds=5), exit_status=0)
    new = _row(created=NOW - timedelta(seconds=2), row_id="agt_new")
    with store_session() as store:
        store.upsert_fleet_agent(old)
        store.upsert_fleet_agent(new)
    tmux = FakeTmux(reference=NOW)
    monkeypatch.setattr(fleet_service, "_now", lambda: NOW)
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: tmux)
    snap = needs_single_agent_now(PROJECT, "coder-1", now=NOW)
    assert snap.status is not None and snap.status.agent.id == "agt_new"
    with pytest.raises(fleet_service.NoSuchAgent):
        needs_single_agent_now(PROJECT, "ghost", now=NOW)


def test_the_agent_alone_never_asks_about_the_pane_of_a_row_that_ended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The agent exited while its Escape landed. Its row reads ``exited``, and the pane under
    its id, which the next server may have given another agent, is never asked about: no
    dialog, no prompt, nothing to type into."""
    row = _row(ended=NOW - timedelta(seconds=1), exit_status=0)
    with store_session() as store:
        store.upsert_fleet_agent(row)
        store.upsert_session(_session(row, state="attention", seen=NOW - timedelta(seconds=30)))
    tmux = FakeTmux(reference=NOW)
    monkeypatch.setattr(fleet_service, "_now", lambda: NOW)
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: tmux)
    monkeypatch.setattr(remote_needs, "_needs_cached_tail", lambda path: _tail(_tool("toolu_a")))
    snap = needs_single_agent_now(PROJECT, "coder-1", now=NOW)
    assert snap.status is not None and snap.status.state == "exited"
    assert tmux.asked == [] and not snap.pane_is_agent and snap.pane_quiet is None
    assert snap.items == () and snap.tail is None
    for predicate in (needs_dialog_open, needs_tool_pending, needs_at_input_prompt):
        assert not predicate(snap), predicate.__name__


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
        project = store.onboard_project(_rooted(root))
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


def test_the_cache_of_tails_keeps_the_newest_it_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One entry a transcript path, for every agent a long-running server ever scanned: past
    its bound the oldest read goes first."""
    monkeypatch.setattr(remote_needs, "_tails", {})
    monkeypatch.setattr(remote_needs, "_TAILS_KEPT", 3)
    paths = [tmp_path / f"coder-{n}.jsonl" for n in range(5)]
    for path in paths:
        path.write_text(json.dumps(_asking_record(NOW)) + "\n", encoding="utf-8")
    tail_of = remote_needs.live_needs_sources().transcript_tail
    for path in paths:
        assert tail_of(str(path)) is not None
    assert list(remote_needs._tails) == [str(path) for path in paths[2:]]


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
    dormant = _rooted(tmp_path / "dormant")
    active = _rooted(tmp_path / "active")
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
    project = _rooted(tmp_path / "busy")

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


def test_the_live_sources_take_a_turn_that_died_on_an_api_error_for_one_that_needs_you(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the team's own hook (``hook_stop_failure``) and the store: an expired login
    is a ``failed`` card, and the agent's next prompt ends it."""
    from aisquare.services import team as team_service

    hour_ago = datetime.now(UTC) - timedelta(hours=1)
    root = tmp_path / "alpha"
    with store_session() as store:
        project = store.onboard_project(_rooted(root))
        store.upsert_session(
            TeamSession(
                id="ses_c", project_id=project.id, role="coder", started_at=hour_ago,
                last_seen_at=hour_ago, state="working",
            )
        )  # fmt: skip
        store.upsert_fleet_agent(
            FleetAgent(
                id="agt_c", project_id=project.id, label="coder-1", role="coder", pane_id="%1",
                session_id="ses_c", cwd=root, created_at=hour_ago,
            )
        )  # fmt: skip
    team_service.hook_stop_failure(
        "ses_c",
        error="authentication_failed",
        message="Login expired · Please run /login",
        details=None,
    )
    tmux = FakeTmux()
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: tmux)
    items = scan_needs_you(remote_needs.live_needs_sources(), now=datetime.now(UTC), dismissed=())
    assert [(item.kind, item.reason) for item in items] == [
        ("failed", "coder-1's turn failed (authentication_failed)")
    ]
    with store_session() as store:
        store.touch_session("ses_c", state="working")  # its next prompt
    assert (
        scan_needs_you(remote_needs.live_needs_sources(), now=datetime.now(UTC), dismissed=()) == []
    )


@pytest.mark.parametrize("hub_listed", [False, True])
def test_under_a_team_hub_the_scan_reads_the_hubs_board_once_for_its_projects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hub_listed: bool
) -> None:
    """Under ``AISQUARE_TEAM_HUB`` the team writes every session and event to the hub's
    project, and the scan read each project's board by the project's own id: a manager's
    question, a coder's failed turn, the words of a dialog never became a card or a push,
    while the Board tab showed them (sweep 4 of #243). The scan reads the board where the
    team writes it, as the Board tab does, and a question on a board two projects share is
    one card, the asking row's project's, whether or not the hub is a listed project."""
    from aisquare.services import team as team_service

    now = datetime.now(UTC)
    hour_ago = now - timedelta(hours=1)
    hub = _rooted(tmp_path / "hub")
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(hub.root))
    with store_session() as store:
        alpha = store.onboard_project(_rooted(tmp_path / "alpha"))
        beta = store.onboard_project(_rooted(tmp_path / "beta"))
        if hub_listed:
            store.onboard_project(hub)
        else:
            store.ensure_project(hub)  # as the hooks' first write makes it
        rows = ((alpha, "manager", "manager", "%1"), (beta, "coder-1", "coder", "%2"))
        for project, label, role, pane in rows:
            store.upsert_session(
                TeamSession(
                    id=f"ses_{label}", project_id=hub.id, role=role, label=label,
                    started_at=hour_ago, last_seen_at=hour_ago, state="working",
                )
            )  # fmt: skip
            store.upsert_fleet_agent(
                FleetAgent(
                    id=f"agt_{label}", project_id=project.id, label=label, role=role,
                    pane_id=pane, session_id=f"ses_{label}", cwd=project.root, created_at=hour_ago,
                )
            )  # fmt: skip
        store.add_team_event(
            TeamEvent(
                id="evt_q", project_id=hub.id, session_id="ses_manager", kind="question",
                text="Ship on Friday?", created_at=now - timedelta(minutes=1),
            )
        )  # fmt: skip
    team_service.hook_stop_failure(
        "ses_coder-1", error="overloaded", message="Overloaded · try again", details=None
    )
    tmux = FakeTmux()
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: tmux)
    items = scan_needs_you(remote_needs.live_needs_sources(), now=now, dismissed=())
    assert [(item.kind, item.project_id, item.agent) for item in items] == [
        ("board_question", alpha.id, "manager"),
        ("failed", beta.id, "coder-1"),
    ]
    assert items[0].detail["text"] == "Ship on Friday?"
    assert [item.kind for item in needs_agent_now(beta, "coder-1").items] == ["failed"]


def test_under_a_team_hub_a_resumed_agent_at_the_trust_question_is_a_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restart resumed coder-1's session, on the hub's board, on an account that never
    trusted the folder, and its pane stopped at the question. The listing read it there; the
    scan derived the row again with its session by ``_status`` alone, which read the old
    session's ``waiting``: no card and no push (review of #259, round 13)."""
    now = datetime.now(UTC)
    hub = _rooted(tmp_path / "hub")
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(hub.root))
    with store_session() as store:
        alpha = store.onboard_project(_rooted(tmp_path / "alpha"))
        store.ensure_project(hub)
        store.upsert_session(
            TeamSession(
                id="ses_coder-1", project_id=hub.id, role="coder", label="coder-1",
                started_at=now - timedelta(hours=1), last_seen_at=now - timedelta(hours=1),
                state="waiting",
            )
        )  # fmt: skip
        store.upsert_fleet_agent(
            FleetAgent(
                id="agt_coder-1", project_id=alpha.id, label="coder-1", role="coder",
                pane_id="%2", session_id="ses_coder-1", cwd=alpha.root,
                created_at=now - timedelta(minutes=1),
            )
        )  # fmt: skip
    tmux = FakeTmux()
    tmux.screen = (
        (Path(__file__).parent / "fixtures" / "claude_code" / "trust_prompt_2.1.299.txt")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: tmux)

    items = scan_needs_you(remote_needs.live_needs_sources(), now=now, dismissed=())

    assert [(item.kind, item.agent) for item in items] == [("permission", "coder-1")]
    assert "waiting for folder trust" in items[0].reason


@pytest.mark.parametrize(
    ("spawned", "remote"),
    [("no hub", "no hub"), ("hub", "hub"), ("hub", "no hub"), ("no hub", "hub")],
    ids=lambda under: under,
)
def test_a_coder_whose_hand_over_never_started_its_replacement_is_reported_under_a_hub_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spawned: str, remote: str
) -> None:
    """A coder's switch whose replacement never started is a card and a push, the case
    ``HANDOVER_FAILED`` is for, with a hub and without: the scan reads the exit where the
    fleet writes it (review of #243, round 18, 1/4). That is the board the row recorded at
    spawn (``fleet._emit_exit``), the hub's under one, where its manager reads it too
    (review of #259, round 24, 3/6), whatever hub Remote itself runs under: read only from
    the board Remote's environment resolves, it was lost when the two differ (round 25,
    6/10)."""
    hour_ago = datetime.now(UTC) - timedelta(hours=1)
    stopped = datetime.now(UTC) - timedelta(minutes=10)
    with store_session() as store:
        alpha = store.onboard_project(_rooted(tmp_path / "alpha"))
        hub = _rooted(tmp_path / "hub")
        store.ensure_project(hub)
        board = hub if spawned == "hub" else alpha
        if remote == "hub":
            monkeypatch.setenv("AISQUARE_TEAM_HUB", str(hub.root))
        store.upsert_session(
            TeamSession(
                id="ses_c", project_id=board.id, role="coder", label="coder-1",
                started_at=hour_ago, last_seen_at=stopped, state="working",
            )
        )  # fmt: skip
        row = store.upsert_fleet_agent(
            FleetAgent(
                id="agt_c", project_id=alpha.id, label="coder-1", role="coder", pane_id="%1",
                session_id="ses_c", cwd=alpha.root, created_at=hour_ago, ended_at=stopped,
                exit_status=0, board=board.id,
            )
        )  # fmt: skip
        fleet_service._emit_exit(store, row, why=fleet_service.HANDOVER_FAILED)
    tmux = FakeTmux()
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: tmux)
    items = scan_needs_you(remote_needs.live_needs_sources(), now=datetime.now(UTC), dismissed=())
    assert [(item.kind, item.project_id, item.agent, item.reason) for item in items] == [
        ("crashed", alpha.id, "coder-1", "coder-1 stopped, and its replacement did not start")
    ]


@pytest.mark.parametrize(
    ("spawned", "remote"),
    [("no hub", "no hub"), ("hub", "hub"), ("hub", "no hub"), ("no hub", "hub")],
    ids=lambda under: under,
)
def test_an_agents_limit_and_failed_turn_are_read_on_the_board_it_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spawned: str, remote: str
) -> None:
    """Review of #259, round 26, 3/11: round 25 read an agent's exit on the board its row
    recorded, and its own ``limited`` and ``attention`` events and its ``turn_failed`` were
    still read on the board Remote's own environment resolves. Remote without the hub the
    agents were spawned under found no failed turn, so a coder whose turn died on an API
    error was no card and no push, and no ``limited`` event, so a parked agent's item was
    keyed on another and came back under a new id. Each is read where the row recorded."""
    from aisquare.services import team as team_service

    save_config(AppConfig(accounts=AccountsSettings(on_limit="wait")))
    with store_session() as store:
        alpha = store.onboard_project(_rooted(tmp_path / "alpha"))
        hub = _rooted(tmp_path / "hub")
        store.ensure_project(hub)
        board = hub if spawned == "hub" else alpha
        _seat_up(store, _Seat("alpha", "coder-1", limited=True), alpha, board)
        _seat_up(store, _Seat("alpha", "coder-2", waits=True), alpha, board)
        limited = team_service._emit(
            store, board.id, "limited", "coder-1 hit its limit", session_id="ses_alpha_coder-1"
        )
        failed = team_service._emit(
            store, board.id, "turn_failed", "overloaded: busy", session_id="ses_alpha_coder-2"
        )
    if remote == "hub":
        monkeypatch.setenv("AISQUARE_TEAM_HUB", str(hub.root))
    tmux = FakeTmux()
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: tmux)

    items = scan_needs_you(remote_needs.live_needs_sources(), now=datetime.now(UTC), dismissed=())

    assert sorted((item.kind, item.agent, item.id) for item in items) == [
        ("failed", "coder-2", needs_item_id(alpha.id, "failed", str(failed.seq))),
        ("limited", "coder-1", needs_item_id(alpha.id, "limited", str(limited.seq))),
    ]


def test_a_live_manager_on_another_board_keeps_nothing_on_this_one_off_the_phone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 26, 4/11: alpha's manager was spawned under a hub and reads its
    board, and two coders spawned without it read alpha's own. A coder's question there, and
    another's crash, nudged a manager whose delta holds neither, and Remote, run without the
    hub, saw a live manager and made no card: nobody heard of either. A manager takes only
    the items on the board it reads (``fleet.nudge_manager``'s ``board``)."""
    with store_session() as store:
        alpha = store.onboard_project(_rooted(tmp_path / "alpha"))
        hub = _rooted(tmp_path / "hub")
        store.ensure_project(hub)
        _seat_up(store, _manager_of("alpha"), alpha, hub)
        _seat_up(store, _Seat("alpha", "coder-1", asks=True), alpha, alpha)
        _seat_up(store, _Seat("alpha", "coder-2", end="crash"), alpha, alpha)
    tmux = FakeTmux()
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: tmux)

    items = scan_needs_you(remote_needs.live_needs_sources(), now=datetime.now(UTC), dismissed=())

    assert sorted((item.kind, item.project_id, item.agent) for item in items) == [
        ("board_question", alpha.id, "coder-1"),
        ("crashed", alpha.id, "coder-2"),
    ]


@pytest.mark.parametrize(
    ("manager", "coder", "remote", "card"),
    [
        ("hub", "alpha", "hub", True),  # the coder spawned without its manager's hub
        ("alpha", "hub", "no hub", True),  # the mirror: the coder under a hub, its manager not
        ("hub", "hub", "no hub", False),  # the manager reads the board the coder asked on
        ("alpha", "alpha", "hub", False),
    ],
    ids=lambda under: str(under),
)
def test_a_coders_question_is_read_on_the_board_its_row_recorded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manager: str,
    coder: str,
    remote: str,
    card: bool,
) -> None:
    """Review of #259, round 27, 1/7: the scan read questions and results only on the board
    Remote's own environment resolves. alpha's manager spawned from ``asq`` under a hub, a
    coder spawned without it asking on alpha's own board, and Remote under the hub: the
    coder's nudge rightly woke no manager, as it reads the hub's, and the scan never read the
    question, so nobody heard of it. Each board a row recorded is read for that row's posts,
    and a post is the manager's only when it reads that board."""
    with store_session() as store:
        alpha = store.onboard_project(_rooted(tmp_path / "alpha"))
        hub = _rooted(tmp_path / "hub")
        store.ensure_project(hub)
        on = {"hub": hub, "alpha": alpha}
        _seat_up(store, _manager_of("alpha"), alpha, on[manager])
        _seat_up(store, _Seat("alpha", "coder-1", asks=True), alpha, on[coder])
    if remote == "hub":
        monkeypatch.setenv("AISQUARE_TEAM_HUB", str(hub.root))
    tmux = FakeTmux()
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: tmux)

    items = scan_needs_you(remote_needs.live_needs_sources(), now=datetime.now(UTC), dismissed=())

    cards = [(item.kind, item.project_id, item.agent) for item in items]
    assert cards == ([("board_question", alpha.id, "coder-1")] if card else []), cards


def test_a_question_on_a_board_its_row_recorded_is_answered_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 27, 1/7: read on a board the scan does not read whole, a coder's
    question is cleared as on its own: by the human's later reply to it, on that board."""
    from aisquare.services import team as team_service

    with store_session() as store:
        alpha = store.onboard_project(_rooted(tmp_path / "alpha"))
        hub = _rooted(tmp_path / "hub")
        store.ensure_project(hub)
        _seat_up(store, _Seat("alpha", "coder-1", asks=True), alpha, alpha)
        team_service._emit(store, alpha.id, "note", "main", to_role="coder-1")
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(hub.root))
    tmux = FakeTmux()
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: tmux)

    items = scan_needs_you(remote_needs.live_needs_sources(), now=datetime.now(UTC), dismissed=())

    assert items == []


def test_a_board_its_row_recorded_is_read_for_what_the_scan_takes_of_it_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 28, 8/10: a board a row recorded, which the scan does not read
    whole, was read whole for the day (``board_since``) and filtered here to its rows' posts,
    the human's replies and the fleet's kinds: on a board other sessions write, every scan
    read a day of all their questions, results and decisions. The store is asked for those
    alone (``board_posts``), and the card is the same."""
    from aisquare.services import team as team_service

    with store_session() as store:
        alpha = store.onboard_project(_rooted(tmp_path / "alpha"))
        hub = _rooted(tmp_path / "hub")
        store.ensure_project(hub)
        _seat_up(store, _manager_of("alpha"), alpha, hub)
        _seat_up(store, _Seat("alpha", "coder-1", asks=True), alpha, alpha)
        for n in range(5):  # another session's posts on alpha's board, which no row holds
            team_service._emit(store, alpha.id, "question", f"#{n}?", session_id="ses_by_hand")
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(hub.root))
    tmux = FakeTmux()
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: tmux)
    live = remote_needs.live_needs_sources()
    days: list[str] = []
    posts: dict[str, list[str]] = {}

    def day(board: str, since: datetime) -> list[TeamEvent]:
        days.append(board)
        return live.board_since(board, since)

    def board_posts(board: str, since: datetime, sessions: Collection[str]) -> list[TeamEvent]:
        read = live.board_posts(board, since, sessions)
        posts[board] = [event.text for event in read]
        return read

    sources = replace(live, board_since=day, board_posts=board_posts)
    items = scan_needs_you(sources, now=datetime.now(UTC), dismissed=())

    assert [(item.kind, item.project_id, item.agent) for item in items] == [
        ("board_question", alpha.id, "coder-1")
    ]
    assert set(days) == {hub.id}, "alpha's board is not read whole"
    assert posts == {alpha.id: ["Which branch?"]}


def test_a_question_read_on_a_board_another_project_reads_whole_is_one_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 27, 1/7: beta's coder spawned to read alpha's board asks there.
    beta's scan reads it, as a board its row recorded, and alpha's scan reads alpha's board
    whole, where a post no row of alpha's holds is alpha's card too. It is one card, the
    poster's project's."""
    with store_session() as store:
        alpha = store.onboard_project(_rooted(tmp_path / "alpha"))
        beta = store.onboard_project(_rooted(tmp_path / "beta"))
        _seat_up(store, _Seat("beta", "coder-1", asks=True), beta, alpha)
    tmux = FakeTmux()
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: tmux)

    items = scan_needs_you(remote_needs.live_needs_sources(), now=datetime.now(UTC), dismissed=())

    assert [(item.kind, item.project_id, item.agent) for item in items] == [
        ("board_question", beta.id, "coder-1")
    ]


def test_a_failed_scans_card_on_a_board_others_share_leaves_every_feed_standing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 28, 1/10: R and Q both read board H whole, R being H's own
    project and Q one rooted below it. P's manager, spawned under H, asks there, and P's scan
    reads H apart. When P's scan failed, its last card stood, met R's and Q's copies of the
    same question on H, and was ranked by its index among H's writers, which P is not one of:
    ``ValueError``, so every scan raised and no project's feed changed until P's scan worked
    again. P's card is the one card, and Q's own question still renders beside it."""
    import sqlite3

    with store_session() as store:
        r = store.onboard_project(_rooted(tmp_path / "repo"))
        q = store.onboard_project(_rooted(tmp_path / "api"))
        p = store.onboard_project(_rooted(tmp_path / "p"))
        _seat_up(store, _manager_of("p", asks=True), p, r)
        _seat_up(store, _Seat("q", "coder-1", asks=True), q, r)
    tmux = FakeTmux()
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: tmux)
    live = remote_needs.live_needs_sources()
    shared = replace(
        live, board_project=lambda project: r.id if project.id in (r.id, q.id) else project.id
    )

    def ended(project_id: str, since: datetime) -> list[FleetAgent]:
        if project_id == p.id:
            raise sqlite3.OperationalError("database is locked")
        return shared.ended_agents(project_id, since)

    now = datetime.now(UTC)
    first = scan_needs_you(shared, now=now, dismissed=())
    cards = sorted((item.kind, item.project_id, item.agent) for item in first)
    assert cards == sorted(
        [("board_question", p.id, "manager"), ("board_question", q.id, "coder-1")]
    ), cards

    again = scan_needs_you(replace(shared, ended_agents=ended), now=now, dismissed=(), kept=first)

    assert sorted(item.id for item in again) == sorted(item.id for item in first)


def test_a_question_read_apart_is_one_card_while_its_posters_scan_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 28, 3/10: Remote runs with no hub, and the hub's project is
    listed. P's manager, spawned under the hub, asks on the hub's board, which P's scan reads
    apart and the hub's own reads whole. Only a scan that succeeds names its posts, so while
    P's scan failed, P's last card stood beside the hub project's copy, a second card with an
    id of its own, pushed as new. P's card is still the one card."""
    import sqlite3

    monkeypatch.delenv("AISQUARE_TEAM_HUB", raising=False)
    with store_session() as store:
        hub = store.onboard_project(_rooted(tmp_path / "hub"))
        p = store.onboard_project(_rooted(tmp_path / "p"))
        _seat_up(store, _manager_of("p", asks=True), p, hub)
    tmux = FakeTmux()
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: tmux)
    live = remote_needs.live_needs_sources()

    def ended(project_id: str, since: datetime) -> list[FleetAgent]:
        if project_id == p.id:
            raise sqlite3.OperationalError("database is locked")
        return live.ended_agents(project_id, since)

    now = datetime.now(UTC)
    first = scan_needs_you(live, now=now, dismissed=())
    assert [(item.kind, item.project_id, item.agent) for item in first] == [
        ("board_question", p.id, "manager")
    ]

    again = scan_needs_you(replace(live, ended_agents=ended), now=now, dismissed=(), kept=first)

    assert [item.id for item in again] == [first[0].id]


def test_a_result_on_a_board_the_stopped_manager_never_read_stays_a_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 28, 4/10: a result to a manager that stopped is news, not a
    card, on the premise that the manager took it while it lived. P's manager reads P's
    board, and coder-1, spawned under a hub, reports to it on the hub's: a card, as no
    manager reads there. Once the manager stopped, the stop's time was passed for every
    board the scan read, so the result left the phone, though no manager ever read it."""
    from aisquare.services import team as team_service

    monkeypatch.delenv("AISQUARE_TEAM_HUB", raising=False)
    with store_session() as store:
        p = store.onboard_project(_rooted(tmp_path / "p"))
        hub = _rooted(tmp_path / "hub")
        store.ensure_project(hub)
        _seat_up(store, _manager_of("p"), p, p)
        _seat_up(store, _Seat("p", "coder-1"), p, hub)
        team_service._emit(
            store, hub.id, "result", "tests pass", session_id="ses_p_coder-1", to_role="manager"
        )
    tmux = FakeTmux()
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: tmux)

    def results() -> list[tuple[str, str | None]]:
        items = scan_needs_you(
            remote_needs.live_needs_sources(), now=datetime.now(UTC), dismissed=()
        )
        return [(item.kind, item.agent) for item in items if item.kind == "board_result"]

    assert results() == [("board_result", "coder-1")], "no manager reads the hub's board"
    with store_session() as store:
        store.end_fleet_agent("agt_p_manager")

    assert results() == [("board_result", "coder-1")]


def test_a_phones_reply_to_a_card_is_posted_on_the_board_its_row_asked_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 28, 2/10: P's manager, spawned under a hub, asks the human on the
    hub's board, and Remote runs without the hub. The phone's Reply was ``note`` with
    ``project: P``, written in P's root under Remote's environment: on P's own board, which
    the manager does not read, so the question stayed open where it was asked, and the page
    dismissed the card on the 200. The reply names the card's row, and goes to the board that
    row recorded; a row the project does not hold is refused, and nothing is written."""
    from aisquare.services.remote_server import RequestError, live_writes

    monkeypatch.delenv("AISQUARE_TEAM_HUB", raising=False)
    with store_session() as store:
        p = store.onboard_project(_rooted(tmp_path / "p"))
        other = store.onboard_project(_rooted(tmp_path / "other"))
        hub = _rooted(tmp_path / "hub")
        store.ensure_project(hub)
        _seat_up(store, _manager_of("p", asks=True), p, hub)
        _seat_up(store, _Seat("other", "coder-1"), other, other)
    tmux = FakeTmux()
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: tmux)

    def cards() -> list[tuple[str, str | None]]:
        items = scan_needs_you(
            remote_needs.live_needs_sources(), now=datetime.now(UTC), dismissed=()
        )
        return [(item.kind, item.agent) for item in items]

    def notes() -> dict[str, list[str]]:
        with store_session() as store:
            boards = (p.id, hub.id, other.id)
            return {
                board: [e.text for e in store.recent_events(board, limit=9) if e.kind == "note"]
                for board in boards
            }

    reply = {"text": "Postgres", "kind": "note", "project": p.id, "to": "manager"}
    assert cards() == [("board_question", "manager")]
    with pytest.raises(RequestError) as refused:
        live_writes().handlers["note"]({**reply, "agent_id": "agt_other_coder-1"})
    assert refused.value.status == 404, "another project's row"
    assert notes() == {p.id: [], hub.id: [], other.id: []}

    live_writes().handlers["note"]({**reply, "agent_id": "agt_p_manager"})

    assert notes() == {p.id: [], hub.id: ["Postgres"], other.id: []}
    assert cards() == [], "answered where it was asked"


@dataclass(frozen=True)
class _Seat:
    """One agent of a board: its fleet project, label and role; whether a fleet row holds it
    (``False``: a manager session started outside the fleet, on its project's board); how it
    ended, ``None`` while it works; whether it waits on its usage limit, or at its prompt;
    and whether it asks the manager on the board (``question --to manager``)."""

    where: str
    label: str
    role: str = "coder"
    rowed: bool = True
    end: str | None = None
    """``crash`` (exit 1), ``exit`` (exit 0), ``hand-over`` (exit 0, its replacement never
    started), ``stop`` (no status, as a kill leaves it) or ``lost`` (no status and no exit
    announced, as a pane gone leaves it)."""
    limited: bool = False
    waits: bool = False
    asks: bool = False
    registered: bool = True
    """``False``: its row has a session id that never reached the board (no aisquare hooks)."""
    pruned: bool = False
    """Its board session was ended under its live row, as ``team prune`` ends an idle one."""


def _manager_of(where: str, label: str = "manager", **kw: Any) -> _Seat:
    return _Seat(where, label, role="manager", **kw)


def _seat_up(store: Any, seat: _Seat, project: ProjectInfo, board: ProjectInfo) -> None:
    """``seat``'s session on ``board`` and its row in ``project``, spawned to read ``board``,
    as of an hour ago, and what it ended with and asked ten minutes ago."""
    from aisquare.services import team as team_service

    now = datetime.now(UTC)
    stopped = now - timedelta(minutes=10)
    session = f"ses_{seat.where}_{seat.label}"
    if seat.registered:
        store.upsert_session(
            TeamSession(
                id=session, project_id=board.id, role=seat.role, label=seat.label,
                started_at=now - timedelta(hours=1),
                state="limited" if seat.limited else "waiting" if seat.waits else "working",
                limit_resets_at=now + timedelta(hours=4) if seat.limited else None,
                last_seen_at=stopped if seat.end else now - timedelta(minutes=1),
                ended_at=stopped if seat.pruned else None,
            )
        )  # fmt: skip
    if seat.asks:
        team_service._emit(
            store, board.id, "question", "Which branch?", session_id=session, to_role="manager"
        )
    if not seat.rowed:
        return
    status = {"crash": 1, "exit": 0, "hand-over": 0}.get(seat.end or "")
    row = store.upsert_fleet_agent(
        FleetAgent(
            id=f"agt_{seat.where}_{seat.label}", project_id=project.id, label=seat.label,
            role=seat.role, pane_id=f"%{seat.where}-{seat.label}", session_id=session,
            cwd=project.root, created_at=now - timedelta(hours=1), task_id="tsk_open",
            ended_at=stopped if seat.end else None, exit_status=status, board=board.id,
        )
    )  # fmt: skip
    if seat.end and seat.end != "lost":
        why = fleet_service.HANDOVER_FAILED if seat.end == "hand-over" else None
        fleet_service._emit_exit(store, row, why=why)


def _seated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seats: tuple[_Seat, ...], *, hub: bool | str
) -> dict[str, ProjectInfo]:
    """alpha, beta, gamma and delta onboarded, every one of ``seats`` up, on a hub's board or
    each on its own project's. A name for ``hub``: that project's root is the hub."""
    with store_session() as store:
        projects = {
            name: store.onboard_project(_rooted(tmp_path / name))
            for name in ("alpha", "beta", "gamma", "delta")
        }
        board = projects[hub] if isinstance(hub, str) else _rooted(tmp_path / "hub")
        if hub:
            monkeypatch.setenv("AISQUARE_TEAM_HUB", str(board.root))
            store.ensure_project(board)
        for seat in seats:
            project = projects[seat.where]
            _seat_up(store, seat, project, board if hub else project)
    return projects


@pytest.mark.parametrize(
    ("hub", "seats", "expected"),
    [
        pytest.param(
            False,
            (
                _manager_of("alpha"),
                _Seat("alpha", "coder-1", asks=True),
                _Seat("alpha", "coder-2", end="crash"),
                _Seat("alpha", "coder-3", limited=True),
            ),
            [("limited", "alpha", "coder-3", 90)],
            id="no hub, a live manager the nudge wakes",
        ),
        pytest.param(
            False,
            (
                _Seat("alpha", "coder-1", asks=True),
                _Seat("alpha", "coder-2", end="crash"),
                _Seat("alpha", "coder-3", limited=True),
            ),
            [
                ("board_question", "alpha", "coder-1", 0),
                ("crashed", "alpha", "coder-2", 30),
                ("limited", "alpha", "coder-3", 0),
            ],
            id="no hub, no manager",
        ),
        pytest.param(
            False,
            (
                _manager_of("alpha", rowed=False),
                _manager_of("alpha", "manager-2"),
                _Seat("alpha", "coder-1", asks=True),
                _Seat("alpha", "coder-2", end="crash"),
            ),
            [("board_question", "alpha", "coder-1", 0), ("crashed", "alpha", "coder-2", 30)],
            id="no hub, managers the nudge never wakes: no row, a numbered seat",
        ),
        pytest.param(
            False,
            (
                _manager_of("alpha", end="crash"),
                _manager_of("alpha", "manager-2"),
                _Seat("alpha", "coder-1", asks=True),
            ),
            [("board_question", "alpha", "coder-1", 0), ("manager_down", "alpha", "manager", 60)],
            id="no hub, a numbered seat that runs hides no crash of the manager",
        ),
        pytest.param(
            False,
            (_manager_of("alpha"), _manager_of("alpha", "manager-2", end="crash")),
            [],
            id="no hub, a numbered seat's crash, the live manager woken for it",
        ),
        pytest.param(
            False,
            (_manager_of("alpha", "manager-2", end="crash"), _Seat("alpha", "coder-1")),
            [("crashed", "alpha", "manager-2", 30)],
            id="no hub, a numbered seat's crash, no manager",
        ),
        pytest.param(
            True,
            (
                _manager_of("alpha"),
                _Seat("alpha", "coder-1", asks=True),
                _Seat("beta", "coder-2", asks=True),
                _manager_of("gamma"),
                _Seat("gamma", "coder-3", end="crash"),
            ),
            [("board_question", "beta", "coder-2", 0)],
            id="hub, a live manager takes its own agents' items, not another project's",
        ),
        pytest.param(
            True,
            (
                _manager_of("alpha"),
                _Seat("alpha", "coder-1", asks=True, rowed=False),
                _Seat("alpha", "coder-2", asks=True),
            ),
            [("board_question", "alpha", None, 0)],
            id="hub, a session no row holds asks: a card, its manager unknown",
        ),
        pytest.param(
            True,
            (
                _Seat("alpha", "coder-1", limited=True),
                _Seat("alpha", "coder-2", end="hand-over"),
            ),
            [
                ("crashed", "alpha", "coder-2", 30),
                ("limited", "alpha", "coder-1", 0),
            ],
            id="hub, a limit and a coder's hand-over that never started its replacement",
        ),
        pytest.param(
            True,
            (
                _manager_of("alpha"),
                _Seat("alpha", "coder-1", limited=True),
                _Seat("alpha", "coder-2", end="hand-over"),
            ),
            [("limited", "alpha", "coder-1", 90)],
            id="hub, the same with its live manager: its own, as without a hub",
        ),
        pytest.param(
            True,
            (_manager_of("alpha", end="hand-over"), _Seat("alpha", "coder-1")),
            [("manager_down", "alpha", "manager", 60)],
            id="hub, the manager's hand-over that never started its replacement",
        ),
        pytest.param(
            True,
            (_manager_of("alpha", end="hand-over"), _Seat("beta", "coder-1")),
            [("manager_down", "alpha", "manager", 60)],
            id="hub, the manager's failed hand-over, its crew in another project",
        ),
        pytest.param(
            True,
            (
                _manager_of("alpha"),
                _Seat("alpha", "coder-1"),
                _manager_of("beta", end="stop"),
                _Seat("beta", "coder-2"),
                _manager_of("gamma", end="stop"),
                _manager_of("delta", end="exit"),
            ),
            [("manager_down", "beta", "manager", 60), ("manager_down", "gamma", "manager", 60)],
            id="hub, a manager stopped is down whatever its crew, one that exited is done",
        ),
        pytest.param(
            "alpha",
            (_manager_of("alpha", end="stop"), _Seat("beta", "coder-1")),
            [("manager_down", "alpha", "manager", 60)],
            id="hub in alpha, its manager stopped, its crew in another project",
        ),
        pytest.param(
            False,
            (
                _manager_of("alpha", end="stop"),
                _Seat("alpha", "coder-1", waits=True),
                _manager_of("beta", end="hand-over"),
                _Seat("beta", "coder-2", waits=True),
                _manager_of("gamma", end="lost"),
                _manager_of("delta", end="exit"),
                _Seat("delta", "coder-4"),
            ),
            [
                ("manager_down", "alpha", "manager", 60),
                ("manager_down", "beta", "manager", 60),
                ("manager_down", "gamma", "manager", 60),
            ],
            id="no hub, whatever its crew: a manager's stop is down, but a clean exit",
        ),
    ],
)
def test_a_live_manager_keeps_its_own_agents_items_off_the_phone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hub: bool | str,
    seats: tuple[_Seat, ...],
    expected: list[tuple[str, str, str, int]],
) -> None:
    """Which manager would handle an item on a board several projects share had no answer
    that held: keyed by the board, the project or the spawner, each left items no card
    showed and no nudge reached (review of #243, rounds 18 to 21; 1/8 of round 21 a
    silent stall). A project's live ``manager`` row, the one the fleet's nudge wakes for
    its agents on whatever board they write, keeps their question to it and their crash
    off the phone and holds back a limit's push; nothing else does. Under
    ``AISQUARE_TEAM_HUB`` that is as without one since the nudge wakes the writer's row's
    project's manager (review of #259, round 25, 7/10): another project's agents' items,
    and a post by a session no row holds, are cards, each once. A crash of that
    ``manager`` is ``manager_down`` whatever numbered seat runs (round 22, 5/7), and a
    numbered seat's own crash is a crash like any agent's. A manager that is down is
    its own project's, from that project's rows: stopped whatever its crew, which may be
    another project's (round 22, 2/7), the hub's own project's too (round 23, 1/7). Any
    stop but a clean exit is down whatever its crew, with a hub or not (round 23, 3/7;
    round 25, 2/4). Each
    tuple is a card's kind, project, agent and the seconds its push waits. An action's
    check of each agent finds what the scan found, and no more."""
    # Waiting on a limit, so a live manager is all a limit's push waits for.
    save_config(AppConfig(accounts=AccountsSettings(on_limit="wait")))
    projects = _seated(tmp_path, monkeypatch, seats, hub=hub)
    tmux = FakeTmux()
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: tmux)
    now = datetime.now(UTC)
    items = scan_needs_you(remote_needs.live_needs_sources(), now=now, dismissed=())
    names = {project.id: name for name, project in projects.items()}
    assert [
        (item.kind, names[item.project_id], item.agent, (item.push_after - item.since).seconds)
        for item in items
        if item.push_after is not None
    ] == expected
    for seat in seats:
        if seat.rowed:
            project = projects[seat.where]
            snap = needs_agent_now(project, seat.label, now=now)
            scanned = [i for i in items if i.project_id == project.id and i.agent == seat.label]
            assert sorted(i.id for i in snap.items) == sorted(i.id for i in scanned), seat


def _board_cards(sources: NeedsSources) -> list[tuple[str, str | None]]:
    items = scan_needs_you(sources, now=datetime.now(UTC), dismissed=())
    return [(item.kind, item.agent) for item in items if item.kind.startswith("board_")]


_ASKED = [("board_question", "coder-1")]
_REPORTED = [("board_result", "coder-3"), ("board_result", "coder-4")]
_GOING = {
    "it can take them": (_manager_of("alpha"), []),
    "it exits cleanly": (_manager_of("alpha", end="exit"), _ASKED),
    "it is killed": (_manager_of("alpha", end="stop"), _ASKED),
    "it dies with no status": (_manager_of("alpha", end="lost"), _ASKED),
    "it crashes": (_manager_of("alpha", end="crash"), _ASKED),
    "its hand-over fails": (_manager_of("alpha", end="hand-over"), _ASKED),
    "it parks on its usage limit": (_manager_of("alpha", limited=True), _ASKED + _REPORTED),
    "its session never registered": (_manager_of("alpha", registered=False), _ASKED + _REPORTED),
    "its session was ended under it": (_manager_of("alpha", pruned=True), _ASKED + _REPORTED),
    "its session never registered, and it exits": (
        _manager_of("alpha", registered=False, end="exit"),
        _ASKED,
    ),
}


@pytest.mark.parametrize(("manager", "expected"), list(_GOING.values()), ids=list(_GOING))
def test_what_the_manager_cannot_take_is_a_card_but_a_result_to_one_that_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manager: _Seat,
    expected: list[tuple[str, str]],
) -> None:
    """A coder's question or result to the manager is the manager's while it can take it: a
    live row on a board session the fleet's nudge wakes. Once it cannot, every question to
    it still open is a card: someone waits on the answer. So is a result to it while it
    runs: parked on its limit, or on a session that never registered or was ended, nothing
    wakes it, and no card says it is down; counted as a manager, every result to it was
    hidden for good (review of #243, round 25, 1/4). A result to a manager that stopped
    stays on the board, before or after the stop: the stop is its own card, or a clean
    exit (round 24, 2/8). coder-1 asks, unanswered; coder-2 asks and the human answers;
    coder-3 reported an hour ago, and coder-4 a minute ago."""
    seats = (manager, *(_Seat("alpha", f"coder-{n}") for n in range(1, 5)))
    alpha = _seated(tmp_path, monkeypatch, seats, hub=False)["alpha"]
    tmux = FakeTmux()
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: tmux)
    now = datetime.now(UTC)
    posts = [
        ("coder-1", "question", now - timedelta(minutes=5)),
        ("coder-2", "question", now - timedelta(minutes=5)),
        (None, "note", now - timedelta(minutes=4)),
        ("coder-3", "result", now - timedelta(hours=1)),
        ("coder-4", "result", now - timedelta(minutes=1)),
    ]
    with store_session() as store:
        for n, (author, kind, at) in enumerate(posts):
            store.add_team_event(
                TeamEvent(
                    id=f"evt_{n}", project_id=alpha.id, kind=kind, text=f"{kind} {n}",
                    session_id=author and f"ses_alpha_{author}",
                    to_role="manager" if author else "coder-2", created_at=at,
                )
            )  # fmt: skip
    assert _board_cards(remote_needs.live_needs_sources()) == expected


@pytest.mark.parametrize("end", ["stop", "lost"])
def test_a_manager_stopped_with_no_status_stays_down_once_the_coder_reporting_to_it_is_idle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, end: str
) -> None:
    """The manager is killed, or its pane lost, while coder-1 works, and coder-1 reports to
    it and goes idle inside the minute ``manager_down`` waits to push. Down only while its
    crew worked, the card went before it was pushed, and the result stayed on the board,
    read by nobody (review of #243, round 25, 2/4). It stays, and is due once its minute
    is up."""
    from aisquare.services import team as team_service

    seats = (_manager_of("alpha", end=end), _Seat("alpha", "coder-1"))
    alpha = _seated(tmp_path, monkeypatch, seats, hub=False)["alpha"]
    tmux = FakeTmux()
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: tmux)
    sources = remote_needs.live_needs_sources()

    def scanned(at: datetime) -> list[tuple[str, str | None, bool]]:
        items = scan_needs_you(sources, now=at, dismissed=())
        return [(i.kind, i.agent, i.push_after is not None and i.push_after <= at) for i in items]

    with store_session() as store:
        row = store.get_fleet_agent("agt_alpha_manager")
        coding = store.get_session("ses_alpha_coder-1")
    assert row is not None and row.ended_at is not None and coding is not None
    assert scanned(row.ended_at + timedelta(seconds=20)) == [("manager_down", "manager", False)]
    with store_session() as store:
        team_service._emit(
            store, alpha.id, "result", "Green.", session_id=coding.id, to_role="manager"
        )
        store.touch_session(coding.id, state="waiting")
    assert scanned(datetime.now(UTC)) == [("manager_down", "manager", True)]


@pytest.mark.parametrize(
    ("stopped", "said", "card"),
    [
        pytest.param(timedelta(hours=24, minutes=30), timedelta(hours=23, minutes=30), False,
                     id="an hour after the stop"),
        pytest.param(timedelta(hours=30), timedelta(hours=5), True,
                     id="more than a day after the stop"),
    ],
)  # fmt: skip
def test_a_result_to_a_manager_that_stopped_is_judged_by_the_stop_in_its_own_day(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stopped: timedelta,
    said: timedelta,
    card: bool,
) -> None:
    """With the ended rows read a day back from now, a result an hour after a clean exit
    became a card, already due, once the exit was a day old, and was pushed nearly a day
    late (review of #243, round 25, 3/4). Each result is judged by the stop in its own day,
    and is the same card, or none, from its first scan to its last: a result more than a
    day after the stop is no stopped manager's."""
    alpha = _seated(tmp_path, monkeypatch, (_Seat("alpha", "coder-1"),), hub=False)["alpha"]
    tmux = FakeTmux()
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: tmux)
    now = datetime.now(UTC)
    with store_session() as store:
        store.upsert_fleet_agent(
            FleetAgent(
                id="agt_m", project_id=alpha.id, label="manager", role="manager", pane_id="%m",
                session_id="ses_m", cwd=alpha.root, created_at=now - stopped - timedelta(hours=1),
                ended_at=now - stopped, exit_status=0,
            )
        )  # fmt: skip
        store.add_team_event(
            TeamEvent(
                id="evt_r", project_id=alpha.id, kind="result", text="Green.",
                session_id="ses_alpha_coder-1", to_role="manager", created_at=now - said,
            )
        )  # fmt: skip
    sources = remote_needs.live_needs_sources()
    for at in (now - said + timedelta(minutes=1), now):
        items = scan_needs_you(sources, now=at, dismissed=())
        cards = [(item.kind, item.agent) for item in items if item.kind.startswith("board_")]
        assert cards == ([("board_result", "coder-1")] if card else []), at


def test_with_no_manager_in_its_day_a_coders_result_to_the_manager_is_a_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a manager that stopped leaves a result to it on the board: with none on the
    project's board, or under a hub, it is a card."""
    from aisquare.services import team as team_service

    alpha = _seated(tmp_path, monkeypatch, (_Seat("alpha", "coder-1"),), hub=False)["alpha"]
    tmux = FakeTmux()
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: tmux)
    with store_session() as store:
        team_service._emit(
            store, alpha.id, "result", "Green.", session_id="ses_alpha_coder-1", to_role="manager"
        )
    assert _board_cards(remote_needs.live_needs_sources()) == [("board_result", "coder-1")]


@pytest.mark.parametrize(
    ("before", "session"),
    [
        pytest.param(None, "ses_minted", id="a spawn"),
        pytest.param(
            _manager_of("alpha", end="exit", pruned=True), "ses_alpha_manager", id="a restart"
        ),
        pytest.param(_manager_of("alpha", end="exit", pruned=True), "ses_minted", id="a switch"),
    ],
)
def test_a_manager_starting_takes_them_until_its_session_should_have_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, before: _Seat | None, session: str
) -> None:
    """The fleet records a manager's row once its window is up, and its start hook registers
    the board session seconds later: on the session it resumes (a restart), or a new one (a
    spawn, a fresh switch). Read meanwhile as a manager that cannot take them, every coder's
    open question and result to it of the day was a card, due at once (review of #243, round
    26, 1/2). It is starting for ``_NEEDS_STARTING``; after that, one that never registered
    cannot take them."""
    from aisquare.services import team as team_service

    crew = (_Seat("alpha", "coder-1", asks=True), _Seat("alpha", "coder-2"))
    seats = crew if before is None else (before, *crew)
    alpha = _seated(tmp_path, monkeypatch, seats, hub=False)["alpha"]
    tmux = FakeTmux()
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: tmux)
    now = datetime.now(UTC)
    with store_session() as store:
        team_service._emit(
            store, alpha.id, "result", "Green.", session_id="ses_alpha_coder-2", to_role="manager"
        )
        store.upsert_fleet_agent(
            FleetAgent(
                id="agt_starting", project_id=alpha.id, label="manager", role="manager",
                pane_id="%starting", session_id=session, cwd=alpha.root, created_at=now,
            )
        )  # fmt: skip
    sources = remote_needs.live_needs_sources()

    def cards(at: datetime) -> list[tuple[str, str | None]]:
        items = scan_needs_you(sources, now=at, dismissed=())
        return [(item.kind, item.agent) for item in items if item.kind.startswith("board_")]

    assert cards(now) == []
    starting = remote_needs._NEEDS_STARTING
    assert cards(now + starting - timedelta(seconds=1)) == []
    assert cards(now + starting) == [("board_question", "coder-1"), ("board_result", "coder-2")]


@pytest.mark.parametrize(
    ("ahead", "starting"),
    [
        pytest.param(remote_needs._NEEDS_STARTING - timedelta(seconds=1), True,
                     id="written while the scan ran"),
        pytest.param(remote_needs._NEEDS_STARTING, False, id="further ahead than the grace"),
        pytest.param(timedelta(hours=1), False, id="the clock set back an hour"),
    ],
)  # fmt: skip
def test_a_manager_dated_ahead_of_the_scan_is_starting_only_within_the_grace(
    ahead: timedelta, starting: bool
) -> None:
    """A live manager row whose session has yet to register is starting for
    ``_NEEDS_STARTING`` (round 26, 1/2). With no lower bound, a row dated after the scan,
    the clock set back since its spawn, was starting until the clock caught up with it: a
    manager that never registered held every coder's question to it, and every crash, off
    the phone all that time (review of #243, round 27, 6/6)."""
    manager = _row("manager", role="manager", created=NOW + ahead)
    fleet = _crew_and_manager(_status(manager, "working"))
    cards = [(item.kind, item.agent) for item in _scan(fleet)]
    assert cards == ([] if starting else [("board_question", "coder-1"), ("crashed", "coder-2")])


_READS = (
    "list_agents",
    "has_live_agents",
    "tmux_answers",
    "ended_agents",
    "board_since",
    "board_sessions",
    "accounts",
    "board_events",
    "session_event",
    "transcript_tail",
    "pane_output",
    "task_status",
)
"""Every read of a project's scan (:class:`NeedsSources`) but ``list_projects``, which a
scan cannot go without, and ``board_project``, whose failure is an answer: the team off."""


def _every_read() -> tuple[Fleet, NeedsSources]:
    """A hub's project whose scan makes each of :data:`_READS`: coder-1 parked on its limit,
    coder-2 at an MCP form and coder-3 at the usage-limit dialog, each named by an event the
    window no longer holds; coder-4 at a sub-agent's prompt; coder-5 crashed with its task
    open, its exit in the project's own events; coder-6 asking the human on the board."""
    fleet = _parked_and_asking()
    _board_traffic(fleet)
    prompted, tail = _in_a_sub_agent(NOW - timedelta(minutes=1), "coder-4")
    asker = _row("coder-6")
    asking = _session(asker)
    crashed = _row("coder-5", ended=NOW - timedelta(minutes=10), exit_status=1, task_id="tsk_1")
    fleet.agents += [prompted, _status(asker, "working", asking)]
    fleet.tails["/transcripts/coder-4.jsonl"] = tail
    fleet.ended.append(crashed)
    fleet.tasks["tsk_1"] = "doing"
    fleet.sessions.append(asking)
    fleet.events.append(_event(400, "question", "Ship on Friday?", session=asking))
    # The live ones spawned under the hub recorded its board: what the team writes of each
    # is in the hub's window and day (review of #259, round 26, 3/11). coder-5, spawned
    # without it, recorded the project's own, whose exits are read apart.
    fleet.agents = [
        status.model_copy(update={"agent": status.agent.model_copy(update={"board": "prj_hub"})})
        for status in fleet.agents
    ]
    return fleet, replace(_sources(fleet), board_project=lambda project: "prj_hub")


def _failing(sources: NeedsSources, read: str, asked: list[str]) -> NeedsSources:
    """``sources`` with ``read`` failing, as a store held past its busy timeout does, and
    each time it was asked put in ``asked``."""

    def locked(*args: Any) -> Any:
        asked.append(read)
        raise RuntimeError("database is locked")

    return replace(sources, **{read: locked})


@pytest.mark.parametrize("read", _READS)
def test_a_scan_that_cannot_read_one_of_its_facts_leaves_the_last_items_as_they_were(
    runtime: Runtime, tmp_path: Path, read: str
) -> None:
    """Unknown is not absent. Each read that failed was taken for none, one kind of read at a
    time: a listing or ended rows not read dropped a crash or a manager's stop and restarted
    its push streak, events not read named a dialog anew, its dismissal lost and pushed
    again (review of #243, rounds 22 to 27). A scan decides only from a complete read: with
    one read failing, the last scan's items stand, ids, dismissal and push streak and all,
    and nothing a complete scan would change, a new question or a crash whose task closed,
    shows until the read works again."""
    from aisquare.services.remote_push import RemotePushSender

    fleet, sources = _every_read()
    reading = [sources]
    app = build_app(runtime, sources=_server_sources(), dist_dir=tmp_path)
    watcher = RemoteNeedsWatcher(app.kit, sources=lambda: reading[0], clock=lambda: NOW)
    sender = RemotePushSender(app.kit, clock=lambda: NOW)
    app.kit.needs_listeners.append(sender.push_scan_seen)
    last = watcher.scan_needs_now()
    assert [(item.kind, item.agent) for item in last] == [
        ("permission", "coder-2"),
        ("permission", "coder-4"),
        ("board_question", "coder-6"),
        ("crashed", "coder-5"),
        ("limited", "coder-1"),
        ("limited", "coder-3"),
    ]
    form = last[0]
    record_needs_dismissal(form.id)
    fleet.tasks["tsk_1"] = "done"
    fleet.events.append(_event(401, "question", "Or Monday?", session=fleet.sessions[-1]))
    asked: list[str] = []
    reading[0] = _failing(sources, read, asked)
    kept = last[1:]
    assert watcher.scan_needs_now() == kept
    assert asked == [read], "the read was made, and failed"
    assert sender._streak == {item.id: 2 for item in kept}
    reading[0] = sources
    healed = [(item.kind, item.agent, item.excerpt) for item in watcher.scan_needs_now()]
    assert ("crashed", "coder-5", "") not in healed
    assert ("board_question", "coder-6", "Or Monday?") in healed


@pytest.mark.parametrize("read", _READS)
def test_an_actions_check_is_a_503_while_any_read_of_its_scan_fails(
    monkeypatch: pytest.MonkeyPatch, read: str
) -> None:
    """An action's check took a read that failed for none, so a card's own item was missing
    or named anew: a Restart on a ``crashed`` or ``manager_down`` card was told "That agent
    is gone.", and on a failed hand-over's, or a Switch on a usage-limit dialog's, a 409
    ``stale``, whatever other card the agent had (review of #243, round 25, 4/4; round 26,
    2/2; round 27, 2/6 and 7/7). Any read of its scan that fails is a 503, to try again."""
    from aisquare.services.remote_server import RequestError

    _fleet, sources = _every_read()
    monkeypatch.setattr(remote_needs, "live_needs_sources", lambda: sources)
    monkeypatch.setattr(
        fleet_service, "server_for", lambda socket, config=None: FakeTmux(reference=NOW)
    )
    assert [item.kind for item in needs_agent_now(PROJECT, "coder-2", now=NOW).items] == [
        "permission"
    ]
    asked: list[str] = []
    failing = _failing(sources, read, asked)
    monkeypatch.setattr(remote_needs, "live_needs_sources", lambda: failing)
    with pytest.raises(RequestError) as refused:
        needs_agent_now(PROJECT, "coder-2", now=NOW)
    assert (refused.value.status, refused.value.error) == (503, "unavailable")
    assert asked == [read]


def test_a_tmux_question_that_fails_is_no_first_sighting_of_tmux_down() -> None:
    """A scan whose question to tmux failed is not used, and dates nothing either: read as
    no answer, it was the first sighting of an outage nobody saw, and the card of the next
    scan that did see one, and its push, were dated from before it."""
    fleet = _on_a_dead_tmux()
    fleet.silent.add("asq")
    memory: dict[str, datetime] = {}
    asked: list[str] = []
    failing = _failing(_sources(fleet), "tmux_answers", asked)
    assert scan_needs_you(failing, now=NOW, dismissed=(), first_seen=memory) == []
    assert asked == ["tmux_answers"] and memory == {}
    later = NOW + timedelta(seconds=3)
    down = _one(_scan(fleet, now=later, first_seen=memory))
    assert (down.kind, down.since) == ("fleet_down", later)


def test_a_board_that_cannot_be_resolved_is_the_projects_own_and_the_scan_goes_on() -> None:
    """With the team off ``team.resolve_project`` refuses every time: a failure to name the
    board is an answer, not a read that failed, or nothing would ever be scanned."""
    fleet = _crash(exit_status=1)

    def refused(project: ProjectInfo) -> str:
        raise RuntimeError("the team is disabled")

    sources = replace(_sources(fleet), board_project=refused)
    assert [item.kind for item in scan_needs_you(sources, now=NOW, dismissed=())] == ["crashed"]


def test_under_a_hub_a_projects_needs_read_no_other_projects_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under ``AISQUARE_TEAM_HUB`` an action's check read every project's rows and derived
    their managers under the agent's lock, and the scan judged each project by the rows
    of every other (review of #243, round 21, 6/8 and 8/8). Each project's needs are read
    from its own rows alone: the action lists the agent's project and nothing else, even
    for a stopped manager, whose stop is down whatever its crew, wherever it works (round
    25, 2/4); the scan reads each project's rows once, and the hub's day and sessions once
    for all of them (round 21, 7/8)."""
    seats = (
        _manager_of("alpha"),
        _Seat("alpha", "coder-a", asks=True),
        _Seat("beta", "coder-1", asks=True),
        _Seat("gamma", "coder-g"),
        _Seat("delta", "coder-d"),
    )
    projects = _seated(tmp_path, monkeypatch, seats, hub=True)
    names = {project.id: name for name, project in projects.items()}
    names[_rooted(tmp_path / "hub").id] = "hub"
    reads: list[tuple[str, str]] = []
    live = remote_needs.live_needs_sources

    def counted() -> NeedsSources:
        sources = live()

        def count(what: str, read: Callable[..., Any], name: Callable[..., str]) -> Any:
            def counting(*args: Any) -> Any:
                reads.append((what, name(*args)))
                return read(*args)

            return counting

        return replace(
            sources,
            list_projects=count("projects", sources.list_projects, lambda: "every project"),
            list_agents=count("list", sources.list_agents, lambda p: names[p.id]),
            ended_agents=count("ended", sources.ended_agents, lambda pid, since: names[pid]),
            board_since=count("day", sources.board_since, lambda pid, since: names[pid]),
            board_sessions=count("sessions", sources.board_sessions, lambda pid, ids: names[pid]),
        )

    tmux = FakeTmux()
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: tmux)
    monkeypatch.setattr(remote_needs, "live_needs_sources", counted)
    snap = needs_agent_now(projects["beta"], "coder-1")
    assert [item.kind for item in snap.items] == ["board_question"]
    assert reads == [("list", "beta"), ("ended", "beta"), ("day", "hub"), ("sessions", "hub")]
    assert set(tmux.asked) == {"%beta-coder-1"}, "no other agent's pane"
    with store_session() as store:
        store.end_fleet_agent("agt_alpha_manager")
    reads.clear()
    assert [item.kind for item in needs_agent_now(projects["alpha"], "manager").items] == [
        "manager_down"
    ], "for its own project's coder-a"
    assert [name for what, name in reads if what in ("projects", "list", "ended")] == [
        "alpha",
        "alpha",
    ]
    reads.clear()
    scan_needs_you(remote_needs.live_needs_sources(), now=datetime.now(UTC), dismissed=())
    assert sorted(name for what, name in reads if what == "ended") == sorted(projects)
    assert [name for what, name in reads if what in ("day", "sessions")] == ["hub", "hub"]


def test_under_a_team_hub_a_projects_rows_are_derived_again_in_one_look_at_tmux(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every row's session is the hub's, so every row was derived again by
    ``fleet.status_of``: a store session and a look at its tmux server for each agent, on
    top of the listing's one look, every 3 s (verifier of sweep 4's item 8 on #243). The
    rows are derived again together, each with its own session."""
    now = datetime.now(UTC)
    hour_ago = now - timedelta(hours=1)
    hub = _rooted(tmp_path / "hub")
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(hub.root))
    labels = ("coder-1", "coder-2", "coder-3")
    with store_session() as store:
        alpha = store.onboard_project(_rooted(tmp_path / "alpha"))
        store.ensure_project(hub)
        for n, label in enumerate(labels, start=1):
            store.upsert_session(
                TeamSession(
                    id=f"ses_{label}", project_id=hub.id, role="coder", label=label,
                    started_at=hour_ago, last_seen_at=now - timedelta(minutes=1),
                    state="attention" if label == "coder-2" else "working",
                )
            )  # fmt: skip
            store.upsert_fleet_agent(
                FleetAgent(
                    id=f"agt_{label}", project_id=alpha.id, label=label, role="coder",
                    pane_id=f"%{n}", session_id=f"ses_{label}", cwd=alpha.root,
                    created_at=hour_ago,
                )
            )  # fmt: skip
    looks: list[list[str]] = []
    observe = fleet_service._observe

    def looked(srv: Any, tmux_session: str | None, agents: Any, reads: Any = None) -> Any:
        looks.append(sorted(agent.label for agent in agents))
        return observe(srv, tmux_session, agents, reads)

    tmux = FakeTmux()
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: tmux)
    monkeypatch.setattr(fleet_service, "_observe", looked)
    items = scan_needs_you(remote_needs.live_needs_sources(), now=now, dismissed=())
    assert looks == [list(labels), list(labels)], "the listing's look, and one more"
    assert [(item.kind, item.agent) for item in items] == [("permission", "coder-2")]
    assert items[0].reason == "coder-2 shows a dialog that needs you"


def test_under_a_team_hub_each_rows_session_is_read_once_a_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 14: the listing read each silent row's hub session by its id,
    for the trust question alone, and dropped it; the scan then read the same ids again to
    derive the rows. Two store opens and two lookups per live row, every 3 s. The scan
    takes the listing's."""
    from aisquare.core.store import SqliteStore

    now = datetime.now(UTC)
    hub = _rooted(tmp_path / "hub")
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(hub.root))
    labels = ("coder-1", "coder-2")
    with store_session() as store:
        alpha = store.onboard_project(_rooted(tmp_path / "alpha"))
        store.ensure_project(hub)
        for n, label in enumerate(labels, start=1):
            store.upsert_session(
                TeamSession(
                    id=f"ses_{label}", project_id=hub.id, role="coder", label=label,
                    started_at=now - timedelta(hours=1), last_seen_at=now - timedelta(minutes=1),
                    state="attention" if label == "coder-2" else "working",
                )
            )  # fmt: skip
            store.upsert_fleet_agent(
                FleetAgent(
                    id=f"agt_{label}", project_id=alpha.id, label=label, role="coder",
                    pane_id=f"%{n}", session_id=f"ses_{label}", cwd=alpha.root,
                    created_at=now - timedelta(hours=1),
                )
            )  # fmt: skip
    tmux = FakeTmux()
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: tmux)
    asked: list[str] = []
    get_session = SqliteStore.get_session

    def counted(self: SqliteStore, session_id: str) -> TeamSession | None:
        asked.append(session_id)
        return get_session(self, session_id)

    monkeypatch.setattr(SqliteStore, "get_session", counted)

    statuses = remote_needs.live_needs_sources().list_agents(alpha)

    assert sorted(asked) == ["ses_coder-1", "ses_coder-2"], "each once"
    assert {status.agent.label: status.state for status in statuses} == {
        "coder-1": "working",
        "coder-2": "attention",
    }, "derived with their sessions all the same"


def _rooted(root: Path) -> ProjectInfo:
    """A project as ``asq init`` registers it: its id derived from its root, which is where
    the team writes its board too (``team_project``), and where the scan reads it."""
    from aisquare.core.workspace import project_id_for

    resolved = root.resolve()
    return ProjectInfo(id=project_id_for(resolved), root=resolved)


def _live_agent(
    store: Any,
    project: ProjectInfo,
    label: str,
    *,
    state: str,
    seen: datetime,
    born: datetime,
    transcript: Path | None = None,
    resets: datetime | None = None,
) -> TeamSession:
    """``label``'s fleet row and session in the store, its pane ``%<n>`` of its label."""
    session = TeamSession(
        id=f"ses_{label}", project_id=project.id, role="coder", label=label, started_at=born,
        last_seen_at=seen, state=state, limit_resets_at=resets,
        transcript_path=None if transcript is None else str(transcript),
    )  # fmt: skip
    store.upsert_session(session)
    store.upsert_fleet_agent(
        FleetAgent(
            id=f"agt_{label}", project_id=project.id, label=label, role="coder",
            pane_id=f"%{label[-1]}", session_id=session.id, cwd=project.root, created_at=born,
        )
    )  # fmt: skip
    return session


def _transcript(path: Path, *records: dict[str, Any]) -> Path:
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return path


def _counted_session_events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Every ``(session, kind)`` the store is asked for its newest event of, from now on."""
    from aisquare.core.store import SqliteStore

    asked: list[tuple[str, str]] = []
    real = SqliteStore.newest_session_event

    def counted(self: Any, project_id: str, session_id: str, kind: str, **kw: Any) -> Any:
        asked.append((session_id, kind))
        return real(self, project_id, session_id, kind, **kw)

    monkeypatch.setattr(SqliteStore, "newest_session_event", counted)
    return asked


def test_the_live_sources_keep_a_dialogs_and_a_limits_cards_past_a_busy_board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the store and the fleet's listing: a dialog and a parked limit are named by
    their sessions' own ``attention`` and ``limited`` events. The window of the newest
    events holds them, and the store is asked for neither; 300 notes later the window
    no longer does, and the store's own read of each session finds them, so both cards
    keep their ids and their words. Every other test of this hands the scan fakes: the
    live window, the session read and their arguments could each be stubbed out with the
    suite green, and a card that changed its id was pushed again, its dismissal lost
    (review of #243, sweep 3)."""
    now = datetime.now(UTC)
    root = tmp_path / "alpha"
    said = {
        "type": "assistant",
        "uuid": "a1",
        "timestamp": (now - timedelta(minutes=2)).isoformat(),
        "message": {"id": "m1", "role": "assistant", "content": [{"type": "text", "text": "OK"}]},
    }
    words = "Claude needs your permission to use the deploy MCP tool"
    with store_session() as store:
        project = store.onboard_project(_rooted(root))
        asking = _live_agent(
            store, project, "coder-1", state="attention", seen=now - timedelta(minutes=1),
            born=now - timedelta(hours=1), transcript=_transcript(tmp_path / "c1.jsonl", said),
        )  # fmt: skip
        parked = _live_agent(
            store, project, "coder-2", state="limited", seen=now - timedelta(hours=2),
            born=now - timedelta(hours=3), resets=now + timedelta(hours=3),
        )  # fmt: skip
        for session, kind, text, at in (  # in the order they were written: seq is time
            (parked, "limited", "coder-2 hit its usage limit", now - timedelta(hours=2)),
            (asking, "attention", words, now - timedelta(minutes=1)),
        ):
            store.add_team_event(
                TeamEvent(
                    id=f"evt_{kind}", project_id=project.id, session_id=session.id, kind=kind,
                    text=text, created_at=at,
                )
            )  # fmt: skip
    tmux = FakeTmux()
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: tmux)
    asked = _counted_session_events(monkeypatch)

    def cards() -> dict[str, tuple[str, str, object]]:
        items = scan_needs_you(remote_needs.live_needs_sources(), now=now, dismissed=())
        return {item.agent or "": (item.kind, item.id, item.detail.get("text")) for item in items}

    before = cards()
    assert before == {
        "coder-1": ("permission", before["coder-1"][1], words),
        "coder-2": ("limited", before["coder-2"][1], "coder-2 hit its usage limit"),
    }
    assert asked == [], "the window held both events: no read of a session of its own"
    with store_session() as store:
        for n in range(remote_needs.NEEDS_BOARD_EVENTS):
            store.add_team_event(
                TeamEvent(
                    id=f"evt_note_{n}", project_id=project.id, kind="note", text=f"note {n}",
                    created_at=now - timedelta(seconds=30),
                )
            )  # fmt: skip
    assert cards() == before, "the same cards, under the same ids, with the same words"
    assert sorted(asked) == [("ses_coder-1", "attention"), ("ses_coder-2", "limited")]
    snap = needs_agent_now(project, "coder-1", now=now)
    assert [(item.kind, item.id) for item in snap.items] == [("permission", before["coder-1"][1])]


def test_the_live_sources_ask_tmux_when_a_sub_agents_pane_last_printed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the store, the fleet's listing and the pane's facts: a sub-agent's next prompt
    has no card until its notice, and a pane that printed after the last one, within
    ``_NOTICE_WAIT``, is that next prompt being drawn. The scan read when the pane printed
    with a ``display-message`` and a parse of its own, beside the one ``PaneFacts`` owns
    and the snapshot reads, so the two could read one pane by two rules; and no test ran
    the live read at all: stubbed out, the card of the prompt before stayed, and its "1"
    approved the next (review of #243, round 5)."""
    now = datetime.now(UTC)
    root = tmp_path / "alpha"
    task = {"type": "tool_use", "id": "toolu_task", "name": "Task", "input": {"description": "x"}}
    running = {
        "type": "assistant",
        "uuid": "a1",
        "timestamp": (now - timedelta(minutes=5)).isoformat(),
        "message": {"id": "m1", "role": "assistant", "content": [task]},
    }
    with store_session() as store:
        project = store.onboard_project(_rooted(root))
        _live_agent(
            store, project, "coder-1", state="attention", seen=now - timedelta(minutes=1),
            born=now - timedelta(hours=1), transcript=_transcript(tmp_path / "c1.jsonl", running),
        )  # fmt: skip
    drawing = FakeTmux(quiet_for=10)
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: drawing)
    assert scan_needs_you(remote_needs.live_needs_sources(), now=now, dismissed=()) == []
    assert "%1" in drawing.asked, "asked through the pane's facts"
    snap = needs_agent_now(project, "coder-1", now=now)
    assert snap.items == () and snap.pane_quiet is True
    quiet = FakeTmux(quiet_for=120)
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: quiet)
    (item,) = scan_needs_you(remote_needs.live_needs_sources(), now=now, dismissed=())
    assert (item.kind, item.reason) == (
        "permission",
        "coder-1 waits for a permission answer (in a sub-agent)",
    )
    assert [card.id for card in needs_agent_now(project, "coder-1", now=now).items] == [item.id]


def test_a_session_at_its_fresh_prompt_is_told_now_in_prompt_and_interrupt_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the store, the fleet's listing, the cached tail and the tell itself: a session
    the board has as ``working`` since it started, its transcript not made yet (Claude Code
    makes it with the first record), its pane quiet. Prompt mode answered ``agent_busy``,
    and Interrupt & tell sent its Escape, waited 8 s and typed nothing (review of #243,
    sweep 3). Once its first prompt is written, a turn runs, and both refuse again."""
    from aisquare.services import remote_actions
    from aisquare.services.remote_server import RequestError

    now = datetime.now(UTC)
    transcript = tmp_path / "coder-1.jsonl"
    with store_session() as store:
        project = store.onboard_project(_rooted(tmp_path / "alpha"))
        _live_agent(
            store, project, "coder-1", state="working", seen=now - timedelta(seconds=10),
            born=now - timedelta(seconds=12), transcript=transcript,
        )  # fmt: skip
    tmux = FakeTmux(quiet_for=8)
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: tmux)
    snap = needs_agent_now(project, "coder-1")
    assert snap.status is not None and snap.status.state == "working"
    assert snap.tail is not None and snap.tail.empty and needs_at_input_prompt(snap)
    body = {"agent": "coder-1", "text": "start on the cache", "project": project.id}
    for mode in ("prompt", "interrupt"):
        result, _line = remote_actions.action_tell({**body, "mode": mode})
        assert result["delivered"] is True, result
    assert tmux.typed == [
        ("paste", "%1", "start on the cache"), ("keys", "%1", "Enter"),
        ("keys", "%1", "Escape"), ("paste", "%1", "start on the cache"), ("keys", "%1", "Enter"),
    ]  # fmt: skip
    prompted = {
        "type": "user",
        "uuid": "u1",
        "timestamp": now.isoformat(),
        "message": {"role": "user", "content": "start on the cache"},
    }
    _transcript(transcript, prompted)
    assert not needs_at_input_prompt(needs_agent_now(project, "coder-1"))
    with pytest.raises(RequestError) as busy:
        remote_actions.action_tell({**body, "mode": "prompt"})
    assert (busy.value.status, busy.value.error) == (409, "agent_busy")


def _local_command(at: datetime, name: str, output: str) -> list[dict[str, Any]]:
    """What Claude Code 2.1.296 writes for a local slash command (``/model``, ``/compact``):
    a caveat it marks ``isMeta``, the command, its output, and no turn: no prompt hook, no
    query, no Stop."""

    def user(uuid: str, content: str, **flags: bool) -> dict[str, Any]:
        record = {"type": "user", "uuid": uuid, "timestamp": at.isoformat(), **flags}
        return {**record, "message": {"role": "user", "content": content}}

    caveat = "<local-command-caveat>Caveat: generated by the user running local commands."
    return [
        user(f"{name}-c", caveat + "</local-command-caveat>", isMeta=True),
        user(f"{name}-n", f"<command-name>{name}</command-name>\n<command-args></command-args>"),
        user(f"{name}-o", f"<local-command-stdout>{output}</local-command-stdout>"),
    ]


@pytest.mark.parametrize("case", ["a fresh agent's /model", "a /compact after its turn"])
def test_an_agent_idle_after_a_local_command_is_at_its_prompt_and_told_now(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A local command runs no turn, so no hook follows it, and a manual ``/compact``'s
    ``SessionStart`` puts the session back to ``working``. Its records, and the summary the
    compaction leaves, read as the human's prompt of a turn under way: prompt mode answered
    ``agent_busy`` and Interrupt & tell sent an Escape and gave up, for the board's 30
    minutes, to an agent idle at its prompt (sweep 4 of #243). They are Claude Code's own
    records; a quiet pane over them is the prompt."""
    from aisquare.services import remote_actions

    now = datetime.now(UTC)
    path = tmp_path / "coder-1.jsonl"
    born = now - timedelta(minutes=10)
    records: list[dict[str, Any]] = []
    if case == "a /compact after its turn":
        asked, done, compacted = (
            (born + timedelta(minutes=1)).isoformat(),
            (born + timedelta(minutes=3)).isoformat(),
            (now - timedelta(minutes=1)).isoformat(),
        )
        said = {"type": "text", "text": "Done: the cache is in."}
        records = [
            {"type": "user", "uuid": "u1", "timestamp": asked,
             "message": {"role": "user", "content": "add the cache"}},
            {"type": "assistant", "uuid": "a1", "timestamp": done,
             "message": {"id": "m1", "role": "assistant", "content": [said]}},
            {"type": "system", "subtype": "compact_boundary", "uuid": "b1", "timestamp": compacted},
            {"type": "user", "uuid": "s1", "isCompactSummary": True, "timestamp": compacted,
             "message": {"role": "user", "content": "This session is being continued from..."}},
        ]  # fmt: skip
        records += _local_command(now - timedelta(minutes=1), "/compact", "Compacted")
    else:
        records = _local_command(now - timedelta(minutes=8), "/model", "Set model to opus")
    _transcript(path, *records)
    with store_session() as store:
        project = store.onboard_project(_rooted(tmp_path / "alpha"))
        session = _live_agent(
            store, project, "coder-1", state="waiting", seen=now - timedelta(minutes=1),
            born=born, transcript=path,
        )  # fmt: skip
        store.upsert_session(session)  # as SessionStart (startup or compact) writes it
    tmux = FakeTmux(quiet_for=30)
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: tmux)
    snap = needs_agent_now(project, "coder-1")
    assert snap.status is not None and snap.status.state == "working"
    assert snap.tail is not None and snap.tail.newest == "own"
    assert needs_at_input_prompt(snap)
    body = {"agent": "coder-1", "text": "now the docs", "project": project.id}
    for mode in ("prompt", "interrupt"):
        result, _line = remote_actions.action_tell({**body, "mode": mode})
        assert result["delivered"] is True, result
    assert ("paste", "%1", "now the docs") in tmux.typed


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


def _a_page_open(app: Any, runtime: Runtime) -> Callable[[], None]:
    """A phone unlocked, its page's socket open as the stream registers it; the closer."""
    assert unlock(make_client(app), runtime).status_code == 200
    (device_id,) = runtime.device_ids()

    def closer(code: int) -> None:
        return None

    app.kit.kit_socket_opened(device_id, closer)
    return lambda: app.kit.kit_socket_closed(device_id, closer)


def test_the_watcher_scans_only_while_a_page_or_a_subscription_hears_of_it(
    runtime: Runtime, tmp_path: Path
) -> None:
    """Nobody to show it to, no scan: no store reads, no tmux spawns. A device on record was
    enough: one signed out, or that never turned notifications on, has no socket and gets no
    push, and a phone unlocked once and closed cost a scan of every project every 3 s for
    the rest of its 7 days, the result read by nobody (review of #243, round 6). A page's
    socket is someone, and so is a subscription of a device the runtime still has, signed
    out or not, through the push sender."""
    from aisquare.services import remote_push

    app = build_app(runtime, sources=_server_sources(), dist_dir=tmp_path)
    made: list[NeedsSources] = []

    def counted() -> NeedsSources:
        made.append(_sources(Fleet()))
        return made[-1]

    def quiet() -> bool:
        before = len(made)
        threading.Event().wait(0.2)
        return len(made) == before

    watcher = RemoteNeedsWatcher(app.kit, sources=counted, interval=0.01)
    watcher.start_watching()
    try:
        assert quiet() and watcher.needs_scanned_at() is None
        assert unlock(make_client(app), runtime).status_code == 200
        assert quiet(), "a device on record, and nothing that would hear of a scan"
        (device_id,) = runtime.device_ids()

        def closer(code: int) -> None:
            return None

        app.kit.kit_socket_opened(device_id, closer)
        _until_true(lambda: watcher.needs_scanned_at() is not None)
        app.kit.kit_socket_closed(device_id, closer)
        _until_true(quiet)
        sender = remote_push.RemotePushSender(app.kit)
        app.kit.lane_state["push"] = sender
        assert quiet(), "a push sender with nobody subscribed"
        phone = remote_push.PushSubscriptionRecord(
            "https://fcm.googleapis.com/fcm/send/x", "p256dh", "auth", NOW.isoformat()
        )
        remote_push.push_subscribe_device(device_id, phone, {device_id})
        assert not quiet(), "a phone with notifications on, its page closed"
        runtime.revoke_device(device_id)
        _until_true(quiet)
        assert any(t.name == "asq-remote-needs" for t in threading.enumerate())
    finally:
        watcher.stop_watching()
    assert not watcher.needs_watching()


def test_a_read_with_nobody_listening_scans_for_itself_once_the_latest_is_old(
    runtime: Runtime, tmp_path: Path
) -> None:
    """The watcher scans for nobody, so a page that reads the feed with no socket open (one
    that cannot connect, or before it has) gets a scan of its own when the latest is older
    than the interval, and the latest when it is not."""
    app = build_app(runtime, sources=_server_sources(), dist_dir=tmp_path)
    client = make_client(app)
    assert unlock(client, runtime).status_code == 200
    made: list[NeedsSources] = []

    def counted() -> NeedsSources:
        made.append(_sources(Fleet()))
        return made[-1]

    watcher = RemoteNeedsWatcher(app.kit, sources=counted, interval=1.0)
    app.kit.lane_state["needs"] = watcher
    watcher.start_watching()
    try:
        threading.Event().wait(0.2)
        assert made == []
        url = f"{base(runtime)}/api/needs"
        first = client.get(url).json()["scanned_at"]
        assert first is not None and len(made) == 1
        assert client.get(url).json()["scanned_at"] == first and len(made) == 1, "fresh"
        threading.Event().wait(1.1)
        client.get(url)
        assert len(made) == 2, "older than the interval, and nobody else scans"
    finally:
        watcher.stop_watching()


def test_the_watcher_scans_nothing_past_the_auto_off_deadline(
    runtime: Runtime, tmp_path: Path
) -> None:
    """From the deadline on every request is a 404 and every socket closed, whatever turns
    Remote off has yet to run, the TUI's check every 30 s: no scan feeds a phone, or the push
    sender, meanwhile. A deadline moved later scans again."""
    app = build_app(runtime, sources=_server_sources(), dist_dir=tmp_path)
    _a_page_open(app, runtime)
    runtime.set_auto_off(datetime.now(UTC) - timedelta(seconds=1))
    made: list[NeedsSources] = []

    def counted() -> NeedsSources:
        made.append(_sources(Fleet()))
        return made[-1]

    watcher = RemoteNeedsWatcher(app.kit, sources=counted, interval=0.01)
    watcher.start_watching()
    try:
        threading.Event().wait(0.2)
        assert made == [] and watcher.needs_scanned_at() is None
        runtime.set_auto_off(datetime.now(UTC) + timedelta(hours=1))
        _until_true(lambda: watcher.needs_scanned_at() is not None)
    finally:
        watcher.stop_watching()


class _CountedLock:
    """The watcher's one-scan-at-a-time lock, counting who asked for it."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.asked = 0

    def __enter__(self) -> _CountedLock:
        self.asked += 1
        self.lock.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.lock.release()


@pytest.mark.parametrize("fails", [False, True])
def test_requests_that_waited_for_a_scan_share_it_instead_of_each_running_one(
    runtime: Runtime, tmp_path: Path, fails: bool
) -> None:
    """Each request made before the first scan ran its own, one after another, though one had
    just published: five early reads over a 2 s fleet were answered at 2, 4, 6, 8 and 10 s,
    each holding a thread of the shared pool, and a Remote off waited behind them (sweep 4
    of #243). Those that waited share the scan they waited for, its failure included."""
    from aisquare.core.store import StoreUnopenable

    app = build_app(runtime, sources=_server_sources(), dist_dir=tmp_path)
    started, release = threading.Event(), threading.Event()
    scans: list[int] = []

    def slow() -> list[ProjectInfo]:
        scans.append(len(scans))
        started.set()
        assert release.wait(5)
        if fails:
            raise StoreUnopenable("file is not a database")
        return []

    watcher = RemoteNeedsWatcher(
        app.kit, sources=lambda: replace(_sources(Fleet()), list_projects=slow)
    )
    counted = _CountedLock()
    watcher._scanning = counted  # type: ignore[assignment]
    answers: list[Exception | None] = []
    requests = [
        threading.Thread(target=lambda: answers.append(watcher.needs_scan_for_request()))
        for _ in range(5)
    ]
    requests[0].start()
    assert started.wait(5)
    for request in requests[1:]:
        request.start()
    _until_true(lambda: counted.asked >= 3 + 4)  # the first one's scan, and four waiting
    release.set()
    for request in requests:
        request.join(5)
    assert scans == [0], "one scan for the five"
    assert len(answers) == 5
    if fails:
        assert all(isinstance(answer, StoreUnopenable) for answer in answers)
    else:
        assert answers == [None] * 5 and watcher.needs_scanned_at() is not None
    again = watcher.needs_scan_for_request()
    assert isinstance(again, StoreUnopenable) if fails else again is None
    assert scans == [0, 1], "a request with nothing in flight scans for itself"


@pytest.mark.parametrize("then", ["nothing", "the watcher stops", "Remote goes off"])
def test_the_scan_after_quick_answers_is_one_and_never_runs_once_the_watcher_stopped(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, then: str
) -> None:
    """Each answer started a timer of its own, each a scan of every project with tmux: five
    quick taps were five scans back to back, and the timers of the last answers before the
    watcher stopped, or Remote went off, scanned after it (review of #243, sweep 5). One
    scan follows the last of them, and none follows a stop."""
    after = 0.3
    monkeypatch.setattr(remote_needs, "NEEDS_RESCAN_AFTER_ANSWER", after)
    app = build_app(runtime, sources=_server_sources(), dist_dir=tmp_path)
    scans: list[float] = []

    def counted() -> list[ProjectInfo]:
        # The clock a Timer's wait keeps everywhere: Windows' time.monotonic stepped 15.6 ms
        # before Python 3.13, and read a 0.3 s wait as 0.297 s.
        scans.append(time.perf_counter())
        return []

    watcher = RemoteNeedsWatcher(
        app.kit, sources=lambda: replace(_sources(Fleet()), list_projects=counted)
    )
    for _ in range(4):
        watcher.needs_rescan_soon()
    last = time.perf_counter()
    watcher.needs_rescan_soon()
    if then == "the watcher stops":
        watcher.stop_watching()
        watcher.needs_rescan_soon()  # an answer that finished as it stopped
    elif then == "Remote goes off":
        runtime.remote_going_off()
    if then == "nothing":
        _until_true(lambda: bool(scans))
        threading.Event().wait(after)
        assert len(scans) == 1, f"{len(scans)} scans for five answers"
        assert scans[0] - last >= after, "a second after the LAST answer, its pane had moved"
    else:
        threading.Event().wait(2 * after)
        assert scans == [], f"{len(scans)} scans after {then}"
    watcher.stop_watching()


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
    caplog.set_level(logging.DEBUG, logger=remote_needs.__name__)
    first = watcher.scan_needs_now()
    second = watcher.scan_needs_now()
    assert [at for _items, at in heard] == [NOW, NOW + timedelta(seconds=3)]
    assert [items for items, _at in heard] == [first, second]
    assert [item.kind for item in first] == ["question"] and first == second
    told = [r.levelname for r in caplog.records if "needs listener failed" in r.getMessage()]
    assert told == ["WARNING", "DEBUG"], "a listener that fails every scan is told once a streak"
    assert watcher.needs_items_now() == second
    assert watcher.needs_scanned_at() == NOW + timedelta(seconds=3)


def _failing_store() -> NeedsSources:
    """Sources over a store that cannot be opened, as ``open_store`` says it: SQLite's
    words alone, no file and no recovery."""
    from aisquare.core.store import StoreUnopenable

    def unopenable() -> list[ProjectInfo]:
        raise StoreUnopenable("file is not a database")

    return replace(_sources(Fleet()), list_projects=unopenable)


def test_a_scan_that_keeps_failing_is_told_once_until_it_works_again(
    runtime: Runtime, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``asq remote serve`` has no log handler, so each failed scan was the last-resort
    handler's 25-line traceback on its terminal, every 3 s while the store could not be
    read: 500 lines a minute, the link and the passphrase scrolled off. One warning a
    streak, a damaged store's in the sentence the CLI prints for it, with no traceback;
    debug lines after; and one line once a scan works again."""
    from aisquare.core.store import damaged_store_recovery

    app = build_app(runtime, sources=_server_sources(), dist_dir=tmp_path)
    _a_page_open(app, runtime)
    broken = threading.Event()
    broken.set()
    scans = 0

    def counted() -> NeedsSources:
        nonlocal scans
        scans += 1
        return _failing_store() if broken.is_set() else _sources(Fleet())

    def told() -> list[logging.LogRecord]:
        return [r for r in caplog.records if r.name == remote_needs.__name__]

    watcher = RemoteNeedsWatcher(app.kit, sources=counted, interval=0.01)
    caplog.set_level(logging.INFO, logger=remote_needs.__name__)
    watcher.start_watching()
    try:
        _until_true(lambda: scans >= 3)
        broken.clear()
        _until_true(lambda: any(r.levelname == "INFO" for r in told()))
        _until_true(lambda: scans >= 6)
    finally:
        watcher.stop_watching()
    records = told()
    assert [r.levelname for r in records] == ["WARNING", "INFO"], [r.getMessage() for r in records]
    assert damaged_store_recovery() in records[0].getMessage() and records[0].exc_info is None
    assert records[1].getMessage() == "remote: the needs scan works again"


def test_a_store_that_cannot_be_opened_is_told_by_its_file_and_its_recovery(
    runtime: Runtime, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``open_store``'s exception carries only SQLite's words, so the warning over a real
    corrupt ``context.db`` was "remote: the needs scan failed: file is not a database":
    no file, no way back. It is the sentence the CLI prints for it, once a streak."""
    from aisquare.core.paths import db_path
    from aisquare.core.store import damaged_store_recovery

    db_path().parent.mkdir(parents=True, exist_ok=True)
    db_path().write_bytes(b"this is not a database\n" * 256)
    app = build_app(runtime, sources=_server_sources(), dist_dir=tmp_path)
    watcher = RemoteNeedsWatcher(app.kit, sources=remote_needs.live_needs_sources)
    caplog.set_level(logging.DEBUG, logger=remote_needs.__name__)
    for _ in range(2):
        watcher._needs_scan_told()
    told = [r for r in caplog.records if r.name == remote_needs.__name__]
    assert [r.levelname for r in told] == ["WARNING", "DEBUG"], [r.getMessage() for r in told]
    said = told[0].getMessage()
    assert said.startswith("remote: the needs scan failed: the context store cannot be opened")
    assert str(db_path()) in said and damaged_store_recovery() in said, said
    assert told[0].exc_info is None


def test_a_scan_that_fails_on_a_bug_is_told_once_with_its_traceback(
    runtime: Runtime, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    app = build_app(runtime, sources=_server_sources(), dist_dir=tmp_path)

    def buggy() -> NeedsSources:
        raise RuntimeError("a bug in the scan")

    watcher = RemoteNeedsWatcher(app.kit, sources=buggy)
    for _ in range(3):
        watcher._needs_scan_told()
        watcher._needs_rescan()
    told = [r for r in caplog.records if r.name == remote_needs.__name__]
    assert [r.levelname for r in told] == ["WARNING"]
    assert told[0].exc_info is not None, "a bug's first failure keeps its traceback"


def test_a_project_whose_scan_keeps_failing_is_told_once_a_streak(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    real = remote_needs._needs_scan_project

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("a query the store could not answer")

    caplog.set_level(logging.INFO, logger=remote_needs.__name__)
    failing: set[str] = set()
    last = _scan(_crash(exit_status=1))
    monkeypatch.setattr(remote_needs, "_needs_scan_project", broken)
    for _ in range(3):
        kept = scan_needs_you(_sources(Fleet()), now=NOW, dismissed=(), failing=failing, kept=last)
        assert kept == last, "its last items stand, as for a read that failed"
    monkeypatch.setattr(remote_needs, "_needs_scan_project", real)
    scan_needs_you(_sources(Fleet()), now=NOW, dismissed=(), failing=failing)
    told = [(r.levelname, r.getMessage()) for r in caplog.records]
    assert told == [
        ("WARNING", "remote: the needs scan of prj_alpha failed"),
        ("INFO", "remote: the needs scan of prj_alpha works again"),
    ]
    assert failing == set()


def test_a_project_whose_read_keeps_failing_is_told_once_and_works_again_only_when_read_whole(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A scan that could not read all it needs decides nothing, so it has not recovered. It
    ended the streak and said "works again", and the read that failed was a debug line: a
    project frozen at its last items, every action on it a 503, was never told of (review
    of #243, round 28, 1/10). One warning naming the read, its traceback, for the streak,
    an outright failure in it too; "works again" once a scan reads it whole."""
    caplog.set_level(logging.INFO, logger=remote_needs.__name__)
    failing: set[str] = set()
    fleet = _crash(exit_status=1)
    last = scan_needs_you(_sources(fleet), now=NOW, dismissed=(), failing=failing)
    assert [item.kind for item in last] == ["crashed"]
    locked = _failing(_sources(fleet), "ended_agents", [])
    real = remote_needs._needs_scan_project

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("a query the store could not answer")

    for scan in (real, real, broken, real):
        monkeypatch.setattr(remote_needs, "_needs_scan_project", scan)
        kept = scan_needs_you(locked, now=NOW, dismissed=(), failing=failing, kept=last)
        assert kept == last, "its last items stand"
        assert failing == {"prj_alpha"}, "a scan that decides nothing is still failing"
    told = [(r.levelname, r.getMessage()) for r in caplog.records]
    assert told == [("WARNING", "remote: the needs scan of prj_alpha reading rows failed")]
    assert caplog.records[0].exc_info is not None, "the read's own traceback"
    assert scan_needs_you(_sources(fleet), now=NOW, dismissed=(), failing=failing) == last
    scan_needs_you(locked, now=NOW, dismissed=(), failing=failing, kept=last)
    told = [(r.levelname, r.getMessage()) for r in caplog.records]
    assert told[1:] == [
        ("INFO", "remote: the needs scan of prj_alpha works again"),
        ("WARNING", "remote: the needs scan of prj_alpha reading rows failed"),
    ], "a new streak is told anew"


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


def test_a_dismissal_is_held_apart_only_until_a_scan_has_read_it(
    runtime: Runtime, tmp_path: Path
) -> None:
    """A card dismissed while a scan runs is dropped from what that scan publishes, until a
    scan that read the dismissal from the file has published: no longer, or the watcher kept
    every id ever dismissed for as long as the server ran."""
    app = build_app(runtime, sources=_server_sources(), dist_dir=tmp_path)
    fleet = _working(_tail(_tool("toolu_q", "AskUserQuestion", **QUESTION)))
    watcher = RemoteNeedsWatcher(app.kit, sources=lambda: _sources(fleet))
    (card,) = watcher.scan_needs_now()
    record_needs_dismissal(card.id)
    watcher.needs_forget(card.id)
    assert watcher._forgotten == {card.id}
    assert watcher.scan_needs_now() == []
    assert watcher._forgotten == set(), "the file says it now"


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


def test_the_feeds_stamps_are_the_apis_whatever_zone_they_were_read_in(
    runtime: Runtime, tmp_path: Path
) -> None:
    """``since`` and ``scanned_at`` were made by hand, ``isoformat(timespec="seconds")``:
    copies of ``remote_server._iso_seconds`` that agreed with the API's other stamps only
    while the clock and every fact read were in UTC. A watcher on a clock in another zone
    said that zone's offset where every other stamp of the API says ``+00:00`` (sweep of
    #243, the hand-made stamps round 3 left in this file)."""
    elsewhere = timezone(timedelta(hours=-7))
    app = build_app(runtime, sources=_server_sources(), dist_dir=tmp_path)
    fleet = _working(_tail(_tool("toolu_q", "AskUserQuestion", **QUESTION)))
    watcher = RemoteNeedsWatcher(
        app.kit, sources=lambda: _sources(fleet), clock=lambda: NOW.astimezone(elsewhere)
    )
    app.kit.lane_state["needs"] = watcher
    (item,) = watcher.scan_needs_now()
    assert remote_needs.needs_scanned_iso(app.kit) == "2026-10-07T12:00:00+00:00"
    assert watcher.needs_payload_now()["scanned_at"] == "2026-10-07T12:00:00+00:00"
    shown = replace(item, since=item.since.astimezone(elsewhere)).needs_item_json()["since"]
    assert shown == item.since.astimezone(UTC).isoformat(timespec="seconds")
    assert isinstance(shown, str) and shown.endswith("+00:00")


def test_a_dismissal_holds_for_as_long_as_its_card_would_show(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pane that stays lost keeps its id until it is reaped, and an agent idle at its
    question keeps its own. A dismissal was dropped a week after it was made, at the next one
    written, and the card came back to every phone (review of #243, sweep 3). Each scan that
    still finds a dismissed item dates its dismissal again, once a day; one whose item is
    gone goes a week later, as before."""
    clock = [NOW]
    monkeypatch.setattr(remote_needs, "_needs_now", lambda: clock[0])
    lost = _row()
    fleet = Fleet(agents=[_status(lost, "lost", _session(lost))])
    app = build_app(runtime, sources=_server_sources(), dist_dir=tmp_path)
    watcher = RemoteNeedsWatcher(app.kit, sources=lambda: _sources(fleet), clock=lambda: clock[0])
    (card,) = watcher.scan_needs_now()
    record_needs_dismissal(card.id)
    for day in range(1, 9):
        clock[0] = NOW + timedelta(days=day, minutes=1)
        assert watcher.scan_needs_now() == []
    record_needs_dismissal("ny_another_card0")  # what no scan needed for a week goes now
    assert card.id in load_needs_dismissals()
    assert watcher.scan_needs_now() == [], "the pane is still lost, and still dismissed"
    fleet.agents.clear()  # reaped
    for day in range(9, 17):
        clock[0] = NOW + timedelta(days=day, minutes=1)
        assert watcher.scan_needs_now() == []
    record_needs_dismissal("ny_a_third_card0")
    assert card.id not in load_needs_dismissals(), "unneeded for a week: dropped"


def test_tmux_not_answering_is_one_card_through_a_restart_of_remote(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``fleet_down``'s id is the first scan that saw tmux stop answering, and the watcher
    kept that in memory alone: every Remote toggle, TUI restart or ``serve`` restart made the
    same outage a new card, its dismissal lost and the phone pushed again, and ``asq remote
    needs`` gave it an id and an age of its own each run (review of #243, sweep 3). The
    sighting is kept on disk, read by the next watcher and by the command, and let go when
    tmux answers again; one from before a live row was made is another outage's."""
    from aisquare.services.remote_server import RemoteKit

    one, two = _row("coder-1"), _row("coder-2")
    fleet = Fleet(agents=[_status(one, "unknown"), _status(two, "unknown")])
    monkeypatch.setattr(remote_needs, "live_needs_sources", lambda: _sources(fleet))

    def watcher_at(at: datetime) -> RemoteNeedsWatcher:
        return RemoteNeedsWatcher(
            RemoteKit(runtime), sources=lambda: _sources(fleet), clock=lambda: at
        )

    elsewhere = timezone(timedelta(hours=-7))  # the id is the same whatever zone a clock reads
    (down,) = watcher_at(NOW.astimezone(elsewhere)).scan_needs_now()
    assert down.kind == "fleet_down"
    monkeypatch.setattr(remote_needs, "_needs_now", lambda: NOW + timedelta(minutes=5))
    items = remote_needs.needs_cli_payload()["items"]
    assert isinstance(items, list)
    (said,) = items
    assert (said["id"], said["since"]) == (down.id, "2026-10-07T12:00:00+00:00")
    record_needs_dismissal(down.id)
    restarted = watcher_at(NOW + timedelta(minutes=10))
    assert restarted.scan_needs_now() == [], "the same outage, still dismissed"
    fleet.agents = [_status(row, "waiting", _session(row, state="waiting")) for row in (one, two)]
    assert restarted.scan_needs_now() == []
    assert remote_needs._needs_first_seen_kept() == {}, "tmux answers: let go on disk too"
    fleet.agents = [_status(one, "unknown"), _status(two, "unknown")]
    (again,) = watcher_at(NOW + timedelta(minutes=12)).scan_needs_now()
    assert again.id != down.id, "tmux answered in between: a new outage, a new card"
    spawned = _row("coder-3", created=NOW + timedelta(minutes=20))
    fleet.agents.append(_status(spawned, "unknown"))
    (later,) = watcher_at(NOW + timedelta(minutes=30)).scan_needs_now()
    assert later.id != again.id and later.since == NOW + timedelta(minutes=30), (
        "a row made since the kept sighting: tmux answered while no watcher looked"
    )


def test_a_lost_panes_card_keeps_its_date_through_a_restart_and_in_asq_remote_needs(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pane gone has no date of its own, so its card is dated by the first scan that saw
    it, in the same memory as ``fleet_down``'s: a restart of Remote said it had just gone,
    and ``asq remote needs`` said so every run (sweep of #243, the instance of round 5's
    ``fleet_down`` in the same memory)."""
    from aisquare.services.remote_server import RemoteKit

    lost = _row()
    fleet = Fleet(agents=[_status(lost, "lost", _session(lost))])
    monkeypatch.setattr(remote_needs, "live_needs_sources", lambda: _sources(fleet))

    def watcher_at(at: datetime) -> RemoteNeedsWatcher:
        return RemoteNeedsWatcher(
            RemoteKit(runtime), sources=lambda: _sources(fleet), clock=lambda: at
        )

    (card,) = watcher_at(NOW).scan_needs_now()
    (again,) = watcher_at(NOW + timedelta(minutes=10)).scan_needs_now()
    assert (again.id, again.since) == (card.id, NOW)
    monkeypatch.setattr(remote_needs, "_needs_now", lambda: NOW + timedelta(minutes=15))
    items = remote_needs.needs_cli_payload()["items"]
    assert isinstance(items, list)
    assert [item["since"] for item in items] == ["2026-10-07T12:00:00+00:00"]


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


def test_a_dismissal_that_cannot_be_saved_says_so_and_keeps_the_card(live: Live) -> None:
    """Saved nowhere, it was a 500 with no JSON; hidden in memory alone, the card came back
    with the next start, the phone told it was gone for good."""
    card = live.card("permission")
    remote_needs_path().unlink(missing_ok=True)
    remote_needs_path().mkdir(parents=True)  # a path no file can be written to
    response = live.client.post(live.url("needs/dismiss"), json={"id": card["id"]})
    assert response.status_code == 503, response.text
    assert response.json()["error"] == "unavailable"
    assert response.json()["message"].startswith("the dismissal could not be saved: ")
    assert card["id"] in [item["id"] for item in live.feed()]
    assert not any(" needs/dismiss " in line for line in live.audit())


def test_a_needs_read_over_a_store_that_cannot_be_opened_is_a_503_as_every_read_is(
    runtime: Runtime, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The feed, a dismissal and an answer scan for themselves when no scan has run, and that
    scan raised: a bare 500 ``text/plain``, its traceback on the terminal once a request,
    where ``api/projects`` and every other read answer 503 ``unavailable`` in JSON and the
    watcher tells a failing scan once a streak (review of #243, sweep 3)."""
    from aisquare.core.paths import db_path

    db_path().parent.mkdir(parents=True, exist_ok=True)
    db_path().write_bytes(b"this is not a database\n" * 256)
    app = build_app(runtime, sources=_server_sources(), dist_dir=tmp_path)
    client = make_client(app)
    assert unlock(client, runtime).status_code == 200
    runtime.set_allow_write(True)
    caplog.set_level(logging.DEBUG, logger=remote_needs.__name__)
    api = f"{base(runtime)}/api"
    answers = [
        client.get(f"{api}/needs"),
        client.get(f"{api}/needs"),
        client.post(f"{api}/needs/dismiss", json={"id": "ny_0000000000000000"}),
        client.post(f"{api}/needs/answer", json={"id": "ny_0000000000000000", "keys": ["1"]}),
    ]
    for response in answers:
        assert response.status_code == 503, response.text
        assert response.headers["content-type"].startswith("application/json")
        assert response.json() == {"error": "unavailable", "message": "file is not a database"}
    told = [r for r in caplog.records if r.name == remote_needs.__name__ and r.levelno >= 30]
    assert len(told) == 1 and told[0].exc_info is None, [r.getMessage() for r in told]


def test_an_answer_is_refused_while_writes_are_off(live: Live) -> None:
    card = live.card("permission")
    response = live.client.post(live.url("needs/answer"), json={"id": card["id"], "keys": ["1"]})
    assert response.status_code == 403
    assert response.json() == {"error": "read_only", "message": READ_ONLY_REASON}
    assert live.tmux.typed == []


@pytest.mark.parametrize(
    ("change", "status", "error"),
    [
        ("writes switched off", 403, "read_only"),
        ("the device revoked", 401, "unauthorized"),
        ("auto-off passed", 404, "not_found"),
    ],
)
def test_an_answer_asks_the_gates_again_in_the_thread_that_types_it(
    live: Live, monkeypatch: pytest.MonkeyPatch, change: str, status: int, error: str
) -> None:
    """The gates are read when the request arrives, and the typing comes after the agent is
    derived again and a thread of the shared pool is free: as a queued write, an extend and
    a revoke did, an answer whose writes were switched off, whose device was revoked or
    whose Remote passed its auto-off meanwhile was typed all the same (review of #243,
    round 4)."""
    from aisquare.services.remote_server import _remote_now

    live.runtime.set_allow_write(True)
    card = live.card("permission")
    (device_id,) = live.runtime.device_ids()
    derive = remote_needs.needs_agent_now

    def derived_meanwhile(project: ProjectInfo, label: str) -> Any:
        if change == "writes switched off":
            live.runtime.set_allow_write(False)
        elif change == "the device revoked":
            live.runtime.revoke_device(device_id)
        else:
            live.runtime.set_auto_off(_remote_now() - timedelta(seconds=1))
        return derive(project, label)

    monkeypatch.setattr(remote_needs, "needs_agent_now", derived_meanwhile)
    response = live.client.post(live.url("needs/answer"), json={"id": card["id"], "keys": ["1"]})
    assert (response.status_code, response.json()["error"]) == (status, error), response.text
    assert live.tmux.typed == []
    assert not any(" needs/answer " in line for line in live.audit())


def test_an_answer_is_a_write_on_the_write_pool_counted_and_named_while_it_runs(
    live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It ran on the loop's shared pool, which every read, frame and unlock waits on, its
    re-derivation a scan of the project with tmux, while it held the agent's lock: with
    tmux not answering, a few taps stalled every phone. It was past the per-device cap of
    writes waiting, and a quit or a Remote off meanwhile did not name it among the writes
    still running (review of #243, round 6)."""
    from aisquare.services import remote_server

    live.runtime.set_allow_write(True)
    card = live.card("permission")
    derive = remote_needs.needs_agent_now
    seen: list[tuple[str, list[str]]] = []

    def derived_where(project: ProjectInfo, label: str) -> Any:
        seen.append((threading.current_thread().name, remote_server.remote_writes_running()))
        return derive(project, label)

    monkeypatch.setattr(remote_needs, "needs_agent_now", derived_where)
    response = live.client.post(live.url("needs/answer"), json={"id": card["id"], "keys": ["1"]})
    assert response.status_code == 200, response.text
    ((thread, running),) = seen
    assert thread.startswith("asq-remote-write"), thread
    assert running == ["needs/answer for coder-1"]
    assert remote_server.remote_writes_running() == []
    watcher = live.app.kit.lane_state["needs"]
    _until_true(lambda: watcher.needs_lookup(card["id"]) is not None)  # this pane never moved
    monkeypatch.setattr(remote_server, "WRITE_WAITING_PER_DEVICE", 0)
    again = live.client.post(live.url("needs/answer"), json={"id": card["id"], "keys": ["1"]})
    assert (again.status_code, again.json()["error"]) == (409, "busy"), again.text
    assert live.tmux.typed == [("keys", "%7", "1")], "the one past the cap was never typed"


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


@pytest.mark.parametrize(
    "then_up", ["the card's call", "the call beside it", "a prompt naming neither", "no prompt"]
)
def test_an_answer_to_one_of_two_calls_is_typed_only_while_the_screen_still_shows_it(
    live: Live, monkeypatch: pytest.MonkeyPatch, then_up: str
) -> None:
    """Review of #259, round 8, 1/9: with two calls pending the card is the call whose prompt
    the screen showed when it was derived, and the screen is read again right before the
    keys. The prompt answered at the terminal meanwhile and the next one up, the tap is a
    409 ``stale``, nothing typed, and the card is out of the feed until the next scan.

    Round 9, 2/8: the refusal's ``current`` was empty, and the page said "nothing waits on
    coder-1 now" while ``rm -rf dist`` waited. It is what the screen shows instead: the call
    it names, or the dialog card when it names neither.

    Round 10, 1/9: with no prompt drawn at all, the prompt answered at the terminal and its
    call at work, it was the dialog card too, a dialog nobody drew, and the tap was told
    "shows another prompt". Nothing is up, and the refusal says so."""
    monkeypatch.setattr(remote_needs, "NEEDS_RESCAN_AFTER_ANSWER", 30.0)
    live.runtime.set_allow_write(True)
    called = datetime.now(UTC) - timedelta(minutes=1)
    push, rm = replace(_PUSH, at=called), replace(_RM, at=called)
    live.fleet.tails["/transcripts/coder-1.jsonl"] = _tail(push, rm, at=called)
    live.fleet.screen = _screen_of(push, (push, rm))
    card = live.card("permission")
    assert card["id"] == needs_item_id(PROJECT.id, "permission", push.tool_use_id)
    assert card["detail"]["input"]["command"] == "git push --force origin main"
    up = {"the card's call": push, "the call beside it": rm, "no prompt": None}.get(
        then_up, _SUB_AGENTS
    )
    live.tmux.screen = _screen_of(up, (push, rm))
    response = live.client.post(live.url("needs/answer"), json={"id": card["id"], "keys": ["1"]})
    watcher = live.app.kit.lane_state["needs"]
    if then_up == "the card's call":
        assert response.status_code == 200, response.text
        assert live.tmux.typed == [("keys", "%7", "1")]
        return
    assert (response.status_code, response.json()["error"]) == (409, "stale"), response.text
    assert live.tmux.typed == [], "the 1 would have approved rm -rf dist"
    assert watcher.needs_lookup(card["id"]) is None
    assert not any(" needs/answer " in line for line in live.audit())
    if up is None:
        assert response.json()["message"] == "coder-1 shows no prompt now — nothing was sent"
        assert response.json()["current"] == [], "no dialog card for a screen with none on it"
        return
    assert "shows another prompt" in response.json()["message"]
    (instead,) = response.json()["current"]
    assert instead["agent"] == "coder-1" and instead["id"] != card["id"]
    if then_up == "the call beside it":
        assert instead["id"] == needs_item_id(PROJECT.id, "permission", rm.tool_use_id)
        assert instead["detail"]["input"]["command"] == "rm -rf dist"
        assert instead["answers"], "the call on screen, as the next scan's card will be"
    else:
        assert instead["reason"] == "coder-1 shows a dialog that needs you"
        assert instead["answers"] == []


def test_an_answer_whose_screen_cannot_be_read_is_unknown_not_another_prompt(
    live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 9, 3/8: a capture that failed read as another prompt up. The tap
    was refused as ``stale``, which said so, and the card left every phone, its prompt maybe
    the one up. Unknown is a 503 to try again: nothing typed, the card kept."""
    monkeypatch.setattr(remote_needs, "NEEDS_RESCAN_AFTER_ANSWER", 30.0)
    live.runtime.set_allow_write(True)
    called = datetime.now(UTC) - timedelta(minutes=1)
    push, rm = replace(_PUSH, at=called), replace(_RM, at=called)
    live.fleet.tails["/transcripts/coder-1.jsonl"] = _tail(push, rm, at=called)
    live.fleet.screen = _screen_of(push, (push, rm))
    card = live.card("permission")
    assert card["id"] == needs_item_id(PROJECT.id, "permission", push.tool_use_id)
    live.tmux.screen = None  # capture-pane fails
    response = live.client.post(live.url("needs/answer"), json={"id": card["id"], "keys": ["1"]})
    assert (response.status_code, response.json()["error"]) == (503, "fleet_unavailable")
    assert "could not show coder-1's screen" in response.json()["message"]
    assert "another prompt" not in response.json()["message"]
    assert live.tmux.typed == []
    assert live.app.kit.lane_state["needs"].needs_lookup(card["id"]) is not None
    assert not any(" needs/answer " in line for line in live.audit())


def test_an_answer_whose_rederivation_cannot_read_the_screen_keeps_its_card(
    live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 11, 3/13: the re-derivation before the screen check
    (``needs_agent_now``) read the screen through the live sources, which turned a failed
    capture into ``None``, a screen that names no call: the card about one of two calls was
    not among the items, and the tap a 409 ``stale``, the card off the page, before
    ``needs_prompt_now``'s 503 could keep it. A capture that fails is a failed read: 503,
    nothing typed, the card kept."""
    monkeypatch.setattr(remote_needs, "NEEDS_RESCAN_AFTER_ANSWER", 30.0)
    live.runtime.set_allow_write(True)
    called = datetime.now(UTC) - timedelta(minutes=1)
    push, rm = replace(_PUSH, at=called), replace(_RM, at=called)
    live.fleet.tails["/transcripts/coder-1.jsonl"] = _tail(push, rm, at=called)
    live.fleet.screen = _screen_of(push, (push, rm))
    card = live.card("permission")
    assert card["id"] == needs_item_id(PROJECT.id, "permission", push.tool_use_id)

    # The live sources' own screen reader, on the fake tmux, whose capture-pane now fails.
    sources = replace(_sources(live.fleet), pane_screen=_LIVE_NEEDS_SOURCES().pane_screen)
    monkeypatch.setattr(remote_needs, "live_needs_sources", lambda: sources)
    live.tmux.screen = None
    response = live.client.post(live.url("needs/answer"), json={"id": card["id"], "keys": ["1"]})
    assert (response.status_code, response.json()["error"]) == (503, "unavailable"), response.text
    assert "can't find pane" in response.json()["message"]
    assert live.tmux.typed == []
    assert live.app.kit.lane_state["needs"].needs_lookup(card["id"]) is not None


def test_the_live_screen_tmux_cannot_show_is_a_failed_read_not_an_empty_screen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Review of #259, round 11, 3/13: the live sources' ``pane_screen`` answered a capture
    that failed with ``None``, which the scan took for a screen naming nothing. It raises, so
    the scan's read fails (its last items stand, an action's check is a 503); the check
    right before a quick answer's keys still reads it as unknown (``None``)."""
    from aisquare.core.tmux import TmuxError

    tmux = FakeTmux(reference=NOW)  # its screen is None: capture-pane fails
    monkeypatch.setattr(fleet_service, "server", lambda config=None: tmux)
    row = _row()
    status = _status(row, "attention", _session(row), socket_at=tmp_path / "asq")
    with pytest.raises(TmuxError, match="can't find pane"):
        remote_needs.live_needs_sources().pane_screen(status)
    assert remote_needs._needs_screen_now(status) is None


def test_an_answered_card_leaves_the_feed_at_once_as_a_dismissed_one_does(
    live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An answer only scheduled a scan a second on, and its card stayed in the feed until it
    ran: on every page with its buttons live, and before the push sender, whose window
    closing meanwhile pushed the answered prompt to every phone, the one that answered it
    too (review of #243, sweep 5). It leaves at once, and a scan begun before the answer
    is over does not bring it back, though this fleet still shows the prompt."""
    monkeypatch.setattr(remote_needs, "NEEDS_RESCAN_AFTER_ANSWER", 30.0)
    live.runtime.set_allow_write(True)
    card = live.card("permission")
    watcher = live.app.kit.lane_state["needs"]
    try:
        response = live.client.post(
            live.url("needs/answer"), json={"id": card["id"], "keys": ["1"]}
        )
        assert response.status_code == 200, response.text
        assert card["id"] not in [item.id for item in watcher.needs_items_now()]
        assert [item["kind"] for item in live.feed()] == ["board_question"], (
            "the feed's own scan, begun right after the answer, still shows the prompt"
        )
    finally:
        watcher.stop_watching()


def test_an_answered_card_is_gone_before_its_audit_line_is_written(
    live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, remote focus, 2/12: the card left the feed only once the audit line
    was written, an await after the keys: a scan that published in between put the answered
    card back on every page with its buttons live, and before the push sender, and a second
    tap on a question or a plan typed its keys again. It leaves as the keys are typed."""
    monkeypatch.setattr(remote_needs, "NEEDS_RESCAN_AFTER_ANSWER", 30.0)
    live.runtime.set_allow_write(True)
    card = live.card("permission")
    watcher = live.app.kit.lane_state["needs"]
    heard: list[list[str]] = []
    live.app.kit.needs_listeners.append(lambda items, at: heard.append([i.id for i in items]))
    audit = live.app.kit.kit_audit
    during: list[list[str]] = []

    def scanned_meanwhile(device: Any, endpoint: str, summary: str) -> None:
        if endpoint == "needs/answer":
            watcher.scan_needs_now()  # the watcher's tick, as the line is being written
            during.append([item.id for item in watcher.needs_items_now()])
        audit(device, endpoint, summary)

    monkeypatch.setattr(live.app.kit, "kit_audit", scanned_meanwhile)
    try:
        body = {"id": card["id"], "keys": ["1"]}
        response = live.client.post(live.url("needs/answer"), json=body)
        assert response.status_code == 200, response.text
        assert live.tmux.typed == [("keys", "%7", "1")]
        assert len(during) == 1 and during[0], "the scan published the rest of the feed"
        assert card["id"] not in during[0], "not the answered card again"
        assert heard and card["id"] not in heard[-1], "nor handed to the push sender"
        assert live.audit()[-1].endswith("keys=[1] text=0ch enter=False")
    finally:
        watcher.stop_watching()


def test_an_answered_prompt_still_up_comes_back_with_the_first_scan_begun_after_the_answer(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing of an answer is kept on disk, unlike a dismissal: keys that did not close the
    prompt leave it in the feed by the scan after the answer. A scan under way as the answer
    came read the fleet before it, and one begun at once may read the pane before the agent
    acted on the key: neither brings the card back, nor hands it to the listeners."""
    monkeypatch.setattr(remote_needs, "NEEDS_RESCAN_AFTER_ANSWER", 1.0)
    app = build_app(runtime, sources=_server_sources(), dist_dir=tmp_path)
    status, tail = _asking(datetime.now(UTC))
    fleet = Fleet(agents=[status])
    fleet.tails["/transcripts/coder-1.jsonl"] = tail
    started, release = threading.Event(), threading.Event()
    holding = [False]

    def projects() -> list[ProjectInfo]:
        if holding[0]:
            holding[0] = False
            started.set()
            assert release.wait(5)
        return [PROJECT]

    heard: list[list[str]] = []
    app.kit.needs_listeners.append(lambda items, at: heard.append([item.id for item in items]))
    watcher = RemoteNeedsWatcher(
        app.kit, sources=lambda: replace(_sources(fleet), list_projects=projects)
    )
    try:
        (card,) = watcher.scan_needs_now()
        holding[0] = True
        under_way = threading.Thread(target=watcher.scan_needs_now)
        under_way.start()
        assert started.wait(5)
        watcher.needs_answered(card.id)
        assert watcher.needs_items_now() == [] and watcher.needs_items_json() == []
        release.set()
        under_way.join(5)
        assert watcher.needs_scan_for_request() is None  # begun at once
        assert watcher.needs_items_now() == [] and heard[1:3] == [[], []]
        _until_true(lambda: [item.id for item in watcher.needs_items_now()] == [card.id])
        assert heard[-1] == [card.id], "the same card, its id unchanged"
    finally:
        release.set()
        watcher.stop_watching()


def test_an_answered_prompt_still_up_comes_back_by_the_clock_the_rescan_waits_on(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The answer and the scan were stamped by ``time.monotonic``, the rescan's wait kept by
    the clock a lock's timeout keeps. On Windows before Python 3.13 the first steps by the
    system tick, and at 15.6001 ms the timer's whole second read as 0.998 s: the scan it
    began kept the prompt still up hidden, till the next scan 3 s on (round 7 of #243). A
    ``time.monotonic`` that runs a tenth slow is that disagreement, every time."""
    monkeypatch.setattr(remote_needs, "NEEDS_RESCAN_AFTER_ANSWER", 0.3)

    class SlowMonotonic:
        def __getattr__(self, name: str) -> Any:
            return getattr(time, name)

        @staticmethod
        def monotonic() -> float:
            return time.monotonic() * 0.9

    monkeypatch.setattr(remote_needs, "time", SlowMonotonic())
    app = build_app(runtime, sources=_server_sources(), dist_dir=tmp_path)
    status, tail = _asking(datetime.now(UTC))
    fleet = Fleet(agents=[status])
    fleet.tails["/transcripts/coder-1.jsonl"] = tail
    watcher = RemoteNeedsWatcher(app.kit, sources=lambda: _sources(fleet))
    try:
        (card,) = watcher.scan_needs_now()
        watcher.needs_answered(card.id)
        assert watcher.needs_items_now() == []
        _until_true(lambda: [item.id for item in watcher.needs_items_now()] == [card.id], 2.0)
    finally:
        watcher.stop_watching()


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


def test_an_answer_in_words_beside_no_keys_is_words_as_send_keys_takes_it(live: Live) -> None:
    """A client that always sends ``keys``, empty when the human typed, sent no key to lose
    the order of: ``send-keys`` takes the body, and a quick answer refused it as both."""
    live.runtime.set_allow_write(True)
    card = live.card("permission")
    body = {"id": card["id"], "keys": [], "text": "yes", "enter": True}
    response = live.client.post(live.url("needs/answer"), json=body)
    assert response.status_code == 200, response.text
    assert live.tmux.typed == [("text", "%7", "yes"), ("keys", "%7", "Enter")]


@pytest.mark.parametrize("text", ["\x03", "yes\x1b[201~", "no\x7f", "ok\x04", "1\r"])
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


def test_an_answer_needs_the_listing_it_rechecks_against(
    live: Live, caplog: pytest.LogCaptureFixture
) -> None:
    """``needs_agent_now``'s own 503 reaches the phone as it is. The answer's catch-all took
    it for a bug, said a warning on every tap, and made it anew (review of #243, round 28,
    6/10)."""
    live.runtime.set_allow_write(True)
    card = live.card("permission")
    live.fleet.listing_fails = True
    caplog.set_level(logging.DEBUG, logger=remote_needs.__name__)
    response = live.client.post(live.url("needs/answer"), json={"id": card["id"], "keys": ["1"]})
    assert response.status_code == 503
    said = "alpha could not be read: tmux is not installed"
    assert response.json() == {"error": "unavailable", "message": said}
    assert live.tmux.typed == []
    assert not remote_agent_lock(PROJECT.id, "coder-1").locked(), "the lock is let go"
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_an_answer_whose_store_fails_as_it_rechecks_is_a_503_not_a_bare_500(
    live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scan read the card, and the store failed as the answer derived the agent again:
    only the fleet's own errors were answered, and this one was a 500 with no JSON."""
    from aisquare.core.store import StoreUnopenable

    live.runtime.set_allow_write(True)
    card = live.card("permission")

    def unopenable(project: ProjectInfo) -> list[FleetAgentStatus]:
        raise StoreUnopenable("file is not a database")

    monkeypatch.setattr(
        remote_needs,
        "live_needs_sources",
        lambda: replace(_sources(live.fleet), list_agents=unopenable),
    )
    response = live.client.post(live.url("needs/answer"), json={"id": card["id"], "keys": ["1"]})
    assert response.status_code == 503, response.text
    said = "alpha could not be read: file is not a database"
    assert response.json() == {"error": "unavailable", "message": said}
    assert live.tmux.typed == []
    assert not remote_agent_lock(PROJECT.id, "coder-1").locked(), "the lock is let go"


def test_tmux_failing_mid_answer_is_said_and_still_on_the_trail(live: Live) -> None:
    live.runtime.set_allow_write(True)
    card = live.card("permission")
    live.tmux.fail = True
    response = live.client.post(live.url("needs/answer"), json={"id": card["id"], "keys": ["1"]})
    assert response.status_code == 503 and response.json()["error"] == "fleet_unavailable"
    assert live.audit()[-1].endswith("enter=False failed"), "part of it may have reached the pane"


@pytest.mark.parametrize("timed_out", [True, False])
def test_an_answer_whose_keys_timed_out_leaves_the_feed_as_an_answered_one(
    live: Live, monkeypatch: pytest.MonkeyPatch, timed_out: bool
) -> None:
    """Review of #259, round 8, 2/9: a send that timed out was a 503, audited, but its card
    stayed live on every phone, and a second tap typed its digit again after a wedged
    server ran the first. It leaves the feed as an answered card does; one tmux refused
    typed nothing, and its card stays to be answered again. Round 10, 8/9: its line says
    ``delivered=maybe``, as every write that timed out does (``fleet.PaneWrite``)."""
    monkeypatch.setattr(remote_needs, "NEEDS_RESCAN_AFTER_ANSWER", 30.0)
    live.runtime.set_allow_write(True)
    card = live.card("permission")
    if timed_out:
        live.tmux.timing_out = True
    else:
        live.tmux.fail = True
    response = live.client.post(live.url("needs/answer"), json={"id": card["id"], "keys": ["1"]})
    assert (response.status_code, response.json()["error"]) == (503, "fleet_unavailable")
    assert live.audit()[-1].endswith(f"enter=False {'delivered=maybe' if timed_out else 'failed'}")
    watcher = live.app.kit.lane_state["needs"]
    if timed_out:
        said = response.json()["message"]
        assert "may still arrive" in said and "before you answer again" in said
        assert watcher.needs_lookup(card["id"]) is None
        assert card["id"] not in [item["id"] for item in live.feed()]
    else:
        assert watcher.needs_lookup(card["id"]) is not None


@pytest.mark.parametrize("timed_out", [False, True], ids=["refused", "timed out"])
def test_an_answer_in_words_whose_enter_did_not_go_says_it_waits_and_leaves_the_feed(
    live: Live, monkeypatch: pytest.MonkeyPatch, timed_out: bool
) -> None:
    """Review of #259, round 12, 1/9: the text went in and its Enter was refused, and the
    answer was a 503 "tmux could not type it", audited ``failed``, its card live. Answered
    again, the text went in a second time beside the first, and the next Enter sent both.
    It says the answer waits at the prompt (or that its Enter may still arrive), its line
    says so, and the card leaves the feed, as ``send-keys`` does (round 11, 4/13)."""
    monkeypatch.setattr(remote_needs, "NEEDS_RESCAN_AFTER_ANSWER", 30.0)
    live.runtime.set_allow_write(True)
    card = live.card("permission")
    if timed_out:
        live.tmux.timing_out = True  # send_keys alone: the text goes in
    else:
        live.tmux.fail = True
    body = {"id": card["id"], "text": "use the staging remote", "enter": True}
    response = live.client.post(live.url("needs/answer"), json=body)
    assert (response.status_code, response.json()["error"]) == (503, "fleet_unavailable")
    assert live.tmux.typed == [("text", "%7", "use the staging remote")]
    said = response.json()["message"]
    assert said.startswith("coder-1: typed it at its prompt, but "), said
    if timed_out:
        assert "it may still arrive, and send it" in said, said
    else:
        assert said == (
            "coder-1: typed it at its prompt, but tmux could not press Enter (no server running "
            "on /tmp/tmux-1000/asq) — press Enter on the pad to send it"
        )
    verdict = "delivered=maybe" if timed_out else "delivered=unsent"
    assert live.audit()[-1].endswith(f"text=22ch enter=True {verdict}")
    assert live.app.kit.lane_state["needs"].needs_lookup(card["id"]) is None
    assert card["id"] not in [item["id"] for item in live.feed()]


def test_an_answer_tmux_typed_a_part_of_and_refused_the_rest_of_is_unsent_not_maybe(
    live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #259, round 12, 7/9: a part typed and the rest refused was ``maybe``, the word
    for "did not answer in time", audited ``delivered=maybe``, where nothing more can arrive.
    It is ``unsent``: the sentence says how much went in, and the card leaves the feed, where
    a second tap typed the whole answer after the part."""
    from aisquare.core.tmux import TmuxError, TmuxPartlyTyped

    def partly(pane_id: str, text: str) -> None:
        raise TmuxPartlyTyped(TmuxError("server exited unexpectedly"), 512, len(text))

    monkeypatch.setattr(remote_needs, "NEEDS_RESCAN_AFTER_ANSWER", 30.0)
    monkeypatch.setattr(live.tmux, "send_literal", partly)
    live.runtime.set_allow_write(True)
    card = live.card("permission")
    body = {"id": card["id"], "text": "use the staging remote " * 40, "enter": True}
    response = live.client.post(live.url("needs/answer"), json=body)
    assert (response.status_code, response.json()["error"]) == (503, "fleet_unavailable")
    said = response.json()["message"]
    assert said.startswith("tmux typed 512 of the 920 bytes of the answer into coder-1's pane, ")
    assert "then refused the rest (server exited unexpectedly)" in said, said
    assert live.tmux.typed == [], "no Enter after the part"
    assert live.audit()[-1].endswith("enter=True delivered=unsent")
    assert live.app.kit.lane_state["needs"].needs_lookup(card["id"]) is None


def test_the_needs_routes_write_their_audit_lines_off_the_event_loop(
    live: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An audit line opens ``remote-audit.log`` and appends to it, and the first one makes the
    file and restricts it to this account, on Windows an ``icacls`` run: file work, which the
    write dispatcher, extend, revoke and the push routes do in a worker thread. A dismissal,
    which no write switch gates and so can be a fresh home's first line, and both lines of an
    answer did it on the event loop, every request and every socket's tick waiting on it
    (sweep of #243, the instances round 3 left in this file)."""
    import asyncio

    written: list[tuple[str, bool]] = []
    audit = live.runtime.audit

    def audit_where_it_runs(device_id: str, endpoint: str, summary: str) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            written.append((endpoint, False))
        else:
            written.append((endpoint, True))
        audit(device_id, endpoint, summary)

    monkeypatch.setattr(live.runtime, "audit", audit_where_it_runs)
    live.runtime.set_allow_write(True)
    card = live.card("permission")
    body = {"id": card["id"], "keys": ["1"]}
    live.tmux.fail = True
    assert live.client.post(live.url("needs/answer"), json=body).status_code == 503
    live.tmux.fail = False
    assert live.client.post(live.url("needs/answer"), json=body).status_code == 200
    question = live.card("board_question")
    dismissed = live.client.post(live.url("needs/dismiss"), json={"id": question["id"]})
    assert dismissed.status_code == 200
    assert written == [
        ("needs/answer", False),
        ("needs/answer", False),
        ("needs/dismiss", False),
    ]
    assert live.audit()[-3].endswith("enter=False failed")
    assert live.audit()[-1].endswith(
        f"needs/dismiss {question['id']} board_question manager@prj_alpha"
    )


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
