"""The projection: one observation batch becomes one snapshot, and its events.

This module is a pure function with a class around it. :meth:`OfficeProjector.project`
takes an immutable :class:`~aisquare.office.models.LocalObservationBatch`, the
previous :class:`~aisquare.office.models.Projection` and a ``now`` supplied by the
caller, and returns a new projection. It opens no socket, starts no subprocess,
reads no clock, touches no filesystem and imports nothing from ``observe``,
``storage``, Starlette or tmux. Given equal inputs it returns an equal result,
which is the only reason the derived states in the contract are testable at all.

**Two derivations of ``since_s``, deliberately not unified.** ``data-contract.md``
§7 records this as a measured defect rather than a preference. ``mark_attention``
bumps ``last_seen_at`` on *every* re-notification — its second, unconditional
UPDATE — while emitting its feed event only on the transition *into* attention.
So a session parked for 600 seconds that Claude re-notifies every minute has a
last-seen column that is always fresh, and an attention wait derived from that
column reports 0. The queue orders by longest wait first, so the whole ordering
would degenerate into time-since-last-re-fire, which is near-uniform across
parked agents. The freshness bump is not the bug and must not be removed: over
15 minutes of silence is what makes an agent idle, and an agent whose column
stopped moving would age out of the queue while its prompt is still on screen.
Freshness and wait duration are two uses of one clock, separated here, on the
read side:

* ``attention`` counts from the **transition into attention** — the
  transition-guarded ``attention`` board event, carried forward across polls in
  :attr:`Projection.state_since` so it survives the event ageing out of the
  recent window. A re-notification does not reset it.
* ``waiting`` counts from ``last_seen_at``, where the plain column is already
  correct: the stop hook wrote it, and nothing re-fires while the agent waits.
* Every other state is 0.

A known and accepted limitation: a second prompt inside one unbroken attention
streak measures from the first, which over-reports the wait. That is preferred
to the under-report, which is what hides a parked agent.

**What this back-end cannot know, and therefore does not say.** ``snapshot.json``'s
own prose for ``Question.options`` assumes a permission hook carrying ``tool``,
``summary`` and ``questions[]``. This CLI installs five hooks — ``SessionStart``,
``UserPromptSubmit``, ``SessionEnd``, ``Stop`` and ``Notification`` — and P03
confirmed that set against a real install rather than against the plan. None of
them carries an option label, an ordering, a selection, a dialog kind, a tool
name or plan text. So :attr:`Question.tool` and :attr:`Question.summary` are
**not available** for a permission prompt here. They are omitted, which the
contract spells as absent; emitting ``""`` would render as a known-empty value
and claim an observation nobody made. The same rule governs cost, model
association, worktree statistics and run identity: unknown stays unknown.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from aisquare.models import (
    FleetAgent,
    FleetAgentStatus,
    ProjectInfo,
    TeamEvent,
    TeamSession,
    TeamTask,
)
from aisquare.office.models import (
    Agent,
    AgentEndedEvent,
    AgentEnteredEvent,
    AgentHealthEvent,
    AgentLeftEvent,
    AgentModeEvent,
    AgentOutputEvent,
    AgentPaneEvent,
    AgentStateEvent,
    AgentSubEvent,
    EndedReason,
    Event,
    LocalObservationBatch,
    ModelFamily,
    OfficeActivity,
    OfficeState,
    PaneHealth,
    PaneObservation,
    PermissionMode,
    Project,
    ProjectFrozenEvent,
    Projection,
    PromptChangedEvent,
    PromptEvidence,
    Question,
    QueueChangedEvent,
    Snapshot,
    Task,
    TaskChangedEvent,
)
from aisquare.office.providers import claude_observe

ATTENTION_STATE: Final = "attention"
"""The board's own spelling of the state ``mark_attention`` writes."""

WAITING_STATE: Final = "waiting"
"""The board state the ``Stop`` hook writes through ``touch_session``."""

ATTENTION_EVENT_KIND: Final = "attention"
"""The feed event emitted **only** on the transition into attention.

The transition guard is the whole reason this is the right clock for an
attention wait: ``mark_attention`` returns True once per transition, so a
re-notification adds no event and the timestamp below does not move.
"""

SIGNAL_EVENT_KIND: Final = "signal"
PAUSE_SIGNAL: Final = "fleet-paused"
"""``services.fleet.PAUSE_SIGNAL`` — the project-level hiring freeze."""

