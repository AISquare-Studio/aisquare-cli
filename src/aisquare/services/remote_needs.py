"""Needs-you: what in the fleet is waiting on the human, across every project (SPEC §4).

The board rings its bell (``attention``) for four Claude Code notification types
and nothing else, and even that one is lossy: ``mark_attention`` flips the row
only on the way in, and main installs no ``PreToolUse`` or ``PermissionRequest``
hook, so the second to Nth permission prompt of a turn leave no trace on the
board at all. An ``AskUserQuestion`` card, a plan waiting for approval, a turn
that ended on a question, a manager asking on the board, a usage limit and a
crashed agent all read as idle, or as nothing, in ``fleet ls``. So the scan reads
the facts main already keeps — the derived fleet state, the board, and the tail
of each agent's own transcript — and classifies each agent by a fixed order of
rules (:func:`needs_from_agent`), each project by a few more (``crashed``,
``manager_down``, ``fleet_down``) and the board by who asked whom
(:func:`needs_from_board`).

Every item has an id derived from what it is about (the OLDEST pending
``tool_use`` of a permission prompt, the marker record of an interruption, the
seq of a board event), so it keeps its id from scan to scan and a new prompt is a
new id — and a new push. A ``reason`` is a fixed template with every interpolated
name passed through :func:`needs_push_safe`, because it is what a lock screen
shows; the content a human must read before answering (the full command, every
question and option, the plan) lives in ``excerpt`` and ``detail``, which are
served to an unlocked page and never pushed.

``asq remote needs`` runs one scan, here and now. The server's seams still
answer as if nothing had been scanned, and :func:`needs_agent_now` refuses
rather than guess, until the watcher and the routes that serve the scan exist.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import threading
from collections.abc import Callable, Collection, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, TypeVar

from aisquare.models import (
    CLOSED_STATUSES,
    FleetAgent,
    FleetAgentStatus,
    ProjectInfo,
    TeamEvent,
    TeamSession,
)
from aisquare.services.transcript import PendingTool, TranscriptTail, read_transcript_tail

if TYPE_CHECKING:
    from starlette.routing import BaseRoute

    from aisquare.core.config import AccountsSettings
    from aisquare.services.remote_server import RemoteKit

log = logging.getLogger(__name__)

NEEDS_KINDS = (
    "permission",
    "question",
    "plan",
    "board_question",
    "manager_down",
    "crashed",
    "limited",
    "lost",
    "fleet_down",
    "asked",
    "board_result",
    "interrupted",
)
"""Every kind of item, in rank order: the feed sorts by this, then by ``since``."""

NEEDS_SCAN_SECONDS = 3.0
"""How often the watcher scans, while a device exists to show it to."""

DIALOG_SETTLE_SECONDS = 3.0
"""How long an action waits, after one Escape, for a dialog to close or a prompt to show."""

QUESTION_HORIZON = timedelta(hours=24)
"""A board question or result older than this needs nobody any more."""

EXCERPT_CHARS = 280
"""The longest ``excerpt``: one plain line a card shows under its reason."""

NEEDS_BOARD_EVENTS = 300
"""How many of a project's newest board events one scan reads."""

CRASH_WINDOW = timedelta(hours=1)
"""How long after its end a crashed agent stays an item."""

NEEDS_ANSWER_TEXT_MAX = 2_048
"""The longest ``text`` a quick answer types: ``send-keys``' own cap (SPEC §2.6), at most
16 tmux spawns of a hex-chunked paste even for 4-byte UTF-8."""

NEEDS_ID_MAX = 64
"""The longest item id a body may carry (``ny_`` and 16 hex digits fit with room)."""

NEEDS_RESCAN_AFTER_ANSWER = 1.0
"""Seconds from a quick answer to the scan that clears its card: long enough for the agent
to act on the key (its pane moves, its transcript records the answer)."""

LIMIT_DIALOG = re.compile(r"session paused|usage limit|usage credits", re.IGNORECASE)
"""An attention notification that is Claude Code 2.1.292's usage-limit dialog ("Session
paused — choose: continue on usage credits or switch models") rather than a permission
prompt. A Claude Code string, not a contract: matched loosely, pinned by a test."""

NEEDS_ANSWER_KEYS = frozenset(
    {*"123456789", "Escape", "Enter", "Up", "Down", "Space", "Tab", "y", "n"}
)
"""The keys a quick answer may send: digits that pick an option, and the few that move,
confirm or cancel. Never a control key: an answer is never an exit."""

OWNER_ROLES = frozenset({"", "owner", "user", "human", "all", "everyone"})
"""A board ``--to`` that addresses the human (no ``--to`` at all included)."""

_NEEDS_ANSWERABLE = frozenset({"permission", "question", "plan", "asked", "interrupted"})
_NEEDS_BOARD_KINDS = frozenset({"board_question", "board_result"})

_NEEDS_ACTIONS: dict[str, tuple[str, ...]] = {
    "permission": ("answer", "open", "dismiss"),
    "question": ("answer", "open", "dismiss"),
    "plan": ("answer", "open", "dismiss"),
    "asked": ("tell", "open", "dismiss"),
    "interrupted": ("tell", "open", "dismiss"),
    "board_question": ("reply", "dismiss"),
    "board_result": ("reply", "dismiss"),
    "limited": ("switch", "open", "dismiss"),
    "lost": ("restart", "stop", "dismiss"),
    "crashed": ("restart", "dismiss"),
    "manager_down": ("restart", "dismiss"),
    "fleet_down": ("open", "dismiss"),
}
"""What each kind's card offers (hints for the page)."""

_NEEDS_PUSH_DELAY: dict[str, timedelta] = {
    "crashed": timedelta(seconds=30),
    # These three flash during a restart or a switch, between the kill and the row's end.
    "lost": timedelta(seconds=60),
    "manager_down": timedelta(seconds=60),
    "fleet_down": timedelta(seconds=60),
    # It always follows the human's own Esc: they know, and may be typing the answer.
    "interrupted": timedelta(minutes=10),
}
"""How long after ``since`` a push may go out, for the kinds that wait at all."""

_LIMITED_PUSH_DELAY = timedelta(seconds=90)
_ASKED_PUSH_DELAY = timedelta(minutes=5)
_MANAGER_FRESH = timedelta(minutes=30)
"""A manager session seen this recently counts as live (the board's own ``_STALE_AFTER``)."""

_TOOL_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,40}")
"""A tool name a reason may carry; anything else is left out of the sentence."""

_SUBAGENT_TOOLS = frozenset({"Task", "Agent"})
"""The tools a sub-agent runs inside: a prompt pending under one is the sub-agent's."""

_DETAIL_INPUT_KEYS = (
    "command",
    "description",
    "file_path",
    "path",
    "url",
    "pattern",
    "query",
    "prompt",
    "old_string",
    "new_string",
    "content",
)
"""The keys of a pending tool's input a permission card shows: what the tool would DO."""

