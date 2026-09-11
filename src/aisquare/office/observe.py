"""One bounded, read-only look at everything local.

:class:`LocalCollector` implements P01's ``LocalSource``: a blocking, bounded
sequence of short public reads that returns an immutable
:class:`~aisquare.office.models.LocalObservationBatch`. It is called from a
worker, never from the event loop, and it changes nothing — no spawn, no
keystroke, no store write, no ``TeamEvent``. A poll that runs while the user is
working must be invisible to them.

**Order matters.** The board is read first, through one short ``store_session``
block that is closed again *before* any tmux command runs: a pane that takes a
second to answer must not be holding a database handle while it does. Then the
fleet rows are derived, then panes are captured, then evidence is decoded from
what was captured. Each phase records its own failure and leaves the others
intact, so an unreachable tmux yields a batch with projects, sessions and tasks
in it rather than nothing at all.

**Budgets are enforced here, not delegated.** ``core.tmux``'s own
``_COMMAND_TIMEOUT`` is 30 seconds — sixty times the whole 500 ms observation
budget — so a single stuck pane would blow the budget by two orders of magnitude
if this module simply called ``capture`` and waited. Two mechanisms instead:
:func:`_bounded_call` stops *waiting* on one capture at the pane deadline, and
the loop stops issuing captures once the whole-collection deadline has passed,
naming the panes it skipped rather than silently returning fewer.

What that bound does **not** do is kill the tmux process: the call is abandoned
on a daemon thread and tmux's own timeout ends it later. The honest description
is that the collector stops waiting, not that the command stops running. Killing
it would mean this module owning a ``subprocess.run``, which is a new
process-spawn site and requires a written ruling in ``core.spawn.SEAMS`` — a
registry this packet does not own. The handoff proposes that entry; until a
coordinator adds it, abandoning the wait is the bounded behaviour available
without editing the registry.

**Dead is not unreachable.** ``pane_facts`` answers ``None`` both for a pane that
is gone and for a server that could not be asked, and conflating those is what
once made a fleet-wide sweep read an unreachable socket as "every pane is gone".
So when a capture fails, this module asks ``TmuxServer.answers()`` — the one
question that separates the states — and reports ``gone`` only when the server
itself said the pane is absent, ``unknown`` when nothing answered.

**Team off is a preflight, not an empty queue.** ``hook_notification`` and its
four siblings return immediately when ``AISQUARE_TEAM`` is off, so with the
orchestrator disabled there are no attention transitions, no question text and no
board rows at all. That is reported as a condition on the collection, because a
queue that is empty because nothing is waiting and a queue that is empty because
nothing can ever arrive are different facts.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol, TypeVar

from aisquare.core import orchestrator
from aisquare.core.store import store_session
from aisquare.core.tmux import PaneFacts, TmuxError, TmuxServer
from aisquare.models import (
    FleetAgentStatus,
    ProjectInfo,
    TeamEvent,
    TeamSession,
    TeamTask,
)
from aisquare.office.models import (
    HookFact,
    LocalObservationBatch,
    PaneHealth,
    PaneObservation,
    PromptEvidence,
    ServiceError,
    ServiceErrorCode,
    TerminalPaneFacts,
)
from aisquare.office.ports import Clock, ObservationStore
from aisquare.office.providers import claude_observe

TEAM_OFF_DETAIL: Final = (
    "the orchestrator is disabled (AISQUARE_TEAM=0), so no hook records a session, "
    "an attention or a prompt: the queue is unavailable rather than empty"
)

ATTENTION_EVENT_KIND: Final = "attention"
"""The team event kind ``hook_notification`` emits on the transition into attention."""

ATTENTION_STATE: Final = "attention"


@dataclass(frozen=True, slots=True)
class CollectionLimits:
    """Every ceiling one collection observes. Starting points, not measured capacity."""

    budget_s: float = 0.5
    """The whole collection, matching ``OfficeConfig.poll_interval_ms``'s default."""

    pane_deadline_s: float = 0.15
    """How long one capture is waited on — see :func:`_bounded_call`.

    The wait ends; the tmux process is not killed. That is the bound this packet
    can enforce without owning a process-spawn site.
    """

    max_panes: int = 32
    """Panes captured in one pass; the rest are reported as skipped, never dropped."""

    max_tail_lines: int = 6
    """``Agent.output_tail``'s own bound."""

    max_line_chars: int = 500
    """One captured line, matching the sidecar's ``max_line_chars``."""

    max_screen_lines: int = 200
    """Lines of one frame kept for dialog decoding, matching ``max_pane_lines``."""

    max_projects: int = 32
    max_sessions: int = 200
    max_tasks: int = 200
    max_events: int = 50