_SIGNAL_TEXT: Final = re.compile(r"^([a-z0-9][a-z0-9._-]*): (\S+)(?: \(was (\S+)\))?$")
"""``models._SIGNAL_TEXT``: the anchored ``name: value (was prev)`` serialization.

Matched anchored rather than by substring, exactly as the CLI decodes it. A
signal is set from validated single tokens, so this decode is exact — and a
``fleet-paused`` mentioned inside someone's note text can never be read as one.
"""

_PANE_ID: Final = re.compile(r"^%\d+$")
"""``snapshot.json#/$defs/PaneId``. A pane id that does not match is dropped
rather than emitted: the field is optional, and an arbitrary string here is a
tmux target travelling toward a browser."""

_MODEL_FAMILIES: Final[tuple[tuple[str, ModelFamily], ...]] = (
    ("fable", "fable"),
    ("opus", "opus"),
    ("sonnet", "sonnet"),
    ("haiku", "haiku"),
)
"""``data-contract.md`` §7's substring rule, in its documented order."""

NEUTRAL_MORALE: Final = 50
"""``morale`` with no feedback evidence at all.

§7 derives morale from gifts and whips over 24 hours. This CLI records neither:
there is no gift, whip or morale column, and no board event kind carries one.
50 is the contract's own neutral, and it is emitted as the required field's
safe default — not as a claim that anybody measured an agent's morale.
"""

_LIVE: Final[PaneHealth] = "live"
_UNKNOWN_HEALTH: Final[PaneHealth] = "unknown"
_POSITIVELY_GONE: Final[frozenset[PaneHealth]] = frozenset({"dead", "gone"})
"""Health readings that are *evidence a pane is finished*.

``unknown`` is deliberately not here. ``dead`` is tmux reporting an exited
process and ``gone`` is a server that answered and does not have the pane;
``unknown`` is a server that could not be asked, which is evidence about the
observer and none at all about the agent.
"""


class ProjectionError(ValueError):
    """Required observation identity is malformed.

    Raised at the boundary and never swallowed into a snapshot: a project with
    no id, an agent with no id or a naive timestamp cannot be projected into
    anything a client should trust. Optional unknown values take the other path
    entirely — they stay null.
    """


@dataclass(frozen=True, slots=True)
class ProjectionLimits:
    """Every bound and threshold the projection applies. Starting points."""

    idle_after_s: float = 900.0
    """§7's 15 minutes without a heartbeat. Never applied to an unanswered
    prompt: ``attention`` outranks ``idle`` and this threshold cannot evict it."""

    ended_retention_s: float = 600.0
    """§7's 10-minute window an ended row stays visible, so the office can show
    an agent leaving and offer Respawn."""

    max_output_tail: int = 6
    max_options: int = 8
    max_text_chars: int = 400
    max_line_chars: int = 400
    max_title_chars: int = 200
    max_projects: int = 32
    max_agents: int = 200
    max_tasks: int = 200
    max_needs: int = 50


@dataclass(frozen=True, slots=True)
class _Row:
    """One projected agent plus the private facts a diff and the next poll need."""

    agent: Agent
    evidence: PromptEvidence | None = None
    state_since: datetime | None = None
    """When the current wait began, carried into the next projection so that a
    re-notification cannot restart it."""


def _aware(value: datetime, what: str) -> datetime:
    """A timestamp that carries a real offset, or a typed error naming the field."""
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ProjectionError(f"{what} must be timezone-aware (UTC); got a naive datetime")
    return value


def _age_s(now: datetime, then: datetime | None) -> int:
    """Whole seconds from ``then`` to ``now``, clamped at zero.

    Clamped rather than allowed negative because the system clock is corrected
    while agents are running, and a negative age would fail the schema's
    ``minimum: 0`` on a field whose whole job is to be displayed. The original
    timestamps are preserved as they were observed; only the derived age moves.
    """
    if then is None:
        return 0
    if then.tzinfo is None or then.tzinfo.utcoffset(then) is None:
        return 0
    return max(0, int((now - then).total_seconds()))