_DETAIL_STRING_MAX = 2_000
_DETAIL_TOOL_MAX = 4_096
_DETAIL_TEXT_MAX = 8_192
_DETAIL_PLAN_MAX = 16_384
_QUESTIONS_MAX = 8
_OPTIONS_MAX = 16
"""AskUserQuestion's own schema allows 4 questions of 4 options; these bound a malformed
one, so every detail fits its cap by cutting strings alone."""

_ESCAPES = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\|$)"  # OSC, to BEL or ST, or to the end
    r"|\x1b[P^_X][^\x1b]*(?:\x1b\\|$)"  # DCS, PM, APC, SOS
    r"|\x1b\[[0-?]*[ -/]*[@-~]"  # CSI, SGR included
    r"|\x1b[@-Z\\-_]?"  # any other escape, or a lone ESC
)

_DISMISSALS_KEEP = 500
_DISMISSALS_AGE = timedelta(days=7)
_dismissals_lock = threading.Lock()


# --- the data model -----------------------------------------------------------------------


@dataclass(frozen=True)
class QuickAnswer:
    """One button a card offers: its label and the tmux key names it sends."""

    label: str
    keys: tuple[str, ...]


@dataclass(frozen=True)
class NeedsItem:
    """One thing that needs the human, with what they must read before answering it."""

    id: str
    kind: str
    project_id: str
    project_name: str
    agent: str | None
    agent_id: str | None
    reason: str
    excerpt: str
    detail: Mapping[str, object]
    answers: tuple[QuickAnswer, ...]
    since: datetime
    actions: tuple[str, ...]
    push_after: datetime | None
    """When a push may go out; ``None`` is feed only. Internal: never serialized."""

    def needs_item_json(self) -> dict[str, object]:
        """The wire shape (SPEC §1.4): the project as ``{id, name}``, ``push_after`` left out."""
        return {
            "id": self.id,
            "kind": self.kind,
            "project": {"id": self.project_id, "name": self.project_name},
            "agent": self.agent,
            "agent_id": self.agent_id,
            "reason": self.reason,
            "excerpt": self.excerpt,
            "detail": dict(self.detail),
            "answers": [{"label": a.label, "keys": list(a.keys)} for a in self.answers],
            "since": self.since.isoformat(timespec="seconds"),
            "actions": list(self.actions),
        }


@dataclass(frozen=True)
class AgentNow:
    """One agent, re-derived now — what an action or a quick answer checks before typing."""

    project: ProjectInfo
    status: FleetAgentStatus | None
    """``None``: the label's newest row has ended and has no window left."""
    tail: TranscriptTail | None
    pane_is_agent: bool
    """The pane's foreground is the agent; ``False`` when it is not live."""
    pane_quiet: bool | None
    """``#{window_activity}`` older than ``fleet.ACTIVITY_WINDOW``; ``None``: tmux did not say."""
    items: tuple[NeedsItem, ...]
    """Every current item of this project whose ``agent`` is this label, project-level kinds
    included, and dismissed ones too: a dismissal hides a card, it does not close a dialog."""


@dataclass(frozen=True)
class NeedsSources:
    """Everything the scan reads, as callables: the live store and tmux, or a test's fakes."""

    list_projects: Callable[[], list[ProjectInfo]]
    list_agents: Callable[[ProjectInfo], list[FleetAgentStatus]]
    """``fleet.list_agents(project, live_only=True)``. It runs first: it records dead panes
    as ended, exit status and all, which ``crashed`` reads."""
    ended_agents: Callable[[str, datetime], list[FleetAgent]]
    """The project's rows that ended at or after the given time."""
    board_events: Callable[[str, int], list[TeamEvent]]
    board_sessions: Callable[[str], list[TeamSession]]
    task_status: Callable[[str], str | None]
    """A task's status; ``None`` when it is gone."""
    transcript_tail: Callable[[str], TranscriptTail | None]
    accounts: Callable[[], AccountsSettings]


# --- names on a lock screen, and text on a card -------------------------------------------


def needs_push_safe(text: str, limit: int = 40) -> str:
    """``text`` fit for a lock screen: printable, one line, at most ``limit`` characters.

    Escape sequences go whole; control and format characters (C0, C1, DEL, the
    bidi overrides, zero-width marks), line and paragraph separators and lone
    surrogates go too, whitespace among them as a space; runs of spaces collapse
    to one; a longer text is cut with ``…``. Every name a reason interpolates
    passes here: a label, an author's role (free text an agent set), a project.
    """
    return _needs_cut(_needs_plain(text), limit)


def _needs_plain(text: str) -> str:
    """``text`` without escapes or invisible characters, its whitespace collapsed."""
    bare = _ESCAPES.sub("", text)
    kept = "".join(ch if ch.isprintable() else " " if ch.isspace() else "" for ch in bare)
    return " ".join(kept.split())


def _needs_cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _needs_excerpt(text: str | None) -> str:
    return _needs_cut(_needs_plain(text or ""), EXCERPT_CHARS)


def _needs_json_size(value: object) -> int:
    """Bytes of ``value`` as the server sends JSON (UTF-8, no ASCII escaping, compact)."""
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return len(encoded.encode("utf-8", "replace"))


def _needs_strings(value: object) -> list[tuple[Any, Any]]:
    """Every string inside ``value``, as ``(container, key)`` to rewrite it through."""
    if isinstance(value, dict):
        pairs: list[tuple[Any, Any]] = list(value.items())
    elif isinstance(value, list):
        pairs = list(enumerate(value))
    else:
        return []
    found: list[tuple[Any, Any]] = []
    for key, inner in pairs:
        if isinstance(inner, str):
            found.append((value, key))
        else:
            found.extend(_needs_strings(inner))
    return found


def _needs_fit(detail: dict[str, Any], limit: int) -> dict[str, Any]:
    """``detail`` with its longest strings cut (``…``) until its JSON is at most ``limit``.

    A detail is built fresh for each item, so it is cut in place. A cut takes
    at least the excess off the longest string, so a pass or two does it; the
    item shapes bound everything that is not a string.
    """
    while (size := _needs_json_size(detail)) > limit:
        strings = _needs_strings(detail)
        if not strings:
            break
        container, key = max(strings, key=lambda found: len(found[0][found[1]]))
        value: str = container[key]
        if len(value) <= 1:
            break
        keep = max(0, len(value) - (size - limit) - 3)  # "…" is 3 bytes of UTF-8
        container[key] = value[:keep] + "…"
    return detail


