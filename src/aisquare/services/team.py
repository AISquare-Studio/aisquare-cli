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

import json
import os
import re
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aisquare.core import brain, harness, insights, orchestrator, workspace
from aisquare.core.config import FleetSettings, load_config
from aisquare.core.ids import new_event_id, new_task_id
from aisquare.core.store import ContextStore, store_session, unmet_needs
from aisquare.models import ProjectInfo, TaskStatus, TeamEvent, TeamSession, TeamTask
from aisquare.services import distill as distill_service

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
#: an agent that went away (``agent_exited`` is the fleet's, emitted by ``reap``).
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
    }
)

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


def _project(store: ContextStore, cwd: Path | None) -> ProjectInfo:
    project = orchestrator.team_project(cwd)
    store.ensure_project(project)
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
    """Where a command delivers, and how to talk about it in receipts."""

    id: str
    name: str
    root: Path | None
    warning: str | None = None


def _board_of(store: ContextStore, project_id: str) -> _Board:
    """The board a known project id names (for task-ref commands)."""
    project = store.get_project(project_id)
    name = (project.root.name if project is not None else "") or project_id
    return _Board(id=project_id, name=name, root=project.root if project is not None else None)


def _board(store: ContextStore, session: TeamSession | None, cwd: Path | None) -> _Board:
    """The board an attributed command delivers to.

    With ``--as`` the SESSION's registered board wins — a session legitimately
    works across many worktrees and subdirectories, so cwd is a hint, not an
    identity (#20's misrouting bug: cwd resolution silently sent writes to a
    different board than the session's audience). Without a session, cwd
    resolves the board exactly as before. A cwd that disagrees with the
    session's board produces a warning, never a reroute.
    """
    if session is None:
        project = _project(store, cwd)
        return _Board(id=project.id, name=project.root.name or project.id, root=project.root)
    board = _board_of(store, session.project_id)
    cwd_board = orchestrator.team_project(cwd)
    if cwd_board.id == board.id:
        return board
    warning = (
        f"cwd resolves to board {cwd_board.root.name or cwd_board.id}, but session "
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


def account_label(account: str | None) -> str | None:
    """The short display form of an account: its directory name."""
    return Path(account).name if account else None


def task_key(title: str) -> str:
    """Derive the idempotency key for a task: a slug of its title."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:64] or "task"


def _nudge_manager(project_id: str, *, reason: str) -> None:
    """Wake the project's WAITING manager after a board write it must act on (§7.3, path 2).

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
            nudge(project_id, reason=reason)
    except Exception:  # the write already succeeded; a lost nudge is the whole cost
        return


# --- commands ----------------------------------------------------------------


def activate(cwd: Path | None = None) -> ProjectInfo:
    """Turn the orchestrator on for this project (``team on``)."""
    _require_enabled()
    _DELIVERY.set(None)
    with store_session() as store:
        project = _project(store, cwd)
        event = None
        if not store.team_active(project.id):
            event = _emit(store, project.id, "activate", "agent orchestrator activated")
        board = _Board(id=project.id, name=project.root.name or project.id, root=project.root)
    if event is not None:
        _record_delivery(event, board)
    return project


def board_data(
    cwd: Path | None = None,
    *,
    events: int = _BOARD_EVENTS,
    since_seq: int | None = None,
    project: ProjectInfo | None = None,
) -> tuple[ProjectInfo, list[TeamSession], list[TeamTask], list[TeamEvent]]:
    """Everything the board shows: sessions, tasks and recent events.

    ``since_seq`` turns the event fetch incremental (only rows past it) so a
    watch UI polling every few seconds does not rehydrate its whole window.
    ``project`` lets a long-lived caller resolve identity once and pass it in,
    sparing a ``git rev-parse`` per call (the watch TUI does this).
    """
    _require_enabled()
    with store_session() as store:
        resolved = project if project is not None else _project(store, cwd)
        if since_seq is None:
            fetched = store.recent_events(resolved.id, limit=events)
        else:
            fetched = store.events_since(resolved.id, since_seq, limit=events)
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
            task = store.get_task(task_ref)
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
            holder = store.get_project(event.project_id)
            elsewhere = (holder.root.name if holder is not None else "") or event.project_id
            return VerifyResult(
                event=None, board_id=board.id, board_name=board.name, elsewhere=elsewhere
            )
        roles = {s.id: s.role for s in store.team_sessions(board.id)}
        return VerifyResult(
            event=event, board_id=board.id, board_name=board.name, line=event_line(event, roles)
        )


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


def _signal_key(project_id: str, name: str) -> str:
    return f"signal/{project_id}/{name}"


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
    name: str, value: str, *, session_ref: str | None = None, cwd: Path | None = None
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
    _DELIVERY.set(None)
    if not _SIGNAL_NAME.fullmatch(name):
        raise ValueError(
            f"signal name {name!r} must be a lowercase token "
            "([a-z0-9._-], starting alphanumeric, max 64)"
        )
    if not _SIGNAL_VALUE.fullmatch(value):
        raise ValueError(f"signal value {value!r} must be a single token (no whitespace)")
    with store_session() as store:
        session = _resolve_session(store, session_ref)
        board = _board(store, session, cwd)
        key = _signal_key(board.id, name)
        prior = store.get_meta(key)
        prev = _signal_state(name, prior).value if prior is not None else None
        text = f"{name}: {value}" if prev is None else f"{name}: {value} (was {prev})"
        now = _now()
        event = store.add_signal_event(
            TeamEvent(
                id=new_event_id(),
                project_id=board.id,
                session_id=session.id if session else None,
                kind="signal",
                text=text,
                created_at=now,
            ),
            key,
            {
                "value": value,
                "session_id": session.id if session else None,
                "updated_at": now.isoformat(),
            },
        )
    stored = _record_delivery(event, board)
    state = SignalState(
        name=name,
        value=value,
        set_by=session.id if session else None,
        seq=stored.seq,
        updated_at=now,
    )
    return state, prev


def read_signal(
    name: str, *, session_ref: str | None = None, cwd: Path | None = None
) -> SignalState | None:
    """The current value of one named board state (``team signal NAME``)."""
    _require_enabled()
    with store_session() as store:
        session = _resolve_session(store, session_ref)
        board = _board(store, session, cwd)
        blob = store.get_meta(_signal_key(board.id, name))
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
) -> TeamEvent:
    """Put a note/decision/question/result on the team pipe."""
    _require_enabled()
    _DELIVERY.set(None)
    with store_session() as store:
        session = _resolve_session(store, session_ref)
        board = _board(store, session, cwd)
        task_id: str | None = None
        if task_ref is not None:
            task = store.get_task(task_ref)
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
    stored = _record_delivery(event, board)
    if kind in distill_service.DISTILL_KINDS:
        distill_service.spawn_drain(root=board.root)
    if kind in ("result", "question"):
        _nudge_manager(board.id, reason=kind)
    elif _addressed_to_manager(to_role):
        # §7.3 path 2 lists `note --to manager` among the writes that wake a
        # waiting manager, and only the KIND was consulted here: a note
        # addressed to the manager reached a waiting one on no path at all —
        # 'note' is not a MANAGER_WAKE_KIND either, so its Stop hook did not
        # deliver it and it sat past the cursor until some unrelated event.
        _nudge_manager(board.id, reason=f"{kind}_to_manager")
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
        board = _board(store, session, cwd)
        resolved_needs: list[str] = []
        for ref in needs or []:
            needed = store.get_task(ref)
            if needed is None:
                raise KeyError(ref)
            if needed.project_id != board.id:
                # A cross-project need would count as unmet forever (readiness
                # only sees this project's statuses) — starve, silently.
                raise ValueError(f"--needs {ref}: that task belongs to another project's board")
            if needed.id not in resolved_needs:
                resolved_needs.append(needed.id)
        now = _now()
        task, created = store.upsert_task(
            TeamTask(
                id=new_task_id(),
                project_id=board.id,
                key=key or task_key(title),
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
        return store.team_tasks(_project(store, cwd).id, status=status)


def show_task(ref: str) -> TeamTask:
    """One task in full. Raises ``KeyError`` if nothing matches."""
    _require_enabled()
    with store_session() as store:
        task = store.get_task(ref)
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


def claim_task(ref: str, *, session_ref: str | None = None) -> TeamTask:
    """Atomically claim a task; exactly one concurrent claimer wins.

    Raises :class:`ClaimLostError` when the task is already claimed (with a
    live lease) and ``KeyError`` when the ref matches nothing.
    """
    _require_enabled()
    _DELIVERY.set(None)
    with store_session() as store:
        task = store.get_task(ref)
        if task is None:
            raise KeyError(ref)
        session = _resolve_session(store, session_ref)
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
) -> tuple[TeamTask, _Board]:
    _DELIVERY.set(None)
    with store_session() as store:
        task = store.get_task(ref)
        if task is None:
            raise KeyError(ref)
        session = _resolve_session(store, session_ref)
        updated = store.set_task_status(task.id, status)
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
    return updated, board


def finish_task(ref: str, *, note: str | None = None, session_ref: str | None = None) -> TeamTask:
    """Mark a task done (``task done``)."""
    _require_enabled()
    task, board = _finish_task(ref, "done", "task_done", note=note, session_ref=session_ref)
    distill_service.spawn_drain(root=board.root)
    _nudge_manager(board.id, reason="task_done")
    return task


def review_task(ref: str, *, note: str | None = None, session_ref: str | None = None) -> TeamTask:
    """Send a task to review — done coding, awaiting verification (``task review``)."""
    _require_enabled()
    task, board = _finish_task(ref, "review", "task_review", note=note, session_ref=session_ref)
    distill_service.spawn_drain(root=board.root)
    _nudge_manager(board.id, reason="task_review")
    return task


def reopen_task(ref: str, *, reason: str, session_ref: str | None = None) -> TeamTask:
    """Send a task back to the pool with feedback (``task reopen``).

    The reason lands on the pipe as a task-linked event, so whoever picks the
    task up next (usually its previous owner's loop) sees the feedback.
    """
    _require_enabled()
    _DELIVERY.set(None)
    with store_session() as store:
        task = store.get_task(ref)
        if task is None:
            raise KeyError(ref)
        session = _resolve_session(store, session_ref)
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
    distill_service.spawn_drain(root=board.root)
    _nudge_manager(board.id, reason="task_reopened")
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
        board = _board(store, session, cwd)
        claimant = session.id if session else "cli"
        lease = _now() + timedelta(minutes=orchestrator.lease_minutes())
        event = None
        while True:
            task = store.next_task(board.id, role=role, status=status)
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
    task, board = _finish_task(ref, "blocked", "task_blocked", note=reason, session_ref=session_ref)
    distill_service.spawn_drain(root=board.root)
    _nudge_manager(board.id, reason="task_blocked")
    return task


def drop_task(ref: str, *, session_ref: str | None = None) -> TeamTask:
    """Drop a task that is no longer worth doing (``task drop``)."""
    _require_enabled()
    task, _ = _finish_task(ref, "dropped", "task_dropped", session_ref=session_ref)
    return task


def release_task(ref: str, *, session_ref: str | None = None) -> TeamTask:
    """Give a claimed task back to the pool (``task release``)."""
    _require_enabled()
    _DELIVERY.set(None)
    with store_session() as store:
        task = store.get_task(ref)
        if task is None:
            raise KeyError(ref)
        session = _resolve_session(store, session_ref)
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


def distill_now(cwd: Path | None = None, *, rescan: bool = False) -> int | None:
    """Drain the pipe into the project brain synchronously (``team distill``).

    ``None`` means another drain (usually a detached one) is already running.
    """
    _require_enabled()
    return distill_service.drain(cwd, rescan=rescan)


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
        distill_service.drain(cwd)  # a busy (None) drain still means progress
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
    which activates it.
    """
    if not orchestrator.team_enabled():
        return ""
    with store_session() as store:
        project = _project(store, cwd)
        role = orchestrator.env_role()
        if not store.team_active(project.id) and role is None:
            return ""
        known = store.get_session(session_id)
        now = _now()
        # Computed before upsert_session(), which overwrites transcript_path.
        collision = _shared_row_banner(known, transcript_path, now)
        # Self-reported by the payload — validated before it can reach any
        # other session's injected context.
        model = harness.clean_model_id(model)
        effort = harness.clean_effort(effort)
        session = store.upsert_session(
            TeamSession(
                id=session_id,
                project_id=project.id,
                role=role or (known.role if known else "unassigned"),
                started_at=now,
                last_seen_at=now,
                cursor=store.latest_seq(project.id),
                transcript_path=transcript_path,
                account=session_account(transcript_path),
                model=model,
                effort=effort,
            )
        )
        if role is not None and known is not None and known.role != role:
            session = store.update_session(session.id, role=role)
        # Presence is board state, not feed traffic: /clear cycles, resumes and
        # ephemeral `claude -p` children would otherwise spam join/left pairs.
        return collision + _render_board(
            project,
            store.team_sessions(project.id),
            store.team_tasks(project.id),
            store.recent_events(project.id, limit=_BOARD_EVENTS),
            me=session,
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
    """
    if not orchestrator.team_enabled():
        return ""
    with store_session() as store:
        session = store.get_session(session_id)
        if session is None:
            project = _project(store, cwd)
            role = orchestrator.env_role()
            if not store.team_active(project.id) and role is None:
                return ""
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
                )
            )
            return _render_board(
                project,
                store.team_sessions(project.id),
                store.team_tasks(project.id),
                store.recent_events(project.id, limit=_BOARD_EVENTS),
                me=session,
            )
        # Same check as session_start, on the path that actually runs every turn.
        # It must survive the empty-delta early return below: a collision warning
        # that only rides along with unrelated teammate traffic would go unseen for
        # exactly as long as the two agents were quiet, which is when the
        # interleaved claims do their damage.
        collision = _shared_row_banner(session, transcript_path, _now())
        lease = _now() + timedelta(minutes=orchestrator.lease_minutes())
        store.renew_leases(session.id, lease)
        raw = store.events_since(
            session.project_id,
            session.cursor,
            exclude_session=session.id,
            limit=_DELTA_LIMIT * 3 + 1,
        )
        # Attention notices are for the human board, not teammate context.
        events = [event for event in raw if event.kind != "attention"]
        if not events or not orchestrator.delta_enabled():
            cursor = raw[-1].seq if raw else None
            store.touch_session(session.id, cursor=cursor, state="working")
            return collision
        truncated = len(events) > _DELTA_LIMIT
        shown = events[:_DELTA_LIMIT]
        store.touch_session(session.id, cursor=shown[-1].seq, state="working")
        roles = {s.id: s.role for s in store.team_sessions(session.project_id)}
        return collision + _render_delta(shown, roles, truncated=truncated)


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
    """
    if not orchestrator.team_enabled():
        return None
    with store_session() as store:
        session = store.get_session(session_id)
        if session is None:
            return None
        store.renew_leases(session.id, _now() + timedelta(minutes=orchestrator.lease_minutes()))
        failure: Exception | None = None
        deferred: str | None = None
        if session.role == MANAGER_ROLE:
            try:
                decision = _manager_wakeup(store, session, stop_hook_active=stop_hook_active)
                if decision is None and stop_hook_active:
                    deferred = _deferred_wake_reason(store, session)
            except Exception as exc:  # waiting is the contract; the wake-up is the extra
                failure = exc
            else:
                if decision is not None:
                    return decision
        store.touch_session(session.id, state="waiting")
    if deferred is not None:
        # After the row says waiting, never before: nudge_manager refuses every
        # other state. Outside the `with` too — it opens its own connection.
        _nudge_manager(session.project_id, reason=deferred)
    if failure is not None:
        raise ManagerWakeupError(failure)
    return None


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
    :func:`hook_prompt_heartbeat`. Attention notices are for the human board,
    not teammate context.
    """
    raw = store.events_since(
        me.project_id, me.cursor, exclude_session=me.id, limit=_DELTA_LIMIT * 3 + 1
    )
    return [event for event in raw if event.kind != "attention"]


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
    store.set_meta(key, str(used + 1))
    store.touch_session(me.id, cursor=shown[-1].seq, state="working")
    return StopDecision(
        reason=_render_wakeup(shown, roles, truncated=truncated), cursor=shown[-1].seq
    )


