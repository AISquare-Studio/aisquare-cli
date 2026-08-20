"""Per-turn metrics for the CI test bed: record on the hot path, aggregate on read.

One row per turn, opened by ``UserPromptSubmit`` and closed by ``Stop``. A row
is written whether or not CI was consulted, which is the point: the stretch
before the endpoint goes live is not a gap in the record, it is the **baseline**
— the same instrumentation over real sessions with no CI in the loop. Nothing
else produces the noise floor, and without it the first "CI helped 8%" has
nothing to be compared against.

Two rules hold everything else up:

**Recording never disrupts a session.** Every entry point here swallows its own
failures. A metrics write is the least important thing happening when a
developer submits a prompt, and it is not permitted to be the thing that breaks
it.

**A degradation reason is always recorded with the action.** The two travel
together or the data is unreadable later — see :class:`TurnMetric`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from aisquare.core.ids import new_trace_id
from aisquare.core.store import ContextStore, store_session
from aisquare.models import MetricsSummary, TurnMetric
from aisquare.services.ci_client import Call


def open_turn(
    project_id: str,
    *,
    session_id: str | None,
    call: Call | None = None,
    injected_chars: int | None = None,
    store: ContextStore | None = None,
) -> str | None:
    """Record the start of a turn; returns its trace id, or ``None`` on failure.

    ``call`` is the CI outcome for this turn when one was attempted. Passing
    ``None`` records a turn CI never saw, which is what every row looks like
    until the endpoint is live.
    """
    metric = TurnMetric(
        trace_id=new_trace_id(),
        project_id=project_id,
        session_id=session_id,
        started_at=datetime.now(tz=UTC),
        injected_chars=injected_chars,
        **_call_fields(call),
    )
    try:
        if store is not None:
            store.open_turn(metric)
        else:
            with store_session() as owned:
                owned.open_turn(metric)
    except Exception:  # never disrupt the session for a metrics write
        return None
    return metric.trace_id


def close_turn(session_id: str | None, *, store: ContextStore | None = None) -> None:
    """Close this session's newest open turn. Silent when there is nothing open."""
    if not session_id:
        return
    ended_at = datetime.now(tz=UTC)
    try:
        if store is not None:
            store.close_turn(session_id, ended_at=ended_at)
        else:
            with store_session() as owned:
                owned.close_turn(session_id, ended_at=ended_at)
    except Exception:  # never disrupt the session for a metrics write
        return


def _call_fields(call: Call | None) -> dict[str, object]:
    """The CI columns for ``call``, or the "never asked" defaults."""
    if call is None:
        return {}
    return {
        "ci_action": call.action.value,
        "degradation_reason": call.reason.value,
        "cache_hit": call.cache_hit,
        "server_ms": call.server_ms,
        "round_trip_ms": call.round_trip_ms,
        "budget_breach": call.reason.value == "backstop_exceeded",
    }


def recent(
    *, project_id: str | None = None, session_id: str | None = None, limit: int = 500
) -> list[TurnMetric]:
    """Recorded turns, newest first."""
    with store_session() as store:
        return store.turn_metrics(project_id=project_id, session_id=session_id, limit=limit)


def summarize(turns: list[TurnMetric], *, project_id: str | None = None) -> MetricsSummary:
    """Aggregate recorded turns. Computed here, never on the write path."""
    summary = MetricsSummary(turns=len(turns), project_id=project_id)
    if not turns:
        return summary

    for turn in turns:
        summary.by_action[turn.ci_action] = summary.by_action.get(turn.ci_action, 0) + 1
        reason = turn.degradation_reason
        summary.by_reason[reason] = summary.by_reason.get(reason, 0) + 1
        if reason == "none":
            summary.ci_consulted += 1
        elif reason not in ("disabled", "not_configured"):
            # "off" and "unconfigured" are not degradations of anything — the
            # client was never asked. Counting them would report a baseline run
            # as one where every call failed.
            summary.degraded += 1
        if turn.cache_hit:
            summary.cache_hits += 1
        if turn.budget_breach:
            summary.budget_breaches += 1
        if turn.injected_chars:
            summary.injected_turns += 1
        if turn.tokens_in is not None or turn.tokens_out is not None:
            summary.turns_with_tokens += 1

    summary.median_wall_ms = _percentile([t.wall_ms for t in turns], 50)
    round_trips = [t.round_trip_ms for t in turns if t.degradation_reason != "disabled"]
    summary.median_round_trip_ms = _percentile(round_trips, 50)
    summary.p95_round_trip_ms = _percentile(round_trips, 95)
    return summary


def _percentile(values: list[int | None], percentile: int) -> int | None:
    """The ``percentile``th value, ignoring rows that never recorded one.

    Nearest-rank rather than interpolated: these are millisecond counts read by
    people deciding whether an endpoint is too slow, and a real observed value
    is easier to trust than an average of two that never happened.
    """
    present = sorted(value for value in values if value is not None)
    if not present:
        return None
    rank = max(1, (percentile * len(present) + 99) // 100)
    return present[min(rank, len(present)) - 1]


def summary_for(project_id: str | None = None, *, limit: int = 500) -> MetricsSummary:
    """Summarise the most recent turns, optionally scoped to one project."""
    return summarize(recent(project_id=project_id, limit=limit), project_id=project_id)