def looks_like_a_question(text: str) -> bool:
    """Whether an assistant's last words ask the human something.

    Each line is read without its markdown (``*_`>#``) and trailing quotes,
    brackets and spaces. The text asks when a line ending in ``?`` lies in its
    last paragraph (after its last blank line), or among its last 12 non-empty
    lines and within its last 600 characters. So "Which approach? 1. … 2. …"
    asks, and so does a coder's closing "Want me to commit this?" — which the
    push policy, not this test, keeps from crying wolf.
    """
    body = text.strip()
    lines = body.splitlines()
    blank = max((index for index, line in enumerate(lines) if not line.strip()), default=-1)
    if any(_needs_line_asks(line) for line in lines[blank + 1 :]):
        return True
    ends: list[int] = []
    position = 0
    for line in lines:
        position += len(line)
        ends.append(position)
        position += 1
    window = len(body) - 600
    counted = 0
    for line, end in zip(reversed(lines), reversed(ends), strict=True):
        if not line.strip():
            continue
        counted += 1
        if counted > 12 or end < window:
            return False
        if _needs_line_asks(line):
            return True
    return False


_NEEDS_TRAILING = " \t*_`>#\"'\u201d\u2019\u00bb)]}"
"""What a line may end with after its question mark: markdown, closing quotes and brackets."""


def _needs_line_asks(line: str) -> bool:
    return line.rstrip(_NEEDS_TRAILING).endswith("?")


def _needs_asked_tail(text: str) -> str:
    """The question an assistant ended on: its last line ending in ``?``, to the end."""
    lines = text.strip().splitlines()
    for index in range(len(lines) - 1, -1, -1):
        if _needs_line_asks(lines[index]):
            return "\n".join(lines[index:])
    return text


# --- items --------------------------------------------------------------------------------


def needs_item_id(project_id: str, kind: str, subject: str) -> str:
    """``ny_`` and 16 hex digits of what the item is about: the same subject, the same id."""
    digest = hashlib.sha256(f"{project_id}|{kind}|{subject}".encode()).hexdigest()
    return f"ny_{digest[:16]}"


def _needs_now() -> datetime:
    return datetime.now(UTC)


def _needs_item(
    kind: str,
    subject: str,
    *,
    project: ProjectInfo,
    agent: FleetAgent | None,
    reason: str,
    excerpt: str | None,
    detail: dict[str, Any],
    since: datetime,
    push_after: datetime | None,
    answers: tuple[QuickAnswer, ...] = (),
) -> NeedsItem:
    return NeedsItem(
        id=needs_item_id(project.id, kind, subject),
        kind=kind,
        project_id=project.id,
        project_name=project.root.name or project.id,
        agent=None if agent is None else agent.label,
        agent_id=None if agent is None else agent.id,
        reason=reason,
        excerpt=_needs_excerpt(excerpt),
        detail=detail,
        answers=answers,
        since=since,
        actions=_NEEDS_ACTIONS[kind],
        push_after=push_after,
    )


def _needs_is_manager(role: str | None) -> bool:
    """Whether ``role`` is the manager's, a numbered seat of it included."""
    from aisquare.core import harness

    return role is not None and harness.base_role(role) == "manager"


# --- one agent ----------------------------------------------------------------------------


def needs_from_agent(
    status: FleetAgentStatus,
    tail: TranscriptTail | None,
    *,
    project: ProjectInfo,
    events: Sequence[TeamEvent],
    now: datetime,
    manager_live: bool = False,
    accounts: AccountsSettings | None = None,
) -> list[NeedsItem]:
    """What one agent needs from the human: at most one item, by the first rule that holds.

    In order (SPEC §4.2):

    1. derived ``lost``, its pane gone → ``lost``;
    2. derived ``limited`` → ``limited``;
    3. a pending ``AskUserQuestion`` → ``question``;
    4. a pending ``ExitPlanMode`` → ``plan``;
    5. any other pending tool, with attention → ``permission``, about the OLDEST one: each
       prompt has its own tool use, so the 2nd prompt of a turn is a new item;
    6. no pending tool, and the newest record an interruption later than the session's last
       hook → ``interrupted``, whatever the row reads (Esc fires no Stop, so a dismissed
       prompt still reads ``attention`` and an interrupted turn ``working``);
    7. attention, and its notification is the usage-limit dialog → ``limited``;
    8. attention → ``permission``, the dialog form: an MCP elicitation, Claude Code's own;
    9. ``waiting`` on its own words, which end on a question → ``asked``.

    Attention is the derived ``attention``, or a session still marked so after the row
    went stale (past ``_STALE_AFTER`` it derives ``waiting``, the dialog maybe still up).
    Records older than the row are ignored throughout: a resumed session's old pending tool
    or closing question belong to the process before it. Without a readable tail, rules
    3 to 6 and 9 cannot hold. ``exited`` and ``unknown`` agents need nothing here; ``crashed``,
    ``manager_down`` and ``fleet_down`` speak for them. ``manager_live`` and ``accounts``
    decide only when a push may go out.
    """
    if status.state in ("exited", "unknown"):
        return []
    agent, session = status.agent, status.session
    name = needs_push_safe(agent.label)
    own = [e for e in events if session is not None and e.session_id == session.id]
    attention_event = max((e for e in own if e.kind == "attention"), key=_needs_seq, default=None)
    limited_event = max((e for e in own if e.kind == "limited"), key=_needs_seq, default=None)
    if status.state == "lost":
        return [
            _needs_item(
                "lost",
                agent.id,
                project=project,
                agent=agent,
                reason=f"{name}'s pane is gone",
                excerpt=None,
                detail={},
                since=now,
                push_after=now + _NEEDS_PUSH_DELAY["lost"],
            )
        ]
    if status.state == "limited":
        return [
            _needs_limited_item(
                status,
                limited_event or attention_event,
                project=project,
                name=name,
                now=now,
                manager_live=manager_live,
                accounts=accounts,
            )
        ]
    pending = _needs_pending(tail, agent)
    asking = next((tool for tool in pending if tool.name == "AskUserQuestion"), None)
    if asking is not None:
        return [_needs_question_item(asking, project=project, agent=agent, name=name, now=now)]
    planning = next((tool for tool in pending if tool.name == "ExitPlanMode"), None)
    if planning is not None:
        return [_needs_plan_item(planning, project=project, agent=agent, name=name, now=now)]
    attention = _needs_attention(status)
    if pending:
        if not attention:
            return []  # a tool running, or the 6 s before Claude Code's notification
        return [
            _needs_permission_item(pending[0], project=project, agent=agent, name=name, now=now)
        ]
    if tail is not None and _needs_marker_later(status, tail):
        return [_needs_interrupted_item(tail, project=project, agent=agent, name=name, now=now)]
    if attention:
        if attention_event is not None and LIMIT_DIALOG.search(attention_event.text):
            since = attention_event.created_at
            return [
                _needs_item(
                    "limited",
                    f"attention:{attention_event.seq}",
                    project=project,
                    agent=agent,
                    reason=f"{name} hit its usage limit (Claude Code is asking what to do)",
                    excerpt=attention_event.text,
                    detail=_needs_fit({"text": attention_event.text}, _DETAIL_TEXT_MAX),
                    since=since,
                    push_after=_needs_limited_push(
                        since, None, now=now, manager_live=manager_live, accounts=accounts
                    ),
                )
            ]
        seen = session.last_seen_at if session is not None else now
        seq = "-" if attention_event is None else str(attention_event.seq)
        text = "" if attention_event is None else attention_event.text
        return [
            _needs_item(
                "permission",
                f"attention:{seq}:{seen.isoformat()}",
                project=project,
                agent=agent,
                reason=f"{name} shows a dialog that needs you",
                excerpt=text,
                detail=_needs_fit({"text": text}, _DETAIL_TEXT_MAX),
                since=seen,
                push_after=seen,
            )
        ]
    if (
        status.state == "waiting"
        and tail is not None
        and tail.newest == "assistant_text"
        and tail.last_text
        and (tail.newest_at is None or tail.newest_at >= agent.created_at)
        and looks_like_a_question(tail.last_text)
    ):
        since = tail.newest_at or now
        prompt_now = _needs_is_manager(agent.role) or agent.spawned_by == "user" or not manager_live
        return [
            _needs_item(
                "asked",
                tail.marker_key or since.isoformat(),
                project=project,
                agent=agent,
                reason=f"{name} ended its turn with a question",
                excerpt=_needs_asked_tail(tail.last_text),
                detail=_needs_fit({"text": tail.last_text}, _DETAIL_TEXT_MAX),
                since=since,
                # A crew agent's closing question is its manager's to answer first.
                push_after=since if prompt_now else since + _ASKED_PUSH_DELAY,
            )
        ]
    return []


