"""One bounded collection, asserted one property at a time.

Nothing here starts tmux, launches an agent, opens the real store or reaches a
network: every seam the collector uses is injected, which is the point of
injecting them. The fakes are small on purpose — a port that needed a framework
to fake would be the wrong port.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aisquare.core.tmux import TmuxError
from aisquare.models import (
    FleetAgent,
    FleetAgentStatus,
    ProjectInfo,
    TeamEvent,
    TeamSession,
    TeamTask,
)
from aisquare.office.models import LocalObservationBatch, TerminalPaneFacts
from aisquare.office.observe import (
    TEAM_OFF_DETAIL,
    CollectionLimits,
    LocalCollector,
    PaneSnapshot,
    TmuxPaneReader,
    _observation,
)

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)

CURSOR = "\u276f"
"""The pane's selection marker, escaped rather than pasted so it stays legible."""

PERMISSION_FRAME = (
    "\x1b[1mBash command\x1b[0m",
    "",
    "make check",
    "",
    "Do you want to proceed?",
    f"{CURSOR} 1. Yes",
    "  2. Yes, and don't ask again for similar commands in ~/code/aisquare-cli",
    "  3. No, and tell Claude what to do differently (esc)",
)

IDLE_FRAME = ("● Ran 3 shell commands", f"{CURSOR} ", "  ⏵⏵ auto mode on · ← for agents")

FACTS = TerminalPaneFacts(
    width=120,
    height=40,
    cursor_x=2,
    cursor_y=39,
    cursor_visible=True,
    alternate_on=True,
    history_size=0,
    dead=False,
    dead_status=None,
    in_mode=False,
)

DEAD_FACTS = TerminalPaneFacts(
    width=120,
    height=40,
    cursor_x=0,
    cursor_y=0,
    cursor_visible=False,
    alternate_on=False,
    history_size=12,
    dead=True,
    dead_status=1,
    in_mode=False,
)


class FrozenClock:
    """Wall time a test sets, monotonic time a test advances. Nothing sleeps."""

    def __init__(self, now: datetime = NOW) -> None:
        self._now = now
        self._monotonic = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, seconds: float) -> None:
        self._monotonic += seconds


class FakeStore:
    """The five reads the collector makes — and every write, recorded not performed."""

    def __init__(
        self,
        *,
        projects: tuple[ProjectInfo, ...] = (),
        sessions: tuple[TeamSession, ...] = (),
        tasks: tuple[TeamTask, ...] = (),
        events: tuple[TeamEvent, ...] = (),
        seq: int = 0,
        broken: bool = False,
    ) -> None:
        self._projects = projects
        self._sessions = sessions
        self._tasks = tasks
        self._events = events
        self._seq = seq
        self._broken = broken
        self.writes: list[str] = []
        self.closed = False

    def list_projects(self) -> list[ProjectInfo]:
        if self._broken:
            raise RuntimeError("context.db is unreadable")
        return list(self._projects)

    def team_sessions(self, project_id: str) -> list[TeamSession]:
        return [s for s in self._sessions if s.project_id == project_id]

    def team_tasks(self, project_id: str) -> list[TeamTask]:
        return [t for t in self._tasks if t.project_id == project_id]

    def recent_events(self, project_id: str, *, limit: int = 10) -> list[TeamEvent]:
        return [e for e in self._events if e.project_id == project_id][:limit]

    def latest_seq(self, project_id: str) -> int:
        return self._seq

    # Writes. Recorded rather than raising, so the assertion is "nothing called
    # these" rather than "the collection happened to survive".
    def add_team_event(self, event: TeamEvent) -> TeamEvent:
        self.writes.append("add_team_event")
        return event

    def touch_session(self, *args: object, **kwargs: object) -> None:
        self.writes.append("touch_session")

    def mark_attention(self, session_id: str) -> bool:
        self.writes.append("mark_attention")
        return False

    def upsert_session(self, session: TeamSession) -> TeamSession:
        self.writes.append("upsert_session")
        return session


