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

import re
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aisquare.core import brain, orchestrator
from aisquare.core.ids import new_event_id, new_task_id
from aisquare.core.store import ContextStore, store_session, unmet_needs
from aisquare.models import ProjectInfo, TaskStatus, TeamEvent, TeamSession, TeamTask
from aisquare.services import distill as distill_service

_SHORT_ID = 8
_DELTA_LIMIT = 10
_BOARD_TASKS = 8
_BOARD_EVENTS = 5
_STALE_AFTER = timedelta(minutes=30)


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
        """The human receipt appended to a ✓ line: where the write proved durable."""
        if self.seq is None:
            return f"on {self.board_name}"
        return f"seq {self.seq} on {self.board_name}"


_DELIVERY: ContextVar[Delivery | None] = ContextVar("aisquare_team_delivery", default=None)


def last_delivery() -> Delivery | None:
    """The receipt of the most recent write in this call context (or ``None``).

    Deliberately out-of-band: the CLI and the MCP server both attach receipts
    to success output, and threading a receipt through every service signature
    would churn an API surface other RC work is touching in parallel. Each
    write resets this before doing anything, so a stale receipt can never leak
    into the next command's output.
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
    return store.add_team_event(
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


def task_key(title: str) -> str:
    """Derive the idempotency key for a task: a slug of its title."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:64] or "task"


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
    """The outcome of a receipt check (``team verify``)."""

    event: TeamEvent | None
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
            return VerifyResult(event=None, board_name=board.name)
        if event.project_id != board.id:
            holder = store.get_project(event.project_id)
            elsewhere = (holder.root.name if holder is not None else "") or event.project_id
            return VerifyResult(event=None, board_name=board.name, elsewhere=elsewhere)
        roles = {s.id: s.role for s in store.team_sessions(board.id)}
        return VerifyResult(event=event, board_name=board.name, line=event_line(event, roles))


def set_role(role: str, session_ref: str, cwd: Path | None = None) -> TeamSession:
    """Set the role of a session (``team role``)."""
    _require_enabled()
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
    return task


def review_task(ref: str, *, note: str | None = None, session_ref: str | None = None) -> TeamTask:
    """Send a task to review — done coding, awaiting verification (``task review``)."""
    _require_enabled()
    task, board = _finish_task(ref, "review", "task_review", note=note, session_ref=session_ref)
    distill_service.spawn_drain(root=board.root)
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


def hook_session_start(
    session_id: str,
    cwd: Path | None,
    source: str | None,
    *,
    transcript_path: str | None = None,
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
        session = store.upsert_session(
            TeamSession(
                id=session_id,
                project_id=project.id,
                role=role or (known.role if known else "unassigned"),
                started_at=now,
                last_seen_at=now,
                cursor=store.latest_seq(project.id),
                transcript_path=transcript_path,
            )
        )
        if role is not None and known is not None and known.role != role:
            session = store.update_session(session.id, role=role)
        # Presence is board state, not feed traffic: /clear cycles, resumes and
        # ephemeral `claude -p` children would otherwise spam join/left pairs.
        return _render_board(
            project,
            store.team_sessions(project.id),
            store.team_tasks(project.id),
            store.recent_events(project.id, limit=_BOARD_EVENTS),
            me=session,
        )


def hook_prompt_heartbeat(
    session_id: str, cwd: Path | None, *, transcript_path: str | None = None
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
                )
            )
            return _render_board(
                project,
                store.team_sessions(project.id),
                store.team_tasks(project.id),
                store.recent_events(project.id, limit=_BOARD_EVENTS),
                me=session,
            )
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
            return ""
        truncated = len(events) > _DELTA_LIMIT
        shown = events[:_DELTA_LIMIT]
        store.touch_session(session.id, cursor=shown[-1].seq, state="working")
        roles = {s.id: s.role for s in store.team_sessions(session.project_id)}
        return _render_delta(shown, roles, truncated=truncated)


def hook_stop(session_id: str, cwd: Path | None) -> None:
    """The session finished its turn: it is now waiting for input.

    Also renews claim leases — the end of a long agentic turn is exactly when
    a lease is at its oldest.
    """
    if not orchestrator.team_enabled():
        return
    with store_session() as store:
        session = store.get_session(session_id)
        if session is None:
            return
        store.renew_leases(session.id, _now() + timedelta(minutes=orchestrator.lease_minutes()))
        store.touch_session(session.id, state="waiting")


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
    cwd: Path | None = None,
) -> PruneReport:
    """Retire ghost sessions — live rows with no heartbeat past the threshold.

    A session stays registered until its Claude Code process fires the end
    hook; a killed loop, a crashed terminal, or an MCP server that never says
    goodbye lingers on the board as ``(stale)`` indefinitely. This ends those
    rows so the board reflects who is actually present — and, because
    :meth:`ContextStore.end_session` returns each ghost's in-flight (``doing``)
    claims to the pool, it also frees any task stranded under a dead session so
    a live one can pick it up.

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
    now = _now()
    pruned: list[PrunedSession] = []
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
            released = 0
            if not dry_run:
                released_tasks = store.end_session(session.id)
                released = len(released_tasks)
                for task in released_tasks:
                    _emit(
                        store,
                        project.id,
                        "task_released",
                        f"{task.title} (session pruned)",
                        session_id=session.id,
                        task_id=task.id,
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
        board = _Board(id=project.id, name=project.root.name or project.id, root=project.root)
    if summary is not None:
        # The summary is the batch's last insert: confirming it confirms the
        # released-claim events committed before it on the same connection.
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
    if live:
        lines.append("sessions:")
        for session in live:
            stale = now - session.last_seen_at > _STALE_AFTER
            parts = [f"  - {short_id(session.id)} {session.role}"]
            if me is not None and session.id == me.id:
                parts.append("(you)")
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
    """The standing work cycle for a role — injected so nobody has to paste it."""
    sid = short_id(me.id)
    if me.role == "planner":
        return [
            "Your standing cycle (planner): keep the board stocked — turn findings and",
            'requests into `aisquare task add "<title>" --role coder|runner --detail "…"`',
            "(re-emitting is safe). Record choices:",
            f'`aisquare note "…" --kind decision --as {sid}`.',
        ]
    if me.role == "coder":
        return [
            f"Your standing cycle (coder): `aisquare task next --role coder --claim --as {sid}`;",
            "if nothing is available, tell the user and stop. Otherwise do the work in the",
            f'task\'s repo, then `aisquare task review <id> --note "<how to verify>" --as {sid}`',
            "and pick up the next one.",
        ]
    if me.role == "runner":
        return [
            "Your standing cycle (runner): `aisquare task next --status review`; if nothing,",
            "tell the user and stop. Otherwise verify the change end-to-end by actually",
            f'running it, then `aisquare task done <id> --note "verified: …" --as {sid}` or',
            f'`aisquare task reopen <id> --reason "<what failed + repro>" --as {sid}`. Repeat.',
        ]
    return []


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