def _needs_seq(event: TeamEvent) -> int:
    return event.seq


def _needs_attention(status: FleetAgentStatus) -> bool:
    """Derived ``attention``, or a session still marked so after its row went stale."""
    session = status.session
    return status.state == "attention" or (
        status.state == "waiting" and session is not None and session.state == "attention"
    )


def _needs_marker_later(status: FleetAgentStatus, tail: TranscriptTail) -> bool:
    """The newest record is an interruption this process made after its session's last hook."""
    if tail.newest != "interrupted" or tail.newest_at is None:
        return False
    if tail.newest_at < status.agent.created_at:
        return False
    return status.session is None or tail.newest_at > status.session.last_seen_at


def _needs_pending(tail: TranscriptTail | None, agent: FleetAgent) -> tuple[PendingTool, ...]:
    """The tail's pending tools, less any older than the row (a resumed session's leftovers)."""
    if tail is None:
        return ()
    return tuple(tool for tool in tail.pending if tool.at is None or tool.at >= agent.created_at)


def _needs_limited_item(
    status: FleetAgentStatus,
    event: TeamEvent | None,
    *,
    project: ProjectInfo,
    name: str,
    now: datetime,
    manager_live: bool,
    accounts: AccountsSettings | None,
) -> NeedsItem:
    """A row parked on a usage limit: the subject is its newest ``limited`` event."""
    agent, session = status.agent, status.session
    resets = session.limit_resets_at if session is not None else None
    if event is not None and event.kind == "limited":
        subject, since = str(event.seq), event.created_at
    else:
        subject = f"{agent.id}:{resets.isoformat() if resets is not None else '-'}"
        since = session.last_seen_at if session is not None else now
    reason = f"{name} hit its usage limit"
    if status.detail and status.detail != "usage limit":
        reason += f" · {status.detail}"
    text = "" if event is None else event.text
    return _needs_item(
        "limited",
        subject,
        project=project,
        agent=agent,
        reason=reason,
        excerpt=text or status.detail,
        detail=_needs_fit({"text": text}, _DETAIL_TEXT_MAX),
        since=since,
        push_after=_needs_limited_push(
            since, resets, now=now, manager_live=manager_live, accounts=accounts
        ),
    )


def _needs_limited_push(
    since: datetime,
    resets: datetime | None,
    *,
    now: datetime,
    manager_live: bool,
    accounts: AccountsSettings | None,
) -> datetime | None:
    """When a limit may push: never when it lifts soon; later when someone else is on it.

    A reset within ``[accounts] wait_if_reset_within_minutes`` is cheaper to wait
    out than to act on, so it stays in the feed. With ``on_limit = "switch"`` or a
    live manager, the hand-over or the manager is already moving it, and its
    ``switched`` event usually ends the item first.
    """
    if accounts is None:
        from aisquare.core.config import AccountsSettings

        accounts = AccountsSettings()
    if resets is not None and resets - now <= timedelta(
        minutes=accounts.wait_if_reset_within_minutes
    ):
        return None
    if accounts.on_limit == "switch" or manager_live:
        return since + _LIMITED_PUSH_DELAY
    return since


def _needs_questions(raw: object) -> list[dict[str, Any]]:
    """An ``AskUserQuestion``'s questions, in the four keys a card shows, bounded."""
    questions: list[dict[str, Any]] = []
    for question in raw[:_QUESTIONS_MAX] if isinstance(raw, list) else []:
        if not isinstance(question, dict):
            continue
        options = question.get("options")
        questions.append(
            {
                "header": _needs_str(question.get("header")),
                "question": _needs_str(question.get("question")),
                "multiSelect": question.get("multiSelect") is True,
                "options": [
                    {
                        "label": _needs_str(option.get("label")),
                        "description": _needs_str(option.get("description")),
                    }
                    for option in (options[:_OPTIONS_MAX] if isinstance(options, list) else [])
                    if isinstance(option, dict)
                ],
            }
        )
    return questions


def _needs_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _needs_question_item(
    tool: PendingTool, *, project: ProjectInfo, agent: FleetAgent, name: str, now: datetime
) -> NeedsItem:
    """A pending ``AskUserQuestion``: every question in ``detail``, digits for a simple one.

    Quick answers only for one single-select question of at most nine options,
    which its digits pick; anything else is answered from the key pad, with the
    live pane beside it.
    """
    questions = _needs_questions(tool.input.get("questions"))
    excerpt = ""
    answers: tuple[QuickAnswer, ...] = ()
    if questions:
        first = questions[0]
        labels = " · ".join(option["label"] for option in first["options"])
        excerpt = first["question"] + (f" — {labels}" if labels else "")
        if len(questions) > 1:
            excerpt += f" (+{len(questions) - 1} more)"
        if len(questions) == 1 and not first["multiSelect"] and 1 <= len(first["options"]) <= 9:
            answers = (
                *(
                    QuickAnswer(needs_push_safe(option["label"], 80) or str(n), (str(n),))
                    for n, option in enumerate(first["options"], start=1)
                ),
                QuickAnswer("Cancel", ("Escape",)),
            )
    since = tool.at or now
    return _needs_item(
        "question",
        tool.tool_use_id,
        project=project,
        agent=agent,
        reason=f"{name} asks you a question",
        excerpt=excerpt,
        detail=_needs_fit({"questions": questions}, _DETAIL_TEXT_MAX),
        since=since,
        push_after=since,
        answers=answers,
    )


