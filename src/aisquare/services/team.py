"""The agent orchestrator: shared working memory for parallel agent sessions.

One project = one board. Sessions are registered automatically by the Claude
Code hooks (``hook_*`` functions below); agents talk to the orchestrator through the
``team``/``task``/``note``/``board`` commands. Every mutation appends a
:class:`TeamEvent` to the pipe, and each session receives the events it has
not yet seen as a compact delta on its next prompt.

Activation is deliberate: hooks stay silent in a project until a session is
launched with ``AISQUARE_ROLE`` set or someone runs ``aisquare team on`` —
so repos that never opted in never see team output.
"""

from __future__ import annotations

import enum
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import unicodedata
from collections.abc import Callable, Collection, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import EllipsisType
from typing import Any, Final, NamedTuple

from aisquare.core import brain, experimental, harness, insights, orchestrator, workspace
from aisquare.core import claude_accounts as claude_accounts_core
from aisquare.core.config import FleetSettings, load_config
from aisquare.core.ids import new_event_id, new_task_id
from aisquare.core.store import AmbiguousIdError, ContextStore, store_session, unmet_needs
from aisquare.core.tmux import TmuxError
from aisquare.models import (
    CLOSED_STATUSES,
    FleetAgent,
    ProjectInfo,
    TaskStatus,
    TeamEvent,
    TeamSession,
    TeamTask,
)
from aisquare.services import distill as distill_service
from aisquare.services import install_route
from aisquare.services import project as project_service

#: The fail-open reads of the fleet row say here what they swallowed. Nothing
#: in the CLI configures logging, so a WARNING reaches stderr through the
#: logging module's last-resort handler — a hook's stderr is Claude Code's hook
#: log — as one line: a traceback is what an operator must never be handed for
#: a preference that stopped working.
_log = logging.getLogger(__name__)

_SHORT_ID = 8
_DELTA_LIMIT = 10
_BOARD_TASKS = 8
_BOARD_EVENTS = 5
_STALE_AFTER = timedelta(minutes=30)

#: How long a session must be silent before its IN-PROGRESS CLAIMS are returned
#: to the pool — deliberately far longer than ``_STALE_AFTER`` (#49).
#:
#: Retiring presence and orphaning work are different decisions with different
#: costs. For an AGENT, thirty minutes of silence is not idleness; it is one
#: long tool call — a multi-PR review, a build, a fan-out of subagents — and
#: nothing on the board distinguishes that from a crashed terminal. Retiring
#: presence too eagerly is self-healing: the next heartbeat re-registers the
#: session. Releasing a claim too eagerly is not: a second agent picks up work
#: the first is still doing, and both push.
#:
#: So presence goes at ``_STALE_AFTER`` and claims wait for this. A caller that
#: genuinely knows the session is dead passes ``release_claims=True``.
_CLAIM_ORPHAN_AFTER = timedelta(hours=4)

MANAGER_ROLE = "manager"
"""The one role whose ``Stop`` hook may keep it going (docs/plans/fleet-tui.md §7.3)."""

CAPTAIN_ROLE = "captain"
"""The home-level captain's role (services.captain): briefed by :func:`_captain_briefing`."""

_COMPACT_SOURCE = "compact"
"""The ``SessionStart`` source of a compaction — the one start that can come mid-turn."""

CLEAR_REASON = "clear"
"""Claude Code's ``SessionEnd`` reason for ``/clear``: the session id ends, the process
does not — and the ``SessionStart`` of the id that follows comes AFTER this end
(measured on 2.1.272). See rule 2 in the fleet-row section below."""

HANDOVER_STATE = "switching"
"""The ``team_session.state`` ``fleet switch`` sets before it ``/exit``s an agent that is
about to start again under another account (#146), and ``fleet restart`` before it
``/exit``s a running one (#163): its ``SessionEnd`` then parks the claims, as a
``/clear`` does, instead of releasing them (review of #205, finding 6) — for the SAME
id when the replacement resumes the session, for the replacement's new id to take over
when it starts fresh (fourth round). Transient — a resumed session's start hook writes
``working`` over it, a fresh start retires the old presence — and unknown to
``fleet._derive``, which falls back to the pane. The agent's own hooks between the
``/exit`` and its end — the turn it finishes first, a notification, a failed turn —
leave it where it is (review of #163, round 2). Besides that start and the command
taking it back, no state write replaces it: the store keeps it
(``SqliteStore.touch_session``; review of the #205 fold, round 1). So ``switch`` and
``restart`` take it back as soon as their ``stop`` returns or raises — the one reader,
the old process's ``SessionEnd``, has run by then or never will (round 2)."""

#: A numbered SEAT: a first-class role with a crew index glued on — ``coder1``,
#: ``reviewer2``. ``cli/launch.py`` accepts these because crews run several agents
#: in one role and need them apart on the board; the base is derived by stripping
#: the digits rather than matched against a second copy of the role list, so the


def base_role(role: str) -> str:
    """The first-class role a numbered seat belongs to — ``coder1`` → ``coder``.

    A one-line delegate to :func:`aisquare.core.harness.base_role`, which is the
    one home for the rule: the harness itself now keys its profile lookups on
    it, so a seat gets its role's cycle, ladder and effort offset. Kept as a
    name here because this module's callers read as board logic, not harness
    plumbing.
    """
    return harness.base_role(role)


#: The board events worth waking a manager for: a sub-agent's verdict or hand-off
#: (``task_review``, ``task_done``), a task that needs the manager back
#: (``task_blocked``, ``task_reopened``), a result or a question on the board, and
#: an agent that went away (``agent_exited`` is the fleet's, emitted wherever a
#: fleet row ends: ``fleet.stop`` — and so ``fleet shutdown`` — and ``fleet.reap``).
#: A plain note, a decision or a claim is news, not a decision the manager has to
#: make — it arrives with the next prompt's delta like everyone else's.
MANAGER_WAKE_KINDS: frozenset[str] = frozenset(
    {
        "task_review",
        "task_done",
        "task_blocked",
        "task_reopened",
        "result",
        "question",
        "agent_exited",
        # #146: an agent parked on a usage limit needs a decision (switch it, or
        # wait) — unless the automatic hand-over is moving it, which is its own kind,
        # `LIMITED_HANDING_OVER`, and no wake (#277). A hand-over that moved one is
        # news, not a decision: `switched` arrives with the next delta. One that could
        # not is the manager's to act on: `handover_failed`.
        "limited",
        "handover_failed",
        # P0-W1a (#122): an agent whose first message never started a turn sits idle
        # until someone tells it again or restarts it.
        "prompt_unsent",
    }
)

#: The kind of the usage-limit line when the automatic hand-over is about to move the agent
#: (``on_limit = "switch"``): the manager has nothing to do, so it is no wake kind. Woken by
#: a ``limited`` line, a manager following its cycle switched the agent too — once the
#: hand-over had finished, it moved the replacement again (#277). Its own KIND, not a
#: phrase in the text: a reworded line must not change who is woken (review of #304).
LIMITED_HANDING_OVER = "limited_handing_over"
#: Every kind a usage-limit line is written as, for the readers that show one.
LIMITED_KINDS: frozenset[str] = frozenset({"limited", LIMITED_HANDING_OVER})
#: What that line says of the hand-over.
HANDING_OVER_SAID = "moving it to another account automatically"

#: Event kinds written for the HUMAN board only: the 🔔 transition and a Claude
#: Code notification's feed line (#153). They never reach a teammate's prompt
#: delta or a manager's wake-up reason — no agent can answer another's permission
#: prompt, and each one would take a ``_DELTA_LIMIT`` slot from the real news.
#: ``captain_action`` is the captain's audit line (``services.captain.actions``):
#: one per tool call, reads included, for the owner — the effects it records
#: (a note, a claim, a tell) reach teammates as their own events.
#:
#: No agent-facing reader shows any of them: not the delta or the wake-up, and not
#: the recent updates of a briefing (:func:`_briefing_events`) or of the MCP
#: ``team_board``. Each leaves them out IN its query, before the LIMIT (the store's
#: ``exclude_kinds``). Dropped after it, or not at all, a few questions to the
#: captain briefed the next coder with five audit lines and no decision, and a run
#: of 62 of them hid a coder's result from a waiting manager's wake-up (review of
#: #240, finding 11).
HUMAN_BOARD_KINDS: frozenset[str] = frozenset({"attention", "notice", "captain_action"})

#: The one of them the human board's own recent list leaves out too (``aisquare
#: board``, its watch): the audit is one line per captain tool call, with the tool's
#: arguments and the owner's words, and it filled that list. The bell and the notices
#: stay, they are that board's own; ``aisquare captain log`` reads the audit.
CAPTAIN_AUDIT_KINDS: frozenset[str] = frozenset({"captain_action"})

#: The last sentence of every wake-up reason. Claude Code continues the turn with
#: the reason as its instruction, so the instruction must license stopping — a
#: reason that only says "here is news" is an invitation to loop.
WAKEUP_CLOSE = "If nothing needs you, stop."


def _addressed_to_manager(to_role: str | None) -> bool:
    """Whether a board write is addressed to the manager (``--to manager``).

    The fleet's manager label and the manager role are the same word
    (``services.fleet.MANAGER_LABEL``), so one comparison covers both
    ``note --to manager`` and ``fleet tell manager``'s board-note fallback.
    Case- and space-insensitive because the CLI passes through whatever the
    operator typed.
    """
    return (to_role or "").strip().lower() == MANAGER_ROLE


def _wakes_manager(event: TeamEvent) -> bool:
    """Whether one board event is something the manager has to come back for (§7.3).

    Two ways in. Its KIND is one of :data:`MANAGER_WAKE_KINDS` — a verdict, a
    hand-off, a result, a question, an exit. Or it is ADDRESSED to the manager:
    §7.3 path 2 counts ``note --to manager`` as wake-worthy, and a mailbox that
    delivers only while the recipient happens to be idle is not a mailbox. The
    same note written while the manager is WORKING has no other path — path 2
    refuses a working manager by design, and 'note' is not a wake kind — so
    without this it would sit past the cursor until an unrelated event arrived.
    Unaddressed news (a plain note, a decision, a claim) still waits its turn.
    """
    return event.kind in MANAGER_WAKE_KINDS or _addressed_to_manager(event.to_role)


class TeamDisabledError(RuntimeError):
    """Raised when a team command runs with the orchestrator disabled (AISQUARE_TEAM=0)."""

    def __init__(self) -> None:
        super().__init__("the agent orchestrator is disabled (AISQUARE_TEAM=0)")


class ClaimLostError(RuntimeError):
    """Raised when a claim attempt loses to another session."""

    def __init__(self, task: TeamTask) -> None:
        holder = short_id(task.claimed_by) if task.claimed_by else "another session"
        super().__init__(f"task {task.id} is already claimed by {holder}")
        self.task = task


class DeliveryUnconfirmedError(RuntimeError):
    """A committed write could not be read back from a fresh store connection.

    Raised instead of returning success: a ✓ the store cannot corroborate is
    exactly the lying-success failure (#20) this read-back retires.
    """

    def __init__(self, ref: str, board_name: str) -> None:
        super().__init__(
            f"write {ref} was not confirmed on board {board_name} — "
            "not reporting success; check `aisquare log` before retrying"
        )
        self.ref = ref
        self.board_name = board_name


class NoSuchTaskNumber(KeyError):
    """``12`` / ``#12`` names no task on the caller's board — said so, never looked for
    elsewhere."""

    def __init__(self, number: int, board: str) -> None:
        super().__init__(f"#{number}")
        self.number = number
        self.board = board

    def __str__(self) -> str:
        return f"no task #{self.number} on this board ({self.board})"


class ManagerWakeupError(RuntimeError):
    """The manager's wake-up branch failed; the row still says ``waiting``, as before.

    Raised only AFTER the session is marked waiting, so the CLI can swallow it and
    report the wake-up's own cost line ("the manager will not be woken by this
    turn's board updates") instead of the generic Stop line — which would be a
    wrong sentence: the board does show the session as waiting.
    """

    def __init__(self, cause: Exception) -> None:
        super().__init__(f"manager wake-up failed ({type(cause).__name__}: {cause})")
        self.cause = cause


@dataclass(frozen=True)
class StopDecision:
    """What a manager's ``Stop`` hook tells Claude Code: keep going, and why (§7.3).

    Printed by ``aisquare hook stop`` as the top-level ``{"decision": "block",
    "reason": …}`` object — the shape the hooks reference documents for Stop and
    SubagentStop (re-checked 2026-08-28: "Other events like PostToolUse and Stop
    continue to use top-level `decision` and `reason`"). The same reference
    offers ``{"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext":
    …}}``, which continues the conversation too but is labelled "Stop hook
    feedback" in the transcript rather than a hook error; switching is one
    method. Claude Code itself ends the turn after 8 consecutive blocks and caps
    hook output at 10,000 characters, truncating the rest — mid-JSON-string if it
    must, which costs the whole payload and not just the tail. The event COUNT
    does not bound that: ``event.text`` is unbounded board text, and the coder's
    own standing cycle asks for ``--note "how to verify + evidence"``. Measured
    with the real renderer: ten ``task_review`` events carrying a 2 KB note each
    rendered a 20,541-character reason, 20,603 as the JSON object — twice the
    cap. So :func:`_render_wakeup` budgets CHARACTERS (see
    :data:`_WAKEUP_REASON_BUDGET`) and the count limit is only the other half.
    """

    reason: str
    cursor: int
    """The event seq the manager's cursor advanced to — what the reason covers."""

    def as_hook_output(self) -> dict[str, str]:
        return {"decision": "block", "reason": self.reason}


@dataclass(frozen=True)
class Delivery:
    """The read-back receipt of one confirmed team-store write.

    ``seq`` is the event's stream position; ``None`` marks a confirmed
    no-event write (an idempotent ``task add`` that matched an existing row).
    """

    seq: int | None
    board_id: str
    board_name: str
    warning: str | None = None

    @property
    def receipt(self) -> str:
        """The human receipt appended to a ✓ line: where the write proved durable.

        Quotes the board's ``project_id``, not its display name — root
        directory names collide across checkouts, and a receipt must name
        its board unambiguously (the ``--json`` envelope always did).
        """
        if self.seq is None:
            return f"on {self.board_id}"
        return f"seq {self.seq} on {self.board_id}"


_DELIVERY: ContextVar[Delivery | None] = ContextVar("aisquare_team_delivery", default=None)


def last_delivery() -> Delivery | None:
    """The receipt of the most recent write in this call context (or ``None``).

    Deliberately out-of-band: the CLI and the MCP server both attach receipts
    to success output, and threading a receipt through every service signature
    would churn an API surface other RC work is touching in parallel. Each
    write resets this before doing anything, so a stale receipt can never leak
    into the next command's output. Hook ``_emit``s are exempt from read-back
    and never set a delivery: hooks print no success marker, so there is no ✓
    for a receipt to make honest.
    """
    return _DELIVERY.get()


def short_id(value: str) -> str:
    """The display form of a session id (leading characters, git-style)."""
    return value[:_SHORT_ID]


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _require_enabled() -> None:
    if not orchestrator.team_enabled():
        raise TeamDisabledError()


def _project(store: ContextStore, cwd: Path | None, *, write: bool = False) -> ProjectInfo:
    """The board ``cwd`` resolves to, captured as #139 did while ``[projects] capture`` is on.

    Off, nothing is written: a read gets the project as resolved, whose board is empty
    when nobody added it, and a ``write`` there is refused
    (:class:`~aisquare.services.project.NotAdded`) — the board of a directory no list
    shows is not where anyone meant to post.
    """
    project = orchestrator.team_project(cwd)
    if not project_service.tracked(store, project):
        if write:
            raise project_service.NotAdded(project)
        return project
    project_service.capture(store, project)
    return project


def _emit(
    store: ContextStore,
    project_id: str,
    kind: str,
    text: str,
    *,
    session_id: str | None = None,
    task_id: str | None = None,
    to_role: str | None = None,
) -> TeamEvent:
    """Write one board event — and, when configured, spool it for the gateway.

    Every board write funnels through here, which is why the spool call lives
    here and not at each of the twenty call sites: an insight the board records
    but the gateway never hears about is exactly the gap #50 exists to close.
    It runs after the commit and cannot raise, so a full disk costs a span, not
    a note.
    """
    stored = store.add_team_event(
        TeamEvent(
            id=new_event_id(),
            project_id=project_id,
            session_id=session_id,
            kind=kind,
            text=text,
            task_id=task_id,
            to_role=to_role,
            created_at=_now(),
        )
    )
    insights.record_team_event(
        event_kind=stored.kind,
        text=stored.text,
        event_id=stored.id,
        session_id=stored.session_id,
        project_id=stored.project_id,
        task_id=stored.task_id,
        seq=stored.seq,
    )
    return stored


def _project_root(store: ContextStore, project_id: str) -> Path | None:
    """The registered root of a project — spares hot paths a git subprocess."""
    project = store.get_project(project_id)
    return project.root if project is not None else None


def _resolve_session(store: ContextStore, ref: str | None) -> TeamSession | None:
    if ref is None:
        return None
    session = store.get_session(ref)
    if session is None:
        raise KeyError(ref)
    return session


@dataclass(frozen=True)
class _Board:
    """Where a command delivers, and how to talk about it in receipts: ``name`` is the
    project's as every list names it (``project.display_name``: its nickname, else its
    directory, else its id), from its stored row."""

    id: str
    name: str
    root: Path | None
    warning: str | None = None


TASK_NUMBER_REF = re.compile(r"^#?([0-9]{1,6})$")
"""A task ref that is a board number: ``12`` or ``#12`` (:func:`find_task`)."""


def task_number_of(ref: str) -> int | None:
    """The board number ``ref`` spells (``12``, ``#12``), or ``None`` when it is not one."""
    match = TASK_NUMBER_REF.match(ref.strip())
    return int(match.group(1)) if match else None


def task_label(task: TeamTask) -> str:
    """``#12 tsk_01m4…`` — the number people read, then the id agents keep working with."""
    return f"#{task.number} {task.id}" if task.number is not None else task.id


def find_task(store: ContextStore, ref: str, board_id: str | Callable[[], str]) -> TeamTask | None:
    """The task ``ref`` names, a board number first: ``12`` or ``#12`` on ``board_id``.

    ``board_id`` is the CALLER's board (:func:`_caller_board_id`): numbers are per
    project, so ``7`` means this board's seventh task, never another's. Given as a
    callable, it is asked only for a number: working the board out runs ``git`` in the
    current directory, which an id never needs, and Remote's claim and close, which pass
    the id they resolved, paid it on every write, and failed in a directory since removed
    (review of #259, round 13). A number the
    board does not have, ``12`` or ``#12``, raises :class:`NoSuchTaskNumber` and is never
    looked for as an id: the tail lookup (``get_task``) spans every board, so ``12``
    would close whichever task's id happens to end in ``12``, here or on another
    project's board (review of #261, M1). No real ref needs it: old board lines showed
    eight-character tails, which :data:`TASK_NUMBER_REF` never matches. Anything else is
    the id lookup it always was: a full id, a prefix, a tail.
    """
    number = task_number_of(ref)
    if number is None:
        return store.get_task(ref)
    board = board_id() if callable(board_id) else board_id
    task = store.task_by_number(board, number)
    if task is None:
        raise NoSuchTaskNumber(number, _board_of(store, board).name)
    return task


def _caller_board_id(store: ContextStore, session: TeamSession | None, cwd: Path | None) -> str:
    """The board a task ref is read on: the session's (``--as``), else the directory's —
    as :func:`_board` delivers, but without registering anything for a read."""
    if session is not None:
        return session.project_id
    return orchestrator.team_project(cwd).id


def _board_of(store: ContextStore, project_id: str) -> _Board:
    """The board a known project id names (for task-ref commands).

    One forgotten since is still named and rooted by its row: its tasks and events are
    kept, and the agents whose rows recorded it still read it. Read as one the store does
    not hold, a note ``fleet tell`` or the phone put on a forgotten hub was named by its id,
    and its drain ran in the caller's directory, with a ``git rev-parse``, distilling the
    caller's board and not the hub's (review of #259, round 32, 7/9)."""
    project = store.get_project(project_id, include_forgotten=True)
    if project is None:
        return _Board(id=project_id, name=project_id, root=None)
    return _Board(id=project_id, name=project_service.display_name(project), root=project.root)


def _board_named(store: ContextStore, project: ProjectInfo) -> _Board:
    """``project``'s board, named by its stored row: one worked out from a directory
    (``orchestrator.team_project``) carries no nickname. Named by its folder, a refusal
    said "no task #99 on this board (api-v2)" of the project every list calls "Billing"
    (review of #259, round 14)."""
    named = store.get_project(project.id) or project
    return _Board(id=project.id, name=project_service.display_name(named), root=project.root)


