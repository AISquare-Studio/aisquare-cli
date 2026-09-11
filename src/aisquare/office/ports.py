"""The seams. Small protocols now, implementations in later packets.

Every port here is declared so that the packet which implements it and the
packets which consume it cannot invent two versions of the same interface. They
are deliberately tiny: a port that describes an architecture is a port nobody
can fake in a test.

**Most methods are synchronous, and that is the design.** Collection, SQLite
and tmux are blocking, so pretending otherwise with ``async def`` would only
move the blocking call onto the event loop with a coroutine wrapped around it.
The application owns the offloading: a bounded executor started by the ASGI
lifespan runs these, and nothing here may be called directly from a request
handler. The two ports that *are* async — :class:`StreamHub` and the
coordinator's execution — are the ones that genuinely belong to the loop.

Fakes are meant to be one small class each. If implementing a fake for a test
requires a framework, the port is wrong.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol, TypeVar

from aisquare.office.models import (
    ActionContext,
    ActionSpec,
    CheckpointRecord,
    ContextPreview,
    Event,
    InjectionRecord,
    InsightSummary,
    LocalObservationBatch,
    ObservationUpdate,
    Operation,
    OperationReservation,
    OperationStatus,
    Page,
    PlatformBinding,
    PlatformQuery,
    PlatformScope,
    PolicySummary,
    Projection,
    ProvenanceSummary,
    ReasoningSummary,
    Receipt,
    RunDetail,
    RunJoin,
    RunSummary,
    ServiceError,
    ServiceResult,
    TeachAck,
    TeachRequest,
    TeamOSMeta,
    TeamOSRosterEntry,
    TeamOSView,
    TerminalFrame,
    TerminalTarget,
    TransportResult,
)


class Clock(Protocol):
    """Wall time for display, monotonic time for budgets.

    Two methods rather than one because they answer different questions and one
    of them lies. ``now()`` is what a timestamp shows a user and can jump
    backwards when the system clock is corrected; ``monotonic()`` cannot, which
    is why every age, deadline and budget is measured with it. A fake clock
    implements both and a test drives time instead of sleeping.
    """

    def now(self) -> datetime:
        """Timezone-aware UTC wall time."""

    def monotonic(self) -> float:
        """Seconds from an arbitrary origin, never decreasing."""


class LocalSource(Protocol):
    """One bounded read of everything local, through public CLI APIs."""

    def collect(self, now: datetime) -> LocalObservationBatch:
        """Blocking. The caller runs this off the event loop.

        Read-only: collection never spawns, types, stops or repairs anything,
        so a poll that runs while the user is working changes nothing.
        """


class ObservationStore(Protocol):
    """Latest-fact storage for the Office sidecar.

    A cache of evidence, not a competing fleet or task store: it never writes a
    ``TeamEvent``, and the CLI's own store stays authoritative for everything it
    owns.
    """

    def read(self, *, now: datetime) -> LocalObservationBatch:
        """The latest facts and their freshness — values, never a live cursor."""

    def merge(self, batch: LocalObservationBatch) -> None:
        """Transactionally upsert independent facts.

        Independent because hooks and the poller write concurrently: a merge
        that replaced whole rows would let one writer erase a column the other
        had just learned.
        """

    def expire(self, *, now: datetime) -> int:
        """Delete only beyond the retention or size policy; return the count."""

    def read_checkpoint(self, consumer: str, source_id: str) -> CheckpointRecord | None:
        """One consumer's cursor in one source, or None when it has never run."""

    def write_checkpoint(
        self,
        consumer: str,
        source_id: str,
        expected_generation: int,
        state: Mapping[str, object],
        generation: int,
        observed_at: datetime,
        *,
        derived_updates: tuple[ObservationUpdate, ...] = (),
    ) -> bool:
        """Advance the cursor and commit its derived facts in one transaction.

        Compare-and-set on ``expected_generation``; False means someone else
        moved first and nothing was written. The atomicity is the contract: a
        cursor that advanced without its rows loses the work, and rows written
        without the cursor are counted twice on the next pass.
        """


