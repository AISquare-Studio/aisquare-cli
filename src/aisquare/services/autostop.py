"""Auto-stop of finished workers (P2-W1b, #283).

A tester that posted its verdict, a reviewer whose review is in, a validator past its gate:
each kept its seat, its memory and a slot of ``max_agents_total`` until someone stopped it,
and the manager, woken for nothing else, spent a turn on each. A finished worker is stopped
by the fleet itself, with ``actor: "auto-stop"`` on its ``agent_exited``, which wakes nobody
(P1 WS-2's ``wake_class``: a clean exit that left nothing open is news, not a wake).

:func:`finished` is the one predicate. A worker has finished when all of these hold:

- its role is in ``[fleet] auto_stop_roles`` (never a manager or the captain);
- it reads ``waiting`` (not working, ``attention`` or ``limited``);
- it has been idle ``auto_stop_idle_minutes``, by its board row and by its pane's output;
- it holds no claim;
- the task it was spawned for is closed, or, spawned for none, it wrote a ``result``;
- nothing addressed to its label or role waits past its cursor;
- nothing is queued that its cycle would take (``task next`` for its role);
- its input box is drawn, idle and empty, so a human's draft is never killed.

Three triggers, no daemon:

- **(a) Its Stop.** :func:`arm` runs from the Stop hook of an auto-stoppable fleet agent whose
  predicate holds bar the idle clause. It starts ``aisquare hook auto-stop`` detached, and
  the worker (:func:`check`) sleeps, asks again and stops it. Each Stop writes a new token
  (``autostop:<session>``), so a later turn supersedes an earlier worker.
- **(b) ``aisquare fleet reap --finished``** (:func:`reap_finished`), which ``aisquare watch``
  (W2) calls each tick; ``--dry-run`` lists the candidates and stops none.
- **(c) A refused spawn.** With ``auto_stop_finished`` on, a spawn that ``max_agents_total``
  or the memory floor refuses stops the finished workers first and asks once more
  (``services.fleet.spawn``).
"""

from __future__ import annotations

import contextlib
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from aisquare.core import selfcli
from aisquare.core.config import FleetSettings, load_config
from aisquare.core.store import ContextStore, store_session
from aisquare.core.tmux import TmuxError
from aisquare.models import CLOSED_STATUSES, FleetAgent, FleetAgentStatus, ProjectInfo
from aisquare.services.captain import screen

ACTOR = "auto-stop"
"""The ``actor`` of an auto-stop's ``agent_exited`` (P1 WS-2)."""
_NEVER = frozenset({"manager", "captain"})
_REVIEW_ROLES = frozenset({"tester", "runner", "reviewer", "ui-tester"})
_SETTLE = 5.0
"""Seconds the worker waits past the idle window, so the idle clause holds when it asks."""

_sleep: Callable[[float], None] = time.sleep


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _settings() -> FleetSettings:
    return load_config().fleet


def arm_key(session_id: str) -> str:
    """The token of the agent's latest Stop: an earlier Stop's worker stands down."""
    return f"autostop:{session_id}"


@dataclass(frozen=True)
class Verdict:
    """What :func:`finished` found: ``reason`` says why it has finished, or ``blocked`` the
    first condition that keeps it."""

    agent: FleetAgent
    reason: str | None
    blocked: str | None

    @property
    def finished(self) -> bool:
        return self.reason is not None


def _role(agent: FleetAgent) -> str:
    from aisquare.services.team import base_role  # lazy: team imports fleet

    return base_role(agent.role)


