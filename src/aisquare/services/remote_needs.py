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
``tool_use`` of a permission prompt, with its notification when a sub-agent
asks, the marker record of an interruption, the seq of a board event), so it
keeps its id from scan to scan and a new prompt is a new id — and a new push.
A ``reason`` is a fixed template with every interpolated name passed through
:func:`needs_push_safe`, because it is what a lock screen shows; the content a
human must read before answering (the full command, every question and option,
the plan) lives in ``excerpt`` and ``detail``, which are served to an unlocked
page and never pushed.

A phone answers a card through ``POST api/needs/answer``, which re-derives the
agent right then (:func:`needs_agent_now`) and types only while the card is
still true: a stale card's ``1`` must never approve the prompt that replaced it.
:func:`needs_dialog_open` and :func:`needs_at_input_prompt` are the same
re-derivation as predicates, for the agent actions (``remote_actions``) that
must not press Enter into an open dialog. An action that waits for its Escape
to land asks them of :func:`needs_single_agent_now`, the one agent's own facts.

The watcher (:class:`RemoteNeedsWatcher`) runs in its own daemon thread, scans
every :data:`NEEDS_SCAN_SECONDS` while any device exists, and lives at
``kit.lane_state["needs"]``; the stream, the heartbeat and the push sender read
its latest snapshot under a lock and do no I/O of their own. The server imports
this module inside functions only, so it is not on the hook path (SPEC §0.2).
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
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.routing import BaseRoute

    from aisquare.core.config import AccountsSettings
    from aisquare.core.tmux import TmuxServer
    from aisquare.services.remote_server import Device, RemoteKit

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
"""How many of a project's newest board events one scan reads for its agents' own events
(:func:`_needs_own_events`). Its board items are read by time instead (``board_since``)."""

CRASH_WINDOW = timedelta(hours=1)
"""How long after its end a crashed agent stays an item."""

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

_NEEDS_ASKED = ("question", "result", "decision")
"""An agent's board events that open a board item (a question, a result) or move its author
on from one (any of the three, later), in :func:`needs_from_board`."""

_NEEDS_REPLIED = ("note", "decision", "result")
"""The human's board events (no session) that answer an item addressed to its author."""

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

_NOTICE_WAIT = timedelta(seconds=20)
"""How long after its pane printed a sub-agent's prompt may still be waiting for the
notification that names it (:func:`_needs_subagent_prompt`). Claude Code sends it 6 s after
the prompt is drawn; the hook's process starts in a second or so, and may wait out the
store's busy timeout (5 s) for its write; tmux tells the time of output to the second,
rounded down. That is about 13 s at worst. A notification later than this leaves the
prompt named after the one before until it lands, and a redraw (an attach, a resize, a
key that moves the highlight) hides the card this long."""

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
        """The wire shape (SPEC §1.4): the project as ``{id, name}``, ``push_after`` left out,
        ``since`` the API's ISO stamp (``remote_server._iso_seconds``)."""
        from aisquare.services.remote_server import _iso_seconds

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
            "since": _iso_seconds(self.since),
            "actions": list(self.actions),
        }


@dataclass(frozen=True)
class AgentNow:
    """One agent, re-derived now — what an action or a quick answer checks before typing.

    :func:`needs_agent_now` derives it from its project's scan. While an action's
    Escape lands, :func:`needs_single_agent_now` derives it from the agent's own
    facts instead, with two differences: its ``status`` is never ``None``, and its
    ``items`` are the agent's own without the board, so they hold no project-level
    kind, and the usage-limit dialog is a plain ``permission`` item there, under
    another id. No card id is matched against them.
    """

    project: ProjectInfo
    status: FleetAgentStatus | None
    """``None``: the label's newest row has ended and has no window left. Never so in
    :func:`needs_single_agent_now`'s, where that row reads ``exited``."""
    tail: TranscriptTail | None
    pane_is_agent: bool
    """The row's own pane runs the agent; ``False`` when it is not live, or when the listing
    did not vouch for the pane under its id (``lost``, ``exited``, ``unknown``)."""
    pane_quiet: bool | None
    """``#{window_activity}`` older than ``fleet.ACTIVITY_WINDOW``; ``None``: tmux did not say,
    or the pane is not the agent's and was not asked."""
    items: tuple[NeedsItem, ...]
    """Every current item of this project whose ``agent`` is this label, project-level kinds
    included, and dismissed ones too: a dismissal hides a card, it does not close a dialog."""


def _needs_lists_every(project_id: str) -> bool:
    """A source's ``has_live_agents`` when it says nothing: every project is listed."""
    return True


def _needs_answers_every(socket: str) -> bool:
    """A source's ``tmux_answers`` when it says nothing: every server answered."""
    return True


def _needs_no_event(
    project_id: str, session_id: str, kind: str, since: datetime
) -> TeamEvent | None:
    """A source's ``session_event`` when it says nothing: the window is all there is."""
    return None


def _needs_no_output(agent: FleetAgent) -> datetime | None:
    """A source's ``pane_output`` when it says nothing: tmux would not say."""
    return None