def _board(
    store: ContextStore, session: TeamSession | None, cwd: Path | None, *, write: bool = False
) -> _Board:
    """The board an attributed command delivers to (``write``: :func:`_project`'s).

    With ``--as`` the SESSION's registered board wins — a session legitimately
    works across many worktrees and subdirectories, so cwd is a hint, not an
    identity (#20's misrouting bug: cwd resolution silently sent writes to a
    different board than the session's audience). Without a session, cwd
    resolves the board exactly as before. A cwd that disagrees with the
    session's board produces a warning, never a reroute.
    """
    if session is None:
        return _board_named(store, _project(store, cwd, write=write))
    board = _board_of(store, session.project_id)
    cwd_board = orchestrator.team_project(cwd)
    if cwd_board.id == board.id:
        return board
    warning = (
        f"cwd resolves to board {_board_named(store, cwd_board).name}, but session "
        f"{short_id(session.id)} belongs to {board.name} — delivered to {board.name}"
    )
    return _Board(id=board.id, name=board.name, root=board.root, warning=warning)


def _confirm_event(event: TeamEvent, board: _Board) -> TeamEvent:
    """Prove the write landed: re-read the event through a FRESH connection.

    The connection that wrote the row would happily report its own state; a
    new one sees only what actually committed, on the board it committed to.
    Returns the stored event (authoritative ``seq``) or raises
    :class:`DeliveryUnconfirmedError` — callers must not print success first.
    """
    with store_session() as store:
        stored = store.get_event(event.id)
    if stored is None or stored.project_id != board.id:
        raise DeliveryUnconfirmedError(event.id, board.name)
    return stored


def _record_delivery(event: TeamEvent, board: _Board) -> TeamEvent:
    """Confirm ``event`` on ``board`` and publish the receipt for this write."""
    stored = _confirm_event(event, board)
    _DELIVERY.set(
        Delivery(seq=stored.seq, board_id=board.id, board_name=board.name, warning=board.warning)
    )
    return stored


def session_account(transcript_path: str | None) -> str | None:
    """Which agent config dir (account) a session runs under, or ``None``.

    Derived from the transcript path in the hook payload
    (``<config-dir>/projects/<slug>/<session>.jsonl``) rather than from
    ``CLAUDE_CONFIG_DIR``: the variable only reaches us if the agent happens to
    export it to hook subprocesses, whereas the transcript path is always in
    the payload and names the directory unambiguously. Falls back to the
    variable when the path has an unexpected shape.
    """
    if transcript_path:
        path = Path(transcript_path)
        # …/<config-dir>/projects/<project-slug>/<session-id>.jsonl
        if len(path.parents) >= 3 and path.parents[1].name == "projects":
            return str(path.parents[2])
    return os.environ.get("CLAUDE_CONFIG_DIR", "").strip() or None


def slot_labels_via(store: ContextStore) -> Mapping[int, str]:
    """Slot → label through the caller's OWN store handle, for a board rendered inside a hook.

    The alias everywhere (review of #205, finding 10 and third round) — but a
    hook renders the board while it holds a session with pending writes, and a
    second connection opened underneath would wait on that write until the
    busy timeout, so the labels are read through the handle it already has.
    Fails open to ``{}``: ``account N`` is the fallback everywhere.
    """
    from aisquare.services import claude_accounts as accounts_service  # lazy: no import cycle

    try:
        return accounts_service.slot_labels(store=store)
    except Exception:
        return {}


def account_label(account: str | None, labels: Mapping[int, str] | None = None) -> str | None:
    """The short display form of an account.

    The slot's label from ``labels`` (``services.claude_accounts.slot_labels``:
    the alias when one is set) when the caller has them, else the built-in
    name — ``plain claude`` for slot 1, ``account N`` for a managed slot — and
    the directory name for one the CLI does not own. A managed slot's
    directory is named by its number alone (``…/claude-accounts/2``), and a
    bare ``[2]`` beside a session row would read as a count. Slot 1 is the
    plain claude's own directory, which ``managed_slot`` does not know, so the
    slot is read with ``slot_of``: through ``managed_slot`` alone an aliased
    slot 1 was ``.claude`` on the board and in ``watch`` while ``fleet ls``
    and the agent header said its alias (review of #205, fourth round).
    """
    if not account:
        return None
    from aisquare.services import claude_accounts as accounts_service  # lazy: no import cycle

    slot = accounts_service.slot_of(account)
    if slot is not None:
        named = (labels or {}).get(slot)
        if named:
            return named
        return "plain claude" if slot == claude_accounts_core.DEFAULT_SLOT else f"account {slot}"
    return Path(account).name


def task_key(title: str) -> str:
    """Derive the idempotency key for a task: a slug of its title, in any script (#270).

    The title is NFKC-normalised and casefolded, and every run of characters that are not
    letters, digits or marks (Unicode ``L*``, ``N*``, ``M*``) becomes one ``-``. A mark is
    kept only after a letter, digit or mark it belongs to: Devanagari is not split at the
    virama, and the variation selector after an emoji (U+FE0F) is not a key of its own, as
    it was for ``⚠️`` and ``✔️`` alike (review of #314). ``[a-z0-9]`` alone keyed every title
    with no ASCII letter ``task``, so the second such ``task add`` returned the first. An
    ASCII title keys exactly as before (:func:`_legacy_task_key`); a title with nothing
    left to slug, only emoji, is keyed by a hash of it.
    """
    folded = unicodedata.normalize("NFKC", title).casefold()
    kept: list[str] = []
    for char in folded:
        category = unicodedata.category(char)[0]
        belongs = category in "LN" or (category == "M" and bool(kept) and kept[-1] != "-")
        kept.append(char if belongs else "-")
    slug = re.sub(r"-+", "-", "".join(kept)).strip("-")
    if slug:
        return slug[:64]
    if title.isascii():
        return "task"
    return "t-" + hashlib.sha256(folded.encode()).hexdigest()[:12]