def hook_notification(session_id: str, cwd: Path | None, message: str | None) -> None:
    """The session needs the user (permission request / idle notice).

    The feed event is emitted only on the transition INTO attention —
    Claude re-notifies while parked, and a per-notice event floods the feed
    with lines nobody can act on twice.
    """
    if not orchestrator.team_enabled():
        return
    with store_session() as store:
        session = store.get_session(session_id)
        if session is None:
            return
        if store.mark_attention(session.id):
            _emit(
                store,
                session.project_id,
                "attention",
                message or "needs your attention",
                session_id=session.id,
            )


def hook_session_end(session_id: str, cwd: Path | None) -> None:
    """Mark the session ended and release its claims back to the pool."""
    if not orchestrator.team_enabled():
        return
    with store_session() as store:
        session = store.get_session(session_id)
        if session is None:
            return
        released = store.end_session(session.id)
        # No "left" feed event — the board's session panel is the presence
        # view. Released claims below are real work signals and do go out.
        for task in released:
            _emit(
                store,
                session.project_id,
                "task_released",
                f"{task.title} (session ended)",
                session_id=session.id,
                task_id=task.id,
            )
        root = _project_root(store, session.project_id)
    # Safety drain: catch anything a per-command spawn missed this session.
    distill_service.spawn_drain(cwd, root=root)


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
        board = _Board(id=project.id, name=project.root.name or project.id, root=project.root)
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


