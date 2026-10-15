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

Every item has an id derived from what it is about (the pending ``tool_use``
whose prompt is up, with its notification when a sub-agent asks, the marker
record of an interruption, the seq of a board event), so it
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
import time
import unicodedata
from collections.abc import Callable, Collection, Mapping, MutableMapping, MutableSet, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from aisquare.models import (
    CLOSED_STATUSES,
    FleetAgent,
    FleetAgentStatus,
    ProjectInfo,
    TeamEvent,
    TeamSession,
)
from aisquare.services.transcript import (
    _TAIL_NOTHING,
    PendingTool,
    TranscriptTail,
    read_transcript_tail,
)

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.routing import BaseRoute

    from aisquare.core.config import AccountsSettings
    from aisquare.core.tmux import TmuxServer
    from aisquare.services.fleet import PaneWrite
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
    "failed",
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

_INPUT_NOTICE = re.compile(r"needs your input", re.IGNORECASE)
"""An attention notification that asks for input in a form, not for a permission ("Claude
needs your permission to use Bash"): an MCP server's elicitation ("Claude Code needs your
input", "An MCP server needs your input") or an agent's ("<label> needs your input"), in
Claude Code 2.1.296. A digit typed there goes into a field. Read only from the turn's first
notice, the one the board keeps in words (:func:`_needs_notice`). A Claude Code string, not
a contract."""

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

_NEEDS_DAY_KINDS = (*_NEEDS_ASKED, "agent_exited", "turn_failed")
"""The kinds of its agents' events the scan reads of a project's day (``board_since``): what
opens a board item or moves on from one, the exits the fleet announces, and the turns that
died on an API error."""

_NEEDS_ACTIONS: dict[str, tuple[str, ...]] = {
    "permission": ("answer", "open", "dismiss"),
    "question": ("answer", "open", "dismiss"),
    "plan": ("answer", "open", "dismiss"),
    "asked": ("tell", "open", "dismiss"),
    "interrupted": ("tell", "open", "dismiss"),
    "board_question": ("reply", "dismiss"),
    "board_result": ("reply", "dismiss"),
    "limited": ("switch", "open", "dismiss"),
    "failed": ("tell", "switch", "open", "dismiss"),
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

_NEEDS_STARTING = _NEEDS_PUSH_DELAY["manager_down"]
"""How long a live row's board session may take to register: its start hook comes seconds
after the row (``fleet.spawn``), more with MCP servers to start or a busy machine. The minute
a restart or a switch flashes ``manager_down`` for (review of #243, round 26, 1/2)."""

_LIMITED_PUSH_DELAY = timedelta(seconds=90)

_TOOL_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,40}")
"""A tool name a reason may carry; anything else is left out of the sentence."""

_FAILURE_KIND = re.compile(r"[A-Za-z0-9_.-]{1,40}")
"""The error a failed turn's reason may name (``authentication_failed``, ``billing_error``,
``server_error``): Claude Code's word for it, or nothing."""

_SUBAGENT_TOOLS = frozenset({"Task", "Agent"})
"""The tools a sub-agent runs inside: a prompt pending under one is the sub-agent's."""

_NOTICE_WAIT = timedelta(seconds=20)
"""How long after its pane printed a sub-agent's prompt may still be waiting for the
notification that names it (:func:`_needs_drawn_since_notice`). Claude Code sends it 6 s
after the prompt is drawn; the hook's process starts in a second or so, and may wait out the
store's busy timeout (5 s) for its write; tmux tells the time of output to the second,
rounded down. That is about 13 s at worst. A notification later than this leaves the
prompt named after the one before until it lands, and a redraw (an attach, a resize, a
key that moves the highlight) hides the card this long."""

_PROMPT_MARK_MIN = 6
_PROMPT_MARK_MAX = 40
"""A call's mark (:func:`_needs_prompt_mark`) is the first ``_PROMPT_MARK_MAX`` characters
of the text its prompt shows, squashed (:func:`_needs_squashed`): a long command is cut or
wrapped on screen. One under ``_PROMPT_MARK_MIN`` (``ls``, ``a.py``) is found in too much
text to name its call by being found (:func:`_needs_on_screen`)."""

_PROMPT_OPTION_ONE = re.compile(r"[\s│┃║|]*(?:[\u276f\u203a>▶]\s*)?1\.\s")
"""A dialog's first option, ``1. Yes``, after the pointer that highlights it or not, in a
box or not."""
_PROMPT_RULE = re.compile(r"\s*[╭╰┌└├]?[─━═]{8,}")
"""A row drawn as a rule: a dialog's top edge or a box's, or the input box's two edges."""
_PROMPT_UNSPACED = re.compile(r"[\s─-╿…`*]+")
"""What :func:`_needs_squashed` drops: blanks, wraps, box edges, an ellipsis and the marks a
terminal draws as style."""

_NEEDS_ASKING_TOOLS = frozenset({"AskUserQuestion", "ExitPlanMode"})
"""The tools whose card needs no notice: their dialog is the call itself."""

_PROMPT_FILE_TOOLS = frozenset({"Edit", "MultiEdit", "Write", "NotebookEdit"})
"""The tools whose prompt names the file by its base name, in the line above its options
("Do you want to make this edit to cache.py?"), however tall its diff."""

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
"""The keys of a pending tool's input a permission card shows first: what the tool would DO.
The rest of the call follows them (:func:`_needs_tool_fields`)."""

_DETAIL_FIELDS_MAX = 20
"""The most fields of a pending tool's input a permission card shows: as many as the page
draws. The rest are counted in the detail's ``omitted``."""
_DETAIL_FIELD_NAME = re.compile(r"[A-Za-z0-9_.:@$-]{1,64}\Z")
"""A field name a permission card shows. Field names are not cut to fit as values are, so
one outside this (a name of 300 characters, control characters that JSON spells in six)
could hold a detail over its cap; it is counted in ``omitted`` instead."""

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
_DISMISSALS_REDATED = timedelta(days=1)
"""How old a dismissal a scan still needs gets before the scan dates it again: one write a
day for each card that stays hidden, and a week to spare."""
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
    sighted: bool = False
    """Whether its id names the scan that first saw it too (:func:`_needs_dated`), so each time
    it is drawn is a card of its own. Internal: never serialized."""

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


def _needs_answers_every(socket: str, path: Path | None) -> bool:
    """A source's ``tmux_answers`` when it says nothing: every server answered."""
    return True


def _needs_hands_over_anywhere(leaving: int | None) -> bool:
    """A source's ``hands_over`` when it says nothing: every agent has somewhere to go."""
    return True


def _needs_no_event(
    project_id: str, session_id: str, kind: str, since: datetime
) -> TeamEvent | None:
    """A source's ``session_event`` when it says nothing: the window is all there is."""
    return None


def _needs_no_output(status: FleetAgentStatus) -> datetime | None:
    """A source's ``pane_output`` when it says nothing: tmux would not say."""
    return None


def _needs_no_screen(status: FleetAgentStatus) -> list[str] | None:
    """A source's ``pane_screen`` when it says nothing: no screen, so no prompt read on it."""
    return None


def _needs_own_board(project: ProjectInfo) -> str:
    """A source's ``board_project`` when it says nothing: each project writes its own board."""
    return project.id


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
    the human's notes, decisions and results (:data:`_NEEDS_REPLIED`); the exits the
    fleet announces (``agent_exited``), which say how a row ended; and the turns that
    ended on an API error (``turn_failed``). An item lives for :data:`QUESTION_HORIZON`
    however busy the board is, so it is read by time, not from a window of the newest
    events that 300 notes push it out of."""
    exits_since: Callable[[str, datetime], list[TeamEvent]]
    """The project's own ``agent_exited`` events written at or after the given time: the
    fleet announces a row's exit on its row's project (``fleet._emit_exit``), so a project
    whose agents write another board finds none in that board's day."""
    board_sessions: Callable[[str, Collection[str]], list[TeamSession]]
    """The project's sessions with the given ids: the authors of the board's open questions."""
    task_status: Callable[[str], str | None]
    """A task's status; ``None`` when it is gone."""
    transcript_tail: Callable[[str], TranscriptTail | None]
    accounts: Callable[[], AccountsSettings]
    """``[accounts]`` as configured: ``on_limit`` and ``wait_if_reset_within_minutes``."""
    has_live_agents: Callable[[str], bool] = _needs_lists_every
    """Whether the project has a row that has not ended. The scan lists only a project that
    has: ``fleet.list_agents`` reads every row and session the project ever had, and with
    no live row there is no pane to ask tmux about and no death to record."""
    tmux_answers: Callable[[str, Path | None], bool] = _needs_answers_every
    """Whether a tmux server listens on the given socket (``TmuxServer.answers``), asked at
    the given path (``tmux -S``) when there is one, as the listing asks a row's server
    (``FleetAgentStatus.socket_at``): asked only where the listing's states cannot tell
    (:func:`_needs_unheard`)."""
    session_event: Callable[[str, str, str, datetime], TeamEvent | None] = _needs_no_event
    """A session's newest board event of a kind written at or after the given time, for the
    facts the window of ``board_events`` no longer holds (:func:`_needs_own_events`). The
    time bounds what the store walks back through to find none."""
    pane_output: Callable[[FleetAgentStatus], datetime | None] = _needs_no_output
    """When the row's pane last printed (``#{window_activity}``); ``None``: tmux would not
    say. Asked only of an agent whose sub-agent waits on a prompt, or whose question or plan
    waits beside a call the screen cannot name (:func:`needs_from_agent`)."""
    pane_screen: Callable[[FleetAgentStatus], list[str] | None] = _needs_no_screen
    """The rows the row's pane shows now (``capture-pane``); ``None``: a source with no screen
    to show. One tmux could not show raises, a failed read of that agent's alone
    (:func:`_needs_screen_read`).
    Asked only of an agent with more than one call pending, whose prompt on screen may be
    any of them (:func:`_needs_on_screen`)."""
    hands_over: Callable[[int | None], bool] = _needs_hands_over_anywhere
    """Whether ``on_limit = "switch"`` hands an agent leaving the given slot over: an
    account to go to (``claude_accounts.somewhere_to_hand_over``), asked with the slot the
    StopFailure hook asks with (``claude_accounts.leaving_slot``). Asked only of a limited
    row that no reset or live manager settles first (:func:`_needs_limited_push`)."""
    board_project: Callable[[ProjectInfo], str] = _needs_own_board
    """The id of the project whose board a project's agents write: the project's own, a
    worktree's principal checkout's, or under ``AISQUARE_TEAM_HUB`` the hub's, as ``asq board``
    run in its root resolves it (``team.resolve_project``). Its sessions and events are
    read there; its rows, by its own id."""


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


def _needs_said(value: object) -> int:
    """How many characters the strings inside ``value`` hold, all told."""
    return sum(len(container[key]) for container, key in _needs_strings(value))


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

    Only prose is read (:func:`_needs_prose`): code ends in ``?`` all the time
    (Swift's ``String?``, SQL's ``WHERE id = ?``, Ruby's ``admin?``, a lazy
    ``(.*?)``), and a closing summary that showed some was an ``asked`` card,
    pushed again every turn. Each line is read without its markdown (``*_`>#``)
    and trailing quotes, brackets, spaces and emoji (a check or cross mark is a
    checklist's result, and stays), nor an aside in parentheses or brackets after
    its question (:func:`_needs_line_asks`). The text asks when a line ending in a
    question mark (``?``, or a script's own) lies in its last paragraph (after its
    last blank line outside a code block), or among its last 12 non-empty lines
    and within its last 600 characters. So "Which approach? 1. … 2. …" asks, and
    so does a coder's closing "Want me to commit this?" — which the push policy,
    not this test, keeps from crying wolf.
    """
    lines = _needs_lines(text)
    body = "\n".join(lines)
    prose = _needs_prose(lines)
    blank = max(
        (index for index, line in enumerate(prose) if line is not None and not line.strip()),
        default=-1,
    )
    if any(line is not None and _needs_line_asks(line) for line in prose[blank + 1 :]):
        return True
    ends: list[int] = []
    position = 0
    for line in lines:
        position += len(line)
        ends.append(position)
        position += 1
    window = len(body) - 600
    counted = 0
    for line, read, end in zip(reversed(lines), reversed(prose), reversed(ends), strict=True):
        if not line.strip():
            continue
        counted += 1
        if counted > 12 or end < window:
            return False
        if read is not None and _needs_line_asks(read):
            return True
    return False


_NEEDS_TRAILING = " \t*_`>#\"'\u201d\u2019\u00bb)]}"
"""What a line may end with after its question mark: markdown, closing quotes and brackets."""

_NEEDS_QUESTION_MARKS = frozenset("?\uff1f\u061f\ufe56\u2047\u2048\u2049\u203d")
"""What ends a question: ``?``, the fullwidth one of Chinese and Japanese (U+FF1F), the one of
Arabic, Persian and Urdu (U+061F), and their variants. Claude answers in the human's language."""

_NEEDS_SYMBOLS = frozenset({"So", "Sk", "Cf", "Mn", "Me"})
"""Unicode categories a line may also end with after its question: an emoji, and its skin
tone, variation selector, joiner or keycap."""

_NEEDS_TICKS = frozenset("\u2705\u2611\u2612\u2713\u2714\u2716\u2717\u2718\u274c\u274e")
"""Check and cross marks, symbols that do not close a question: "Tests pass? ✅" in a closing
checklist reports a result."""

_NEEDS_FENCE = re.compile(r"[ \t>]*(`{3,}|~{3,})")
"""A line that opens or closes a fenced code block: three backticks or tildes or more, after
an indent or a blockquote's marks."""

_NEEDS_TICK_RUN = re.compile(r"`+")
"""A run of backticks, which opens or closes an inline code span."""

_NEEDS_LIST_ITEM = re.compile(r"( *)([-*+]|\d{1,9}[.)])( +|$)")
"""A list item's first line: its marker, after an indent, and the spaces to its content."""


def _needs_lines(text: str) -> list[str]:
    """``text``'s lines, the blank ones around it dropped and its first line's indent kept:
    an indented code block may open the text."""
    lines = text.rstrip().splitlines()
    while lines and not lines[0].strip():
        del lines[0]
    return lines


def _needs_prose(lines: Sequence[str]) -> list[str | None]:
    """Each line as the question test reads it: ``None`` inside a code block, fenced or
    indented, a fence's own lines included, and otherwise the line without its inline code
    spans.

    A fence closes on a line of the same character, at least as long, with nothing
    after it; one that never closes runs to the end, as markdown reads it. An indented
    block is markdown's too: lines four columns deeper than the list item they sit in
    (none: the margin), opened by the first line, or after a blank line or a fence's
    close, since one cannot interrupt a paragraph, and run until a line less deep. So
    ``    WHERE id = ?`` after a blank line is code, while a list item's own continuation
    paragraph, indented to its content, is prose and still asks. Without its spans,
    "Should I run `make check`?" still asks, and "now `String?`" does not.
    """
    prose: list[str | None] = []
    fence: str | None = None
    items: list[int] = []  # the content columns of the list items this line may sit in
    opens = True  # this line starts a block, as the first does, and one after a blank line
    indented = False
    for line in lines:
        marks = _NEEDS_FENCE.match(line)
        rest = "" if marks is None else line[marks.end() :]
        if fence is not None:
            prose.append(None)
            if (
                marks is not None
                and marks.group(1)[0] == fence[0]
                and len(marks.group(1)) >= len(fence)
                and not rest.strip()
            ):
                fence = None
                opens = True
            continue
        flat = line.expandtabs(4)
        if not flat.strip():
            prose.append("")
            opens = True
            continue
        depth = len(flat) - len(flat.lstrip(" "))
        if opens:  # otherwise a shallower line continues the item's paragraph
            while items and depth < items[-1]:
                items.pop()
        indented = depth >= (items[-1] if items else 0) + 4 and (opens or indented)
        opens = False
        if indented:
            prose.append(None)
        elif marks is not None and not (marks.group(1)[0] == "`" and "`" in rest):
            fence = marks.group(1)
            prose.append(None)
        else:
            item = _NEEDS_LIST_ITEM.match(flat)
            if item is not None:
                while items and depth < items[-1]:
                    items.pop()
                gap = len(item.group(3))
                items.append(item.end(2) + (gap if 1 <= gap <= 4 else 1))
            prose.append(_needs_without_spans(line))
    return prose


def _needs_without_spans(line: str) -> str:
    """``line`` without its inline code spans: each a run of backticks, to the next run of as
    many.

    Each run is found once and paired with the next of its length. A pattern that tried
    each run against the rest of the line took over a second on one long line of runs of
    different lengths, an unclosed one each (review of #243, round 5).
    """
    runs = [(found.start(), found.end()) for found in _NEEDS_TICK_RUN.finditer(line)]
    closes: dict[int, int] = {}  # a run → the next run of its length
    nearest: dict[int, int] = {}  # a length → the run of it nearest after this one
    for index in range(len(runs) - 1, -1, -1):
        size = runs[index][1] - runs[index][0]
        if size in nearest:
            closes[index] = nearest[size]
        nearest[size] = index
    kept: list[str] = []
    copied = 0
    index = 0
    while index < len(runs):
        close = closes.get(index)
        if close is None:
            index += 1
            continue
        kept.append(line[copied : runs[index][0]])
        copied = runs[close][1]
        index = close + 1
    kept.append(line[copied:])
    return "".join(kept)


def _needs_line_asks(line: str) -> bool:
    """Whether a prose line ends on a question: its mark, then only what may close a line
    (:data:`_NEEDS_TRAILING`, a symbol such as an emoji), or asides after it.

    ``?`` alone, then quotes and brackets alone, missed "Merge it? (y/n)", "Shall I deploy
    to staging? 🚀", and every question asked in Chinese, Japanese or Arabic: no card and
    no push for an agent waiting on its human's answer (review of #243, sweep 3).

    One pass from the end, each aside read once: searching the line for its last aside
    again after each one took seconds to minutes on a long line of links or ``(a)``s, run
    every scan and every quarter second of an interrupt (review of #243, round 5).
    """
    end = len(line)
    while True:
        mark = end
        while mark and _needs_closes(line[mark - 1]):
            mark -= 1
        if mark and line[mark - 1] in _NEEDS_QUESTION_MARKS:
            return True
        end = _needs_aside_start(line, end)
        if not end:
            return False


def _needs_closes(char: str) -> bool:
    """Whether ``char`` may follow a question's mark at the end of its line."""
    return char in _NEEDS_TRAILING or (
        char not in _NEEDS_TICKS and unicodedata.category(char) in _NEEDS_SYMBOLS
    )


def _needs_aside_start(line: str, end: int) -> int:
    """Where an aside that ends ``line[:end]`` starts, the blanks before it included: ``(…)``
    or ``[…]`` with none of its own kind inside, then nothing but what is neither a word nor a
    bracket (``Merge it? (y/n)``, ``Proceed? [y/N].``); 0 when there is none, or it is all the
    line holds."""
    close = end
    while close and line[close - 1] not in "()[]" and not _needs_wordy(line[close - 1]):
        close -= 1
    if not close or line[close - 1] not in ")]":
        return 0
    pair = "()" if line[close - 1] == ")" else "[]"
    start = close - 1
    while start and line[start - 1] not in pair:
        start -= 1
    if not start or line[start - 1] != pair[0]:
        return 0
    start -= 1
    while start and line[start - 1] in " \t":
        start -= 1
    return start


def _needs_wordy(char: str) -> bool:
    """Whether ``char`` is a word's, as a regular expression's ``\\w`` reads it."""
    return char.isalnum() or char == "_"


def _needs_asked_tail(text: str) -> str:
    """The question an assistant ended on: its last prose line that asks, to the end."""
    lines = _needs_lines(text)
    prose = _needs_prose(lines)
    for index in range(len(lines) - 1, -1, -1):
        read = prose[index]
        if read is not None and _needs_line_asks(read):
            return "\n".join(lines[index:]).lstrip()
    return text


# --- items --------------------------------------------------------------------------------


def needs_item_id(project_id: str, kind: str, subject: str) -> str:
    """``ny_`` and 16 hex digits of what the item is about: the same subject, the same id."""
    digest = hashlib.sha256(f"{project_id}|{kind}|{subject}".encode()).hexdigest()
    return f"ny_{digest[:16]}"


def _needs_now() -> datetime:
    return datetime.now(UTC)


def _needs_project_name(project: ProjectInfo) -> str:
    """How a card, its push and an action's refusal name ``project``: as Remote's project
    list does (``remote_server.remote_project_name``), its nickname first."""
    from aisquare.services.remote_server import remote_project_name

    return remote_project_name(project)


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
    sighted: bool = False,
) -> NeedsItem:
    return NeedsItem(
        id=needs_item_id(project.id, kind, subject),
        kind=kind,
        project_id=project.id,
        project_name=_needs_project_name(project),
        agent=None if agent is None else agent.label,
        agent_id=None if agent is None else agent.id,
        reason=reason,
        excerpt=_needs_excerpt(excerpt),
        detail=detail,
        answers=answers,
        since=since,
        actions=_NEEDS_ACTIONS[kind],
        push_after=push_after,
        sighted=sighted,
    )


