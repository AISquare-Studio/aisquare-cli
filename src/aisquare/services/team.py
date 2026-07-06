"""The team bus: shared working memory for parallel agent sessions.

One project = one bus. Sessions are registered automatically by the Claude
Code hooks (``hook_*`` functions below); agents talk to the bus through the
``team``/``task``/``note``/``board`` commands. Every mutation appends a
:class:`TeamEvent` to the pipe, and each session receives the events it has
not yet seen as a compact delta on its next prompt.

Activation is deliberate: hooks stay silent in a project until a session is
launched with ``AISQUARE_ROLE`` set or someone runs ``aisquare team on`` —
so repos that never opted in never see team output.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aisquare.core import brain, teambus
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
    """Raised when a team command runs with the bus disabled (AISQUARE_TEAM=0)."""

    def __init__(self) -> None:
        super().__init__("the team bus is disabled (AISQUARE_TEAM=0)")


class ClaimLostError(RuntimeError):
    """Raised when a claim attempt loses to another session."""

    def __init__(self, task: TeamTask) -> None:
        holder = short_id(task.claimed_by) if task.claimed_by else "another session"
        super().__init__(f"task {task.id} is already claimed by {holder}")
        self.task = task


def short_id(value: str) -> str:
    """The display form of a session id (leading characters, git-style)."""
    return value[:_SHORT_ID]


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _require_enabled() -> None:
    if not teambus.team_enabled():
        raise TeamDisabledError()


def _project(store: ContextStore, cwd: Path | None) -> ProjectInfo:
    project = teambus.team_project(cwd)
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


def task_key(title: str) -> str:
    """Derive the idempotency key for a task: a slug of its title."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:64] or "task"


# --- commands ----------------------------------------------------------------


def activate(cwd: Path | None = None) -> ProjectInfo:
    """Turn the team bus on for this project (``team on``)."""
    _require_enabled()
    with store_session() as store:
        project = _project(store, cwd)
        if not store.team_active(project.id):
            _emit(store, project.id, "activate", "team bus activated")
        return project


def board_data(
    cwd: Path | None = None, *, events: int = _BOARD_EVENTS
) -> tuple[ProjectInfo, list[TeamSession], list[TeamTask], list[TeamEvent]]:
    """Everything the board shows: sessions, tasks and recent events."""
    _require_enabled()
    with store_session() as store:
        project = _project(store, cwd)
        return (
            project,
            store.team_sessions(project.id),
            store.team_tasks(project.id),
            store.recent_events(project.id, limit=events),
        )