@dataclass(frozen=True, slots=True)
class SourceFailure:
    """One source that did not answer, and what that cost.

    Carried beside the batch rather than inside it: P01's
    :class:`~aisquare.office.models.LocalObservationBatch` has a ``partial`` flag
    and no error field, and this packet may not add one to ``models.py``. See the
    handoff — the proposed amendment is a ``failures`` tuple on the batch.
    """

    source: str
    error: ServiceError


@dataclass(frozen=True, slots=True)
class CollectionReport:
    """What one collection cost and what it could not see."""

    collected_at: datetime
    elapsed_s: float
    team_enabled: bool
    budget_exceeded: bool
    panes_observed: int = 0
    partial: bool = False
    """Whether the OBSERVATION is known incomplete.

    Stored rather than derived from ``failures``, because the two legitimately
    disagree in one case: a batch that was collected in full and then failed to
    persist is a complete observation with a storage problem beside it. Deriving
    this would report holes in the office's view of the fleet that are not there.
    """
    failures: tuple[SourceFailure, ...] = ()
    skipped_panes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PaneSnapshot:
    """What one capture attempt learned, including how it failed.

    ``present`` and ``reachable`` are separate because the difference is the
    whole point: a pane that is positively absent from a server that answered is
    ``gone``, while a server that never answered leaves the pane ``unknown``.
    """

    lines: tuple[str, ...] = ()
    facts: TerminalPaneFacts | None = None
    present: bool = True
    reachable: bool = True
    detail: str | None = None


class BoardReader(Protocol):
    """Exactly the store surface this packet touches — five reads, no writes.

    Narrower than ``ContextStore`` on purpose. A collector typed against the
    whole store could call ``add_team_event`` or ``touch_session`` and nothing
    would object; typed against this, writing is not reachable, and the protocol
    doubles as the list of what a fake has to provide. ``ContextStore`` satisfies
    it structurally, so ``store_session`` remains the production factory.
    """

    def list_projects(self) -> list[ProjectInfo]: ...

    def team_sessions(self, project_id: str) -> list[TeamSession]: ...

    def team_tasks(self, project_id: str) -> list[TeamTask]: ...

    def recent_events(self, project_id: str, *, limit: int = 10) -> list[TeamEvent]: ...

    def latest_seq(self, project_id: str) -> int: ...


class PaneReader(Protocol):
    """The tmux surface this module needs: capture one pane, bounded."""

    def capture(self, socket: str, pane_id: str, *, deadline_s: float) -> PaneSnapshot:
        """One frame of the live screen, or a snapshot saying why there is none."""


_T = TypeVar("_T")


def _bounded_call(work: Callable[[], _T], deadline_s: float) -> _T | None:
    """Run ``work`` on a daemon thread and stop waiting after ``deadline_s``.

    ``None`` means the deadline passed with no answer. The thread is abandoned
    rather than killed — Python cannot interrupt a blocking read — so the tmux
    process behind it keeps running until its own timeout ends it. That is why
    the result is reported as ``unknown`` rather than as a dead pane: the
    collector stopped asking, which is not evidence about the pane.

    Daemon threads specifically, so a hung capture cannot hold the process open
    at exit. A pooled executor would join its workers on shutdown and turn one
    stuck tmux command into a 30-second wait on the way out.
    """
    result: list[_T] = []
    failure: list[Exception] = []

    def attempt() -> None:
        try:
            result.append(work())
        except Exception as exc:  # re-raised on the caller's thread below
            failure.append(exc)

    thread = threading.Thread(target=attempt, name="office-pane-capture", daemon=True)
    thread.start()
    thread.join(max(deadline_s, 0.0))
    if thread.is_alive():
        return None
    if failure:
        raise failure[0]
    return result[0] if result else None