class FakePaneReader:
    """Answers per pane id, and can spend the budget while doing it."""

    def __init__(
        self,
        frames: dict[str, PaneSnapshot] | None = None,
        *,
        clock: FrozenClock | None = None,
        cost_s: float = 0.0,
    ) -> None:
        self._frames = frames or {}
        self._clock = clock
        self._cost = cost_s
        self.calls: list[tuple[str, str, float]] = []

    def capture(self, socket: str, pane_id: str, *, deadline_s: float) -> PaneSnapshot:
        self.calls.append((socket, pane_id, deadline_s))
        if self._clock is not None and self._cost:
            self._clock.advance(self._cost)
        return self._frames.get(pane_id, PaneSnapshot(lines=IDLE_FRAME, facts=FACTS))


def _project(project_id: str = "prj-1") -> ProjectInfo:
    return ProjectInfo(id=project_id, root=Path("/tmp/project"), codename="amber-otter")


def _session(
    session_id: str = "ses-1",
    *,
    state: str = "working",
    project_id: str = "prj-1",
    ended_at: datetime | None = None,
) -> TeamSession:
    return TeamSession(
        id=session_id,
        project_id=project_id,
        role="coder",
        started_at=NOW - timedelta(minutes=10),
        last_seen_at=NOW,
        ended_at=ended_at,
        state=state,
        model="claude-opus-5",
        transcript_path="/home/someone/.claude/projects/x.jsonl",
    )


def _agent(
    agent_id: str = "agt-1", *, pane_id: str = "%1", session_id: str | None = "ses-1"
) -> FleetAgent:
    return FleetAgent(
        id=agent_id,
        project_id="prj-1",
        label=agent_id,
        role="coder",
        pane_id=pane_id,
        session_id=session_id,
        cwd=Path("/tmp/project"),
        created_at=NOW - timedelta(minutes=10),
    )


def _status(agent: FleetAgent, session: TeamSession | None = None) -> FleetAgentStatus:
    return FleetAgentStatus(agent=agent, state="working", session=session)


def _task(task_id: str = "tsk-1") -> TeamTask:
    return TeamTask(
        id=task_id,
        project_id="prj-1",
        key=task_id,
        title="wire the collector",
        created_at=NOW,
        updated_at=NOW,
    )


def _collector(
    store: FakeStore,
    *,
    clock: FrozenClock | None = None,
    fleet: tuple[FleetAgentStatus, ...] = (),
    reader: FakePaneReader | None = None,
    limits: CollectionLimits | None = None,
    sidecar: object | None = None,
    team: bool = True,
) -> LocalCollector:
    @contextmanager
    def factory() -> Iterator[FakeStore]:
        yield store

    return LocalCollector(
        clock or FrozenClock(),
        limits=limits,
        store_factory=factory,
        fleet_lister=lambda _project: list(fleet),
        pane_reader=reader or FakePaneReader(),
        sidecar=sidecar,  # type: ignore[arg-type]
        team_probe=lambda: team,
    )


def test_a_seeded_board_keeps_every_identity() -> None:
    """Ids are carried through untouched: the CLI store stays authoritative."""
    session = _session()
    store = FakeStore(projects=(_project(),), sessions=(session,), tasks=(_task(),), seq=4471)
    collector = _collector(store, fleet=(_status(_agent(), session),))

    batch = collector.collect(NOW)

    assert [project.id for project in batch.projects] == ["prj-1"]
    assert [s.id for s in batch.sessions] == ["ses-1"]
    assert [t.id for t in batch.tasks] == ["tsk-1"]
    assert [row.agent.id for row in batch.fleet] == ["agt-1"]
    assert batch.board_seq == 4471
    assert batch.collected_at == NOW
    assert batch.partial is False


def test_a_seeded_batch_is_values_only_and_holds_no_store() -> None:
    """A batch crosses a thread boundary; a live handle inside it would too."""
    store = FakeStore(projects=(_project(),), sessions=(_session(),))

    batch = _collector(store).collect(NOW)

    assert isinstance(batch, LocalObservationBatch)
    assert not hasattr(batch, "store")
    assert isinstance(batch.projects, tuple)
    assert isinstance(batch.sessions, tuple)


def test_a_partial_collection_survives_a_broken_store() -> None:
    """A store that cannot be read costs the board, not the collection."""
    collector = _collector(FakeStore(broken=True))

    batch, report = collector.collect_with_report(NOW)

    assert batch.projects == ()
    assert batch.partial is True
    assert [failure.source for failure in report.failures] == ["cli.store"]
    assert report.failures[0].error.retryable is True