_TRUST_PUSH_DELAY = timedelta(seconds=30)
"""How long after its start an agent at the folder-trust question waits for a push: the
human who spawned it from the UI is often at its window and answers it first."""


def _needs_trust_item(
    status: FleetAgentStatus, *, project: ProjectInfo, name: str
) -> NeedsItem | None:
    """The card of an agent at Claude Code's folder-trust question (``fleet.TRUST_PROMPT``).

    One per row, since the question is asked once per start. No quick answer: the dialog's
    highlight decides what Enter picks, and a key sent blind could pick "No, exit". The
    card says how to unblock it (its excerpt, the row's detail); the push says only who.
    """
    from aisquare.services.fleet import TRUST_PROMPT

    detail = status.detail or ""
    if status.state != "attention" or not detail.startswith(TRUST_PROMPT):
        return None
    agent = status.agent
    return _needs_item(
        "permission",
        f"trust:{agent.id}",
        project=project,
        agent=agent,
        reason=f"{name} is waiting for folder trust (Claude Code asks before it starts)",
        excerpt=detail,
        detail={"text": detail},
        since=agent.created_at,
        push_after=agent.created_at + _TRUST_PUSH_DELAY,
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
    hands_over: Callable[[int | None], bool] | None = None,
    pane_output: Callable[[], datetime | None] | None = None,
    pane_screen: Callable[[], Sequence[str] | None] | None = None,
) -> list[NeedsItem]:
    """What one agent needs from the human: at most one item, by the first rule that holds.

    Rules 3 to 5 are about the call whose prompt is up (:func:`_needs_prompt_call`): the one
    pending call, or with more than one, the call the prompt on the pane's screen names
    (:func:`_needs_on_screen`), never one taken by its place among them. With more than one
    the screen may show any of them: a call answered writes nothing until it ends, a call
    Claude Code lets run may run beside the next one's prompt, and a question after a
    command is not drawn until the command's prompt is answered. The first
    ``AskUserQuestion`` anywhere was the card while a ``git push --force`` beside it was
    prompting, and its "1" approved the push (review of #259, round 8, 1/9). A screen that
    names none of them, or more than one, is rule 8's dialog card, whose answers are the
    key pad's beside the live pane.

    In order (SPEC §4.2):

    1. derived ``lost``, its pane gone → ``lost``; and a row held as another tmux server's
       (``FleetAgentStatus.held``), whatever its board says: its agent is one nothing here
       reaches, so it is a ``lost`` card of its own reason, never a prompt (below);
    2. derived ``limited`` → ``limited``; derived ``attention`` at Claude Code's folder-trust
       question (``fleet.TRUST_PROMPT``) → ``permission``, its own card: the agent has no
       session, so nothing below can read it, and rule 8's id would move with every scan;
    3. the call up an ``AskUserQuestion`` → ``question``;
    4. the call up an ``ExitPlanMode`` → ``plan``;
    5. a pending tool, with attention → ``permission``, about the call up: each prompt has
       its own tool use, so the 2nd prompt of a turn is a new item. Not under a ``Task``,
       whose sub-agent's tool uses are in its own records: every prompt of the
       sub-agent's has that one pending tool, so the prompt is also its notice, the
       ``last_seen_at`` each notification moves (:func:`_needs_subagent_prompt`). Nor
       while the notice asks for input (:data:`_INPUT_NOTICE`): an MCP server's
       elicitation comes while its tool call is pending, and that form is rule 8's. Nor,
       of one call pending, after a notice later than the turn's first with nothing written
       since it (:func:`_needs_later_notice`): the call's own prompt was answered, and what
       is up now is rule 8's too. Of more than one, the screen named the call, and a later
       notice is its prompt's own, which lands seconds after the prompt is drawn: the card
       was the call's until then and rule 8's under another id after, one prompt pushed
       twice (review of #259, round 9, 4/8). Once the agent wrote after the turn's first
       notice, a later one's words are not on the board, and nothing tells a form asked
       then from its tool's own prompt: it reads as the prompt, whose digits would go into
       the form. Nor with no call up: more than one pending, and the screen names none,
       which is rule 8's, and with a question or a plan among them is rule 8's before any
       notice too, once a dialog is drawn (:func:`_needs_drawn_dialog`), named after the
       question or the plan and what the dialog shows, before its notice and after alike;
    6. no pending tool, and the newest record an interruption later than the session's last
       hook, or one only Claude Code's own records follow (a local or ``!`` command run at
       the prompt since, which fires no hook either) → ``interrupted``, whatever the row
       reads (Esc fires no Stop, so a dismissed prompt still reads ``attention`` and an
       interrupted turn ``working``);
    7. attention, and its notification is the usage-limit dialog → ``limited``;
    8. attention → ``permission``, the dialog form: an MCP elicitation, Claude Code's own;
    9. ``waiting`` on its own words, which end on a question → ``asked``, a local or ``!``
       command run at its prompt since, which runs no turn, changing nothing (as for 6);
    10. ``waiting`` since its turn ended on an API error (the session's ``turn_failed``
        event, no hook since) → ``failed``.

    Rules 5, 7 and 8 read the notification from the session's newest ``attention`` event
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
    printed, asked only of a sub-agent's prompt; ``pane_screen`` is what it shows, asked
    only with more than one call pending.

    A held row got rules 3 to 10 from its board while that was fresh: permission and
    question cards, pushed, whose every answer was a 409 ``not_agent`` (its pane id is
    another agent's on the server that answers at its socket), and then, the board stale,
    nothing at all, as an ``unknown`` row needs nothing and ``fleet_down`` leaves held rows
    out. Before 0.8.1 the same row read ``lost`` and had its card (review of #259, round 2).
    """
    agent, session = status.agent, status.session
    name = needs_push_safe(agent.label)
    if status.held and agent.ended_at is None:
        return [
            _needs_item(
                "lost",
                agent.id,
                project=project,
                agent=agent,
                reason=f"{name} is out of reach: another tmux server answers on its socket",
                excerpt=None,
                detail={},
                since=now,
                push_after=now + _NEEDS_PUSH_DELAY["lost"],
            )
        ]
    if status.state in ("exited", "unknown"):
        return []
    own = [e for e in events if session is not None and e.session_id == session.id]
    attention_event = max((e for e in own if e.kind == "attention"), key=_needs_seq, default=None)
    limited_event = max((e for e in own if e.kind == "limited"), key=_needs_seq, default=None)
    failed_event = max((e for e in own if e.kind == "turn_failed"), key=_needs_seq, default=None)
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
                hands_over=hands_over,
            )
        ]
    trust = _needs_trust_item(status, project=project, name=name)
    if trust is not None:
        return [trust]
    pending = _needs_pending(tail, agent)
    attention = _needs_attention(status)
    asking = next((tool for tool in pending if tool.name in _NEEDS_ASKING_TOOLS), None)
    screen = _needs_screen_once(pane_screen)
    # The screen is read only where a card could come of it: not for calls at work, unasked.
    call = _needs_prompt_call(pending, screen) if attention or asking is not None else None
    if call is not None and call.name == "AskUserQuestion":
        return [_needs_question_item(call, project=project, agent=agent, name=name, now=now)]
    if call is not None and call.name == "ExitPlanMode":
        return [_needs_plan_item(call, project=project, agent=agent, name=name, now=now)]
    # Rule 8's card beside a question or a plan no screen names is named after it, not after
    # the notices around it: one card before its notice and after (round 10, 2/9).
    asked = asking if call is None else None
    drawn = None if asked is None else _needs_drawn_dialog(screen)
    if pending:
        if not attention:
            if drawn is not None:
                return [
                    _needs_dialog_item(
                        status,
                        attention_event,
                        None,
                        project=project,
                        now=now,
                        asked=asked,
                        drawn=drawn,
                    )
                ]
            return []  # a tool running, or the 6 s before Claude Code's notification
        notice = _needs_notice(attention_event, tail, session)
        if notice is not None:
            form = _INPUT_NOTICE.search(notice.text) is not None
        else:  # a sub-agent's prompts each send a notice, its records not this transcript's
            # Of more than one call, the screen named this one's prompt, and a later notice is
            # that prompt's own landing after its card: the card stays its (round 9, 4/8).
            form = (
                len(pending) == 1
                and call is not None
                and call.name not in _SUBAGENT_TOOLS
                and _needs_later_notice(attention_event, tail, session)
            )
        if not form and call is not None:
            if call.name in _SUBAGENT_TOOLS and session is not None:
                return _needs_subagent_prompt(
                    call, session, pane_output, project=project, agent=agent, now=now
                )
            return [_needs_permission_item(call, project=project, agent=agent, name=name, now=now)]
    elif tail is not None and _needs_marker_later(status, tail):
        return [_needs_interrupted_item(tail, project=project, agent=agent, name=name, now=now)]
    if attention or _needs_unanswered(status, tail, unread=False):
        notice = _needs_notice(attention_event, tail, session)
        if notice is not None and LIMIT_DIALOG.search(notice.text):
            # Pushed at once, whatever ``on_limit`` says and whether a manager is live: the
            # dialog is a notification, which starts no hand-over (a turn that failed on its
            # limit does, ``hooks.turn_failed``) and wakes no manager (``attention`` is a
            # human board kind), so the 90 s :func:`_needs_limited_push` waits for someone
            # else to act had nobody behind them (review of #243, sweep 5).
            return [
                _needs_item(
                    "limited",
                    f"attention:{notice.seq}",
                    project=project,
                    agent=agent,
                    reason=f"{name} hit its usage limit (Claude Code is asking what to do)",
                    excerpt=notice.text,
                    detail=_needs_fit({"text": notice.text}, _DETAIL_TEXT_MAX),
                    since=notice.created_at,
                    push_after=notice.created_at,
                )
            ]
        return [
            _needs_dialog_item(
                status, attention_event, notice, project=project, now=now, asked=asked, drawn=drawn
            )
        ]
    said, said_at, said_key = ("none", None, None) if tail is None else _needs_settled(tail)
    if (
        status.state == "waiting"
        and tail is not None
        and said == "assistant_text"
        and tail.last_text
        and (said_at is None or said_at >= agent.created_at)
        and looks_like_a_question(tail.last_text)
    ):
        since = said_at or now
        return [
            _needs_item(
                "asked",
                said_key or since.isoformat(),
                project=project,
                agent=agent,
                reason=f"{name} ended its turn with a question",
                excerpt=_needs_asked_tail(tail.last_text),
                detail=_needs_fit({"text": tail.last_text}, _DETAIL_TEXT_MAX),
                since=since,
                # At once, a live manager or not: a turn's end wakes no manager
                # (``team.hook_stop``), so none would answer it first (review of #243,
                # round 22, 6/7).
                push_after=since,
            )
        ]
    if (
        status.state == "waiting"
        and session is not None
        and session.state == "waiting"
        and failed_event is not None
        and failed_event.created_at >= max(agent.created_at, session.last_seen_at)
    ):
        return [_needs_failed_item(failed_event, project=project, agent=agent, name=name)]
    return []


def _needs_dialog_item(
    status: FleetAgentStatus,
    attention_event: TeamEvent | None,
    notice: TeamEvent | None,
    *,
    project: ProjectInfo,
    now: datetime,
    asked: PendingTool | None = None,
    drawn: str | None = None,
) -> NeedsItem:
    """Rule 8's card: a dialog that needs the human, named by the session's newest
    ``attention`` event and the notice ``last_seen_at`` says came last, its words ``notice``'s
    while they still name it. No quick answers: the key pad beside the live pane answers what
    it shows.

    Beside a question or a plan pending with a call the screen cannot name (``asked``), it is
    named after that question or plan, and what the dialog drawn shows above its options
    (``drawn``, :func:`_needs_prompt_region`). Named by the notices, the card made before any
    (:func:`_needs_drawn_dialog`) had the turn before's, and a notice that came later, as
    Claude Code's idle one does after a minute, named the same dialog anew: pushed twice, the
    first push's link a card that was gone (review of #259, round 10, 2/9). Named after the
    question alone, every dialog drawn while it waited was that one card: a sub-agent's
    prompts after its first were never pushed (a pushed id is kept a week), and one
    dismissal hid them all, the question's own too (round 11, 1/13). What the dialog shows
    leaves out its options, so a highlight moved or a notice landing keeps the card, and
    the next dialog is a card of its own. Its key pad answers whatever is up. The same
    dialog drawn again (the same command asked for twice) showed the same, and was the same
    card: never pushed, hidden by the first one's dismissal. So its id names the scan that
    first saw it since a scan last did not (``sighted``, :func:`_needs_dated`), as
    ``_needs_subagent_prompt`` tells a sub-agent's prompts apart by their notices (review of
    #259, round 12, 5/9).
    """
    session, agent = status.session, status.agent
    seen = session.last_seen_at if session is not None else now
    if asked is not None and drawn is not None:
        # Drawn when no fact says: as of now, dated by the first scan that saw it (round 11,
        # 8/13). It was the question's asking, and a sub-agent's prompt drawn 25 minutes
        # after it was a card 25 minutes old.
        subject, since = f"dialog:{asked.tool_use_id}:{drawn}", now
    else:
        seq = "-" if attention_event is None else str(attention_event.seq)
        subject, since = f"attention:{seq}:{seen.isoformat()}", seen
    text = "" if notice is None else notice.text
    return _needs_item(
        "permission",
        subject,
        project=project,
        agent=agent,
        reason=f"{needs_push_safe(agent.label)} shows a dialog that needs you",
        excerpt=text,
        detail=_needs_fit({"text": text}, _DETAIL_TEXT_MAX),
        since=since,
        push_after=since,
        sighted=asked is not None and drawn is not None,
    )


def _needs_seq(event: TeamEvent) -> int:
    return event.seq


def _needs_notice(
    event: TeamEvent | None, tail: TranscriptTail | None, session: TeamSession | None
) -> TeamEvent | None:
    """The session's newest ``attention`` event, while it still names the dialog on screen.

    ``mark_attention`` flips a session once per turn, so a turn's first notice is
    the only one the board records: a later one moves ``last_seen_at`` and writes
    nothing. Once the agent wrote anything after the event (a granted tool's
    result, its reply after the dialog was answered), or a notice came after it,
    the dialog the event named was answered, and one on screen now is another,
    which its text would misname: the usage-limit dialog read as the Bash prompt
    approved before it, and the reverse, an MCP server's form as its tool's
    permission prompt granted just before it. Without a tail nothing says the
    agent moved on.
    """
    if event is None or _needs_moved_on(event, tail):
        return None
    if session is not None and session.last_seen_at > event.created_at:
        return None
    return event


def _needs_moved_on(event: TeamEvent, tail: TranscriptTail | None) -> bool:
    """The agent wrote a record of its conversation after ``event``."""
    return tail is not None and tail.newest_at is not None and tail.newest_at > event.created_at