def finished(
    status: FleetAgentStatus,
    store: ContextStore,
    settings: FleetSettings,
    *,
    idle: bool = True,
    now: datetime | None = None,
) -> Verdict:
    """Whether the agent ``status`` lists has finished (see the module), and why.

    ``idle=False`` leaves the idle clause out: what the Stop asks before it arms a worker
    that will ask everything again once the window has passed.
    """
    agent = status.agent
    now = now or _now()

    def keep(why: str) -> Verdict:
        return Verdict(agent, None, why)

    role = _role(agent)
    if role in _NEVER or role not in settings.auto_stop_roles:
        return keep(f"{role} is not in auto_stop_roles")
    if agent.ended_at is not None:
        return keep("it has ended")
    if status.state != "waiting":
        return keep(f"it reads {status.state}")
    session = status.session
    if session is None or agent.session_id is None:
        return keep("it has no board session")
    window = timedelta(minutes=settings.auto_stop_idle_minutes)
    if idle and now - session.last_seen_at < window:
        return keep(f"its last turn ended under {settings.auto_stop_idle_minutes:g} min ago")
    tasks = store.team_tasks(session.project_id)
    if any(task.claimed_by == session.id for task in tasks):
        return keep("it holds a claim")
    if agent.task_id is not None:
        task = next((t for t in tasks if t.id == agent.task_id), None)
        if task is None or task.status not in CLOSED_STATUSES:
            return keep("the task it was spawned for is still open")
        reason = f"its task #{task.number} is {task.status}"
    else:
        wrote = [
            e
            for e in store.recent_events(session.project_id, limit=200)
            if e.session_id == session.id
            and e.kind == "result"
            and e.created_at >= agent.created_at
        ]
        if not wrote:
            return keep("it has written no result since it was spawned")
        reason = "it posted its result"
    names = {n.strip().lower() for n in (agent.label, agent.role, role) if n and n.strip()}
    for event in store.events_since(session.project_id, session.cursor, limit=200):
        if (event.to_role or "").strip().lower() in names:
            return keep(f"a {event.kind} addressed to it is unread (#{event.seq})")
    if agent.task_id is None and _queued(store, session.project_id, role):
        return keep(f"a task is queued that a {role} would take")
    return Verdict(agent, reason, None)


def _queued(store: ContextStore, project_id: str, role: str) -> bool:
    """Whether ``task next`` would hand a task-less ``role`` something: the review pool for a
    checking role, its role's ready tasks for any other."""
    if role in _REVIEW_ROLES:
        return store.next_task(project_id, status="review") is not None
    if role == "validator":
        return False  # it gates once, when it is spawned
    return store.next_task(project_id, role=role) is not None


def pane_refusal(agent: FleetAgent) -> str | None:
    """Why ``agent``'s pane is not an idle prompt with an empty input box, or ``None``: the
    last check before a stop, so a human's half-typed draft is never killed with it. Then
    P0-W2's own guard (:func:`delivery.draft_guard`), which adds someone typing into the pane
    in ``asq`` within ``[fleet] draft_quiet_seconds`` (the typing lease): a draft the box
    reader cannot see, or a key between two reads (W1b's owner call 6)."""
    from aisquare.services import delivery, fleet  # lazy: fleet imports this module

    try:
        srv = fleet.server_of(agent)
        lines = srv.capture(agent.pane_id).lines
    except (TmuxError, OSError) as exc:
        return f"tmux would not show its pane ({exc})"
    # By the pane's own agent (`screen.ready`: its input drawn, idle and empty where it reads
    # one); a draft Claude Code's box holds is `delivery.draft_guard`'s below.
    if not screen.ready(agent, lines):
        return "it is not idle at its prompt"
    try:
        return delivery.draft_guard(srv, agent.pane_id, quiet=fleet.settings().draft_quiet_seconds)
    except (TmuxError, OSError) as exc:
        return f"tmux would not show its pane ({exc})"


def _pane_idle_since(agent: FleetAgent) -> datetime | None:
    from aisquare.services import fleet

    try:
        facts = fleet.server_of(agent).pane_facts(agent.pane_id)
    except (TmuxError, OSError):
        return None
    return None if facts is None else facts.last_output


def _stop(project: ProjectInfo, verdict: Verdict) -> bool:
    """Stop the finished agent, ``actor=auto-stop``, nudging nobody; ``False`` when it was
    no longer there to stop."""
    from aisquare.services import fleet

    try:
        fleet.stop(
            project,
            verdict.agent.label,
            agent_id=verdict.agent.id,
            nudge=False,
            live_only=True,
            actor=ACTOR,
            reason=verdict.reason,
        )
    except fleet.FleetError:
        return False
    return True