class Projector(Protocol):
    """Batch plus previous projection to one immutable snapshot, and its diff."""

    def project(
        self,
        batch: LocalObservationBatch,
        previous: Projection | None,
        now: datetime,
    ) -> Projection:
        """Pure and deterministic in its inputs.

        No store, no clock of its own, no tmux, no HTTP. Given the same batch,
        previous projection and ``now``, it returns the same projection — which
        is what makes the derived states testable at all.
        """

    def diff(self, previous: Projection | None, current: Projection) -> tuple[Event, ...]:
        """The canonical ordered events between two projections.

        Must handle an unchanged board sequence: pane-derived state (a tool
        starting, a prompt appearing) moves without the store moving, and a
        differ keyed on ``seq`` would drop exactly the events the office exists
        to show.
        """


class StreamHub(Protocol):
    """Event-loop-owned publication. The subscription type belongs to P05.

    Generic so that the concrete queue/subscription object stays P05's choice
    while consumers can still be typed against it.
    """

    def publish(self, events: Sequence[Event]) -> None:
        """Hand events to every subscriber. Never blocks on a slow one."""

    async def subscribe(self, *, max_queue: int) -> object:
        """Attach, receiving the current snapshot and later events atomically.

        Atomic because the gap is the bug: a subscriber that reads a snapshot
        and *then* starts listening misses everything in between and never
        finds out.
        """

    async def close(self) -> None:
        """Drop every subscriber and release the hub. Idempotent."""


class OperationStore(Protocol):
    """The durable journal. Reserves before any side effect happens."""

    def create_or_get(self, spec: ActionSpec, context: ActionContext) -> OperationReservation:
        """Reserve ``(auth_scope, idempotency_key)``, or report the conflict.

        Uniqueness is that pair and deliberately not the kind or target:
        including them would let one key be reused for a different action and
        slip past detection. The *fingerprint* compared is kind + resolved
        target + validated body.
        """

    def transition(
        self,
        operation_id: str,
        expected: OperationStatus,
        status: OperationStatus,
        receipt: Receipt | None = None,
        error: ServiceError | None = None,
    ) -> Operation:
        """Compare-and-set the status, so two workers cannot both finish one job."""

    def get(self, operation_id: str, auth_scope: str) -> Operation | None:
        """Scoped read. One local session cannot enumerate another's operations."""


class OperationCoordinator(Protocol):
    """Owns execution past the request, and reports the outcome truthfully."""

    async def submit(self, spec: ActionSpec, context: ActionContext) -> Receipt | Operation:
        """A :class:`Receipt` when the work finished, an Operation when it did not.

        Execution survives request cancellation: a browser that navigates away
        mid-keystroke must not leave a half-typed pane and no record of it.
        """

    async def get(self, operation_id: str, context: ActionContext) -> Operation | None:
        """Poll one operation within the caller's own scope."""


class LocalActions(Protocol):
    """Fleet, task and feedback effects, through the existing CLI services."""

    def execute(self, spec: ActionSpec, context: ActionContext) -> Receipt | Operation:
        """Re-check ownership and capability at execution time.

        The checks are repeated here on purpose: whatever was true when the
        browser rendered the button may not be true now, and a lease that
        expired in between is exactly the case that must not silently succeed.
        """


class TerminalSource(Protocol):
    """Shared read-only capture of server-resolved panes."""

    def capture(
        self,
        target: TerminalTarget,
        *,
        scrollback: int = 0,
        viewport_height: int | None = None,
    ) -> TerminalFrame:
        """One frame. ``scrollback`` 0 is the live screen.

        The returned frame reports the *effective* offset, which tmux clamps to
        the pane's history — never the requested one.
        """

    def scrollback(
        self,
        target: TerminalTarget,
        *,
        before: int,
        viewport_height: int | None = None,
    ) -> TerminalFrame:
        """The screen as it was ``before`` lines above the live view."""


class PromptActions(Protocol):
    """Answering a prompt: journaled, locked, and fenced to one lifecycle."""

    def resolve(self, spec: ActionSpec, context: ActionContext) -> Receipt | Operation:
        """Send the planned keystrokes, or none at all.

        A prompt id that is no longer the agent's current prompt sends **zero**
        input and fails. Partial delivery is reported as ``outcome_unknown``
        rather than as success: the keys that did land cannot be unsent.
        """