def test_a_partial_pane_failure_leaves_the_other_panes_intact() -> None:
    """One unreachable socket must not empty the office."""
    reader = FakePaneReader(
        {"%2": PaneSnapshot(present=False, reachable=False, detail="tmux unavailable")}
    )
    fleet = (
        _status(_agent("agt-1", pane_id="%1")),
        _status(_agent("agt-2", pane_id="%2", session_id="ses-2")),
    )
    collector = _collector(FakeStore(projects=(_project(),)), fleet=fleet, reader=reader)

    batch = collector.collect(NOW)
    health = {pane.agent_id: pane.health for pane in batch.panes}

    assert health == {"agt-1": "live", "agt-2": "unknown"}
    assert len(batch.panes) == 2


def test_a_dead_pane_is_distinguishable_from_an_unreachable_server() -> None:
    """The distinction a fleet-wide reap once lost: gone is not unknown."""
    reader = FakePaneReader(
        {
            "%1": PaneSnapshot(lines=IDLE_FRAME, facts=DEAD_FACTS),
            "%2": PaneSnapshot(present=False, reachable=True, detail="pane gone"),
            "%3": PaneSnapshot(present=False, reachable=False, detail="tmux unavailable"),
        }
    )
    fleet = (
        _status(_agent("agt-1", pane_id="%1")),
        _status(_agent("agt-2", pane_id="%2", session_id="ses-2")),
        _status(_agent("agt-3", pane_id="%3", session_id="ses-3")),
    )

    batch = _collector(FakeStore(projects=(_project(),)), fleet=fleet, reader=reader).collect(NOW)
    panes = {pane.agent_id: pane for pane in batch.panes}

    assert panes["agt-1"].health == "dead"
    assert panes["agt-1"].exit_status == 1
    assert panes["agt-2"].health == "gone"
    assert panes["agt-3"].health == "unknown"
    assert [pane.alive for pane in batch.panes] == [False, False, False]


def test_the_budget_stops_capturing_and_names_the_skipped_panes() -> None:
    """A slow pane costs the panes behind it, and says which ones."""
    clock = FrozenClock()
    reader = FakePaneReader(clock=clock, cost_s=0.4)
    fleet = tuple(
        _status(_agent(f"agt-{n}", pane_id=f"%{n}", session_id=f"ses-{n}")) for n in range(1, 5)
    )
    collector = _collector(
        FakeStore(projects=(_project(),)),
        clock=clock,
        fleet=fleet,
        reader=reader,
        limits=CollectionLimits(budget_s=0.5),
    )

    batch, report = collector.collect_with_report(NOW)

    assert len(reader.calls) == 2
    assert report.skipped_panes == ("agt-3", "agt-4")
    assert batch.partial is True
    assert report.budget_exceeded is True


def test_the_budget_gives_each_pane_its_own_deadline() -> None:
    """Never tmux's 30 s: that is sixty times the whole collection budget."""
    reader = FakePaneReader()
    collector = _collector(
        FakeStore(projects=(_project(),)),
        fleet=(_status(_agent()),),
        reader=reader,
        limits=CollectionLimits(pane_deadline_s=0.15),
    )

    collector.collect(NOW)

    assert reader.calls[0][2] <= 0.15


def test_the_budget_bounds_how_many_panes_one_pass_captures() -> None:
    fleet = tuple(
        _status(_agent(f"agt-{n}", pane_id=f"%{n}", session_id=f"ses-{n}")) for n in range(1, 6)
    )
    collector = _collector(
        FakeStore(projects=(_project(),)), fleet=fleet, limits=CollectionLimits(max_panes=2)
    )

    batch, report = collector.collect_with_report(NOW)

    assert len(batch.panes) == 2
    assert len(report.skipped_panes) == 3


def test_readonly_collection_writes_nothing_to_the_store() -> None:
    """Read-only is the whole contract: a poll the user never notices."""
    session = _session(state="attention")
    store = FakeStore(projects=(_project(),), sessions=(session,), tasks=(_task(),))
    collector = _collector(store, fleet=(_status(_agent(), session),))

    collector.collect(NOW)

    assert store.writes == []


def test_readonly_collection_never_sends_input_to_a_pane() -> None:
    """The reader is given one verb. There is nowhere for a keystroke to go."""
    reader = FakePaneReader()
    collector = _collector(
        FakeStore(projects=(_project(),)), fleet=(_status(_agent()),), reader=reader
    )

    collector.collect(NOW)

    assert all(len(call) == 3 for call in reader.calls)
    assert not hasattr(reader, "sent")