def _needs_plan_item(
    tool: PendingTool, *, project: ProjectInfo, agent: FleetAgent, name: str, now: datetime
) -> NeedsItem:
    """A plan waiting for approval: the plan itself in ``detail``, its first line as excerpt."""
    raw = tool.input.get("plan")
    plan = raw if isinstance(raw, str) else ""
    first = next(
        (line.strip().lstrip("#").strip() for line in plan.splitlines() if line.strip()), ""
    )
    since = tool.at or now
    return _needs_item(
        "plan",
        tool.tool_use_id,
        project=project,
        agent=agent,
        reason=f"{name} asks you to approve a plan",
        excerpt=first,
        detail=_needs_fit({"plan": plan}, _DETAIL_PLAN_MAX),
        since=since,
        push_after=since,
        answers=(
            QuickAnswer("1", ("1",)),
            QuickAnswer("2", ("2",)),
            QuickAnswer("3", ("3",)),
            QuickAnswer("Keep planning", ("Escape",)),
        ),
    )


def _needs_permission_item(
    tool: PendingTool, *, project: ProjectInfo, agent: FleetAgent, name: str, now: datetime
) -> NeedsItem:
    """A permission prompt: the full command or path in ``detail``, so nobody approves blind.

    The buttons are the dialog's own digits and Esc; the card shows the live
    pane beside them, so the options' real text is on screen. A prompt under a
    ``Task``/``Agent`` tool is a sub-agent's, whose own tool use is in its own
    records, not this transcript.
    """
    if tool.name in _SUBAGENT_TOOLS:
        reason = f"{name} waits for a permission answer (in a sub-agent)"
    elif _TOOL_NAME.fullmatch(tool.name):
        reason = f"{name} waits for a permission answer to use {tool.name}"
    else:
        reason = f"{name} waits for a permission answer"
    shown: dict[str, Any] = {}
    for key in _DETAIL_INPUT_KEYS:
        value = tool.input.get(key)
        if isinstance(value, str):
            shown[key] = _needs_cut(value, _DETAIL_STRING_MAX)
        elif isinstance(value, bool | int) or (isinstance(value, float) and math.isfinite(value)):
            shown[key] = value
    since = tool.at or now
    return _needs_item(
        "permission",
        tool.tool_use_id,
        project=project,
        agent=agent,
        reason=reason,
        excerpt=tool.summary,
        detail=_needs_fit({"tool": _needs_cut(tool.name, 200), "input": shown}, _DETAIL_TOOL_MAX),
        since=since,
        push_after=since,
        answers=(
            QuickAnswer("1", ("1",)),
            QuickAnswer("2", ("2",)),
            QuickAnswer("No", ("Escape",)),
        ),
    )


def _needs_interrupted_item(
    tail: TranscriptTail, *, project: ProjectInfo, agent: FleetAgent, name: str, now: datetime
) -> NeedsItem:
    """An agent stopped by an Esc (or a rejected prompt), back at its prompt waiting."""
    text = tail.last_text or ""
    paragraphs = [part for part in re.split(r"\n\s*\n", text.strip()) if part.strip()]
    since = tail.newest_at or now
    return _needs_item(
        "interrupted",
        tail.marker_key or since.isoformat(),
        project=project,
        agent=agent,
        reason=f"{name} was interrupted and waits for you",
        excerpt=paragraphs[-1] if paragraphs else "",
        detail=_needs_fit({"text": text}, _DETAIL_TEXT_MAX),
        since=since,
        push_after=since + _NEEDS_PUSH_DELAY["interrupted"],
    )


# --- the board ----------------------------------------------------------------------------


def is_needs_board_event(event: TeamEvent, *, author_role: str | None, manager_live: bool) -> bool:
    """Whether a board event asks the HUMAN something, or reports to them (SPEC §4.4).

    On main the manager asks the human with ``note --kind question`` and no
    ``--to``, coders ask ``--to manager``, and the manager's final report is a
    ``result``. So: anything a manager asks or reports; a question addressed to
    the human, or to nobody; and a coder's question or result to a manager that
    is not there to read it. The human's own writes are never items.
    """
    if event.session_id is None:
        return False
    to = (event.to_role or "").strip().lower()
    manager_author = _needs_is_manager(author_role)
    if event.kind == "question":
        return manager_author or to in OWNER_ROLES or (to == "manager" and not manager_live)
    if event.kind == "result":
        return manager_author or (to == "manager" and not manager_live)
    return False


def needs_from_board(
    events: Sequence[TeamEvent],
    sessions: Sequence[TeamSession],
    agents: Sequence[FleetAgent],
    *,
    project: ProjectInfo,
    now: datetime,
    manager_live: bool | None = None,
) -> list[NeedsItem]:
    """The board's open questions and results for the human (:func:`is_needs_board_event`).

    One is cleared, besides a dismissal, when it is older than
    :data:`QUESTION_HORIZON`, when a LATER note, decision or result of the human's
    is addressed to its author (by label or role), or when its author posts a
    later question, result or decision: they have moved on. An unaddressed
    human note clears nothing; it is news, not an answer. ``agents`` names each
    author by its fleet label; ``manager_live`` defaults to what the sessions and
    rows say.
    """
    by_session = {session.id: session for session in sessions}
    rows: dict[str, FleetAgent] = {}
    for agent in sorted(agents, key=lambda agent: agent.created_at):
        if agent.session_id:
            rows[agent.session_id] = agent  # the newest row of a session names it
    if manager_live is None:
        gone = {row.session_id for row in agents if row.ended_at is not None and row.session_id}
        live_rows = [row for row in agents if row.ended_at is None]
        manager_live = _needs_manager_live(live_rows, gone, sessions, now)
    addressed: set[str] = set()  # whom the human's later writes were addressed to
    moved_on: set[str] = set()  # sessions that asked, reported or decided again later
    items: list[NeedsItem] = []
    for event in sorted(events, key=_needs_seq, reverse=True):
        if event.session_id is None:
            if event.kind in ("note", "decision", "result"):
                addressed.add((event.to_role or "").strip().lower())
            continue
        if event.kind in ("question", "result") and now - event.created_at <= QUESTION_HORIZON:
            session = by_session.get(event.session_id)
            role = None if session is None else session.role
            row = rows.get(event.session_id)
            own = None if session is None else session.label
            label = row.label if row is not None else own
            names = {n.strip().lower() for n in (label, own, role) if n and n.strip()}
            open_still = event.session_id not in moved_on and not names & addressed
            if open_still and is_needs_board_event(
                event, author_role=role, manager_live=manager_live
            ):
                items.append(
                    _needs_board_item(event, project=project, row=row, author=label or role)
                )
        if event.kind in ("question", "result", "decision"):
            moved_on.add(event.session_id)
    return items