def _needs_later_notice(
    event: TeamEvent | None, tail: TranscriptTail | None, session: TeamSession | None
) -> bool:
    """A notice came after the turn's first, ``event``, with nothing written between.

    ``mark_attention`` writes the event after it moves ``last_seen_at``, so the
    notice the event records leaves ``last_seen_at`` before it, and only a later
    one moves it past. A pending tool's prompt is answered with no record written
    (its result comes when it ends), so the dialog that notice is for is not the
    one the event named: an MCP server's form, asked during the call its
    permission prompt let run, or the prompt of a tool called beside it in the
    same message. Neither is that tool's own prompt.
    """
    if event is None or session is None or _needs_moved_on(event, tail):
        return False
    return session.last_seen_at > event.created_at


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


def _needs_settled(tail: TranscriptTail) -> tuple[str, datetime | None, str | None]:
    """Where the agent is by its transcript, as a record's kind, time and key: the newest,
    or, under Claude Code's own records (``own``), the one they follow.

    A local command or a ``!`` command typed at the agent's prompt runs no turn and fires
    no hook. Taken for where the agent is, it hid an interruption: the session, still
    ``attention`` from the prompt an Esc answered, read as a dialog up, a card pushed at
    once, and every action refused ``dialog_open`` (sweep 5 of #243).
    """
    if tail.newest == "own" and tail.under_own != "none":
        return tail.under_own, tail.under_own_at, tail.under_own_key
    return tail.newest, tail.newest_at, tail.marker_key


def _needs_marker_later(status: FleetAgentStatus, tail: TranscriptTail) -> bool:
    """The agent sits at an interruption this process made after its session's last hook
    (:func:`_needs_settled`)."""
    kind, at, _key = _needs_settled(tail)
    if kind != "interrupted" or at is None or at < status.agent.created_at:
        return False
    return status.session is None or at > status.session.last_seen_at


def _needs_pending(tail: TranscriptTail | None, agent: FleetAgent) -> tuple[PendingTool, ...]:
    """The tail's pending tools, less any older than the row (a resumed session's leftovers)."""
    if tail is None:
        return ()
    return tuple(tool for tool in tail.pending if tool.at is None or tool.at >= agent.created_at)


def _needs_cut_short(tail: TranscriptTail | None, agent: FleetAgent) -> bool:
    """The tail's walk ran out before the newest message, in a file written since the row
    began: a tool use of it may be pending unseen (``TranscriptTail.cut_at``). What a
    stop, a tell or a guarded key reads as a pending tool; no card is made of it, as it
    names no tool to show."""
    return tail is not None and tail.cut_at is not None and tail.cut_at >= agent.created_at


def _needs_limited_item(
    status: FleetAgentStatus,
    event: TeamEvent | None,
    *,
    project: ProjectInfo,
    name: str,
    now: datetime,
    manager_live: bool,
    accounts: AccountsSettings | None,
    hands_over: Callable[[int | None], bool] | None = None,
) -> NeedsItem:
    """A row parked on a usage limit: the subject is its newest ``limited`` event.

    When the limit lifts is said as a distance, and its instant goes in ``detail``
    as ``resets_at``, for the page to tell by the phone's clock. The machine's
    clock time (``format_reset``'s ``(13:10)``) read as the phone's own on a card,
    a lock screen and the Fleet tab: 06:10 there, for a phone in UTC-7 (review of
    #243, sweep of round 4). The board's line keeps it for the machine's terminal;
    the card shows that line without its reset, as of when the hook ran.
    """
    from aisquare.core import claude_accounts as claude_accounts_core
    from aisquare.services import claude_accounts as claude_accounts_service
    from aisquare.services.remote_server import _iso_seconds

    agent, session = status.agent, status.session
    resets = session.limit_resets_at if session is not None else None
    if event is not None and event.kind == "limited":
        subject, since = str(event.seq), event.created_at
    else:
        subject = f"{agent.id}:{resets.isoformat() if resets is not None else '-'}"
        since = session.last_seen_at if session is not None else now
    reason = f"{name} hit its usage limit"
    detail: dict[str, Any] = {"text": "" if event is None else _needs_without_reset(event.text)}
    lifts = "usage limit"
    if resets is not None:
        lifts = f"limit resets {claude_accounts_core.format_reset(resets, now=now, clock=False)}"
        reason += f" · {lifts}"
        detail["resets_at"] = _iso_seconds(resets)

    def handed_over() -> bool:
        account = session.account if session is not None else None
        leaving = claude_accounts_service.leaving_slot(agent.account_slot, account)
        return hands_over is None or hands_over(leaving)

    return _needs_item(
        "limited",
        subject,
        project=project,
        agent=agent,
        reason=reason,
        excerpt=detail["text"] or lifts,
        detail=_needs_fit(detail, _DETAIL_TEXT_MAX),
        since=since,
        push_after=_needs_limited_push(
            since,
            resets,
            now=now,
            manager_live=manager_live,
            accounts=accounts,
            hands_over=handed_over,
        ),
    )


_NEEDS_RESET_SAID = re.compile(r" · resets (?:now|in \d+[dhm](?: \d+[hm])?)(?: \([^()]*\))?")
"""How a ``limited`` line of the board says when the limit lifts (``team._limited_text``,
through ``format_reset``): `` · resets in 3h 10m (13:10)``, a distance as of when the hook
ran and a clock time in the machine's zone."""


def _needs_without_reset(text: str) -> str:
    """A ``limited`` event's text without what it says of the reset (:data:`_NEEDS_RESET_SAID`).

    Claude Code's own words, which a line quotes when it could not read the reset, are left
    as they are: they name their zone.
    """
    return _NEEDS_RESET_SAID.sub("", text)