def terminal_facts(facts: PaneFacts) -> TerminalPaneFacts:
    """``PaneFacts`` narrowed to what may travel toward a browser.

    The pane id, the foreground command and the title are dropped: a title and a
    command line are arbitrary text the process chose, and this value ends up
    inside a frame the office renders.
    """
    return TerminalPaneFacts(
        width=facts.width,
        height=facts.height,
        cursor_x=facts.cursor_x,
        cursor_y=facts.cursor_y,
        cursor_visible=facts.cursor_visible,
        alternate_on=facts.alternate_on,
        history_size=facts.history_size,
        dead=facts.dead,
        dead_status=facts.dead_status,
        in_mode=facts.in_mode,
    )


class TmuxPaneReader:
    """The real pane reader: one bounded capture, and a reachability probe on failure."""

    def __init__(
        self,
        *,
        limits: CollectionLimits | None = None,
        server_factory: Callable[[str], TmuxServer] | None = None,
    ) -> None:
        self._limits = limits or CollectionLimits()
        self._server_factory = server_factory or TmuxServer

    def capture(self, socket: str, pane_id: str, *, deadline_s: float) -> PaneSnapshot:
        server = self._server_factory(socket)
        try:
            frame = _bounded_call(lambda: server.capture(pane_id, scrollback=0), deadline_s)
        except TmuxError:
            # Only now, and only because nothing answered: the probe costs one
            # more command and is the sole way to tell a dead pane from a dead
            # server. The ordinary path never pays for it.
            reachable = self._answers(socket, deadline_s)
            return PaneSnapshot(
                present=False,
                reachable=reachable,
                detail="pane gone" if reachable else "tmux unavailable",
            )
        if frame is None:
            # The deadline passed, so nothing was learned about the pane. Not
            # "gone": we stopped asking before tmux answered.
            return PaneSnapshot(present=False, reachable=False, detail="pane deadline exceeded")
        lines = tuple(
            line[: self._limits.max_line_chars]
            for line in frame.lines[-self._limits.max_screen_lines :]
        )
        return PaneSnapshot(lines=lines, facts=terminal_facts(frame.facts))

    def _answers(self, socket: str, deadline_s: float) -> bool:
        server = self._server_factory(socket)
        try:
            return bool(_bounded_call(server.answers, deadline_s))
        except TmuxError:
            return False