def _needs_board_item(
    event: TeamEvent, *, project: ProjectInfo, row: FleetAgent | None, author: str | None
) -> NeedsItem:
    """One board question or result. ``agent`` is the author's fleet row, when it has one."""
    who = needs_push_safe(author or "") or "an agent"
    asks = event.kind == "question"
    return _needs_item(
        "board_question" if asks else "board_result",
        str(event.seq),
        project=project,
        agent=row,
        reason=f"{who} asks on the board" if asks else f"{who} reports a result",
        excerpt=event.text,
        detail=_needs_fit(
            {"text": event.text, "author": author or "", "seq": event.seq}, _DETAIL_TEXT_MAX
        ),
        since=event.created_at,
        push_after=event.created_at,
    )


def _needs_manager_live(
    live_rows: Sequence[FleetAgent],
    gone: Collection[str],
    sessions: Sequence[TeamSession],
    now: datetime,
) -> bool:
    """Whether the project has a manager to act on what needs doing.

    A live manager row says so outright. Otherwise a manager session counts
    (one started outside the fleet, say) while it has not ended and was seen
    within the board's stale window — unless its fleet row is ``gone``: a crash
    fires no ``SessionEnd``, and its session would otherwise read live for half
    an hour after the manager died.
    """
    if any(_needs_is_manager(row.role) for row in live_rows):
        return True
    return any(
        _needs_is_manager(session.role)
        and session.ended_at is None
        and now - session.last_seen_at <= _MANAGER_FRESH
        and session.id not in gone
        for session in sessions
    )


# --- one project --------------------------------------------------------------------------


@dataclass(frozen=True)
class _NeedsProject:
    """One project's scan: its items, and the facts they came from."""

    items: list[NeedsItem]
    statuses: list[FleetAgentStatus]
    ended: list[FleetAgent]
    tails: dict[str, TranscriptTail | None]
    """Agent id → the tail its items were read from."""


_T = TypeVar("_T")


def _needs_read(read: Callable[[], list[_T]], what: str, project: ProjectInfo) -> list[_T]:
    """One source read for one project; a failure costs what it would have shown."""
    try:
        return read()
    except Exception:
        log.debug("remote: needs could not read %s of %s", what, project.id, exc_info=True)
        return []


def _needs_scan_project(
    sources: NeedsSources,
    project: ProjectInfo,
    statuses: list[FleetAgentStatus] | None,
    *,
    now: datetime,
    first_seen: MutableMapping[str, datetime],
    seen: set[str],
    accounts: AccountsSettings | None,
) -> _NeedsProject:
    """Every item of one project. ``statuses`` is its ``list_agents``; ``None``, it failed.

    Without a listing only the board speaks: an agent's state, a crash, a
    manager gone and tmux down all depend on rows the listing could not read.
    ``first_seen`` dates the items whose facts carry no date (a pane gone, tmux
    not answering) from the first scan that saw them; ``seen`` collects what
    this scan saw, so the caller can forget the rest.
    """
    from aisquare.services.fleet import RECENTLY_ENDED

    ended = _needs_read(
        lambda: sources.ended_agents(project.id, now - RECENTLY_ENDED), "rows", project
    )
    events = _needs_read(
        lambda: sources.board_events(project.id, NEEDS_BOARD_EVENTS), "board", project
    )
    sessions = _needs_read(lambda: sources.board_sessions(project.id), "sessions", project)
    listed = statuses or []
    rows = list({row.id: row for row in [*ended, *(status.agent for status in listed)]}.values())
    gone = {row.session_id for row in ended if row.session_id}
    gone |= {
        status.agent.session_id
        for status in listed
        if status.agent.session_id
        and (status.agent.ended_at is not None or status.state in ("exited", "lost"))
    }
    live_rows = [
        status.agent
        for status in listed
        if status.agent.ended_at is None and status.state not in ("exited", "lost")
    ]
    manager_live = _needs_manager_live(live_rows, gone, sessions, now)
    items: list[NeedsItem] = []
    tails: dict[str, TranscriptTail | None] = {}
    if statuses is not None:
        for status in statuses:
            tail = tails[status.agent.id] = _needs_tail_of(sources, status)
            for item in needs_from_agent(
                status,
                tail,
                project=project,
                events=events,
                now=now,
                manager_live=manager_live,
                accounts=accounts,
            ):
                items.append(_needs_dated(item, first_seen, seen) if item.kind == "lost" else item)
        items.extend(
            _needs_crashed(
                ended, rows, project=project, now=now, manager_live=manager_live, sources=sources
            )
        )
        items.extend(_needs_manager_down(statuses, rows, events, project=project, now=now))
        items.extend(
            _needs_fleet_down(statuses, project=project, now=now, first_seen=first_seen, seen=seen)
        )
    items.extend(
        needs_from_board(
            events, sessions, rows, project=project, now=now, manager_live=manager_live
        )
    )
    return _NeedsProject(items=items, statuses=listed, ended=ended, tails=tails)


def _needs_tail_of(sources: NeedsSources, status: FleetAgentStatus) -> TranscriptTail | None:
    """The agent's transcript tail, read only where a rule can use it."""
    session = status.session
    path = None if session is None else session.transcript_path
    if not path or status.state in ("exited", "unknown", "lost"):
        return None
    try:
        return sources.transcript_tail(path)
    except Exception:
        log.debug("remote: needs could not read the tail of %s", path, exc_info=True)
        return None


def _needs_dated(
    item: NeedsItem, first_seen: MutableMapping[str, datetime], seen: set[str]
) -> NeedsItem:
    """``item`` dated from the first scan that saw it, its push delay moved with it."""
    seen.add(item.id)
    since = first_seen.setdefault(item.id, item.since)
    if since == item.since:
        return item
    shift = item.since - since
    pushed = None if item.push_after is None else item.push_after - shift
    return replace(item, since=since, push_after=pushed)