class TeamOSClient(Protocol):
    """The local Team OS peer. Allow-listed views only — never a path read."""

    def meta(self) -> ServiceResult[TeamOSMeta]:
        """The peer's own description. A stopped peer is an ordinary result."""

    def roster(self, query: PlatformQuery | None = None) -> ServiceResult[Page[TeamOSRosterEntry]]:
        """Peer identities, labelled as peer data. Never merged into the fleet."""

    def view(self, name: str) -> ServiceResult[TeamOSView]:
        """One named view from the back-end's allow-list.

        ``name`` is matched against that list, not forwarded: the browser can
        only ask for a name the peer already advertised.
        """


class PlatformTransport(Protocol):
    """Bounded HTTP to the configured platform. Not a browser proxy."""

    def request(
        self,
        method: str,
        path: str,
        *,
        query: PlatformQuery | None = None,
        body: Mapping[str, object] | None = None,
        binding: PlatformBinding,
        timeout_class: str,
    ) -> TransportResult:
        """Resolve endpoint and credential from the binding, then read bounded JSON.

        The credential is never an argument and never a field of the binding —
        it is resolved at this moment from the existing CLI mechanism. The
        decoded body never reaches a browser directly; P13/P14 map it into a
        :class:`ServiceResult` first.
        """


class ExplainabilityClient(Protocol):
    """Normalised run data, with the join verified rather than guessed."""

    def list_runs(
        self, binding: PlatformBinding, query: PlatformQuery
    ) -> ServiceResult[Page[RunSummary]]:
        """Runs in the bound scope."""

    def get_run(self, binding: PlatformBinding, run_id: str) -> ServiceResult[RunDetail]:
        """One run, plus which detail views it actually has."""

    def get_policies(self, binding: PlatformBinding, run_id: str) -> ServiceResult[PolicySummary]:
        """Policy results. Still ``processing`` is a successful read, not an outage."""

    def get_reasoning(
        self, binding: PlatformBinding, run_id: str, kind: str
    ) -> ServiceResult[ReasoningSummary]:
        """Bounded titled sections. Never a serialised trace tree."""

    def join(self, local_session: str, binding: PlatformBinding) -> RunJoin:
        """Whether this local session is provably one remote run.

        A name match, a shared prefix or a close timestamp is not a join. More
        than one candidate is ``ambiguous``, which reports as ``unjoined``.
        """


class PraxisClient(Protocol):
    """Learning views. Two invariants here have teeth."""

    def insights(
        self, binding: PlatformBinding, scope: PlatformScope
    ) -> ServiceResult[Page[InsightSummary]]:
        """Lessons in the bound studio/agent scope."""

    def preview(
        self, binding: PlatformBinding, agent_uid: str, scope: PlatformScope
    ) -> ServiceResult[ContextPreview]:
        """What *would* be assembled.

        There is no ``run_id`` parameter, and there must never be one: a preview
        that carried the current session's run would read like an injection that
        happened. It creates no audit record.
        """

    def injections(
        self, binding: PlatformBinding, run_id: str
    ) -> ServiceResult[Page[InjectionRecord]]:
        """What was *served*, and when. Never what a model consumed."""

    def chain(self, binding: PlatformBinding, insight_id: str) -> ServiceResult[ProvenanceSummary]:
        """One lesson's bounded provenance. Not a graph query endpoint."""

    def teach(self, binding: PlatformBinding, request: TeachRequest) -> ServiceResult[TeachAck]:
        """Submit a lesson, capability permitting.

        The acknowledgement says the platform ingested the text. It does not say
        a lesson was created, and no caller may present it as if it did.
        """


SubscriptionT = TypeVar("SubscriptionT", covariant=True)
"""Reserved for P05 to parametrise its own subscription type against StreamHub."""


__all__ = [
    "Clock",
    "ExplainabilityClient",
    "LocalActions",
    "LocalSource",
    "ObservationStore",
    "OperationCoordinator",
    "OperationStore",
    "PlatformTransport",
    "PraxisClient",
    "Projector",
    "PromptActions",
    "StreamHub",
    "SubscriptionT",
    "TeamOSClient",
    "TerminalSource",
]