@dataclass(frozen=True)
class NeedsSources:
    """Everything the scan reads, as callables: the live store and tmux, or a test's fakes.

    Each read is bounded by what the scan can use. It runs every few seconds, and the
    store keeps every row, session and event a project ever had.
    """

    list_projects: Callable[[], list[ProjectInfo]]
    list_agents: Callable[[ProjectInfo], list[FleetAgentStatus]]
    """``fleet.list_agents(project, live_only=True)``. It runs first: it records dead panes
    as ended, exit status and all, which ``crashed`` reads."""
    ended_agents: Callable[[str, datetime], list[FleetAgent]]
    """The project's rows that ended at or after the given time."""
    board_events: Callable[[str, int], list[TeamEvent]]
    """The project's newest events, as many as given: a window, read for its agents' own."""
    board_since: Callable[[str, datetime], list[TeamEvent]]
    """The project's events written at or after the given time that open a board item or
    close one: its agents' questions, results and decisions (:data:`_NEEDS_ASKED`), and
    the human's notes, decisions and results (:data:`_NEEDS_REPLIED`); and the exits the
    fleet announces (``agent_exited``), which say how a row ended. An item lives for
    :data:`QUESTION_HORIZON` however busy the board is, so it is read by time, not from a
    window of the newest events that 300 notes push it out of."""
    board_sessions: Callable[[str, datetime, Collection[str]], list[TeamSession]]
    """The project's sessions seen at or after the given time, and those with the given ids:
    the ones a live manager may be, and the authors of the board's open questions."""
    task_status: Callable[[str], str | None]
    """A task's status; ``None`` when it is gone."""
    transcript_tail: Callable[[str], TranscriptTail | None]
    accounts: Callable[[], AccountsSettings]
    has_live_agents: Callable[[str], bool] = _needs_lists_every
    """Whether the project has a row that has not ended. The scan lists only a project that
    has: ``fleet.list_agents`` reads every row and session the project ever had, and with
    no live row there is no pane to ask tmux about and no death to record."""
    tmux_answers: Callable[[str], bool] = _needs_answers_every
    """Whether a tmux server listens on the given socket (``TmuxServer.answers``): asked
    only where the listing's states cannot tell (:func:`_needs_unheard`)."""
    session_event: Callable[[str, str, str, datetime], TeamEvent | None] = _needs_no_event
    """A session's newest board event of a kind written at or after the given time, for the
    facts the window of ``board_events`` no longer holds (:func:`_needs_own_events`). The
    time bounds what the store walks back through to find none."""
    pane_output: Callable[[FleetAgent], datetime | None] = _needs_no_output
    """When the row's pane last printed (``#{window_activity}``); ``None``: tmux would not
    say. Asked only of an agent whose sub-agent waits on a prompt (:func:`needs_from_agent`)."""


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
    pane_output: Callable[[], datetime | None] | None = None,
) -> list[NeedsItem]:
    """What one agent needs from the human: at most one item, by the first rule that holds.

    In order (SPEC §4.2):

    1. derived ``lost``, its pane gone → ``lost``;
    2. derived ``limited`` → ``limited``;
    3. a pending ``AskUserQuestion`` → ``question``;
    4. a pending ``ExitPlanMode`` → ``plan``;
    5. any other pending tool, with attention → ``permission``, about the OLDEST one: each
       prompt has its own tool use, so the 2nd prompt of a turn is a new item. Not under
       a ``Task``, whose sub-agent's tool uses are in its own records: every prompt of the
       sub-agent's has that one pending tool, so the prompt is also its notice, the
       ``last_seen_at`` each notification moves (:func:`_needs_subagent_prompt`);
    6. no pending tool, and the newest record an interruption later than the session's last
       hook → ``interrupted``, whatever the row reads (Esc fires no Stop, so a dismissed
       prompt still reads ``attention`` and an interrupted turn ``working``);
    7. attention, and its notification is the usage-limit dialog → ``limited``;
    8. attention → ``permission``, the dialog form: an MCP elicitation, Claude Code's own;
    9. ``waiting`` on its own words, which end on a question → ``asked``.

    Rules 7 and 8 read the notification from the session's newest ``attention`` event
    only while it still names the dialog on screen (:func:`_needs_notice`); after it a
    dialog is the plain form, its words not on the board. So the usage-limit dialog is a
    ``limited`` card only as the first notice of its turn; later in a turn it is the
    plain dialog's, without Switch (the agent's own menu has it) and pushed at once.

    Attention is the derived ``attention``, or a session still marked so after the row
    went stale (past ``_STALE_AFTER`` it derives ``waiting``, the dialog maybe still up).
    Rules 7 and 8 also hold for a row that reads ``working`` on output since the notice,
    while its transcript shows its agent wrote nothing since (:func:`_needs_unanswered`):
    a key that moves the dialog's highlight prints too, and a card that went and came back
    with it answered ``stale``. Rule 5 does not: a granted tool prints until it ends, its
    prompt answered.
    Records older than the row are ignored throughout: a resumed session's old pending tool
    or closing question belong to the process before it. Without a readable tail, rules
    3 to 6 and 9 cannot hold. ``exited`` and ``unknown`` agents need nothing here; ``crashed``,
    ``manager_down`` and ``fleet_down`` speak for them. ``manager_live`` and ``accounts``
    decide only when a push may go out. ``pane_output`` says when the agent's pane last
    printed, asked only of a sub-agent's prompt.
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
        if pending[0].name in _SUBAGENT_TOOLS and session is not None:
            return _needs_subagent_prompt(
                pending[0], session, pane_output, project=project, agent=agent, now=now
            )
        return [
            _needs_permission_item(pending[0], project=project, agent=agent, name=name, now=now)
        ]
    if tail is not None and _needs_marker_later(status, tail):
        return [_needs_interrupted_item(tail, project=project, agent=agent, name=name, now=now)]
    if attention or _needs_unanswered(status, tail, unread=False):
        notice = _needs_notice(attention_event, tail)
        if notice is not None and LIMIT_DIALOG.search(notice.text):
            since = notice.created_at
            return [
                _needs_item(
                    "limited",
                    f"attention:{notice.seq}",
                    project=project,
                    agent=agent,
                    reason=f"{name} hit its usage limit (Claude Code is asking what to do)",
                    excerpt=notice.text,
                    detail=_needs_fit({"text": notice.text}, _DETAIL_TEXT_MAX),
                    since=since,
                    push_after=_needs_limited_push(
                        since, None, now=now, manager_live=manager_live, accounts=accounts
                    ),
                )
            ]
        seen = session.last_seen_at if session is not None else now
        seq = "-" if attention_event is None else str(attention_event.seq)
        text = "" if notice is None else notice.text
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


def _needs_notice(event: TeamEvent | None, tail: TranscriptTail | None) -> TeamEvent | None:
    """The session's newest ``attention`` event, while it still names the dialog on screen.

    ``mark_attention`` flips a session once per turn, so a turn's first notice is
    the only one the board records: a later one moves ``last_seen_at`` and writes
    nothing. Once the agent wrote anything after the event (a granted tool's
    result, its reply after the dialog was answered), the dialog the event named
    was answered, and one on screen now is another, which its text would misname:
    the usage-limit dialog read as the Bash prompt approved before it, and the
    reverse. Without a tail nothing says it moved on.
    """
    if event is None:
        return None
    if tail is not None and tail.newest_at is not None and tail.newest_at > event.created_at:
        return None
    return event


def _needs_attention(status: FleetAgentStatus) -> bool:
    """Derived ``attention``, or a session still marked so after its row went stale."""
    session = status.session
    return status.state == "attention" or (
        status.state == "waiting" and session is not None and session.state == "attention"
    )


def _needs_unanswered(
    status: FleetAgentStatus, tail: TranscriptTail | None, *, unread: bool
) -> bool:
    """A row that reads ``working`` while its session is still marked ``attention``, and its
    agent wrote nothing since the notice: a dialog may be up all the same.

    ``fleet._derive`` takes output after the notice for the dialog answered (#153), for
    ``fleet.ACTIVITY_WINDOW``. But a key that moves the dialog's highlight, the redraw of
    an attach or a resize and an Escape that did not close it print too, and for those
    seconds a dialog with no tool behind it (the usage-limit one, Claude Code's own) read
    as an agent at work: no card, and a stop's ``/exit`` and Enter picked the highlighted
    option. An answer leaves a record newer than the notice in the transcript (the reply
    to a choice, an interruption); a dialog still up leaves none, and an empty transcript
    none at all. A pending tool is not looked at here: a granted one prints while it runs
    and writes nothing until it ends, so this would read it as its own prompt.

    ``unread`` is the answer when the transcript cannot say: none to read, records without
    times, or none the tail's walk could read as conversation (``newest`` is ``none`` then
    as well, but the file is not empty: it says nothing of what was written). Then nothing
    tells a dialog whose pane printed from a granted tool at work, which prints until the
    turn's Stop. The guard takes it for a dialog, a refusal being its cheap mistake
    (:func:`needs_dialog_open`); the feed does not, a card it cannot vouch for, there for
    the rest of the turn, being its dear one. Nor can a key that only moves a dialog's
    highlight be told from thinking: the card of a dialog answered at the machine stays
    until the agent writes its first text or tool record (thinking alone moves no
    ``newest_at``), a stale card at worst, never a key typed.
    """
    session = status.session
    if status.state != "working" or session is None or session.ended_at is not None:
        return False
    if session.state != "attention":
        return False
    if tail is not None and tail.empty:
        return True  # nothing written at all, so nothing since the notice
    if tail is None or tail.newest_at is None:
        return unread
    return tail.newest_at <= session.last_seen_at


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


def _needs_subagent_prompt(
    tool: PendingTool,
    session: TeamSession,
    pane_output: Callable[[], datetime | None] | None,
    *,
    project: ProjectInfo,
    agent: FleetAgent,
    now: datetime,
) -> list[NeedsItem]:
    """A prompt of a sub-agent's: one item per notification, never the one before's.

    The sub-agent's own tool uses are in its own records, so the ``Task`` it runs
    in is the one pending tool through all its prompts. Named after it alone,
    every prompt after the first was the first again: never pushed, hidden by its
    dismissal, and answered by a card left from it, whose "1" approved whatever
    the sub-agent asked next. Each prompt sends its own notification, which moves
    ``last_seen_at``, so the item is about the tool and that moment. In the seconds
    between a new prompt's drawing and its notification, the pane printed after
    ``last_seen_at``, which still names the prompt before: there is no item until the
    notification lands, or :data:`_NOTICE_WAIT` passes without one.
    """
    seen = session.last_seen_at
    output = None if pane_output is None else pane_output()
    if output is not None and output > seen and now - output < _NOTICE_WAIT:
        return []
    return [
        _needs_permission_item(
            tool,
            project=project,
            agent=agent,
            name=needs_push_safe(agent.label),
            now=now,
            subject=f"{tool.tool_use_id}:{seen.isoformat()}",
            since=seen,
        )
    ]


def _needs_permission_item(
    tool: PendingTool,
    *,
    project: ProjectInfo,
    agent: FleetAgent,
    name: str,
    now: datetime,
    subject: str | None = None,
    since: datetime | None = None,
) -> NeedsItem:
    """A permission prompt: the full command or path in ``detail``, so nobody approves blind.

    The buttons are the dialog's own digits and Esc; the card shows the live
    pane beside them, so the options' real text is on screen. A prompt under a
    ``Task``/``Agent`` tool is a sub-agent's, whose own tool use is in its own
    records, not this transcript. ``subject`` and ``since`` default to the
    tool use's.
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
    since = since or tool.at or now
    return _needs_item(
        "permission",
        subject or tool.tool_use_id,
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
    rows say, a row whose session is parked on its usage limit not counting.
    """
    by_session = {session.id: session for session in sessions}
    rows: dict[str, FleetAgent] = {}
    for agent in sorted(agents, key=lambda agent: agent.created_at):
        if agent.session_id:
            rows[agent.session_id] = agent  # the newest row of a session names it
    if manager_live is None:
        parked = {session.id for session in sessions if session.state == "limited"}
        live_rows = [row for row in agents if row.ended_at is None and row.session_id not in parked]
        rowed = {row.session_id for row in agents if row.session_id}
        manager_live = _needs_manager_live(live_rows, rowed, sessions, now)
    addressed: set[str] = set()  # whom the human's later writes were addressed to
    moved_on: set[str] = set()  # sessions that asked, reported or decided again later
    items: list[NeedsItem] = []
    for event in sorted(events, key=_needs_seq, reverse=True):
        if event.session_id is None:
            if event.kind in _NEEDS_REPLIED:
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
        if event.kind in _NEEDS_ASKED:
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
    rowed: Collection[str],
    sessions: Sequence[TeamSession],
    now: datetime,
) -> bool:
    """Whether the project has a manager to act on what needs doing.

    A manager among ``live_rows`` says so outright: the caller passes only the
    rows that can act, never one parked on its usage limit (it fires no hook and
    takes no nudge until the reset, which can be hours away) or one whose tmux
    would not answer. Otherwise a manager session with no fleet row (one started
    outside the fleet, say) counts while it has not ended, is not parked on a
    limit and was seen within the board's stale window. A session that has a row
    (``rowed``) is its row's to decide: a crash fires no ``SessionEnd``, so a
    dead manager's session read live for half an hour after it died, and a
    parked one's for as long as it waited.
    """
    if any(_needs_is_manager(row.role) for row in live_rows):
        return True
    return any(
        _needs_is_manager(session.role)
        and session.ended_at is None
        and session.state != "limited"
        and now - session.last_seen_at <= _MANAGER_FRESH
        and session.id not in rowed
        for session in sessions
    )


# --- one project --------------------------------------------------------------------------


_NEEDS_NOT_ACTING = frozenset({"exited", "lost", "limited", "unknown"})
"""Derived states of a live row that takes no nudge: dead, gone, parked on its usage limit
until a reset that can be hours away, or on a tmux server that would not answer. A manager
in one of them is no manager to leave a crash or a coder's question to."""


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
    return _needs_read_or_none(read, what, project) or []


def _needs_read_or_none(
    read: Callable[[], list[_T]], what: str, project: ProjectInfo
) -> list[_T] | None:
    """:func:`_needs_read`, ``None`` when the read failed: for facts whose absence says
    something, as a manager's last word that is not its result does."""
    try:
        return read()
    except Exception:
        log.debug("remote: needs could not read %s of %s", what, project.id, exc_info=True)
        return None


def _needs_scan_project(
    sources: NeedsSources,
    project: ProjectInfo,
    statuses: list[FleetAgentStatus] | None,
    *,
    now: datetime,
    first_seen: MutableMapping[str, datetime],
    seen: set[str],
    accounts: AccountsSettings | None,
    answers: Callable[[str], bool] | None = None,
) -> _NeedsProject:
    """Every item of one project. ``statuses`` is its ``list_agents``; ``None``, it failed.

    Without a listing only the board speaks: an agent's state, a crash, a
    manager gone and tmux down all depend on rows the listing could not read.
    ``first_seen`` dates the items whose facts carry no date (a pane gone, tmux
    not answering) from the first scan that saw them; ``seen`` collects what
    this scan saw, so the caller can forget the rest. ``answers`` says whether a
    tmux server answers (:func:`_needs_unheard`), once per socket for a whole scan.
    """
    from aisquare.services.fleet import RECENTLY_ENDED

    if statuses is not None:
        statuses = _needs_unheard(statuses, answers or _needs_hearing(sources))
    ended = _needs_read(
        lambda: sources.ended_agents(project.id, now - RECENTLY_ENDED), "rows", project
    )
    day = now - max(QUESTION_HORIZON, RECENTLY_ENDED)
    board_read = _needs_read_or_none(lambda: sources.board_since(project.id, day), "board", project)
    board = board_read or []
    window = _needs_window(sources, project)
    authors = _needs_board_authors(board, now)
    sessions = _needs_read(
        lambda: sources.board_sessions(project.id, now - _MANAGER_FRESH, authors),
        "sessions",
        project,
    )
    listed = statuses or []
    rows = list({row.id: row for row in [*ended, *(status.agent for status in listed)]}.values())
    rowed = {row.session_id for row in rows if row.session_id}
    acting = [
        status.agent
        for status in listed
        if status.agent.ended_at is None and status.state not in _NEEDS_NOT_ACTING
    ]
    manager_live = _needs_manager_live(acting, rowed, sessions, now)
    items: list[NeedsItem] = []
    tails: dict[str, TranscriptTail | None] = {}
    if statuses is not None:
        for status in statuses:
            tail = tails[status.agent.id] = _needs_tail_of(sources, status)
            live = manager_live
            if _needs_is_manager(status.agent.role):
                # Its own items wait for no manager but another: at the usage-limit dialog
                # a manager reads attention, and its limit's push waited 90 s for itself.
                others = [row for row in acting if row.id != status.agent.id]
                live = _needs_manager_live(others, rowed, sessions, now)
            for item in needs_from_agent(
                status,
                tail,
                project=project,
                events=_needs_own_events(sources, project, status, window),
                now=now,
                manager_live=live,
                accounts=accounts,
                pane_output=_needs_output_of(sources, status.agent),
            ):
                items.append(_needs_dated(item, first_seen, seen) if item.kind == "lost" else item)
        items.extend(
            _needs_crashed(
                ended,
                rows,
                board,
                project=project,
                now=now,
                manager_live=manager_live,
                sources=sources,
            )
        )
        items.extend(_needs_manager_down(statuses, rows, board_read, project=project, now=now))
        items.extend(
            _needs_fleet_down(statuses, project=project, now=now, first_seen=first_seen, seen=seen)
        )
    items.extend(
        needs_from_board(board, sessions, rows, project=project, now=now, manager_live=manager_live)
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


def _needs_own_events(
    sources: NeedsSources,
    project: ProjectInfo,
    status: FleetAgentStatus,
    window: Callable[[], Sequence[TeamEvent]],
) -> list[TeamEvent]:
    """The board events :func:`needs_from_agent` reads of an agent: its session's newest
    ``attention`` and ``limited`` events, wherever they are.

    ``window`` is the project's newest :data:`NEEDS_BOARD_EVENTS`, read only for a row
    parked on its limit or a session marked ``attention``: no rule reads the events of any
    other. The team writes ``limited`` once per park and ``attention`` once per turn, so
    while an agent stays parked, or a dialog stays up overnight, its event leaves that
    window as newer ones come in; and the item keyed on it became another: a new id,
    pushed again, its dismissal lost, the usage-limit dialog read as a plain one. A kind
    the window lacks is asked of the store, for that one session, where a rule reads it.
    Only since the row was created, from the window too: a session's start puts it back
    to ``working``, so the park or the dialog it is in began after its process did, and
    an older event is the process before it's. Asked without that bound, a session with
    no such event cost every event the project ever had, every scan.
    """
    session = status.session
    if session is None or status.state in ("exited", "unknown", "lost"):
        return []
    if status.state != "limited" and session.state != "attention":
        return []
    born = status.agent.created_at
    own = [e for e in window() if e.session_id == session.id and e.created_at >= born]
    # A limit names its item by its own event, and falls back on the attention one.
    wanted = ("limited", "attention") if status.state == "limited" else ("attention",)
    for kind in wanted:
        if any(event.kind == kind for event in own):
            break
        found = _needs_session_event(sources, project, session.id, kind, born)
        if found is not None:
            own.append(found)
            break
    return own


def _needs_output_of(sources: NeedsSources, agent: FleetAgent) -> Callable[[], datetime | None]:
    """When ``agent``'s pane last printed, asked of tmux only when called."""

    def needs_output() -> datetime | None:
        try:
            return sources.pane_output(agent)
        except Exception:
            log.debug("remote: needs could not ask tmux about %s", agent.pane_id, exc_info=True)
            return None

    return needs_output


def _needs_window(sources: NeedsSources, project: ProjectInfo) -> Callable[[], list[TeamEvent]]:
    """The project's newest :data:`NEEDS_BOARD_EVENTS` events, read the first time they are
    asked for in a scan, and not at all by one that asks nothing of them: most scans of
    most projects, whose agents are not at a dialog or parked on a limit."""
    read: list[list[TeamEvent]] = []

    def needs_window() -> list[TeamEvent]:
        if not read:
            read.append(
                _needs_read(
                    lambda: sources.board_events(project.id, NEEDS_BOARD_EVENTS), "board", project
                )
            )
        return read[0]

    return needs_window


def _needs_session_event(
    sources: NeedsSources, project: ProjectInfo, session_id: str, kind: str, since: datetime
) -> TeamEvent | None:
    try:
        return sources.session_event(project.id, session_id, kind, since)
    except Exception:
        log.debug("remote: needs could not read %s's events", session_id, exc_info=True)
        return None


def _needs_hearing(sources: NeedsSources) -> Callable[[str], bool]:
    """``sources.tmux_answers``, asked once per socket: one per scan, across its projects.

    A question that fails to be put is no sign the server is gone: it answers yes.
    """
    heard: dict[str, bool] = {}

    def needs_heard(socket: str) -> bool:
        if socket not in heard:
            try:
                heard[socket] = sources.tmux_answers(socket)
            except Exception:
                log.debug("remote: needs could not ask tmux on %s", socket, exc_info=True)
                heard[socket] = True
        return heard[socket]

    return needs_heard


def _needs_unheard(
    statuses: list[FleetAgentStatus], answers: Callable[[str], bool]
) -> list[FleetAgentStatus]:
    """The listing, with each live row on a tmux server that does not answer read ``unknown``.

    ``fleet._derive`` takes a fresh board row over tmux (§5.1). With the server
    gone, a row reads working, waiting or attention for the board's stale window
    (30 minutes), and ``limited`` until its reset: ``fleet_down``, every live row
    ``unknown``, came that late, under cards for agents tmux had taken with it. A
    row reads ``unknown`` only when the listing could not ask its server, since a
    row it could ask reads its pane (``lost`` once it is gone, ``exited`` once it
    is dead), so one such row says its server did not answer and one of those says
    it did. A server whose live rows all read from the board is asked.
    """
    live = [status for status in statuses if status.agent.ended_at is None]
    silent: set[str] = set()
    for socket in sorted({status.agent.tmux_socket for status in live}):
        states = {status.state for status in live if status.agent.tmux_socket == socket}
        if "unknown" in states or (not states & {"lost", "exited"} and not answers(socket)):
            silent.add(socket)
    return [
        status.model_copy(
            update={
                "state": "unknown",
                "detail": "tmux unavailable" + ("" if status.agent.session_id else "; no hooks"),
            }
        )
        if status.agent.ended_at is None
        and status.agent.tmux_socket in silent
        and status.state != "unknown"
        else status
        for status in statuses
    ]


def _needs_board_authors(events: Sequence[TeamEvent], now: datetime) -> set[str]:
    """The sessions whose board questions and results may still need the human: the ones
    :func:`needs_from_board` names an item's author from."""
    return {
        event.session_id
        for event in events
        if event.session_id
        and event.kind in ("question", "result")
        and now - event.created_at <= QUESTION_HORIZON
    }


def _needs_dated(
    item: NeedsItem, first_seen: MutableMapping[str, datetime], seen: set[str]
) -> NeedsItem:
    """``item`` dated from the first scan that saw it, its push delay moved with it."""
    key = _needs_memory_key(item.project_id, item.id)
    seen.add(key)
    since = first_seen.setdefault(key, item.since)
    if since == item.since:
        return item
    shift = item.since - since
    pushed = None if item.push_after is None else item.push_after - shift
    return replace(item, since=since, push_after=pushed)


def _needs_memory_key(project_id: str, what: str) -> str:
    """The watcher's key for when it first saw ``what`` of a project, the project's first."""
    return f"{project_id}|{what}"


def _needs_still_remembered(
    first_seen: MutableMapping[str, datetime], seen: set[str], project: ProjectInfo
) -> None:
    """Keep every date ``first_seen`` holds for ``project`` through a scan that could not look.

    A listing that failed, or a project whose scan did, says nothing of whether a
    pane is still gone or tmux still silent. Forgotten, the next scan that saw it
    again dated it anew: ``fleet_down``, whose id is its first sighting, became a
    new item, pushed again past the dismissal of the one before.
    """
    prefix = _needs_memory_key(project.id, "")
    seen.update(key for key in first_seen if key.startswith(prefix))


def _needs_crashed(
    ended: Sequence[FleetAgent],
    rows: Sequence[FleetAgent],
    board: Sequence[TeamEvent],
    *,
    project: ProjectInfo,
    now: datetime,
    manager_live: bool,
    sources: NeedsSources,
) -> list[NeedsItem]:
    """Agents that died with a failing exit status in the last hour, with nobody on it.

    A clean ``/exit`` is 0 and a forced stop has no status: neither is a crash.
    Except the stop of a switch or a restart that then could not start the
    replacement (its ``/exit``, or the kill when that did not land in time):
    nothing took the agent's place, and the exit it announced says so
    (``fleet.HANDOVER_FAILED``, read from the board's day, ``board``). An
    agent whose task is closed did its work; one a live manager has (it was
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
        if _needs_is_manager(row.role):
            continue
        clean = row.exit_status in (0, None)
        abandoned = clean and _needs_handover_failed(_needs_exit_of(row, board))
        if clean and not abandoned:
            continue
        if newest.get(row.label, row.created_at) > row.created_at:
            continue
        if row.task_id is not None and _needs_task_closed(sources, row.task_id):
            continue
        name = needs_push_safe(row.label)
        items.append(
            _needs_item(
                "crashed",
                row.id,
                project=project,
                agent=row,
                reason=(
                    f"{name} stopped, and its replacement did not start"
                    if abandoned
                    else f"{name} exited unexpectedly (exit {row.exit_status})"
                ),
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


def _needs_handover_failed(event: TeamEvent | None) -> bool:
    """Whether ``event`` is the exit a hand-over announced once its replacement did not start."""
    from aisquare.services import fleet as fleet_service

    return (
        event is not None
        and event.kind == "agent_exited"
        and event.text.endswith(fleet_service.HANDOVER_FAILED)
    )


def _needs_exit_of(row: FleetAgent, board: Sequence[TeamEvent]) -> TeamEvent | None:
    """The exit the fleet announced for ``row`` (``agent_exited``, under its session), from
    the board's day; none older than the row, which is an earlier row's of the session."""
    if row.session_id is None:
        return None
    return max(
        (
            event
            for event in board
            if event.kind == "agent_exited"
            and event.session_id == row.session_id
            and event.created_at >= row.created_at
        ),
        key=_needs_seq,
        default=None,
    )


def _needs_last_word(row: FleetAgent, board: Sequence[TeamEvent]) -> TeamEvent | None:
    """What ``row``'s session last posted of its own on the board's day: its newest question,
    result or decision. Not its notes, and not the exits the fleet announces under it."""
    if row.session_id is None:
        return None
    return max(
        (
            event
            for event in board
            if event.session_id == row.session_id and event.kind in _NEEDS_ASKED
        ),
        key=_needs_seq,
        default=None,
    )


def _needs_manager_down(
    statuses: Sequence[FleetAgentStatus],
    rows: Sequence[FleetAgent],
    board: Sequence[TeamEvent] | None,
    *,
    project: ProjectInfo,
    now: datetime,
) -> list[NeedsItem]:
    """The manager has ended and none replaced it, while its crew still needs one.

    A failing exit status is always reported. A forced stop (no status) is
    reported only while another agent still works, waits on a prompt or is
    limited, and the manager's last word on the board was not its ``result``:
    a manager stopped after reporting, or exiting cleanly, finished its job.
    Its last word is its newest question, result or decision of the board's
    day (``board``), not the exit the fleet announces for it under its session
    once it is stopped, which followed every result and hid it. Not a clean
    exit a switch or a restart made, though, and then could not start the
    replacement for: the hand-over's own ``/exit`` is status 0, and the exit it
    announces says so (``fleet.HANDOVER_FAILED``). That one is reported as a
    stop is. ``board`` is ``None`` when the day could not be read: a stop is then
    not reported at all, rather than for a last word nobody read; a crash needs no
    board.
    """
    managers = [row for row in rows if _needs_is_manager(row.role)]
    if not managers or any(row.ended_at is None for row in managers):
        return []
    manager = max(managers, key=lambda row: row.created_at)
    if manager.ended_at is None:
        return []
    if manager.exit_status not in (0, None):
        reason = f"the manager exited unexpectedly (exit {manager.exit_status})"
    else:
        busy = [
            status
            for status in statuses
            if status.agent.id != manager.id
            and status.agent.ended_at is None
            and status.state in ("working", "attention", "limited")
        ]
        if not busy or board is None:
            return []
        if manager.exit_status == 0 and not _needs_handover_failed(_needs_exit_of(manager, board)):
            return []
        said = _needs_last_word(manager, board)
        if said is not None and said.kind == "result":
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

    ``statuses`` has been through :func:`_needs_unheard`, so a fresh board row on a
    server that is gone reads ``unknown`` too. The subject is the first scan that
    saw it, so it is one item until the condition clears and a new one if it comes
    back.
    """
    live = [status for status in statuses if status.agent.ended_at is None]
    if not live or any(status.state != "unknown" for status in live):
        return []
    key = _needs_memory_key(project.id, "fleet_down")
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
    it, each scan is the first. What it holds for a project that could not be
    looked at this time is kept for the next look.
    """
    memory: MutableMapping[str, datetime] = {} if first_seen is None else first_seen
    seen: set[str] = set()
    accounts = _needs_accounts(sources)
    answers = _needs_hearing(sources)
    items: list[NeedsItem] = []
    for project in sources.list_projects():
        statuses: list[FleetAgentStatus] | None
        try:
            statuses = sources.list_agents(project) if _needs_has_live(sources, project) else []
        except Exception:
            log.debug("remote: needs could not list the agents of %s", project.id, exc_info=True)
            statuses = None
        if statuses is None:
            _needs_still_remembered(memory, seen, project)
        try:
            scanned = _needs_scan_project(
                sources,
                project,
                statuses,
                now=now,
                first_seen=memory,
                seen=seen,
                accounts=accounts,
                answers=answers,
            )
        except Exception:
            log.warning("remote: the needs scan of %s failed", project.id, exc_info=True)
            _needs_still_remembered(memory, seen, project)
            continue
        items.extend(scanned.items)
    for key in [key for key in memory if key not in seen]:
        del memory[key]
    return _needs_ranked([item for item in items if item.id not in dismissed])


def _needs_has_live(sources: NeedsSources, project: ProjectInfo) -> bool:
    """Whether to list ``project``: it has a live row, or the store would not say."""
    try:
        return sources.has_live_agents(project.id)
    except Exception:
        log.debug("remote: needs could not count the live rows of %s", project.id, exc_info=True)
        return True


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


_NEEDS_PANE_UNVOUCHED = frozenset({"lost", "exited", "unknown"})
"""Derived states whose pane id the listing did not vouch for. ``lost`` is a pane gone, or a
row that outlived its tmux server, whose pane id the next server gave to ANOTHER agent
(``fleet._outlived``, FLEET-1); ``exited`` is a dead pane; ``unknown``, a server that could
not be asked."""


def needs_agent_now(project: ProjectInfo, label: str, *, now: datetime | None = None) -> AgentNow:
    """One agent of ``project`` re-derived now, synchronously: never call it on the event loop.

    It runs the scan's part for this one project — one listing, its ended rows,
    its board, the cached tails — plus tmux questions about the label's pane: is
    its foreground the agent, and has its window been quiet. Only a pane the
    listing vouched for is asked about: one that reads ``lost`` may be another
    agent's pane under the row's old id, and an Escape an action sends on the
    strength of the answer would land in that agent's turn. The listing is not
    caught here: a snapshot made without it would claim a pane shows no dialog
    when nobody asked. Raises ``fleet.NoSuchAgent`` only when the label has had
    no row at all within ``RECENTLY_ENDED``.
    """
    from aisquare.services import fleet as fleet_service

    when = now or _needs_now()
    sources = live_needs_sources()
    statuses = sources.list_agents(project)
    scanned = _needs_scan_project(
        sources,
        project,
        statuses,
        now=when,
        first_seen={},
        seen=set(),
        accounts=_needs_accounts(sources),
    )
    rows = [
        row for row in [*scanned.ended, *(s.agent for s in scanned.statuses)] if row.label == label
    ]
    if not rows:
        raise fleet_service.NoSuchAgent(
            f"no agent {label!r} in {project.root.name or project.id} within the last day"
        )
    newest = max(rows, key=lambda row: row.created_at)
    status = next((s for s in scanned.statuses if s.agent.id == newest.id), None)
    return _needs_snapshot(
        project,
        status,
        None if status is None else scanned.tails.get(status.agent.id),
        tuple(item for item in scanned.items if item.agent == label),
        when,
    )


def needs_single_agent_now(
    project: ProjectInfo, label: str, *, now: datetime | None = None
) -> AgentNow:
    """The label's newest row re-derived now from its own facts alone: never call it on the
    event loop.

    What an action reads every quarter second while its Escape lands, for up to
    8 s, to learn whether the agent's dialog closed or its prompt came back
    (``remote_actions.action_settle``). :func:`needs_agent_now` answers that with
    the project's scan: tmux on every socket of the project, every row the project
    ever had, its board, its sessions and every agent's tail, and a listing that
    ends dead rows on the way. This reads the newest row holding the label,
    derives it on its own server from its own session (``fleet.status_of``), asks
    its pane what :func:`needs_agent_now` asks, and reads its tail as the scan reads
    every agent's: through the live sources' cached reader, the one of them it uses
    (review of #243, round 3, 4/13).

    So ``items`` are what :func:`needs_from_agent` derives for this agent without
    the board, and no project-level kind is derived. What the predicates read of
    them is the same: the usage-limit dialog, which the board names by its
    notification's text, is the plain dialog's ``permission`` item here, a dialog
    all the same. An id may differ from the scan's, so no card is matched against
    them: an action matches its card on :func:`needs_agent_now`'s snapshot, before
    it sends anything. ``status`` is never ``None``: an ended row reads
    ``exited``. Nothing is recorded: a dead pane reads ``exited``, and the next
    listing ends its row. Raises ``fleet.NoSuchAgent`` when no row holds the label.
    """
    from aisquare.core.store import store_session
    from aisquare.services import fleet as fleet_service

    when = now or _needs_now()
    with store_session() as store:
        row = store.fleet_agent_by_label(project.id, label, live_only=False)
    if row is None:
        raise fleet_service.NoSuchAgent(f"no agent {label!r} in {project.root.name or project.id}")
    status = fleet_service.status_of(row)
    tail = _needs_tail_of(live_needs_sources(), status)
    items = needs_from_agent(status, tail, project=project, events=(), now=when)
    return _needs_snapshot(project, status, tail, tuple(items), when)


def _needs_snapshot(
    project: ProjectInfo,
    status: FleetAgentStatus | None,
    tail: TranscriptTail | None,
    items: tuple[NeedsItem, ...],
    now: datetime,
) -> AgentNow:
    """The snapshot of ``status``'s row, with tmux asked about its pane only where the
    derivation vouched for it: a live row that reads none of :data:`_NEEDS_PANE_UNVOUCHED`."""
    from aisquare.services import fleet as fleet_service

    pane_is_agent, pane_quiet = False, None
    if (
        status is not None
        and status.agent.ended_at is None
        and status.state not in _NEEDS_PANE_UNVOUCHED
    ):
        server = fleet_service.server_for(status.agent.tmux_socket)
        pane_is_agent = _needs_pane_is_the_agent(server, status.agent)
        if pane_is_agent:
            pane_quiet = _needs_pane_quiet(server, status.agent.pane_id, now)
    return AgentNow(
        project=project,
        status=status,
        tail=tail,
        pane_is_agent=pane_is_agent,
        pane_quiet=pane_quiet,
        items=items,
    )


def _needs_pane_is_the_agent(server: TmuxServer, agent: FleetAgent) -> bool:
    """Whether the row's pane runs the agent now, on the server the row was recorded on.

    The listing vouches for a pane only when it could ask tmux: a fresh board
    row derives its state without it. And a server that started after the row
    was written numbers its panes from ``%0`` again, so the pane under the row's
    id is another agent's (``fleet._outlived``). ``send-keys`` asks the same two
    questions before it types, so both ask them in one place:
    ``remote_server._remote_pane_refusal``.
    """
    from aisquare.services import remote_server

    return remote_server._remote_pane_refusal(server, agent) is None


def _needs_pane_quiet(server: TmuxServer, pane_id: str, now: datetime) -> bool | None:
    """Whether the pane's window printed nothing for ``fleet.ACTIVITY_WINDOW``.

    Claude Code animates its spinner while a tool runs, so a quiet pane with a
    tool pending is a dialog waiting. One-second resolution, the fact
    ``fleet._derive`` reads too; ``None`` when tmux would not say.
    """
    from aisquare.services import fleet as fleet_service

    output = _needs_pane_output_at(server, pane_id)
    return None if output is None else now - output > fleet_service.ACTIVITY_WINDOW


def _needs_pane_output_at(server: TmuxServer, pane_id: str) -> datetime | None:
    """When the pane's window last printed (``#{window_activity}``, to the second rounded
    down); ``None`` when tmux would not say."""
    try:
        raw = server.run("display-message", "-p", "-t", pane_id, "#{window_activity}").strip()
    except Exception:
        return None
    return datetime.fromtimestamp(int(raw), tz=UTC) if raw.isdigit() else None


def needs_dialog_open(snap: AgentNow) -> bool:
    """Whether the agent may show a dialog that an Enter (or a typed ``/exit``) would answer.

    Never for a pane that is not the agent's: an exited, lost or not-yet-started
    agent shows no dialog, even when its transcript ends on a pending tool (a
    crash mid-tool). Otherwise any of: a pending tool in a quiet pane (the
    spinner stops while a dialog waits); attention with no interruption since,
    the row reading ``working`` on output its dialog may have printed included,
    unless its transcript shows the agent wrote since (:func:`_needs_unanswered`,
    which a transcript that cannot be read does not show); a current prompt,
    question or plan item, or the usage-limit dialog. A false positive costs a
    refusal with a sentence, or an Escape to an agent about to be stopped anyway —
    never an Enter into a dialog.

    A dialog's first seconds are not seen here: quiet means no output for
    ``fleet.ACTIVITY_WINDOW`` (5 s), and the notification that makes the row
    ``attention`` comes at 6 s, so until then a prompt reads like a tool at work.
    An action that would type an Enter asks :func:`needs_tool_pending` as well.
    """
    status = snap.status
    if status is None or not snap.pane_is_agent:
        return False
    pending = _needs_pending(snap.tail, status.agent)
    if pending and snap.pane_quiet is not False:
        return True
    if _needs_attention(status) and not (
        snap.tail is not None and _needs_marker_later(status, snap.tail)
    ):
        return True
    if not pending and _needs_unanswered(status, snap.tail, unread=True):
        return True
    return any(
        item.kind in ("permission", "question", "plan")
        # A `limited` item of a row that does not derive `limited` is the dialog form.
        or (item.kind == "limited" and status.state != "limited")
        for item in snap.items
    )


def needs_tool_pending(snap: AgentNow) -> bool:
    """Whether the agent's newest message holds a tool use that has no result yet.

    A tool at work, or a permission prompt for it that opened too recently for
    :func:`needs_dialog_open` to tell: in a dialog's first seconds the pane is
    not quiet yet and no notification has come. Nothing on screen tells the two
    apart, so a stop, a restart or a switch, whose ``/exit`` and Enter would
    answer "1. Yes", takes it for a prompt. Tools older than the row do not count
    (a resumed session's leftovers), and neither does a pane that is not the
    agent's.
    """
    status = snap.status
    if status is None or not snap.pane_is_agent:
        return False
    return bool(_needs_pending(snap.tail, status.agent))


def needs_at_input_prompt(snap: AgentNow) -> bool:
    """Whether the agent sits at its input prompt, where typed text is a message to it.

    No dialog, the pane is the agent and quiet (tmux must say so), no tool
    pending, and the newest record is an interruption or the agent's own words —
    or the row derives ``waiting``, the only sign there is without a tail.
    """
    status = snap.status
    if status is None or not snap.pane_is_agent or snap.pane_quiet is not True:
        return False
    if needs_dialog_open(snap):
        return False
    if snap.tail is None:
        return status.state == "waiting"
    if _needs_pending(snap.tail, status.agent):
        return False
    return snap.tail.newest in ("interrupted", "assistant_text") or status.state == "waiting"


def needs_item_current(snap: AgentNow, item_id: str) -> bool:
    """Whether ``item_id`` is still one of the agent's items right now."""
    return any(item.id == item_id for item in snap.items)


# --- the live sources ---------------------------------------------------------------------


def live_needs_sources() -> NeedsSources:
    """The scan over the live store and tmux: the same calls ``fleet ls`` and the board make."""
    from aisquare.core.store import AmbiguousIdError, store_session
    from aisquare.services import claude_accounts as claude_accounts_service
    from aisquare.services import fleet as fleet_service
    from aisquare.services import project as project_service

    def needs_rows_ended(project_id: str, since: datetime) -> list[FleetAgent]:
        with store_session() as store:
            return store.fleet_agents_ended_since(project_id, since)

    def needs_rows_live(project_id: str) -> bool:
        with store_session() as store:
            return bool(store.fleet_agents(project_id, live_only=True))

    def needs_board_events(project_id: str, limit: int) -> list[TeamEvent]:
        with store_session() as store:
            return store.recent_events(project_id, limit=limit)

    def needs_board_since(project_id: str, since: datetime) -> list[TeamEvent]:
        with store_session() as store:
            return store.team_events_since(
                project_id, since, kinds=(*_NEEDS_ASKED, "agent_exited"), human_kinds=_NEEDS_REPLIED
            )

    def needs_board_sessions(
        project_id: str, since: datetime, ids: Collection[str]
    ) -> list[TeamSession]:
        with store_session() as store:
            return store.team_sessions_seen_since(project_id, since, ids=sorted(ids))

    def needs_task_status(ref: str) -> str | None:
        with store_session() as store:
            try:
                task = store.get_task(ref)
            except AmbiguousIdError:
                return None
        return None if task is None else task.status

    def needs_live_agents(project: ProjectInfo) -> list[FleetAgentStatus]:
        return fleet_service.list_agents(project, live_only=True)

    def needs_tmux_answers(socket: str) -> bool:
        return fleet_service.server_for(socket).answers()

    def needs_pane_output(agent: FleetAgent) -> datetime | None:
        return _needs_pane_output_at(fleet_service.server_for(agent.tmux_socket), agent.pane_id)

    def needs_session_event(
        project_id: str, session_id: str, kind: str, since: datetime
    ) -> TeamEvent | None:
        with store_session() as store:
            return store.newest_session_event(project_id, session_id, kind, since=since)

    return NeedsSources(
        list_projects=project_service.list_projects,
        list_agents=needs_live_agents,
        ended_agents=needs_rows_ended,
        board_events=needs_board_events,
        board_since=needs_board_since,
        board_sessions=needs_board_sessions,
        task_status=needs_task_status,
        transcript_tail=_needs_cached_tail,
        accounts=claude_accounts_service.accounts_settings,
        has_live_agents=needs_rows_live,
        tmux_answers=needs_tmux_answers,
        session_event=needs_session_event,
        pane_output=needs_pane_output,
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


# --- the watcher --------------------------------------------------------------------------


class RemoteNeedsWatcher:
    """The scanner: a daemon thread scanning every ``interval`` while any device exists.

    Signed-out devices count: they still receive pushes. Each scan replaces the
    latest snapshot at once and then calls every listener in
    ``kit.needs_listeners`` with ``(all items, scanned_at)``; a listener that
    raises is logged and the rest are called. Readers (the stream, the
    heartbeat, the routes, the push sender) take the snapshot under a lock and
    do no I/O.
    """

    def __init__(
        self,
        kit: RemoteKit,
        *,
        sources: Callable[[], NeedsSources] = live_needs_sources,
        interval: float = NEEDS_SCAN_SECONDS,
        clock: Callable[[], datetime] = _needs_now,
    ) -> None:
        self._kit = kit
        self._sources = sources
        self._interval = interval
        self._clock = clock
        self._lock = threading.Lock()
        self._scanning = threading.Lock()
        """One scan at a time: the watcher's own, a route's, the one after a quick answer."""
        self._latest: list[NeedsItem] = []
        self._latest_json: list[dict[str, object]] = []
        self._scanned_at: datetime | None = None
        self._projects: dict[str, ProjectInfo] = {}
        self._first_seen: dict[str, datetime] = {}
        self._forgotten: set[str] = set()
        """Ids :meth:`needs_forget` dropped that no scan has read from the dismissals file yet."""
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    def start_watching(self) -> None:
        """Start the daemon thread ``asq-remote-needs``; its first scan runs at once."""
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._needs_loop, name="asq-remote-needs", daemon=True
            )
            self._thread.start()

    def stop_watching(self) -> None:
        """Stop the thread, waiting a few seconds for a scan in progress."""
        self._stopping.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5.0)

    def needs_watching(self) -> bool:
        """Whether the thread is scanning (a lifespan started it)."""
        return self._thread is not None and self._thread.is_alive()

    def _needs_loop(self) -> None:
        while not self._stopping.is_set():
            if self._needs_devices():
                try:
                    self.scan_needs_now()
                except Exception:
                    log.warning("remote: the needs scan failed", exc_info=True)
            self._stopping.wait(self._interval)

    def _needs_devices(self) -> bool:
        """Whether any device is on record, signed in or not: nobody to show it to, no scan."""
        try:
            return bool(self._kit.runtime.device_ids())
        except Exception:
            return False

    def scan_needs_now(self) -> list[NeedsItem]:
        """One synchronous scan: the snapshot replaced, then every listener called.

        The dismissals are read before the scan starts, and a scan takes as long as
        its projects' tmux and store reads do. A card dismissed meanwhile is not in
        what it read, and :meth:`needs_forget` drops it only from the snapshot there
        is then: published, the scan put it back on every phone and before the push
        sender, which could push it. So each id dropped since is dropped from what the
        scan publishes too, until a scan that read it from the file has published.
        """
        with self._scanning:
            now = self._clock()
            sources = self._sources()
            projects = sources.list_projects()
            dismissed = load_needs_dismissals()
            scanned = scan_needs_you(
                replace(sources, list_projects=lambda: projects),
                now=now,
                dismissed=dismissed,
                first_seen=self._first_seen,
            )
            with self._lock:
                items = [item for item in scanned if item.id not in self._forgotten]
                self._forgotten.difference_update(dismissed)
                payload = [item.needs_item_json() for item in items]
                self._latest, self._latest_json, self._scanned_at = items, payload, now
                self._projects = {project.id: project for project in projects}
            for listener in list(self._kit.needs_listeners):
                try:
                    listener(list(items), now)
                except Exception:
                    log.warning("remote: a needs listener failed", exc_info=True)
        return items

    def needs_items_now(self) -> list[NeedsItem]:
        """The latest scan's items, ranked."""
        with self._lock:
            return list(self._latest)

    def needs_scanned_at(self) -> datetime | None:
        """When the latest scan ran; ``None`` before the first."""
        with self._lock:
            return self._scanned_at

    def needs_items_json(self) -> list[dict[str, object]] | None:
        """The latest items in their wire shape, made once per scan; ``None`` before the first."""
        with self._lock:
            return None if self._scanned_at is None else self._latest_json

    def needs_payload_now(self) -> dict[str, object]:
        """``GET api/needs``: ``{"items", "scanned_at"}``."""
        from aisquare.services.remote_server import _iso_seconds

        with self._lock:
            scanned = self._scanned_at
            items = list(self._latest_json)
        stamp = None if scanned is None else _iso_seconds(scanned)
        return {"items": items, "scanned_at": stamp}

    def needs_lookup(self, item_id: str) -> tuple[NeedsItem, ProjectInfo] | None:
        """The latest scan's item with this id, and its project; ``None`` when it is not there."""
        with self._lock:
            item = next((item for item in self._latest if item.id == item_id), None)
            project = None if item is None else self._projects.get(item.project_id)
        return None if item is None or project is None else (item, project)

    def needs_forget(self, item_id: str) -> None:
        """Drop a dismissed item now, rather than at the next scan, and from a scan in flight.

        Called once the dismissal is on file: the scan that publishes next drops it too,
        whether or not it read the file before the dismissal reached it.
        """
        with self._lock:
            self._forgotten.add(item_id)
            self._latest = [item for item in self._latest if item.id != item_id]
            self._latest_json = [item for item in self._latest_json if item.get("id") != item_id]

    def needs_rescan_soon(self) -> None:
        """Scan again in :data:`NEEDS_RESCAN_AFTER_ANSWER` seconds, off every caller's thread."""
        timer = threading.Timer(NEEDS_RESCAN_AFTER_ANSWER, self._needs_rescan)
        timer.daemon = True
        timer.start()

    def _needs_rescan(self) -> None:
        try:
            self.scan_needs_now()
        except Exception:
            log.warning("remote: the needs scan after an answer failed", exc_info=True)


def _needs_watcher(kit: RemoteKit) -> RemoteNeedsWatcher:
    """The kit's watcher, or one that is not started (no lifespan): it scans when asked."""
    watcher = kit.lane_state.get("needs")
    if isinstance(watcher, RemoteNeedsWatcher):
        return watcher
    made = RemoteNeedsWatcher(kit, sources=live_needs_sources)
    kit.lane_state["needs"] = made
    return made


# --- the routes ---------------------------------------------------------------------------


def _needs_id_field(body: Mapping[str, object]) -> str:
    """The body's item ``id``: required, at most :data:`NEEDS_ID_MAX` characters."""
    from aisquare.services.remote_server import RequestError

    value = body.get("id")
    if not isinstance(value, str) or not value:
        raise RequestError(400, "invalid", "'id' is required: the needs item's id")
    if len(value) > NEEDS_ID_MAX:
        raise RequestError(413, "too_large", f"'id' is at most {NEEDS_ID_MAX} characters")
    return value


def _needs_answer_body(body: Mapping[str, object]) -> tuple[str, list[str], str, bool]:
    """``(id, keys, text, enter)`` of a quick answer: exactly one of ``keys`` and ``text``.

    The text reaches the pane byte for byte, as ``send-keys``' does, so it passes the
    same :func:`~aisquare.services.remote_server.check_remote_text`: a ``"\\x03"`` in it
    would be a Ctrl-C that no key allowlist saw and the audit line could not show.
    """
    from aisquare.services.remote_server import (
        SEND_KEYS_TEXT_MAX,
        RequestError,
        _remote_flag,
        check_remote_key_names,
        check_remote_text,
    )

    item_id = _needs_id_field(body)
    raw_keys, raw_text = body.get("keys"), body.get("text")
    if raw_text is not None and not isinstance(raw_text, str):
        raise RequestError(400, "invalid", "'text' must be a string")
    if raw_keys is not None and raw_text:
        raise RequestError(
            400, "text_and_keys", "send 'keys' or 'text', not both: the order would be lost"
        )
    keys = [] if raw_keys is None else check_remote_key_names(raw_keys)
    refused = [key for key in keys if key not in NEEDS_ANSWER_KEYS]
    if refused:
        allowed = ", ".join(sorted(NEEDS_ANSWER_KEYS))
        named = needs_push_safe(refused[0], 32)
        raise RequestError(
            400, "invalid_key", f"{named!r} is not an answer key — one of: {allowed}"
        )
    text = raw_text or ""
    if not keys and not text:
        raise RequestError(400, "invalid", "give 'keys' or 'text' to answer with")
    if len(text) > SEND_KEYS_TEXT_MAX:
        raise RequestError(413, "too_large", f"'text' is at most {SEND_KEYS_TEXT_MAX} characters")
    check_remote_text(text)
    return item_id, keys, text, _remote_flag(body, "enter")


def _needs_send(agent: FleetAgent, keys: Sequence[str], text: str, enter: bool) -> None:
    """Type the answer into the agent's pane: the keys, or the text, then Enter if asked."""
    from aisquare.services import fleet as fleet_service

    server = fleet_service.server_for(agent.tmux_socket)
    if keys:
        server.send_keys(agent.pane_id, *keys)
    if text:
        server.send_literal(agent.pane_id, text)
    if enter:
        server.send_keys(agent.pane_id, "Enter")


def needs_routes(kit: RemoteKit) -> list[BaseRoute]:
    """``GET api/needs``, ``POST api/needs/dismiss``, ``POST api/needs/answer`` (SPEC §1.3)."""
    import asyncio

    from starlette.responses import JSONResponse

    from aisquare.services import fleet as fleet_service
    from aisquare.services.remote_server import _audit_keys, remote_agent_lock

    async def needs_list_endpoint(
        request: Request, device: Device, body: dict[str, Any]
    ) -> Response:
        """The feed. A watcher that is not running (no lifespan) scans for this request."""
        watcher = _needs_watcher(kit)
        if not watcher.needs_watching() or watcher.needs_scanned_at() is None:
            await asyncio.to_thread(watcher.scan_needs_now)
        return JSONResponse(watcher.needs_payload_now())

    async def needs_dismiss_endpoint(
        request: Request, device: Device, body: dict[str, Any]
    ) -> Response:
        """Hide one card for good. Not write-gated: it changes what is shown, not the fleet."""
        item_id = _needs_id_field(body)
        watcher = _needs_watcher(kit)
        if watcher.needs_scanned_at() is None:
            await asyncio.to_thread(watcher.scan_needs_now)
        found = watcher.needs_lookup(item_id)
        if found is None:
            return kit.kit_refuse(404, "not_found", "no such item in the needs feed")
        item, _project = found
        await asyncio.to_thread(record_needs_dismissal, item.id)
        watcher.needs_forget(item.id)
        summary = f"{item.id} {item.kind} {item.agent or '-'}@{item.project_id}"
        await asyncio.to_thread(kit.kit_audit, device, "needs/dismiss", summary)
        return JSONResponse({"dismissed": item.id})

    async def needs_answer_endpoint(
        request: Request, device: Device, body: dict[str, Any]
    ) -> Response:
        """Answer a card on the agent it is about, only while the card is still true.

        The item comes from the latest scan; the agent is re-derived right
        before typing, under the agent's action lock, and a card that is no
        longer current is a 409 ``stale`` that carries what is current instead.
        """
        item_id, keys, text, enter = _needs_answer_body(body)
        watcher = _needs_watcher(kit)
        if watcher.needs_scanned_at() is None:
            await asyncio.to_thread(watcher.scan_needs_now)
        found = watcher.needs_lookup(item_id)
        if found is None:
            return kit.kit_refuse(409, "stale", "that card no longer needs you", current=[])
        item, project = found
        label = item.agent
        if item.kind not in _NEEDS_ANSWERABLE or label is None:
            board = item.kind in _NEEDS_BOARD_KINDS
            why = "reply on the board instead" if board else f"a {item.kind} card takes its actions"
            return kit.kit_refuse(400, "not_answerable", why)
        lock = remote_agent_lock(project.id, label)
        if not lock.acquire(blocking=False):
            return kit.kit_refuse(409, "busy", f"another action on {label} is still running")
        try:
            try:
                snap = await asyncio.to_thread(needs_agent_now, project, label)
            except fleet_service.NoSuchAgent:
                return kit.kit_refuse(409, "stale", f"{label} is gone", current=[])
            except fleet_service.FleetUnavailable as exc:
                return kit.kit_refuse(503, "fleet_unavailable", str(exc))
            except fleet_service.FleetError as exc:
                return kit.kit_refuse(409, "fleet_error", str(exc))
            if not needs_item_current(snap, item.id):
                current = [now_item.needs_item_json() for now_item in snap.items]
                gone = f"{label} no longer shows that {item.kind}"
                return kit.kit_refuse(409, "stale", gone, current=current)
            if snap.status is None or not snap.pane_is_agent:
                why = f"{label}'s pane is not running the agent — nothing was sent"
                return kit.kit_refuse(409, "not_agent", why)
            summary = (
                f"answer {item.id} {item.kind} {label}@{project.id} keys={_audit_keys(keys)} "
                f"text={len(text)}ch enter={enter}"
            )
            try:
                await asyncio.to_thread(_needs_send, snap.status.agent, keys, text, enter)
            except Exception as exc:
                # Part of it may have reached the pane: the trail says it was tried.
                await asyncio.to_thread(kit.kit_audit, device, "needs/answer", f"{summary} failed")
                return kit.kit_refuse(503, "fleet_unavailable", f"tmux could not type it: {exc}")
            await asyncio.to_thread(kit.kit_audit, device, "needs/answer", summary)
        finally:
            lock.release()
        watcher.needs_rescan_soon()
        return JSONResponse(
            {"answered": item.id, "agent": label, "project": project.id, "sent": True}
        )

    return [
        kit.kit_route("/api/needs", needs_list_endpoint, methods=["GET"], write_gated=False),
        kit.kit_route(
            "/api/needs/dismiss", needs_dismiss_endpoint, methods=["POST"], write_gated=False
        ),
        kit.kit_route(
            "/api/needs/answer", needs_answer_endpoint, methods=["POST"], write_gated=True
        ),
    ]


# --- the seams the server calls -----------------------------------------------------------


def start_needs_watch(kit: RemoteKit) -> Callable[[], None] | None:
    """Start the watcher at ``kit.lane_state["needs"]``; its stopper, for the lifespan."""
    watcher = RemoteNeedsWatcher(kit, sources=live_needs_sources, interval=NEEDS_SCAN_SECONDS)
    kit.lane_state["needs"] = watcher
    watcher.start_watching()
    return watcher.stop_watching


def needs_ws_frames(kit: RemoteKit) -> list[tuple[str, object]]:
    """``[("needs_you", {"items": [...]})]`` once the watcher has scanned; ``[]`` until then.

    No ``scanned_at`` in it, so the stream sends it only on a real change. No I/O:
    the items were made into their wire shape once, by the scan.
    """
    watcher = kit.lane_state.get("needs")
    if not isinstance(watcher, RemoteNeedsWatcher):
        return []
    items = watcher.needs_items_json()
    return [] if items is None else [("needs_you", {"items": items})]


def needs_scanned_iso(kit: RemoteKit) -> str | None:
    """When the watcher last scanned, for the heartbeat frame; ``None``: it never has."""
    from aisquare.services.remote_server import _iso_seconds

    watcher = kit.lane_state.get("needs")
    if not isinstance(watcher, RemoteNeedsWatcher):
        return None
    scanned = watcher.needs_scanned_at()
    return None if scanned is None else _iso_seconds(scanned)


def needs_cli_payload() -> dict[str, object]:
    """``asq remote needs``: one scan, here and now, in the shape of ``GET api/needs``."""
    from aisquare.services.remote_server import _iso_seconds

    now = _needs_now()
    items = scan_needs_you(live_needs_sources(), now=now, dismissed=load_needs_dismissals())
    return {
        "items": [item.needs_item_json() for item in items],
        "scanned_at": _iso_seconds(now),
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
    "RemoteNeedsWatcher",
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
    "needs_single_agent_now",
    "needs_tool_pending",
    "needs_ws_frames",
    "record_needs_dismissal",
    "scan_needs_you",
    "start_needs_watch",
]