class LocalCollector:
    """One bounded read of everything local, through public CLI APIs only.

    Every seam is injected — the store factory, the fleet listing, the pane
    reader, the clock, the orchestrator probe — so that a test states the machine
    it means instead of monkeypatching a private global, and so nothing here has
    to reach for ambient state to be exercised.
    """

    def __init__(
        self,
        clock: Clock,
        *,
        limits: CollectionLimits | None = None,
        store_factory: Callable[[], AbstractContextManager[BoardReader]] = store_session,
        fleet_lister: Callable[[ProjectInfo], Sequence[FleetAgentStatus]] | None = None,
        pane_reader: PaneReader | None = None,
        sidecar: ObservationStore | None = None,
        team_probe: Callable[[], bool] = orchestrator.team_enabled,
    ) -> None:
        self._clock = clock
        self._limits = limits or CollectionLimits()
        self._store_factory = store_factory
        self._fleet_lister = fleet_lister if fleet_lister is not None else _list_agents
        self._pane_reader = pane_reader or TmuxPaneReader(limits=self._limits)
        self._sidecar = sidecar
        self._team_probe = team_probe
        self._lifecycles = claude_observe.PromptLifecycles()
        self._report: CollectionReport | None = None

    @property
    def last_report(self) -> CollectionReport | None:
        """What the most recent collection cost, or ``None`` before the first."""
        return self._report

    def collect(self, now: datetime) -> LocalObservationBatch:
        """P01's ``LocalSource.collect``. Blocking; the caller owns the thread."""
        batch, _report = self.collect_with_report(now)
        return batch

    def collect_with_report(self, now: datetime) -> tuple[LocalObservationBatch, CollectionReport]:
        """The batch, plus the per-source detail the batch itself cannot carry."""
        started = self._clock.monotonic()
        deadline = started + self._limits.budget_s
        failures: list[SourceFailure] = []

        team_enabled = self._probe_team(failures)
        board = self._read_board(failures)
        fleet = self._read_fleet(board.projects, failures)
        panes, skipped = self._read_panes(fleet, deadline, now)
        prompts = self._decode_prompts(fleet, board, panes, now)
        hook_facts = self._hook_facts(board.sessions, now)

        # Fixed here, before the sidecar is offered the batch: `partial` is a
        # property of the observation, and a failed write does not make what was
        # observed any less complete.
        partial = bool(failures or skipped)
        batch = LocalObservationBatch(
            collected_at=now,
            board_seq=board.board_seq,
            projects=board.projects,
            sessions=board.sessions,
            tasks=board.tasks,
            events=board.events,
            fleet=fleet,
            # TurnMetric's token columns are never assigned by any code path, so
            # a turn read here would carry nulls dressed as usage. P10 sources
            # turns when it has real evidence to put in them.
            turns=(),
            panes=panes,
            prompts=prompts,
            hook_facts=hook_facts,
            partial=partial,
        )
        # Persist BEFORE the report is built, or a storage failure is appended to
        # a list the report has already copied and the office never hears about
        # it. Found by the test that asserts the failure is reported.
        self._persist(batch, failures)

        report = CollectionReport(
            collected_at=now,
            elapsed_s=self._clock.monotonic() - started,
            team_enabled=team_enabled,
            budget_exceeded=self._clock.monotonic() > deadline,
            panes_observed=len(panes),
            partial=partial,
            failures=tuple(failures),
            skipped_panes=tuple(skipped),
        )
        self._report = report
        return batch, report

    # -- phases ------------------------------------------------------------

    def _probe_team(self, failures: list[SourceFailure]) -> bool:
        try:
            enabled = self._team_probe()
        except Exception as exc:  # a probe may never cost the collection
            failures.append(_failure("team", "internal", type(exc).__name__))
            return False
        if not enabled:
            failures.append(
                SourceFailure(
                    "team",
                    ServiceError(
                        code="unsupported_capability", detail=TEAM_OFF_DETAIL, retryable=False
                    ),
                )
            )
        return enabled

    def _read_board(self, failures: list[SourceFailure]) -> _Board:
        """Projects, sessions, tasks and events, in one short store block.

        The block is closed before anything else runs. Everything read here is a
        value; no cursor, connection or live object escapes it.
        """
        try:
            with self._store_factory() as store:
                projects = tuple(store.list_projects()[: self._limits.max_projects])
                sessions: list[TeamSession] = []
                tasks: list[TeamTask] = []
                events: list[TeamEvent] = []
                board_seq = 0
                for project in projects:
                    sessions.extend(store.team_sessions(project.id))
                    tasks.extend(store.team_tasks(project.id))
                    events.extend(store.recent_events(project.id, limit=self._limits.max_events))
                    board_seq = max(board_seq, store.latest_seq(project.id))
        except Exception as exc:
            failures.append(_failure("cli.store", "service_unavailable", type(exc).__name__))
            return _Board()
        return _Board(
            projects=projects,
            sessions=tuple(sessions[: self._limits.max_sessions]),
            tasks=tuple(tasks[: self._limits.max_tasks]),
            events=tuple(events),
            board_seq=board_seq,
        )

    def _read_fleet(
        self, projects: Sequence[ProjectInfo], failures: list[SourceFailure]
    ) -> tuple[FleetAgentStatus, ...]:
        """Fleet rows with their derived state, per project, failing per project.

        One project whose socket is unreachable must not remove another's agents
        from the office, which is why the failure is recorded per project rather
        than aborting the phase.
        """
        rows: list[FleetAgentStatus] = []
        for project in projects:
            try:
                rows.extend(self._fleet_lister(project))
            except Exception as exc:
                failures.append(_failure("cli.fleet", "service_unavailable", type(exc).__name__))
        return tuple(rows)

    def _read_panes(
        self, fleet: Sequence[FleetAgentStatus], deadline: float, now: datetime
    ) -> tuple[tuple[PaneObservation, ...], list[str]]:
        """Capture the live panes, stopping at the budget and naming what was skipped."""
        panes: list[PaneObservation] = []
        skipped: list[str] = []
        for status in fleet:
            agent = status.agent
            if agent.ended_at is not None or not agent.pane_id:
                continue
            remaining = deadline - self._clock.monotonic()
            if len(panes) >= self._limits.max_panes or remaining <= 0:
                skipped.append(agent.id)
                continue
            snapshot = self._capture(agent.tmux_socket, agent.pane_id, remaining)
            panes.append(_observation(agent.id, snapshot, now, self._limits))
        return tuple(panes), skipped

    def _capture(self, socket: str, pane_id: str, remaining: float) -> PaneSnapshot:
        deadline_s = min(self._limits.pane_deadline_s, remaining)
        try:
            return self._pane_reader.capture(socket, pane_id, deadline_s=deadline_s)
        except Exception as exc:  # a reader may not take the collection with it
            return PaneSnapshot(present=False, reachable=False, detail=type(exc).__name__)

    def _decode_prompts(
        self,
        fleet: Sequence[FleetAgentStatus],
        board: _Board,
        panes: Sequence[PaneObservation],
        now: datetime,
    ) -> tuple[PromptEvidence, ...]:
        """What each agent is waiting on, from the frame first and the hook second.

        The frame is authoritative for anything structured, because no installed
        hook carries an option, a key, an order or a selection. The hook is
        authoritative for *that* an agent wants attention — and when the two
        disagree, the pane wins: an attention row whose pane shows no dialog is
        recorded ``stale`` rather than kept alive, because ``mark_attention``
        fires only on the transition and cannot notice that the prompt is gone.
        """
        captured = {pane.agent_id: pane for pane in panes}
        attention_text = _attention_text(board.events)
        evidence: list[PromptEvidence] = []
        joined: set[str] = set()

        for status in fleet:
            agent = status.agent
            if agent.session_id:
                joined.add(agent.session_id)
            pane = captured.get(agent.id)
            if pane is None or not pane.lines:
                continue
            in_attention = status.session is not None and status.session.state == ATTENTION_STATE
            dialog = claude_observe.classify_dialog(pane.lines)
            if dialog is None:
                self._lifecycles.observe(agent.id, None)
                if in_attention and status.session is not None:
                    evidence.append(
                        claude_observe.notification_evidence(
                            agent_id=agent.id,
                            text=attention_text.get(
                                status.session.id, claude_observe.DEFAULT_NOTIFICATION
                            ),
                            generation=max(1, self._lifecycles.generation_of(agent.id)),
                            observed_at=now,
                            stale=True,
                        )
                    )
                continue
            generation = self._lifecycles.observe(agent.id, dialog.fingerprint())
            if generation is None:  # unreachable: a dialog always has a fingerprint
                continue
            evidence.append(
                claude_observe.frame_evidence(
                    agent_id=agent.id,
                    dialog=dialog,
                    generation=generation,
                    observed_at=now,
                    detected_by="both" if in_attention else "frame",
                )
            )

        evidence.extend(self._self_origin_prompts(board, joined, attention_text, now))
        return tuple(evidence)

    def _self_origin_prompts(
        self,
        board: _Board,
        joined: set[str],
        attention_text: Mapping[str, str],
        now: datetime,
    ) -> list[PromptEvidence]:
        """Attention evidence for a session the fleet did not spawn.

        Origin is derived structurally, never from a payload field: ``pane_id``
        exists only on a fleet row, and a session with no fleet row therefore has
        no pane. (``SessionStart``'s ``source`` is read, forwarded and then never
        referenced by any function body, so it records nothing.) With no pane
        there is nothing to contradict the hook, so this evidence is not stale —
        and it carries no options, because the labels only ever existed on a
        screen this session does not have.
        """
        found: list[PromptEvidence] = []
        for session in board.sessions:
            if session.id in joined or session.ended_at is not None:
                continue
            if session.state != ATTENTION_STATE:
                self._lifecycles.observe(session.id, None)
                continue
            text = attention_text.get(session.id, claude_observe.DEFAULT_NOTIFICATION)
            generation = self._lifecycles.observe(session.id, text)
            found.append(
                claude_observe.notification_evidence(
                    agent_id=session.id,
                    text=text,
                    generation=generation if generation is not None else 1,
                    observed_at=now,
                )
            )
        return found

    def _hook_facts(self, sessions: Sequence[TeamSession], now: datetime) -> tuple[HookFact, ...]:
        """Bounded facts the hooks wrote, read back off the board row they wrote them to.

        The collector never sees a hook payload — a hook is a separate process —
        so what is available is what the hook persisted. The transcript is
        recorded as present or absent and never as a path, matching the sidecar,
        which asserts that the path bytes are absent from the database file.
        """
        facts: list[HookFact] = []
        for session in sessions:
            scalars: tuple[tuple[str, str | None], ...] = (
                ("state", session.state),
                ("model", session.model),
                ("effort", session.effort),
                ("transcript", "present" if session.transcript_path else None),
            )
            facts.extend(
                HookFact(session.id, name, value, now)
                for name, value in scalars
                if value is not None
            )
        return tuple(facts)

    def _persist(self, batch: LocalObservationBatch, failures: list[SourceFailure]) -> None:
        """Hand the batch to the sidecar when there is one, and never fail on it.

        Optional by design: collection is useful before P02's database exists and
        must keep working if it becomes unavailable. A storage failure is a
        recorded condition, not a lost observation — the batch is already built
        and the caller still gets it.
        """
        if self._sidecar is None:
            return
        try:
            self._sidecar.merge(batch)
        except Exception as exc:
            failures.append(_failure("office.sidecar", "service_unavailable", type(exc).__name__))