def _decide(status: FleetAgentStatus, settings: FleetSettings) -> Verdict:
    """:func:`finished`, then the pane's own two clauses: its last output as old as the idle
    window, and an empty input box."""
    with store_session() as store:
        verdict = finished(status, store, settings)
    if not verdict.finished:
        return verdict
    window = timedelta(minutes=settings.auto_stop_idle_minutes)
    printed = _pane_idle_since(status.agent)
    if printed is not None and _now() - printed < window:
        return Verdict(status.agent, None, "its pane printed within the idle window")
    why = pane_refusal(status.agent)
    if why is not None:
        return Verdict(status.agent, None, why)
    return verdict


# --- (a) its Stop -------------------------------------------------------------------------


def arm(session_id: str, *, detach: Callable[[list[str]], None]) -> str | None:
    """From the Stop hook: start the worker for a fleet agent that may have finished. Returns
    why not (for the caller's log), or ``None`` when the worker was started.

    Every Stop of an auto-stoppable agent writes a new token, whether it arms or not: the
    worker an earlier turn started stands down."""
    settings = _settings()
    if not settings.auto_stop_finished:
        return "auto_stop_finished is off"
    from aisquare.services import fleet

    with store_session() as store:
        agent = store.fleet_agent_for_session(session_id)
        if agent is None or agent.ended_at is not None:
            return "not a live fleet agent"
        if _role(agent) in _NEVER or _role(agent) not in settings.auto_stop_roles:
            return f"{agent.role} is not in auto_stop_roles"
        token = uuid.uuid4().hex
        store.set_meta(arm_key(session_id), token)
    status = fleet.status_of(agent)
    with store_session() as store:
        verdict = finished(status, store, settings, idle=False)
    if not verdict.finished:
        return verdict.blocked
    after = settings.auto_stop_idle_minutes * 60 + _SETTLE
    detach(
        selfcli.argv_for(
            ["--quiet", "hook", "auto-stop", session_id, "--after", f"{after:g}", "--token", token]
        )
    )
    return None


def check(session_id: str, *, after: float, token: str) -> str:
    """The worker (``aisquare hook auto-stop``): sleep ``after`` seconds, then stop the agent
    if its Stop's token is still the latest and it has finished. Returns what happened."""
    from aisquare.services import fleet

    _sleep(after)
    with store_session() as store:
        if store.get_meta(arm_key(session_id)) != token:
            return "a later turn ended since it was armed"
        agent = store.fleet_agent_for_session(session_id)
        project = store.get_project(agent.project_id) if agent is not None else None
    if agent is None or project is None or agent.ended_at is not None:
        return "it has ended"
    verdict = _decide(fleet.status_of(agent), _settings())
    if not verdict.finished:
        return f"kept: {verdict.blocked}"
    return "stopped" if _stop(project, verdict) else "it was gone"


# --- (b) fleet reap --finished, and (c) a refused spawn -------------------------------------


@dataclass(frozen=True)
class Finished:
    """One live agent :func:`reap_finished` looked at: its verdict, and whether it was
    stopped."""

    verdict: Verdict
    stopped: bool


def reap_finished(projects: Sequence[ProjectInfo], *, dry_run: bool = False) -> list[Finished]:
    """Every live agent of ``projects`` that has finished, stopped unless ``dry_run``. The
    ones kept are listed too, with the condition that keeps them, for ``--dry-run``."""
    from aisquare.services import fleet

    settings = _settings()
    out: list[Finished] = []
    for project in projects:
        with store_session() as store:
            agents = store.fleet_agents(project.id, live_only=True)
        for agent in agents:
            if _role(agent) in _NEVER or _role(agent) not in settings.auto_stop_roles:
                continue
            with contextlib.suppress(fleet.FleetError):
                verdict = _decide(fleet.status_of(agent), settings)
                stopped = verdict.finished and not dry_run and _stop(project, verdict)
                out.append(Finished(verdict, stopped))
    return out


def every_project() -> list[ProjectInfo]:
    """The projects with a live fleet agent: where a refused spawn looks for finished
    workers (``max_agents_total`` and memory are the whole machine's)."""
    with store_session() as store:
        ids = {agent.project_id for agent in store.live_fleet_agents()}
        found = [store.get_project(pid) for pid in sorted(ids)]
    return [project for project in found if project is not None]
