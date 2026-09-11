"""The coordinator: one validated :class:`ActionSpec` in, one truthful answer out.

Four properties are load-bearing, and each has a test with its name on it.

**Reserve, then act.** Every mutation commits a journal row before an executor
is called. A conflicting idempotency key is refused here, with the executor
never invoked, so a 409 cannot be a side effect that already happened.

**Execution outlives the request.** The work runs in a task the HTTP handler
does not own and cannot cancel. A browser that navigates away mid-keystroke
leaves a half-typed pane; it must not also leave no record of one, and it must
not release the target lock early so that the next action interleaves with the
keys still going out.

**Same target, one at a time.** Operations queue behind a per-target lock and
release it in ``finally``, *after* the terminal transition. Different targets
run concurrently up to the worker bound.

**Unknown is an answer.** An executor that cannot prove delivery produces
``outcome_unknown``: not success, not failure, and never retried automatically.
Nothing in this module ever calls an executor twice for one operation — not on
restart, not on a repeated request, not on a timeout.

Consumers supply an :class:`ActionExecutor` per kind. P07 brings fleet, task and
feedback effects; P09 brings prompt and terminal input with its own post-send
uncertainty classification; P14 brings the platform teach submission. None of
them creates a second coordinator, and none of them can hand a browser-supplied
callback to this one: :class:`~aisquare.office.models.ActionSpec` carries a
finite kind, a resolved target and a validated model, and that is the whole
argument surface.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Literal, Protocol, TypeVar

from aisquare.office.models import (
    ActionContext,
    ActionKind,
    ActionSpec,
    Operation,
    OperationStatus,
    OperationTarget,
    Receipt,
    ServiceError,
)
from aisquare.office.operation_store import (
    ACTION_KINDS,
    WIRE_OPERATION_KINDS,
    IdempotencyConflict,
    OperationError,
    OperationJournal,
    OperationRecord,
    ReconcileHook,
    failure_error,
    unknown_error,
)
from aisquare.office.ports import Clock

T = TypeVar("T")

CONTRACT_REVISION_FOR_OPERATIONS = "1.4"
"""The revision a changed 1.4 mutation must declare. Checked before side effects."""


class ActionRejected(OperationError):
    """A submission refused before anything happened, carrying its typed error.

    Every subclass means the same thing operationally: no executor ran, no row
    moved past ``queued``, and nothing reached an agent. P15 maps ``error.code``
    to the local ``error.json`` body.
    """

    def __init__(self, error: ServiceError) -> None:
        super().__init__(error.detail)
        self.error = error


class UnsupportedAction(ActionRejected):
    """No executor is registered for this kind, so the capability is off."""


class ContractRevisionRequired(ActionRejected):
    """A changed 1.4 mutation arrived without ``X-Office-Contract: 1.4``."""


class LegacyActionFailed(ActionRejected):
    """A retained legacy mutation did not succeed.

    Raised rather than returned because the contract kept these routes
    unchanged: they answer a :class:`Receipt` or an error, and they have no
    ``Operation`` a client could poll. Inventing one to describe the failure
    would be adding pending semantics to a route that deliberately has none.
    """


class ActionFailed(Exception):
    """Raised by an executor that knows the action did NOT take effect.

    The classification is the executor's because only it can make it: a rejected
    task claim failed cleanly, while a write to a pane that may have been half
    delivered did not.
    """

    def __init__(self, error: ServiceError) -> None:
        super().__init__(error.detail)
        self.error = error


class ActionUncertain(Exception):
    """Raised by an executor that cannot prove whether the action took effect."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    """What one executor observed. Exactly one of receipt or error is set.

    There is no ``queued`` or ``running`` member: those are the journal's states,
    and an executor that could report them would be reporting on itself.
    """

    status: Literal["succeeded", "failed", "outcome_unknown"]
    receipt: Receipt | None = None
    error: ServiceError | None = None

    def __post_init__(self) -> None:
        if self.status == "succeeded":
            if self.receipt is None:
                raise ValueError("a succeeded outcome must carry its receipt")
            if self.error is not None:
                raise ValueError("a succeeded outcome carries no error")
        else:
            if self.error is None:
                raise ValueError(f"a {self.status!r} outcome must carry its error")
            if self.receipt is not None:
                raise ValueError(f"a {self.status!r} outcome carries no receipt")

    @classmethod
    def succeeded(cls, receipt: Receipt) -> ExecutionOutcome:
        return cls(status="succeeded", receipt=receipt)

    @classmethod
    def failed(cls, error: ServiceError) -> ExecutionOutcome:
        return cls(status="failed", error=error)

    @classmethod
    def unknown(cls, detail: str) -> ExecutionOutcome:
        """Possible delivery, no confirmation. Never retryable."""
        return cls(status="outcome_unknown", error=unknown_error(detail))


class ActionExecutor(Protocol):
    """What P07, P09 and P14 implement, once per kind.

    ``execute`` is BLOCKING and runs on the coordinator's bounded worker, never
    on the event loop. It receives the immutable validated spec — never a raw
    request mapping — and returns an :class:`ExecutionOutcome`, or raises
    :class:`ActionFailed` / :class:`ActionUncertain` to say which of the two it
    knows.
    """

    @property
    def version(self) -> str:
        """Recorded on the journal row, so a later reader knows what ran."""

    def execute(self, spec: ActionSpec, context: ActionContext) -> ExecutionOutcome: ...


class WorkerPool(Protocol):
    """Where blocking work runs. Bounded, and owned by the application lifespan."""

    async def run(self, call: Callable[[], T]) -> T: ...

    async def aclose(self) -> None: ...


class ThreadWorkerPool:
    """A bounded thread pool. The production seam for blocking executors."""

    def __init__(self, max_workers: int = 4, *, name: str = "office-operation") -> None:
        if max_workers <= 0:
            raise ValueError("ThreadWorkerPool needs at least one worker")
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=name)

    async def run(self, call: Callable[[], T]) -> T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, call)

    async def aclose(self) -> None:
        await asyncio.to_thread(self._pool.shutdown, True)


class InlineWorkerPool:
    """Runs the call on the calling thread. A TEST SEAM, not a deployment.

    It blocks the event loop for the duration of the work, which is exactly what
    the bounded pool exists to prevent. It is here because a test that drives a
    fake executor wants deterministic ordering more than it wants concurrency,
    and because a fake pool written per test file is a fake that drifts.
    """

    async def run(self, call: Callable[[], T]) -> T:
        return call()

    async def aclose(self) -> None:
        return None


@dataclass(slots=True)
class OperationMetrics:
    """Counted separately, because they mean different things to an operator.

    A conflict is a client bug; a rejection is a capability or contract problem;
    an unknown is a human decision waiting to happen. Summing them would hide
    the only one that needs somebody to look at it.
    """

    reserved: int = 0
    conflicts: int = 0
    rejected: int = 0
    executed: int = 0
    succeeded: int = 0
    failed: int = 0
    unknown: int = 0
    reconciled: int = 0


class ActionCoordinator:
    """Implements P01's :class:`~aisquare.office.ports.OperationCoordinator`."""

    def __init__(
        self,
        journal: OperationJournal,
        clock: Clock,
        *,
        executors: Mapping[ActionKind, ActionExecutor] | None = None,
        pool: WorkerPool | None = None,
        journal_pool: WorkerPool | None = None,
        ack_s: float = 2.0,
        holder: str | None = None,
        reconciler: ReconcileHook | None = None,
    ) -> None:
        if ack_s <= 0:
            raise ValueError("ack_s must be positive")
        self._journal = journal
        self._clock = clock
        self._executors: dict[ActionKind, ActionExecutor] = dict(executors or {})
        self._pool = pool if pool is not None else ThreadWorkerPool()
        # Journal writes get their own small pool on purpose. Sharing the
        # execution pool would queue a RESERVATION behind a long-running
        # mutation, and "reserve before any side effect" is a promise about
        # order that a shared queue quietly turns into a promise about luck.
        self._journal_pool = journal_pool if journal_pool is not None else ThreadWorkerPool(2)
        self._ack_s = ack_s
        self._holder = holder or f"office-{uuid.uuid4().hex[:12]}"
        self._reconciler = reconciler
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: dict[str, asyncio.Task[OperationRecord]] = {}
        self._metrics = OperationMetrics()

    @property
    def metrics(self) -> OperationMetrics:
        return self._metrics

    @property
    def holder(self) -> str:
        """This coordinator's instance marker, recorded on target-lock rows."""
        return self._holder

    def register(self, kind: ActionKind, executor: ActionExecutor) -> None:
        """Attach the executor for one kind. A second registration is refused.

        Refused rather than replaced because a silently overwritten executor is
        a mutation going somewhere nobody chose, and the packet that registered
        first would have no way to find out.
        """
        if kind not in ACTION_KINDS:
            raise OperationError(f"unknown action kind {kind!r}")
        if kind in self._executors:
            raise OperationError(f"an executor for {kind!r} is already registered")
        self._executors[kind] = executor

    # -- the P01 port ------------------------------------------------------

    async def submit(self, spec: ActionSpec, context: ActionContext) -> Receipt | Operation:
        """Validate, reserve, then execute past the request.

        Order matters and is asserted: capability, contract revision and key are
        checked BEFORE the journal is touched, and the journal is committed
        before the executor is reached. A caller that gets an exception from this
        method can rely on nothing having been done.
        """
        executor = self._executor_for(spec)
        self._require_resolved_target(spec)
        self._require_key(context)
        self._require_contract(spec, context)

        version = executor.version
        reservation = await self._journal_pool.run(
            lambda: self._journal.reserve(spec, context, executor_version=version)
        )
        if reservation.disposition == "conflict" or reservation.record is None:
            self._metrics.conflicts += 1
            raise IdempotencyConflict(context.idempotency_key or "")

        record = reservation.record
        if reservation.disposition == "existing":
            return await self._answer_existing(record, context)

        self._metrics.reserved += 1
        task = asyncio.create_task(
            self._execute(record, spec, context, executor),
            name=f"operation:{record.operation_id}",
        )
        self._tasks[record.operation_id] = task
        task.add_done_callback(lambda _done: self._tasks.pop(record.operation_id, None))
        return await self._answer(task, record, context)

    async def get(self, operation_id: str, context: ActionContext) -> Operation | None:
        """Poll one operation within the caller's own scope.

        Reads the journal and nothing else. A status endpoint that could reach an
        executor would make polling a way to cause side effects.
        """
        scope = context.auth_scope
        return await self._journal_pool.run(lambda: self._journal.get(operation_id, scope))

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> tuple[OperationRecord, ...]:
        """Settle what the last process left unfinished. Repeats nothing.

        A row that was ``running`` at shutdown is a question, not an instruction.
        The optional reconciler answers the ones it can actually prove; the rest
        become ``outcome_unknown`` for a human, with a deliberate retry needing a
        new key.
        """
        settled = await self._journal_pool.run(lambda: self._journal.reconcile(self._reconciler))
        self._metrics.reconciled += len(settled)
        return settled

    async def aclose(self) -> None:
        """Stop accepting work and release the pools.

        In-flight tasks are waited for, never cancelled. Cancelling one would
        abandon a mutation halfway through with the row still ``running`` — and
        the keys already sent are not unsendable. Anything still going after the
        acknowledgement budget is left to the next start's reconciliation.
        """
        tasks = tuple(self._tasks.values())
        if tasks:
            await asyncio.wait(tasks, timeout=self._ack_s)
        await self._pool.aclose()
        await self._journal_pool.aclose()

    # -- validation --------------------------------------------------------

    def _executor_for(self, spec: ActionSpec) -> ActionExecutor:
        if spec.kind not in ACTION_KINDS:
            raise UnsupportedAction(
                failure_error(f"{spec.kind!r} is not an action this office performs")
            )
        executor = self._executors.get(spec.kind)
        if executor is None:
            raise UnsupportedAction(
                ServiceError(
                    code="unsupported_capability",
                    detail=f"no executor is configured for {spec.kind!r} in this office",
                    retryable=False,
                )
            )
        return executor

    def _require_resolved_target(self, spec: ActionSpec) -> None:
        """The target must be the server's own resolved value, not a lookalike."""
        if not isinstance(spec.target, OperationTarget):
            raise UnsupportedAction(
                failure_error("an action target must be a server-resolved OperationTarget")
            )

    def _require_key(self, context: ActionContext) -> None:
        if not context.idempotency_key:
            raise ActionRejected(
                failure_error(
                    "this mutation needs a resolved idempotency key before it can be journalled",
                    code="internal",
                )
            )

    def _require_contract(self, spec: ActionSpec, context: ActionContext) -> None:
        """A changed 1.4 mutation refuses an older caller before any side effect."""
        if spec.kind not in WIRE_OPERATION_KINDS:
            return
        if context.contract_revision != CONTRACT_REVISION_FOR_OPERATIONS:
            raise ContractRevisionRequired(
                failure_error(
                    f"{spec.kind!r} requires contract revision {CONTRACT_REVISION_FOR_OPERATIONS}",
                    code="unsupported_capability",
                )
            )

    # -- answering ---------------------------------------------------------

    async def _answer_existing(
        self, record: OperationRecord, context: ActionContext
    ) -> Receipt | Operation:
        """An identical repeat. The executor is not called a second time."""
        if record.is_terminal:
            return self._respond(record)
        task = self._tasks.get(record.operation_id)
        if task is None:
            return self._respond(record)
        return await self._answer(task, record, context)

    async def _answer(
        self,
        task: asyncio.Task[OperationRecord],
        record: OperationRecord,
        context: ActionContext,
    ) -> Receipt | Operation:
        """Wait briefly for a short action; hand back an Operation for a long one.

        ``shield`` is the whole point: when this wait is abandoned — the budget
        expires, or the HTTP request is cancelled — the task keeps running and
        the journal row keeps being its record.
        """
        if not record.is_wire:
            # A retained legacy route has no 202 and no operation to poll, so
            # there is nothing to hand back early; it finishes or it raises.
            return self._respond(await asyncio.shield(task))
        try:
            final = await asyncio.wait_for(asyncio.shield(task), self._ack_s)
        except TimeoutError:
            scope = context.auth_scope
            operation_id = record.operation_id
            current = await self._journal_pool.run(
                lambda: self._journal.record(operation_id, scope)
            )
            return (current or record).to_operation()
        return self._respond(final)

    def _respond(self, record: OperationRecord) -> Receipt | Operation:
        if record.status == "succeeded" and record.receipt is not None:
            return record.receipt
        if record.is_wire:
            return record.to_operation()
        raise LegacyActionFailed(
            record.error
            or failure_error("this retained legacy mutation did not complete", code="internal")
        )

    # -- execution ---------------------------------------------------------

    async def _execute(
        self,
        record: OperationRecord,
        spec: ActionSpec,
        context: ActionContext,
        executor: ActionExecutor,
    ) -> OperationRecord:
        """Own the work: lock the target, mark running, execute, record, release."""
        key = record.target_key
        operation_id = record.operation_id
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            await self._journal_pool.run(
                lambda: self._journal.acquire_lock(key, operation_id, self._holder)
            )
            try:
                try:
                    await self._journal_pool.run(
                        lambda: self._journal.apply_transition(operation_id, "queued", "running")
                    )
                except OperationError:
                    self._metrics.rejected += 1
                    return await self._fail_before_execution(record)
                self._metrics.executed += 1
                outcome = await self._run_executor(spec, context, executor)
                final = await self._journal_pool.run(
                    lambda: self._journal.apply_transition(
                        operation_id,
                        "running",
                        outcome.status,
                        outcome.receipt,
                        outcome.error,
                    )
                )
                self._count(final.status)
                return final
            finally:
                # After the terminal transition, always. Releasing earlier would
                # let the next same-target action start while this one's keys
                # were still going out.
                await self._journal_pool.run(lambda: self._journal.release_lock(key, operation_id))

    async def _run_executor(
        self, spec: ActionSpec, context: ActionContext, executor: ActionExecutor
    ) -> ExecutionOutcome:
        """Call the executor once, and translate what it raises.

        An unrecognised exception becomes ``outcome_unknown`` rather than
        ``failed``. "Failed" is a claim that nothing happened, and a traceback
        out of an executor is not evidence for that claim — the call may have
        reached tmux and died on the way back. An executor that knows better
        raises :class:`ActionFailed` and says so.
        """
        try:
            return await self._pool.run(lambda: executor.execute(spec, context))
        except ActionFailed as exc:
            return ExecutionOutcome.failed(exc.error)
        except ActionUncertain as exc:
            return ExecutionOutcome.unknown(exc.detail)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return ExecutionOutcome.unknown(
                f"the executor raised {type(exc).__name__} without reporting an outcome; "
                "whether the action reached its target was not observed"
            )

    async def _fail_before_execution(self, record: OperationRecord) -> OperationRecord:
        """The worker could not be entered. ``queued -> failed``, nothing ran."""
        operation_id = record.operation_id
        error = failure_error(
            "this operation could not be started and was not attempted", code="internal"
        )
        try:
            failed = await self._journal_pool.run(
                lambda: self._journal.apply_transition(
                    operation_id, "queued", "failed", None, error
                )
            )
        except OperationError:
            return record
        self._metrics.failed += 1
        return failed

    def _count(self, status: OperationStatus) -> None:
        if status == "succeeded":
            self._metrics.succeeded += 1
        elif status == "failed":
            self._metrics.failed += 1
        elif status == "outcome_unknown":
            self._metrics.unknown += 1


__all__ = [
    "CONTRACT_REVISION_FOR_OPERATIONS",
    "ActionCoordinator",
    "ActionExecutor",
    "ActionFailed",
    "ActionRejected",
    "ActionUncertain",
    "ContractRevisionRequired",
    "ExecutionOutcome",
    "InlineWorkerPool",
    "LegacyActionFailed",
    "OperationMetrics",
    "ThreadWorkerPool",
    "UnsupportedAction",
    "WorkerPool",
]
