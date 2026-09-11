"""What the coordinator promises, asserted against fake executors and real time.

The executors here are deliberately crude — an event to block on, a list of
calls, an outcome chosen by the test. That is the point of the port: if proving
"the executor was called exactly once" needed a framework, the seam would be
wrong.

Nothing in this file spawns an agent, touches tmux, opens a socket or reads a
real home. The coordinator's whole job is ordering and truthfulness around an
injected call, and both are observable without the call doing anything.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aisquare.office.config import OfficeConfig
from aisquare.office.models import (
    ActionContext,
    ActionKind,
    ActionSpec,
    Operation,
    OperationTarget,
    Receipt,
    ServiceError,
    TellRequest,
)
from aisquare.office.operation_store import (
    OPERATION_TABLE,
    IdempotencyConflict,
    OperationError,
    OperationJournal,
    OperationRecord,
    Reconciliation,
)
from aisquare.office.operations import (
    ActionCoordinator,
    ActionExecutor,
    ActionFailed,
    ActionUncertain,
    ContractRevisionRequired,
    ExecutionOutcome,
    InlineWorkerPool,
    LegacyActionFailed,
    ThreadWorkerPool,
    UnsupportedAction,
    WorkerPool,
)
from aisquare.office.ports import OperationCoordinator
from aisquare.office.storage import ObservationDatabase

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


class FrozenClock:
    def now(self) -> datetime:
        return NOW

    def monotonic(self) -> float:
        return 0.0


class FakeExecutor:
    """Records every call and returns whatever the test told it to."""

    def __init__(
        self,
        outcome: ExecutionOutcome | None = None,
        *,
        raises: Exception | None = None,
        version: str = "fake-1",
    ) -> None:
        self._outcome = outcome or ExecutionOutcome.succeeded(
            Receipt(delivered="typed", detail="sent", agent_id="agt-1")
        )
        self._raises = raises
        self._version = version
        self.calls: list[ActionSpec] = []
        self.lock = threading.Lock()

    @property
    def version(self) -> str:
        return self._version

    def execute(self, spec: ActionSpec, context: ActionContext) -> ExecutionOutcome:
        with self.lock:
            self.calls.append(spec)
        if self._raises is not None:
            raise self._raises
        return self._outcome


class BlockingExecutor:
    """Signals that it started, then waits until the test lets it finish."""

    def __init__(self, *, version: str = "blocking-1") -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self._version = version

    @property
    def version(self) -> str:
        return self._version

    def execute(self, spec: ActionSpec, context: ActionContext) -> ExecutionOutcome:
        self.calls += 1
        self.started.set()
        self.release.wait(timeout=10)
        return ExecutionOutcome.succeeded(
            Receipt(delivered="typed", detail="eventually sent", agent_id="agt-1")
        )


class IntervalExecutor:
    """Appends ``(marker, "in")`` and ``(marker, "out")`` around a short wait."""

    def __init__(self, log: list[tuple[str, str]], *, hold_s: float = 0.05) -> None:
        self.log = log
        self.hold_s = hold_s
        self.lock = threading.Lock()

    @property
    def version(self) -> str:
        return "interval-1"

    def execute(self, spec: ActionSpec, context: ActionContext) -> ExecutionOutcome:
        marker = spec.target.agent_id or "none"
        with self.lock:
            self.log.append((marker, "in"))
        time_to_hold = self.hold_s
        threading.Event().wait(time_to_hold)
        with self.lock:
            self.log.append((marker, "out"))
        return ExecutionOutcome.succeeded(
            Receipt(delivered="typed", detail="sent", agent_id=marker)
        )


class BarrierExecutor:
    """Only completes if another execution is in flight at the same time."""

    def __init__(self, parties: int = 2) -> None:
        self.barrier = threading.Barrier(parties)
        self.overlapped = False

    @property
    def version(self) -> str:
        return "barrier-1"

    def execute(self, spec: ActionSpec, context: ActionContext) -> ExecutionOutcome:
        self.barrier.wait(timeout=5)
        self.overlapped = True
        return ExecutionOutcome.succeeded(
            Receipt(delivered="typed", detail="sent", agent_id=spec.target.agent_id)
        )


def _journal(home: Path) -> OperationJournal:
    database = ObservationDatabase.from_config(OfficeConfig(home=home), FrozenClock())
    return OperationJournal(database, FrozenClock())


def _coordinator(
    journal: OperationJournal,
    executors: dict[ActionKind, ActionExecutor],
    *,
    pool: WorkerPool | None = None,
    ack_s: float = 5.0,
    reconciler: object = None,
) -> ActionCoordinator:
    return ActionCoordinator(
        journal,
        FrozenClock(),
        executors=executors,
        pool=pool if pool is not None else InlineWorkerPool(),
        journal_pool=InlineWorkerPool(),
        ack_s=ack_s,
        reconciler=reconciler,  # type: ignore[arg-type]
    )


def _context(
    *,
    scope: str = "local:session-a",
    key: str | None = "idem-1",
    revision: str | None = "1.4",
) -> ActionContext:
    return ActionContext(
        auth_scope=scope,
        received_at=NOW,
        contract_revision="1.4" if revision == "1.4" else None,
        request_id="req-1",
        idempotency_key=key,
    )


def _spec(
    kind: ActionKind = "agent.tell",
    *,
    agent_id: str | None = "agt-1",
    text: str = "status please",
) -> ActionSpec:
    return ActionSpec(
        kind=kind,
        target=OperationTarget(agent_id=agent_id),
        body=TellRequest(text=text),
    )


def _statuses(path: Path) -> list[str]:
    """Every journalled status, or none at all when the sidecar was never opened.

    A submission refused before it reserves anything does not create the
    database file, so an absent file IS the assertion these tests want: nothing
    was written, because nothing was even started.
    """
    if not path.exists():
        return []
    connection = sqlite3.connect(str(path))
    try:
        rows = connection.execute(f"SELECT status FROM {OPERATION_TABLE}").fetchall()
    finally:
        connection.close()
    return [str(row[0]) for row in rows]


# -- the two answers -------------------------------------------------------


def test_a_completed_short_action_answers_with_its_receipt(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "home")
    executor = FakeExecutor()
    coordinator = _coordinator(journal, {"agent.tell": executor})

    answer = asyncio.run(coordinator.submit(_spec(), _context()))

    assert isinstance(answer, Receipt), "a finished short action is a 200 Receipt"
    assert answer.detail == "sent"
    assert _statuses(journal.database_path) == ["succeeded"]


def test_work_that_has_not_finished_answers_with_an_operation(tmp_path: Path) -> None:
    """202 with a journal id, rather than holding the request open."""
    journal = _journal(tmp_path / "home")
    executor = BlockingExecutor()
    pool = ThreadWorkerPool(2)
    coordinator = _coordinator(journal, {"agent.tell": executor}, pool=pool, ack_s=0.05)

    async def drive() -> Operation | Receipt:
        answer = await coordinator.submit(_spec(), _context())
        executor.release.set()
        await coordinator.aclose()
        return answer

    answer = asyncio.run(drive())

    assert isinstance(answer, Operation), "unfinished work is a 202 Operation"
    assert answer.status in ("queued", "running")
    assert answer.receipt is None
    assert answer.kind == "agent.tell"


def test_the_record_is_committed_before_the_executor_is_reached(tmp_path: Path) -> None:
    """The ordering the whole packet exists for, observed from inside the call."""
    journal = _journal(tmp_path / "home")
    seen: list[list[str]] = []

    class Observing:
        @property
        def version(self) -> str:
            return "observing-1"

        def execute(self, spec: ActionSpec, context: ActionContext) -> ExecutionOutcome:
            seen.append(_statuses(journal.database_path))
            return ExecutionOutcome.succeeded(Receipt(delivered="typed", detail="sent"))

    coordinator = _coordinator(journal, {"agent.tell": Observing()})

    asyncio.run(coordinator.submit(_spec(), _context()))

    assert seen == [["running"]], "the executor must never run ahead of its own record"


# -- idempotency -----------------------------------------------------------


def test_an_identical_retry_returns_the_first_result_and_executes_once(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "home")
    executor = FakeExecutor()
    coordinator = _coordinator(journal, {"agent.tell": executor})

    async def drive() -> tuple[Receipt | Operation, Receipt | Operation]:
        first = await coordinator.submit(_spec(), _context())
        second = await coordinator.submit(_spec(), _context())
        return first, second

    first, second = asyncio.run(drive())

    assert isinstance(first, Receipt)
    assert isinstance(second, Receipt)
    assert second == first
    assert len(executor.calls) == 1, "a repeat must not do the thing twice"
    assert len(_statuses(journal.database_path)) == 1


@pytest.mark.parametrize(
    ("what", "spec"),
    [
        ("body", _spec(text="something else")),
        ("kind", _spec("agent.interrupt")),
        ("target", _spec(agent_id="agt-9")),
    ],
)
def test_a_conflicting_reuse_is_refused_with_no_executor_call(
    tmp_path: Path, what: str, spec: ActionSpec
) -> None:
    journal = _journal(tmp_path / "home")
    executor = FakeExecutor()
    coordinator = _coordinator(journal, {"agent.tell": executor, "agent.interrupt": executor})

    async def drive() -> None:
        await coordinator.submit(_spec(), _context())
        await coordinator.submit(spec, _context())

    with pytest.raises(IdempotencyConflict):
        asyncio.run(drive())

    assert len(executor.calls) == 1, f"a changed {what} must not reach the executor"
    assert coordinator.metrics.conflicts == 1


# -- failure and uncertainty ----------------------------------------------


def test_a_known_domain_failure_is_recorded_as_failed(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "home")
    error = ServiceError(code="upstream_error", detail="the agent refused", retryable=True)
    coordinator = _coordinator(journal, {"agent.tell": FakeExecutor(raises=ActionFailed(error))})

    answer = asyncio.run(coordinator.submit(_spec(), _context()))

    assert isinstance(answer, Operation)
    assert answer.status == "failed"
    assert answer.error is not None
    assert answer.error.code == "upstream_error"
    assert answer.error.retryable is True
    assert coordinator.metrics.failed == 1


def test_an_uncertain_outcome_is_not_success_and_is_never_retried(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "home")
    executor = FakeExecutor(raises=ActionUncertain("the keys may have landed; tmux never answered"))
    coordinator = _coordinator(journal, {"agent.tell": executor})

    async def drive() -> tuple[Receipt | Operation, Receipt | Operation]:
        first = await coordinator.submit(_spec(), _context())
        second = await coordinator.submit(_spec(), _context())
        return first, second

    first, second = asyncio.run(drive())

    assert isinstance(first, Operation)
    assert first.status == "outcome_unknown"
    assert first.receipt is None, "an unknown outcome must not invent a delivery"
    assert first.error is not None
    assert first.error.retryable is False
    assert isinstance(second, Operation)
    assert second.operation_id == first.operation_id
    assert len(executor.calls) == 1, "an uncertain mutation is never replayed automatically"


def test_an_unrecognised_exception_is_unknown_rather_than_failed(tmp_path: Path) -> None:
    """'Failed' claims nothing happened; a traceback is not evidence of that."""
    journal = _journal(tmp_path / "home")
    coordinator = _coordinator(
        journal, {"agent.tell": FakeExecutor(raises=RuntimeError("tmux went away"))}
    )

    answer = asyncio.run(coordinator.submit(_spec(), _context()))

    assert isinstance(answer, Operation)
    assert answer.status == "outcome_unknown"
    assert answer.error is not None
    assert "RuntimeError" in answer.error.detail
    assert "tmux went away" not in answer.error.detail, "no raw exception text on the wire"


# -- cancellation ----------------------------------------------------------


def test_request_cancellation_keeps_the_record_and_the_lock(tmp_path: Path) -> None:
    """The browser navigating away must not orphan a mutation that is under way.

    Two properties at once, because they fail together: the journal row stays
    ``running`` rather than disappearing, and the target stays locked so the next
    action for that agent cannot interleave with keys still going out.
    """
    journal = _journal(tmp_path / "home")
    executor = BlockingExecutor()
    pool = ThreadWorkerPool(2)
    coordinator = _coordinator(journal, {"agent.tell": executor}, pool=pool, ack_s=30.0)

    async def drive() -> tuple[list[str], tuple[str, ...], list[str], tuple[str, ...]]:
        task = asyncio.create_task(coordinator.submit(_spec(), _context()))
        await asyncio.to_thread(executor.started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        during_status = _statuses(journal.database_path)
        during_locks = journal.held_targets()

        executor.release.set()
        for _ in range(500):
            if _statuses(journal.database_path) == ["succeeded"]:
                break
            await asyncio.sleep(0.01)
        after_status = _statuses(journal.database_path)
        after_locks = journal.held_targets()
        await coordinator.aclose()
        return during_status, during_locks, after_status, after_locks

    during_status, during_locks, after_status, after_locks = asyncio.run(drive())

    assert during_status == ["running"], "a cancelled request must not discard the record"
    assert during_locks == ("\x1fagt-1",), "the target lock must outlive the request"
    assert after_status == ["succeeded"], "the work itself continues to completion"
    assert after_locks == (), "and the lock is released after the terminal transition"
    assert executor.calls == 1


# -- serialisation ---------------------------------------------------------


def test_two_operations_on_one_target_never_overlap(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "home")
    log: list[tuple[str, str]] = []
    pool = ThreadWorkerPool(4)
    coordinator = _coordinator(journal, {"agent.tell": IntervalExecutor(log)}, pool=pool)

    async def drive() -> None:
        await asyncio.gather(
            coordinator.submit(_spec(text="first"), _context(key="k-1")),
            coordinator.submit(_spec(text="second"), _context(key="k-2")),
        )
        await coordinator.aclose()

    asyncio.run(drive())

    assert [phase for _marker, phase in log] == ["in", "out", "in", "out"], (
        "same-target work must queue behind the lock, not interleave"
    )
    assert len(log) == 4


def test_two_different_targets_can_run_at_the_same_time(tmp_path: Path) -> None:
    """The barrier only clears if both executions are genuinely in flight."""
    journal = _journal(tmp_path / "home")
    executor = BarrierExecutor()
    pool = ThreadWorkerPool(2)
    coordinator = _coordinator(journal, {"agent.tell": executor}, pool=pool)

    async def drive() -> tuple[Receipt | Operation, ...]:
        answers = await asyncio.gather(
            coordinator.submit(_spec(agent_id="agt-1"), _context(key="k-1")),
            coordinator.submit(_spec(agent_id="agt-2"), _context(key="k-2")),
        )
        await coordinator.aclose()
        return answers

    answers = asyncio.run(drive())

    assert executor.overlapped is True
    assert all(isinstance(answer, Receipt) for answer in answers)


# -- refusals before anything happens --------------------------------------


def test_a_kind_with_no_executor_is_refused_before_the_journal_is_touched(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path / "home")
    coordinator = _coordinator(journal, {})

    with pytest.raises(UnsupportedAction) as caught:
        asyncio.run(coordinator.submit(_spec(), _context()))

    assert caught.value.error.code == "unsupported_capability"
    assert _statuses(journal.database_path) == []


def test_a_changed_mutation_without_the_contract_revision_is_refused(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "home")
    executor = FakeExecutor()
    coordinator = _coordinator(journal, {"agent.tell": executor})

    with pytest.raises(ContractRevisionRequired):
        asyncio.run(coordinator.submit(_spec(), _context(revision=None)))

    assert executor.calls == []
    assert _statuses(journal.database_path) == [], "refused before any side effect"


def test_a_mutation_without_a_resolved_key_is_refused(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "home")
    executor = FakeExecutor()
    coordinator = _coordinator(journal, {"agent.tell": executor})

    with pytest.raises(OperationError, match="resolved idempotency key"):
        asyncio.run(coordinator.submit(_spec(), _context(key=None)))

    assert executor.calls == []
    assert _statuses(journal.database_path) == []


def test_a_second_executor_for_one_kind_is_refused(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "home")
    coordinator = _coordinator(journal, {"agent.tell": FakeExecutor()})

    with pytest.raises(OperationError, match="already registered"):
        coordinator.register("agent.tell", FakeExecutor())


def test_an_unknown_kind_cannot_be_registered(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "home")
    coordinator = _coordinator(journal, {})

    with pytest.raises(OperationError, match="unknown action kind"):
        coordinator.register("agent.teleport", FakeExecutor())  # type: ignore[arg-type]


# -- retained legacy mutations ---------------------------------------------


def test_a_retained_legacy_mutation_answers_a_receipt_and_no_operation(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path / "home")
    executor = FakeExecutor()
    coordinator = _coordinator(journal, {"agent.note": executor})
    spec = ActionSpec(
        kind="agent.note", target=OperationTarget(agent_id="agt-1"), body=TellRequest(text="noted")
    )

    answer = asyncio.run(coordinator.submit(spec, _context(revision=None)))

    assert isinstance(answer, Receipt), "an unchanged legacy route has no 202 to give"
    assert _statuses(journal.database_path) == ["succeeded"], "it is still journalled"
    assert len(executor.calls) == 1


def test_a_failed_legacy_mutation_raises_rather_than_inventing_an_operation(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path / "home")
    error = ServiceError(code="upstream_error", detail="the board refused", retryable=False)
    coordinator = _coordinator(journal, {"agent.note": FakeExecutor(raises=ActionFailed(error))})
    spec = ActionSpec(kind="agent.note", target=OperationTarget(agent_id="agt-1"))

    with pytest.raises(LegacyActionFailed) as caught:
        asyncio.run(coordinator.submit(spec, _context(revision=None)))

    assert caught.value.error.code == "upstream_error"
    assert _statuses(journal.database_path) == ["failed"]


# -- status reads ----------------------------------------------------------


def test_a_status_read_touches_the_journal_and_never_the_executor(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "home")
    executor = FakeExecutor()
    coordinator = _coordinator(journal, {"agent.tell": executor})

    async def drive() -> tuple[Operation | None, Operation | None]:
        await coordinator.submit(_spec(), _context())
        record = journal.record(_statuses_ids(journal.database_path)[0], "local:session-a")
        assert record is not None
        mine = await coordinator.get(record.operation_id, _context())
        theirs = await coordinator.get(record.operation_id, _context(scope="local:session-b"))
        return mine, theirs

    mine, theirs = asyncio.run(drive())

    assert mine is not None
    assert mine.status == "succeeded"
    assert theirs is None, "another scope gets not-found, not a detail about the operation"
    assert len(executor.calls) == 1, "polling must not be a way to cause side effects"


def _statuses_ids(path: Path) -> list[str]:
    connection = sqlite3.connect(str(path))
    try:
        rows = connection.execute(f"SELECT operation_id FROM {OPERATION_TABLE}").fetchall()
    finally:
        connection.close()
    return [str(row[0]) for row in rows]


# -- restart ---------------------------------------------------------------


def test_start_settles_what_the_last_process_left_without_calling_an_executor(
    tmp_path: Path,
) -> None:
    journal = _journal(tmp_path / "home")
    reservation = journal.reserve(_spec(), _context(), executor_version="fake-1")
    assert reservation.record is not None
    journal.apply_transition(reservation.record.operation_id, "queued", "running")
    executor = FakeExecutor()
    coordinator = _coordinator(journal, {"agent.tell": executor})

    settled = asyncio.run(coordinator.start())

    assert [record.status for record in settled] == ["outcome_unknown"]
    assert settled[0].reconciled_source == "restart"
    assert executor.calls == [], "a restart must never repeat a side effect"
    assert coordinator.metrics.reconciled == 1


def test_a_reconciler_may_settle_a_row_it_can_actually_prove(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "home")
    reservation = journal.reserve(_spec(), _context())
    assert reservation.record is not None
    journal.apply_transition(reservation.record.operation_id, "queued", "running")

    def hook(record: OperationRecord) -> Reconciliation:
        return Reconciliation(
            status="succeeded",
            source="hook evidence",
            receipt=Receipt(delivered="typed", detail="confirmed later"),
        )

    coordinator = _coordinator(journal, {"agent.tell": FakeExecutor()}, reconciler=hook)
    settled = asyncio.run(coordinator.start())

    assert [record.status for record in settled] == ["succeeded"]
    assert settled[0].reconciled_source == "hook evidence"


# -- metrics and the port --------------------------------------------------


def test_each_outcome_is_counted_separately(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "home")
    good = FakeExecutor()
    bad = FakeExecutor(
        raises=ActionFailed(ServiceError(code="internal", detail="no", retryable=False))
    )
    unsure = FakeExecutor(raises=ActionUncertain("maybe"))
    coordinator = _coordinator(
        journal,
        {"agent.tell": good, "agent.stop": bad, "agent.interrupt": unsure},
    )

    async def drive() -> None:
        await coordinator.submit(_spec(), _context(key="k-1"))
        await coordinator.submit(_spec("agent.stop"), _context(key="k-2"))
        await coordinator.submit(_spec("agent.interrupt"), _context(key="k-3"))
        with pytest.raises(UnsupportedAction):
            await coordinator.submit(_spec("agent.spawn"), _context(key="k-4"))

    asyncio.run(drive())

    metrics = coordinator.metrics
    assert (metrics.succeeded, metrics.failed, metrics.unknown) == (1, 1, 1)
    assert metrics.reserved == 3
    assert metrics.executed == 3
    assert metrics.conflicts == 0


def test_the_coordinator_satisfies_the_p01_port(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "home")
    built = _coordinator(journal, {"agent.tell": FakeExecutor()})
    coordinator: OperationCoordinator = built

    async def drive() -> tuple[Receipt | Operation, Operation | None]:
        answer = await coordinator.submit(_spec(), _context())
        missing = await coordinator.get("op_nothing", _context())
        return answer, missing

    answer, missing = asyncio.run(drive())

    assert isinstance(answer, Receipt)
    assert missing is None
