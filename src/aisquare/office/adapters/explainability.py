"""Normalised Explainability reads: runs, one run, its policies, its reasoning.

P12 fetches; this module *interprets*. It takes a
:class:`~aisquare.office.models.TransportResult` — exactly one of a decoded
response or a sanitised error — and turns it into P01's public
:class:`~aisquare.office.models.ServiceResult`, admitting only fields the
recorded fixtures establish and leaving everything else behind.

Four readings decide almost every line here.

**A status code is not a verdict.** The status lives on the response and this
module reads it. A 200 whose row says ``processing`` is a *successful* read of
a run that has not finished, and a 200 naming failed studios is a *successful*
read that covered less than the whole workspace. Neither is an outage, and
turning either into ``unavailable`` would throw away rows the platform did
return.

**A 404 means four different things on this API**, and they are not
interchangeable: the workspace is absent (a binding problem), the run is masked
or absent (deliberately identical wording, so a response cannot confirm that
another tenant's run exists), a derived document is not built *yet* (poll), or
that document is gone for good (stop). The last two differ only in prose, which
is fragile, so :meth:`HttpExplainabilityClient.read_policies` and
:meth:`~HttpExplainabilityClient.read_reasoning` poll on the run's own state
where the caller supplies it and treat the wording as corroboration. A masked
404 never becomes a 403 and never becomes a global error.

**A join is proven or it does not exist.** The only evidence admitted is the
verified correlation P02 recorded, scoped to this binding *and* its revision. A
display label, a role, a model name, a repository basename, a close timestamp
or a shared prefix is never a remote identity — none of those fields is even
read on the join path. More than one candidate is ``ambiguous``, which reports
as unjoined, because an unproven link shown as verified is worse than no link.

**Prompt text never enters an Office view.** ``root_input``, ``root_output``
and the story's ``metadata`` excerpts carry verbatim human and model text.
Every one of them is dropped at the decode boundary rather than bounded, so
there is no path by which a transcript reaches a browser payload or a log.

Nothing here mounts a route, and nothing here is async: the transport is
blocking and P15 owns running it off the event loop.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final, Literal, Protocol, TypeVar

from aisquare.office.adapters.platform_transport import NOT_FOUND_OR_MASKED, classify_status
from aisquare.office.models import (
    DetailState,
    Page,
    PageCoverage,
    PlatformBinding,
    PlatformQuery,
    PlatformScope,
    PolicyRow,
    PolicyStatus,
    PolicySummary,
    ReasoningSection,
    ReasoningSummary,
    RunDetail,
    RunJoin,
    RunState,
    RunSummary,
    ServiceError,
    ServiceErrorCode,
    ServiceResult,
    ServiceStatus,
    TransportResult,
    service_failed,
    service_ok,
)
from aisquare.office.platform_redaction import detail_of, sanitize_detail
from aisquare.office.ports import Clock

if TYPE_CHECKING:  # pragma: no cover - import cost, not behaviour
    from aisquare.office.storage import CorrelationRecord

T = TypeVar("T")
"""What a no-data result would have carried. The failure helpers below are
generic in it for one reason: a result with null data is the same object
whatever the read was for, and giving them one concrete type would have made
every other caller cast — which is how a page ends up typed as a policy."""

# --------------------------------------------------------------------------
# Bounds
# --------------------------------------------------------------------------

MAX_PAGE_ITEMS: Final = 200
"""``Page.items``' own ceiling, restated so the clip happens before validation."""

DEFAULT_PAGE_LIMIT: Final = 50
MAX_REQUEST_LIMIT: Final = 100
"""``PlatformQuery.limit``'s upper bound. The gateway's own ``page_limit``
derives from an operator knob and is read off the response, never assumed."""

MAX_ACTIVE_REFRESH: Final = 8
"""How many known-active runs one list call may refresh individually.

Bounded because the refresh is one request per run: a workspace with two
hundred running agents must not turn one page read into two hundred reads."""

MAX_SECTIONS: Final = 32
MAX_SECTION_CHARS: Final = 8000
MAX_SUMMARY_CHARS: Final = 4000
MAX_POLICY_ROWS: Final = 100
MAX_ENFORCEMENTS: Final = 100
MAX_LABEL_CHARS: Final = 120
MAX_ROW_SUMMARY_CHARS: Final = 1000
MAX_SECONDS: Final = 31_536_000
"""``RunSummary.duration_s``' ceiling — one year. Longer is not clamped, it is
reported as unknown: a clamped duration is a number nobody measured."""

ACTIVE_STATES: Final[frozenset[RunState]] = frozenset({"queued", "running"})

ReasoningKind = Literal["story", "rml", "chain"]
REASONING_KINDS: Final[tuple[ReasoningKind, ...]] = ("story", "rml", "chain")

NotFoundMeaning = Literal["workspace_absent", "masked", "not_ready", "gone", "route_absent"]
"""The four meanings of a 404 on this API, plus the unrouted path that looks
like a fifth. See the module docstring."""

FORBIDDEN_TEXT_FIELDS: Final = frozenset(
    {
        "root_input",
        "root_output",
        "user_prompt",
        "system_prompt",
        "args_excerpt",
        "result_excerpt",
        "input_excerpt",
        "output_excerpt",
        "before",
        "after",
    }
)
"""Fields recorded as carrying verbatim prompt, tool or model text.

Named here rather than merely omitted from the decode so the rule is testable:
a test asserts no admitted value ever equals one of these fields' contents."""

_LIST_FILTERS: Final = frozenset(
    {"since", "agent", "status", "q", "has_errors", "has_policies", "sort"}
)
"""Exactly the narrowing parameters the runs list documents. ``offset`` is not
here: it is derived from the opaque cursor, never taken from a caller."""

_CURSOR_VERSION: Final = 1


# --------------------------------------------------------------------------
# Seams
# --------------------------------------------------------------------------


class RunTransport(Protocol):
    """P12's transport, as this adapter uses it.

    Narrower than the :class:`~aisquare.office.ports.PlatformTransport` port on
    one axis and wider on another: this module never sends a body, and it needs
    the two bookkeeping methods the HTTP implementation adds —
    :meth:`last_success_at` for freshness and :meth:`invalidate` so a changed
    selection cannot keep painting the previous binding's rows.
    """

    def request(
        self,
        method: str,
        path: str,
        *,
        query: PlatformQuery | None = None,
        body: Mapping[str, object] | None = None,
        binding: PlatformBinding,
        timeout_class: str = "ordinary",
    ) -> TransportResult:
        """One bounded request. Returns a response *or* an error, never both."""

    def last_success_at(self, binding: PlatformBinding) -> datetime | None:
        """When this exact binding revision last read something successfully."""

    def invalidate(self, *, binding_id: str | None = None, revision: int | None = None) -> int:
        """Drop cached results for a binding, a revision, or everything."""