@dataclass(frozen=True, slots=True)
class _Board:
    """What one store block produced."""

    projects: tuple[ProjectInfo, ...] = ()
    sessions: tuple[TeamSession, ...] = ()
    tasks: tuple[TeamTask, ...] = ()
    events: tuple[TeamEvent, ...] = ()
    board_seq: int = 0


def _list_agents(project: ProjectInfo) -> Sequence[FleetAgentStatus]:
    """The default fleet listing, imported at call time.

    Lazily because ``services.fleet`` reaches for tmux, git and the harness at
    import, and a process that only wanted the collector's types should not pay
    for that — the same reason ``fleet`` itself imports ``services.team`` lazily.
    """
    from aisquare.services import fleet

    return fleet.list_agents(project, live_only=False)


def _attention_text(events: Sequence[TeamEvent]) -> dict[str, str]:
    """The latest attention message per session.

    This is the only place the notification's own text survives: the payload key
    is read once by the hook and stored as a feed event, never as a column. It is
    opaque free text and is used as text — never parsed for a dialog kind, which
    is the frame's job.
    """
    latest: dict[str, tuple[int, str]] = {}
    for event in events:
        if event.kind != ATTENTION_EVENT_KIND or not event.session_id:
            continue
        known = latest.get(event.session_id)
        # By sequence rather than by position: `recent_events` is free to return
        # newest-first or oldest-first, and a reader that assumed one of them
        # would show the wrong message on whichever ordering it did not expect.
        if known is None or event.seq >= known[0]:
            latest[event.session_id] = (event.seq, event.text)
    return {session_id: text for session_id, (_seq, text) in latest.items()}