def log_events(cwd: Path | None = None, *, limit: int = 30) -> list[TeamEvent]:
    """The recent team-pipe events for this project, oldest first."""
    _require_enabled()
    with store_session() as store:
        project = _project(store, cwd)
        return store.recent_events(project.id, limit=limit)


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
    with store_session() as store:
        session = store.get_session(session_ref)
        if session is None:
            raise KeyError(session_ref)
        updated = store.update_session(session.id, focus=text)
        _emit(store, updated.project_id, "focus", text, session_id=updated.id)
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
    with store_session() as store:
        project = _project(store, cwd)
        session = _resolve_session(store, session_ref)
        task_id: str | None = None
        if task_ref is not None:
            task = store.get_task(task_ref)
            if task is None:
                raise KeyError(task_ref)
            task_id = task.id
        event = _emit(
            store,
            project.id,
            kind,
            text,
            session_id=session.id if session else None,
            task_id=task_id,
            to_role=to_role,
        )
    if kind in distill_service.DISTILL_KINDS:
        distill_service.spawn_drain(root=project.root)
    return event


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
    with store_session() as store:
        project = _project(store, cwd)
        session = _resolve_session(store, session_ref)
        resolved_needs: list[str] = []
        for ref in needs or []:
            needed = store.get_task(ref)
            if needed is None:
                raise KeyError(ref)
            if needed.project_id != project.id:
                # A cross-project need would count as unmet forever (readiness
                # only sees this project's statuses) — starve, silently.
                raise ValueError(f"--needs {ref}: that task belongs to another project's board")
            if needed.id not in resolved_needs:
                resolved_needs.append(needed.id)
        now = _now()
        task, created = store.upsert_task(
            TeamTask(
                id=new_task_id(),
                project_id=project.id,
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
        if created:
            _emit(
                store,
                project.id,
                "task_added",
                task.title,
                session_id=session.id if session else None,
                task_id=task.id,
                to_role=task.role,
            )
        return task, created


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
    with store_session() as store:
        task = store.get_task(ref)
        if task is None:
            raise KeyError(ref)
        session = _resolve_session(store, session_ref)
        claimant = session.id if session else "cli"
        lease = _now() + timedelta(minutes=teambus.lease_minutes())
        if not store.claim_task(task.id, claimant, lease):
            current = store.get_task(task.id)
            assert current is not None  # it existed a moment ago
            raise ClaimLostError(current)
        claimed = store.get_task(task.id)
        assert claimed is not None  # just claimed
        _emit(
            store,
            claimed.project_id,
            "task_claimed",
            claimed.title,
            session_id=session.id if session else None,
            task_id=claimed.id,
        )
        return claimed


def _finish_task(
    ref: str,
    status: TaskStatus,
    kind: str,
    *,
    note: str | None = None,
    session_ref: str | None = None,
) -> tuple[TeamTask, Path | None]:
    with store_session() as store:
        task = store.get_task(ref)
        if task is None:
            raise KeyError(ref)
        session = _resolve_session(store, session_ref)
        updated = store.set_task_status(task.id, status)
        text = updated.title if note is None else f"{updated.title} — {note}"
        _emit(
            store,
            updated.project_id,
            kind,
            text,
            session_id=session.id if session else None,
            task_id=updated.id,
        )
        return updated, _project_root(store, updated.project_id)


def finish_task(ref: str, *, note: str | None = None, session_ref: str | None = None) -> TeamTask:
    """Mark a task done (``task done``)."""
    _require_enabled()
    task, root = _finish_task(ref, "done", "task_done", note=note, session_ref=session_ref)
    distill_service.spawn_drain(root=root)
    return task


def review_task(ref: str, *, note: str | None = None, session_ref: str | None = None) -> TeamTask:
    """Send a task to review — done coding, awaiting verification (``task review``)."""
    _require_enabled()
    task, root = _finish_task(ref, "review", "task_review", note=note, session_ref=session_ref)
    distill_service.spawn_drain(root=root)
    return task


def reopen_task(ref: str, *, reason: str, session_ref: str | None = None) -> TeamTask:
    """Send a task back to the pool with feedback (``task reopen``).

    The reason lands on the pipe as a task-linked event, so whoever picks the
    task up next (usually its previous owner's loop) sees the feedback.
    """
    _require_enabled()
    with store_session() as store:
        task = store.get_task(ref)
        if task is None:
            raise KeyError(ref)
        session = _resolve_session(store, session_ref)
        reopened = store.reopen_task(task.id)
        _emit(
            store,
            reopened.project_id,
            "task_reopened",
            f"{reopened.title} — {reason}",
            session_id=session.id if session else None,
            task_id=reopened.id,
        )
        root = _project_root(store, reopened.project_id)
    distill_service.spawn_drain(root=root)
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
    if claim and status != "todo":
        raise ValueError("--claim only applies to todo tasks")
    with store_session() as store:
        project = _project(store, cwd)
        session = _resolve_session(store, session_ref)
        claimant = session.id if session else "cli"
        lease = _now() + timedelta(minutes=teambus.lease_minutes())
        while True:
            task = store.next_task(project.id, role=role, status=status)
            if task is None or not claim:
                return task
            if store.claim_task(task.id, claimant, lease):
                claimed = store.get_task(task.id)
                assert claimed is not None  # just claimed
                _emit(
                    store,
                    claimed.project_id,
                    "task_claimed",
                    claimed.title,
                    session_id=session.id if session else None,
                    task_id=claimed.id,
                )
                return claimed
            # Lost the race for this one — the next loop iteration sees the
            # following todo task (the winner's claim moved this one to doing).


def block_task(ref: str, *, reason: str, session_ref: str | None = None) -> TeamTask:
    """Mark a task blocked, with the reason on the pipe (``task block``)."""
    _require_enabled()
    task, root = _finish_task(ref, "blocked", "task_blocked", note=reason, session_ref=session_ref)
    distill_service.spawn_drain(root=root)
    return task


def drop_task(ref: str, *, session_ref: str | None = None) -> TeamTask:
    """Drop a task that is no longer worth doing (``task drop``)."""
    _require_enabled()
    task, _ = _finish_task(ref, "dropped", "task_dropped", session_ref=session_ref)
    return task


def release_task(ref: str, *, session_ref: str | None = None) -> TeamTask:
    """Give a claimed task back to the pool (``task release``)."""
    _require_enabled()
    with store_session() as store:
        task = store.get_task(ref)
        if task is None:
            raise KeyError(ref)
        session = _resolve_session(store, session_ref)
        released = store.release_task(task.id)
        _emit(
            store,
            released.project_id,
            "task_released",
            released.title,
            session_id=session.id if session else None,
            task_id=released.id,
        )
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
    project = teambus.team_project(cwd)
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
    """Register this session on the bus and return the board injection.

    Silent (returns ``""``) unless the bus is enabled and this project has
    been activated — or the session was launched with ``AISQUARE_ROLE``,
    which activates it.
    """
    if not teambus.team_enabled():
        return ""
    with store_session() as store:
        project = _project(store, cwd)
        role = teambus.env_role()
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

    A session unknown to the bus but prompting inside an *active* project
    joins right here (the bus may have been activated after it started) and
    receives the full board + protocol instead of a delta.
    """
    if not teambus.team_enabled():
        return ""
    with store_session() as store:
        session = store.get_session(session_id)
        if session is None:
            project = _project(store, cwd)
            role = teambus.env_role()
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
        lease = _now() + timedelta(minutes=teambus.lease_minutes())
        store.renew_leases(session.id, lease)
        events = store.events_since(
            session.project_id,
            session.cursor,
            exclude_session=session.id,
            limit=_DELTA_LIMIT + 1,
        )
        if not events or not teambus.delta_enabled():
            cursor = events[-1].seq if events else None
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
    if not teambus.team_enabled():
        return
    with store_session() as store:
        session = store.get_session(session_id)
        if session is None:
            return
        store.renew_leases(session.id, _now() + timedelta(minutes=teambus.lease_minutes()))
        store.touch_session(session.id, state="waiting")


def hook_notification(session_id: str, cwd: Path | None, message: str | None) -> None:
    """The session needs the user (permission request / idle notice)."""
    if not teambus.team_enabled():
        return
    with store_session() as store:
        session = store.get_session(session_id)
        if session is None:
            return
        store.touch_session(session.id, state="attention")
        _emit(
            store,
            session.project_id,
            "attention",
            message or "needs your attention",
            session_id=session.id,
        )


def hook_session_end(session_id: str, cwd: Path | None) -> None:
    """Mark the session ended and release its claims back to the pool."""
    if not teambus.team_enabled():
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