class CorrelationReader(Protocol):
    """P02's correlation table, read-only.

    The adapter takes a reader rather than a path: it opens no database of its
    own, and it never writes. Joins are *recorded* by whatever performed the
    launch or the ship; reading them is all this packet is entitled to do.
    """

    def correlations_for(
        self, *, project_id: str, session_id: str | None = None
    ) -> tuple[CorrelationRecord, ...]:
        """Every join record for a project, newest first; optionally one session."""


# --------------------------------------------------------------------------
# Adapter-private readings
#
# The public models are deliberately narrow — P00 froze them and P13 does not
# own them — so facts that have no home on the wire live here instead, where
# P15 can still reach them. Each one names in its docstring the field it could
# not carry publicly.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunFacts:
    """Per-run facts the public :class:`RunSummary` has no field for.

    ``parent_run_id`` is the load-bearing one: a nested in-process subagent run
    reports its own cost, and attributing that cost to the child as if it were
    separate work double counts the parent. See :attr:`cost_attribution`.
    """

    run_id: str
    parent_run_id: str | None = None
    run_kind: str | None = None
    upstream_session_id: str | None = None
    token_count: int | None = None
    node_count: int | None = None
    error_count: int | None = None
    flagged_count: int | None = None
    is_governed: bool | None = None
    policy_unverified_count: int | None = None
    policy_needs_review_count: int | None = None
    runtime_decision_count: int | None = None
    graph_available: bool | None = None
    root_output_missing_reason: str | None = None

    @property
    def cost_attribution(self) -> Literal["run", "shared"]:
        """``shared`` when this run has a parent.

        A nested run's cost is not separable from its parent's without a
        producer that proves an identity boundary, and this API supplies no
        such proof — only the parent id. Shared is the honest answer.
        """
        return "shared" if self.parent_run_id else "run"


@dataclass(frozen=True, slots=True)
class RunListReading:
    """One list read: the public result plus the coverage it could not carry.

    ``page_limit`` and ``runs_reachable`` have no home on
    :class:`~aisquare.office.models.PageCoverage`, whose five fields P00 froze.
    They are how a caller learns a ceiling exists at all — when
    ``runs_reachable`` is below the reported total, the difference is reachable
    by no offset, limit or ``since`` value whatsoever — so they are carried
    here and named in the handoff as a proposed amendment.
    """

    result: ServiceResult[Page[RunSummary]]
    facts: tuple[RunFacts, ...] = ()
    studios_read: tuple[str, ...] = ()
    studios_failed: tuple[str, ...] = ()
    studios_omitted: int | None = None
    page_limit: int | None = None
    runs_reachable: int | None = None
    refreshed_run_ids: tuple[str, ...] = ()
    """Known-active runs re-read individually because the list's ``since``
    filter is on *start* time and cannot reach a run that started earlier."""


@dataclass(frozen=True, slots=True)
class RunDetailReading:
    result: ServiceResult[RunDetail]
    facts: RunFacts | None = None
    poll_after_ms: int | None = None
    """The route's own polling advice. Non-null only while the run is
    unfinished, and the cleanest pollable signal this API offers."""
    not_found: NotFoundMeaning | None = None


@dataclass(frozen=True, slots=True)
class PolicyGate:
    """One post-hoc rule-book verdict.

    ``outcome`` wins over ``passed`` when both are present: ``unverified`` and
    ``needs_review`` both carry ``passed: true`` for legacy readers, and folding
    either into a pass is a false green the upstream's own notes record as a
    past defect.
    """

    policy_id: str
    outcome: str | None
    status: PolicyStatus
    triggered: bool | None = None
    tag: str | None = None
    degraded_reason: str | None = None


@dataclass(frozen=True, slots=True)
class EnforcementReading:
    """What the live runtime gate actually did, which is a different system
    from the audit above. The two are never merged and never summed."""

    span_id: str | None
    action: str | None
    outcome: str | None
    violation_count: int | None = None
    degraded: bool | None = None


@dataclass(frozen=True, slots=True)
class PolicyReading:
    result: ServiceResult[PolicySummary]
    gates: tuple[PolicyGate, ...] = ()
    enforcements: tuple[EnforcementReading, ...] = ()
    is_governed: bool | None = None
    """Tri-state and it must stay tri-state: true is "a gate ran and found
    nothing", false is "ran with no rule book bound", null is "unknown"."""
    audited_at: datetime | None = None
    unverified_count: int = 0
    needs_review_count: int = 0
    pollable: bool = False
    not_found: NotFoundMeaning | None = None


@dataclass(frozen=True, slots=True)
class _ReasoningExtras:
    """What one reasoning kind establishes beyond its sections.

    Typed rather than a loose mapping because the three readers populate
    disjoint halves of it, and a dictionary would let one of them quietly stop
    reporting coverage without anything noticing.
    """

    spans_total: int | None = None
    spans_projected: int | None = None
    is_enriching: bool | None = None
    extraction_confidence: float | None = None
    low_confidence: bool | None = None


@dataclass(frozen=True, slots=True)
class ReasoningReading:
    result: ServiceResult[ReasoningSummary]
    kind: str = "story"
    spans_total: int | None = None
    spans_projected: int | None = None
    """Story coverage. Null means *not measured* — explicitly not "complete"."""
    is_enriching: bool | None = None
    extraction_confidence: float | None = None
    low_confidence: bool | None = None
    pollable: bool = False
    not_found: NotFoundMeaning | None = None


@dataclass(frozen=True, slots=True)
class RunCostComparison:
    """Gateway cost beside local transcript cost, never added together.

    There is deliberately no total on this type. The two are overlapping
    measurements of the same work from different vantage points, and a sum is
    not a larger truth — it is double counting with a decimal point.
    """

    platform_usd: float | None
    local_usd: float | None
    state: Literal["both", "platform_only", "local_only", "neither"]
    attribution: Literal["run", "shared"] = "run"
    local_is_lower_bound: bool = False
    """``price_known: false`` with a USD figure means at least one call was
    unpriced, so the figure is a floor. Zero with it is *unknown*, not free."""

    @property
    def platform_measured_zero(self) -> bool:
        """True only for a measured zero. Null is unmeasured and stays null."""
        return self.platform_usd == 0.0


# --------------------------------------------------------------------------
# Decoding helpers — every one of them returns None rather than a guess
# --------------------------------------------------------------------------


def _object(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, dict) else None