def _age(when: datetime, now: datetime) -> str:
    minutes = max(0, int((now - when).total_seconds() // 60))
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h{minutes % 60:02d}m"


def render_board(
    project: ProjectInfo,
    sessions: list[TeamSession],
    tasks: list[TeamTask],
    events: list[TeamEvent],
) -> str:
    """The human/board view (``asq board``), without the protocol contract."""
    return _render_board(project, sessions, tasks, events, me=None)


def _render_board(
    project: ProjectInfo,
    sessions: list[TeamSession],
    tasks: list[TeamTask],
    events: list[TeamEvent],
    *,
    me: TeamSession | None,
) -> str:
    now = _now()
    lines = ["<aisquare-team>"]
    if me is not None:
        lines.append(
            f"You are team session {short_id(me.id)} (role: {me.role}) in "
            f"project {project.root.name or project.id}."
        )
    live = [s for s in sessions if s.ended_at is None]
    accounts = len({s.account for s in live if s.account})
    if live:
        lines.append("sessions:")
        for session in live:
            stale = now - session.last_seen_at > _STALE_AFTER
            parts = [f"  - {short_id(session.id)} {session.role}"]
            if me is not None and session.id == me.id:
                parts.append("(you)")
            label = account_label(session.account)
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
            if session.focus:
                parts.append(f"— focus: {session.focus}")
            parts.append(f"— {_age(session.last_seen_at, now)} ago")
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
            waits = " ⧗ waits on " + ", ".join(need[-8:] for need in waiting) if waiting else ""
            lines.append(f"  - {task.id} [{task.status}{claim}] {task.title}{waits}")
        if len(open_tasks) > _BOARD_TASKS:
            lines.append(f"  … {len(open_tasks) - _BOARD_TASKS} more — `aisquare task list`")
    if events:
        roles = {s.id: s.role for s in sessions}
        lines.append("recent updates:")
        lines.extend(f"  - {event_line(event, roles)}" for event in events)
    if me is not None:
        lines += [
            "Protocol: check this board before starting work; teammate updates",
            "arrive automatically on each prompt. Tasks are shared and idempotent —",
            'e.g. `aisquare task add "wire auth flow"` (safe to re-run). Claim before',
            f"working: `aisquare task claim <id> --as {short_id(me.id)}`; finish with",
            f"`aisquare task done <id> --as {short_id(me.id)}`. Share decisions/results:",
            f'`aisquare note "…" --as {short_id(me.id)}`. Full board: `aisquare board`.',
            "Every ✓ prints a receipt (seq N); `aisquare team verify <seq>` re-checks it.",
            *_role_cycle(me),
        ]
    lines.append("</aisquare-team>")
    return "\n".join(lines)


def _role_cycle(me: TeamSession) -> list[str]:
    """The standing work cycle for a role — injected so nobody has to paste it.

    The cycles themselves live in :mod:`aisquare.core.harness` (one source of
    truth for the whole harness: profiles, ladders, and briefings).

    Keyed on :func:`base_role`, so a numbered seat (``coder1``) receives its
    role's cycle. Without that a seat got an empty briefing — the one thing the
    seat's own comment in ``cli/launch.py`` promises it does not lose.
    """
    return harness.role_cycle(base_role(me.role), short_id(me.id))


def event_line(event: TeamEvent, roles: dict[str, str]) -> str:
    who = (
        f"{short_id(event.session_id)} ({roles.get(event.session_id, '?')})"
        if event.session_id
        else "cli"
    )
    target = f" → {event.to_role}" if event.to_role else ""
    task = f" [{event.task_id}]" if event.task_id else ""
    kind = event.kind.replace("task_", "")
    return f"{who} {kind}{target}:{task} {event.text}"


def _render_delta(events: list[TeamEvent], roles: dict[str, str], *, truncated: bool) -> str:
    count = f"{len(events)}{'+' if truncated else ''}"
    lines = [
        "<aisquare-team-delta>",
        f"{count} teammate update(s) since your last prompt:",
        *(f"- {event_line(event, roles)}" for event in events),
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


def _render_wakeup(events: list[TeamEvent], roles: dict[str, str], *, truncated: bool) -> str:
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
    lines = [head, *(f"- {_clip(event_line(event, roles), per_line)}" for event in events), *frame]
    return "\n".join(lines)