def test_team_off_is_a_preflight_condition_rather_than_an_empty_queue() -> None:
    """With the orchestrator off no hook records anything, ever."""
    collector = _collector(FakeStore(projects=(_project(),)), team=False)

    batch, report = collector.collect_with_report(NOW)

    assert report.team_enabled is False
    assert batch.partial is True
    failure = next(f for f in report.failures if f.source == "team")
    assert failure.error.code == "unsupported_capability"
    assert failure.error.retryable is False
    assert failure.error.detail == TEAM_OFF_DETAIL


def test_a_dialog_on_a_pane_becomes_evidence_detected_by_frame() -> None:
    """Option labels exist only on screen, so the evidence says frame."""
    reader = FakePaneReader({"%1": PaneSnapshot(lines=PERMISSION_FRAME, facts=FACTS)})
    collector = _collector(
        FakeStore(projects=(_project(),)), fleet=(_status(_agent()),), reader=reader
    )

    batch = collector.collect(NOW)

    assert len(batch.prompts) == 1
    evidence = batch.prompts[0]
    assert evidence.agent_id == "agt-1"
    assert evidence.kind == "permission"
    assert evidence.detected_by == "frame"
    assert [option.key for option in evidence.options] == ["allow", "allow-remember", "deny"]


def test_a_dialog_confirmed_by_the_attention_hook_is_detected_by_both() -> None:
    session = _session(state="attention")
    reader = FakePaneReader({"%1": PaneSnapshot(lines=PERMISSION_FRAME, facts=FACTS)})
    collector = _collector(
        FakeStore(projects=(_project(),), sessions=(session,)),
        fleet=(_status(_agent(), session),),
        reader=reader,
    )

    batch = collector.collect(NOW)

    assert batch.prompts[0].detected_by == "both"


def test_stale_attention_cannot_resurrect_a_prompt_the_pane_has_cleared() -> None:
    """``mark_attention`` fires only on the transition, so the row outlives the dialog."""
    session = _session(state="attention")
    reader = FakePaneReader({"%1": PaneSnapshot(lines=IDLE_FRAME, facts=FACTS)})
    events = (
        TeamEvent(
            seq=9,
            id="evt-1",
            project_id="prj-1",
            session_id="ses-1",
            kind="attention",
            text="Claude is waiting for your input",
            created_at=NOW,
        ),
    )
    collector = _collector(
        FakeStore(projects=(_project(),), sessions=(session,), events=events),
        fleet=(_status(_agent(), session),),
        reader=reader,
    )

    batch = collector.collect(NOW)

    assert len(batch.prompts) == 1
    assert batch.prompts[0].stale is True
    assert batch.prompts[0].options == ()
    assert batch.prompts[0].detected_by == "hook"


def test_a_self_origin_session_is_derived_structurally_and_carries_no_options() -> None:
    """Origin is "has no fleet row", never a payload field — `source` records nothing."""
    session = _session("ses-solo", state="attention")
    events = (
        TeamEvent(
            seq=4,
            id="evt-2",
            project_id="prj-1",
            session_id="ses-solo",
            kind="attention",
            text="needs your attention",
            created_at=NOW,
        ),
    )
    collector = _collector(
        FakeStore(projects=(_project(),), sessions=(session,), events=events), fleet=()
    )

    batch = collector.collect(NOW)

    assert [evidence.agent_id for evidence in batch.prompts] == ["ses-solo"]
    assert batch.prompts[0].options == ()
    assert batch.prompts[0].detected_by == "hook"
    assert batch.prompts[0].stale is False


def test_hook_facts_come_off_the_board_row_and_carry_no_transcript_path() -> None:
    """The sidecar refuses to store the path; nothing may hand it one."""
    store = FakeStore(projects=(_project(),), sessions=(_session(),))

    batch = _collector(store).collect(NOW)
    values = {fact.name: fact.value for fact in batch.hook_facts}

    assert values["state"] == "working"
    assert values["model"] == "claude-opus-5"
    assert values["transcript"] == "present"
    assert ".jsonl" not in str(values)