def _needs_limited_push(
    since: datetime,
    resets: datetime | None,
    *,
    now: datetime,
    manager_live: bool,
    accounts: AccountsSettings | None,
    hands_over: Callable[[], bool] | None = None,
) -> datetime | None:
    """When a limit may push: never when it lifts soon; later when someone else is on it.

    A reset within ``[accounts] wait_if_reset_within_minutes`` is cheaper to wait
    out than to act on, so it stays in the feed. With a live manager, or
    ``on_limit = "switch"`` and an account for the hand-over to go to
    (``hands_over``, the StopFailure hook's own question of this agent; asked last,
    as only this needs it), the hand-over or the manager is already moving it, and
    its ``switched`` event usually ends the item first.
    """
    if accounts is None:
        from aisquare.core.config import AccountsSettings

        accounts = AccountsSettings()
    if resets is not None and resets - now <= timedelta(
        minutes=accounts.wait_if_reset_within_minutes
    ):
        return None
    if manager_live or (accounts.on_limit == "switch" and (hands_over is None or hands_over())):
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
    detail: dict[str, Any] = {"questions": questions}
    said = _needs_said(detail)
    detail = _needs_tool_detail(tool, detail, _DETAIL_TEXT_MAX)
    if _needs_said(detail) < said:
        # Cut to fit, an option's words read as the whole of them beside the digit that
        # picks it, as a call's did (review of #243, round 6): the card says so.
        detail["cut"] = {"questions": said}
        _needs_fit(detail, _DETAIL_TEXT_MAX)
    return _needs_item(
        "question",
        tool.tool_use_id,
        project=project,
        agent=agent,
        reason=f"{name} asks you a question",
        excerpt=excerpt,
        detail=detail,
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
    detail: dict[str, Any] = {"plan": plan}
    return _needs_item(
        "plan",
        tool.tool_use_id,
        project=project,
        agent=agent,
        reason=f"{name} asks you to approve a plan",
        excerpt=first,
        detail=_needs_tool_detail(tool, detail, _DETAIL_PLAN_MAX, shown=detail),
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
    if _needs_drawn_since_notice(session, pane_output, now):
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


def _needs_drawn_since_notice(
    session: TeamSession | None,
    pane_output: Callable[[], datetime | None] | None,
    now: datetime,
) -> bool:
    """The pane printed after the notice ``last_seen_at`` names, less than :data:`_NOTICE_WAIT`
    ago: the prompt on screen may be one whose notification has not landed yet.

    A sub-agent's prompts are all under its one ``Task`` (:func:`_needs_subagent_prompt`),
    so only the notice tells one from the next. Its cost is a redraw (an attach, a resize, a
    key that moves the highlight) hiding the card this long. Calls of the agent's own are
    told apart by what the screen shows (:func:`_needs_on_screen`), not by when it printed:
    that rested on when Claude Code draws a call's prompt beside another call, which nothing
    here knows (review of #259, round 8, 1/9).
    """
    if session is None:
        return False
    output = None if pane_output is None else pane_output()
    return output is not None and output > session.last_seen_at and now - output < _NOTICE_WAIT


def _needs_screen_once(
    pane_screen: Callable[[], Sequence[str] | None] | None,
) -> Callable[[], Sequence[str] | None] | None:
    """``pane_screen``, asked of tmux the first time it is called and remembered: one
    derivation reads the screen once, however many of its rules look at it."""
    if pane_screen is None:
        return None
    read: list[Sequence[str] | None] = []

    def once() -> Sequence[str] | None:
        if not read:
            read.append(pane_screen())
        return read[0]

    return once


def _needs_drawn_dialog(screen: Callable[[], Sequence[str] | None] | None) -> str | None:
    """What the dialog up shows above its options (:func:`_needs_prompt_region`), beside a
    question or a plan pending with a call the screen cannot name; ``None`` with no dialog
    drawn. With no notice, a dialog is up and waits for the human: rule 8's card, before any
    notice, named after it (:func:`_needs_dialog_item`).

    Its own dialog is all an ``AskUserQuestion`` or an ``ExitPlanMode`` needs (rules 3 and
    4), but beside a call with no mark (a ``Task``, a ``Read``, an MCP tool) the screen
    names none of them (:func:`_needs_on_screen`), and rule 8 waited for a notice that
    Claude Code may never send: no card at all (review of #259, round 9, 5/8). Only while a
    dialog is drawn (:func:`_needs_prompt_region`): a question waiting behind a command at
    work is not up yet. Nor from a screen nobody could read, which counted as one drawn: a
    question queued behind a sub-agent at work, two captures failing, was pushed "shows a
    dialog that needs you" while nothing was, under the id its real dialog needed later
    (round 11, 5/13). A live scan that cannot read it decides nothing, its last items
    standing (:func:`_needs_screen_read`). The card is named after the question or
    the plan (:func:`_needs_dialog_item`), so a notice landing later names it the same: it
    no longer waits :data:`_NOTICE_WAIT` for one, which a notice later still outran. A dialog
    with no rule above its first option (taller than the screen: a long Edit diff, a plan
    whose top scrolled off) is ``""``, named after the question or plan alone: named by the
    rows it showed, a resize (asq attaching to the pane adopts its size) made it a new card,
    pushed again, its dismissal lost (review of #259, round 12, 6/9).

    A derivation that asks no screen at all (:func:`needs_single_agent_now`) has no such
    card: a screen not asked for is not one that could not be read. It had one for every
    question or plan beside an unmarked call, one queued behind a sub-agent at work too,
    and the guards that read it refused a merely pending tool as "is showing a prompt"
    (review of #259, round 10, 9/9).
    """
    rows = None if screen is None else screen()
    return None if rows is None else _needs_prompt_region(rows, ruled=True)


def _needs_prompt_call(
    pending: Sequence[PendingTool], pane_screen: Callable[[], Sequence[str] | None] | None
) -> PendingTool | None:
    """The pending call whose prompt is up: the one call, or of more than one, the call the
    pane's screen names (:func:`_needs_on_screen`); ``None`` when nothing says which."""
    if len(pending) <= 1:
        return pending[0] if pending else None
    return _needs_on_screen(pending, None if pane_screen is None else pane_screen())


def _needs_on_screen(
    pending: Sequence[PendingTool], rows: Sequence[str] | None
) -> PendingTool | None:
    """Of more than one pending call, the one whose prompt the screen ``rows`` show: the only
    one whose mark (:func:`_needs_prompt_mark`) is in the prompt's text above its options
    (:func:`_needs_prompt_region`); ``None`` when that is not exactly one.

    It fails closed. A call with no mark (an MCP tool, a ``Task`` whose sub-agent's call is
    on screen, a plan whose first line may have scrolled away) may be the one up, so beside
    one nothing is named. A mark found beside another's (a description that quotes the next
    command) names neither. A mark under :data:`_PROMPT_MARK_MIN` (``ls``) found is found in
    too much text to name its call, though one not found rules its call out. A card then
    has no quick answers, and the key pad beside the live pane answers what it shows.
    """
    region = None if rows is None else _needs_prompt_region(rows)
    if region is None:
        return None
    shown: list[tuple[PendingTool, str]] = []
    for tool in pending:
        mark = _needs_prompt_mark(tool)
        if mark is None:
            return None
        if mark in region:
            shown.append((tool, mark))
    if len(shown) != 1 or len(shown[0][1]) < _PROMPT_MARK_MIN:
        return None
    return shown[0][0]


def _needs_prompt_mark(tool: PendingTool) -> str | None:
    """The text a call's prompt shows of it, squashed and cut to :data:`_PROMPT_MARK_MAX`:
    a question's words, a command, a file's base name, a URL; ``None`` for a call whose
    prompt is not known to show one, or that shows nothing of it."""
    if tool.name == "AskUserQuestion":
        questions = _needs_questions(tool.input.get("questions"))
        text = questions[0]["question"] if questions else ""
    elif tool.name == "Bash":
        text = _needs_str(tool.input.get("command"))
    elif tool.name in _PROMPT_FILE_TOOLS:
        path = _needs_str(tool.input.get("file_path")) or _needs_str(
            tool.input.get("notebook_path")
        )
        text = path.replace("\\", "/").rsplit("/", 1)[-1]
    elif tool.name == "WebFetch":
        text = _needs_str(tool.input.get("url"))
    else:
        return None
    return _needs_squashed(text)[:_PROMPT_MARK_MAX] or None


def _needs_prompt_region(rows: Sequence[str], *, ruled: bool = False) -> str | None:
    """The squashed text of the dialog on screen above its options: from the rule nearest
    above its first option (or the screen's top) to that option; ``None`` with no option
    row, or with the input box drawn below it (two rules: a list in the conversation, not
    a dialog, which replaces the input box while it is up). ``ruled``: ``""`` for a dialog
    with no rule above it, taller than the screen, whose rows on screen a resize changes."""
    plain = [_ESCAPES.sub("", row) for row in rows]
    option = next(
        (n for n in range(len(plain) - 1, -1, -1) if _PROMPT_OPTION_ONE.match(plain[n])), None
    )
    if option is None or sum(1 for row in plain[option + 1 :] if _PROMPT_RULE.match(row)) > 1:
        return None
    top = next((n + 1 for n in range(option - 1, -1, -1) if _PROMPT_RULE.match(plain[n])), None)
    if top is None and ruled:
        return ""
    return _needs_squashed("".join(plain[top or 0 : option]))


def _needs_squashed(text: str) -> str:
    """``text`` without what a terminal changes when it draws it (:data:`_PROMPT_UNSPACED`),
    case folded: a command wrapped across three rows of a box is the command."""
    return _PROMPT_UNSPACED.sub("", _ESCAPES.sub("", text)).casefold()


def _needs_tool_detail(
    tool: PendingTool,
    detail: dict[str, Any],
    limit: int,
    *,
    shown: dict[str, Any] | None = None,
    whole: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """A pending tool's ``detail`` fit to ``limit``, saying what it leaves out of the call.

    ``dropped``: the call's input never came (:attr:`PendingTool.input_dropped`). ``cut``:
    each string of the input that ``shown``, where the detail holds the input's values,
    holds only the start of, by the whole one's length in characters. A command cut to
    2 000 characters, or fit to the card's 4 KiB, read as the whole of it beside the
    buttons that approve it; ``cut`` is fit with the rest, so the detail keeps to ``limit``.
    ``whole`` is each value as the card would show it whole, the input's own by default.
    """
    if tool.input_dropped:
        detail["dropped"] = True
    wholes = tool.input if whole is None else whole
    while True:
        _needs_fit(detail, limit)
        cut = {
            key: len(value)
            for key, value in wholes.items()
            if isinstance(value, str) and shown is not None and key in shown and shown[key] != value
        }
        if cut == detail.get("cut", {}):
            return detail
        detail["cut"] = cut


def _needs_tool_fields(raw: Mapping[str, object]) -> tuple[dict[str, object], int]:
    """The fields of a tool call's input its permission card shows, each as the card would show
    it whole, and how many it leaves out.

    What the tool would do first (:data:`_DETAIL_INPUT_KEYS`), then the rest in the call's
    own order, at most :data:`_DETAIL_FIELDS_MAX`, under a name the card can hold
    (:data:`_DETAIL_FIELD_NAME`). A string, a number or a bool as it is; a list, an object
    or a null as its JSON. Only those eleven keys' scalar values were kept, and nothing
    said the rest were gone: an MCP merge's owner, repo and pull request showed as no
    input at all, a ``MultiEdit`` as its file without its edits, each card reading as the
    whole call beside the "1" that approves it (review of #243, round 6).
    """
    order = [key for key in _DETAIL_INPUT_KEYS if key in raw]
    order += [key for key in raw if key not in _DETAIL_INPUT_KEYS]
    fields: dict[str, object] = {}
    for key in order:
        if len(fields) >= _DETAIL_FIELDS_MAX or not _DETAIL_FIELD_NAME.match(key):
            continue
        value = raw[key]
        if isinstance(value, str | bool | int) or (
            isinstance(value, float) and math.isfinite(value)
        ):
            fields[key] = value
            continue
        try:
            fields[key] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError, RecursionError):  # not as the call's JSON had it
            continue
    return fields, len(raw) - len(fields)


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
    """A permission prompt: the call in ``detail``, and what of it the card cannot hold
    (:func:`_needs_tool_fields`, :func:`_needs_tool_detail`), so nobody approves blind.

    The buttons are the dialog's own digits and Esc; the card shows the live
    pane beside them, so the options' real text is on screen. A prompt under a
    ``Task``/``Agent`` tool is a sub-agent's, whose own tool use is in its own
    records, not this transcript: its detail is the task the sub-agent was given,
    marked ``subagent``, since the call its "1" approves is not in it.
    ``subject`` and ``since`` default to the tool use's.
    """
    if tool.name in _SUBAGENT_TOOLS:
        reason = f"{name} waits for a permission answer (in a sub-agent)"
    elif _TOOL_NAME.fullmatch(tool.name):
        reason = f"{name} waits for a permission answer to use {tool.name}"
    else:
        reason = f"{name} waits for a permission answer"
    whole, omitted = _needs_tool_fields(tool.input)
    shown: dict[str, Any] = {
        key: _needs_cut(value, _DETAIL_STRING_MAX) if isinstance(value, str) else value
        for key, value in whole.items()
    }
    since = since or tool.at or now
    detail: dict[str, Any] = {"tool": _needs_cut(tool.name, 200), "input": shown}
    if omitted:
        detail["omitted"] = omitted
    if tool.name in _SUBAGENT_TOOLS:
        detail["subagent"] = True
    return _needs_item(
        "permission",
        subject or tool.tool_use_id,
        project=project,
        agent=agent,
        reason=reason,
        excerpt=tool.summary,
        detail=_needs_tool_detail(tool, detail, _DETAIL_TOOL_MAX, shown=shown, whole=whole),
        since=since,
        push_after=since,
        answers=(
            QuickAnswer("1", ("1",)),
            QuickAnswer("2", ("2",)),
            QuickAnswer("No", ("Escape",)),
        ),
    )


def _needs_failed_item(
    event: TeamEvent, *, project: ProjectInfo, agent: FleetAgent, name: str
) -> NeedsItem:
    """A turn that ended on an API error, its agent at its prompt since: no hook has fired.

    ``team.hook_stop_failure`` marks the session ``waiting``, as a Stop would, and writes
    ``turn_failed`` (``<error>: <message>``): a login that expired, credit run out, the API
    overloaded past Claude Code's own retries. Nothing else tells anyone, no manager is
    nudged, and nothing retries: the agent sits there, its claim held, until someone logs
    it in, switches its account or tells it to go on. The next prompt, or any hook, moves
    ``last_seen_at`` past the event and ends the item; a hand-over's own failures are the
    hand-over's (the session reads ``switching`` then, not ``waiting``).
    """
    error = event.text.split(":", 1)[0].strip()
    said = f" ({error})" if _FAILURE_KIND.fullmatch(error) else ""
    return _needs_item(
        "failed",
        str(event.seq),
        project=project,
        agent=agent,
        reason=f"{name}'s turn failed{said}",
        excerpt=event.text,
        detail=_needs_fit({"text": event.text}, _DETAIL_TEXT_MAX),
        since=event.created_at,
        push_after=event.created_at,
    )


def _needs_interrupted_item(
    tail: TranscriptTail, *, project: ProjectInfo, agent: FleetAgent, name: str, now: datetime
) -> NeedsItem:
    """An agent stopped by an Esc (or a rejected prompt), back at its prompt waiting: about
    its marker, also when a local command run since is the newest record."""
    text = tail.last_text or ""
    paragraphs = [part for part in re.split(r"\n\s*\n", text.strip()) if part.strip()]
    _kind, at, key = _needs_settled(tail)
    since = at or now
    return _needs_item(
        "interrupted",
        key or since.isoformat(),
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
    manager_live: bool = False,
    stopped: datetime | None = None,
) -> list[NeedsItem]:
    """The board's open questions and results for the human (:func:`is_needs_board_event`).

    One is cleared, besides a dismissal, when it is older than
    :data:`QUESTION_HORIZON`, when a LATER note, decision or result of the human's
    is addressed to its author (by label or role), or when its author posts a
    later question, result or decision: they have moved on. An unaddressed
    human note clears nothing; it is news, not an answer. ``agents`` names each
    author by its fleet label. ``manager_live``: the board's write wakes a manager that
    can take it (:func:`_needs_nudged`). Once none can, however it went, a coder's question
    or result to it still open is the human's, but for a result to a manager that stopped
    (``stopped``, when): posted before the stop, or within ``RECENTLY_ENDED`` after it, a
    result stays on the board, news, not a question, while the stop is a card of its own or
    a clean exit. Judged by the result's own time, it is the same card every scan of its
    day (review of #243, rounds 22 to 25).
    """
    from aisquare.services.fleet import RECENTLY_ENDED

    by_session = {session.id: session for session in sessions}
    rows: dict[str, FleetAgent] = {}
    for agent in sorted(agents, key=lambda agent: agent.created_at):
        if agent.session_id:
            rows[agent.session_id] = agent  # the newest row of a session names it
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
            taken = manager_live or (
                event.kind == "result"
                and stopped is not None
                and event.created_at - stopped <= RECENTLY_ENDED
            )
            if open_still and is_needs_board_event(event, author_role=role, manager_live=taken):
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


# --- one project --------------------------------------------------------------------------


_NEEDS_NOT_ACTING = frozenset({"exited", "lost", "limited", "unknown"})
"""Derived states of a live row that takes no nudge: dead, gone, parked on its usage limit
until a reset that can be hours away, or on a tmux server that would not answer. A manager
in one of them is no manager to leave a crash or a coder's question to."""


def _needs_nudged(
    project: ProjectInfo,
    board_id: str,
    manager: FleetAgent | None,
    listed: list[FleetAgentStatus],
    now: datetime,
) -> FleetAgent | None:
    """``manager``, the project's (:func:`_needs_manager`), while it hears of what its agents
    need and can take it: live in ``listed``, the listing, on a board session not ended, in
    none of :data:`_NEEDS_NOT_ACTING`. ``board_id`` is the project's board. ``None``:
    nobody does, and what they need is the human's. A row younger than
    :data:`_NEEDS_STARTING` whose session has yet to register is starting, not one that
    cannot take them: read as none, a restart, a switch or a spawn made every coder's open
    question and result to it of the day a card, due at once (review of #243, round 26, 1/2).
    So is one dated less than that ahead of the scan, written while it ran; dated further
    ahead (the clock set back), it was starting until the clock caught up (round 27, 6/6).

    The one rule for which manager keeps an item off the phone: a coder's question or
    result to the manager, a crash, and for a while the push of a limit. A turn that
    ended on a question wakes no manager, so it waits on none.

    On its own board it is the one manager the fleet wakes (``fleet.nudge_manager``),
    which a board write (``team.note``), an agent's exit and a usage limit all nudge by
    the project's id, through its board session: a row whose session never registered
    (no aisquare hooks) takes nothing (review of #243, round 24, 5/8). A manager session
    no row holds, or a numbered seat (``manager-2``), is sent nothing.

    When its agents write another project's board (under ``AISQUARE_TEAM_HUB`` the
    hub's, for a worktree its principal checkout's) it is nobody. A board write there
    nudges the board's manager, not the project's, and which manager on a shared board
    would handle an item had no answer that held: keyed by the board, the project or
    the spawner, each left items that no card showed and no nudge reached (review of
    #243, rounds 18 to 21). So every question and result on that board, and every
    crash, limit and failed hand-over of the project's, is a card and a push: more
    cards on a hub, never a silent stall. A manager that is down is its own project's
    either way, from that project's rows alone (:func:`_needs_manager_down`).
    """
    if board_id != project.id or manager is None or manager.ended_at is not None:
        return None
    status = next((s for s in listed if s.agent.id == manager.id), None)
    if status is None or status.state in _NEEDS_NOT_ACTING:
        return None
    woken = status.session is not None and status.session.ended_at is None
    return manager if woken or abs(now - manager.created_at) < _NEEDS_STARTING else None


def _needs_manager(rows: Sequence[FleetAgent]) -> FleetAgent | None:
    """The ``manager`` row: the newest live, else the newest. Of any kind, with a session or
    not, as ``fleet.nudge_manager`` picks it (``fleet_agent_by_label``): the newest with a
    session judged an older row the nudge never picks (review of #243, round 24, 8/8)."""
    from aisquare.services.fleet import MANAGER_LABEL

    seats = [row for row in rows if row.label == MANAGER_LABEL]
    return max(seats, key=lambda row: (row.ended_at is None, row.created_at), default=None)


@dataclass(frozen=True)
class _NeedsProject:
    """One project's scan: its items, and the facts they came from."""

    items: list[NeedsItem]
    statuses: list[FleetAgentStatus]
    rows: list[FleetAgent]
    """Its live rows, and those that ended in the scan's two days."""
    tails: dict[str, TranscriptTail | None]
    """Agent id → the tail its items were read from."""
    failed: Exception | None
    """The read that failed (:class:`_NeedsReads`): then none of the above is to be used."""
    failed_read: str
    """What that read was of."""
    blind: Mapping[str, Exception]
    """Agent id → the capture of its screen that failed (:class:`_NeedsReads`): that agent's
    own item is not to be used, and every other's is."""


_T = TypeVar("_T")


class _NeedsReads:
    """Every read of one project's scan, each through :meth:`needs_read`. ``failed`` is the
    first that failed, ``failed_read`` what it was of; ``None`` while all worked. ``blind``
    holds the agents whose screen could not be captured, by row id, each one's failure."""

    def __init__(self, project: ProjectInfo) -> None:
        self.project = project
        self.failed: Exception | None = None
        self.failed_read = ""
        self.blind: dict[str, Exception] = {}

    def needs_read(self, read: Callable[[], _T], what: str, *, agent: str = "") -> _T | None:
        """``read()``; ``None`` once a read failed, this one or one before: nothing is read
        after one fails, since nothing the scan finds then is used. A read of one agent's own
        (``agent``, its row's id: its screen) that fails is that agent's alone. Taken for the
        project's, one pane that closed between the listing and its capture failed the whole
        scan, and every action on every other agent of the project was a 503 (review of #259,
        round 12, 4/9)."""
        if self.failed is not None:
            return None
        try:
            return read()
        except Exception as exc:
            log.debug("remote: needs could not read %s of %s", what, self.project.id, exc_info=True)
            if agent:
                self.blind[agent] = exc
            else:
                self.failed, self.failed_read = exc, what
            return None


def _needs_scan_project(
    sources: NeedsSources,
    project: ProjectInfo,
    *,
    board_id: str,
    now: datetime,
    first_seen: MutableMapping[str, datetime],
    seen: set[str],
) -> _NeedsProject:
    """Every item of one fleet project, and whether every read they came from worked.

    Unknown is not absent: what a read that failed would have said is not known, so a
    scan decides only from a complete read (:class:`_NeedsReads`). Each read taken for
    none changed cards: a listing or ended rows not read lost a manager that stopped, and
    every coder's result to it was a card; a day or exits not read made a failed hand-over
    no card; events not read named a dialog anew, its dismissal lost (review of #243,
    rounds 22 to 27). So when one fails the scan's items are not used: its last items
    stand (:func:`scan_needs_you`), and an action's check is a 503 (:func:`needs_agent_now`).
    A screen tmux could not capture is one agent's read: that agent's item alone is not
    used, the same way (``blind``).
    The ended rows go back a day before the board's day: a result to the manager is judged
    by the stop in its own day (:func:`needs_from_board`), and read a day from now, a result
    an hour after a stop became a card once the stop was a day old (round 25, 3/4).
    ``first_seen`` dates the items whose facts carry no date (a pane gone, tmux
    not answering, a dialog drawn: an agent's item dated ``now``) from the first scan that
    saw them; ``seen`` collects what this scan saw, so the caller can forget the rest.

    The board's sessions and events are read from ``board_id``, the project's board
    (:func:`_needs_board_of`), its rows by its own id. Under ``AISQUARE_TEAM_HUB``
    every session and event is the hub's: read by the fleet project's id, the board
    was empty to the scan, and a manager's question or result, a failed turn and the
    usage-limit dialog's words never became a card or a push, while the Board tab
    showed them (sweep 4 of #243). The exits the fleet announces are the row's own
    project's, though (``fleet._emit_exit``), and are read there (:func:`_needs_exits`;
    read from the hub's, a hand-over that could not start its replacement was no card,
    review of #243, round 18, 1/4). No other project's rows are read: whether a manager
    hears of an item is :func:`_needs_nudged`'s to say.
    """
    from aisquare.services.fleet import RECENTLY_ENDED

    reads = _NeedsReads(project)
    listed = reads.needs_read(
        lambda: sources.list_agents(project) if sources.has_live_agents(project.id) else [],
        "the agents",
    )
    # A question that failed is no answer: no row reads ``unknown``, no outage is first seen.
    statuses = _needs_unheard(
        listed or [],
        lambda socket, path: (
            reads.needs_read(partial(sources.tmux_answers, socket, path), "tmux") is not False
        ),
    )
    day = now - max(QUESTION_HORIZON, RECENTLY_ENDED)
    ended = reads.needs_read(lambda: sources.ended_agents(project.id, day - RECENTLY_ENDED), "rows")
    board = reads.needs_read(lambda: sources.board_since(board_id, day), "the board") or []
    rows = list(
        {row.id: row for row in [*(ended or []), *(status.agent for status in statuses)]}.values()
    )
    manager = _needs_manager(rows)
    taking = _needs_nudged(project, board_id, manager, statuses, now)
    authors = _needs_board_authors(board, now)
    sessions = (
        reads.needs_read(lambda: sources.board_sessions(board_id, authors), "sessions")
        if authors
        else []
    )
    accounts = reads.needs_read(sources.accounts, "the accounts")
    window = _needs_window(sources, board_id, reads)
    items: list[NeedsItem] = []
    tails: dict[str, TranscriptTail | None] = {}
    for status in statuses:
        tail = tails[status.agent.id] = _needs_tail_of(sources, status, reads)
        derived = needs_from_agent(
            status,
            tail,
            project=project,
            events=[
                *_needs_own_events(sources, board_id, status, window, reads),
                *_needs_failures(board, status),
            ],
            now=now,
            # The manager's own items wait on no one: its own limit's nudge goes to itself.
            manager_live=taking is not None and taking.id != status.agent.id,
            accounts=accounts,
            hands_over=sources.hands_over,
            pane_output=_needs_output_of(sources, status, reads),
            pane_screen=_needs_screen_of(sources, status, reads),
        )
        for item in [] if status.agent.id in reads.blind else derived:
            undated = item.kind == "lost" or item.since == now
            items.append(_needs_dated(item, first_seen, seen) if undated else item)
    own = board_id == project.id
    exits = board if own else _needs_exits(sources, project, day, reads) or []
    items.extend(
        _needs_crashed(
            ended or [],
            rows,
            exits,
            project=project,
            now=now,
            manager_live=taking is not None,
            task_status=lambda ref: reads.needs_read(partial(sources.task_status, ref), "a task"),
        )
    )
    items.extend(_needs_manager_down(manager, exits, project=project, now=now))
    items.extend(
        _needs_fleet_down(statuses, project=project, now=now, first_seen=first_seen, seen=seen)
    )
    items.extend(
        needs_from_board(
            board,
            sessions or [],
            rows,
            project=project,
            now=now,
            manager_live=taking is not None,
            stopped=manager.ended_at if own and manager is not None else None,
        )
    )
    return _NeedsProject(
        items=items,
        statuses=statuses,
        rows=rows,
        tails=tails,
        failed=reads.failed,
        failed_read=reads.failed_read,
        blind=reads.blind,
    )


def _needs_tail_of(
    sources: NeedsSources, status: FleetAgentStatus, reads: _NeedsReads
) -> TranscriptTail | None:
    """The agent's transcript tail, read only where a rule can use it."""
    session = status.session
    path = None if session is None else session.transcript_path
    if not path or status.state in ("exited", "unknown", "lost"):
        return None
    return reads.needs_read(partial(sources.transcript_tail, path), "a transcript")


def _needs_own_events(
    sources: NeedsSources,
    board_id: str,
    status: FleetAgentStatus,
    window: Callable[[], Sequence[TeamEvent]],
    reads: _NeedsReads,
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
        asked = partial(sources.session_event, board_id, session.id, kind, born)
        found = reads.needs_read(asked, "events")
        if found is not None:
            own.append(found)
            break
    return own


def _needs_failures(board: Sequence[TeamEvent], status: FleetAgentStatus) -> list[TeamEvent]:
    """The ``turn_failed`` events of the agent's session in the board's day, which rule 10 of
    :func:`needs_from_agent` reads: the day is read every scan anyway, for all its kinds."""
    session = status.session
    if session is None:
        return []
    return [e for e in board if e.kind == "turn_failed" and e.session_id == session.id]


def _needs_output_of(
    sources: NeedsSources, status: FleetAgentStatus, reads: _NeedsReads
) -> Callable[[], datetime | None]:
    """When the pane of ``status``'s row last printed, asked of tmux only when called."""
    return lambda: reads.needs_read(partial(sources.pane_output, status), "tmux")


def _needs_screen_of(
    sources: NeedsSources, status: FleetAgentStatus, reads: _NeedsReads
) -> Callable[[], list[str] | None]:
    """What the pane of ``status``'s row shows, asked of tmux only when called: that agent's
    read alone (:meth:`_NeedsReads.needs_read`)."""
    read = partial(sources.pane_screen, status)
    return lambda: reads.needs_read(read, "a screen", agent=status.agent.id)


def _needs_window(
    sources: NeedsSources, board_id: str, reads: _NeedsReads
) -> Callable[[], list[TeamEvent]]:
    """The project's board's newest :data:`NEEDS_BOARD_EVENTS` events, read the first time
    they are asked for in a scan, and not at all by one that asks nothing of them: most
    scans of most projects, whose agents are not at a dialog or parked on a limit."""
    read: list[list[TeamEvent]] = []

    def needs_window() -> list[TeamEvent]:
        if not read:
            asked = partial(sources.board_events, board_id, NEEDS_BOARD_EVENTS)
            read.append(reads.needs_read(asked, "the window") or [])
        return read[0]

    return needs_window


def _needs_exits(
    sources: NeedsSources, project: ProjectInfo, day: datetime, reads: _NeedsReads
) -> list[TeamEvent] | None:
    """The exits the fleet announced on ``project``'s own board since ``day`` (``agent_exited``,
    by the row's ``project_id``), for a project whose agents write another board. Only
    those: the day's other kinds are its board's (review of #243, round 19, 3/3)."""
    return reads.needs_read(lambda: sources.exits_since(project.id, day), "exits")


def _needs_board_of(sources: NeedsSources, project: ProjectInfo) -> str:
    """The id of ``project``'s board (:attr:`NeedsSources.board_project`); its own when the
    source cannot say (the team off, a root that is gone): a board read there finds what
    it finds. Unlike a read of the scan's, a failure here is an answer: with the team off
    ``team.resolve_project`` refuses every time."""
    try:
        return sources.board_project(project)
    except Exception:
        log.debug("remote: needs could not resolve the board of %s", project.id, exc_info=True)
        return project.id


def _needs_unheard(
    statuses: list[FleetAgentStatus], answers: Callable[[str, Path | None], bool]
) -> list[FleetAgentStatus]:
    """The listing, with each live row on a tmux server that does not answer read ``unknown``.

    ``fleet._derive`` takes a fresh board row over tmux (§5.1). With the server
    gone, a row reads working, waiting or attention for the board's stale window
    (30 minutes), and ``limited`` until its reset: ``fleet_down``, every live row
    ``unknown``, came that late, under cards for agents tmux had taken with it. A
    row reads ``unknown`` when the listing could not ask its server, since a
    row it could ask reads its pane (``lost`` once it is gone, ``exited`` once it
    is dead), so one such row says its server did not answer and one of those says
    it did. A server whose live rows all read from the board is asked.

    Save one: a row held as another server's (``FleetAgentStatus.held``) reads ``unknown``
    once its board goes stale, but its server answered, which is how the listing knew it
    for another's, so it says the server was heard, as ``lost`` and ``exited`` do. Read by
    its state word, one held row after a ``kill-server`` and a respawn made every healthy
    row on the new server ``unknown``: their prompts left the phone, and ``fleet_down``
    said tmux was not answering while it was (review of #259, round 1).

    A server is a socket's name and the path its rows' spawns recorded (``socket_at``), as
    the listing groups them (``fleet._observe_sockets``): by the name alone, a shell under
    another ``TMUX_TMPDIR`` asks another server (round 28 of #257).
    """
    live = [status for status in statuses if status.agent.ended_at is None]
    servers: dict[tuple[str, Path | None], set[str]] = {}
    for status in live:
        servers.setdefault(_needs_server(status), set()).add(
            "held" if status.held else status.state
        )
    silent: set[tuple[str, Path | None]] = set()
    for server in sorted(servers, key=lambda server: (server[0], str(server[1] or ""))):
        states = servers[server]
        heard = states & {"lost", "exited", "held"}
        if "unknown" in states or (not heard and not answers(*server)):
            silent.add(server)
    return [
        status.model_copy(
            update={
                "state": "unknown",
                "detail": "tmux unavailable" + ("" if status.agent.session_id else "; no hooks"),
            }
        )
        if status.agent.ended_at is None
        and _needs_server(status) in silent
        and status.state != "unknown"
        else status
        for status in statuses
    ]


def _needs_server(status: FleetAgentStatus) -> tuple[str, Path | None]:
    """The tmux server ``status``'s row is asked of: its socket's name, and the path its
    spawn recorded where that is not where the name leads from here (``None`` when it is)."""
    return status.agent.tmux_socket, status.socket_at


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
    """``item`` dated from the first scan that saw it, its push delay moved with it; one
    ``sighted`` named after that scan too, in UTC seconds as the kept sighting reads back. A
    sighting is forgotten once a scan does not see it: drawn again, it is a new card."""
    from aisquare.services.remote_server import _iso_seconds

    key = _needs_memory_key(item.project_id, item.id)
    seen.add(key)
    since = first_seen.setdefault(key, item.since)
    if item.sighted:
        named = needs_item_id(item.project_id, item.kind, f"{item.id}@{_iso_seconds(since)}")
        item = replace(item, id=named)
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

    A scan that could not read all it needs, or failed outright, says nothing of whether a
    pane is still gone or tmux still silent. Forgotten, the next scan that saw it
    again dated it anew: ``fleet_down``, whose id is its first sighting, became a
    new item, pushed again past the dismissal of the one before.
    """
    prefix = _needs_memory_key(project.id, "")
    seen.update(key for key in first_seen if key.startswith(prefix))


def _needs_crashed(
    ended: Sequence[FleetAgent],
    rows: Sequence[FleetAgent],
    exits: Sequence[TeamEvent],
    *,
    project: ProjectInfo,
    now: datetime,
    manager_live: bool,
    task_status: Callable[[str], str | None],
) -> list[NeedsItem]:
    """Agents that died with a failing exit status in the last hour, with nobody on it.

    A clean ``/exit`` is 0 and a forced stop has no status: neither is a crash.
    Except the stop of a switch or a restart that then could not start the
    replacement (its ``/exit``, or the kill when that did not land in time):
    nothing took the agent's place, and the exit it announced says so
    (``fleet.HANDOVER_FAILED``, read from the day's ``exits``). An agent whose task
    is closed (``task_status``) did its work; one the fleet woke a live manager for
    (``manager_live``, :func:`_needs_nudged`) is the manager's; one that was restarted
    since — a newer row holds its label — was handled. The ``manager`` row's own crash is
    ``manager_down``; a numbered seat's is a crash like any agent's, which the fleet
    wakes the ``manager`` for.
    """
    from aisquare.services.fleet import MANAGER_LABEL

    if manager_live:
        return []
    newest: dict[str, datetime] = {}
    for row in rows:
        newest[row.label] = max(newest.get(row.label, row.created_at), row.created_at)
    items: list[NeedsItem] = []
    for row in ended:
        if row.ended_at is None or now - row.ended_at > CRASH_WINDOW:
            continue
        if row.label == MANAGER_LABEL:
            continue
        clean = row.exit_status in (0, None)
        abandoned = clean and _needs_handover_failed(_needs_exit_of(row, exits))
        if clean and not abandoned:
            continue
        if newest.get(row.label, row.created_at) > row.created_at:
            continue
        if row.task_id is not None and task_status(row.task_id) in CLOSED_STATUSES:
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


def _needs_handover_failed(event: TeamEvent | None) -> bool:
    """Whether ``event`` is the exit a hand-over announced once its replacement did not start."""
    from aisquare.services import fleet as fleet_service

    return (
        event is not None
        and event.kind == "agent_exited"
        and event.text.endswith(fleet_service.HANDOVER_FAILED)
    )


def _needs_exit_of(row: FleetAgent, exits: Sequence[TeamEvent]) -> TeamEvent | None:
    """The exit the fleet announced for ``row`` (``agent_exited``, under its session), from
    the day's ``exits``; none older than the row, which is an earlier row's of the session."""
    if row.session_id is None:
        return None
    return max(
        (
            event
            for event in exits
            if event.kind == "agent_exited"
            and event.session_id == row.session_id
            and event.created_at >= row.created_at
        ),
        key=_needs_seq,
        default=None,
    )


def _needs_manager_down(
    manager: FleetAgent | None,
    exits: Sequence[TeamEvent],
    *,
    project: ProjectInfo,
    now: datetime,
) -> list[NeedsItem]:
    """The ``manager`` row ended in the last day (``RECENTLY_ENDED``), and none replaced it.

    Every stop but a clean exit (status 0) is reported, whatever its crew does: a failing
    exit status, a stop with none (a kill, a pane gone), and a switch or a restart that
    could not start the replacement (``fleet.HANDOVER_FAILED``, in the day's ``exits``).
    A clean exit (``fleet stop``, the phone's Stop, its own ``/exit``) is taken to be
    done. Raised only while an agent of its crew worked, a stop was cleared by the coder
    that reported to it and went idle, before its push (review of #243, round 25, 2/4),
    and on a shared board its crew may be another project's (round 22, 2/7). It goes when
    a manager is back, or its day is over.

    ``manager`` is the row the fleet nudges (:func:`_needs_manager`): a numbered seat that
    runs is woken for nothing, so it hid the ``manager``'s crash, reported nowhere (review
    of #243, round 22, 5/7); one that ends is :func:`_needs_crashed`'s.
    """
    from aisquare.services.fleet import RECENTLY_ENDED

    if manager is None or manager.ended_at is None or now - manager.ended_at > RECENTLY_ENDED:
        return []
    if manager.exit_status not in (0, None):
        reason = f"the manager exited unexpectedly (exit {manager.exit_status})"
    elif _needs_handover_failed(_needs_exit_of(manager, exits)):
        reason = "the manager stopped, and its replacement did not start"
    elif manager.exit_status is None:
        reason = "the manager stopped"
    else:
        return []
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
    server that is gone reads ``unknown`` too. A row held as another server's
    (``FleetAgentStatus.held``) is not counted: a server answered at its socket, so it is
    no sign that tmux does not. The subject is the first scan that saw it, so it is one
    item until the condition clears and a new one if it comes back. The watcher keeps
    that sighting on disk, so a restart of Remote, and ``asq remote needs``, give the
    same outage the same id.
    """
    from aisquare.services.remote_server import _iso_seconds

    live = [status for status in statuses if status.agent.ended_at is None and not status.held]
    if not live or any(status.state != "unknown" for status in live):
        return []
    key = _needs_memory_key(project.id, "fleet_down")
    seen.add(key)
    first = first_seen.get(key)
    # Kept on disk, a sighting from before one of these rows was made is another outage's,
    # cleared while no watcher looked: a row is made only while its tmux answers. A row
    # dated after this scan (the clock set back, a VM restored from a snapshot) is no
    # sign of that: every scan's sighting is before it, and each one started the outage
    # anew, a new id every 3 s that no dismissal held and no push streak reached.
    newest = max(status.agent.created_at for status in live).replace(microsecond=0)
    if first is None or first < newest <= now:
        first = first_seen[key] = now
    return [
        _needs_item(
            "fleet_down",
            f"{project.id}:{_iso_seconds(first)}",  # in UTC: as the kept sighting reads back
            project=project,
            agent=None,
            reason=f"tmux is not answering for {needs_push_safe(_needs_project_name(project))}",
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
    failing: MutableSet[str] | None = None,
    kept: Sequence[NeedsItem] = (),
) -> list[NeedsItem]:
    """Every item across every project, dismissals dropped, ranked by kind then ``since``.

    A project whose scan could not read all it needs (:func:`_needs_scan_project`), or
    failed outright, decides nothing: its items of ``kept``, the last scan's, stand as they
    were, ids and all, so a dismissal still hides them and a push's streak goes on, and no
    item is new (review of #243, round 27). So does an agent's whose screen could not be
    captured, alone (:func:`_needs_blind_kept`). ``first_seen`` is the watcher's
    memory of when it first saw the items whose facts carry no date; without
    it, each scan is the first. What it holds for a project that could not be
    looked at this time is kept for the next look. ``failing`` is the watcher's
    memory of the projects whose scans fail, outright or by a read: one warning a streak,
    naming the read, not one every 3 s (:func:`_needs_failed`), and "works again" only
    once a scan reads all it needs.
    """
    memory: MutableMapping[str, datetime] = {} if first_seen is None else first_seen
    seen: set[str] = set()
    sources = _needs_read_once(sources)
    items: list[NeedsItem] = []
    projects = sources.list_projects()
    boards = {project.id: _needs_board_of(sources, project) for project in projects}
    for project in projects:
        scanned: _NeedsProject | None = None
        try:
            scanned = _needs_scan_project(
                sources, project, board_id=boards[project.id], now=now, first_seen=memory, seen=seen
            )
        except Exception as exc:
            _needs_failed(f"the needs scan of {project.id}", exc, failing, project.id)
        else:
            if scanned.failed is not None:
                # A scan that decides nothing has not recovered: a read that keeps failing
                # froze the project's cards and 503'd its actions with debug lines alone
                # (review of #243, round 28, 1/10).
                what = f"the needs scan of {project.id} reading {scanned.failed_read}"
                _needs_failed(what, scanned.failed, failing, project.id)
            elif failing is not None and project.id in failing:
                failing.discard(project.id)
                log.info("remote: the needs scan of %s works again", project.id)
        if scanned is not None and scanned.failed is None:
            items.extend(scanned.items)
            items.extend(_needs_blind_kept(scanned, project, kept, memory, seen, failing))
        else:
            _needs_still_remembered(memory, seen, project)
            items.extend(item for item in kept if item.project_id == project.id)
    for key in [key for key in memory if key not in seen]:
        del memory[key]
    items = _needs_board_once(items, boards)
    return _needs_ranked([item for item in items if item.id not in dismissed])


def _needs_blind_kept(
    scanned: _NeedsProject,
    project: ProjectInfo,
    kept: Sequence[NeedsItem],
    first_seen: MutableMapping[str, datetime],
    seen: set[str],
    failing: MutableSet[str] | None,
) -> list[NeedsItem]:
    """The items of ``kept`` that stand for the agents whose screen ``scanned`` could not
    capture: each one's own item, as a project's stand when its scan cannot read all it needs.
    Its board items are the board's, read this scan. The project's first sightings are kept
    through the scan, as for a project's (:func:`_needs_still_remembered`): one agent's are
    not told apart from the rest's. Each such agent is told once a streak
    (:func:`_needs_failed`)."""
    prefix = f"{project.id}|"
    if failing is not None:
        told = {key for key in failing if key.startswith(prefix)}
        failing -= told - {prefix + agent_id for agent_id in scanned.blind}
    if not scanned.blind:
        return []
    labels = {status.agent.id: status.agent.label for status in scanned.statuses}
    for agent_id, exc in scanned.blind.items():
        what = f"the needs scan of {project.id} reading {labels.get(agent_id, agent_id)}'s screen"
        _needs_failed(what, exc, failing, prefix + agent_id)
    _needs_still_remembered(first_seen, seen, project)
    return [
        item
        for item in kept
        if item.agent_id in scanned.blind and item.kind not in _NEEDS_BOARD_KINDS
    ]


def _needs_read_once(sources: NeedsSources) -> NeedsSources:
    """``sources`` with each board's day, window and sessions, each tmux server's answer, the
    accounts and each slot's hand-over question read once for a whole scan.

    Under ``AISQUARE_TEAM_HUB`` every project's board is the hub's, and each project's
    scan read the same day, window and sessions of it again: N copies every 3 s
    (review of #243, round 21, 7/8). A read that fails is not kept: the next asks again.
    """
    kept: dict[tuple[object, ...], Any] = {}

    def once(what: str, read: Callable[..., _T]) -> Callable[..., _T]:
        def needs_once(*args: Any) -> _T:
            key = (what, *(frozenset(a) if isinstance(a, (set, frozenset)) else a for a in args))
            if key not in kept:
                kept[key] = read(*args)
            result: _T = kept[key]
            return result

        return needs_once

    return replace(
        sources,
        board_since=once("day", sources.board_since),
        board_events=once("window", sources.board_events),
        board_sessions=once("sessions", sources.board_sessions),
        tmux_answers=once("tmux", sources.tmux_answers),
        accounts=once("accounts", sources.accounts),
        hands_over=once("hands over", sources.hands_over),
    )


def _needs_board_once(items: Sequence[NeedsItem], boards: Mapping[str, str]) -> list[NeedsItem]:
    """``items`` with each question or result of a board that several projects write one card.

    Under ``AISQUARE_TEAM_HUB`` every project's board is the hub's, so each project's scan
    found every one of its questions and results: one event, one card per project, each
    its own id and its own push. It is the card of the project whose row asked it; one
    that no row asked (a manager started outside the fleet) is the card of the board's
    own project when it is listed, else of the first project that writes the board.
    ``boards`` is each scanned project's board, by its id, in the order scanned.
    """
    writers: dict[str, list[str]] = {}
    for project_id, written in boards.items():
        writers.setdefault(written, []).append(project_id)
    shared = {board: ids for board, ids in writers.items() if len(ids) > 1}
    if not shared:
        return list(items)
    kept: dict[tuple[str, object], NeedsItem] = {}
    rest: list[NeedsItem] = []
    for item in items:
        board = boards.get(item.project_id)
        if item.kind not in _NEEDS_BOARD_KINDS or board not in shared:
            rest.append(item)
            continue
        key = (board, item.detail.get("seq"))
        held = kept.get(key)
        if held is None or _needs_board_rank(item, board, shared[board]) < _needs_board_rank(
            held, board, shared[board]
        ):
            kept[key] = item
    return [*rest, *kept.values()]


def _needs_board_rank(item: NeedsItem, board: str, writers: Sequence[str]) -> tuple[int, int]:
    """Which project's card a shared board's item is: the asker's row's, the board's own, the
    first writer's, in that order."""
    return (
        0 if item.agent_id is not None else 1 if item.project_id == board else 2,
        writers.index(item.project_id),
    )


_K = TypeVar("_K")


def _needs_failed(what: str, exc: Exception, failing: MutableSet[_K] | None, key: _K) -> None:
    """Tell that ``what`` failed, once a streak of ``key``'s failures (``failing``, the keys
    failing since they last worked; ``None``: every one is the first).

    ``asq remote serve`` has no log handler, so a warning is the last-resort handler's
    lines on its terminal, traceback and all: a store that could not be read was 25 lines
    every 3 s, the link and the passphrase scrolled off (review of #243, sweep of round 4).
    The first of a streak is a warning, with its traceback; a store that cannot be opened
    is told instead in the sentence the CLI prints for it (``damaged_store_message``),
    which names the file and how to recover, since ``open_store``'s own exception carries
    only SQLite's words ("file is not a database"). The rest are debug lines.
    """
    from aisquare.core.store import StoreUnopenable, damaged_store_message

    if failing is not None and key in failing:
        log.debug("remote: %s failed again", what, exc_info=exc)
        return
    if failing is not None:
        failing.add(key)
    if isinstance(exc, StoreUnopenable):
        log.warning("remote: %s failed: %s", what, damaged_store_message(exc))
    else:
        log.warning("remote: %s failed", what, exc_info=exc)


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

    It runs the scan's part for this one project — one listing, its ended rows, its
    board, the cached tails, no other project's rows — plus tmux questions about the
    label's pane: is its foreground the agent, and has its window been quiet. Only a
    pane the listing vouched for is asked about: one that reads ``lost`` may be another
    agent's pane under the row's old id, and an Escape an action sends on the strength
    of the answer would land in that agent's turn.

    Raises a 503 ``RequestError`` when a read that scan needs failed, whatever the label's
    items: unknown is not absent (:func:`_needs_scan_project`). A snapshot without the
    listing claimed a pane shows no dialog when nobody asked; without the ended rows a
    Restart on a ``crashed`` or ``manager_down`` card was told "That agent is gone." (review
    of #243, round 25, 4/4); without the day, the exits or a session's events the card's
    item was missing or named anew, and its action a 409 ``stale`` (rounds 26 and 27). So
    an action that names no card is told to try again too. A screen tmux could not capture
    is its agent's alone: a 503 for the label whose it is, and no other's (review of #259,
    round 12, 4/9). Raises ``fleet.NoSuchAgent``
    only when the label has had no row at all among the scan's (two days of ended ones).
    """
    from aisquare.services import fleet as fleet_service
    from aisquare.services.remote_server import RequestError

    when = now or _needs_now()
    sources = live_needs_sources()
    board_id = _needs_board_of(sources, project)
    # The sightings the watcher kept, which name its cards (a dialog's, :func:`_needs_dated`).
    first_seen = _needs_first_seen_kept()
    scanned = _needs_scan_project(
        sources, project, board_id=board_id, now=when, first_seen=first_seen, seen=set()
    )
    where = _needs_project_name(project)
    if scanned.failed is not None:
        raise RequestError(503, "unavailable", f"{where} could not be read: {scanned.failed}")
    rows = [row for row in scanned.rows if row.label == label]
    if not rows:
        raise fleet_service.NoSuchAgent(f"no agent {label!r} in {where} within the last two days")
    newest = max(rows, key=lambda row: row.created_at)
    status = next((s for s in scanned.statuses if s.agent.id == newest.id), None)
    if status is not None and status.agent.id in scanned.blind:
        said = f"{label}'s screen could not be read: {scanned.blind[status.agent.id]}"
        raise RequestError(503, "unavailable", said)
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
    the board or its screen, and no project-level kind is derived. What the predicates read of
    them is the same: the usage-limit dialog, which the board names by its
    notification's text, is the plain dialog's ``permission`` item here, a dialog
    all the same. An id may differ from the scan's, so no card is matched against
    them: an action matches its card on :func:`needs_agent_now`'s snapshot, before
    it sends anything. ``status`` is never ``None``: an ended row reads
    ``exited``. Nothing is recorded: a dead pane reads ``exited``, and the next
    listing ends its row. Raises ``fleet.NoSuchAgent`` when no row holds the label, and a
    503 ``RequestError`` when its tail could not be read, as :func:`needs_agent_now` does.
    """
    from aisquare.core.store import store_session
    from aisquare.services import fleet as fleet_service
    from aisquare.services.remote_server import RequestError

    when = now or _needs_now()
    with store_session() as store:
        row = store.fleet_agent_by_label(project.id, label, live_only=False)
    if row is None:
        raise fleet_service.NoSuchAgent(f"no agent {label!r} in {_needs_project_name(project)}")
    status = fleet_service.status_of(row)
    reads = _NeedsReads(project)
    tail = _needs_tail_of(live_needs_sources(), status, reads)
    if reads.failed is not None:
        raise RequestError(503, "unavailable", f"{label} could not be read: {reads.failed}")
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
    pane_is_agent, pane_quiet = False, None
    if (
        status is not None
        and status.agent.ended_at is None
        and status.state not in _NEEDS_PANE_UNVOUCHED
    ):
        pane_is_agent, pane_quiet = _needs_pane_now(_needs_pane_server(status), status.agent, now)
    return AgentNow(
        project=project,
        status=status,
        tail=tail,
        pane_is_agent=pane_is_agent,
        pane_quiet=pane_quiet,
        items=items,
    )


def _needs_pane_now(
    server: TmuxServer, agent: FleetAgent, now: datetime
) -> tuple[bool, bool | None]:
    """Whether the row's pane runs the agent now, on the server the row was recorded on, and
    whether it is quiet, from one ``display-message`` (``PaneFacts``).

    The listing vouches for a pane only when it could ask tmux: a fresh board
    row derives its state without it. And a server that started after the row
    was written numbers its panes from ``%0`` again, so the pane under the row's
    id is another agent's (``fleet._outlived``). ``send-keys`` asks the same two
    questions before it types, so both ask them in one place:
    ``remote_server._remote_facts_refusal``.

    Quiet: the window printed nothing for ``fleet.ACTIVITY_WINDOW``. Claude Code
    animates its spinner while a tool runs, so a quiet pane with a tool pending is
    a dialog waiting. One-second resolution, the fact ``fleet._derive`` reads too;
    ``None`` when tmux would not say, and for a pane that is not the agent's. It was
    a process of its own after the two the refusal took, and an action polls this
    every quarter second (review of #243, round 4).
    """
    from aisquare.services import fleet as fleet_service
    from aisquare.services import remote_server

    facts = remote_server._remote_pane_facts(server, agent)
    if facts is None or remote_server._remote_facts_refusal(agent, facts) is not None:
        return False, None
    output = facts.last_output
    return True, None if output is None else now - output > fleet_service.ACTIVITY_WINDOW


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
    A tail whose walk ran out before the newest message (:func:`_needs_cut_short`) may hold
    a pending tool it did not read, and counts as one here.
    """
    status = snap.status
    if status is None or not snap.pane_is_agent:
        return False
    pending = bool(_needs_pending(snap.tail, status.agent)) or _needs_cut_short(
        snap.tail, status.agent
    )
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
    agent's. A tail whose walk ran out before the newest message
    (:func:`_needs_cut_short`) may hold one it did not read, so it counts.
    """
    status = snap.status
    if status is None or not snap.pane_is_agent:
        return False
    return bool(_needs_pending(snap.tail, status.agent)) or _needs_cut_short(
        snap.tail, status.agent
    )


def needs_unseen(snap: AgentNow) -> bool:
    """Whether the agent's pane is its own, and nothing read here can say what it shows: its
    row has no board session, or its transcript cannot be read.

    A row with no session is an agent no aisquare hook reports on (``fleet spawn --bin``,
    a Claude Code started without the hooks: ``no hooks`` in ``fleet ls``), or one whose
    session could not be found. Its tail is never read, so a permission prompt there is
    no pending tool and never ``attention``, and ``fleet._derive`` reads its pane quiet
    for 5 s as ``waiting``. Taken for an agent at rest, a stop typed ``/exit`` and Enter
    into the prompt, which took "1. Yes", and a tell's Enter did the same (review of
    #243, round 7). A session whose transcript cannot be read hides a tool use's first
    seconds the same way. Such an agent is a dialog that may be up: nothing is typed
    into it without an Escape before it.
    """
    status = snap.status
    if status is None or not snap.pane_is_agent:
        return False
    return status.session is None or snap.tail is None


def needs_at_input_prompt(snap: AgentNow) -> bool:
    """Whether the agent sits at its input prompt, where typed text is a message to it.

    No dialog, the pane is the agent and quiet (tmux must say so), no tool
    pending, and the newest record is an interruption, the agent's own words or
    one Claude Code wrote itself (a local command's output, a compaction's
    summary: ``own``), or this process has written no conversation yet
    (:func:`_needs_nothing_said`), or the row derives ``waiting``. Never for an
    agent nothing here reads (:func:`needs_unseen`): a quiet pane that derives
    ``waiting`` was all it had, and a permission prompt left 5 s is that too.

    A session starts ``working`` on the board, and the board is trusted for 30
    minutes: an agent just spawned with no prompt, or after a ``/clear``, read as
    busy at its fresh prompt. Prompt mode refused it, Interrupt & tell sent its Escape
    and gave up, and the only way to reach it was the Live tab (review of #243, sweep
    3). A turn writes the human's prompt first, and Claude Code animates while one runs.
    No hook follows a local command either (``/model``, ``/mcp``, a ``/compact``, whose
    ``SessionStart`` marks the session ``working`` again), and its records read as the
    human's prompt of a turn under way, for the same 30 minutes (sweep 4 of #243).
    """
    status = snap.status
    if status is None or not snap.pane_is_agent or snap.pane_quiet is not True:
        return False
    if needs_dialog_open(snap) or snap.tail is None or needs_unseen(snap):
        return False
    if _needs_pending(snap.tail, status.agent) or _needs_cut_short(snap.tail, status.agent):
        return False
    if snap.tail.newest in ("interrupted", "assistant_text", "own") or status.state == "waiting":
        return True
    return _needs_nothing_said(snap.tail, status.agent)


def _needs_nothing_said(tail: TranscriptTail, agent: FleetAgent) -> bool:
    """The row's process has written no conversation: its transcript is empty or not made
    yet (Claude Code makes it with the first record), or every record in it is older than
    the row, a resumed session's before its first prompt. A walk that met no conversation
    record is not this: it says nothing of what was written."""
    if tail.empty:
        return True
    return tail.newest_at is not None and tail.newest_at < agent.created_at


def needs_item_current(snap: AgentNow, item_id: str) -> bool:
    """Whether ``item_id`` is still one of the agent's items right now."""
    return any(item.id == item_id for item in snap.items)


def needs_prompt_now(
    snap: AgentNow, item: NeedsItem, now: datetime
) -> tuple[NeedsItem, ...] | None:
    """What the prompt on the agent's screen right now is, of the card ``item`` and the calls
    beside it, asked of tmux again: what a quick answer checks last, right before its keys.
    ``(item,)`` while it is still the call ``item`` names; otherwise the items of what is up
    instead, ``()`` when no prompt is drawn at all; ``None`` when tmux could not show the
    screen: not known either way.

    With more than one call pending, the card was the call its screen named when it was
    derived (:func:`_needs_on_screen`), and the screen is read again here: a prompt answered
    at the terminal meanwhile, and the next call's drawn, is not the card's. With one call
    pending the prompt can only be its own, as rule 5 says, and a card naming no call (the
    plain dialog's) is answered from the live pane, which shows what is up.

    A capture that failed or timed out read as another prompt up: the tap was refused as
    ``stale`` with a sentence that said so, and the card left every phone until a scan
    brought it back and pushed it again, while its prompt may have been the one up (review
    of #259, round 9, 3/8).

    What is up instead is derived from the same screen, as the scan derives it but without
    the board (:func:`needs_single_agent_now` reads the agent so too): the call it names, or
    rule 8's dialog card. The refusal carries it as ``current``, which the page says in the
    card's place. It carried none, and the page said "nothing waits on <agent> now" while
    the next call's prompt waited (review of #259, round 9, 2/8). A screen with no prompt on
    it, the most common stale tap (the prompt answered at the terminal, its call at work),
    is none: rule 8's card there was a dialog that was not drawn, the tap told "shows
    another prompt" and the page "No longer needs you: coder-1 shows a dialog that needs
    you" while nothing waited (round 10, 1/9).
    """
    status = snap.status
    pending = () if status is None else _needs_pending(snap.tail, status.agent)
    named = {needs_item_id(snap.project.id, item.kind, tool.tool_use_id): tool for tool in pending}
    if len(pending) <= 1 or item.id not in named or status is None:
        return (item,)
    rows = _needs_screen_now(status)
    if rows is None:
        return None
    if _needs_prompt_region(rows) is None:
        return ()
    if _needs_on_screen(pending, rows) is named[item.id]:
        return (item,)
    instead = needs_from_agent(
        status, snap.tail, project=snap.project, events=(), now=now, pane_screen=lambda: rows
    )
    # A prompt is drawn, whatever the board would say of it.
    return tuple(instead) or (
        _needs_dialog_item(status, None, None, project=snap.project, now=now),
    )


def _needs_screen_now(status: FleetAgentStatus) -> list[str] | None:
    """The rows the pane of ``status``'s row shows now (:func:`_needs_screen_read`); ``None``
    when tmux cannot say (a pane gone, a server not answering)."""
    from aisquare.core.tmux import TmuxError

    try:
        return _needs_screen_read(status)
    except TmuxError:
        return None


def _needs_screen_read(status: FleetAgentStatus) -> list[str]:
    """The rows the pane of ``status``'s row shows now, on the server the row lives on; raises
    ``TmuxError`` when tmux cannot say: the live sources' ``pane_screen``, whose failure is a
    failed read of the scan's, that agent's alone (:class:`_NeedsReads`).

    It answered ``None`` there, a screen that names no call, and a quick answer's
    re-derivation (:func:`needs_agent_now`) no longer had the card about one of several
    calls: a 409 ``stale`` "no longer shows that question", the card dropped from the page,
    before :func:`needs_prompt_now` could answer the 503 that keeps it (review of #259,
    round 11, 3/13). Unknown is not absent: the agent's last item stands, and an action's
    check on it is a 503."""
    return _needs_pane_server(status).capture(status.agent.pane_id).lines


def _needs_pane_server(status: FleetAgentStatus) -> TmuxServer:
    """The server ``status``'s row lives on, asked where the listing worked out it listens
    (``FleetAgentStatus.socket_at``), and by the socket's name the row recorded.

    ``fleet.server_of`` works the address out again, reading ``config.toml`` each time
    (round 29 of #257 measured that), and the screen was read through it for every agent
    with more than one call pending, every scan, and again under the agent's lock for each
    quick answer (review of #259, round 9, 7/8), whose keys go here too (:func:`_needs_send`).
    """
    from aisquare.services import fleet as fleet_service

    return fleet_service.server_for(status.agent.tmux_socket).at(status.socket_at)


# --- the live sources ---------------------------------------------------------------------


def live_needs_sources() -> NeedsSources:
    """The scan over the live store and tmux: the same calls ``fleet ls`` and the board make."""
    from aisquare.core.store import AmbiguousIdError, store_session
    from aisquare.services import claude_accounts as claude_accounts_service
    from aisquare.services import fleet as fleet_service
    from aisquare.services import project as project_service
    from aisquare.services import remote_server

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
                project_id, since, kinds=_NEEDS_DAY_KINDS, human_kinds=_NEEDS_REPLIED
            )

    def needs_exits_since(project_id: str, since: datetime) -> list[TeamEvent]:
        with store_session() as store:
            return store.team_events_since(project_id, since, kinds=("agent_exited",))

    def needs_board_sessions(project_id: str, ids: Collection[str]) -> list[TeamSession]:
        with store_session() as store:
            return store.team_sessions_seen_since(project_id, None, ids=sorted(ids))

    def needs_task_status(ref: str) -> str | None:
        with store_session() as store:
            try:
                task = store.get_task(ref)
            except AmbiguousIdError:
                return None
        return None if task is None else task.status

    def needs_live_agents(project: ProjectInfo) -> list[FleetAgentStatus]:
        return _needs_with_board_sessions(fleet_service.list_agents(project, live_only=True))

    def needs_tmux_answers(socket: str, path: Path | None) -> bool:
        return fleet_service.server_for(socket).at(path).answers()

    def needs_pane_output(status: FleetAgentStatus) -> datetime | None:
        # The fact the snapshot reads (`_needs_pane_now`), from the one format and parse
        # `PaneFacts` owns: a second copy of both read the same pane quiet by another rule.
        facts = remote_server._remote_pane_facts(_needs_pane_server(status), status.agent)
        return None if facts is None else facts.last_output

    def needs_session_event(
        project_id: str, session_id: str, kind: str, since: datetime
    ) -> TeamEvent | None:
        with store_session() as store:
            return store.newest_session_event(project_id, session_id, kind, since=since)

    def needs_board_project(project: ProjectInfo) -> str:
        """The project's board as ``asq board`` in its root resolves it, the server's own
        answer (``remote_server._remote_board_project``): kept here as well, the two could
        disagree for the life of the server (review of #243, round 11, 6/9)."""
        return remote_server._remote_board_project(project.root).id

    return NeedsSources(
        list_projects=project_service.list_projects,
        list_agents=needs_live_agents,
        ended_agents=needs_rows_ended,
        board_events=needs_board_events,
        board_since=needs_board_since,
        exits_since=needs_exits_since,
        board_sessions=needs_board_sessions,
        task_status=needs_task_status,
        transcript_tail=_needs_cached_tail,
        accounts=claude_accounts_service.accounts_settings,
        hands_over=_needs_hands_over_kept,
        has_live_agents=needs_rows_live,
        tmux_answers=needs_tmux_answers,
        session_event=needs_session_event,
        pane_output=needs_pane_output,
        pane_screen=_needs_screen_read,
        board_project=needs_board_project,
    )


def _needs_with_board_sessions(statuses: list[FleetAgentStatus]) -> list[FleetAgentStatus]:
    """The listing, each live row whose session the team keeps on another project's board
    derived again with it, the session found by its id.

    ``fleet.list_agents`` reads the sessions of the project's own board, and under
    ``AISQUARE_TEAM_HUB`` every session is the hub's: no row had one, so none read
    attention, a usage limit or a failed turn, and no transcript was read (sweep 4 of
    #243). Only such rows are derived again, in one read of the store and one look at
    each tmux server they are on, as the listing looks: ``fleet.status_of`` of each was a
    store session and a server's three questions per row, every row of a hub's every 3 s
    scan. A server that cannot be asked reads its rows as the listing would; a look that
    fails is the listing's failure (:func:`_needs_scan_project`).

    A row the listing read at Claude Code's folder-trust question (``fleet.TRUST_PROMPT``,
    from its screen) keeps that state: ``_status`` reads the board alone, and a restart
    that resumed a session on an account that never trusted the folder read as the old
    session's ``waiting``, with no card and no push (review of #259, round 13). Its screen
    is not captured again: the listing looked at it a moment ago.
    """
    from aisquare.core.store import store_session
    from aisquare.services import fleet as fleet_service

    elsewhere = [
        status
        for status in statuses
        if status.session is None and status.agent.session_id and status.agent.ended_at is None
    ]
    if not elsewhere:
        return statuses
    with store_session() as store:
        held = {
            status.agent.id: session
            for status in elsewhere
            if status.agent.session_id
            and (session := store.get_session(status.agent.session_id)) is not None
        }
    if not held:
        return statuses
    rows = [status.agent for status in elsewhere if status.agent.id in held]
    tmux_session = elsewhere[0].tmux_session  # the listing's one, its project's
    config = fleet_service.settings()
    views = fleet_service._observe_sockets(rows, tmux_session, config)
    now = fleet_service._now()
    derived = {
        agent.id: fleet_service._status(
            agent,
            held[agent.id],
            views.get(fleet_service._address(agent, config)),  # as given: `_status` reads it
            tmux_session,
            now,
            config,
        )
        for agent in rows
    }

    def again(status: FleetAgentStatus) -> FleetAgentStatus:
        found = derived.get(status.agent.id)
        if found is None:
            return status
        if status.state == "attention" and (status.detail or "").startswith(
            fleet_service.TRUST_PROMPT
        ):
            return found.model_copy(update={"state": status.state, "detail": status.detail})
        return found

    return [again(status) for status in statuses]


_NEEDS_HANDS_OVER_KEPT = 60.0
"""Seconds an answer of :func:`_needs_hands_over_kept` is kept: the question opens the store
and parses every account's ``.claude.json`` (tens of MB) and ``.credentials.json``, and an
agent that no hand-over can move stays limited for hours, every 3 s scan of them."""
_hands_over: dict[tuple[str, int | None], tuple[float, bool]] = {}
_hands_over_lock = threading.Lock()


def _needs_hands_over_kept(leaving: int | None) -> bool:
    """``claude_accounts.somewhere_to_hand_over(leaving)`` for the scan: kept a minute
    (:data:`_NEEDS_HANDS_OVER_KEPT`), by the machine's accounts directory and the slot,
    and fail-open.

    It is asked only of a limited row that no reset, live manager or ``wait`` settles
    first (:func:`_needs_limited_push`). Read as ``[accounts]`` itself was, every scan
    whatever the rows said, it cost every 3 s scan a store open, the registry's
    reconcile and a parse of each account's files, and it could raise where
    ``accounts_settings`` never could: an ``OSError`` listing the slot directories was the
    project's failed read, a 503 on every action there (review of #259, round 14). A
    question that raises is a yes, as an unreadable registry is: the hand-over's worker
    says on the board why it switched nothing, and the push waits for it.
    """
    from aisquare.core import claude_accounts as claude_accounts_core
    from aisquare.services import claude_accounts as claude_accounts_service

    key = (str(claude_accounts_core.accounts_root()), leaving)
    now = time.monotonic()
    with _hands_over_lock:
        kept = _hands_over.get(key)
    if kept is not None and now - kept[0] < _NEEDS_HANDS_OVER_KEPT:
        return kept[1]
    try:
        answer = claude_accounts_service.somewhere_to_hand_over(leaving)
    except Exception as exc:
        log.debug("remote: whether slot %s can be handed over is not known: %s", leaving, exc)
        answer = True
    with _hands_over_lock:
        if len(_hands_over) > 64:  # one accounts directory a machine; bounded all the same
            _hands_over.clear()
        _hands_over[key] = (now, answer)
    return answer


_tails: dict[str, tuple[tuple[int, int], TranscriptTail | None]] = {}
_tails_lock = threading.Lock()
_TAILS_KEPT = 512


def _needs_cached_tail(path: str) -> TranscriptTail | None:
    """:func:`read_transcript_tail`, read again only when the file's size or mtime moved.

    An unchanged transcript costs one ``stat()``. Shared by the watcher and
    :func:`needs_agent_now`, and bounded: the oldest entries go first. A path the
    session named that does not exist is a conversation with nothing in it yet, as an
    empty file is (``transcript._TAIL_NOTHING``, its one spelling): Claude Code makes the
    file with the first record it writes, so a session that has had no prompt has none.
    One that cannot be read is ``None``.
    """
    try:
        stat = os.stat(path)
    except FileNotFoundError:
        return _TAIL_NOTHING
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


# --- what is kept on disk: dismissals, and first sightings --------------------------------


_NEEDS_KEPT_PARTS = ("dismissed", "first_seen")
"""The parts of ``remote-needs.json``: item id → when a dismissal was last needed, and the
watcher's memory key → when a scan first saw what it dates (:func:`_needs_dated`)."""

_NEEDS_KEPT_VERSION = 1
"""The ``version`` this aisquare writes ``remote-needs.json`` as, and the last it builds on."""


class LaterNeedsFile(OSError):
    """``remote-needs.json`` is a version this aisquare does not know (a later release's, run
    in another shell): it is never written over. An ``OSError``, so every write's caller says
    it as the write that could not be made it is: a dismissal's 503, the watcher's streak."""


def _needs_kept() -> dict[str, dict[str, str]]:
    """Each part of ``remote-needs.json``, ``key → ISO stamp``; empty where unreadable."""
    try:
        raw = _needs_kept_raw()
    except (OSError, ValueError):
        raw = None
    parts: dict[str, dict[str, str]] = {}
    for part in _NEEDS_KEPT_PARTS:
        kept = raw.get(part) if isinstance(raw, dict) else None
        pairs = kept.items() if isinstance(kept, dict) else ()
        parts[part] = {k: v for k, v in pairs if isinstance(k, str) and isinstance(v, str)}
    return parts


def _needs_kept_raw() -> object:
    """``remote-needs.json`` as its JSON; ``None`` when there is none. Raises ``OSError`` for a
    file that could not be read, ``ValueError`` for one that is not JSON, or nests deeper
    than ``json`` reads: its ``RecursionError`` escaped every reader and the writer, so each
    scan failed and the file was never moved aside (review of #259, round 6, 4/10)."""
    from aisquare.core.paths import remote_needs_path

    try:
        text = remote_needs_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        return json.loads(text)
    except RecursionError as exc:
        raise ValueError("it nests deeper than JSON is read here") from exc


def _needs_kept_whole(raw: object) -> tuple[dict[str, dict[str, str]], dict[str, object]]:
    """Each part of ``raw`` when all of it is as this version writes it, and every other key
    it holds as it is; else ``ValueError``: an object whose parts, where there, are objects of
    strings, and whose ``version``, where there, is a number. A ``version`` later than
    :data:`_NEEDS_KEPT_VERSION` is :class:`LaterNeedsFile`: a file this version cannot build
    on, and must not move aside from the release that wrote it either."""
    if raw is not None and not isinstance(raw, dict):
        raise ValueError("it is not a JSON object")
    version = None if raw is None else raw.get("version", _NEEDS_KEPT_VERSION)
    if version is not None and (type(version) is not int or version < 1):
        raise ValueError(f"its version is {version!r}, not a version")
    if version is not None and version > _NEEDS_KEPT_VERSION:
        raise LaterNeedsFile(
            f"remote-needs.json is version {version}, written by a later aisquare: it is left "
            "as it is — run Remote with that version"
        )
    parts: dict[str, dict[str, str]] = {}
    for part in _NEEDS_KEPT_PARTS:
        kept = None if raw is None else raw.get(part)
        if kept is None:
            kept = {}
        if not isinstance(kept, dict) or not all(isinstance(v, str) for v in kept.values()):
            raise ValueError(f"its {part!r} is not an object of strings")
        parts[part] = dict(kept)
    known = {"version", *_NEEDS_KEPT_PARTS}
    rest = {} if raw is None else {key: value for key, value in raw.items() if key not in known}
    return parts, rest


def _needs_keep(edit: Callable[[dict[str, dict[str, str]]], None]) -> None:
    """Write ``remote-needs.json`` again, ``edit`` applied to its parts; one writer at a time,
    and owner-only, like every Remote file.

    What is written builds on the whole file, so only a file that is not there is empty. A
    read that failed (``EIO``, ``EACCES``) is the ``OSError`` it was, and nothing is written:
    a dismissal answers 503, and the watcher tells its streak. A file this version cannot
    read in full (not JSON, a hand edit, a part of another shape) is moved aside to
    ``remote-needs.json.bad`` (or the next free ``.bad.<n>``), kept for the human, and a new
    one started. It was read as empty and replaced by the one entry being written, every
    dismissal and first sighting in it lost for good: every dismissed card back on every
    phone, and pushed again, an ongoing outage under a new id (review of #259, remote
    focus, 3/12).

    A file of a later ``version`` is :class:`LaterNeedsFile`, and left as it is. Its parts
    were read as whole when they kept this version's shape, and rewritten as ``version: 1``
    without whatever else the later release kept there, with no copy and no word (review of
    #259, round 8, 5/9). A key this version does not know, in a file of its own version, is
    written back as it was.
    """
    from aisquare.core.atomic import write_replacing
    from aisquare.core.paths import remote_needs_path

    with _dismissals_lock:
        path = remote_needs_path()
        parts, rest = _needs_kept_to_write(path)
        edit(parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        whole = {"version": _NEEDS_KEPT_VERSION, **rest, **parts}
        write_replacing(path, json.dumps(whole, indent=2), owner_only=True)


def _needs_kept_to_write(path: Path) -> tuple[dict[str, dict[str, str]], dict[str, object]]:
    """The parts of ``path`` for a write to build on, and its other keys, empty once a file it
    cannot build on was moved to ``remote-needs.json.bad``; the caller holds
    ``_dismissals_lock``. A later version's is :class:`LaterNeedsFile`, left where it is."""
    try:
        return _needs_kept_whole(_needs_kept_raw())
    except ValueError as exc:  # json's, a text that is not UTF-8, or the shape's
        aside = _needs_aside(path)
        os.replace(path, aside)
        log.warning("remote: %s could not be read (%s); it is kept as %s", path, exc, aside)
        return _needs_kept_whole(None)


def _needs_aside(path: Path) -> Path:
    """Where a ``remote-needs.json`` this version cannot build on is kept: ``.bad``, or the
    first of ``.bad.1``, ``.bad.2``… not taken. Moved over the one before, the second file
    put aside took the first one's dismissals and first sightings with it, the copy kept
    for the human lost (review of #259, round 8, 8/9). The caller holds the lock every
    writer of the file takes."""
    aside = path.with_name(f"{path.name}.bad")
    n = 0
    while aside.exists() or aside.is_symlink():
        n += 1
        aside = path.with_name(f"{path.name}.bad.{n}")
    return aside


def load_needs_dismissals() -> dict[str, str]:
    """Item id → when a phone dismissed it, or a scan last found it hidden by that, from
    ``remote-needs.json``; ``{}`` when unreadable."""
    return _needs_kept()["dismissed"]


def record_needs_dismissal(item_id: str) -> None:
    """Remember that ``item_id`` was dismissed, so no later scan shows it again.

    Dismissals no scan has needed for 7 days are dropped, and at most the newest 500
    are kept, so the file stays small whatever a phone does. A scan that finds a
    dismissed item still there dates its dismissal again (:func:`_needs_still_hidden`):
    a pane that stays lost, an agent idle at its question, keep their ids for as long as
    they last, and a dismissal dropped a week after it was made brought the card back
    to every phone at the next one written (review of #243, sweep 3).
    """
    from aisquare.services.remote_server import _iso_seconds, _remote_instant

    now = _needs_now()

    def dismissed(parts: dict[str, dict[str, str]]) -> None:
        kept: dict[str, datetime] = {}
        for key, stamp in parts["dismissed"].items():
            when = _remote_instant(stamp)
            if when is not None and now - when <= _DISMISSALS_AGE:
                kept[key] = when
        kept[item_id] = now
        newest = sorted(kept.items(), key=lambda pair: pair[1])[-_DISMISSALS_KEEP:]
        parts["dismissed"] = {k: _iso_seconds(v) for k, v in newest}

    _needs_keep(dismissed)


def _needs_still_hidden(item_ids: Collection[str], now: datetime) -> None:
    """Date the dismissals of ``item_ids`` ``now``: a scan found their items still there."""
    from aisquare.services.remote_server import _iso_seconds

    def seen(parts: dict[str, dict[str, str]]) -> None:
        for key in item_ids:
            if key in parts["dismissed"]:
                parts["dismissed"][key] = _iso_seconds(now)

    _needs_keep(seen)


def _needs_first_seen_kept() -> dict[str, datetime]:
    """The first sightings the watcher kept, for the next watcher and ``asq remote needs``."""
    from aisquare.services.remote_server import _remote_instant

    kept = {key: _remote_instant(stamp) for key, stamp in _needs_kept()["first_seen"].items()}
    return {key: when for key, when in kept.items() if when is not None}


def _needs_stamps(memory: Mapping[str, datetime]) -> dict[str, str]:
    from aisquare.services.remote_server import _iso_seconds

    return {key: _iso_seconds(when) for key, when in memory.items()}


# --- the watcher --------------------------------------------------------------------------


class RemoteNeedsWatcher:
    """The scanner: a daemon thread scanning every ``interval`` while anyone would hear of it.

    That is a page with its socket open, or a device with a push subscription, signed
    in or signed out (:meth:`_needs_heard`); a page that reads the feed with no socket
    scans for itself (:meth:`needs_scan_wanted`). Each scan replaces the latest
    snapshot at once and then calls every listener in ``kit.needs_listeners`` with
    ``(all items, scanned_at)``; a listener that raises is logged and the rest are
    called. Readers (the stream, the heartbeat, the routes, the push sender) take the
    snapshot under a lock and do no I/O.
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
        self._scanning = threading.RLock()
        """One scan at a time: the watcher's own, a route's, the one after a quick answer."""
        self._scans_told = 0
        """How many scans :meth:`_needs_scan_told` has finished, and ``_scan_failure`` how the
        last one ended: what a request that waited for another's scan reads instead of its
        own (:meth:`needs_scan_for_request`)."""
        self._scan_failure: Exception | None = None
        self._heard = False
        """Whether the thread's last turn found anyone to scan for (:meth:`_needs_heard`)."""
        self._latest: list[NeedsItem] = []
        self._latest_json: list[dict[str, object]] = []
        self._scanned_at: datetime | None = None
        self._projects: dict[str, ProjectInfo] = {}
        self._first_seen: dict[str, datetime] = {}
        self._first_seen_kept: dict[str, str] | None = None
        """What ``remote-needs.json`` holds of ``_first_seen``; ``None`` until the first scan
        reads it."""
        self._forgotten: set[str] = set()
        """Ids :meth:`needs_forget` dropped that no scan has read from the dismissals file yet."""
        self._answered: dict[str, float] = {}
        """Ids :meth:`needs_answered` dropped, each with when (``time.perf_counter()``), until
        a scan that began :data:`NEEDS_RESCAN_AFTER_ANSWER` after it publishes. That is the
        clock the rescan's timer waits by: on Windows before Python 3.13 ``time.monotonic``
        steps by the system tick, and one of 15.6001 ms read the timer's whole second as
        0.998 s, so the scan it began kept a prompt the keys left up hidden till the next."""
        self._failing: set[object] = set()
        """What has failed since it last worked, the scan itself (``"scan"``) or a listener,
        and ``_failing_projects`` the projects whose part of it has: each streak is told once
        (:func:`_needs_failed`)."""
        self._failing_projects: set[str] = set()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._rescan: threading.Timer | None = None
        """The scan after the latest quick answer, until it runs (:meth:`needs_rescan_soon`)."""

    def start_watching(self) -> None:
        """Start the daemon thread ``asq-remote-needs``; its first scan runs at once."""
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._needs_loop, name="asq-remote-needs", daemon=True
            )
            self._thread.start()

    def stop_watching(self) -> None:
        """Stop the thread, waiting a few seconds for a scan in progress. The scan after a
        quick answer that has yet to run never does."""
        self._stopping.set()
        with self._lock:
            rescan, self._rescan = self._rescan, None
        if rescan is not None:
            rescan.cancel()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5.0)

    def needs_watching(self) -> bool:
        """Whether the thread is scanning (a lifespan started it)."""
        return self._thread is not None and self._thread.is_alive()

    def _needs_loop(self) -> None:
        while not self._stopping.is_set():
            self._heard = self._needs_heard()
            if self._heard:
                self._needs_scan_told()
            self._stopping.wait(self._interval)

    def _needs_scan_told(self) -> Exception | None:
        """:meth:`scan_needs_now`, its failure told once a streak, and its recovery once; the
        failure, or ``None``."""
        with self._scanning:
            failure: Exception | None = None
            try:
                self.scan_needs_now()
            except Exception as exc:
                _needs_failed("the needs scan", exc, self._failing, "scan")
                failure = exc
            else:
                if "scan" in self._failing:
                    self._failing.discard("scan")
                    log.info("remote: the needs scan works again")
            self._scans_told += 1
            self._scan_failure = failure
            return failure

    def needs_scan_for_request(self) -> Exception | None:
        """A scan for a request that has nothing fresh to read, unless one finished while it
        waited for the scan before it: how that one ended, a failure or ``None``.

        Each request made before the first scan ran a scan of its own, one after another,
        each re-reading every project though one had just published, and each holding a
        thread of the shared pool meanwhile: a page sends two on loading and one on each
        wake, and five early requests over a 2 s fleet were answered at 2, 4, 6, 8 and
        10 s, a turn of Remote off waiting behind them (sweep 4 of #243). Now the requests
        that waited share the scan they waited for.
        """
        told = self._scans_told
        with self._scanning:
            if self._scans_told != told:
                return self._scan_failure
            return self._needs_scan_told()

    def _needs_heard(self) -> bool:
        """Whether a scan reaches anyone: a page's socket is open (its ``needs_you`` frames
        and heartbeat), or a device the runtime still has holds a push subscription (the
        push sender's, signed out or not), and Remote is not past its auto-off deadline,
        from which on every request is a 404 and every socket closed, whatever turns Remote
        off has yet to run.

        Any device on record was enough: one that signed out, or never turned
        notifications on, has no socket and gets no push, and a phone unlocked once and
        closed cost a scan of every project every 3 s for the rest of its 7 days, the
        result read by nobody (review of #243, round 6). A push file that cannot be read
        is taken for a subscription: the sender may yet send.
        """
        if self._needs_off():
            return False
        if self._kit.sockets:
            return True
        listening = getattr(self._kit.lane_state.get("push"), "push_listening", None)
        if listening is None:
            return False
        try:
            return bool(listening())
        except Exception:
            log.debug("remote: could not tell whether a phone has notifications on", exc_info=True)
            return True

    def _needs_off(self) -> bool:
        """Whether Remote is past its auto-off deadline or turning off, or cannot say: a scan
        then reaches nobody, every request a 404 and every socket closed."""
        try:
            return self._kit.runtime.auto_off_passed(self._clock())
        except Exception:
            return True

    def needs_scan_wanted(self) -> bool:
        """Whether a read of the feed must scan for itself: no thread scans (no lifespan),
        none has scanned yet, or the thread scans for nobody (:meth:`_needs_heard`) and the
        latest scan is older than its interval, as a page with no socket finds it."""
        scanned = self.needs_scanned_at()
        if scanned is None or not self.needs_watching():
            return True
        stale = (self._clock() - scanned).total_seconds() >= self._interval
        return stale and not self._heard

    def scan_needs_now(self) -> list[NeedsItem]:
        """One synchronous scan: the snapshot replaced, then every listener called.

        The dismissals are read before the scan starts, and a scan takes as long as
        its projects' tmux and store reads do. A card dismissed meanwhile is not in
        what it read, and :meth:`needs_forget` drops it only from the snapshot there
        is then: published, the scan put it back on every phone and before the push
        sender, which could push it. So each id dropped since is dropped from what the
        scan publishes too, until a scan that read it from the file has published. A card
        answered (:meth:`needs_answered`) is, until a scan that began long enough after the
        answer for the agent to have acted on it publishes, whatever that scan finds.
        """
        with self._scanning:
            began = time.perf_counter()
            now = self._clock()
            sources = self._sources()
            projects = sources.list_projects()
            if self._first_seen_kept is None:
                kept = _needs_first_seen_kept()
                self._first_seen = {**kept, **self._first_seen}
                self._first_seen_kept = _needs_stamps(kept)
            dismissed = load_needs_dismissals()
            everything = scan_needs_you(
                replace(sources, list_projects=lambda: projects),
                now=now,
                dismissed=(),
                first_seen=self._first_seen,
                failing=self._failing_projects,
                kept=self.needs_items_now(),
            )
            scanned = [item for item in everything if item.id not in dismissed]
            with self._lock:
                self._answered = {
                    key: at
                    for key, at in self._answered.items()
                    if began - at < NEEDS_RESCAN_AFTER_ANSWER
                }
                hidden = self._forgotten.union(self._answered)
                items = [item for item in scanned if item.id not in hidden]
                self._forgotten.difference_update(dismissed)
                payload = [item.needs_item_json() for item in items]
                self._latest, self._latest_json, self._scanned_at = items, payload, now
                self._projects = {project.id: project for project in projects}
            for listener in list(self._kit.needs_listeners):
                try:
                    listener(list(items), now)
                except Exception as exc:
                    _needs_failed("a needs listener", exc, self._failing, listener)
                else:
                    self._failing.discard(listener)
            self._needs_keep_what_it_saw(everything, dismissed, now)
        return items

    def _needs_keep_what_it_saw(
        self, items: Sequence[NeedsItem], dismissed: Mapping[str, str], now: datetime
    ) -> None:
        """Keep on disk what a restart must not lose: the first sightings that date items, and
        the dismissals of items still there, dated now once a day.

        The watcher's memory of when it first saw tmux stop answering is ``fleet_down``'s
        id. Kept in memory alone, every Remote toggle, TUI restart or ``serve`` restart
        made the same outage a new item: its dismissal lost, the phone pushed again, and
        ``asq remote needs`` gave it an id of its own each run (review of #243, sweep 3).
        """
        from aisquare.services.remote_server import _remote_instant

        stale = now - _DISMISSALS_REDATED
        hidden = [
            item.id
            for item in items
            if item.id in dismissed
            and ((when := _remote_instant(dismissed[item.id])) is None or when <= stale)
        ]
        stamps = _needs_stamps(self._first_seen)
        try:
            if hidden:
                _needs_still_hidden(hidden, now)
            if stamps != self._first_seen_kept:

                def first_seen(parts: dict[str, dict[str, str]]) -> None:
                    parts["first_seen"] = stamps

                _needs_keep(first_seen)
                self._first_seen_kept = stamps
        except OSError as exc:
            _needs_failed("keeping what the needs scan saw", exc, self._failing, "kept")
        else:
            self._failing.discard("kept")

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
            self._needs_drop(item_id)

    def needs_answered(self, item_id: str) -> None:
        """A quick answer was typed: its card leaves the feed now, and a scan follows
        (:meth:`needs_rescan_soon`).

        It stayed in the snapshot until that scan, a second on, and a scan already under
        way published it again: the push sender, whose window closed meanwhile, pushed
        the answered prompt to every phone, the one that answered it too, and each page
        kept its live buttons (review of #243, sweep 5). Unlike a dismissal nothing is
        kept on disk: a prompt the keys did not close is back with the first scan that
        began :data:`NEEDS_RESCAN_AFTER_ANSWER` after the answer, by when the agent has
        acted on them.
        """
        with self._lock:
            self._answered[item_id] = time.perf_counter()
            self._needs_drop(item_id)
        self.needs_rescan_soon()

    def _needs_drop(self, item_id: str) -> None:
        """Take ``item_id`` out of the snapshot; the caller holds ``_lock``."""
        self._latest = [item for item in self._latest if item.id != item_id]
        self._latest_json = [item for item in self._latest_json if item.get("id") != item_id]

    def needs_rescan_soon(self) -> None:
        """Scan again :data:`NEEDS_RESCAN_AFTER_ANSWER` seconds after the latest quick answer,
        off every caller's thread: one scan for however many answers came meanwhile.

        Each answer started a timer of its own, each a scan of every project with tmux:
        five quick taps were five scans back to back, as the scans for requests were
        before they shared one (sweep 4), and the timers of the last answers before the
        watcher stopped, or Remote went off, scanned after it (review of #243, sweep 5).
        """
        with self._lock:
            if self._stopping.is_set():
                return
            if self._rescan is not None:
                self._rescan.cancel()
            self._rescan = threading.Timer(NEEDS_RESCAN_AFTER_ANSWER, self._needs_rescan)
            self._rescan.daemon = True
            self._rescan.start()

    def _needs_rescan(self) -> None:
        """The armed timer's scan; none for a timer a later answer replaced (one that fired as
        it was cancelled), once the watcher stopped, or once Remote is off."""
        with self._lock:
            if self._rescan is not None and self._rescan is not threading.current_thread():
                return
            self._rescan = None
        if self._stopping.is_set() or self._needs_off():
            return
        self._needs_scan_told()


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
    would be a Ctrl-C that no key allowlist saw and the audit line could not show. Both
    is what ``send-keys`` calls both, keys given and words given: an empty ``keys`` next
    to words, from a client that always sends the field, gives no key to lose the order
    of. Refused as both, it was told it sent what it had not.
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
    keys = [] if raw_keys is None else check_remote_key_names(raw_keys)
    if keys and raw_text:
        raise RequestError(
            400, "text_and_keys", "send 'keys' or 'text', not both: the order would be lost"
        )
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


def _needs_send(status: FleetAgentStatus, keys: Sequence[str], text: str, enter: bool) -> PaneWrite:
    """Type the answer into the pane of ``status``'s row: the keys, or the text, then Enter if
    asked; how it ended, the first write that did not go through (``fleet.PaneWrite``). On
    the server whose screen :func:`needs_prompt_now` just read (:func:`_needs_pane_server`):
    through ``fleet.server_of`` each answer read ``config.toml`` under the agent's lock, and
    the screen checked and the server typed into came of two workings-out of the row's
    address (review of #259, round 10, 7/9)."""
    from aisquare.services import fleet as fleet_service

    server, pane = _needs_pane_server(status), status.agent.pane_id
    return fleet_service.pane_writes(
        ("the answer", lambda: server.send_keys(pane, *keys)) if keys else None,
        ("the answer", lambda: server.send_literal(pane, text)) if text else None,
        ("Enter", lambda: server.send_keys(pane, "Enter")) if enter else None,
    )


def _needs_answer_now(
    kit: RemoteKit,
    watcher: RemoteNeedsWatcher,
    device: Device,
    item: NeedsItem,
    project: ProjectInfo,
    keys: list[str],
    text: str,
    enter: bool,
) -> tuple[dict[str, object], str]:
    """Type a quick answer into the agent ``item`` is about, in a thread of the write pool:
    its response and its audit summary, or :class:`RequestError`.

    Under the agent's action lock, taken without waiting (409 ``busy``). The agent is
    re-derived first (:func:`needs_agent_now`), and a card no longer current is a 409
    ``stale`` carrying what is current instead. The gates are asked again right before
    typing (:meth:`RemoteKit.kit_write_still_allowed`): the re-derivation came between
    them and the keys. So is the screen, last (:func:`needs_prompt_now`): a card about
    one of several calls pending is answered only while the prompt up is still its call's,
    and otherwise is a 409 ``stale``, out of the feed until the next scan; a screen tmux
    could not show is a 503 ``fleet_unavailable``, and the card stays. A tmux that fails
    mid-answer is a 503 whose ``audit`` says it was tried, as part of it may have reached
    the pane; one that timed out may still type all of it (``fleet.PaneWrite``), so its card
    leaves the feed as an answered one does, and its line says ``delivered=maybe``. So does
    one whose answer went in and whose Enter did not, which says what waits at the prompt
    (``fleet.pasted_unsent``), as ``send-keys`` does: it was "tmux could not type it", the
    card live, and a second tap typed the answer again beside the first, the next Enter
    sending both (review of #259, round 12, 1/9).

    The card leaves ``watcher``'s feed as soon as the keys are typed, here on the write
    thread (:meth:`RemoteNeedsWatcher.needs_answered`). It left once the audit line was written,
    after an await a scan could publish in: the answered card back on every page with
    live buttons, pushed if its window had just closed, and a question's or a plan's
    second tap typed again, those cards being current with no ``attention`` until the
    agent writes its result (review of #259, remote focus, 2/12).
    """
    from aisquare.services import fleet as fleet_service
    from aisquare.services.remote_server import RequestError, _audit_keys, remote_agent_held

    label = item.agent or ""
    with remote_agent_held(project.id, label):
        try:
            snap = needs_agent_now(project, label)
        except fleet_service.NoSuchAgent:
            raise RequestError(409, "stale", f"{label} is gone", current=[]) from None
        except RequestError:  # a read its scan needs failed: its own 503, as it is
            raise
        except Exception as exc:  # a bug: as a failed scan is, not a bare 500
            log.warning("remote: a needs answer could not read %s: %s", label, exc)
            raise RequestError(503, "unavailable", str(exc)) from None
        if not needs_item_current(snap, item.id):
            current = [now_item.needs_item_json() for now_item in snap.items]
            gone = f"{label} no longer shows that {item.kind}"
            raise RequestError(409, "stale", gone, current=current)
        if snap.status is None or not snap.pane_is_agent:
            why = f"{label}'s pane is not running the agent — nothing was sent"
            raise RequestError(409, "not_agent", why)
        summary = (
            f"answer {item.id} {item.kind} {label}@{project.id} keys={_audit_keys(keys)} "
            f"text={len(text)}ch enter={enter}"
        )
        kit.kit_write_still_allowed(device)
        shown = needs_prompt_now(snap, item, _needs_now())
        if shown is None:
            # Unknown, not another prompt: the card stays in the feed, and nothing is typed.
            said = (
                f"tmux could not show {label}'s screen, to check that its prompt is still "
                f"that {item.kind}'s — nothing was sent; try again"
            )
            raise RequestError(503, "fleet_unavailable", said)
        if item.id not in {one.id for one in shown}:
            # Out of the feed as an answered card is, until the scan after it says what is up.
            watcher.needs_answered(item.id)
            instead = f"another prompt than that {item.kind}" if shown else "no prompt"
            gone = f"{label} shows {instead} now — nothing was sent"
            current = [one.needs_item_json() for one in shown]
            raise RequestError(409, "stale", gone, current=current)
        wrote = _needs_send(snap.status, keys, text, enter)
        if wrote.typed == "no":
            said = f"tmux could not type it: {wrote.error}"
            raise RequestError(503, "fleet_unavailable", said, audit=f"{summary} failed")
        if wrote.typed != "yes":
            # In the pane, or on its way: a wedged server may still type it once it recovers,
            # and a card left live meanwhile typed its digit again (review of #259, round 8,
            # 2/9); an answer whose Enter did not go waits at the prompt (round 12, 1/9).
            watcher.needs_answered(item.id)
            said = wrote.said(label, press="press Enter on the pad", again="answer again")
            raise RequestError(503, "fleet_unavailable", said, audit=f"{summary} {wrote.audited}")
        watcher.needs_answered(item.id)
        return {"answered": item.id, "agent": label, "project": project.id, "sent": True}, summary


def needs_routes(kit: RemoteKit) -> list[BaseRoute]:
    """``GET api/needs``, ``POST api/needs/dismiss``, ``POST api/needs/answer`` (SPEC §1.3)."""
    import asyncio

    from starlette.responses import JSONResponse

    from aisquare.services.remote_server import RequestError, _remote_write_tracked

    async def needs_scanned_here(watcher: RemoteNeedsWatcher) -> Response | None:
        """One scan for a request that has none to read; ``None``, or the 503 its failure is.

        A scan that raised, over a store that cannot be opened, answered a bare 500
        ``text/plain`` and printed its traceback on the terminal, where every other read
        answers 503 ``unavailable`` in JSON and the watcher's own scans are told once a
        streak (review of #243, sweep 3).
        """
        failed = await asyncio.to_thread(watcher.needs_scan_for_request)
        return None if failed is None else kit.kit_refuse(503, "unavailable", str(failed))

    async def needs_list_endpoint(
        request: Request, device: Device, body: dict[str, Any]
    ) -> Response:
        """The feed. A watcher that does not scan for anyone, or is not running (no
        lifespan), scans for this request (:meth:`RemoteNeedsWatcher.needs_scan_wanted`)."""
        watcher = _needs_watcher(kit)
        wanted = watcher.needs_scan_wanted()
        if wanted and (failed := await needs_scanned_here(watcher)) is not None:
            return failed
        return JSONResponse(watcher.needs_payload_now())

    async def needs_dismiss_endpoint(
        request: Request, device: Device, body: dict[str, Any]
    ) -> Response:
        """Hide one card for good. Not write-gated: it changes what is shown, not the fleet.
        A dismissal that could not be saved is a 503, and the card stays: hidden here
        alone, it came back with the next start."""
        item_id = _needs_id_field(body)
        watcher = _needs_watcher(kit)
        unread = watcher.needs_scanned_at() is None
        if unread and (failed := await needs_scanned_here(watcher)) is not None:
            return failed
        found = watcher.needs_lookup(item_id)
        if found is None:
            return kit.kit_refuse(404, "not_found", "no such item in the needs feed")
        item, _project = found
        try:
            await asyncio.to_thread(record_needs_dismissal, item.id)
        except OSError as exc:
            log.warning("remote: a needs dismissal could not be saved: %s", exc)
            return kit.kit_refuse(503, "unavailable", f"the dismissal could not be saved: {exc}")
        watcher.needs_forget(item.id)
        summary = f"{item.id} {item.kind} {item.agent or '-'}@{item.project_id}"
        await asyncio.to_thread(kit.kit_audit, device, "needs/dismiss", summary)
        return JSONResponse({"dismissed": item.id})

    async def needs_answer_endpoint(
        request: Request, device: Device, body: dict[str, Any]
    ) -> Response:
        """Answer a card on the agent it is about, only while the card is still true.

        The item comes from the latest scan; the rest is a write like every other
        (:meth:`RemoteKit.kit_run_write`), on the write pool, counted among the device's
        writes waiting and among the writes running: the agent re-derived under its action
        lock (:func:`_needs_answer_now`), and typed into. It ran on the shared pool, its
        re-derivation a scan of the whole project with tmux, while its lock was held: with
        tmux not answering, each tap held a thread of the pool every read, frame and
        unlock waits on, past the per-device cap, and a quit or a Remote off that came
        meanwhile did not name it (review of #243, round 6).
        """
        arrived = time.monotonic()
        item_id, keys, text, enter = _needs_answer_body(body)
        watcher = _needs_watcher(kit)
        unread = watcher.needs_scanned_at() is None
        if unread and (failed := await needs_scanned_here(watcher)) is not None:
            return failed
        found = watcher.needs_lookup(item_id)
        if found is None:
            return kit.kit_refuse(409, "stale", "that card no longer needs you", current=[])
        item, project = found
        if item.kind not in _NEEDS_ANSWERABLE or item.agent is None:
            board = item.kind in _NEEDS_BOARD_KINDS
            why = "reply on the board instead" if board else f"a {item.kind} card takes its actions"
            return kit.kit_refuse(400, "not_answerable", why)

        def answer_now(_named: dict[str, Any]) -> tuple[dict[str, object], str]:
            return _needs_answer_now(kit, watcher, device, item, project, keys, text, enter)

        tracked = _remote_write_tracked("needs/answer", answer_now)
        try:
            result, summary = await kit.kit_run_write(
                tracked, {"agent": item.agent}, arrived, device
            )
        except RequestError as exc:
            if exc.audit is not None:  # part of it may have reached the pane
                await asyncio.to_thread(kit.kit_audit, device, "needs/answer", exc.audit)
            raise
        await asyncio.to_thread(kit.kit_audit, device, "needs/answer", summary)
        return JSONResponse(result)

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
    items = scan_needs_you(
        live_needs_sources(),
        now=now,
        dismissed=load_needs_dismissals(),
        first_seen=_needs_first_seen_kept(),  # the phone's dates and ids, not this run's
    )
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
    "needs_prompt_now",
    "needs_push_safe",
    "needs_routes",
    "needs_scanned_iso",
    "needs_single_agent_now",
    "needs_tool_pending",
    "needs_unseen",
    "needs_ws_frames",
    "record_needs_dismissal",
    "scan_needs_you",
    "start_needs_watch",
]