def _needs_crashed(
    ended: Sequence[FleetAgent],
    rows: Sequence[FleetAgent],
    *,
    project: ProjectInfo,
    now: datetime,
    manager_live: bool,
    sources: NeedsSources,
) -> list[NeedsItem]:
    """Agents that died with a failing exit status in the last hour, with nobody on it.

    A clean ``/exit`` is 0 and a forced stop has no status: neither is a crash.
    An agent whose task is closed did its work; one a live manager has (it was
    nudged on the exit) is the manager's; one that was restarted since — a newer
    row holds its label — was handled. The manager's own crash is
    ``manager_down``.
    """
    if manager_live:
        return []
    newest: dict[str, datetime] = {}
    for row in rows:
        newest[row.label] = max(newest.get(row.label, row.created_at), row.created_at)
    items: list[NeedsItem] = []
    for row in ended:
        if row.ended_at is None or now - row.ended_at > CRASH_WINDOW:
            continue
        if row.exit_status in (0, None) or _needs_is_manager(row.role):
            continue
        if newest.get(row.label, row.created_at) > row.created_at:
            continue
        if row.task_id is not None and _needs_task_closed(sources, row.task_id):
            continue
        items.append(
            _needs_item(
                "crashed",
                row.id,
                project=project,
                agent=row,
                reason=f"{needs_push_safe(row.label)} exited unexpectedly (exit {row.exit_status})",
                excerpt=None,
                detail={"exit_status": row.exit_status, "task_id": row.task_id},
                since=row.ended_at,
                push_after=row.ended_at + _NEEDS_PUSH_DELAY["crashed"],
            )
        )
    return items


def _needs_task_closed(sources: NeedsSources, task_id: str) -> bool:
    try:
        status = sources.task_status(task_id)
    except Exception:
        return False  # an unreadable task is not proof the work is done
    return status in CLOSED_STATUSES


def _needs_manager_down(
    statuses: Sequence[FleetAgentStatus],
    rows: Sequence[FleetAgent],
    events: Sequence[TeamEvent],
    *,
    project: ProjectInfo,
    now: datetime,
) -> list[NeedsItem]:
    """The manager has ended and none replaced it, while its crew still needs one.

    A failing exit status is always reported. A forced stop (no status) is
    reported only while another agent still works, waits on a prompt or is
    limited, and the manager's last word on the board was not its ``result``:
    a manager stopped after reporting, or exiting cleanly, finished its job.
    """
    managers = [row for row in rows if _needs_is_manager(row.role)]
    if not managers or any(row.ended_at is None for row in managers):
        return []
    manager = max(managers, key=lambda row: row.created_at)
    if manager.ended_at is None or manager.exit_status == 0:
        return []
    if manager.exit_status is not None:
        reason = f"the manager exited unexpectedly (exit {manager.exit_status})"
    else:
        busy = [
            status
            for status in statuses
            if status.agent.id != manager.id
            and status.agent.ended_at is None
            and status.state in ("working", "attention", "limited")
        ]
        own = [e for e in events if manager.session_id and e.session_id == manager.session_id]
        last = max(own, key=_needs_seq, default=None)
        if not busy or (last is not None and last.kind == "result"):
            return []
        count = len(busy)
        reason = (
            f"the manager stopped while {count} agent{'s' if count != 1 else ''} "
            f"still work{'' if count != 1 else 's'}"
        )
    return [
        _needs_item(
            "manager_down",
            manager.id,
            project=project,
            agent=manager,
            reason=reason,
            excerpt=None,
            detail={"exit_status": manager.exit_status, "task_id": manager.task_id},
            since=manager.ended_at,
            push_after=manager.ended_at + _NEEDS_PUSH_DELAY["manager_down"],
        )
    ]


def _needs_fleet_down(
    statuses: Sequence[FleetAgentStatus],
    *,
    project: ProjectInfo,
    now: datetime,
    first_seen: MutableMapping[str, datetime],
    seen: set[str],
) -> list[NeedsItem]:
    """Every live row of the project derives ``unknown``: its tmux is not answering.

    The subject is the first scan that saw it, so it is one item until the
    condition clears and a new one if it comes back.
    """
    live = [status for status in statuses if status.agent.ended_at is None]
    if not live or any(status.state != "unknown" for status in live):
        return []
    key = f"fleet_down:{project.id}"
    seen.add(key)
    first = first_seen.setdefault(key, now)
    return [
        _needs_item(
            "fleet_down",
            f"{project.id}:{first.isoformat(timespec='seconds')}",
            project=project,
            agent=None,
            reason=f"tmux is not answering for {needs_push_safe(project.root.name or project.id)}",
            excerpt=None,
            detail={},
            since=first,
            push_after=first + _NEEDS_PUSH_DELAY["fleet_down"],
        )
    ]


# --- the scan -----------------------------------------------------------------------------


def scan_needs_you(
    sources: NeedsSources,
    *,
    now: datetime,
    dismissed: Collection[str],
    first_seen: MutableMapping[str, datetime] | None = None,
) -> list[NeedsItem]:
    """Every item across every project, dismissals dropped, ranked by kind then ``since``.

    A project whose listing fails still yields its board items, and one that
    fails outright costs only its own items. ``first_seen`` is the watcher's
    memory of when it first saw the items whose facts carry no date; without
    it, each scan is the first.
    """
    memory: MutableMapping[str, datetime] = {} if first_seen is None else first_seen
    seen: set[str] = set()
    accounts = _needs_accounts(sources)
    items: list[NeedsItem] = []
    for project in sources.list_projects():
        statuses: list[FleetAgentStatus] | None
        try:
            statuses = sources.list_agents(project)
        except Exception:
            log.debug("remote: needs could not list the agents of %s", project.id, exc_info=True)
            statuses = None
        try:
            scanned = _needs_scan_project(
                sources, project, statuses, now=now, first_seen=memory, seen=seen, accounts=accounts
            )
        except Exception:
            log.warning("remote: the needs scan of %s failed", project.id, exc_info=True)
            continue
        items.extend(scanned.items)
    for key in [key for key in memory if key not in seen]:
        del memory[key]
    return _needs_ranked([item for item in items if item.id not in dismissed])


def _needs_accounts(sources: NeedsSources) -> AccountsSettings | None:
    try:
        return sources.accounts()
    except Exception:
        return None


def _needs_ranked(items: Sequence[NeedsItem]) -> list[NeedsItem]:
    """One item per id, in rank order (:data:`NEEDS_KINDS`), then oldest first."""
    unique = list({item.id: item for item in reversed(items)}.values())
    return sorted(unique, key=lambda item: (NEEDS_KINDS.index(item.kind), item.since))


# --- one agent, now: what an action checks before it types -------------------------------


def needs_agent_now(project: ProjectInfo, label: str, *, now: datetime | None = None) -> AgentNow:
    """One agent re-derived now, synchronously, never on the event loop.

    Not built yet. It raises rather than answering empty: an ``AgentNow`` with
    no items and no dialog is a claim about a live pane, and a caller trusting
    it would type into whatever that pane is showing.
    """
    raise NotImplementedError("needs-you is not built yet")


def needs_dialog_open(snap: AgentNow) -> bool:
    """Whether the agent shows a dialog an Enter would answer. Not built yet: ``False``."""
    return False