def _text(value: object, *, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return stripped[:limit]


def _flag(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _whole(value: object) -> int | None:
    """An integer, refusing ``bool``.

    ``isinstance(True, int)`` is true in Python, so a boolean column read as a
    count would silently become 1 — which is exactly the kind of fabricated
    number the null-versus-zero rule exists to prevent.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _moment(value: object) -> datetime | None:
    """An upstream timestamp, or None when it is not an unambiguous instant.

    A naive timestamp is refused rather than assumed to be UTC: a displayed
    time that is wrong by an offset looks exactly like a correct one.
    """
    text = _text(value, limit=64)
    if text is None:
        return None
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _usd(value: object) -> float | None:
    """Cost in USD. Null stays null, ``0.0`` stays a measured zero."""
    amount = _number(value)
    if amount is None or amount < 0:
        return None
    return amount


def _duration_seconds(value: object) -> int | None:
    milliseconds = _number(value)
    if milliseconds is None or milliseconds < 0:
        return None
    seconds = round(milliseconds / 1000)
    if seconds > MAX_SECONDS:
        return None
    return seconds


def _strings(value: object, *, limit: int, each: int) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    found: list[str] = []
    for item in value[:limit]:
        text = _text(item, limit=each)
        if text is not None:
            found.append(text)
    return tuple(found)


# --------------------------------------------------------------------------
# Domain classification
# --------------------------------------------------------------------------


def run_state_of(row: Mapping[str, object]) -> tuple[RunState, str | None]:
    """The normalised state and the label the platform actually used.

    Two fields carry two different facts and neither is sufficient alone.
    ``status`` is the *pipeline's* progress through the trace — a ``failed``
    there means the worker could not process the trace, not that the agent
    failed. ``run_verdict`` is the run's own outcome and is null until a
    recount runs. So the pipeline decides while it is still working, the
    verdict decides once it exists, and an ended run with neither is reported
    ``completed`` on the strength of its end time rather than guessed at.
    """
    label = _text(row.get("status"), limit=64)
    pipeline = (label or "").lower()
    if pipeline in ("received", "queued"):
        return "queued", label
    if pipeline in ("processing", "running"):
        return "running", label
    verdict = (_text(row.get("run_verdict"), limit=64) or "").lower()
    if verdict in ("completed", "succeeded", "success"):
        return "completed", label
    if verdict == "failed":
        return "failed", label
    if verdict in ("cancelled", "canceled"):
        return "cancelled", label
    if pipeline == "failed":
        return "failed", label
    if _moment(row.get("ended_at")) is not None:
        return "completed", label
    return "unknown", label


def policy_status_of(gate: Mapping[str, object]) -> tuple[PolicyStatus, str | None]:
    """One gate's verdict, with the upstream word kept beside it.

    ``unverified`` (no trustworthy verdict could be obtained) and
    ``needs_review`` (a second model declined to uphold a flag) are neither
    passes nor failures. The public enum has no member for either, so both map
    to ``unknown`` — never to ``passed`` — and the exact word survives in the
    returned label and in :class:`PolicyReading`'s separate counts.
    """
    outcome = _text(gate.get("outcome"), limit=32)
    lowered = (outcome or "").lower()
    if lowered == "pass":
        return "passed", outcome
    if lowered in ("fail", "blocked"):
        return "failed", outcome
    if lowered in ("unverified", "needs_review"):
        return "unknown", outcome
    if lowered:
        return "unknown", outcome
    passed = _flag(gate.get("passed"))
    if passed is True:
        return "passed", None
    if passed is False:
        return "failed", None
    return "unknown", None


def not_found_meaning(detail: str | None) -> NotFoundMeaning:
    """Which of this API's four 404s (plus an unrouted path) arrived.

    Prose is a fragile discriminator and this function is not the safety net —
    the callers poll on the run's own state where they have it. What the
    wording *is* reliable for is the terminal signal: the ``gone`` helper
    deliberately never says "yet" or "building" and says "Polling will not
    change this", precisely so a poller stops.
    """
    text = (detail or "").strip()
    lowered = text.lower()
    if not text or lowered == "not found":
        return "route_absent"
    if "workspace not found" in lowered:
        return "workspace_absent"
    if "polling will not change this" in lowered or " is gone" in lowered:
        return "gone"
    if "the run resolved" in lowered:
        return "not_ready"
    return "masked"


def status_for_error(code: ServiceErrorCode) -> ServiceStatus:
    """Which availability a transport error code reports.

    ``binding_required`` becomes ``unconfigured`` rather than ``unavailable``:
    the platform is fine and the binding is not, so the fix is to resolve it
    again, and calling that an outage sends an operator looking at the wrong
    thing.
    """
    if code == "service_unconfigured" or code == "binding_required":
        return "unconfigured"
    if code == "unauthorized":
        return "unauthorized"
    if code == "forbidden":
        return "forbidden"
    if code == "unsupported_capability":
        return "unsupported"
    return "unavailable"


# --------------------------------------------------------------------------
# Cursors
# --------------------------------------------------------------------------


def encode_cursor(offset: int, binding: PlatformBinding) -> str:
    """An opaque cursor bound to the binding that produced it.

    The upstream paginates by integer offset, and handing that integer to a
    browser as the cursor would make it a *parameter*: a client could widen its
    own scope by inventing one, and a cursor minted under one workspace would
    silently continue into another. Binding the workspace and the revision into
    the token means a stale or foreign cursor is refused rather than followed.
    """
    payload = json.dumps(
        {"v": _CURSOR_VERSION, "o": offset, "w": binding.workspace_id, "r": binding.revision},
        separators=(",", ":"),
        sort_keys=True,
    )
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str, binding: PlatformBinding) -> int | None:
    """The offset, or None when the cursor is not this binding's."""
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        decoded = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(decoded, dict) or decoded.get("v") != _CURSOR_VERSION:
        return None
    if decoded.get("w") != binding.workspace_id or decoded.get("r") != binding.revision:
        return None
    offset = _whole(decoded.get("o"))
    if offset is None or offset < 0:
        return None
    return offset


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------


def run_summary_of(
    row: Mapping[str, object],
    binding: PlatformBinding,
    *,
    joined_sessions: Mapping[str, str] | None = None,
    studio_id: str | None = None,
) -> tuple[RunSummary, RunFacts] | None:
    """One upstream run row, normalised — or None when it names no run.

    ``local_agent_id`` is filled only from ``joined_sessions``, which the caller
    builds exclusively from verified correlations. There is no fallback path,
    which is the point: ``agent_name`` is read into nothing, so no amount of
    resemblance between a label and a session can produce a join here.
    """
    run_id = _text(row.get("run_id"), limit=128)
    if run_id is None:
        return None
    state, label = run_state_of(row)
    session = (joined_sessions or {}).get(run_id)
    summary = RunSummary(
        run_id=run_id,
        workspace_id=binding.workspace_id,
        studio_id=_text(row.get("studio_id"), limit=128) or studio_id,
        stable_agent_id=_text(row.get("agent_name"), limit=128),
        local_agent_id=session,
        join="verified" if session else "unjoined",
        state=state,
        upstream_state_label=label,
        started_at=_moment(row.get("started_at")),
        updated_at=_moment(row.get("updated_at")),
        platform_usd=_usd(row.get("cost_usd")),
        duration_s=_duration_seconds(row.get("duration_ms")),
    )
    facts = RunFacts(
        run_id=run_id,
        parent_run_id=_text(row.get("parent_run_id"), limit=128),
        run_kind=_text(row.get("run_kind"), limit=64),
        upstream_session_id=_text(row.get("session_id"), limit=128),
        token_count=_whole(row.get("token_count")),
        node_count=_whole(row.get("node_count")),
        error_count=_whole(row.get("error_count")),
        flagged_count=_whole(row.get("flagged_count")),
        is_governed=_flag(row.get("is_governed")),
        policy_unverified_count=_whole(row.get("policy_unverified_count")),
        policy_needs_review_count=_whole(row.get("policy_needs_review_count")),
        runtime_decision_count=_whole(row.get("runtime_decision_count")),
        graph_available=_flag(row.get("graph_available")),
        root_output_missing_reason=_text(row.get("root_output_missing_reason"), limit=64),
    )
    return summary, facts


def coverage_of(body: Mapping[str, object]) -> PageCoverage:
    """The five coverage fields P00 froze, from the ten the upstream sends.

    ``total_is_exact`` is kept exactly as sent because it is false for two
    different reasons — a bounded scan *or* a studio that failed — and this
    route, unlike the studio-scoped one, sends no window size to tell them
    apart. Rendering a floor as an exact count is the failure this prevents.
    """
    studios_read = _strings(body.get("studios_read"), limit=10_000, each=128)
    studios_failed = _strings(body.get("studios_failed"), limit=10_000, each=128)
    return PageCoverage(
        reported_total=_whole(body.get("total_count")),
        total_is_exact=_flag(body.get("total_is_exact")),
        reachable_scope_count=len(studios_read),
        failed_scope_count=len(studios_failed),
        omitted_scope_count=_whole(body.get("studios_omitted")),
    )


def merge_summaries(runs: Sequence[RunSummary]) -> tuple[RunSummary, ...]:
    """Collapse duplicate run ids, keeping the freshest row for each.

    Overlapping pages are not a mistake to be avoided: ``since`` filters on the
    run's *start* time, so the only safe way to notice a run that started
    before the window and moved inside it is to overlap and deduplicate. First
    appearance sets the position, so a refresh does not reshuffle the list
    under a reader; a later row updates the fields.
    """
    order: list[str] = []
    best: dict[str, RunSummary] = {}
    for run in runs:
        previous = best.get(run.run_id)
        if previous is None:
            order.append(run.run_id)
            best[run.run_id] = run
            continue
        best[run.run_id] = _fresher(previous, run)
    return tuple(best[run_id] for run_id in order)


def _fresher(previous: RunSummary, current: RunSummary) -> RunSummary:
    """The newer of two readings of one run, field by field where it matters.

    A detail read carries no ``studio_id`` — the workspace detail route does
    not return one — so taking the newer row wholesale would erase the studio
    the list already established. Losing a known fact to a fresher read is a
    regression, not an update. Rebuilt rather than copied so the model's own
    join invariant is re-checked on the merged row.
    """
    if previous.updated_at is not None and current.updated_at is not None:
        newer = current if current.updated_at >= previous.updated_at else previous
        older = previous if newer is current else current
    else:
        newer, older = current, previous
    session = newer.local_agent_id or older.local_agent_id
    return RunSummary(
        run_id=newer.run_id,
        workspace_id=newer.workspace_id,
        studio_id=newer.studio_id or older.studio_id,
        stable_agent_id=newer.stable_agent_id or older.stable_agent_id,
        local_agent_id=session,
        join="verified" if session else "unjoined",
        state=newer.state,
        upstream_state_label=newer.upstream_state_label,
        started_at=newer.started_at or older.started_at,
        updated_at=newer.updated_at or older.updated_at,
        platform_usd=newer.platform_usd,
        duration_s=newer.duration_s,
    )


def compare_costs(
    summary: RunSummary,
    facts: RunFacts | None = None,
    *,
    local_usd: float | None = None,
    local_price_known: bool | None = None,
) -> RunCostComparison:
    """Gateway cost and local transcript cost, side by side and never summed.

    They measure the same work from two vantage points — the platform bills
    what it served, the transcript records what the client observed — so adding
    them counts the work twice. P10 owns the local figure's derivation; this
    function only refuses to blend it.
    """
    platform = summary.platform_usd
    if platform is not None and local_usd is not None:
        state: Literal["both", "platform_only", "local_only", "neither"] = "both"
    elif platform is not None:
        state = "platform_only"
    elif local_usd is not None:
        state = "local_only"
    else:
        state = "neither"
    return RunCostComparison(
        platform_usd=platform,
        local_usd=local_usd,
        state=state,
        attribution=facts.cost_attribution if facts is not None else "run",
        local_is_lower_bound=local_price_known is False,
    )


# --------------------------------------------------------------------------
# The client
# --------------------------------------------------------------------------


class HttpExplainabilityClient:
    """P01's :class:`~aisquare.office.ports.ExplainabilityClient`, over P12.

    Each public port method returns the frozen public model; each has a
    ``read_*`` twin returning that result *plus* the facts the public shape has
    no field for. P15 can use either, and the narrow one is never made to lie
    to keep the wide one honest.
    """

    def __init__(
        self,
        *,
        transport: RunTransport,
        clock: Clock,
        correlations: CorrelationReader | None = None,
    ) -> None:
        self._transport = transport
        self._clock = clock
        self._correlations = correlations
        self._active: dict[tuple[str, int], dict[str, RunSummary]] = {}

    # -- runs --------------------------------------------------------------

    def list_runs(
        self, binding: PlatformBinding, query: PlatformQuery
    ) -> ServiceResult[Page[RunSummary]]:
        return self.read_runs(binding, query).result

    def read_runs(
        self,
        binding: PlatformBinding,
        query: PlatformQuery,
        *,
        refresh_active: bool = True,
    ) -> RunListReading:
        """One page of runs, plus the known-active runs it could not reach.

        The refresh is not an optimisation. ``since`` narrows on start time, so
        a run that began before the window and is still going simply is not in
        any page the window can produce — and an agent that vanishes from the
        office because it has been working too long is the exact failure this
        avoids.
        """
        scope = self._scope_key(binding)
        offset: int | None = None
        if query.cursor is not None:
            offset = decode_cursor(query.cursor, binding)
            if offset is None:
                return RunListReading(
                    result=self._failed(
                        binding,
                        ServiceError(
                            code="binding_required",
                            detail=sanitize_detail(
                                "this page cursor was issued for a different workspace or an "
                                "older binding revision; start the listing again"
                            ),
                            retryable=False,
                        ),
                    )
                )

        result = self._transport.request(
            "GET",
            f"/v1/workspaces/{binding.workspace_id}/runs",
            query=self._list_query(query, offset),
            binding=binding,
            timeout_class="ordinary",
        )
        if result.error is not None:
            return RunListReading(result=self._failed(binding, result.error))

        response = result.response
        assert response is not None  # TransportResult carries exactly one
        body = _object(response.json_body)
        if not 200 <= response.status_code < 300 or body is None:
            return RunListReading(result=self._from_status(binding, result))

        joined = self._verified_runs(binding)
        rows = body.get("runs")
        summaries: list[RunSummary] = []
        facts: list[RunFacts] = []
        for row in rows if isinstance(rows, list) else []:
            item = _object(row)
            if item is None:
                continue
            normalised = run_summary_of(item, binding, joined_sessions=joined)
            if normalised is None:
                continue
            summaries.append(normalised[0])
            facts.append(normalised[1])

        self._track_active(scope, summaries)
        refreshed: tuple[str, ...] = ()
        if refresh_active:
            extra, refreshed = self._refresh_active(binding, {run.run_id for run in summaries})
            summaries.extend(extra)

        items = merge_summaries(summaries)[:MAX_PAGE_ITEMS]
        studios_read = _strings(body.get("studios_read"), limit=10_000, each=128)
        studios_failed = _strings(body.get("studios_failed"), limit=10_000, each=128)
        omitted = _whole(body.get("studios_omitted"))
        next_offset = _whole(body.get("next_offset"))
        page = Page[RunSummary](
            items=items,
            next_cursor=encode_cursor(next_offset, binding) if next_offset is not None else None,
            partial=bool(studios_failed) or bool(omitted),
            coverage=coverage_of(body),
        )

        observed_at = response.received_at
        last_success = self._transport.last_success_at(binding)
        if studios_failed:
            # A failed studio is a success that covered less than the whole
            # workspace: the runs that were read are in the page, and the
            # result says so rather than discarding them as an outage.
            result_out: ServiceResult[Page[RunSummary]] = ServiceResult[Page[RunSummary]](
                status="partial",
                data=page,
                stale=False,
                observed_at=observed_at,
                last_success_at=last_success,
                error=ServiceError(
                    code="service_unavailable",
                    detail=sanitize_detail(
                        f"{len(studios_failed)} of {len(studios_read) + len(studios_failed)} "
                        "studios did not answer; this page carries the runs that were read "
                        "and the reported total is a floor"
                    ),
                    retryable=True,
                ),
            )
        else:
            result_out = service_ok(page, observed_at=observed_at, last_success_at=last_success)

        return RunListReading(
            result=result_out,
            facts=tuple(facts),
            studios_read=studios_read,
            studios_failed=studios_failed,
            studios_omitted=omitted,
            page_limit=_whole(body.get("page_limit")),
            runs_reachable=_whole(body.get("runs_reachable")),
            refreshed_run_ids=refreshed,
        )

    def get_run(self, binding: PlatformBinding, run_id: str) -> ServiceResult[RunDetail]:
        return self.read_run(binding, run_id).result

    def read_run(self, binding: PlatformBinding, run_id: str) -> RunDetailReading:
        """One run's detail. Carries no prompt text, by construction.

        ``summary_text`` is always null from this route. The only human-readable
        summary the detail body offers is the root span's output, which is
        verbatim model text; admitting it bounded would still put a transcript
        fragment in a browser payload, so it is not admitted at all.
        """
        result = self._transport.request(
            "GET",
            f"/v1/workspaces/{binding.workspace_id}/runs/{run_id}",
            binding=binding,
            timeout_class="ordinary",
        )
        if result.error is not None:
            return RunDetailReading(result=self._failed(binding, result.error))

        response = result.response
        assert response is not None
        body = _object(response.json_body)
        if not 200 <= response.status_code < 300 or body is None:
            meaning = (
                not_found_meaning(detail_of(response.json_body))
                if response.status_code == 404
                else None
            )
            return RunDetailReading(result=self._from_status(binding, result), not_found=meaning)

        row = _object(body.get("run")) or {}
        joined = self._verified_runs(binding)
        normalised = run_summary_of(row, binding, joined_sessions=joined)
        if normalised is None:
            return RunDetailReading(
                result=self._failed(
                    binding,
                    ServiceError(
                        code="upstream_invalid",
                        detail=sanitize_detail("the platform returned a run detail with no run id"),
                        retryable=False,
                    ),
                )
            )
        summary, facts = normalised
        detail = RunDetail(
            run=summary,
            summary_text=None,
            available_details=self._available_details(row),
        )
        self._track_active(self._scope_key(binding), (summary,))
        return RunDetailReading(
            result=service_ok(
                detail,
                observed_at=response.received_at,
                last_success_at=self._transport.last_success_at(binding),
            ),
            facts=facts,
            poll_after_ms=_whole(body.get("poll_after_ms")),
        )

    # -- derived documents -------------------------------------------------

    def get_policies(
        self, binding: PlatformBinding, run_id: str, *, run_state: RunState | None = None
    ) -> ServiceResult[PolicySummary]:
        return self.read_policies(binding, run_id, run_state=run_state).result

    def read_policies(
        self, binding: PlatformBinding, run_id: str, *, run_state: RunState | None = None
    ) -> PolicyReading:
        """The rule-book audit for one run.

        ``run_state`` is how a caller lets this poll on the run's own status
        rather than on prose. It is optional because the port's signature is
        P01's, and absent it the wording decides — which is precisely the
        fragile path the packet's fixtures warn about.
        """
        result = self._transport.request(
            "GET",
            f"/v1/workspaces/{binding.workspace_id}/runs/{run_id}/policies",
            binding=binding,
            timeout_class="ordinary",
        )
        if result.error is not None:
            return PolicyReading(result=self._failed(binding, result.error))

        response = result.response
        assert response is not None
        if response.status_code == 404:
            meaning, state, pollable = self._derived_404(response.json_body, run_state)
            if state is None:
                return PolicyReading(result=self._from_status(binding, result), not_found=meaning)
            return PolicyReading(
                result=service_ok(
                    PolicySummary(state=state, rows=()),
                    observed_at=response.received_at,
                    last_success_at=self._transport.last_success_at(binding),
                ),
                pollable=pollable,
                not_found=meaning,
            )

        body = _object(response.json_body)
        if not 200 <= response.status_code < 300 or body is None:
            return PolicyReading(result=self._from_status(binding, result))

        gates: list[PolicyGate] = []
        rows: list[PolicyRow] = []
        unverified = 0
        needs_review = 0
        raw_gates = body.get("gates")
        for entry in (raw_gates if isinstance(raw_gates, list) else [])[:MAX_POLICY_ROWS]:
            gate = _object(entry)
            if gate is None:
                continue
            policy_id = (
                _text(gate.get("rule_id"), limit=128)
                or _text(gate.get("gate"), limit=128)
                or _text(gate.get("name"), limit=128)
            )
            if policy_id is None:
                continue
            status, outcome = policy_status_of(gate)
            lowered = (outcome or "").lower()
            if lowered == "unverified":
                unverified += 1
            elif lowered == "needs_review":
                needs_review += 1
            gates.append(
                PolicyGate(
                    policy_id=policy_id,
                    outcome=outcome,
                    status=status,
                    triggered=_flag(gate.get("triggered")),
                    tag=_text(gate.get("tag"), limit=32),
                    degraded_reason=_text(gate.get("degraded_reason"), limit=200),
                )
            )
            rows.append(
                PolicyRow(
                    policy_id=policy_id,
                    status=status,
                    summary=_text(gate.get("name"), limit=MAX_ROW_SUMMARY_CHARS),
                )
            )

        enforcements = self._enforcements(body.get("enforcements"))
        is_governed = _flag(body.get("is_governed"))
        audited_at = _moment(body.get("aisquare_audit_at"))
        never_audited = (
            not gates and not enforcements and is_governed is None and audited_at is None
        )
        audit_state: DetailState = "not_available" if never_audited else "ready"
        return PolicyReading(
            result=service_ok(
                PolicySummary(state=audit_state, rows=() if never_audited else tuple(rows)),
                observed_at=response.received_at,
                last_success_at=self._transport.last_success_at(binding),
            ),
            gates=tuple(gates),
            enforcements=enforcements,
            is_governed=is_governed,
            audited_at=audited_at,
            unverified_count=unverified,
            needs_review_count=needs_review,
        )

    def get_reasoning(
        self,
        binding: PlatformBinding,
        run_id: str,
        kind: str,
        *,
        run_state: RunState | None = None,
    ) -> ServiceResult[ReasoningSummary]:
        return self.read_reasoning(binding, run_id, kind, run_state=run_state).result

    def read_reasoning(
        self,
        binding: PlatformBinding,
        run_id: str,
        kind: str,
        *,
        run_state: RunState | None = None,
    ) -> ReasoningReading:
        """One of three reasoning documents, whose reach genuinely differs.

        ``story`` and ``rml`` are workspace-scoped. ``chain`` exists only
        studio-scoped, so a workspace-only binding cannot read it at all — and
        the answer to that is to say so, not to serve a different document
        under the requested name.
        """
        if kind not in REASONING_KINDS:
            return ReasoningReading(
                kind=kind,
                result=self._unsupported(
                    binding,
                    f"{kind!r} is not a reasoning document this platform serves; "
                    f"known kinds are {', '.join(REASONING_KINDS)}",
                ),
            )
        if kind == "chain" and binding.studio_id is None:
            return ReasoningReading(
                kind=kind,
                result=self._unsupported(
                    binding,
                    "the reasoning chain exists only on a studio-scoped route and this "
                    "binding resolved no studio; the story and rml documents are reachable",
                ),
            )

        if kind == "story":
            path = f"/v1/workspaces/{binding.workspace_id}/runs/{run_id}/story"
            # enrich defaults to TRUE upstream and spawns a detached background
            # LLM task when no enriched copy is cached. Opening a drawer in the
            # office must not start work on the platform, or spend money there.
            query: PlatformQuery | None = PlatformQuery(filters={"enrich": "false"})
        elif kind == "rml":
            path = f"/v1/workspaces/{binding.workspace_id}/runs/{run_id}/rml"
            query = None
        else:
            path = f"/v1/studios/{binding.studio_id}/ui/runs/{run_id}/reasoning"
            query = None

        result = self._transport.request(
            "GET", path, query=query, binding=binding, timeout_class="ordinary"
        )
        if result.error is not None:
            return ReasoningReading(kind=kind, result=self._failed(binding, result.error))

        response = result.response
        assert response is not None
        if response.status_code == 404:
            meaning, state, pollable = self._derived_404(response.json_body, run_state)
            if state is None:
                return ReasoningReading(
                    kind=kind, result=self._from_status(binding, result), not_found=meaning
                )
            return ReasoningReading(
                kind=kind,
                result=service_ok(
                    ReasoningSummary(state=state, summary=None, sections=()),
                    observed_at=response.received_at,
                    last_success_at=self._transport.last_success_at(binding),
                ),
                pollable=pollable,
                not_found=meaning,
            )

        body = _object(response.json_body)
        if not 200 <= response.status_code < 300 or body is None:
            return ReasoningReading(kind=kind, result=self._from_status(binding, result))

        if kind == "story":
            reading = self._story(body)
        elif kind == "rml":
            reading = self._rml(body)
        else:
            reading = self._chain(body)
        sections, extra = reading
        return ReasoningReading(
            kind=kind,
            result=service_ok(
                ReasoningSummary(state="ready", summary=None, sections=sections),
                observed_at=response.received_at,
                last_success_at=self._transport.last_success_at(binding),
            ),
            spans_total=extra.spans_total,
            spans_projected=extra.spans_projected,
            is_enriching=extra.is_enriching,
            extraction_confidence=extra.extraction_confidence,
            low_confidence=extra.low_confidence,
        )

    # -- correlation -------------------------------------------------------

    def join(self, local_session: str, binding: PlatformBinding) -> RunJoin:
        """Whether this local session is *provably* one remote run.

        Four things must all hold, and any missing one is ``unjoined``: a
        persisted pipeline marker, a record scoped to this binding at this
        revision, a verified status naming a run, and evidence that has not
        expired. Two verified records naming different runs is ``ambiguous`` —
        which reports as unjoined, because picking one would be a guess wearing
        a verified badge.
        """
        observed = self._clock.now()
        scope = PlatformScope(
            binding_id=binding.binding_id,
            revision=binding.revision,
            workspace_id=binding.workspace_id,
            studio_id=binding.studio_id,
            agent_uid=binding.agent_uid,
        )
        if self._correlations is None:
            return RunJoin(
                state="unjoined",
                evidence="no correlation store is attached, so no join can be proven",
                scope=scope,
                observed_at=observed,
            )

        records = self._correlations.correlations_for(
            project_id=binding.project_id, session_id=local_session
        )
        scoped = [record for record in records if self._in_scope(record, binding)]
        if not scoped:
            return RunJoin(
                state="unjoined",
                evidence="no correlation was recorded for this session under this binding",
                scope=scope,
                observed_at=observed,
            )

        verified = [record for record in scoped if self._is_verified(record, observed)]
        if not verified:
            statuses = sorted({record.status for record in scoped})
            return RunJoin(
                state="unjoined",
                evidence=(
                    "correlations exist for this session but none is currently verified "
                    f"({', '.join(statuses)})"
                ),
                scope=scope,
                observed_at=observed,
            )

        run_ids = sorted({record.run_id for record in verified if record.run_id})
        if len(run_ids) > 1:
            return RunJoin(
                state="ambiguous",
                evidence=f"{len(run_ids)} verified correlations name different runs",
                scope=scope,
                observed_at=observed,
            )

        record = verified[0]
        return RunJoin(
            state="joined",
            run_id=run_ids[0],
            evidence=(
                "a verified correlation recorded by "
                f"{sanitize_detail(record.source, limit=32)} names this run under this "
                "binding revision, with a persisted pipeline marker"
            ),
            scope=scope,
            observed_at=observed,
        )

    # -- selection ---------------------------------------------------------

    def active_run_ids(self, binding: PlatformBinding) -> tuple[str, ...]:
        """Runs this adapter has seen queued or running under this binding."""
        return tuple(self._active.get(self._scope_key(binding), {}))

    def discard(self, binding: PlatformBinding | None = None) -> None:
        """Forget one binding's tracked runs, or every binding's.

        Called when the selected project, profile or workspace changes. A run
        id is meaningful only inside the binding that produced it, so carrying
        the set across a change would refresh one workspace's runs against
        another's credential.
        """
        if binding is None:
            self._active.clear()
            self._transport.invalidate()
            return
        self._active.pop(self._scope_key(binding), None)
        self._transport.invalidate(binding_id=binding.binding_id)

    # -- internals ---------------------------------------------------------

    def _scope_key(self, binding: PlatformBinding) -> tuple[str, int]:
        return (binding.binding_id, binding.revision)

    def _list_query(self, query: PlatformQuery, offset: int | None) -> PlatformQuery:
        """The upstream query, from the caller's — allow-listed, never forwarded.

        A caller's ``cursor`` is this adapter's own token and is translated into
        an offset here; an ``offset`` a caller supplied directly is dropped,
        because a paging bound that a client can set is not a bound.
        """
        filters = {key: value for key, value in query.filters.items() if key in _LIST_FILTERS}
        if offset is not None:
            # `if offset:` would drop a zero, and a cursor that decoded to 0 is
            # a cursor the caller was handed: silently sending no offset for it
            # would make one page position unreachable by the token that names
            # it. Zero is a position, not an absence.
            filters["offset"] = str(offset)
        limit = query.limit if query.limit is not None else DEFAULT_PAGE_LIMIT
        return PlatformQuery(limit=min(limit, MAX_REQUEST_LIMIT), filters=filters)

    def _available_details(
        self, row: Mapping[str, object]
    ) -> tuple[Literal["policies", "reasoning"], ...]:
        """Which detail tabs this run actually has.

        Derived from counts the run itself reports, and empty when it reports
        none: offering a tab that will answer ``not_available`` is worse than
        offering none, and a null ``summary_counts`` is "not measured" rather
        than "zero".
        """
        counts = _object(row.get("summary_counts"))
        available: list[Literal["policies", "reasoning"]] = []
        if counts is not None and (_whole(counts.get("policies")) or 0) > 0:
            available.append("policies")
        graph = _flag(row.get("graph_available"))
        spans = _whole(counts.get("spans")) if counts is not None else None
        if graph is True or (spans or 0) > 0:
            available.append("reasoning")
        return tuple(available)

    def _derived_404(
        self, json_body: object, run_state: RunState | None
    ) -> tuple[NotFoundMeaning, DetailState | None, bool]:
        """Read a 404 on a derived document: its meaning, its state, pollability.

        A ``None`` state means this is not a document state at all — the run is
        masked or absent, or the route is not there — and the caller reports the
        preserved status rather than inventing an empty success.

        The run's own state outranks the prose where the caller supplied it: a
        run that is still running has a document that may still appear, whatever
        the sentence says. The one sentence that outranks *everything* is the
        terminal one, which exists so a poller stops.
        """
        meaning = not_found_meaning(detail_of(json_body))
        if meaning in ("masked", "workspace_absent", "route_absent"):
            return meaning, None, False
        if meaning == "gone":
            return meaning, "not_available", False
        if run_state is not None and run_state not in ACTIVE_STATES:
            # The document is still absent, so the state is still `processing`
            # — but the signal we were told to poll on has stopped moving. A
            # finished run's missing audit may yet appear and may never appear,
            # and the wording admits both; polling on a run that will not move
            # again is polling on nothing, so this says so rather than
            # spinning.
            return meaning, "processing", False
        return meaning, "processing", True

    def _enforcements(self, value: object) -> tuple[EnforcementReading, ...]:
        """The live gate's decisions — kept apart from the audit's verdicts.

        ``before`` and ``after`` are not read: they carry the raw model output
        that was blocked or rewritten, which is exactly the text that must not
        reach a browser.
        """
        if not isinstance(value, list):
            return ()
        found: list[EnforcementReading] = []
        for entry in value[:MAX_ENFORCEMENTS]:
            record = _object(entry)
            if record is None:
                continue
            found.append(
                EnforcementReading(
                    span_id=_text(record.get("span_id"), limit=64),
                    action=_text(record.get("action"), limit=32),
                    outcome=_text(record.get("outcome"), limit=32),
                    violation_count=_whole(record.get("violation_count")),
                    degraded=_flag(record.get("degraded")),
                )
            )
        return tuple(found)

    def _story(
        self, body: Mapping[str, object]
    ) -> tuple[tuple[ReasoningSection, ...], _ReasoningExtras]:
        """Title and summary per moment. ``metadata`` is not read at all.

        The story is by construction a retelling of the run, and its metadata
        carries the human prompt, the system prompt and tool arguments
        verbatim. Bounding those would still publish a fragment of a
        transcript, so the whole map is skipped.
        """
        moments = body.get("moments")
        sections: list[ReasoningSection] = []
        for entry in (moments if isinstance(moments, list) else [])[:MAX_SECTIONS]:
            moment = _object(entry)
            if moment is None:
                continue
            title = _text(moment.get("title"), limit=MAX_LABEL_CHARS)
            text = _text(moment.get("summary"), limit=MAX_SECTION_CHARS)
            if title is None or text is None:
                continue
            sections.append(ReasoningSection(title=title, text=text))
        coverage = _object(body.get("coverage"))
        return tuple(sections), _ReasoningExtras(
            spans_total=_whole(coverage.get("spans_total")) if coverage else None,
            spans_projected=_whole(coverage.get("spans_projected")) if coverage else None,
            is_enriching=_flag(body.get("is_enriching")),
        )

    def _rml(
        self, body: Mapping[str, object]
    ) -> tuple[tuple[ReasoningSection, ...], _ReasoningExtras]:
        """Claims and assumptions, each as one bounded section.

        This route runs ``SELECT *`` and returns the row verbatim, so its wire
        shape is a database table's current columns and any new one appears
        without warning. Admitting two known keys and ignoring the rest is the
        only stable reading available.
        """
        sections: list[ReasoningSection] = []
        for title, key in (("Claims", "claims"), ("Assumptions", "assumptions")):
            entries = _strings(body.get(key), limit=MAX_SECTIONS, each=MAX_SECTION_CHARS)
            if not entries:
                continue
            sections.append(
                ReasoningSection(title=title, text="\n".join(entries)[:MAX_SECTION_CHARS])
            )
        return tuple(sections), _ReasoningExtras(
            extraction_confidence=_number(body.get("extraction_confidence")),
            low_confidence=_flag(body.get("low_confidence")),
        )

    def _chain(
        self, body: Mapping[str, object]
    ) -> tuple[tuple[ReasoningSection, ...], _ReasoningExtras]:
        """One section per span that carries an extraction.

        ``has_rml`` is an explicit boolean upstream, so a span without an
        extraction is a stated fact rather than an inference from a missing
        key — and a span with none contributes no section rather than an empty
        one.
        """
        chain = body.get("chain")
        sections: list[ReasoningSection] = []
        for entry in (chain if isinstance(chain, list) else [])[:MAX_SECTIONS]:
            span = _object(entry)
            if span is None or _flag(span.get("has_rml")) is not True:
                continue
            rml = _object(span.get("rml"))
            if rml is None:
                continue
            claims = _strings(rml.get("claims"), limit=MAX_SECTIONS, each=MAX_SECTION_CHARS)
            if not claims:
                continue
            title = _text(span.get("span_name"), limit=MAX_LABEL_CHARS) or "span"
            sections.append(
                ReasoningSection(title=title, text="\n".join(claims)[:MAX_SECTION_CHARS])
            )
        return tuple(sections), _ReasoningExtras()

    def _track_active(self, scope: tuple[str, int], runs: Sequence[RunSummary]) -> None:
        tracked = self._active.setdefault(scope, {})
        for run in runs:
            if run.state in ACTIVE_STATES:
                tracked[run.run_id] = run
            else:
                tracked.pop(run.run_id, None)

    def _refresh_active(
        self, binding: PlatformBinding, seen: set[str]
    ) -> tuple[list[RunSummary], tuple[str, ...]]:
        """Re-read known-active runs the page did not contain."""
        scope = self._scope_key(binding)
        pending = [run_id for run_id in self._active.get(scope, {}) if run_id not in seen]
        found: list[RunSummary] = []
        refreshed: list[str] = []
        for run_id in pending[:MAX_ACTIVE_REFRESH]:
            reading = self.read_run(binding, run_id)
            refreshed.append(run_id)
            data = reading.result.data
            if data is not None:
                found.append(data.run)
            elif reading.not_found in ("masked", "gone"):
                # The run is no longer readable under this binding. Keeping it
                # tracked would re-request it on every refresh forever.
                self._active.get(scope, {}).pop(run_id, None)
        return found, tuple(refreshed)

    def _verified_runs(self, binding: PlatformBinding) -> dict[str, str]:
        """Remote run id to local session id, for verified joins only.

        Ambiguity is dropped rather than resolved: if two sessions both claim
        one run, neither is attached. The map is keyed by run id because that
        is what a page row carries, and a run with no session — a verified
        record that never recorded one — cannot be attached to anything.
        """
        if self._correlations is None:
            return {}
        now = self._clock.now()
        claims: dict[str, set[str]] = {}
        for record in self._correlations.correlations_for(project_id=binding.project_id):
            if not self._in_scope(record, binding) or not self._is_verified(record, now):
                continue
            if record.run_id is None or record.session_id is None:
                continue
            claims.setdefault(record.run_id, set()).add(record.session_id)
        return {
            run_id: next(iter(sessions))
            for run_id, sessions in claims.items()
            if len(sessions) == 1
        }

    def _in_scope(self, record: CorrelationRecord, binding: PlatformBinding) -> bool:
        """Both the binding *and* its revision must match.

        The revision moves when the profile, endpoint, workspace, studio, agent
        or credential moves, so this is what stops a join recorded against a
        rotated key or a repointed gateway from surviving into a scope where it
        was never proven.
        """
        return (
            record.binding_id == binding.binding_id and record.binding_revision == binding.revision
        )

    def _is_verified(self, record: CorrelationRecord, now: datetime) -> bool:
        if record.status != "verified" or not record.run_id or not record.pipeline_marker:
            return False
        return record.expires_at is None or record.expires_at > now

    def _failed(self, binding: PlatformBinding, error: ServiceError) -> ServiceResult[T]:
        failed: ServiceResult[T] = service_failed(
            status_for_error(error.code),
            error,
            last_success_at=self._transport.last_success_at(binding),
        )
        return failed

    def _from_status(self, binding: PlatformBinding, result: TransportResult) -> ServiceResult[T]:
        """A non-2xx response, read for availability and never for existence.

        The 404 keeps P12's ``not_found_or_masked`` prefix rather than becoming
        a 403 or a global failure: a response that distinguished "absent" from
        "another tenant's" would confirm that the other tenant's run exists.
        """
        response = result.response
        assert response is not None
        status, error = classify_status(response.status_code, detail=detail_of(response.json_body))
        if error is None:  # a 2xx with a body this adapter could not read
            error = ServiceError(
                code="upstream_invalid",
                detail=sanitize_detail("the platform answered with a body Office could not read"),
                retryable=False,
            )
            status = "unavailable"
        failed: ServiceResult[T] = service_failed(
            status, error, last_success_at=self._transport.last_success_at(binding)
        )
        return failed

    def _unsupported(self, binding: PlatformBinding, reason: str) -> ServiceResult[T]:
        """A capability fact, refused before a request is spent discovering it."""
        result: ServiceResult[T] = service_failed(
            "unsupported",
            ServiceError(
                code="unsupported_capability",
                detail=sanitize_detail(reason),
                retryable=False,
            ),
            last_success_at=self._transport.last_success_at(binding),
        )
        return result


__all__ = [
    "ACTIVE_STATES",
    "DEFAULT_PAGE_LIMIT",
    "FORBIDDEN_TEXT_FIELDS",
    "MAX_ACTIVE_REFRESH",
    "MAX_PAGE_ITEMS",
    "NOT_FOUND_OR_MASKED",
    "REASONING_KINDS",
    "CorrelationReader",
    "EnforcementReading",
    "HttpExplainabilityClient",
    "NotFoundMeaning",
    "PolicyGate",
    "PolicyReading",
    "ReasoningKind",
    "ReasoningReading",
    "RunCostComparison",
    "RunDetailReading",
    "RunFacts",
    "RunListReading",
    "RunTransport",
    "compare_costs",
    "coverage_of",
    "decode_cursor",
    "encode_cursor",
    "merge_summaries",
    "not_found_meaning",
    "policy_status_of",
    "run_state_of",
    "run_summary_of",
    "status_for_error",
]
