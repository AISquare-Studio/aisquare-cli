"""Per-turn metrics for the CI test bed: record on the hot path, aggregate on read.

One row per turn, opened by ``UserPromptSubmit`` and closed by ``Stop``. A row
is written whether or not CI was consulted, which is the point: the stretch
before the endpoint goes live is not a gap in the record, it is the **baseline**
— the same instrumentation over real sessions with no CI in the loop. Nothing
else produces the noise floor, and without it the first "CI helped 8%" has
nothing to be compared against.

Three rules hold everything else up:

**Recording never disrupts a session.** Every entry point here swallows its own
failures. A metrics write is the least important thing happening when a
developer submits a prompt, and it is not permitted to be the thing that breaks
it.

**The client reason is always recorded beside the server status.** The two
travel together or the data is unreadable later — see :class:`TurnMetric`.

**Aggregates keep the three reason groups apart.** Baseline rows (the client
never asked), by-design skips (it chose not to) and failures (it tried) are
counted separately and never summed; round-trip percentiles are taken over
consulted rows only, because a row that made no round trip has no round trip,
and its zero would make a 300 ms endpoint read as instant.
"""

from __future__ import annotations

from datetime import UTC, datetime

from aisquare.core.ids import new_trace_id
from aisquare.core.store import ContextStore, store_session
from aisquare.core.workspace import active_project
from aisquare.models import (
    BASELINE_REASONS,
    BY_DESIGN_REASONS,
    FAILURE_REASONS,
    ClientReason,
    HookTrigger,
    MetricsSummary,
    TurnMetric,
)


def baseline_turn(
    project_id: str,
    *,
    session_id: str | None,
    reason: ClientReason,
    trigger: HookTrigger = "prompt_submit",
) -> TurnMetric:
    """A row for a turn the client never consulted CI about.

    ``reason`` says why — ``disabled`` for every ordinary user, ``not_configured``
    or ``no_run`` for a half-configured machine. It is deliberately not a
    failure: nothing was tried.
    """
    return TurnMetric(
        trace_id=new_trace_id(),
        project_id=project_id,
        session_id=session_id,
        started_at=datetime.now(tz=UTC),
        trigger=trigger,
        client_reason=reason,
    )


def open_turn(metric: TurnMetric, *, store: ContextStore | None = None) -> str | None:
    """Record ``metric``; returns its trace id, or ``None`` when the write failed."""
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


def recent(
    *, project_id: str | None = None, session_id: str | None = None, limit: int = 500
) -> list[TurnMetric]:
    """Recorded turns, newest first."""
    with store_session() as store:
        return store.turn_metrics(project_id=project_id, session_id=session_id, limit=limit)


def resolve_scope(project: str | None, *, all_projects: bool) -> str | None:
    """The project id ``metrics`` commands report on, or ``None`` for every project.

    The default is the active project of the current directory — the unit an
    experiment compares — rather than every turn on the machine; ``--all`` widens
    it and ``--project`` names another by name or id prefix.
    """
    if all_projects:
        return None
    with store_session() as store:
        if project is None:
            return active_project(store).id
        matches = store.find_projects(project)
    if len(matches) == 1:
        return matches[0].id
    if not matches:
        raise ValueError(f"no project matches {project!r}")
    names = ", ".join(sorted(f"{match.root.name} ({match.id})" for match in matches))
    raise ValueError(f"{project!r} is ambiguous: {names}")


def summarize(turns: list[TurnMetric], *, project_id: str | None = None) -> MetricsSummary:
    """Aggregate recorded turns. Computed here, never on the write path."""
    summary = MetricsSummary(turns=len(turns), project_id=project_id)
    if not turns:
        return summary

    for turn in turns:
        reason = turn.client_reason
        _count(summary.by_reason, reason.value)
        if turn.status is not None:
            _count(summary.by_status, turn.status)
        if turn.action is not None:
            _count(summary.by_action, turn.action)
        if turn.trigger is not None:
            _count(summary.by_trigger, turn.trigger)
        if reason is ClientReason.none:
            summary.consulted += 1
        elif reason in BASELINE_REASONS:
            summary.baseline += 1
        elif reason in BY_DESIGN_REASONS:
            summary.skipped += 1
        elif reason in FAILURE_REASONS:
            summary.failed += 1
        if turn.deadline_breached:
            summary.deadline_breaches += 1
        if turn.injected_chars:
            summary.injected_turns += 1
        if turn.tokens_in is not None or turn.tokens_out is not None:
            summary.turns_with_tokens += 1

    summary.median_wall_ms = percentile([t.wall_ms for t in turns], 50)
    consulted = [t.round_trip_ms for t in turns if t.client_reason is ClientReason.none]
    summary.median_round_trip_ms = percentile(consulted, 50)
    summary.p95_round_trip_ms = percentile(consulted, 95)
    return summary


def _count(bucket: dict[str, int], key: str) -> None:
    bucket[key] = bucket.get(key, 0) + 1


def percentile(values: list[int | None], rank_percent: int) -> int | None:
    """The nearest-rank ``rank_percent``th value, ignoring rows that never recorded one.

    Nearest-rank rather than interpolated: these are millisecond counts read by
    people deciding whether an endpoint is too slow, and a real observed value
    is easier to trust than an average of two that never happened. For
    ``[10, 20, 30, 400]`` the median is ``20`` — ``ceil(0.5 * 4) = 2``nd value.
    """
    present = sorted(value for value in values if value is not None)
    if not present:
        return None
    rank = max(1, (rank_percent * len(present) + 99) // 100)
    return present[min(rank, len(present)) - 1]