def test_the_pane_tail_is_bounded_and_stripped_of_escapes() -> None:
    """``output_tail`` is six lines, and colour is not part of a board row."""
    noisy = tuple(f"\x1b[31mline {n}\x1b[0m" for n in range(20))
    reader = FakePaneReader({"%1": PaneSnapshot(lines=noisy, facts=FACTS)})
    collector = _collector(
        FakeStore(projects=(_project(),)),
        fleet=(_status(_agent()),),
        reader=reader,
        limits=CollectionLimits(max_tail_lines=6),
    )

    batch = collector.collect(NOW)

    assert len(batch.panes[0].lines) == 6
    assert batch.panes[0].lines[-1] == "line 19"
    assert "\x1b" not in "".join(batch.panes[0].lines)


def test_an_ended_agent_is_not_captured_at_all() -> None:
    """A row that has ended has no pane to read; asking would be a wasted command."""
    ended = _agent().model_copy(update={"ended_at": NOW})
    reader = FakePaneReader()
    collector = _collector(
        FakeStore(projects=(_project(),)), fleet=(_status(ended),), reader=reader
    )

    batch = collector.collect(NOW)

    assert reader.calls == []
    assert batch.panes == ()


def test_a_sidecar_failure_is_recorded_and_never_raised() -> None:
    """Collection is useful without the sidecar and must survive losing it."""

    class BrokenSidecar:
        def merge(self, batch: LocalObservationBatch) -> None:
            raise RuntimeError("database is locked")

    collector = _collector(FakeStore(projects=(_project(),)), sidecar=BrokenSidecar())

    batch, report = collector.collect_with_report(NOW)

    assert batch.projects != ()
    assert [failure.source for failure in report.failures] == ["office.sidecar"]
    # The observation itself is complete: only the write failed, and calling the
    # batch partial would report holes in the fleet view that do not exist.
    assert batch.partial is False


def test_the_sidecar_receives_the_batch_when_there_is_one() -> None:
    class RecordingSidecar:
        def __init__(self) -> None:
            self.merged: list[LocalObservationBatch] = []

        def merge(self, batch: LocalObservationBatch) -> None:
            self.merged.append(batch)

    sidecar = RecordingSidecar()
    collector = _collector(FakeStore(projects=(_project(),)), sidecar=sidecar)

    batch = collector.collect(NOW)

    assert sidecar.merged == [batch]


def test_a_failure_detail_names_the_type_and_never_the_exception_text() -> None:
    """A SQLite message carries the database path; a detail string must not."""
    collector = _collector(FakeStore(broken=True))

    _batch, report = collector.collect_with_report(NOW)

    assert "context.db" not in report.failures[0].error.detail
    assert "RuntimeError" in report.failures[0].error.detail


class BlockingServer:
    """A tmux server that does not answer before the deadline."""

    def __init__(self) -> None:
        self.released = threading.Event()
        self.probed = False

    def capture(self, pane_id: str, *, scrollback: int = 0) -> object:
        self.released.wait(5.0)
        raise TmuxError("abandoned; the collector stopped waiting")

    def answers(self) -> bool:
        self.probed = True
        return True


def test_a_pane_that_misses_its_deadline_is_unknown_and_not_gone() -> None:
    """Stopping the wait says nothing about the pane, so it must not say `gone`.

    The reachability probe is deliberately not run either: the server never
    answered the first question, and asking a second one inside a budget that
    has already elapsed spends time to learn nothing.
    """
    server = BlockingServer()
    reader = TmuxPaneReader(server_factory=lambda _socket: server)  # type: ignore[arg-type,return-value]

    try:
        snapshot = reader.capture("asq", "%1", deadline_s=0.01)
    finally:
        server.released.set()

    assert snapshot.present is False
    assert snapshot.reachable is False
    assert snapshot.detail == "pane deadline exceeded"
    assert server.probed is False

    observation = _observation("agt-1", snapshot, NOW, CollectionLimits())
    assert observation.health == "unknown"


def test_the_report_records_what_the_collection_cost() -> None:
    clock = FrozenClock()
    reader = FakePaneReader(clock=clock, cost_s=0.05)
    collector = _collector(
        FakeStore(projects=(_project(),)),
        clock=clock,
        fleet=(_status(_agent()),),
        reader=reader,
    )

    _batch, report = collector.collect_with_report(NOW)

    assert report.elapsed_s >= 0.05
    assert report.panes_observed == 1
    assert collector.last_report is report