def _legacy_task_key(title: str) -> str:
    """The key a title had before #270, which tasks added then are stored under."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:64] or "task"


class _Nowhere(enum.Enum):
    """The type of :data:`NOWHERE`."""

    NOWHERE = "nowhere"


NOWHERE: Final = _Nowhere.NOWHERE
"""The ``cwd`` of a writer with no directory of its own: the phone's task write that names no
project. ``None`` is this process's directory, a shell's own; the directory Remote (or
``asq``) was started in is nobody's, and of several managers on a board it picks none
(:func:`_reading_manager`). Read as the process's, a done from the phone on a task no row
works woke the manager of whichever project Remote was started in (review of #259, round
28, 5/10)."""


def _nudge_target(
    store: ContextStore,
    writer: str | None,
    board_id: str,
    cwd: Path | _Nowhere | None = None,
    *,
    task: TeamTask | None = None,
) -> str:
    """The project whose manager a nudge for session ``writer``'s write on board ``board_id``
    wakes: that of the writer's own live fleet row; with none, for a write on ``task``, that
    of the task's own row (:func:`_task_row`); else that of the manager that reads the board
    (:func:`_reading_manager`); else ``board_id``'s, as every nudge was. The live rows those
    two look among are read once: each read is every live row, twice on every phone, shell
    and MCP task write by a writer with no row (review of #259, round 28, 7/10).

    Which manager is a fleet question, the news a board one (docs/fleet.md, "Under a hub: one
    board, many fleet projects"): the nudge types a fixed line into the manager row's pane,
    and the delta that line wakes it to is read from the manager's session's board. Under
    ``AISQUARE_TEAM_HUB`` every session is on the hub's board, where no manager has a row, so
    a coder's ``task done`` or ``task review``, a question to the manager and a manager's own
    deferred wake reached nobody, or woke the hub directory's own manager. The row is the
    writer's, in whichever project: a coder's manager is in its own row's project, as round
    22 spawns it there, and a manager's Stop wakes its own row (review of #259, round 24,
    4/6). A writer with no row (the phone, the human's own shell, an MCP client, a manager
    launched by hand) woke the board's project's manager: under a hub the hub's, where no
    manager has a row, so a note to the manager from the phone never woke it (round 25,
    5/10). Read in the write's own store session, after the write: it cannot raise, as a
    read that fails costs the nudge its target, never the write. The manager it names is
    woken only when it reads ``board_id`` (``fleet.nudge_manager``'s ``board``, which
    :func:`_nudge_manager` passes).

    A task write knows its project better than the writer's directory does. Asked by the
    directory, a ``task done`` from ``~`` or through the MCP server, outside every root, woke
    nobody, and the phone's done with no project, in the pinned one's root, woke that
    project's manager for another's task (review of #259, round 27, 3/7). ``task`` is the
    task as it was before the write, so its rows still name it and its claim is still held.
    """
    try:
        row = store.fleet_agent_for_session(writer) if writer is not None else None
        if row is not None:
            return row.project_id
        live = store.live_fleet_agents()
        row = None if task is None else _task_row(store, task, live)
        if row is not None:
            return row.project_id
        return _reading_manager(store, board_id, cwd, live) or board_id
    except Exception:
        return board_id


def _task_row(store: ContextStore, task: TeamTask, live: Sequence[FleetAgent]) -> FleetAgent | None:
    """The live fleet row a task is worked in: the newest of ``live``, the live rows, spawned
    for it, else the row of the session that claimed it."""
    spawned = [row for row in live if row.task_id == task.id]
    if spawned:
        return spawned[-1]
    if task.claimed_by is None:
        return None
    return store.fleet_agent_for_session(task.claimed_by)


def _reading_manager(
    store: ContextStore, board_id: str, cwd: Path | _Nowhere | None, live: Sequence[FleetAgent]
) -> str | None:
    """The project of the ``manager`` row of ``live``, the live rows, whose agent reads board
    ``board_id``, as its spawn recorded (``FleetAgent.board``). Of several, as under a hub,
    the one whose project's root holds the writer's directory, ``cwd`` or this process's (the
    phone writes a project's note in its root), the deepest root of those that do; of none
    that holds it, none, and none for a writer with no directory (:data:`NOWHERE`).

    The deepest, not the oldest: a project rooted in a sub-folder of another's reads the same
    board, the checkout's, and its directory is inside both roots, so a note to its manager
    from the phone woke the older project's, and its own waited on (review of #259, round 26,
    5/11).
    """
    managers = [row for row in live if row.label == MANAGER_ROLE and row.board == board_id]
    if len(managers) == 1:
        return managers[0].project_id
    if isinstance(cwd, _Nowhere):
        return None
    here = (cwd or Path.cwd()).resolve()
    holding = [
        project
        for manager in managers
        for project in [store.get_project(manager.project_id)]
        if project is not None and here.is_relative_to(project.root)
    ]
    deepest = max(holding, key=lambda project: len(project.root.parts), default=None)
    return deepest.id if deepest is not None else None


def _nudge_manager(project_id: str, *, reason: str, board: str) -> None:
    """Wake the project's WAITING manager after a board write it must act on (§7.3, path 2);
    ``project_id`` is :func:`_nudge_target`'s, ``board`` the board the write went on.

    Only a manager that reads ``board`` is woken (``fleet.nudge_manager``): the line wakes it
    to its own board's delta. A coder spawned without the hub its manager was spawned under
    writes on its project's board, and the manager it woke read the hub's, found nothing and
    waited again, while Remote counted the question handled (review of #259, round 26, 4/11).

    Runs inside the writing agent's CLI process, after the write is confirmed, so
    it is an observer of the write: it may never raise, and a fleet that is not
    wired (no tmux, no manager, an older ``services.fleet``) costs exactly the
    nudge. The fleet decides whether a nudge is due — state ``waiting``, pane
    alive, debounced — and what the one fixed line says; the details reach the
    manager through the delta its next prompt injects, never through the nudge.
    """
    try:
        from aisquare.services import fleet as fleet_service  # lazy: fleet imports the store

        nudge = getattr(fleet_service, "nudge_manager", None)
        if nudge is not None:
            nudge(project_id, reason=reason, board=board)
    except Exception:  # the write already succeeded; a lost nudge is the whole cost
        return


# --- commands ----------------------------------------------------------------


def activate(cwd: Path | None = None) -> ProjectInfo:
    """Turn the orchestrator on for this project (``team on``)."""
    _require_enabled()
    _DELIVERY.set(None)
    with store_session() as store:
        # `team on` is deliberate (#139): one onboarding write, not _project's capture first.
        project = store.onboard_project(orchestrator.team_project(cwd))
        event = None
        if not store.team_active(project.id):
            event = _emit(store, project.id, "activate", "agent orchestrator activated")
        board = _board_named(store, project)
    if event is not None:
        _record_delivery(event, board)
    return project


def board_data(
    cwd: Path | None = None,
    *,
    events: int = _BOARD_EVENTS,
    since_seq: int | None = None,
    project: ProjectInfo | None = None,
    glance: bool = False,
    exclude_kinds: Collection[str] = (),
) -> tuple[ProjectInfo, list[TeamSession], list[TeamTask], list[TeamEvent]]:
    """Everything the board shows: sessions, tasks and recent events.

    ``since_seq`` turns the event fetch incremental (only rows past it) so a
    watch UI polling every few seconds does not rehydrate its whole window.
    ``project`` lets a long-lived caller resolve identity once and pass it in,
    sparing a ``git rev-parse`` per call (the watch TUI does this).
    ``glance`` reads what a reader of the events alone needs: of the sessions
    only those the events name, newest seen first as ever, and no task. The
    rest is every session and task the project ever had, and the remote's
    ``board`` frame, read every second while a phone shows its Board tab, read
    all of it to drop it.
    ``exclude_kinds`` leaves those event kinds out of either fetch, inside the
    query, so ``events`` counts what is shown: the human board passes
    :data:`CAPTAIN_AUDIT_KINDS`, an agent's (the MCP ``team_board``)
    :data:`HUMAN_BOARD_KINDS`. By default nothing is left out.
    """
    _require_enabled()
    with store_session() as store:
        return board_data_in(
            store,
            cwd,
            events=events,
            since_seq=since_seq,
            project=project,
            glance=glance,
            exclude_kinds=exclude_kinds,
        )


def board_data_in(
    store: ContextStore,
    cwd: Path | None = None,
    *,
    events: int = _BOARD_EVENTS,
    since_seq: int | None = None,
    project: ProjectInfo | None = None,
    glance: bool = False,
    exclude_kinds: Collection[str] = (),
) -> tuple[ProjectInfo, list[TeamSession], list[TeamTask], list[TeamEvent]]:
    """:func:`board_data` through a store the CALLER holds open: Remote's ``board`` frame,
    which finds the board it shows in that store first (``remote_server``'s
    ``remote_board_payload``), read every second while a phone shows its Board tab."""
    _require_enabled()
    return _read_board(
        store,
        cwd,
        events=events,
        since_seq=since_seq,
        project=project,
        glance=glance,
        exclude_kinds=exclude_kinds,
    )


class BoardRead(NamedTuple):
    """What a painted board shows: :func:`board_data`'s four, and the ``fleet switch`` of
    each limited session's chip (:func:`fleet_switches`)."""

    project: ProjectInfo
    sessions: list[TeamSession]
    tasks: list[TeamTask]
    events: list[TeamEvent]
    switches: dict[str, str]


def board_read(
    cwd: Path | None = None,
    *,
    events: int = _BOARD_EVENTS,
    since_seq: int | None = None,
    project: ProjectInfo | None = None,
    exclude_kinds: Collection[str] = (),
) -> BoardRead:
    """:func:`board_data` for a board that paints its sessions, with their chips' switches,
    in its one store session.

    The board panel and ``aisquare board``'s frame asked :func:`fleet_switches` for them
    after :func:`board_data` had closed its session, and that opened another: a new
    connection, the migration check and the presence pass, on the event loop, at every 2 s
    tick of every visible board for as long as a session stayed limited, which is hours
    (review of #259, round 23). ``exclude_kinds`` as in :func:`board_data`.
    """
    _require_enabled()
    with store_session() as store:
        read = _read_board(
            store,
            cwd,
            events=events,
            since_seq=since_seq,
            project=project,
            exclude_kinds=exclude_kinds,
        )
        return BoardRead(*read, fleet_switches(store, read[1]))


def _read_board(
    store: ContextStore,
    cwd: Path | None,
    *,
    events: int,
    since_seq: int | None,
    project: ProjectInfo | None,
    glance: bool = False,
    exclude_kinds: Collection[str] = (),
) -> tuple[ProjectInfo, list[TeamSession], list[TeamTask], list[TeamEvent]]:
    """The body of :func:`board_data`, through the caller's store."""
    resolved = project if project is not None else _project(store, cwd)
    if since_seq is None:
        fetched = store.recent_events(resolved.id, exclude_kinds=exclude_kinds, limit=events)
    else:
        fetched = store.events_since(
            resolved.id, since_seq, exclude_kinds=exclude_kinds, limit=events
        )
    if glance:
        named = sorted({event.session_id for event in fetched if event.session_id})
        sessions = store.team_sessions_seen_since(resolved.id, None, ids=named)
        return resolved, sessions, [], fetched
    return (
        resolved,
        store.team_sessions(resolved.id),
        store.team_tasks(resolved.id),
        fetched,
    )


def board_scope_note(cwd: Path | None = None) -> str | None:
    """Name the board a cwd-resolved read answers for, when it may not be yours.

    Board reads resolve from the current directory. A session in a linked
    worktree, or anywhere outside a repository, silently reads a DIFFERENT
    board — and the harmful case is not the empty one. Measured while this team
    was live: the project directory returned 200 events, a worktree 0, and
    ``$HOME`` TWELVE. Empty invites suspicion; a populated wrong board reads as
    a successful answer.

    Silent when the caller sits in a repository whose team project matches it,
    which is every ordinary invocation — a banner on those is how people learn
    to ignore banners.

    Never raises. This is a diagnostic about a read, and the doctrine is that an
    observer may cost its own output and never the command.
    """
    start = cwd or Path.cwd()
    try:
        board = orchestrator.team_project(start)
        common = workspace.git_common_root(start)
    except OSError:  # a diagnostic must not break a read; git or the fs, not a typo
        return None
    name = (board.root.name if board.root else "") or board.id
    if common is None:
        return (
            f"reading board {name} — {start} is not a git repository, so the board "
            "follows your directory; pass --as <session> to read your own"
        )
    if board.root is not None and board.root != common:
        return (
            f"reading board {name}, not {common.name} — this directory resolves "
            "elsewhere; pass --as <session> to read your own"
        )
    return None


def resolve_project(cwd: Path | None = None) -> ProjectInfo:
    """The team project for ``cwd`` (resolved once by long-lived callers)."""
    _require_enabled()
    return orchestrator.team_project(cwd)


def terminal_attribution(
    cwd: Path | None = None, *, project: ProjectInfo | None = None
) -> dict[str, TeamEvent]:
    """Who closed each task, and when — from the store, not a feed window."""
    _require_enabled()
    with store_session() as store:
        resolved = project if project is not None else _project(store, cwd)
        return store.terminal_events(resolved.id)


def log_events(
    cwd: Path | None = None,
    *,
    limit: int = 30,
    by: str | None = None,
    since: datetime | None = None,
    since_seq: int | None = None,
    kind: str | None = None,
    task_ref: str | None = None,
    session_ref: str | None = None,
) -> list[TeamEvent]:
    """The recent team-pipe events for this board, oldest first.

    Filters compose (AND). ``by`` is a session id prefix, resolved like
    ``--as``; ``session_ref`` routes board resolution through the acting
    session's row, exactly like attributed writes (#20). ``since`` filters on
    event time, ``since_seq`` on stream position (cursor semantics, like the
    MCP ``team_log`` tool).
    """
    _require_enabled()
    with store_session() as store:
        session = _resolve_session(store, session_ref)
        board = _board(store, session, cwd)
        author = _resolve_session(store, by)
        task_id: str | None = None
        if task_ref is not None:
            task = find_task(store, task_ref, board.id)
            if task is None:
                raise KeyError(task_ref)
            task_id = task.id
        if author is None and since is None and since_seq is None and kind is None and not task_id:
            return store.recent_events(board.id, limit=limit)
        return store.filtered_events(
            board.id,
            session_id=author.id if author is not None else None,
            since_iso=since.astimezone(UTC).isoformat() if since is not None else None,
            since_seq=since_seq,
            kind=kind,
            task_id=task_id,
            limit=limit,
        )


@dataclass(frozen=True)
class VerifyResult:
    """The outcome of a receipt check (``team verify``).

    ``board_id`` is what receipts quote (#20 hardening: directory names
    collide across checkouts); ``board_name`` stays for human messages.
    """

    event: TeamEvent | None
    board_id: str
    board_name: str
    elsewhere: str | None = None
    line: str | None = None


def verify_receipt(
    receipt: str, *, session_ref: str | None = None, cwd: Path | None = None
) -> VerifyResult:
    """Re-prove a write: is the receipt's event on the caller's board?

    ``receipt`` is a stream seq (a number) or an event id (prefix ok) — the
    two forms every ✓ receipt quotes. Board resolution follows attributed
    writes (#20): the session's registered board wins over cwd. A receipt
    that exists on a DIFFERENT board is an honest not-found here, with a
    hint naming the board that actually holds it.
    """
    _require_enabled()
    with store_session() as store:
        session = _resolve_session(store, session_ref)
        board = _board(store, session, cwd)
        try:
            seq = int(receipt)
        except ValueError:
            seq = None
        event = store.get_event_by_seq(seq) if seq is not None else store.find_event_by_id(receipt)
        if event is None:
            return VerifyResult(event=None, board_id=board.id, board_name=board.name)
        if event.project_id != board.id:
            elsewhere = _board_of(store, event.project_id).name
            return VerifyResult(
                event=None, board_id=board.id, board_name=board.name, elsewhere=elsewhere
            )
        roles = {s.id: s.role for s in store.team_sessions(board.id)}
        line = event_line(event, roles, store.task_numbers(board.id))
        return VerifyResult(event=event, board_id=board.id, board_name=board.name, line=line)


_SIGNAL_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_SIGNAL_VALUE = re.compile(r"\S{1,128}")


@dataclass(frozen=True)
class SignalState:
    """One named board state: the current value, who set it, and when (#23)."""

    name: str
    value: str
    set_by: str | None
    seq: int
    updated_at: datetime | None


_SIGNALS = "signal/"
"""The ``team_meta`` prefix every board's named states are kept under (:func:`_signal_key`)."""


def _signal_key(project_id: str, name: str) -> str:
    return f"{_SIGNALS}{project_id}/{name}"


def _signal_state(name: str, blob: str) -> SignalState:
    data = json.loads(blob)
    raw_updated = data.get("updated_at")
    return SignalState(
        name=name,
        value=str(data.get("value", "")),
        set_by=data.get("session_id"),
        seq=int(data.get("seq", 0)),
        updated_at=datetime.fromisoformat(raw_updated) if raw_updated else None,
    )


def set_signal(
    name: str,
    value: str,
    *,
    session_ref: str | None = None,
    cwd: Path | None = None,
    project_id: str | None = None,
) -> tuple[SignalState, str | None]:
    """Set a named board state (``team signal NAME VALUE``); returns (state, prev).

    Names and values are single tokens by contract — that is exactly what
    makes the emitted ``signal`` event's text decodable into structured
    payload fields with zero substring matching (#23). The pipe event and
    the current-state blob commit in ONE transaction, then the event is
    read back per the #20 contract, so ``team verify <seq>`` works on
    signal receipts like any other write.
    """
    _require_enabled()
    _DELIVERY.set(None)  # FIRST: a refused signal must not leave the last write's receipt readable
    _validate_signal(name, value)  # before any store is opened or board resolved
    with store_session() as store:
        session = _resolve_session(store, session_ref)
        # A caller that names the project by ID resolves the board id-addressed,
        # never through cwd — `AISQUARE_TEAM_HUB` redirects cwd resolution to one
        # hub project, so `fleet shutdown -P b` cleared project A's pause and
        # reported B (review of #121, round 8). `_board_of` also skips
        # `ensure_project`, so a forgotten project's tombstone is not revived.
        board = (
            _board_of(store, project_id)
            if project_id is not None
            else _board(store, session, cwd, write=True)
        )
        state, prev, event = _write_signal(store, board, session, name, value)
    _record_delivery(event, board)  # the receipt for this write; `state.seq` is already the row's
    return state, prev


def set_signals_in(
    store: ContextStore,
    signals: Sequence[tuple[str, str]],
    value: str,
    *,
    session_ref: str | None = None,
) -> None:
    """:func:`set_signal` of each (project id, name) of ``signals`` to ``value``, through a
    store the CALLER holds open, in ONE transaction: every one is set, or none is.

    For a write to many boards — a project's pause, one signal on each board it lives on,
    and ``fleet shutdown`` clearing every confirmed project's — which used to open a
    connection a signal through :func:`set_signal` (connect, WAL switch, migrations each
    time; review of #203, round 4), and committed each on its own: a store error part-way
    (``database is locked`` while many agents' hooks write) left the project paused on the
    boards before it and not on the rest, and ``fleet pause`` ended in a traceback (review
    of #259, round 34, 5/10). Same validation, of every one before anything is written;
    ``session_ref`` is ``--as``, resolved as :func:`set_signal` resolves it. No delivery
    receipt is published, because the caller is not the ``team signal`` command and there
    is nobody to hand it to — and the receipt of an EARLIER write in this process is
    cleared, so nothing reads it as this one's (round 5 of #203). Returns nothing: its
    callers read nothing.
    """
    _require_enabled()
    for _, name in signals:
        _validate_signal(name, value)
    _DELIVERY.set(None)
    session = _resolve_session(store, session_ref)
    _write_signals(
        store, [(_board_of(store, board), name) for board, name in signals], session, value
    )


def _validate_signal(name: str, value: str) -> None:
    """Names and values are single tokens by contract (#23); refused before any write."""
    if not _SIGNAL_NAME.fullmatch(name):
        raise ValueError(
            f"signal name {name!r} must be a lowercase token "
            "([a-z0-9._-], starting alphanumeric, max 64)"
        )
    if not _SIGNAL_VALUE.fullmatch(value):
        raise ValueError(f"signal value {value!r} must be a single token (no whitespace)")


def _write_signal(
    store: ContextStore,
    board: _Board,
    session: TeamSession | None,
    name: str,
    value: str,
) -> tuple[SignalState, str | None, TeamEvent]:
    """The one write behind :func:`set_signal`: :func:`_write_signals` of one."""
    return _write_signals(store, [(board, name)], session, value)[0]


def _write_signals(
    store: ContextStore,
    signals: Sequence[tuple[_Board, str]],
    session: TeamSession | None,
    value: str,
) -> list[tuple[SignalState, str | None, TeamEvent]]:
    """Set each (board, name) of ``signals`` to ``value`` in one transaction
    (``ContextStore.add_signal_events``): each one's state, the value it replaced, and its
    event.

    Validation is the CALLER's, before it opens or resolves anything:
    ``_board`` runs ``ensure_project``, which revives a forgotten project's
    tombstone, so a signal that was going to be refused used to register the
    directory first (review of the fold).
    """
    now = _now()
    by = session.id if session else None
    priors: list[str | None] = []
    writes: list[tuple[TeamEvent, str, dict[str, Any]]] = []
    for board, name in signals:
        key = _signal_key(board.id, name)
        prior = store.get_meta(key)
        prev = _signal_state(name, prior).value if prior is not None else None
        text = f"{name}: {value}" if prev is None else f"{name}: {value} (was {prev})"
        event = TeamEvent(
            id=new_event_id(),
            project_id=board.id,
            session_id=by,
            kind="signal",
            text=text,
            created_at=now,
        )
        priors.append(prev)
        writes.append(
            (event, key, {"value": value, "session_id": by, "updated_at": now.isoformat()})
        )
    events = store.add_signal_events(writes)
    return [
        (
            SignalState(name=name, value=value, set_by=by, seq=event.seq, updated_at=now),
            prev,
            event,
        )
        for (_, name), prev, event in zip(signals, priors, events, strict=True)
    ]


def read_signal(
    name: str,
    *,
    session_ref: str | None = None,
    cwd: Path | None = None,
    project_id: str | None = None,
) -> SignalState | None:
    """The current value of one named board state (``team signal NAME``)."""
    _require_enabled()
    with store_session() as store:
        session = _resolve_session(store, session_ref)
        board = (
            _board_of(store, project_id) if project_id is not None else _board(store, session, cwd)
        )
        return _read_signal(store, board.id, name)


def board_signals_in(
    store: ContextStore, *, unreadable: list[tuple[str, str]] | None = None
) -> dict[tuple[str, str], SignalState]:
    """Every board's named states, by (board id, name), through a store the CALLER holds open
    (see :func:`set_signals_in`): one read of them all, for a caller that looks a name up on
    whichever boards hold it. A fleet project's pause is named for its project on each board
    (``harness.pause_signal``), so the boards it is on are those that hold it by that name
    (``fleet._pause_where_on``).

    A state whose blob does not parse is left out, and its (board id, name) added to
    ``unreadable`` when the caller gives one, to say whose it was. Read with the rest, one
    malformed blob on any board raised for every caller: ``fleet resume`` of any project
    ended in a traceback, and ``fleet shutdown --all`` confirmed no project down (review of
    #259, round 32, 6/9)."""
    _require_enabled()
    states: dict[tuple[str, str], SignalState] = {}
    for key, blob in store.list_meta(_SIGNALS).items():
        board, _, name = key.removeprefix(_SIGNALS).partition("/")
        try:
            states[(board, name)] = _signal_state(name, blob)
        except (ValueError, TypeError, AttributeError):  # not JSON, not an object, a bad field
            if unreadable is not None:
                unreadable.append((board, name))
    return states


def _read_signal(store: ContextStore, project_id: str, name: str) -> SignalState | None:
    blob = store.get_meta(_signal_key(project_id, name))
    return _signal_state(name, blob) if blob is not None else None


def list_signals(*, session_ref: str | None = None, cwd: Path | None = None) -> list[SignalState]:
    """Every named state on this board (``team signals``), sorted by name."""
    _require_enabled()
    with store_session() as store:
        session = _resolve_session(store, session_ref)
        board = _board(store, session, cwd)
        prefix = _signal_key(board.id, "")
        return [
            _signal_state(key.removeprefix(prefix), blob)
            for key, blob in store.list_meta(prefix).items()
        ]


def set_role(role: str, session_ref: str, cwd: Path | None = None) -> TeamSession:
    """Set the role of a session (``team role``)."""
    _require_enabled()
    # A write like any other: reset the delivery state (last_delivery's
    # invariant) — it stays None because no pipe event is emitted, so the
    # CLI prints no receipt for it.
    _DELIVERY.set(None)
    with store_session() as store:
        session = store.get_session(session_ref)
        if session is None:
            raise KeyError(session_ref)
        return store.update_session(session.id, role=role)


def set_focus(text: str, session_ref: str, cwd: Path | None = None) -> TeamSession:
    """Announce what a session is working on right now (``team focus``)."""
    _require_enabled()
    _DELIVERY.set(None)
    with store_session() as store:
        session = store.get_session(session_ref)
        if session is None:
            raise KeyError(session_ref)
        updated = store.update_session(session.id, focus=text)
        event = _emit(store, updated.project_id, "focus", text, session_id=updated.id)
        board = _board_of(store, updated.project_id)
    _record_delivery(event, board)
    return updated


def add_note(
    text: str,
    *,
    session_ref: str | None = None,
    task_ref: str | None = None,
    to_role: str | None = None,
    kind: str = "note",
    cwd: Path | None = None,
    board_id: str | None = None,
    project_id: str | None = None,
) -> TeamEvent:
    """Put a note/decision/question/result on the team pipe.

    ``board_id`` names the board outright, whoever writes and from wherever: ``fleet tell``'s
    note to an agent goes to the board the agent reads (``fleet._board_of_row``), not the
    teller's. ``project_id`` is the same, by the project whose board it is (review of #240,
    finding 8: a busy agent's note lands on its own board); ``board_id`` wins when both are
    given. Everything else that can pick a board can pick another one, so with either the
    session still says who filed the note, and no longer where; ``_board_of`` as in
    :func:`set_signal`.
    """
    _require_enabled()
    _DELIVERY.set(None)
    with store_session() as store:
        session = _resolve_session(store, session_ref)
        named = board_id if board_id is not None else project_id
        board = (
            _board_of(store, named)
            if named is not None
            else _board(store, session, cwd, write=True)
        )
        task_id: str | None = None
        if task_ref is not None:
            task = find_task(store, task_ref, board.id)
            if task is None:
                raise KeyError(task_ref)
            if task.project_id != board.id:
                # Same contamination --needs rejects: a foreign board's task
                # attached to this board's event would render as a broken
                # ref for every reader here.
                raise ValueError(f"--task {task_ref}: that task belongs to another project's board")
            task_id = task.id
        event = _emit(
            store,
            board.id,
            kind,
            text,
            session_id=session.id if session else None,
            task_id=task_id,
            to_role=to_role,
        )
        wakes = kind in ("result", "question") or _addressed_to_manager(to_role)
        manager = (
            _nudge_target(store, session.id if session else None, board.id, cwd)
            if wakes
            else board.id
        )
    stored = _record_delivery(event, board)
    if kind in distill_service.DISTILL_KINDS:
        distill_service.spawn_drain(root=board.root, board=board.id)
    if kind in ("result", "question"):
        _nudge_manager(manager, reason=kind, board=board.id)
    elif _addressed_to_manager(to_role):
        # §7.3 path 2 lists `note --to manager` among the writes that wake a
        # waiting manager, and only the KIND was consulted here: a note
        # addressed to the manager reached a waiting one on no path at all —
        # 'note' is not a MANAGER_WAKE_KIND either, so its Stop hook did not
        # deliver it and it sat past the cursor until some unrelated event.
        _nudge_manager(manager, reason=f"{kind}_to_manager", board=board.id)
    return stored


def add_task(
    title: str,
    *,
    key: str | None = None,
    detail: str | None = None,
    role: str | None = None,
    needs: list[str] | None = None,
    session_ref: str | None = None,
    cwd: Path | None = None,
) -> tuple[TeamTask, bool]:
    """Add a shared task; idempotent on its key (re-adding returns the original).

    ``needs`` are task refs (prefixes fine) this task depends on; ``task
    next`` will not hand it out until they are resolved.
    """
    _require_enabled()
    _DELIVERY.set(None)
    with store_session() as store:
        session = _resolve_session(store, session_ref)
        board = _board(store, session, cwd, write=True)
        resolved_needs: list[str] = []
        for ref in needs or []:
            needed = find_task(store, ref, board.id)
            if needed is None:
                raise KeyError(ref)
            if needed.project_id != board.id:
                # A cross-project need would count as unmet forever (readiness
                # only sees this project's statuses) — starve, silently.
                raise ValueError(f"--needs {ref}: that task belongs to another project's board")
            if needed.id not in resolved_needs:
                resolved_needs.append(needed.id)
        derived = key or task_key(title)
        legacy = _legacy_task_key(title)
        if key is None and legacy not in (derived, "task"):
            # A title with non-ASCII words keyed differently before #270. The task added
            # then under this very title is this one, so the re-add stays idempotent; the
            # old key alone is not enough, as two titles shared it (`Fix вход`, `Fix тесты`).
            stored = store.team_tasks(board.id)
            if any(task.key == legacy and task.title == title for task in stored):
                derived = legacy
        now = _now()
        task, created = store.upsert_task(
            TeamTask(
                id=new_task_id(),
                project_id=board.id,
                key=derived,
                title=title,
                detail=detail,
                role=role,
                needs=resolved_needs,
                created_by=session.id if session else None,
                created_at=now,
                updated_at=now,
            )
        )
        event = None
        if created:
            event = _emit(
                store,
                board.id,
                "task_added",
                task.title,
                session_id=session.id if session else None,
                task_id=task.id,
                to_role=task.role,
            )
    if event is not None:
        _record_delivery(event, board)
        return task, created
    # Idempotent duplicate: nothing new hit the pipe, but the caller still
    # gets a truthful receipt — the existing row, read back fresh.
    with store_session() as store:
        fresh = store.get_task(task.id)
    if fresh is None or fresh.project_id != board.id:
        raise DeliveryUnconfirmedError(task.id, board.name)
    _DELIVERY.set(
        Delivery(seq=None, board_id=board.id, board_name=board.name, warning=board.warning)
    )
    return fresh, created


def list_tasks(status: TaskStatus | None = None, cwd: Path | None = None) -> list[TeamTask]:
    """The project's shared tasks, optionally filtered by status."""
    _require_enabled()
    with store_session() as store:
        return list_tasks_in(store, _project(store, cwd).id, status=status)


def list_tasks_in(
    store: ContextStore, board_id: str, *, status: TaskStatus | None = None
) -> list[TeamTask]:
    """:func:`list_tasks` of ``board_id`` through a store the CALLER holds open: Remote's
    Tasks tab, which finds the board it shows in that store first (``remote_server``'s
    ``tasks_payload``). The caller has refused a board that is off, as :func:`list_tasks`
    does, before it looked the board up."""
    return store.team_tasks(board_id, status=status)


def show_task(ref: str, *, session_ref: str | None = None, cwd: Path | None = None) -> TeamTask:
    """One task in full. Raises ``KeyError`` if nothing matches."""
    _require_enabled()
    with store_session() as store:
        session = _resolve_session(store, session_ref)
        task = find_task(store, ref, lambda: _caller_board_id(store, session, cwd))
        if task is None:
            raise KeyError(ref)
        return task


#: Which event kind puts a task into each status. The lookup below is keyed on
#: the task's CURRENT status rather than on "does a task_blocked exist for this
#: task", and that is the whole stale-note defence: nothing deletes an event, so
#: a task blocked yesterday and claimed today still HAS its task_blocked. Asking
#: what produced the status it is in now cannot go stale, and needs nothing
#: cleared on claim — which is what the first version of this task's contract
#: proposed and would have duplicated state that already exists.
#:
#: ``doing`` and ``dropped`` are deliberately absent: a claim carries no note,
#: and nothing asked for the dropped case. An unmapped status renders nothing.
_STATUS_EVENT_KIND = {
    "blocked": "task_blocked",
    "done": "task_done",
    "todo": "task_reopened",
}


def stopped_because(task: TeamTask) -> str | None:
    """The note attached to whatever put ``task`` in its current status.

    A READ, not a new column. `task block --reason`, `task reopen --reason` and
    `task done --note` all persist their note as an event whose text is
    ``"<title> — <note>"``; none of them was readable through `task show`, so
    one join answers for all three.

    Returns ``None`` rather than an empty string when no note was given, because
    the event text is then the title alone and a caller must be able to tell
    "none given" from "given and empty".

    FAILS OPEN. This is decoration on a read command: a store that cannot be
    queried, or an event shaped differently by some future writer, costs the
    note and never the exit code.
    """
    kind = _STATUS_EVENT_KIND.get(task.status)
    if kind is None:
        return None
    try:
        with store_session() as store:
            events = store.filtered_events(task.project_id, kind=kind, task_id=task.id, limit=1)
    except Exception:
        return None
    if not events:
        return None
    prefix = f"{task.title} — "
    text = events[-1].text
    return text[len(prefix) :] if text.startswith(prefix) else None


def last_feedback(task: TeamTask) -> str | None:
    """The reason given at the task's newest ``task reopen``, whatever its status now (#74).

    :func:`stopped_because` answers for the CURRENT status only, which is right for
    ``task show`` and wrong for a claim: a reopened task is ``doing`` once claimed, and
    the reason it bounced was gone exactly when the coder picking it up needed it.
    ``None`` when it was never reopened, or reopened with no reason. Fails open, as
    :func:`stopped_because` does: a claim never fails for its decoration.
    """
    try:
        with store_session() as store:
            events = store.filtered_events(
                task.project_id, kind="task_reopened", task_id=task.id, limit=1
            )
    except Exception:
        return None
    if not events:
        return None
    prefix = f"{task.title} — "
    text = events[-1].text
    return text[len(prefix) :] if text.startswith(prefix) else None


def claim_task(ref: str, *, session_ref: str | None = None) -> TeamTask:
    """Atomically claim a task; exactly one concurrent claimer wins.

    Raises :class:`ClaimLostError` when the task is already claimed (with a
    live lease) and ``KeyError`` when the ref matches nothing.
    """
    _require_enabled()
    _DELIVERY.set(None)
    with store_session() as store:
        session = _resolve_session(store, session_ref)
        task = find_task(store, ref, lambda: _caller_board_id(store, session, None))
        if task is None:
            raise KeyError(ref)
        claimant = session.id if session else "cli"
        lease = _now() + timedelta(minutes=orchestrator.lease_minutes())
        if not store.claim_task(task.id, claimant, lease):
            current = store.get_task(task.id)
            assert current is not None  # it existed a moment ago
            raise ClaimLostError(current)
        claimed = store.get_task(task.id)
        assert claimed is not None  # just claimed
        event = _emit(
            store,
            claimed.project_id,
            "task_claimed",
            claimed.title,
            session_id=session.id if session else None,
            task_id=claimed.id,
        )
        board = _board_of(store, claimed.project_id)
    _record_delivery(event, board)
    return claimed


def _finish_task(
    ref: str,
    status: TaskStatus,
    kind: str,
    *,
    note: str | None = None,
    session_ref: str | None = None,
    wakes: bool = True,
    cwd: Path | _Nowhere | None = None,
) -> tuple[TeamTask, _Board, str]:
    """Set the task's status with its event; the board, and the project whose manager the
    write wakes (:func:`_nudge_target`): for a writer with no row the task's own row's, else
    the one whose root holds ``cwd``. Asked before the write, as a closing status retires the
    rows spawned for the task. ``wakes=False``, for a status that wakes nobody (``drop``):
    the board's, with no lookup, as :func:`add_note` does for its quiet kinds (review of
    #259, round 25, 9/10)."""
    _DELIVERY.set(None)
    with store_session() as store:
        session = _resolve_session(store, session_ref)
        task = find_task(store, ref, lambda: _caller_board_id(store, session, None))
        if task is None:
            raise KeyError(ref)
        manager = (
            _nudge_target(store, session.id if session else None, task.project_id, cwd, task=task)
            if wakes
            else task.project_id
        )
        updated = store.set_task_status(task.id, status)
        if status in CLOSED_STATUSES:
            # Rule 3 of the fleet-row section: the rows spawned for this task
            # have nothing left to be told about it.
            store.retire_fleet_assignments(updated.id)
        text = updated.title if note is None else f"{updated.title} — {note}"
        event = _emit(
            store,
            updated.project_id,
            kind,
            text,
            session_id=session.id if session else None,
            task_id=updated.id,
        )
        board = _board_of(store, updated.project_id)
    _record_delivery(event, board)
    return updated, board, manager


def finish_task(
    ref: str,
    *,
    note: str | None = None,
    session_ref: str | None = None,
    cwd: Path | _Nowhere | None = None,
) -> TeamTask:
    """Mark a task done (``task done``). ``cwd`` is the writer's directory, which picks the
    manager a writer with no row wakes (:func:`_nudge_target`): the phone's is the project's
    root, as for its note, not the directory Remote runs in, and :data:`NOWHERE` when it
    names no project."""
    _require_enabled()
    task, board, manager = _finish_task(
        ref, "done", "task_done", note=note, session_ref=session_ref, cwd=cwd
    )
    distill_service.spawn_drain(root=board.root, board=board.id)
    _nudge_manager(manager, reason="task_done", board=board.id)
    return task


def review_task(ref: str, *, note: str | None = None, session_ref: str | None = None) -> TeamTask:
    """Send a task to review — done coding, awaiting verification (``task review``)."""
    _require_enabled()
    task, board, manager = _finish_task(
        ref, "review", "task_review", note=note, session_ref=session_ref
    )
    distill_service.spawn_drain(root=board.root, board=board.id)
    _nudge_manager(manager, reason="task_review", board=board.id)
    return task


def reopen_task(ref: str, *, reason: str, session_ref: str | None = None) -> TeamTask:
    """Send a task back to the pool with feedback (``task reopen``).

    The reason lands on the pipe as a task-linked event, so whoever picks the
    task up next (usually its previous owner's loop) sees the feedback.
    """
    _require_enabled()
    _DELIVERY.set(None)
    with store_session() as store:
        session = _resolve_session(store, session_ref)
        task = find_task(store, ref, lambda: _caller_board_id(store, session, None))
        if task is None:
            raise KeyError(ref)
        manager = _nudge_target(store, session.id if session else None, task.project_id, task=task)
        reopened = store.reopen_task(task.id)
        event = _emit(
            store,
            reopened.project_id,
            "task_reopened",
            f"{reopened.title} — {reason}",
            session_id=session.id if session else None,
            task_id=reopened.id,
        )
        board = _board_of(store, reopened.project_id)
    _record_delivery(event, board)
    distill_service.spawn_drain(root=board.root, board=board.id)
    _nudge_manager(manager, reason="task_reopened", board=board.id)
    return reopened


def next_task(
    *,
    role: str | None = None,
    status: TaskStatus = "todo",
    claim: bool = False,
    session_ref: str | None = None,
    cwd: Path | None = None,
) -> TeamTask | None:
    """The oldest pickable task for a role — the heart of a looped session.

    With ``claim`` (only valid for ``todo``), the returned task is atomically
    claimed; a race with another looper simply moves on to the next task.
    """
    _require_enabled()
    _DELIVERY.set(None)
    if claim and status != "todo":
        raise ValueError("--claim only applies to todo tasks")
    with store_session() as store:
        session = _resolve_session(store, session_ref)
        board = _board(store, session, cwd, write=claim)
        claimant = session.id if session else "cli"
        lease = _now() + timedelta(minutes=orchestrator.lease_minutes())
        event = None
        # The task this session was spawned for comes first — through the same
        # query and predicate as every other candidate. Oldest-first is right for
        # a looper picking from a pool; it was wrong for an agent the manager
        # started FOR a task: that one took whatever was oldest, and two spawned
        # together raced for the same one while their own sat idle.
        row = _fleet_row_hint(store, session, claim=claim)
        prefer = row.task_id if row is not None else None
        picked: TeamTask | None = None
        while True:
            task = store.next_task(board.id, role=role, status=status, prefer=prefer)
            if task is None or not claim:
                picked = task
                break
            if store.claim_task(task.id, claimant, lease):
                claimed = store.get_task(task.id)
                assert claimed is not None  # just claimed
                event = _emit(
                    store,
                    claimed.project_id,
                    "task_claimed",
                    claimed.title,
                    session_id=session.id if session else None,
                    task_id=claimed.id,
                )
                picked = claimed
                break
            # Lost the race for this one — the next loop iteration sees the
            # following todo task (the winner's claim moved this one to doing).
    if event is not None:
        _record_delivery(event, board)
    return picked


def block_task(ref: str, *, reason: str, session_ref: str | None = None) -> TeamTask:
    """Mark a task blocked, with the reason on the pipe (``task block``)."""
    _require_enabled()
    task, board, manager = _finish_task(
        ref, "blocked", "task_blocked", note=reason, session_ref=session_ref
    )
    distill_service.spawn_drain(root=board.root, board=board.id)
    _nudge_manager(manager, reason="task_blocked", board=board.id)
    return task


def drop_task(ref: str, *, session_ref: str | None = None) -> TeamTask:
    """Drop a task that is no longer worth doing (``task drop``)."""
    _require_enabled()
    task, _, _ = _finish_task(ref, "dropped", "task_dropped", session_ref=session_ref, wakes=False)
    return task


def release_task(ref: str, *, session_ref: str | None = None) -> TeamTask:
    """Give a claimed task back to the pool (``task release``)."""
    _require_enabled()
    _DELIVERY.set(None)
    with store_session() as store:
        session = _resolve_session(store, session_ref)
        task = find_task(store, ref, lambda: _caller_board_id(store, session, None))
        if task is None:
            raise KeyError(ref)
        released = store.release_task(task.id)
        event = _emit(
            store,
            released.project_id,
            "task_released",
            released.title,
            session_id=session.id if session else None,
            task_id=released.id,
        )
        board = _board_of(store, released.project_id)
    _record_delivery(event, board)
    return released


def distill_now(
    cwd: Path | None = None, *, rescan: bool = False, board: str | None = None
) -> int | None:
    """Drain the pipe into the project brain synchronously (``team distill``): this
    directory's board, or ``board``, the one a detached drain was spawned for
    (``distill.spawn_drain``).

    ``None`` means another drain (usually a detached one) is already running.
    """
    _require_enabled()
    return distill_service.drain(cwd, rescan=rescan, board=board)


def recall(query: str, cwd: Path | None = None) -> str | None:
    """Search the project brain (``recall``); ``None`` = brain unavailable.

    Recall is human-invoked and latency-tolerant, so any undistilled backlog
    is drained first — a first recall on a busy pipe initialises the brain
    and takes a few seconds; subsequent ones are instant.
    """
    _require_enabled()
    project = orchestrator.team_project(cwd)
    with store_session() as store:
        backlog = distill_service.pending(store, project.id)
    if backlog:
        distill_service.drain(board=project.id)  # a busy (None) drain still means progress
    return brain.recall(project.id, query)


# --- hook integration ---------------------------------------------------------


def _shared_row_banner(
    known: TeamSession | None, transcript_path: str | None, now: datetime
) -> str:
    """Warn when a SECOND live agent is occupying one session row.

    ``team_session.transcript_path`` is the only per-agent value the row carries,
    and ``upsert_session`` overwrites it last-writer-wins
    (``transcript_path = COALESCE(excluded.transcript_path, transcript_path)``).
    So two agents handed the same ``session_id`` merge into one identity leaving
    no trace: the board lists one teammate, both agents read the same short id as
    their own, and every event either writes is stamped with it.

    That is not cosmetic. Attribution becomes unrecoverable -- in one observed
    shift it sent teammates to the wrong agent three times for follow-up on
    findings the other had made. And while ``claim_task`` is genuinely atomic (a
    live lease cannot be stolen), the board renders ``[doing @<id>]``, which the
    twin reads as "I claimed this" -- so the claim protocol's whole purpose,
    letting a teammate see a task is taken, stops applying between exactly the two
    agents that most need separating.

    WARN, NEVER REASSIGN. The CLI cannot know which occupant is "real", and
    guessing would be worse than reporting. This is the same posture ``_board``
    already takes on a cwd that disagrees with the session's board: a warning,
    never a reroute.

    Silent unless BOTH transcripts are known and the row is FRESH. A differing
    transcript on a stale row is an ordinary resume (same id, new conversation),
    not a collision, and warning on it would train people to ignore the banner.
    """
    if known is None or not transcript_path or not known.transcript_path:
        return ""
    if known.transcript_path == transcript_path:
        return ""
    if now - known.last_seen_at > _STALE_AFTER:
        return ""
    return (
        "<aisquare-session-collision>\n"
        f"WARNING: session {short_id(known.id)} is being written by TWO live agents.\n"
        "This row was last heartbeat from a different transcript, within the freshness\n"
        "window, so another agent is acting as this same board identity right now.\n"
        f"  stored : {known.transcript_path}\n"
        f"  yours  : {transcript_path}\n"
        "CONSEQUENCES while this holds: notes and task claims from both agents are\n"
        "recorded under this one id and cannot be told apart afterwards; a task shown\n"
        "as claimed by you may have been claimed by the other agent. Coordinate in\n"
        "prose and state which agent you are -- the claim protocol cannot arbitrate\n"
        "between you. Nothing has been reassigned or rerouted.\n"
        "</aisquare-session-collision>\n\n"
    )


def _transcript_to_keep(known: TeamSession | None, transcript_path: str | None) -> str | None:
    """The transcript a session start records: the one reported, unless the row already names
    a transcript ON DISK and the reported one is not (P0-W3b).

    Measured by P0-W0 on Claude Code 2.1.309 and 2.1.223: ``claude --resume <path>`` under
    another ``CLAUDE_CONFIG_DIR`` — every resumed ``fleet switch`` and usage-limit hand-over —
    hands ``SessionStart`` a path under the NEW account's ``projects/`` that is never created;
    the records keep going to the original file, and ``UserPromptSubmit`` hands that one.
    Recorded, the missing path raised :func:`_shared_row_banner` at every prompt, and the next
    restart or switch found no file to resume and started fresh. Two paths that both exist
    are two transcripts, and recorded as before: the banner is for them.
    """
    if known is None or not known.transcript_path or not transcript_path:
        return transcript_path
    if transcript_path == known.transcript_path or Path(transcript_path).is_file():
        return transcript_path
    return known.transcript_path if Path(known.transcript_path).is_file() else transcript_path


def _heals(session: TeamSession, transcript_path: str | None) -> bool:
    """Whether a prompt's ``transcript_path`` should replace the row's: the row's is not on
    disk and the prompt's is (:func:`_transcript_to_keep`'s other half)."""
    recorded = session.transcript_path
    if not transcript_path or not recorded or transcript_path == recorded:
        return False
    return not Path(recorded).is_file() and Path(transcript_path).is_file()


def _briefing_events(store: ContextStore, project_id: str) -> list[TeamEvent]:
    """The recent updates an agent is briefed with: the board's last :data:`_BOARD_EVENTS`
    that are an agent's to read, :data:`HUMAN_BOARD_KINDS` left out in the query.

    Read unfiltered, the five were whatever came last — and after the owner had asked
    the captain about the board a few times, that was five audit lines carrying the
    owner's words, with the decisions and results pushed out (review of #240, finding 11).
    """
    return store.recent_events(project_id, exclude_kinds=HUMAN_BOARD_KINDS, limit=_BOARD_EVENTS)


def hook_session_start(
    session_id: str,
    cwd: Path | None,
    source: str | None,
    *,
    transcript_path: str | None = None,
    model: str | None = None,
    effort: str | None = None,
) -> str:
    """Register this session with the orchestrator and return the board injection.

    Silent (returns ``""``) unless the orchestrator is enabled and this project has
    been activated — or the session was launched with ``AISQUARE_ROLE``,
    which activates it. With ``[projects] capture`` off, silent too in a project
    nobody added, role or not: ``launch`` adds its own (``team on``), a fleet spawn its.
    """
    if not orchestrator.team_enabled():
        return ""
    with store_session() as store:
        project = orchestrator.team_project(cwd)
        if not project_service.tracked(store, project):
            return ""  # capture is off and nobody added it: no session row, no board (#139)
        role = orchestrator.env_role()
        if not store.team_active(project.id) and role is None:
            return ""
        # Only now, for the session row below: captured before the board was asked, every
        # session start in any directory took the store's write lock for nothing (review of
        # #259, round 18).
        project_service.capture(store, project)
        known = store.get_session(session_id)
        now = _now()
        # The account the session runs on is the reported path's, even where the
        # transcript recorded is the original file (`_transcript_to_keep`).
        account = session_account(transcript_path)
        transcript_path = _transcript_to_keep(known, transcript_path)
        # Computed before upsert_session(), which overwrites transcript_path.
        collision = _shared_row_banner(known, transcript_path, now)
        # Self-reported by the payload — validated before it can reach any
        # other session's injected context.
        model = harness.clean_model_id(model)
        effort = harness.clean_effort(effort)
        asked_persona, persona_note, named = _asked_persona(store, project.id)
        session = store.upsert_session(
            TeamSession(
                id=session_id,
                project_id=project.id,
                role=role or (known.role if known else "unassigned"),
                started_at=now,
                last_seen_at=now,
                cursor=store.latest_seq(project.id),
                transcript_path=transcript_path,
                account=account,
                model=model,
                effort=effort,
                # Recorded as ASKED, loadable or not (§3.7): this pane's fleet row
                # persona, else the variable (§4.7, P20); asking for none keeps what
                # the session row already holds (the store's COALESCE).
                persona=asked_persona,
            )
        )
        if role is not None and known is not None and known.role != role:
            session = store.update_session(session.id, role=role)
        if experimental.captain_role(base_role(session.role)):
            # Bound like every fleet row (rule 1 of the fleet-row section), or a
            # `/clear` left the row on the ended id and `say` waited on a session that
            # would never answer. The captain has no task, so there is nothing to brief.
            _assignment(store, session.id, project.id, named=named)
            _settle_owed_persona(store, session)  # this start briefs the persona itself
            if source != _COMPACT_SOURCE:
                # A captain that just started, cleared or resumed sits at its prompt
                # until something is typed — whose own hook says `working`. Left at
                # the upsert's `working`, the row read busy for its whole fresh window
                # and `say` refused to type into it (T2 fix round). A compaction can
                # come mid-turn, so it keeps what the turn said.
                store.touch_session(session.id, state="waiting")
                session = store.get_session(session.id) or session
            # The captain's own briefing (T2, 13121): it has no shell, so no `aisquare
            # task …` protocol lines, and the home board's captain_action lines are the
            # OWNER's audit, not the captain's context.
            return collision + _captain_briefing(project, session, persona_note)
        # Before the board is read below: the assignment may move this agent's
        # claims onto its new id, and a board read first still named the old one.
        # ``source`` plays no part — see rule 1 of the fleet-row section. The row the window
        # names was read once, above, for the persona, the assignment and the cycle's project.
        assigned = _assignment(store, session.id, project.id, named=named)
        _settle_owed_persona(store, session)  # this start briefs the persona itself
        fleet = _cycle_project(store, project, cwd, session.id, named=named)
        # Presence is board state, not feed traffic: /clear cycles, resumes and
        # ephemeral `claude -p` children would otherwise spam join/left pairs.
        return collision + _render_board(
            project,
            store.team_sessions(project.id),
            store.team_tasks(project.id),
            _briefing_events(store, project.id),
            me=session,
            assigned=assigned,
            briefing=True,
            persona_note=persona_note,
            # Asked only when the briefing needs it: one git call, which a session with no
            # persona, or one whose cycle names its project, never pays.
            persona_root=_directory_root(cwd) if session.persona and fleet is None else None,
            labels=lambda: slot_labels_via(store),
            fleet=fleet,
        )


def hook_prompt_heartbeat(
    session_id: str,
    cwd: Path | None,
    *,
    transcript_path: str | None = None,
    model: str | None = None,
    effort: str | None = None,
) -> str:
    """Heartbeat on prompt submit; returns the teammate delta to inject (or '').

    A session unknown to the orchestrator but prompting inside an *active* project
    joins right here (the orchestrator may have been activated after it started) and
    receives the full board + protocol instead of a delta.

    Every prompt stamps the session's ``prompted_at`` and takes its fleet row's first
    message (``store.mark_prompted``, P0-W1a): the acknowledgement ``fleet spawn``,
    ``restart`` and ``switch`` wait for, and a typed delivery asks before it presses Enter
    again. The row is the window's own (``AISQUARE_FLEET_AGENT``), else the one bound to the
    session. One indexed update, no tmux call.
    """
    if not orchestrator.team_enabled():
        return ""
    with store_session() as store:
        session = store.get_session(session_id)
        if session is None:
            project = orchestrator.team_project(cwd)
            if not project_service.tracked(store, project):
                return ""  # as at session start: nothing on a board nobody added
            role = orchestrator.env_role()
            if not store.team_active(project.id) and role is None:
                return ""
            # Only now, as at session start: before the board was asked, every prompt of a
            # session on no board took the store's write lock again, for a directory
            # `capture_prompt` had captured already (review of #259, round 19).
            project_service.capture(store, project)
            now = _now()
            session = store.upsert_session(
                TeamSession(
                    id=session_id,
                    project_id=project.id,
                    role=role or "unassigned",
                    started_at=now,
                    last_seen_at=now,
                    cursor=store.latest_seq(project.id),
                    transcript_path=transcript_path,
                    account=session_account(transcript_path),
                    model=harness.clean_model_id(model),
                    effort=harness.clean_effort(effort),
                    prompted_at=now,
                )
            )
            store.mark_prompted(session.id, now, agent_id=orchestrator.env_fleet_agent())
            # This door binds a fleet row too (an agent that started before the
            # orchestrator was switched on) — and does so BEFORE the tasks are
            # read: computed as an argument, the claim it moved was rendered
            # under its old holder on this very board (review of #135).
            named = _hook_named_row(store)
            assigned = _assignment(store, session.id, project.id, named=named)
            return _render_board(
                project,
                store.team_sessions(project.id),
                store.team_tasks(project.id),
                _briefing_events(store, project.id),
                me=session,
                assigned=assigned,
                labels=lambda: slot_labels_via(store),
                fleet=_cycle_project(store, project, cwd, session.id, named=named),
            )
        # Same check as session_start, on the path that actually runs every turn.
        # It must survive the empty-delta early return below: a collision warning
        # that only rides along with unrelated teammate traffic would go unseen for
        # exactly as long as the two agents were quiet, which is when the
        # interleaved claims do their damage.
        if _heals(session, transcript_path):
            # A row that recorded a transcript which never appeared, this prompt's being
            # the one on disk (P0-W3b, a row written before `_transcript_to_keep`): pointed
            # at it, so the next restart resumes it and no banner reads it as a second agent.
            assert transcript_path is not None  # `_heals` says so
            store.set_transcript_path(session.id, transcript_path)
            session = session.model_copy(update={"transcript_path": transcript_path})
        collision = _shared_row_banner(session, transcript_path, _now())
        # The fleet-row bind the start hook could not make is tried again here,
        # and the briefing it was owed comes with it (rule 2, second half).
        late = _late_assignment(store, session)
        briefing = "\n".join(_assignment_lines(late, session)) + "\n" if late is not None else ""
        # What `persona attach` owed an agent that was busy is handed over here, to it alone.
        briefing += _owed_persona_briefing(store, session)
        lease = _now() + timedelta(minutes=orchestrator.lease_minutes())
        store.renew_leases(session.id, lease)
        store.mark_prompted(session.id, _now(), agent_id=orchestrator.env_fleet_agent())
        # The board's newest seq, read BEFORE the delta: with nothing to deliver the
        # cursor moves there, past the rows the query left out. Read first, it cannot
        # pass an event the delta did not see — whatever is written after has a later seq.
        newest = store.latest_seq(session.project_id)
        # Bells, notification lines and the captain's audit are for the human, not
        # teammate context — left out IN the query, so the limit counts what is
        # delivered: dropped after it, a run of audit lines was the whole page, prompt
        # after prompt, with a coder's result behind it (review of #240, finding 11).
        events = store.events_since(
            session.project_id,
            session.cursor,
            exclude_session=session.id,
            exclude_kinds=HUMAN_BOARD_KINDS,
            limit=_DELTA_LIMIT * 3 + 1,
        )
        if not events or not orchestrator.delta_enabled():
            # Nothing delivered is still something read: the cursor moves past it, the
            # left-out rows included, rather than stay in front of them for every later
            # prompt to scan again.
            read_to = events[-1].seq if events else newest
            cursor = read_to if read_to > session.cursor else None
            store.touch_session(session.id, cursor=cursor, state="working")
            return collision + briefing
        truncated = len(events) > _DELTA_LIMIT
        shown = events[:_DELTA_LIMIT]
        store.touch_session(session.id, cursor=shown[-1].seq, state="working")
        roles = {s.id: s.role for s in store.team_sessions(session.project_id)}
        board = _board_of(store, session.project_id).name
        # One read, and only when a line names a task: this runs on every prompt.
        numbers = store.task_numbers(session.project_id) if any(e.task_id for e in shown) else {}
        return (
            collision
            + briefing
            + _render_delta(shown, roles, truncated=truncated, board=board, numbers=numbers)
        )


def hook_stop(
    session_id: str, cwd: Path | None, *, stop_hook_active: bool = False
) -> StopDecision | None:
    """The session finished its turn: it is now waiting for input.

    Also renews claim leases — the end of a long agentic turn is exactly when
    a lease is at its oldest.

    A MANAGER may not get to wait (docs/plans/fleet-tui.md §7.3). When teammates
    put decisions on the board since its cursor — see :data:`MANAGER_WAKE_KINDS`
    — the returned :class:`StopDecision` keeps it going with the delta as the
    reason, its cursor advances past what it was shown, and the row says
    ``working``. Three guards keep that from becoming a loop: ``stop_hook_active``
    (Claude Code is already continuing on a stop hook, so this turn's news waits
    for the next prompt's delta or a nudge — nothing is lost, only deferred), the
    hourly cap in ``[fleet] max_continuations_per_hour``, and the cursor itself,
    which is why the same events can never wake it twice. Every other role gets
    ``None`` and exactly the behaviour it always had.

    The wake-up is an extra on top of the contract, so it fails open on its own:
    the row is marked waiting first, then :class:`ManagerWakeupError` carries the
    cause to the CLI's cost line.

    A session a hand-over has marked (:data:`HANDOVER_STATE`) keeps the mark and
    is not kept going: ``/exit`` has been typed into it, and the turn ending is
    only what that ``/exit`` waits behind.
    """
    if not orchestrator.team_enabled():
        return None
    with store_session() as store:
        session = store.get_session(session_id)
        if session is None:
            return None
        store.renew_leases(session.id, _now() + timedelta(minutes=orchestrator.lease_minutes()))
        if session.state == HANDOVER_STATE:
            # `fleet switch`, or `fleet restart` of a running agent, marked it and typed
            # `/exit`, which an agent mid-turn runs once the turn is over. `waiting`
            # written here (or a manager's wake-up writing `working`) replaced the mark,
            # so the `SessionEnd` that followed released the claims the replacement was
            # to inherit, and a listing ended the row under the hand-over (review of
            # #163, round 2). A manager woken here would take its news down with it:
            # the events stay past its cursor, for whatever comes back.
            return None
        failure: Exception | None = None
        deferred: str | None = None
        manager: str | None = None
        if session.role == MANAGER_ROLE:
            try:
                decision = _manager_wakeup(store, session, stop_hook_active=stop_hook_active)
                if decision is None and stop_hook_active:
                    deferred = _deferred_wake_reason(store, session)
                    if deferred is not None:
                        manager = _own_manager_row(store, session)
            except Exception as exc:  # waiting is the contract; the wake-up is the extra
                failure = exc
            else:
                if decision is not None:
                    return decision
        # Over a hand-over's mark the store keeps the mark: this can be the turn
        # of an agent `fleet switch` is exiting, whose SessionEnd must still park
        # the claims (review of the #205 fold, round 1).
        store.touch_session(session.id, state="waiting")
        # A turn ended, and only a prompt starts one: the first message was taken even if its
        # UserPromptSubmit could not record it (P0-W1a; review of #330, round 1).
        store.take_first_prompt(
            _now(), session_id=session.id, agent_id=orchestrator.env_fleet_agent()
        )
    if deferred is not None and manager is not None:
        # After the row says waiting, never before: nudge_manager refuses every
        # other state. Outside the `with` too — it opens its own connection.
        _nudge_manager(manager, reason=deferred, board=session.project_id)
    if failure is not None:
        raise ManagerWakeupError(failure)
    return None


def _own_manager_row(store: ContextStore, session: TeamSession) -> str | None:
    """The project of the live ``manager`` row of ``session`` itself, the one its deferred wake
    types into (``fleet.nudge_manager``), in whichever project it is: under a hub, not the
    board's. ``None`` for a manager no row holds, launched by hand, or a numbered seat: its
    deferred news waits for its next prompt. Asked as any writer is (:func:`_nudge_target`),
    a manager with no row woke the one manager that reads the board, another project's, and
    typed its wake into that pane (review of #259, round 26, 7/11).
    """
    row = store.fleet_agent_for_session(session.id)
    return row.project_id if row is not None and row.label == MANAGER_ROLE else None


def continuation_key(session_id: str, when: datetime) -> str:
    """The ``team_meta`` key counting a manager's continuations in the UTC hour of ``when``."""
    return f"continuations:{session_id}:{when.astimezone(UTC).strftime('%Y-%m-%dT%H')}"


def continuation_cap() -> int:
    """``[fleet] max_continuations_per_hour`` — a default like every other one (§3.10).

    A config that will not load costs the customisation, never the wake-up:
    the same fail-open shape as ``services.fleet.settings``.
    """
    try:
        return load_config().fleet.max_continuations_per_hour
    except Exception:
        return FleetSettings().max_continuations_per_hour


def _count(raw: str | None) -> int:
    try:
        return int(raw or 0)
    except ValueError:
        return 0


def _wake_candidates(store: ContextStore, me: TeamSession) -> list[TeamEvent]:
    """The board since a manager's cursor, exactly as its next prompt would read it.

    One function so the wake-up and the deferral below can never disagree about
    the window they are judging — same source, same exclusions, same limit as
    :func:`hook_prompt_heartbeat`. Bells, notification lines and the captain's audit
    (:data:`HUMAN_BOARD_KINDS`) are for the human, not teammate context, and are left
    out IN the query: a Stop that wakes nobody moves no cursor, so dropped after the
    LIMIT, a run of audit lines was the whole page at every Stop, and the result behind
    it woke nobody until something else was written (review of #240, finding 11).
    """
    return store.events_since(
        me.project_id,
        me.cursor,
        exclude_session=me.id,
        exclude_kinds=HUMAN_BOARD_KINDS,
        limit=_DELTA_LIMIT * 3 + 1,
    )


def _deferred_wake_reason(store: ContextStore, me: TeamSession) -> str | None:
    """The kind of the first wake event a ``stop_hook_active`` Stop left undelivered.

    ``None`` when nothing wake-worthy is pending, or when deltas are muted (the
    nudge's whole payload is the delta the next prompt injects, so a nudge with
    that door shut is noise).

    Why this exists: ``stop_hook_active`` defers this turn's news to "the next
    prompt's delta or a nudge", and neither was scheduled. The Stop that ends a
    stop-hook continuation is *precisely* the Stop Claude Code marks
    ``stop_hook_active``, so "the next Stop will deliver it" is not true of the
    next Stop; and the write-time nudges of those events were already refused
    while the manager was working. At the end of a burst — every coder finished
    — no further board write is coming to nudge it, and the manager parks as
    waiting with unseen ``question``/``task_done`` events past its cursor. That
    is a stalled fleet, not a deferral, so the deferral schedules its own
    delivery: path 2's nudge, from the manager's own process, which produces the
    ``UserPromptSubmit`` the delta rides on. Bounded by construction — the
    prompt's delta advances the cursor, and the nudge is debounced and refused
    for any state but ``waiting``.
    """
    if not orchestrator.delta_enabled():
        return None
    for event in _wake_candidates(store, me):
        if _wakes_manager(event):
            return f"deferred:{event.kind}"
    return None


def _manager_wakeup(
    store: ContextStore, me: TeamSession, *, stop_hook_active: bool
) -> StopDecision | None:
    """Keep a manager going on fresh board decisions, or ``None`` to let it wait.

    Reads the delta exactly as :func:`hook_prompt_heartbeat` does — same source,
    same exclusions, same limit — because the reason IS that delta, delivered a
    turn early. Muted deltas (``AISQUARE_TEAM_DELTA=0``) mute this too: it is the
    same injection through a different door.

    Order of the writes matters. The hourly counter is bumped BEFORE the cursor
    moves and the row flips to ``working``: a lost increment costs one of thirty
    continuations, while a moved cursor with no continuation printed would cost
    the events themselves.
    """
    if stop_hook_active or not orchestrator.delta_enabled():
        return None
    events = _wake_candidates(store, me)
    if not any(_wakes_manager(event) for event in events):
        return None
    key = continuation_key(me.id, _now())
    used = _count(store.get_meta(key))
    if used >= continuation_cap():
        return None
    truncated = len(events) > _DELTA_LIMIT
    shown = events[:_DELTA_LIMIT]
    roles = {s.id: s.role for s in store.team_sessions(me.project_id)}
    board = _board_of(store, me.project_id).name
    numbers = store.task_numbers(me.project_id) if any(e.task_id for e in shown) else {}
    store.set_meta(key, str(used + 1))
    store.touch_session(me.id, cursor=shown[-1].seq, state="working")
    return StopDecision(
        reason=_render_wakeup(shown, roles, truncated=truncated, board=board, numbers=numbers),
        cursor=shown[-1].seq,
    )


#: Claude Code ``notification_type`` values that mean A HUMAN IS NEEDED: a
#: permission prompt, an MCP elicitation (a form or a URL to visit), a sub-agent
#: asking its user. These ring the bell (``attention``); nothing else does.
ATTENTION_NOTIFICATIONS: frozenset[str] = frozenset(
    {
        "permission_prompt",
        "elicitation_dialog",
        "elicitation_url_dialog",
        "agent_needs_input",
    }
)

#: Types that are ROUTINE and change nothing on the board: the idle notice
#: ("Claude is waiting for your input", sent ~60 s into every pause — 164 of the
#: 183 bells measured in #153) and the housekeeping of elicitations closing.
QUIET_NOTIFICATIONS: frozenset[str] = frozenset(
    {"idle_prompt", "elicitation_complete", "elicitation_response"}
)

#: The idle notice's text, which identifies it when Claude Code sent no type (a
#: version before ``notification_type`` existed). It is the one text that matters:
#: without it every old-style notification rang the bell, as before #153.
_IDLE_MESSAGE = "waiting for your input"


def classify_notification(notification_type: str | None, message: str | None) -> str:
    """``attention`` · ``quiet`` · ``notice`` — what a Claude Code notification means for the board.

    By ``notification_type`` first (#153): the documented values are sorted
    above, and an UNKNOWN type is a ``notice`` — a feed line, never a bell,
    because the cost of a false bell (every agent reading as stuck, the real
    prompt lost in the noise) is the defect this exists to end, and the cost of
    a missed bell on a brand-new type is one feed line the operator can read.
    Without a type — older Claude Code — the message decides: the idle notice
    is quiet, and everything else, a permission or browser request included,
    keeps the pre-#153 behaviour (attention), so an old install loses nothing.
    """
    if notification_type:
        if notification_type in ATTENTION_NOTIFICATIONS:
            return "attention"
        if notification_type in QUIET_NOTIFICATIONS:
            return "quiet"
        return "notice"
    return "quiet" if _IDLE_MESSAGE in (message or "").lower() else "attention"


def hook_notification(
    session_id: str,
    cwd: Path | None,
    message: str | None,
    *,
    notification_type: str | None = None,
) -> None:
    """A Claude Code notification: ring the bell only for what needs a human (#153).

    ``attention`` (a permission prompt, an elicitation, a sub-agent asking) flips
    the row to ``attention``; the feed event is emitted only on the transition
    INTO it — Claude re-notifies while parked, and a per-notice event floods the
    feed with lines nobody can act on twice. ``quiet`` (the idle notice) changes
    nothing at all: the agent has finished a turn and is ``waiting``, which the
    board already says, and it was these — nine bells in ten — that made the
    bell meaningless. ``notice`` (``auth_success``, the quota auto-resume
    family, a sub-agent finishing, an unknown type) is a feed line for the
    human board, no state change — and, like the bell, never part of an agent's
    delta or a manager's wake-up (:data:`HUMAN_BOARD_KINDS`).
    """
    if not orchestrator.team_enabled():
        return
    kind = classify_notification(notification_type, message)
    if kind == "quiet":
        return
    with store_session() as store:
        session = store.get_session(session_id)
        if session is None:
            return
        if kind == "notice":
            _emit(
                store,
                session.project_id,
                "notice",
                message or notification_type or "notification",
                session_id=session.id,
            )
            return
        if session.state == HANDOVER_STATE:
            # A prompt that came up in a pane a hand-over is closing: nobody is to
            # answer it, and `attention` written over the mark had the `SessionEnd`
            # that follows release the claims the replacement was to inherit (review
            # of #163, round 2) — as `hook_stop` and `hook_stop_failure` keep it too.
            return
        if store.mark_attention(session.id):
            _emit(
                store,
                session.project_id,
                "attention",
                message or "needs your attention",
                session_id=session.id,
            )


@dataclass(frozen=True)
class TurnFailure:
    """What :func:`hook_stop_failure` recorded: the session, the error, and — for a
    usage limit — what the message said about the reset."""

    session: TeamSession
    error: str
    notice: claude_accounts_core.LimitNotice | None
    already_limited: bool = False
    """The row was ``limited`` before this hook fired: Claude Code re-fires
    ``StopFailure`` for the same window, and a second firing must start no second
    hand-over (review of #205, second round)."""

    @property
    def limited(self) -> bool:
        return self.error == "rate_limit"


def hook_stop_failure(
    session_id: str,
    *,
    error: str | None,
    message: str | None,
    details: str | None,
    handing_over: bool = False,
) -> TurnFailure | None:
    """The turn ended on an API error — Claude Code's ``StopFailure`` (#146).

    ``rate_limit`` is the one that matters: a subscription's allowance ran out
    (``You've hit your session limit · resets 12:30am (America/Toronto)``, the
    text the hook hands over as ``last_assistant_message``), or an API key was
    throttled. Either way the agent can do nothing until something changes, so
    the row is parked as ``limited`` with the reset time the message named
    (``core.claude_accounts.parse_limit_notice``; ``None`` when it named none),
    a ``limited`` event goes on the feed ON THE TRANSITION only — Claude Code
    can fire the hook again for the same window — and a waiting manager is
    nudged (§7.3 path 2), the event kind being a wake kind for its own Stop.

    Every other error (``overloaded``, ``authentication_failed``,
    ``billing_error``…) ends the turn like a Stop would — the row says
    ``waiting``, unless ``fleet switch`` has marked it :data:`HANDOVER_STATE` —
    with a ``turn_failed`` feed line naming it, so the board does not show a
    session as mid-turn for thirty minutes because its turn died.

    ``handing_over`` is the hook's decision (``services.hooks.turn_failed``) that the
    automatic hand-over will move this agent: the ``limited`` line then says so
    (:data:`HANDING_OVER_SAID`) instead of naming ``fleet switch``, and no manager is
    nudged — one limit, one switch (#277). A hand-over that then fails says so with
    ``handover_failed`` (:func:`hook_handover_failed`), which does wake it.
    """
    if not orchestrator.team_enabled():
        return None
    kind = (error or "unknown").strip() or "unknown"
    text = (message or details or kind).strip()
    with store_session() as store:
        session = store.get_session(session_id)
        if session is None:
            return None
        if kind != "rate_limit":
            # Not over a hand-over's mark either (the `rate_limit` branch below
            # keeps it too): an `overloaded` landing while `fleet switch` waits
            # for the `/exit` wrote `waiting` over it, and the SessionEnd that
            # follows released the claims the replacement was to inherit
            # (review of #205, fourth round). The feed line still goes out.
            if session.state != HANDOVER_STATE:
                store.touch_session(session.id, state="waiting")
            _emit(
                store, session.project_id, "turn_failed", f"{kind}: {text}", session_id=session.id
            )
            refreshed = store.get_session(session.id) or session
            return TurnFailure(refreshed, kind, None)
        notice = claude_accounts_core.parse_limit_notice(message, now=_now())
        # A row `fleet switch` has marked HANDOVER_STATE is already handled: a
        # re-fire during the hand-over must neither start a second worker nor
        # write `limited` over the mark — its SessionEnd would then release the
        # claims the resume was to inherit (review of #205, third round).
        already_limited = session.state in ("limited", HANDOVER_STATE)
        if session.state != HANDOVER_STATE:
            store.mark_limited(session.id, notice.resets_at if notice is not None else None)
        # The session's row in whichever project it is: under AISQUARE_TEAM_HUB the session
        # is on the hub's board and its row in its spawn's project, and asked by the board,
        # the line named no command and the agent by its short id (review of #259, round 23).
        agent = store.fleet_agent_for_session(session.id)
        label = agent.label if agent is not None else (session.label or short_id(session.id))
        manager: str | None = None
        if not already_limited:
            # The row just read names the manager's project (`_nudge_target`'s rule); asked
            # again there, the same row was read twice (review of #259, round 25, 8/10).
            manager = (
                None
                if handing_over
                else agent.project_id
                if agent is not None
                else _nudge_target(store, None, session.project_id)
            )
            _emit(
                store,
                session.project_id,
                LIMITED_HANDING_OVER if handing_over else "limited",
                _limited_text(
                    label,
                    text,
                    notice,
                    project=store.get_project(agent.project_id) if agent is not None else None,
                    handing_over=handing_over,
                ),
                session_id=session.id,
            )
        refreshed = store.get_session(session.id) or session
    if manager is not None:  # not already limited, and no hand-over coming
        # After the row says limited, never before, and outside the `with`
        # (it opens its own connection): path 2 types into a WAITING manager;
        # a working one reads the event on its next delta or its own Stop. The manager of
        # the agent's own project, whose row is beside the agent's (`_nudge_target`).
        _nudge_manager(manager, reason=f"{label} hit its usage limit", board=session.project_id)
    return TurnFailure(refreshed, kind, notice, already_limited=already_limited)


def fleet_switches(store: ContextStore, sessions: list[TeamSession]) -> dict[str, str]:
    """The ``fleet switch`` that moves each ``limited`` session's agent, by session id: the
    project and label of its own live fleet row, in whichever project that is.

    Not the board's project: under ``AISQUARE_TEAM_HUB`` the board is the hub's, every
    session on it is a fleet agent of some other project, and ``fleet switch --project
    <hub> <label>`` answered "no live agent" (review of #259, round 22). The row is the one
    the store records against the session (``fleet_agent_for_session``), the lookup the
    ``limited`` feed line and the hand-over make too. A session with no live row, or whose
    row's project is gone, has none. Read through the board's own store session
    (:func:`board_read`), and only when a live session is limited. A read that fails
    costs the commands, never the board — logged, as
    :func:`_cycle_project` does: swallowed silently, a store or schema regression took the
    switch off every limited chip with nothing anywhere saying so (round 23).
    """
    limited = [s.id for s in sessions if s.ended_at is None and s.state == "limited"]
    if not limited:
        return {}
    switches: dict[str, str] = {}
    try:
        for session_id in limited:
            agent = store.fleet_agent_for_session(session_id)
            project = store.get_project(agent.project_id) if agent is not None else None
            if agent is not None and project is not None:
                switches[session_id] = project_service.fleet_command("switch", project, agent.label)
    except Exception as exc:
        _log.warning(
            "the fleet rows of %d limited session(s) could not be read, so their chips name "
            "no fleet switch (%s: %s)",
            len(limited),
            type(exc).__name__,
            exc,
        )
        return {}
    return switches


def _limited_text(
    label: str,
    message: str,
    notice: claude_accounts_core.LimitNotice | None,
    *,
    project: ProjectInfo | None,
    handing_over: bool = False,
) -> str:
    """The feed line: what stopped, when it lifts, and the one command that moves it — for a
    fleet agent, whose ``project`` the command names (review of #259, round 21). With the
    automatic hand-over coming (``handing_over``), no command: the fleet moves it (#277)."""
    if notice is None:
        head = f"{label} is rate limited: {message}"
    else:
        head = f"{label} hit its {notice.window} limit"
        if notice.resets_at is not None:
            # The one formatter (#152; review of #205, finding 9).
            head += f" · resets {claude_accounts_core.format_reset(notice.resets_at, now=_now())}"
    if handing_over:
        head += f' — {HANDING_OVER_SAID} (`on_limit = "switch"`)'
    elif project is not None:
        head += (
            f" — `{project_service.fleet_command('switch', project, label)}` moves it to the "
            "account with the most headroom (or wait for the reset)"
        )
    return head


def hook_handover_failed(
    project_id: str, text: str, *, session_id: str | None = None, label: str | None = None
) -> None:
    """The automatic hand-over could not move a limited agent: a ``handover_failed`` event
    (a wake kind) naming ``fleet switch`` or the reset, and a waiting manager nudged, as a
    ``limited`` line nudged it before the hand-over took that over (#277)."""
    if not orchestrator.team_enabled():
        return
    with store_session() as store:
        _emit(store, project_id, "handover_failed", text, session_id=session_id)
        session = store.get_session(session_id) if session_id is not None else None
        agent = store.fleet_agent_for_session(session_id) if session_id is not None else None
        manager = (
            agent.project_id
            if agent is not None
            else _nudge_target(store, None, session.project_id if session else project_id)
        )
    if manager is not None:
        who = label or (short_id(session_id) if session_id else "an agent")
        _nudge_manager(manager, reason=f"{who}: usage-limit hand-over failed", board=project_id)


def hook_note(project_id: str, text: str, *, session_id: str | None = None) -> None:
    """A plain feed note from a hook — what a hand-over decided, and why (#146)."""
    if not orchestrator.team_enabled():
        return
    with store_session() as store:
        _emit(store, project_id, "note", text, session_id=session_id)


def hook_session_end(session_id: str, cwd: Path | None, *, reason: str | None = None) -> None:
    """Mark the session ended and release its claims — unless a fleet agent is only clearing.

    ``reason`` is Claude Code's. :data:`CLEAR_REASON` is a ``/clear``: the
    session id ends, the process does not, and this hook fires BEFORE the
    ``SessionStart`` of the id that follows. Releasing here put the agent's own
    task back in the pool for the length of that gap — a looper's ``task next
    --claim`` could take it, and the agent came back to "Claim it FIRST", or to a
    stop order for what was now a stranger's claim (review of #135, finding 1).
    So a clear from the process that holds a live fleet row's pane retires the
    presence only (rule 2 of the fleet-row section); the start hook that follows
    moves the claims to the new id with the row, in one transaction — or the
    first prompt after it does, should tmux not answer the start hook. Anything
    else — an exit, a logout, a reason this version does not know, a nested
    child clearing ITS session — releases, as every end always did.
    """
    if not orchestrator.team_enabled():
        return
    with store_session() as store:
        session = store.get_session(session_id)
        if session is None:
            return
        # No "left" feed event either way — the board's session panel is the
        # presence view. Released claims are real work signals and do go out;
        # a clear releases nothing, so it says nothing.
        if reason == CLEAR_REASON and _clearing_its_own_pane(store, session):
            store.end_session(session.id, release_claims=False)
        elif session.state == HANDOVER_STATE and _handing_over_a_fleet_row(store, session):
            # A hand-over (``fleet switch``): the agent starts again on another
            # account moments from now — the SAME id resumed, or a fresh one
            # that takes the claims over — so they wait for it exactly as a
            # /clear parks them. ``switch`` releases them itself should the
            # replacement never start (review of #205, finding 6).
            store.end_session(session.id, release_claims=False)
        else:
            released = _release_session(store, session, why="session ended")
            if released.unannounced:
                # The one path with no report to ride on — a hook, whose stderr
                # goes to Claude Code's hook log — and the one that runs most
                # often (every graceful /exit). Silently dropping the value the
                # class was built to carry left the manager treating released
                # work as held with nothing anywhere saying why (round 8 of
                # #203). Said where the hook can say it.
                print(
                    f"aisquare: session {session.id} released {len(released.tasks)} task(s) back "
                    f"to the board but could not announce {', '.join(released.unannounced)} "
                    f"({released.reason}) — the manager will not see them come free until it "
                    "lists the pool",
                    file=sys.stderr,
                )
        root = _project_root(store, session.project_id)
    # Safety drain: catch anything a per-command spawn missed this session, on its board.
    distill_service.spawn_drain(cwd, root=root, board=session.project_id)


@dataclass(frozen=True)
class Released:
    """What a session release did: the claims returned, and any the board was not told about.

    The release is COMMITTED before its ``task_released`` events are written,
    so a failed event must not read as "nothing was released" — `stop` used to
    report an empty release over tasks already back on the board (review of the
    fold). But the event is how the manager learns the work is free
    (``events_since`` / ``terminal_events``), so a failed one is not swallowed
    either: it is named here, for the caller to report (review of #203,
    round 5). ``reason`` is the first failure's text.
    """

    tasks: list[TeamTask]
    unannounced: list[str] = field(default_factory=list)
    reason: str | None = None


def _release_session(store: ContextStore, session: TeamSession, *, why: str) -> Released:
    """Return ``session``'s ``doing`` claims to the pool, ending its presence
    unless that already happened, and say so on the board (``task_released``)."""
    if session.ended_at is None:
        released = store.end_session(session.id, release_claims=True)
    else:
        released = store.release_claims(session.id)
    unannounced: list[str] = []
    reason: str | None = None
    for task in released:
        try:
            _emit(
                store,
                session.project_id,
                "task_released",
                f"{task.title} ({why})",
                session_id=session.id,
                task_id=task.id,
            )
        except sqlite3.Error as exc:  # the store refusing the event; a code fault still raises
            unannounced.append(task.id)
            reason = reason or f"{type(exc).__name__}: {exc}"
    return Released(released, unannounced, reason)


def release_agent_claims(store: ContextStore, agent: FleetAgent, *, why: str) -> Released:
    """A fleet row that has just ENDED holds nothing: release the claims of the
    session bound to it, and retire that presence if it is still up.

    For ``fleet stop`` and ``fleet reap``, once the pane is verifiably dead or
    gone. An agent's own ``SessionEnd`` hook releases on a clean exit; a killed
    process fires none, and a ``/clear`` parks the claims on the id it ended
    for the start hook that follows (rule 2) — so a stop that landed in that
    gap, or on a row whose start hook could not adopt, left them on a dead id
    with nothing to come back for them until the lease ran out (review of
    #135, second round, finding 5). ``why`` is the board's word for it.
    """
    if agent.session_id is None:
        return Released([])
    session = store.get_session(agent.session_id)
    if session is None:
        return Released([])
    return _release_session(store, session, why=why)


# --- maintenance --------------------------------------------------------------


@dataclass(frozen=True)
class PrunedSession:
    """One ghost session retired by a prune pass."""

    id: str
    role: str
    idle_minutes: int
    released: int  # its in-flight (doing) claims that went back to the pool


@dataclass(frozen=True)
class PruneReport:
    """The outcome of a :func:`prune_sessions` pass."""

    pruned: list[PrunedSession]
    released_total: int
    threshold_minutes: int
    dry_run: bool

    @property
    def changed(self) -> bool:
        return bool(self.pruned)


def prune_sessions(
    older_than_minutes: int | None = None,
    *,
    dry_run: bool = False,
    keep: str | None = None,
    release_claims: bool = False,
    cwd: Path | None = None,
) -> PruneReport:
    """Retire ghost sessions — live rows with no heartbeat past the threshold.

    A session stays registered until its Claude Code process fires the end
    hook; a killed loop, a crashed terminal, or an MCP server that never says
    goodbye lingers on the board as ``(stale)`` indefinitely. This ends those
    rows so the board reflects who is actually present.

    **Presence and claims are retired on different clocks (#49).** The row goes
    at ``threshold``; a session's in-progress CLAIMS are only returned to the
    pool once it has been silent for ``_CLAIM_ORPHAN_AFTER`` — because for an
    agent, thirty minutes of silence is usually one long tool call, and a claim
    released under a working agent hands its lane to a second one. Passing
    ``release_claims=True`` orphans claims at the presence threshold instead,
    for callers that know the sessions are dead.

    Data-safe by construction: only session presence and orphaned CLAIMS
    change; tasks, notes, events and the project brain are untouched.
    ``dry_run`` reports what would go without ending anything. ``keep`` spares
    one session (id prefix); a still-warm session is spared automatically (it
    is not past the threshold).
    """
    _require_enabled()
    _DELIVERY.set(None)
    threshold = (
        timedelta(minutes=older_than_minutes) if older_than_minutes is not None else _STALE_AFTER
    )
    # Claims wait for the longer clock unless the caller asserts these sessions
    # are dead. `max` rather than a bare constant so an explicit --older-than
    # ABOVE the claim clock still governs: the operator saying "six hours"
    # should not have claims released at four.
    claim_threshold = threshold if release_claims else max(_CLAIM_ORPHAN_AFTER, threshold)
    now = _now()
    pruned: list[PrunedSession] = []
    emitted: list[TeamEvent] = []
    released_total = 0
    with store_session() as store:
        project = _project(store, cwd)
        spare = _resolve_session(store, keep) if keep else None
        spare_id = spare.id if spare else None
        for session in store.team_sessions(project.id):
            if session.ended_at is not None or session.id == spare_id:
                continue
            idle = now - session.last_seen_at
            if idle <= threshold:
                continue
            orphan_claims = idle > claim_threshold
            released = 0
            if not dry_run:
                released_tasks = store.end_session(session.id, release_claims=orphan_claims)
                # end_session reports what it FOUND claimed either way, so only
                # count them as released when they actually were — otherwise
                # the summary claims to have freed work that is still held.
                released = len(released_tasks) if orphan_claims else 0
                if orphan_claims:
                    for task in released_tasks:
                        emitted.append(
                            _emit(
                                store,
                                project.id,
                                "task_released",
                                f"{task.title} (session pruned)",
                                session_id=session.id,
                                task_id=task.id,
                            )
                        )
            pruned.append(
                PrunedSession(
                    id=session.id,
                    role=session.role,
                    idle_minutes=int(idle.total_seconds() // 60),
                    released=released,
                )
            )
            released_total += released
        summary = None
        if pruned and not dry_run:
            summary = _emit(
                store,
                project.id,
                "sessions_pruned",
                f"retired {len(pruned)} ghost session(s); released "
                f"{released_total} orphaned claim(s) back to the pool",
            )
            emitted.append(summary)
        board = _board_named(store, project)
    if summary is not None:
        # Every event in the batch committed in its OWN transaction, so a
        # mid-batch death can lose earlier release events while later ones
        # (and the summary) landed — confirming only the tail would report
        # "roll-call clean" over missing releases. Read back the whole batch.
        with store_session() as fresh:
            for event in emitted:
                stored = fresh.get_event(event.id)
                if stored is None or stored.project_id != board.id:
                    raise DeliveryUnconfirmedError(event.id, board.name)
        _record_delivery(summary, board)
    return PruneReport(
        pruned=pruned,
        released_total=released_total,
        threshold_minutes=int(threshold.total_seconds() // 60),
        dry_run=dry_run,
    )


# --- rendering ----------------------------------------------------------------


def minutes_text(minutes: int) -> str:
    """Whole minutes as the board writes an age: ``12m``, ``3h05m``. The one copy of the
    rule: ``asq team prune`` and ``asq remote needs`` write theirs with it, and each had a
    copy of its own to keep in step by hand."""
    return f"{minutes}m" if minutes < 60 else f"{minutes // 60}h{minutes % 60:02d}m"


def age_text(when: datetime, now: datetime) -> str:
    """How long before ``now`` ``when`` was, as the board writes it (:func:`minutes_text`)."""
    return minutes_text(max(0, int((now - when).total_seconds() // 60)))


def render_board(
    project: ProjectInfo,
    sessions: list[TeamSession],
    tasks: list[TeamTask],
    events: list[TeamEvent],
) -> str:
    """The human/board view (``asq board``), without the protocol contract."""
    from aisquare.services import claude_accounts as accounts_service  # lazy: no import cycle

    return _render_board(
        project, sessions, tasks, events, me=None, labels=accounts_service.slot_labels
    )


def _render_board(
    project: ProjectInfo,
    sessions: list[TeamSession],
    tasks: list[TeamTask],
    events: list[TeamEvent],
    *,
    me: TeamSession | None,
    assigned: Assignment | None = None,
    briefing: bool = False,
    persona_note: str | None = None,
    labels: Mapping[int, str] | Callable[[], Mapping[int, str]] | None = None,
    fleet: ProjectInfo | None = None,
    persona_root: Path | None = None,
) -> str:
    """The board, and for ``me`` the protocol and its role's cycle, whose fleet commands
    name ``fleet`` (:func:`_cycle_project`). ``briefing`` is SessionStart's alone: it adds
    ``me``'s persona after the role cycle — never on the board command, never on a
    per-prompt path, so no factual surface carries persona text (§3.1).
    ``persona_note`` is the one line saying why no attached persona was read."""
    now = _now()
    numbers = {t.id: t.number for t in tasks if t.number is not None}
    lines = ["<aisquare-team>"]
    if me is not None:
        lines.append(
            f"You are team session {short_id(me.id)} (role: {me.role}) in "
            f"project {project.root.name or project.id}."
        )
        if assigned is not None:
            lines += _assignment_lines(assigned, me)
    live = [s for s in sessions if s.ended_at is None]
    accounts = len({s.account for s in live if s.account})
    if callable(labels):
        # Read only when a label is shown, as `watch` does: every SessionStart
        # read them — a store read and a directory scan — for a board on one
        # account, which prints none (review of the #205 fold, round 1).
        labels = labels() if accounts > 1 else None
    if live:
        lines.append("sessions:")
        for session in live:
            stale = now - session.last_seen_at > _STALE_AFTER
            parts = [f"  - {short_id(session.id)} {session.role}"]
            if me is not None and session.id == me.id:
                parts.append("(you)")
            label = account_label(session.account, labels)
            # Only worth the noise once several accounts are actually in play.
            if label and accounts > 1:
                parts.append(f"[{label}]")
            if session.model:
                parts.append(f"[{session.model}]")
                # base_role: a seat rides its role's ladder, so `coder1` on a
                # model outside the coder ladder is flagged like `coder` is.
                mismatch = harness.model_mismatch(base_role(session.role), session.model)
                if mismatch:
                    parts.append("⚠ off-ladder")
            if session.persona:
                parts.append(f"persona:{session.persona}")
            if session.focus:
                parts.append(f"— focus: {session.focus}")
            parts.append(f"— {age_text(session.last_seen_at, now)} ago")
            if stale:
                parts.append("(stale)")
            lines.append(" ".join(parts))
    open_tasks = [t for t in tasks if t.status in ("todo", "doing", "review", "blocked")]
    if tasks:
        counts = ", ".join(
            f"{sum(1 for t in tasks if t.status == status)} {status}"
            for status in ("todo", "doing", "review", "blocked", "done")
            if any(t.status == status for t in tasks)
        )
        lines.append(f"tasks ({counts}):")
        statuses = {t.id: t.status for t in tasks}
        for task in open_tasks[:_BOARD_TASKS]:
            claim = f" @{short_id(task.claimed_by)}" if task.claimed_by else ""
            waiting = unmet_needs(task, statuses)
            waits = (
                " ⧗ waits on "
                + ", ".join(
                    f"#{numbers[need]}" if need in numbers else need[-8:] for need in waiting
                )
                if waiting
                else ""
            )
            lines.append(f"  - {task_label(task)} [{task.status}{claim}] {task.title}{waits}")
        if len(open_tasks) > _BOARD_TASKS:
            lines.append(f"  … {len(open_tasks) - _BOARD_TASKS} more — `aisquare task list`")
    if events:
        roles = {s.id: s.role for s in sessions}
        lines.append("recent updates:")
        lines.extend(f"  - {event_line(event, roles, numbers)}" for event in events)
    if me is not None:
        lines += [
            "Protocol: check this board before starting work; teammate updates",
            "arrive automatically on each prompt. Tasks are shared and idempotent —",
            'e.g. `aisquare task add "wire auth flow"` (safe to re-run). Claim before',
            f"working: `aisquare task claim <id> --as {short_id(me.id)}`; finish with",
            f"`aisquare task done <id> --as {short_id(me.id)}`. Share decisions/results:",
            f'`aisquare note "…" --as {short_id(me.id)}`. Full board: `aisquare board`.',
            "Every ✓ prints a receipt (seq N); `aisquare team verify <seq>` re-checks it.",
            *_role_cycle(me, fleet),
        ]
        if briefing and me.persona:
            # The session's own project, whose layer the persona was checked against
            # at spawn and launch: the cycle's (its fleet row's, else its directory's
            # when registered), else ``persona_root``, the directory's own. The
            # board's is the hub's under one (review of #201, F1 and F6).
            root = fleet.root if fleet is not None else (persona_root or project.root)
            lines += _persona_briefing(me.persona, root)
        if briefing and persona_note:
            lines.append(persona_note)
    lines.append("</aisquare-team>")
    return "\n".join(lines)


# --- the fleet row: which SESSION is which AGENT ---------------------------------------
#
# ``fleet spawn --task`` records the task on a ``fleet_agent`` row and exports
# ``AISQUARE_FLEET_AGENT`` — the row's id — onto the agent's tmux window. The
# session-start hook reads it, joins the session to its row and briefs the agent
# on the task the row was spawned for (``_assignment_lines``); ``task next`` puts
# that task first. Three rules, written once here, that the whole section keeps:
#
# 1. THE ROW BELONGS TO A PROCESS, NOT TO A SESSION ID. Claude Code mints a new
#    session id for a ``/clear`` and keeps the process; a nested ``claude -p``
#    started from the agent's shell is a new process that inherits the same
#    variable. The name alone therefore proves nothing. What does: Claude Code
#    hands every hook the pid of the process that fired it (``CLAUDE_PID``), and
#    tmux knows the pid at the root of the row's pane (``#{pane_pid}``);
#    ``launch`` execs the agent, so for the pane's own agent the two are equal
#    and for a child they never are (``orchestrator.env_claude_pid``, measured
#    across a ``/clear`` on 2.1.272). A row bound to another session id is
#    ADOPTED — rebound, its claims moved — only by the pane's process, whatever
#    ``SessionStart`` source the caller reports: ``startup``, ``resume``, ``fork``
#    and ``compact`` are all things a child can say, and a source allowlist let
#    one take its parent's row and task (review of #135, finding 4). A row that
#    cannot be checked — no ``CLAUDE_PID``, tmux not answering — stays with the
#    session it has; only an UNBOUND row binds on first arrival then, because a
#    binary that could not be started on a chosen id has nothing better.
# 2. A /CLEAR KEEPS THE CLAIMS. Claude Code fires ``SessionEnd(reason=clear)``
#    for the old id BEFORE ``SessionStart(source=clear)`` for the new one, so
#    the end hook — when the pane's process is the one clearing — retires the
#    presence and leaves the claims where they are, and the start hook moves
#    them onto the new id together with the row, in one transaction. A looper's
#    ``task next --claim`` in the gap finds the task still ``doing`` under a live
#    lease. Every other end releases, as it always did (finding 1). The hand-off
#    proves the process twice, in two hook processes; when tmux fails the second
#    proof the claims sit on the ended id, so the bind is TRIED AGAIN at every
#    prompt of a session under a fleet window that no row is bound to
#    (``_late_assignment``), and a row that ENDS — ``fleet stop``, ``fleet
#    reap`` — releases whatever its session still holds (``release_agent_claims``):
#    parked claims never outlive the row they were parked for (second round,
#    finding 5).
# 3. AN ASSIGNMENT ENDS WITH ITS TASK. Once the task is done or dropped the rows
#    spawned for it forget it (``retire_fleet_assignments``, from ``_finish_task``
#    and, for rows that predate the rule, from the briefing itself): a later
#    session start is not briefed on finished work, and a reopened task claimed
#    by someone else is not read as an order to stand down (finding 5).
# 4. THE ROW IS FOUND WHATEVER THE BOARD. Under ``AISQUARE_TEAM_HUB`` a session is
#    on the hub's board and its row in the project it was spawned into, so no
#    lookup of a row filters by the board (docs/fleet.md, "Under a hub: one
#    board, many fleet projects"). Filtered, a hub agent's ``SessionEnd`` found no
#    row, and its hand-over or ``/clear`` released the claims its next id was to
#    inherit (review of #259, round 24, 1/6). Only a task is the board's.
#
# Two doors read the row: the briefing — the session-start hook, and the
# heartbeat's first-prompt board for a session that arrived before the
# orchestrator was switched on, which binds too — and ``task next``. Both fail
# open: an unreadable row costs the assignment block or the preference, never
# the board and never the work loop.


def _named_fleet_row(store: ContextStore) -> FleetAgent | None:
    """The live fleet row ``AISQUARE_FLEET_AGENT`` names, in whichever project it is; free
    to raise.

    The variable names a row; it does not prove the reader IS that row's agent
    (rule 1). Binding on the name goes through :func:`_adopt`; ``task next``
    reads it for the order alone (:func:`_fleet_row_hint`). A GLOB on the id, so a hook
    reads it once (:func:`_hook_named_row`) and hands it to the assignment and the cycle's
    project, which each read it again (review of #259, round 23).
    """
    agent_id = orchestrator.env_fleet_agent()
    if agent_id is None:
        return None
    try:
        agent = store.get_fleet_agent(agent_id)
    except AmbiguousIdError:
        return None
    if agent is None or agent.ended_at is not None:
        return None
    return agent


def _hook_named_row(store: ContextStore) -> FleetAgent | None:
    """:func:`_named_fleet_row` for a hook, fail-open: a row that cannot be read costs the
    assignment and the cycle's project, never the board, and is logged."""
    try:
        return _named_fleet_row(store)
    except Exception as exc:
        _log.warning(
            "the fleet row AISQUARE_FLEET_AGENT names could not be read, so this session "
            "has none (%s: %s)",
            type(exc).__name__,
            exc,
        )
        return None


def _fleet_row(
    store: ContextStore,
    session_id: str,
    *,
    named: FleetAgent | None,
    allow_unbound: bool,
) -> FleetAgent | None:
    """The live fleet row for a session: the ONE lookup both doors make, free to raise.

    The row ``AISQUARE_FLEET_AGENT`` names (:func:`_named_fleet_row`), when it
    records this session; otherwise the row bound to the session, if any. In any project:
    under a hub the session's board is another (rule 4). With
    ``allow_unbound`` the named row is returned whoever it records, because that
    is how a session comes to be bound: the briefing hands it to :func:`_adopt`,
    which decides. Nothing else may take it on the name alone.

    The briefing and ``task next`` each spelled this lookup out, with different
    rules on purpose. Three review rounds of #116 running, a guard landed in one
    copy and not the other: the session-identity check, the ended-row check,
    the fail-open ``try`` (review of #116). With one lookup, the next guard has
    one place to go.

    ``named`` is what :func:`_named_fleet_row` read, whatever its project: a hook reads it
    once for the assignment and the cycle's project both.
    """
    if named is not None and (allow_unbound or named.session_id == session_id):
        return named
    return store.fleet_agent_for_session(session_id)


def _fleet_row_for(
    store: ContextStore, session_id: str, *, named: FleetAgent | EllipsisType | None = ...
) -> FleetAgent | None:
    """The live fleet row a session *is* — the row already bound to that session.

    The name in the environment is accepted only when the row it names records
    this very session; otherwise the row bound to the session, if any
    (:func:`_fleet_row`). So a session that never went through the hook —
    ``task next --as coder-1`` from the manager's shell, say — still resolves the
    same row as the pane itself, and a nested child, which inherits the variable,
    resolves nothing (its ``task next`` claimed its parent's task through the
    name alone in review round 2 of #116). Fail-open: which task comes first is
    a preference, and an unreadable row must not take ``task next`` down with it
    — but it is logged: swallowed silently, a regression here (a renamed store
    method, a migrated column) put every spawned agent back on oldest-first with
    nothing anywhere saying the preference had stopped working (review of #116,
    round 5). ``named`` is the row a hook has read already (:func:`_hook_named_row`); left
    out, it is read here.
    """
    try:
        if named is ...:
            named = _named_fleet_row(store)
        return _fleet_row(store, session_id, named=named, allow_unbound=False)
    except Exception as exc:
        _log.warning(
            "the fleet row of session %s could not be read, so it has none (%s: %s)",
            session_id,
            type(exc).__name__,
            exc,
        )
        return None


def _fleet_row_hint(
    store: ContextStore, session: TeamSession | None, *, claim: bool
) -> FleetAgent | None:
    """The row whose task ``task next`` should put first — for the ORDER, never for a claim.

    A session bound to a row is that row. Without one — no session at all (the
    plain ``task next --status review`` of a cycle typed by hand), or a session
    no row is bound to (the MCP server's virtual ``mcp:`` session, whose process
    is the agent's own child) — the window's variable still says which row this
    process works under, and for the order that is enough: the worst a misread
    costs is a nested child being shown its parent's task first, which it may
    not claim anyway. Two testers spawned for two review tasks were both handed
    the older one, because the cycle passed no session and nothing else was
    consulted (review of #135, finding 11). A CLAIM is not taken on the name
    alone: that is identity, and the variable is inherited, so it keeps needing
    a session the row is bound to. The row is found in any project (rule 4): the
    board is asked for the task, by ``store.next_task``, which prefers it among the
    board's own.
    """
    if session is not None:
        row = _fleet_row_for(store, session.id)
        if row is not None:
            return row
    if claim:
        return None
    try:
        return _named_fleet_row(store)
    except Exception as exc:  # the order is a preference: logged, as in _fleet_row_for
        _log.warning(
            "the fleet row AISQUARE_FLEET_AGENT names could not be read, so task next "
            "hands out the oldest task first (%s: %s)",
            type(exc).__name__,
            exc,
        )
        return None


def _pane_holds(agent: FleetAgent) -> bool | None:
    """Whether the process running this hook is the one in ``agent``'s pane (rule 1).

    ``None`` when it cannot be told: no ``CLAUDE_PID`` in the environment (a
    binary that does not export one), a tmux that does not answer, or a pane
    that is already gone. Callers treat that as "not proven" — a bound row is
    not adopted on a guess, because the guess that was wrong here handed a
    parent's task to its child. The fleet's own server factory is used so the
    row's socket is asked (``[fleet] tmux_socket`` may have changed since the
    spawn), at the path its spawn recorded (another ``TMUX_TMPDIR`` reaches
    another server on the name, whose ``%N`` is another agent), and so a test's
    fake tmux is seen; imported here because ``services.fleet`` imports this
    module the same lazy way.
    """
    pid = orchestrator.env_claude_pid()
    if pid is None:
        return None
    from aisquare.services import fleet as fleet_service

    try:
        pane_pid = fleet_service.server_of(agent).pane_pid(agent.pane_id)
    except TmuxError:
        return None
    if pane_pid is None:
        return None
    return pane_pid == pid


def _adopt(store: ContextStore, agent: FleetAgent, session_id: str) -> bool:
    """Bind ``agent``'s row to ``session_id``; ``False`` when this session may not have it.

    The pane's own process always may (a ``/clear``, a compaction, a resume —
    whatever the source says); any other process never may; and when it cannot
    be told, only a row nobody is bound to yet is taken, on first arrival. The
    row and the claims of the session it leaves move in one store transaction
    (``adopt_fleet_agent_session``), which also refuses a row that ended, or was
    bound by somebody else, between the read and the write — ``fleet stop`` and
    this hook are different processes.
    """
    holds = _pane_holds(agent)
    if holds is False or (holds is None and agent.session_id is not None):
        return False
    lease = _now() + timedelta(minutes=orchestrator.lease_minutes())
    return store.adopt_fleet_agent_session(agent.id, agent.session_id, session_id, lease)


def _handing_over_a_fleet_row(store: ContextStore, session: TeamSession) -> bool:
    """Whether ``session`` is a live fleet row's — the premise of parking its claims for the
    replacement. Fail-open towards RELEASING, like :func:`_clearing_its_own_pane`. The row
    in any project (rule 4): asked by the session's board, a hub agent had none."""
    try:
        return _fleet_row_for(store, session.id) is not None
    except Exception:
        return False


def _clearing_its_own_pane(store: ContextStore, session: TeamSession) -> bool:
    """Whether ``session`` is a live fleet row's, ended by the process in that row's pane.

    The premise of rule 2: only then will the id that follows be able to adopt
    the row and the claims kept for it. A nested child clearing its own session
    is not (its pid is not the pane's), nor is a binary without ``CLAUDE_PID``,
    and both release as before. Fail-open towards RELEASING: an unreadable row
    must not leave claims parked on an id nothing will come back for. The row in any
    project, as for a hand-over (rule 4).
    """
    try:
        row = _fleet_row_for(store, session.id)
        return row is not None and _pane_holds(row) is True
    except Exception:
        return False


class Assignment(NamedTuple):
    """The task a session was spawned for, with what the briefing has to decide on.

    ``mine`` — this session holds the claim — is the answer to "is this MY work
    in flight?", which the task alone cannot give across a ``/clear`` until the
    claim has been moved (rule 2). ``waiting_on`` is the unmet needs of a
    ``todo`` task; ``holder_gone`` says a ``doing`` task's holder is somebody
    else whose lease has run out. Both were missing, and the briefing told an
    agent to claim a task ``task next`` would have refused, and to stand down
    from one ``task claim`` would have handed it (review of #135, finding 12).
    """

    task: TeamTask
    mine: bool
    waiting_on: list[str]
    holder_gone: bool


def _assignment(
    store: ContextStore, session_id: str, project_id: str, *, named: FleetAgent | None
) -> Assignment | None:
    """The task this session was spawned for, joining the session to its fleet row.

    ``fleet spawn --task`` recorded the task on the ``fleet_agent`` row and named
    the label and branch after it, and set ``AISQUARE_FLEET_AGENT`` on the window
    — and that was where it stopped: the session inside received the generic
    board and its role's standing cycle, whose ``task next`` hands out the
    OLDEST ready task. A coder spawned for task B took task A; two coders
    spawned together raced for the same one; the manager ended up posting
    "you are coder-x, run task show …" notes by hand (observed 2026-09-10).

    Fail-open throughout: an unreadable row, a tmux that will not answer, a
    task that will not parse — each costs the assignment block, never the board.
    ``named`` is the row the window's variable names (:func:`_hook_named_row`).
    """
    try:
        return _resolve_assignment(store, session_id, project_id, named)
    except Exception:
        return None


def _late_assignment(store: ContextStore, session: TeamSession) -> Assignment | None:
    """The bind a session's start hook could not make, tried again at a prompt.

    Only for a session under a fleet window (``AISQUARE_FLEET_AGENT`` set) that
    no row is yet bound to — a bound session costs two indexed reads here and
    is never re-briefed, and a session outside the fleet has no row to try
    for. A ``/clear``'s hand-off needs the pane's process proven twice, at the
    end hook and at the start hook that follows, in two processes; tmux not
    answering the second time left the row bound to the ended id and its
    claims parked there, with nothing coming back for either until the lease
    ran out (review of #135, second round, finding 5). The prompt is the next
    thing an agent does after a clear, so the bind lands a prompt late at
    worst, and the ASSIGNED TO YOU block it was owed comes with it. A child
    that inherited the variable pays one ``display-message`` per prompt to be
    refused, exactly as it is at its start.

    The named row is read once and handed to both lookups: read again for the
    assignment, every prompt of every fleet agent paid for two GLOB lookups on its id
    (review of #259, round 24, 6/6). With no live row named, nothing can be bound here:
    a row of the session's own is bound already, and no other is taken.
    """
    if orchestrator.env_fleet_agent() is None:
        return None
    named = _hook_named_row(store)
    if named is None or _fleet_row_for(store, session.id, named=named) is not None:
        return None
    return _assignment(store, session.id, session.project_id, named=named)


def _resolve_assignment(
    store: ContextStore, session_id: str, project_id: str, named: FleetAgent | None
) -> Assignment | None:
    """The body of :func:`_assignment`, free to raise; the rules are the section's.

    The row is found, and adopted, in any project; only its task is asked of the board,
    ``project_id`` (rule 4). With the row filtered by the board, a hub agent's ``/clear`` was
    never adopted: the row stayed bound to the ended id, and every lookup by the new one
    missed it, the usage-limit hand-over's and the board chip's included (review of #259,
    round 24, 5/6).
    """
    agent = _fleet_row(store, session_id, named=named, allow_unbound=True)
    if agent is None:
        return None
    if agent.session_id != session_id and not _adopt(store, agent, session_id):
        # The name in the environment is a row this session may not have
        # (rule 1) — but the session may be bound to a row of its OWN, and
        # `task next` (`_fleet_row_for`) reads that row for the order. The
        # two doors read the same row now; this one used to give up here and
        # the agent lost its ASSIGNED TO YOU block while `task next` still put
        # its task first (round 8 of #203). The same lookup, bound rows only.
        agent = _fleet_row(store, session_id, named=named, allow_unbound=False)
        if agent is None:
            return None
    if agent.task_id is None:
        return None
    task = store.get_task(agent.task_id)
    if task is None:
        return None
    if task.status in CLOSED_STATUSES:
        # Rule 3, for a row that was spawned before the rule existed or whose
        # task was closed by a path that did not know the row.
        store.retire_fleet_assignments(task.id)
        return None
    if task.project_id != project_id:
        return None  # another board's task: this session would be told of work it cannot see
    # ``claimed_by is None`` is NOT this session's: an untouched ``todo`` task
    # read as work in flight told the agent to carry on, and it never claimed
    # it — the pool handed it to somebody else (review of #116, round 3).
    mine = task.claimed_by is not None and task.claimed_by == session_id
    waiting_on = unmet_needs(task, store.task_statuses(project_id)) if task.status == "todo" else []
    expired = task.claim_expires_at is not None and task.claim_expires_at < _now()
    return Assignment(task, mine, waiting_on, task.status == "doing" and not mine and expired)


#: Roles whose job is to VERIFY a task in review rather than to work it: exactly
#: the roles whose standing cycle pulls from the review pool, pinned both ways
#: against the harness by ``test_every_verifying_role_is_known_to_the_assignment``.
#: ``ui-tester`` was missing, so the browser verifier spawned for a ``[review]``
#: task was told it was there for the rework — to edit and re-submit someone
#: else's work, against its own lane rule (review of the fourth version).
_VERIFYING_ROLES = frozenset({"tester", "runner", "reviewer", "ui-tester"})

#: Roles that GATE the assembled deliverable once, before handoff — the roles
#: whose standing cycle's verdict is a ``GATE:`` note, pinned by the same test.
#: They neither work a task nor verify one, so no state of the task they were
#: spawned for gives them anything to claim or a per-task verdict to render.
#: The validator sat in the verifying set, and a ``[review]`` assignment sent it
#: to "the verdict your standing cycle below describes" — a cycle that describes
#: no verdict on a task; outside both sets it would be handed a coder's claim or
#: rework (review of #116, round 5).
_GATING_ROLES = frozenset({"validator"})


def _assignment_lines(assignment: Assignment, me: TeamSession) -> list[str]:
    """What the agent is told about the task it was spawned for — by state and role.

    Written per state, not per the coder's cycle: the manager spawns a tester
    once a task reaches review, and that tester's assignment is a ``[review]``
    task it must verify — and NOTHING else is its to touch: a ``[todo]`` task,
    the one it reopened included, is a coder's to claim, and a verifier told to
    claim it raced the coder for the rework (finding 12). A coder spawned at
    ``[review]`` is there for the rework, and was being handed the stop order
    meant for a bystander (reviews of the first two versions of #116). The
    validator is told one thing in every state: the task is the context for its
    GATE, never work or a verdict of its own (round 5).

    ``mine`` — this session IS the one holding the task, across a ``/clear``
    that renamed it — is read INSIDE each state, not ahead of them. Read ahead,
    it answered for states it had no answer for: an agent that put its own task
    up for review and then cleared was told to "carry on … `task review` when it
    is finished", for work already sitting with a verifier.

    Every instruction is one the commands would honour: "claim it" only when
    ``task next`` would hand it out (its needs done) and ``task claim`` would
    accept it (unclaimed, or held on a lease that has run out); the stop order
    only for the one case that earns it — a teammate live on the task right now.
    """
    task = assignment.task
    mine = assignment.mine
    sid = short_id(me.id)
    verifier = base_role(me.role) in _VERIFYING_ROLES
    head = f"ASSIGNED TO YOU: {task.id} [{task.status}] {task.title}"
    if base_role(me.role) in _GATING_ROLES:
        return [
            head,
            "You gate the assembled deliverable, not this task: it is not yours to claim,",
            f"work or verify on its own. `aisquare task show {task.id}` is the context you",
            "were spawned with; your verdict is the GATE note your standing cycle describes.",
        ]
    if verifier and task.status != "review":
        return [
            head,
            f"It is {task.status}, not in review — nothing for you to verify yet, and not",
            "yours to claim. Take review work with your standing cycle; `task next` hands",
            "you this one first the moment it is back in review.",
        ]
    if task.status == "todo":
        if assignment.waiting_on:
            waits = ", ".join(need[-8:] for need in assignment.waiting_on)
            return [
                head,
                f"It waits on {waits} — do not claim it yet; `task next` would not hand it",
                "out either. Take pool work with your standing cycle; it comes to you first",
                "the moment what it needs is done.",
            ]
        return [
            head,
            f"Claim it FIRST — `aisquare task claim {task.id} --as {sid}` — then read its",
            f"contract with `aisquare task show {task.id}` and work it to review/done.",
            "Only when it is finished does your standing cycle's `task next` apply.",
        ]
    if task.status == "doing" and mine:
        return [
            head,
            "You are the one working it — a clear or resume does not hand it back.",
            f"Carry on: `aisquare task show {task.id}`, then `aisquare task review "
            f"{task.id} --as {sid}` / `task done` when it is finished.",
        ]
    if task.status == "doing" and assignment.holder_gone:
        holder = short_id(task.claimed_by) if task.claimed_by else "its holder"
        return [
            head,
            f"{holder} went silent and the lease on it has run out, so it is yours to take:",
            f"`aisquare task claim {task.id} --as {sid}`, then `aisquare task show {task.id}`",
            "and work it to review/done.",
        ]
    if task.status == "review":
        if verifier:
            # No command for the verdict here: the roles differ on it (a
            # tester's `task done`/`task reopen`, a reviewer's PR review), and
            # this block used to name `task next --status review` for all of
            # them — which the validator's cycle, a one-shot GATE note, never
            # ran (review of the fourth version; the validator has its own
            # block now). `task show` is common to every verifier; the standing
            # cycle below carries the verdict.
            return [
                head,
                "It awaits your verification — start there, not with the pool:",
                f"`aisquare task show {task.id}` for its acceptance criteria, then the",
                "verdict your standing cycle below describes.",
            ]
        if mine:
            return [
                head,
                "You put it up for review; it is a verifier's now, not yours to redo.",
                "Take pool work with your standing cycle — if it comes back reopened,",
                "it comes back to you.",
            ]
        return [
            head,
            "It is in review and you were spawned for the rework: `aisquare task show",
            f"{task.id}` carries the verdict. Address what it names, then `aisquare task",
            f"review {task.id} --as {sid}` again — do not take pool work first.",
        ]
    if task.status == "blocked":
        return [
            head,
            f"It is blocked — `aisquare task show {task.id}` names why. Clear that and",
            f"claim it (`aisquare task claim {task.id} --as {sid}`); if you cannot, say so:",
            f'`aisquare note "…" --kind question --to manager --as {sid}`.',
        ]
    # ``doing``, somebody else's, on a live lease: the one case that earns a stop.
    holder = f" by {short_id(task.claimed_by)}" if task.claimed_by else ""
    return [
        head,
        f"It is already {task.status}{holder}. Do not take another task on your own:",
        f'ask the manager — `aisquare note "…" --kind question --to manager --as {sid}`.',
    ]


def kickoff_prompt(
    task: TeamTask,
    *,
    role: str,
    label: str,
    session_id: str | None,
    statuses: Mapping[str, str],
) -> str:
    """The first message of an agent spawned for ``task`` (P0-W1a, #122): ``You are
    <label>.``, then the lines its ASSIGNED TO YOU block carries (:func:`_assignment_lines`),
    worked out as the block works them out at its session start (:func:`_resolve_assignment`),
    so the kickoff and the block never disagree. A context block starts no turn; this does.
    ``statuses`` are the board's task statuses, for what a ``todo`` task still waits on."""
    now = _now()
    me = TeamSession(
        id=session_id or label,
        project_id=task.project_id,
        role=role,
        started_at=now,
        last_seen_at=now,
    )
    mine = task.claimed_by is not None and task.claimed_by == session_id
    waiting_on = unmet_needs(task, statuses) if task.status == "todo" else []
    expired = task.claim_expires_at is not None and task.claim_expires_at < now
    assignment = Assignment(task, mine, waiting_on, task.status == "doing" and not mine and expired)
    return "\n".join([f"You are {label}.", *_assignment_lines(assignment, me)])


def _role_cycle(me: TeamSession, project: ProjectInfo | None) -> list[str]:
    """The standing work cycle for a role — injected so nobody has to paste it.

    The cycles themselves live in :mod:`aisquare.core.harness` (one source of
    truth for the whole harness: profiles, ladders, and briefings).

    Keyed on :func:`base_role`, so a numbered seat (``coder1``) receives its
    role's cycle. Without that a seat got an empty briefing — the one thing the
    seat's own comment in ``cli/launch.py`` promises it does not lose.

    Its fleet commands name ``project``, the session's fleet project
    (:func:`_cycle_project`), through ``project_service.fleet_command``. With none, they
    name a ``<project>`` placeholder, and a line above the cycle says to fill it in. Never
    bare: alone, a fleet command acts on the active project, the machine-wide pin before
    the working directory (round 21 of the review of #259), and a bare one came back for
    every hand-launched session under a hub (round 23). A manager's pause is its project's on
    the session's board (:func:`harness.pause_signal`). A session whose project could not be
    told gets a placeholder for that too, and a line says how to fill it in. Told the bare
    name, under a hub the hub project's own pause, a manager kept spawning in the project the
    human then named while ``fleet pause`` paused that one (review of #259, round 27, 6/7).
    """
    if project is not None:
        return harness.role_cycle(
            base_role(me.role),
            short_id(me.id),
            fleet=lambda verb: project_service.fleet_command(verb, project),
            paused=harness.pause_signal(project.id, me.project_id),
        )
    cycle = harness.role_cycle(
        base_role(me.role),
        short_id(me.id),
        fleet=lambda verb: (
            install_route.command_line(["aisquare", "fleet", verb, "--project"])
            + f" {_NO_CYCLE_PROJECT}"
        ),
        paused=_NO_CYCLE_PAUSE,
    )
    if not any(_NO_CYCLE_PROJECT in line for line in cycle):
        return cycle
    told = [
        f"The fleet commands below name {_NO_CYCLE_PROJECT}: this session's own project "
        "could not be told, so put in the codename of the one they are for (`aisquare "
        "project list` names them)."
    ]
    if any(_NO_CYCLE_PAUSE in line for line in cycle):
        told.append(
            f"Its pause is `{_NO_CYCLE_PAUSE}`: that project's ID, not its codename, in lower "
            "case with `_` as `-` (`prj_ab12` → "
            f"`{harness.pause_signal('prj_ab12', me.project_id)}`); `{harness.PAUSE_SIGNAL}` "
            f"alone when it is this board's own project ({me.project_id})."
        )
    return [*told, *cycle]


_NO_CYCLE_PROJECT = "<project>"
"""What a cycle's fleet commands name when :func:`_cycle_project` has no project."""
_NO_CYCLE_PAUSE = f"{harness.PAUSE_SIGNAL}.<project id>"
"""The pause a cycle names when :func:`_cycle_project` has no project: that project's on a
board it does not own (:func:`harness.pause_signal`)."""


def _cycle_project(
    store: ContextStore,
    board: ProjectInfo,
    cwd: Path | None,
    session_id: str,
    *,
    named: FleetAgent | None,
) -> ProjectInfo | None:
    """The project a session's cycle names in its fleet commands: its fleet row's.

    Not the board's. Under ``AISQUARE_TEAM_HUB`` the board is the hub's, and ``fleet
    spawn`` sets the hub on every window it starts, while each row is in its own project.
    Named by the board, a manager's cycle spawned its coders in the hub's directory, and
    its ``stop``, ``tell`` and ``switch`` found no live agent there (review of #259, round
    22). The row is the one ``AISQUARE_FLEET_AGENT`` names, whatever project it is in: a
    child that inherited the variable runs in the same project. The hook read it already,
    for the assignment, and hands it over as ``named``: read here too, a session start paid
    for the same GLOB lookup twice (round 23). Without one, it is the row the store records
    against the session (``fleet_agent_for_session``), the lookup the hand-over and the
    board's chip make. Its project is read stored, codename and all.

    A session with no live row is named by the board when no hub is set: the board is then
    its directory's project. Read stored too: the board ``orchestrator.team_project`` builds
    carries no codename, so every cycle named its project by its raw id, unlike ``fleet
    ls``, the sidebar and every receipt (round 22). Under a hub it is the project of its
    ``cwd``, the hub aside (``orchestrator.directory_project``), when that is registered:
    left bare, the commands of a manager the human launched in ``/work/app`` acted on
    whichever project another shell had pinned, and spawned its coders there (round 23).
    A directory with no registration has ``None``. Fail-open, like the assignment: a read
    that fails costs the project, never the board, and is logged. Every read, the stored
    board's too: outside the guard, a ``get_project`` that raised took the session's whole
    ``<aisquare-team>`` injection with it (round 23). Without a hub the board, as it is, is
    still this session's project; under one, no project is.
    """
    hub = orchestrator.team_hub() is not None
    try:
        agent = named if named is not None else store.fleet_agent_for_session(session_id)
        if agent is not None:
            return store.get_project(agent.project_id)
        if hub:
            return store.get_project(orchestrator.directory_project(cwd).id)
        return store.get_project(board.id) or board
    except Exception as exc:
        _log.warning(
            "the project of session %s's cycle could not be read, so it names %s (%s: %s)",
            short_id(session_id),
            "no project" if hub else "the board's",
            type(exc).__name__,
            exc,
        )
        return None if hub else board


def _captain_briefing(project: ProjectInfo, me: TeamSession, persona_note: str | None) -> str:
    """The captain's session-start block: who it is, its only hands, its cycle, its persona."""
    lines = [
        "<aisquare-team>",
        f"You are the captain (team session {short_id(me.id)}): the owner's agent across every "
        "project, working from the home board.",
        "Your only hands are the captain tools (mcp__captain__*) — no shell and no files; "
        "what you cannot do through them, you ask the owner or a project's manager to do.",
        *_role_cycle(me, project),
    ]
    if me.persona:
        lines += _persona_briefing(me.persona, project.root)
    if persona_note:
        lines.append(persona_note)
    lines.append("</aisquare-team>")
    return "\n".join(lines)


def _persona_briefing(name: str, root: Path, *, without: str = "launched without it") -> list[str]:
    """The persona block for a session start (docs/plans/spawn-personas.md §3.2).

    NEVER raises: a raise here loses the whole team block, role cycle and lane
    rule included (``harness._lane_rule``'s docstring). A persona removed or
    broken since launch — or anything else going wrong — is one line saying so,
    and the session runs without it. ``without`` ends that line for the caller
    that is not a launch (:func:`_owed_persona_briefing`). Imported here, not at
    module level, so a session with no persona pays nothing for the persona code.
    """
    try:
        from aisquare.core import personas

        return personas.briefing(personas.resolve(name, root))
    except Exception as exc:
        reason = getattr(exc, "rule", None) or f"{type(exc).__name__}: {exc}"
        return [f'persona "{name}": {reason} — {without}']


def _directory_root(cwd: Path | None) -> Path | None:
    """The root of ``cwd``'s own project, the hub aside (``orchestrator.directory_project``):
    the persona layer ``launch --persona`` checked (``cli.launch._check_persona``). For a
    session with no fleet row in a directory nobody added, the briefing had only the board's
    root, the hub's under one, and said a persona the launch had found was missing (review
    of #201, F6). ``None`` when the directory cannot be read (a symlink loop)."""
    try:
        return orchestrator.directory_project(cwd).root
    except OSError:
        return None


def _asked_persona(
    store: ContextStore, project_id: str
) -> tuple[str | None, str | None, FleetAgent | None]:
    """The persona a session start asks for (docs/plans/spawn-personas.md §4.7).

    The persona on the ``fleet_agent`` row ``AISQUARE_FLEET_AGENT`` names, when
    that row carries one: it is the latest recorded intent. ``fleet spawn`` writes
    its ``--persona`` there and ``persona attach`` replaces it, while
    ``AISQUARE_PERSONA`` only holds what the process was launched with. So an
    attachment survives a ``/clear`` or a restart even for an agent spawned with
    ``--persona``. Otherwise ``AISQUARE_PERSONA``, which is all a hand-typed
    launch with no fleet row has. ``None`` asks for nothing, and the session row
    keeps whatever it recorded before.

    Returns ``(persona, note, named)``: ``named`` is the row the window names, in whichever
    project (:func:`_named_fleet_row`), read here once for the persona, the assignment and
    the cycle's project alike (review of #259, round 23), ``None`` when it could not be read
    (as :func:`_hook_named_row` gives it). Only a row of THIS project gives its persona.
    Fail-open, but never silently: a row that cannot be
    read costs only the row, never the team block. It is logged with its traceback,
    and ``note`` is the one briefing line that tells the agent it started without
    an attached persona. The note names the exception class, never a value.
    """
    try:
        named = _named_fleet_row(store)
    except Exception as exc:
        _log.warning(
            "session start: fleet row %s for project %s could not be read; "
            "falling back to AISQUARE_PERSONA",
            orchestrator.env_fleet_agent(),
            project_id,
            exc_info=True,
        )
        note = (
            f"persona: the fleet row could not be read ({type(exc).__name__}) "
            "— started without an attached persona"
        )
        return orchestrator.env_persona(), note, None
    # The row is this board's when its project is, or when its spawn recorded this board
    # (``fleet_agent.board``): under ``AISQUARE_TEAM_HUB`` every seat is in its own
    # project and on the hub's board, and comparing the project alone dropped every
    # attached persona at the next start (review of #201, F2).
    if named is not None and project_id in (named.project_id, named.board) and named.persona:
        return named.persona, None, named
    return orchestrator.env_persona(), None, named


# --- a persona attached to a BUSY agent: owed, then handed over by its own prompt --------
#
# ``fleet.attach_persona`` types its briefing into an agent that is waiting. For one it
# cannot type into, it may put nothing of the persona on the board: a note carried the
# fenced body into every other session's delta, ``aisquare board`` and the next
# teammate's briefing (review of #240, finding 7). So the briefing is OWED to that one
# agent: a ``team_meta`` marker under its fleet row's id, which the agent's own
# ``UserPromptSubmit`` hook turns into the preface and the block the typed path pastes,
# once. A session start briefs the row's persona itself, and settles the marker. No
# table and no column: the marker is a key like the nudge debounce and the continuation
# counters, and ``team_meta`` has no delete, so settled is an empty value.

_OWED = "owed"
"""A marker's value while the briefing is owed, and ``owed:<persona>`` when the
attachment replaced that persona."""

_ATTACHED_WITHOUT = "attached to you without its briefing"
"""How :func:`_persona_briefing`'s one line ends when an owed persona cannot be loaded."""


def persona_attached_preface(name: str, replaced: str | None) -> str:
    """The line an agent reads above an attached persona's block, on either path: typed
    into a waiting agent (``fleet.attach_persona``), or handed to a busy one by its own
    next prompt (:func:`_owed_persona_briefing`)."""
    tail = f"; it replaces {replaced}" if replaced else ""
    return f"aisquare: the operator attached persona {name} to you — it applies from now on{tail}"


def _persona_owed_key(agent_id: str) -> str:
    return f"persona_owed:{agent_id}"


def owe_persona_briefing(store: ContextStore, agent_id: str, *, replaced: str | None) -> None:
    """Record that the agent of fleet row ``agent_id`` has yet to be given its persona's
    briefing, because it could not be typed. ``replaced`` is the persona the attachment
    replaced, for the preface. The persona itself is not recorded: the row holds it, and
    what is handed over is the row's persona at that moment."""
    store.set_meta(_persona_owed_key(agent_id), f"{_OWED}:{replaced}" if replaced else _OWED)


def settle_persona_briefing(
    store: ContextStore, agent_id: str, *, replaced: str | None
) -> str | None:
    """Forget what fleet row ``agent_id`` is owed, if anything: it is about to be typed
    the briefing, or owed it afresh.

    Returns the persona the next preface says is replaced, as the AGENT knows it:
    ``replaced`` (the row's) when nothing was owed, else the one the owed briefing would
    have replaced, since that briefing never reached the agent. Skeptic attached over
    mentor while the agent was busy, then careful: the agent still runs as mentor and
    has never been told of skeptic.
    """
    key = _persona_owed_key(agent_id)
    value = store.get_meta(key)
    if not value:
        return replaced
    store.set_meta(key, "")
    return value.partition(":")[2] or None


class _OwedPersona(NamedTuple):
    """A standing marker, and the row it stands under: the session's own."""

    key: str
    row: FleetAgent
    replaced: str | None


def _owed_persona(store: ContextStore, session: TeamSession) -> _OwedPersona | None:
    """What ``session``'s OWN fleet row is owed, or ``None``; free to raise.

    Outside a fleet window nothing is read at all. Inside one, the window's variable is
    the row's id, so a row that is owed nothing costs one indexed read of ``team_meta``
    and never the row. Only a standing marker pays for the row, and then the variable
    alone is not enough: the agent's child processes inherit it (rule 1 of the fleet-row
    section above), so the marker is honoured only for the session the row is bound to.
    A nested ``claude -p`` is handed nothing, and uses nothing up.
    """
    agent_id = orchestrator.env_fleet_agent()
    if agent_id is None:
        return None
    key = _persona_owed_key(agent_id)
    value = store.get_meta(key)
    if not value:
        return None
    row = _fleet_row(store, session.id, named=None, allow_unbound=False)
    if row is None or row.id != agent_id:
        return None
    return _OwedPersona(key, row, value.partition(":")[2] or None)


def _owed_persona_briefing(store: ContextStore, session: TeamSession) -> str:
    """The briefing ``session``'s own fleet row is owed, handed over ONCE; else ``""``.

    The preface the typed path uses, then the block of the row's CURRENT persona: two
    attaches while the agent was busy deliver the later one. A persona that no longer
    loads is the one line saying so, without the preface. The marker is settled before
    the text is returned, and the text is returned only if that write held, so a store
    that refuses it costs this prompt the briefing instead of repeating it on every one.

    NEVER raises, and says what it swallowed (a hook's stderr is its log): this runs on
    every prompt of every fleet agent, and a failure here may cost the briefing, never
    the delta, the collision banner or the late assignment it is returned with.
    """
    try:
        owed = _owed_persona(store, session)
        if owed is None:
            return ""
        lines: list[str] = []
        name = owed.row.persona
        if name:
            # The row's own project, as attach resolved it; the session's is the board's,
            # the hub's under one (review of #201, F1).
            root = _project_root(store, owed.row.project_id) or owed.row.cwd
            lines = _persona_briefing(name, root, without=_ATTACHED_WITHOUT)
            if len(lines) > 1:  # the block; a single line is why it could not be loaded
                replaced = owed.replaced if owed.replaced != name else None
                lines = [persona_attached_preface(name, replaced), *lines]
        store.set_meta(owed.key, "")
        return "\n".join(lines) + "\n" if lines else ""
    except Exception as exc:
        _log.warning(
            "the persona briefing owed to fleet row %s could not be handed to session %s, "
            "so this prompt goes without it (%s: %s)",
            orchestrator.env_fleet_agent(),
            session.id,
            type(exc).__name__,
            exc,
        )
        return ""


def _settle_owed_persona(store: ContextStore, session: TeamSession) -> None:
    """A session start briefs its fleet row's persona itself: nothing is owed after it.

    Called once the start has bound the session to its row, because only the row's own
    session settles (:func:`_owed_persona`). Never raises: an unsettled marker costs a
    second copy of the block at the next prompt, and a raise here would cost the board.
    """
    try:
        owed = _owed_persona(store, session)
        if owed is not None:
            store.set_meta(owed.key, "")
    except Exception as exc:
        _log.warning(
            "the persona briefing owed to fleet row %s could not be settled at the start of "
            "session %s, so its next prompt may carry the block again (%s: %s)",
            orchestrator.env_fleet_agent(),
            session.id,
            type(exc).__name__,
            exc,
        )


def event_line(
    event: TeamEvent, roles: dict[str, str], numbers: Mapping[str, int] | None = None
) -> str:
    """``583dc9a8 (manager) added → coder: [#12 tsk_…] title``; ``numbers`` is the board's
    task id → number (``store.task_numbers``), so the task reads as the board shows it."""
    who = (
        f"{short_id(event.session_id)} ({roles.get(event.session_id, '?')})"
        if event.session_id
        else "cli"
    )
    target = f" → {event.to_role}" if event.to_role else ""
    number = (numbers or {}).get(event.task_id) if event.task_id else None
    task = ""
    if event.task_id:
        task = f" [#{number} {event.task_id}]" if number is not None else f" [{event.task_id}]"
    kind = event.kind.replace("task_", "")
    return f"{who} {kind}{target}:{task} {event.text}"


def _render_delta(
    events: list[TeamEvent],
    roles: dict[str, str],
    *,
    truncated: bool,
    board: str,
    numbers: Mapping[str, int] | None = None,
) -> str:
    """The per-prompt delta. Each line names its board (fix 5 of the fleet-board card,
    tsk_01m3k89bkhpj): two fleets' updates once mixed in one delta, because the seats of
    both registered on a third board, and no line said which board it came from."""
    count = f"{len(events)}{'+' if truncated else ''}"
    lines = [
        "<aisquare-team-delta>",
        f"{count} teammate update(s) since your last prompt:",
        *(f"- [{board}] {event_line(event, roles, numbers)}" for event in events),
    ]
    if truncated:
        lines.append("… more waiting — run `aisquare board` for the full picture.")
    lines.append("</aisquare-team-delta>")
    return "\n".join(lines)


#: What one Stop reason may cost in the hook's OUTPUT — the thing Claude Code caps
#: at 10,000 characters. Set below the cap because the reason travels inside
#: ``{"decision":"block","reason":…}``: a reason that goes over does not lose its
#: tail, it loses the closing quote and brace, and then Claude Code parses nothing
#: at all and the wake-up is silently gone.
_WAKEUP_REASON_BUDGET = 8_000

#: The most one rendered event line may cost. Board text is unbounded and the
#: coder's own cycle asks for ``--note "how to verify + evidence"``, so one 2 KB
#: note must not crowd out the other nine events — every event the reason shows
#: has to stay legible, because ``_manager_wakeup`` advances the cursor past all
#: of them and nothing will show them again.
_WAKEUP_LINE_BUDGET = 400


def _output_cost(text: str) -> int:
    """What ``text`` costs in the hook's JSON output, escaping included.

    Measured rather than assumed: ``cli/hook.py`` prints the decision through
    ``json.dumps``, whose default escaping turns every non-ASCII character into
    ``\\uXXXX`` (six characters) and every quote or newline into two. A budget
    counted in raw characters therefore bounds nothing at all for a note written
    in a non-Latin script — 1,700 such characters already exceed the cap on their
    own. The ``- 2`` drops the quotes ``json.dumps`` puts around a string.
    """
    return len(json.dumps(text)) - 2


def _clip(text: str, budget: int) -> str:
    """``text`` costing at most ``budget`` characters of hook output.

    An ellipsis rather than a silent truncation: the manager is being asked to
    act on this line, so "there was more here" is part of what it needs.

    Halving rather than one proportional guess, because the relationship between
    characters and output cost depends on the characters: ASCII text needs one
    pass, and the loop terminates because ``keep`` strictly decreases (at most
    ~log2(budget) passes, each encoding at most ``budget`` characters).
    """
    if _output_cost(text) <= budget:
        return text
    keep = budget
    while keep > 1 and _output_cost(_shorten(text, keep)) > budget:
        keep //= 2
    return _shorten(text, keep)


def _shorten(text: str, keep: int) -> str:
    """``text`` cut to ``keep`` characters with the cut marked, or unchanged."""
    if len(text) <= keep:
        return text
    return text[: max(keep - 1, 0)].rstrip() + "…"


#: What the mark a clip leaves behind costs on its own — the floor for a line's
#: share, because a line clipped to less than this still costs this.
_ELLIPSIS_COST = _output_cost("…")


def _render_wakeup(
    events: list[TeamEvent],
    roles: dict[str, str],
    *,
    truncated: bool,
    board: str,
    numbers: Mapping[str, int] | None = None,
) -> str:
    """The Stop reason: the delta in the same lines the prompt hook would have used,
    framed as the instruction Claude Code continues the turn with.

    Bounded in OUTPUT COST, not only in events (:class:`StopDecision` says what
    going over costs). Each event line gets a share derived from the remaining
    budget and the number of events, so raising ``_DELTA_LIMIT`` clips lines
    harder instead of overflowing, and no event the cursor consumed is ever
    dropped from the reason — ``_manager_wakeup`` advances past every event it
    shows, so a dropped line is a lost board update.

    The sum is under budget by arithmetic: the head and frame are paid for first,
    and each of the ``n`` lines costs at most ``share`` plus the four characters
    of ``"- "`` and its newline. That holds while the share stays above
    :data:`_ELLIPSIS_COST` — a clipped line cannot cost less than the mark it
    ends with — which is every event count up to roughly 700. ``_DELTA_LIMIT`` is
    10; the test beside this renders at both counts, so the margin is measured
    rather than assumed.
    """
    count = f"{len(events)}{'+' if truncated else ''}"
    head = f"{count} board update(s) from your fleet arrived while you were finishing this turn:"
    frame = [
        *(["… more waiting — run `aisquare board` for the full picture."] if truncated else []),
        f"Act on what needs the manager — reopen, re-spec, spawn, report. {WAKEUP_CLOSE}",
    ]
    spent = _output_cost(head) + sum(_output_cost(line) + 2 for line in frame)
    share = max((_WAKEUP_REASON_BUDGET - spent) // max(len(events), 1) - 4, _ELLIPSIS_COST)
    per_line = min(_WAKEUP_LINE_BUDGET, share)
    # The board label is inside the clipped text, so the budget above still holds.
    lines = [
        head,
        *(
            f"- {_clip(f'[{board}] {event_line(event, roles, numbers)}', per_line)}"
            for event in events
        ),
        *frame,
    ]
    return "\n".join(lines)