def _observation(
    agent_id: str, snapshot: PaneSnapshot, now: datetime, limits: CollectionLimits
) -> PaneObservation:
    """One capture attempt as a fact, with health that keeps the distinctions.

    Four states, three of them failures that mean different things: ``dead`` is
    tmux reporting an exited process (with its status when known), ``gone`` is a
    server that answered and does not have the pane, and ``unknown`` is a server
    that could not be asked at all. Only ``live`` carries lines.
    """
    facts = snapshot.facts
    health: PaneHealth
    if snapshot.present and facts is not None and facts.dead:
        health, alive, exit_status = "dead", False, facts.dead_status
    elif snapshot.present:
        health, alive, exit_status = "live", True, None
    elif snapshot.reachable:
        health, alive, exit_status = "gone", False, None
    else:
        health, alive, exit_status = "unknown", False, None
    tail = tuple(
        claude_observe.strip_ansi(line).rstrip()
        for line in snapshot.lines[-limits.max_tail_lines :]
    )
    return PaneObservation(
        agent_id=agent_id,
        alive=alive,
        health=health,
        observed_at=now,
        lines=tail,
        facts=facts,
        exit_status=exit_status,
    )


def _failure(source: str, code: ServiceErrorCode, reason: str) -> SourceFailure:
    """A bounded failure that names the exception type and nothing else.

    Never the exception's message: a SQLite error carries the database path, and
    a path is exactly what must not travel toward a browser inside a detail
    string. The type is enough to act on; the rest belongs in a local diagnostic.
    """
    return SourceFailure(
        source=source,
        error=ServiceError(
            code=code,
            detail=f"{source} did not answer ({reason})",
            retryable=code != "unsupported_capability",
        ),
    )


__all__ = [
    "ATTENTION_EVENT_KIND",
    "TEAM_OFF_DETAIL",
    "CollectionLimits",
    "CollectionReport",
    "LocalCollector",
    "PaneReader",
    "PaneSnapshot",
    "SourceFailure",
    "TmuxPaneReader",
    "terminal_facts",
]