def _bounded(value: str | None, limit: int) -> str:
    """One bounded, stripped string, with the cut made visible."""
    text = (value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _identifier(value: str | None, what: str) -> str:
    """A non-empty opaque id, or a typed projection error."""
    text = (value or "").strip()
    if not text:
        raise ProjectionError(f"{what} is required and must not be empty")
    return text


def model_family(model: str | None) -> ModelFamily:
    """§7's family rule. An unrecognised model is ``other``, never a guess."""
    lowered = (model or "").lower()
    for needle, family in _MODEL_FAMILIES:
        if needle in lowered:
            return family
    return "other"


def frozen_projects(events: Sequence[TeamEvent]) -> Mapping[str, bool]:
    """Which projects the *observed window* proves are frozen, and which are not.

    The authoritative value lives in board meta, which P03's ``BoardReader``
    deliberately does not read — five reads, no writes, no meta. What the batch
    does carry is the ``signal`` event ``fleet pause``/``resume`` emits, so a
    freeze is derivable exactly when its event is still in the recent window.

    A project with no such event is **absent from this mapping**, which the
    caller renders as ``frozen`` omitted rather than ``false``: "not observed"
    and "observed not frozen" are different, and only the second may be shown.
    The handoff proposes a meta read so the value stops depending on a window.
    """
    latest: dict[str, tuple[int, bool]] = {}
    for event in events:
        if event.kind != SIGNAL_EVENT_KIND:
            continue
        decoded = _SIGNAL_TEXT.match(event.text)
        if decoded is None or decoded.group(1) != PAUSE_SIGNAL:
            continue
        known = latest.get(event.project_id)
        if known is None or event.seq >= known[0]:
            latest[event.project_id] = (event.seq, decoded.group(2) == "on")
    return {project_id: value for project_id, (_seq, value) in latest.items()}


def attention_since(events: Sequence[TeamEvent]) -> Mapping[str, datetime]:
    """Per session, when it *transitioned* into attention.

    Keyed on the feed event rather than on ``last_seen_at`` for the reason the
    module docstring states: the column is bumped by every re-notification and
    the event is not.
    """
    latest: dict[str, tuple[int, datetime]] = {}
    for event in events:
        if event.kind != ATTENTION_EVENT_KIND or not event.session_id:
            continue
        known = latest.get(event.session_id)
        if known is None or event.seq >= known[0]:
            latest[event.session_id] = (event.seq, event.created_at)
    return {session_id: at for session_id, (_seq, at) in latest.items()}


class OfficeProjector:
    """P01's ``Projector``: a batch becomes a snapshot, and two snapshots events.

    Stateless. Everything carried between polls travels in the
    :class:`~aisquare.office.models.Projection` the caller hands back, so two
    projectors given the same inputs produce the same output and a restarted
    process does not resurrect state it no longer has evidence for.
    """

    def __init__(self, limits: ProjectionLimits | None = None) -> None:
        self._limits = limits or ProjectionLimits()

    @property
    def limits(self) -> ProjectionLimits:
        return self._limits

    # -- projection --------------------------------------------------------

    def project(
        self,
        batch: LocalObservationBatch,
        previous: Projection | None,
        now: datetime,
    ) -> Projection:
        """One immutable snapshot, deterministic in ``(batch, previous, now)``."""
        now = _aware(now, "now")
        _aware(batch.collected_at, "batch.collected_at")

        projects = self._projects(batch)
        rows = self._rows(batch, previous, now)
        snapshot = Snapshot(
            v=1,
            seq=max(0, batch.board_seq),
            taken_at=now,
            stale=batch.partial,
            projects=projects,
            agents=tuple(row.agent for row in rows),
            queue=self._queue(rows),
            tasks=self._tasks(batch),
        )
        prompts: dict[str, PromptEvidence] = {}
        state_since: dict[str, datetime] = {}
        for row in rows:
            if row.evidence is not None:
                prompts[row.agent.id] = row.evidence
            if row.state_since is not None:
                state_since[row.agent.id] = row.state_since
        return Projection(
            snapshot=snapshot,
            generation=0 if previous is None else previous.generation + 1,
            prompts=prompts,
            state_since=state_since,
        )

    # -- projects and tasks ------------------------------------------------

    def _projects(self, batch: LocalObservationBatch) -> tuple[Project, ...]:
        """The live CLI project collection, mapped and ordered by id."""
        frozen = frozen_projects(batch.events)
        found: list[Project] = []
        for info in batch.projects[: self._limits.max_projects]:
            found.append(self._project(info, frozen))
        return tuple(sorted(found, key=lambda project: project.id))

    def _project(self, info: ProjectInfo, frozen: Mapping[str, bool]) -> Project:
        project_id = _identifier(info.id, "ProjectInfo.id")
        root = str(info.root)
        if not root.startswith("/"):
            # ``Project.root`` is the one path the contract carries, and its
            # pattern is anchored. A relative root is malformed identity rather
            # than an unknown optional, so it stops here.
            raise ProjectionError(f"project {project_id} has a non-absolute root")
        return Project(
            id=project_id,
            name=info.root.name or project_id,
            root=root,
            codename=info.codename,
            frozen=frozen.get(project_id),
        )

    def _tasks(self, batch: LocalObservationBatch) -> tuple[Task, ...]:
        """Board tasks, bounded, ordered by ``(project, id)``, associations kept."""
        found: list[Task] = []
        for task in batch.tasks[: self._limits.max_tasks]:
            found.append(
                Task(
                    id=_identifier(task.id, "TeamTask.id"),
                    project_id=_identifier(task.project_id, "TeamTask.project_id"),
                    title=_bounded(task.title, self._limits.max_title_chars) or task.id,
                    status=task.status,
                    needs=tuple(task.needs[: self._limits.max_needs]),
                    claimed_by=task.claimed_by,
                    role=task.role,
                )
            )
        return tuple(sorted(found, key=lambda task: (task.project_id, task.id)))

    # -- agents ------------------------------------------------------------

    def _rows(
        self, batch: LocalObservationBatch, previous: Projection | None, now: datetime
    ) -> tuple[_Row, ...]:
        """Every agent row: fleet first, then sessions the fleet did not spawn."""
        panes = {pane.agent_id: pane for pane in batch.panes}
        prompts = {evidence.agent_id: evidence for evidence in batch.prompts}
        transitions = attention_since(batch.events)
        claimed = self._claims(batch.tasks)
        notes = self._notes(batch.events)
        known = previous.snapshot.agents if previous is not None else ()
        before = {agent.id: agent for agent in known}
        carried = dict(previous.prompts) if previous is not None else {}
        since_before = dict(previous.state_since) if previous is not None else {}

        rows: list[_Row] = []
        joined: set[str] = set()
        for status in batch.fleet:
            if status.agent.session_id:
                joined.add(status.agent.session_id)
            row = self._fleet_row(
                status,
                pane=panes.get(status.agent.id),
                evidence=prompts.get(status.agent.id),
                previous_agent=before.get(status.agent.id),
                previous_evidence=carried.get(status.agent.id),
                previous_since=since_before.get(status.agent.id),
                transitions=transitions,
                now=now,
            )
            if row is not None:
                rows.append(row)

        for session in batch.sessions:
            if session.id in joined:
                continue
            row = self._self_row(
                session,
                evidence=prompts.get(session.id),
                previous_agent=before.get(session.id),
                previous_evidence=carried.get(session.id),
                previous_since=since_before.get(session.id),
                transitions=transitions,
                claimed=claimed,
                notes=notes,
                now=now,
            )
            if row is not None:
                rows.append(row)

        rows.sort(key=lambda row: row.agent.id)
        return tuple(rows[: self._limits.max_agents])

    def _fleet_row(
        self,
        status: FleetAgentStatus,
        *,
        pane: PaneObservation | None,
        evidence: PromptEvidence | None,
        previous_agent: Agent | None,
        previous_evidence: PromptEvidence | None,
        previous_since: datetime | None,
        transitions: Mapping[str, datetime],
        now: datetime,
    ) -> _Row | None:
        """One fleet agent.

        **The row's identity is the fleet agent id, not the session id.** A fleet
        pane exists before any hook registers a session, and the packet requires
        that the transition be documented rather than an id silently replaced.
        Keying on the session id would mean a starting row is destroyed and a
        different row created the moment hooks arrive — an ``agent.left`` and an
        ``agent.entered`` for what the user watched the whole time — which is
        precisely the silent replacement. The fleet id exists across both halves
        of that transition, and it is also the key P03 files panes and evidence
        under, so downstream targeting resolves without a second mapping. The
        handoff records this as a deviation from the schema's prose.
        """
        agent = status.agent
        agent_id = _identifier(agent.id, "FleetAgent.id")
        session = status.session
        health = _health_of(pane)
        reachable = health == _LIVE

        ended_at = self._ended_at(agent.ended_at, session, pane, previous_agent, health, now)
        if ended_at is not None and _age_s(now, ended_at) >= self._limits.ended_retention_s:
            return None  # past the retention window: dropped, with agent.left in the diff

        live, state = self._state(
            ended_at=ended_at,
            session=session,
            evidence=evidence,
            previous_evidence=previous_evidence,
            previous_state=previous_agent.state if previous_agent is not None else None,
            reachable=reachable,
            starting=session is None and bool(agent.pane_id),
            now=now,
        )
        since = self._since(
            state,
            session=session,
            previous_state=previous_agent.state if previous_agent is not None else None,
            previous_since=previous_since,
            transitions=transitions,
            now=now,
        )
        lines = pane.lines if pane is not None else ()
        tail = tuple(
            _bounded(line, self._limits.max_line_chars)
            for line in lines[-self._limits.max_output_tail :]
        )
        project_id = _identifier(agent.project_id, "FleetAgent.project_id")
        return _Row(
            agent=Agent(
                id=agent_id,
                label=_bounded(agent.label, 64) or agent_id[:8],
                project_id=project_id,
                project_now=project_id,
                projects=(project_id,),
                role=(session.role if session is not None else agent.role) or "unassigned",
                origin="fleet",
                state=state,
                activity=_activity(state),
                since_s=_age_s(now, since),
                morale=NEUTRAL_MORALE,
                model_family=model_family(session.model if session is not None else None),
                up_s=_age_s(now, session.started_at if session is not None else agent.created_at),
                output_tail=tail,
                last_seen_at=self._last_seen(session, pane, now),
                sub="none",
                subagents=0,
                permission_mode=_permission_mode(lines),
                health=health,
                doing=session.focus if session is not None else None,
                question=self._question(live) if state in ("waiting", ATTENTION_STATE) else None,
                model=session.model if session is not None else None,
                pane_id=agent.pane_id if _PANE_ID.match(agent.pane_id or "") else None,
                task_id=agent.task_id,
                pane_alive=pane.alive if pane is not None else None,
                exit_status=_exit_of(pane, agent.exit_status),
                ended_at=ended_at,
                ended_reason=self._ended_reason(agent, session, health) if ended_at else None,
            ),
            evidence=live,
            state_since=since,
        )

    def _self_row(
        self,
        session: TeamSession,
        *,
        evidence: PromptEvidence | None,
        previous_agent: Agent | None,
        previous_evidence: PromptEvidence | None,
        previous_since: datetime | None,
        transitions: Mapping[str, datetime],
        claimed: Mapping[str, str],
        notes: Mapping[str, tuple[str, ...]],
        now: datetime,
    ) -> _Row | None:
        """One session the fleet did not spawn: no pane, and therefore no options.

        ``health`` is ``unknown`` here as a statement of fact — there is no pane
        to read — and never ``dead``. Its ``output_tail`` is the session's own
        board notes, which is what §7 specifies for a self origin.
        """
        session_id = _identifier(session.id, "TeamSession.id")
        ended_at = session.ended_at
        if ended_at is not None and _age_s(now, ended_at) >= self._limits.ended_retention_s:
            return None

        live, state = self._state(
            ended_at=ended_at,
            session=session,
            evidence=evidence,
            previous_evidence=previous_evidence,
            previous_state=previous_agent.state if previous_agent is not None else None,
            reachable=False,
            starting=False,
            now=now,
        )
        since = self._since(
            state,
            session=session,
            previous_state=previous_agent.state if previous_agent is not None else None,
            previous_since=previous_since,
            transitions=transitions,
            now=now,
        )
        project_id = _identifier(session.project_id, "TeamSession.project_id")
        return _Row(
            agent=Agent(
                id=session_id,
                label=_bounded(session.label, 64) or session_id[:8],
                project_id=project_id,
                project_now=project_id,
                projects=(project_id,),
                role=session.role or "unassigned",
                origin="self",
                state=state,
                activity=_activity(state),
                since_s=_age_s(now, since),
                morale=NEUTRAL_MORALE,
                model_family=model_family(session.model),
                up_s=_age_s(now, session.started_at),
                output_tail=notes.get(session_id, ()),
                last_seen_at=session.last_seen_at,
                sub="none",
                subagents=0,
                permission_mode="unknown",
                health=_UNKNOWN_HEALTH,
                doing=session.focus,
                question=self._question(live) if state in ("waiting", ATTENTION_STATE) else None,
                model=session.model,
                task_id=claimed.get(session_id),
                ended_at=ended_at,
                ended_reason="exit" if ended_at is not None else None,
            ),
            evidence=live,
            state_since=since,
        )

    # -- the derivations ---------------------------------------------------

    def _state(
        self,
        *,
        ended_at: datetime | None,
        session: TeamSession | None,
        evidence: PromptEvidence | None,
        previous_evidence: PromptEvidence | None,
        previous_state: OfficeState | None,
        reachable: bool,
        starting: bool,
        now: datetime,
    ) -> tuple[PromptEvidence | None, OfficeState]:
        """Explicit evidence, in the contract's precedence order.

        The order is prompt/attention, terminal waiting, working, idle, ended —
        with ``ended`` read *first* because it is the one fact no board row may
        contradict: a process that has exited cannot be waiting, however recently
        its hooks fired, and a killed agent fires no ``SessionEnd`` at all.

        Two distinctions carry the weight here:

        * **Attention outranks idle.** An unanswered prompt stays ``attention``
          however old it is. If the 15-minute threshold could evict it, a prompt
          older than 15 minutes would drop out of the queue — the exact failure
          the product exists to prevent.
        * **A stale source is not an answer.** When the pane could not be read
          at all, the previous prompt is carried forward and the row keeps its
          last-good state. When the pane *was* read and showed no dialog, P03
          marks its evidence ``stale`` and the prompt is genuinely gone: the user
          answered in the terminal. Those two look identical from a distance and
          mean opposite things.
        """
        live = evidence if (evidence is not None and not evidence.stale) else None
        if live is None and evidence is None and not reachable and previous_evidence is not None:
            # Nothing contradicted it: an unreadable pane cannot clear a prompt.
            live = previous_evidence

        if ended_at is not None:
            return None, "ended"
        if live is not None:
            return live, ATTENTION_STATE
        if session is not None and session.state == ATTENTION_STATE and not reachable:
            # The board says the agent wants a human and the pane cannot be read
            # to confirm or deny. Attention, with no question to show for it.
            return None, ATTENTION_STATE
        if starting:
            return None, "starting"
        if session is not None and session.state == WAITING_STATE:
            return None, "waiting"
        if not reachable and previous_state is not None and previous_state != "starting":
            return None, previous_state
        if session is None:
            return None, "idle"
        if _age_s(now, session.last_seen_at) >= self._limits.idle_after_s:
            return None, "idle"
        return None, "working"

    def _since(
        self,
        state: OfficeState,
        *,
        session: TeamSession | None,
        previous_state: OfficeState | None,
        previous_since: datetime | None,
        transitions: Mapping[str, datetime],
        now: datetime,
    ) -> datetime | None:
        """When the current wait began — two clocks, never one.

        See the module docstring. ``attention`` is preserved across polls
        precisely so that a re-notification, which refreshes the column but emits
        no event, cannot restart the count.
        """
        if state == ATTENTION_STATE:
            if previous_state == ATTENTION_STATE and previous_since is not None:
                return previous_since
            if session is not None:
                observed = transitions.get(session.id)
                if observed is not None:
                    return observed
            return now
        if state == "waiting":
            return session.last_seen_at if session is not None else now
        return None

    def _question(self, evidence: PromptEvidence | None) -> Question | None:
        """P03's evidence as the strict ``Question``, claiming nothing extra.

        ``tool`` and ``summary`` are absent by construction: no installed hook
        carries either, and the frame that carries the options does not name the
        tool. ``default`` is likewise never set — P03 reports the cursor position
        as ``selection`` because "this row is highlighted" and "this is the
        remembered default" are different claims, and only the first was seen.
        """
        if evidence is None:
            return None
        raw = evidence.raw
        return Question(
            kind=evidence.kind,
            text=_bounded(_first_line(raw), self._limits.max_text_chars),
            detected_by=evidence.detected_by,
            options=tuple(evidence.options[: self._limits.max_options]) or None,
            questions=tuple(evidence.questions) or None,
            deny_reason=True if any(o.key == "deny" for o in evidence.options) else None,
            raw=raw,
            prompt_id=evidence.prompt_id,
        )

    def _ended_at(
        self,
        agent_ended_at: datetime | None,
        session: TeamSession | None,
        pane: PaneObservation | None,
        previous_agent: Agent | None,
        health: PaneHealth,
        now: datetime,
    ) -> datetime | None:
        """When this row finished, preferring recorded evidence to observation.

        A pane observed positively dead or gone ends the row even with no
        ``SessionEnd`` — that is the crash and the kill, which record nothing.
        The timestamp is then pinned to the previous projection's once it exists,
        so the retention window counts from when the office first saw it end
        rather than restarting on every poll.
        """
        if agent_ended_at is not None:
            return agent_ended_at
        if session is not None and session.ended_at is not None:
            return session.ended_at
        if health not in _POSITIVELY_GONE:
            return None
        if previous_agent is not None and previous_agent.ended_at is not None:
            return previous_agent.ended_at
        return pane.observed_at if pane is not None else now

    def _ended_reason(
        self, agent: FleetAgent, session: TeamSession | None, health: PaneHealth
    ) -> EndedReason:
        """§7's reason vocabulary, from the evidence that is actually available.

        ``SessionEnd``'s own reason is read by the hook and never persisted to a
        column, so ``clear`` and ``logout`` cannot be distinguished from ``exit``
        here and are not guessed at. A dead pane is ``crash``; a window the
        server no longer has is ``killed``.
        """
        if agent.ended_at is not None:
            return "exit"
        if session is not None and session.ended_at is not None:
            return "exit"
        if health == "dead":
            return "crash"
        if health == "gone":
            return "killed"
        return "other"

    def _last_seen(
        self, session: TeamSession | None, pane: PaneObservation | None, now: datetime
    ) -> datetime:
        """The freshest first-hand evidence this row was alive."""
        if session is not None:
            return session.last_seen_at
        return pane.observed_at if pane is not None else now

    def _claims(self, tasks: Sequence[TeamTask]) -> Mapping[str, str]:
        """Session id → the task it holds, lowest task id winning a tie."""
        held: dict[str, str] = {}
        for task in sorted(tasks, key=lambda task: task.id):
            if task.claimed_by and task.claimed_by not in held:
                held[task.claimed_by] = task.id
        return held

    def _notes(self, events: Sequence[TeamEvent]) -> Mapping[str, tuple[str, ...]]:
        """A self session's monitor tail: its own last board notes, oldest first."""
        found: dict[str, list[tuple[int, str]]] = {}
        for event in events:
            if not event.session_id or event.kind == SIGNAL_EVENT_KIND:
                continue
            found.setdefault(event.session_id, []).append(
                (event.seq, _bounded(event.text, self._limits.max_line_chars))
            )
        return {
            session_id: tuple(text for _seq, text in sorted(rows)[-self._limits.max_output_tail :])
            for session_id, rows in found.items()
        }

    def _queue(self, rows: Sequence[_Row]) -> tuple[str, ...]:
        """Attention first (longest wait first), then waiting (longest first).

        An ended row is never here, whatever else it carries: the office may
        still show it leaving, but it cannot hold a place in a queue for a
        question nobody can answer. Ties break on project then opaque id, so the
        order is total and two equal batches produce byte-equal queues.
        """
        ranked: list[tuple[int, int, str, str]] = []
        for row in rows:
            agent = row.agent
            if agent.state == ATTENTION_STATE:
                rank = 0
            elif agent.state == "waiting":
                rank = 1
            else:
                continue
            ranked.append((rank, -agent.since_s, agent.project_id, agent.id))
        return tuple(entry[3] for entry in sorted(ranked))

    # -- diff --------------------------------------------------------------

    def diff(self, previous: Projection | None, current: Projection) -> tuple[Event, ...]:
        """The canonical ordered events between two projections.

        Compares **values**, not object identity, and never consults the board
        sequence: pane-derived facts — a dialog opening, a mode change, a pane
        dying — move while ``seq`` stands still, and a differ keyed on ``seq``
        would drop exactly the events the office exists to show. Duplicate public
        SSE ids are legitimate; the private generation is what advanced.

        On the initial projection this returns an **empty tuple**. P05 sends a
        complete snapshot to a new subscriber, and synthesising an ``agent.entered``
        for every pre-existing row would arrive as a second, contradictory
        description of the same instant. ``tests/office/test_projection.py`` pins
        that decision.
        """
        if previous is None:
            return ()

        before = {agent.id: agent for agent in previous.snapshot.agents}
        after = {agent.id: agent for agent in current.snapshot.agents}
        events: list[Event] = []

        events.extend(self._project_events(previous.snapshot, current.snapshot))
        for agent_id in sorted(set(after) - set(before)):
            events.append(AgentEnteredEvent(agent=after[agent_id]))
        for agent_id in sorted(set(after) & set(before)):
            events.extend(self._agent_events(before[agent_id], after[agent_id]))
        if previous.snapshot.queue != current.snapshot.queue:
            events.append(QueueChangedEvent(queue=current.snapshot.queue))
        events.extend(self._task_events(previous.snapshot, current.snapshot))
        for agent_id in sorted(set(after) & set(before)):
            new, old = after[agent_id], before[agent_id]
            if new.state == "ended" and old.state != "ended":
                events.append(
                    AgentEndedEvent(
                        agent_id=agent_id,
                        ended_reason=new.ended_reason or "other",
                        output_tail=new.output_tail,
                        exit_status=new.exit_status,
                    )
                )
        for agent_id in sorted(set(before) - set(after)):
            events.append(
                AgentLeftEvent(
                    agent_id=agent_id,
                    reason="ended" if before[agent_id].state == "ended" else "pruned",
                )
            )
        return tuple(events)

    def _project_events(self, before: Snapshot, after: Snapshot) -> list[Event]:
        """Freeze changes only. A freeze is a hiring freeze and nothing else: it
        never marks a working agent paused, and no agent event follows from it."""
        known = {project.id: project.frozen for project in before.projects}
        events: list[Event] = []
        for project in sorted(after.projects, key=lambda project: project.id):
            if project.frozen is None:
                continue
            if known.get(project.id) != project.frozen:
                events.append(ProjectFrozenEvent(project_id=project.id, frozen=project.frozen))
        return events

    def _agent_events(self, before: Agent, after: Agent) -> list[Event]:
        """One agent's changes, in a stable field order.

        State first, because everything after it is read in its light; then the
        pane-derived facts, which are the ones that fire on an unchanged board
        sequence; then the prompt. ``agent.health`` precedes the ``agent.ended``
        emitted later in the pass, as §6 requires.
        """
        events: list[Event] = []
        if before.state != after.state:
            events.append(
                AgentStateEvent(
                    agent_id=after.id,
                    state=after.state,
                    since_s=after.since_s,
                    question=after.question,
                )
            )
        if before.output_tail != after.output_tail:
            events.append(AgentOutputEvent(agent_id=after.id, output_tail=after.output_tail))
        if (before.sub, before.subagents, before.sub_detail) != (
            after.sub,
            after.subagents,
            after.sub_detail,
        ):
            events.append(
                AgentSubEvent(
                    agent_id=after.id,
                    sub=after.sub,
                    subagents=after.subagents,
                    sub_detail=after.sub_detail,
                )
            )
        if before.permission_mode != after.permission_mode:
            events.append(AgentModeEvent(agent_id=after.id, permission_mode=after.permission_mode))
        if before.health != after.health:
            events.append(
                AgentHealthEvent(
                    agent_id=after.id, health=after.health, exit_status=after.exit_status
                )
            )
        if before.pane_alive != after.pane_alive and after.pane_alive is not None:
            events.append(
                AgentPaneEvent(
                    agent_id=after.id,
                    pane_alive=after.pane_alive,
                    exit_status=after.exit_status,
                )
            )
        if before.question != after.question:
            events.append(PromptChangedEvent(agent_id=after.id, question=after.question))
        return events

    def _task_events(self, before: Snapshot, after: Snapshot) -> list[Event]:
        known = {task.id: task for task in before.tasks}
        return [TaskChangedEvent(task=task) for task in after.tasks if known.get(task.id) != task]


def _health_of(pane: PaneObservation | None) -> PaneHealth:
    """A row with no pane reads ``unknown`` — the absence of an observer."""
    return pane.health if pane is not None else _UNKNOWN_HEALTH


def _exit_of(pane: PaneObservation | None, recorded: int | None) -> int | None:
    """tmux's exit status when it knew one, else whatever the fleet row recorded."""
    if pane is not None and pane.exit_status is not None:
        return pane.exit_status
    return recorded


def _permission_mode(lines: Sequence[str]) -> PermissionMode:
    """The mode the pane's footer advertises, or ``unknown``.

    ``unknown`` is a real answer and the default. Reporting ``default`` from a
    frame that never mentioned a mode would be a claim about the agent's
    configuration drawn from silence.
    """
    return claude_observe.permission_mode(lines) if lines else "unknown"


def _activity(state: OfficeState) -> OfficeActivity:
    """§7's fallback, which is the only branch this batch can reach.

    The rule classifies the last tool call within 90 seconds, and P03 supplies no
    turn evidence at all — ``TurnMetric``'s token columns are never assigned by
    any code path, so a turn read here would carry nulls dressed as usage. What
    §7 says for that case is exactly this: no call and working means ``planning``,
    an idle state means ``idle``. P10 owns the real classification.
    """
    return "idle" if state in ("idle", "ended") else "planning"


def _first_line(raw: str | None) -> str:
    """The first non-empty line of bounded pane evidence — what is being asked.

    ``PromptEvidence`` carries no separate ``text``: P03's ``FrameDialog`` has
    one, and ``frame_evidence`` does not forward it. For frame evidence ``raw``
    begins two lines above the option block, so its first non-empty line is the
    dialog's own question ("Do you want to proceed?"); for notification evidence
    ``raw`` *is* the message. The handoff proposes ``PromptEvidence.text`` so the
    mapping stops depending on that layout.
    """
    for line in (raw or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


__all__ = [
    "ATTENTION_EVENT_KIND",
    "ATTENTION_STATE",
    "NEUTRAL_MORALE",
    "PAUSE_SIGNAL",
    "WAITING_STATE",
    "OfficeProjector",
    "ProjectionError",
    "ProjectionLimits",
    "attention_since",
    "frozen_projects",
    "model_family",
]