def needs_at_input_prompt(snap: AgentNow) -> bool:
    """Whether the agent waits at its input prompt. Not built yet: ``False``."""
    return False


def needs_item_current(snap: AgentNow, item_id: str) -> bool:
    """Whether ``item_id`` is still one of the agent's items. Not built yet: ``False``."""
    return False


# --- the live sources ---------------------------------------------------------------------


def live_needs_sources() -> NeedsSources:
    """The scan over the live store and tmux: the same calls ``fleet ls`` and the board make."""
    from aisquare.core.store import AmbiguousIdError, store_session
    from aisquare.services import claude_accounts as claude_accounts_service
    from aisquare.services import fleet as fleet_service
    from aisquare.services import project as project_service

    def needs_rows_ended(project_id: str, since: datetime) -> list[FleetAgent]:
        with store_session() as store:
            rows = store.fleet_agents(project_id, live_only=False)
        return [row for row in rows if row.ended_at is not None and row.ended_at >= since]

    def needs_board_events(project_id: str, limit: int) -> list[TeamEvent]:
        with store_session() as store:
            return store.recent_events(project_id, limit=limit)

    def needs_board_sessions(project_id: str) -> list[TeamSession]:
        with store_session() as store:
            return store.team_sessions(project_id)

    def needs_task_status(ref: str) -> str | None:
        with store_session() as store:
            try:
                task = store.get_task(ref)
            except AmbiguousIdError:
                return None
        return None if task is None else task.status

    def needs_live_agents(project: ProjectInfo) -> list[FleetAgentStatus]:
        return fleet_service.list_agents(project, live_only=True)

    return NeedsSources(
        list_projects=project_service.list_projects,
        list_agents=needs_live_agents,
        ended_agents=needs_rows_ended,
        board_events=needs_board_events,
        board_sessions=needs_board_sessions,
        task_status=needs_task_status,
        transcript_tail=_needs_cached_tail,
        accounts=claude_accounts_service.accounts_settings,
    )


_tails: dict[str, tuple[tuple[int, int], TranscriptTail | None]] = {}
_tails_lock = threading.Lock()
_TAILS_KEPT = 512


def _needs_cached_tail(path: str) -> TranscriptTail | None:
    """:func:`read_transcript_tail`, read again only when the file's size or mtime moved.

    An unchanged transcript costs one ``stat()``. Shared by the watcher and
    :func:`needs_agent_now`, and bounded: the oldest entries go first.
    """
    try:
        stat = os.stat(path)
    except OSError:
        return None
    key = (stat.st_size, stat.st_mtime_ns)
    with _tails_lock:
        cached = _tails.get(path)
    if cached is not None and cached[0] == key:
        return cached[1]
    tail = read_transcript_tail(path)
    with _tails_lock:
        _tails.pop(path, None)
        _tails[path] = (key, tail)
        while len(_tails) > _TAILS_KEPT:
            del _tails[next(iter(_tails))]
    return tail


# --- dismissals ---------------------------------------------------------------------------


def load_needs_dismissals() -> dict[str, str]:
    """Item id → when a phone dismissed it, from ``remote-needs.json``; ``{}`` when unreadable."""
    from aisquare.core.paths import remote_needs_path

    try:
        raw = json.loads(remote_needs_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    dismissed = raw.get("dismissed") if isinstance(raw, dict) else None
    if not isinstance(dismissed, dict):
        return {}
    return {k: v for k, v in dismissed.items() if isinstance(k, str) and isinstance(v, str)}


def record_needs_dismissal(item_id: str) -> None:
    """Remember that ``item_id`` was dismissed, so no later scan shows it again.

    Owner-only, like every Remote file. Dismissals older than 7 days are
    dropped (an id that old will not come back), and at most the newest 500 are
    kept, so the file stays small whatever a phone does.
    """
    from aisquare.core.atomic import write_replacing
    from aisquare.core.paths import remote_needs_path

    now = _needs_now()
    with _dismissals_lock:
        kept: dict[str, datetime] = {}
        for key, stamp in load_needs_dismissals().items():
            when = _needs_stamp(stamp)
            if when is not None and now - when <= _DISMISSALS_AGE:
                kept[key] = when
        kept[item_id] = now
        newest = sorted(kept.items(), key=lambda pair: pair[1])[-_DISMISSALS_KEEP:]
        body = {"version": 1, "dismissed": {k: v.isoformat(timespec="seconds") for k, v in newest}}
        path = remote_needs_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        write_replacing(path, json.dumps(body, indent=2), owner_only=True)


def _needs_stamp(raw: str) -> datetime | None:
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


# --- the seams the server calls -----------------------------------------------------------


def needs_routes(kit: RemoteKit) -> list[BaseRoute]:
    """``GET api/needs``, ``POST api/needs/dismiss``, ``POST api/needs/answer``. None yet."""
    return []


def start_needs_watch(kit: RemoteKit) -> Callable[[], None] | None:
    """Start the scanner at ``kit.lane_state["needs"]``; its stopper. Nothing to start yet."""
    return None


def needs_ws_frames(kit: RemoteKit) -> list[tuple[str, object]]:
    """``[("needs_you", {"items": [...]})]`` once the watcher has scanned; ``[]`` until then."""
    return []


def needs_scanned_iso(kit: RemoteKit) -> str | None:
    """When the watcher last scanned, for the heartbeat frame; ``None``: it never has."""
    return None


def needs_cli_payload() -> dict[str, object]:
    """``asq remote needs``: one scan, here and now, in the shape of ``GET api/needs``."""
    now = _needs_now()
    items = scan_needs_you(live_needs_sources(), now=now, dismissed=load_needs_dismissals())
    return {
        "items": [item.needs_item_json() for item in items],
        "scanned_at": now.isoformat(timespec="seconds"),
    }


__all__ = [
    "DIALOG_SETTLE_SECONDS",
    "EXCERPT_CHARS",
    "LIMIT_DIALOG",
    "NEEDS_ANSWER_KEYS",
    "NEEDS_KINDS",
    "NEEDS_SCAN_SECONDS",
    "OWNER_ROLES",
    "QUESTION_HORIZON",
    "AgentNow",
    "NeedsItem",
    "NeedsSources",
    "QuickAnswer",
    "is_needs_board_event",
    "live_needs_sources",
    "load_needs_dismissals",
    "looks_like_a_question",
    "needs_agent_now",
    "needs_at_input_prompt",
    "needs_cli_payload",
    "needs_dialog_open",
    "needs_from_agent",
    "needs_from_board",
    "needs_item_current",
    "needs_item_id",
    "needs_push_safe",
    "needs_routes",
    "needs_scanned_iso",
    "needs_ws_frames",
    "record_needs_dismissal",
    "scan_needs_you",
    "start_needs_watch",
]
