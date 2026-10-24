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
and its zero would make a 300 ms endpoint read as instant. Rows delivered under
the staging override are counted apart and kept out of the percentiles: they
are a connectivity instrument, and a latency quoted from them is a number the
experiment never measured.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from statistics import median

from aisquare.core import paths, pricing
from aisquare.core import usage as usage_core
from aisquare.core.ids import new_trace_id, new_usage_id
from aisquare.core.store import ContextStore, store_session
from aisquare.core.workspace import active_project
from aisquare.models import (
    BASELINE_REASONS,
    BY_DESIGN_REASONS,
    FAILURE_REASONS,
    ClientReason,
    HookTrigger,
    MetricsSummary,
    SessionUsage,
    TokenSummary,
    TurnMetric,
    TurnUsage,
    UsageTrigger,
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


def close_turn(session_id: str | None, *, store: ContextStore | None = None) -> str | None:
    """Close this session's newest open turn; its trace id, or ``None`` when nothing
    was open (or the write failed). Silent either way."""
    if not session_id:
        return None
    ended_at = datetime.now(tz=UTC)
    try:
        if store is not None:
            closed = store.close_turn(session_id, ended_at=ended_at)
        else:
            with store_session() as owned:
                closed = owned.close_turn(session_id, ended_at=ended_at)
    except Exception:  # never disrupt the session for a metrics write
        return None
    return closed.trace_id if closed is not None else None


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
        if turn.delivery_source is not None:
            _count(summary.by_delivery_source, turn.delivery_source)
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
    consulted = [
        t.round_trip_ms
        for t in turns
        if t.client_reason is ClientReason.none and t.delivery_source != "override"
    ]
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


# --- token usage, read off the transcript ---------------------------------------------

_LOG_LIMIT_BYTES = 1_000_000


def record_usage(
    session_id: str | None,
    transcript_path: str | None,
    cwd: Path | None,
    *,
    trigger: UsageTrigger,
    trace_id: str | None = None,
    ended: bool = False,
    agent: str | None = None,
) -> TurnUsage | None:
    """Read what the session's transcript added since the last record, and store it.

    Called by the Stop, StopFailure and SessionEnd hooks. Reads only the bytes
    appended since the session's previous record, through the reader of the session's
    ``agent`` (``AgentTranscript.usage``, the default agent's when none is named: Claude
    Code's session JSONL, Codex's rollout), priced off its own list; an agent whose
    transcript holds no token record records nothing (SCOPE #113 274, L2). Writes
    one turn row for them and folds them into the session's row, and fills the
    ``metric`` row the turn closed (``trace_id``) with the counts it was
    waiting for. Returns the turn row, or ``None`` when there was nothing new,
    nothing to read, or anything at all went wrong — NEVER raises: a failure is
    appended to ``log/metrics.log`` and costs the record, not the session.
    """
    if not session_id or not transcript_path:
        return None
    try:
        return _record_usage(
            session_id,
            Path(transcript_path),
            cwd,
            trigger=trigger,
            trace_id=trace_id,
            ended=ended,
            agent=agent,
        )
    except Exception as exc:  # never disrupt the session for a metrics write
        _log_failure(f"usage not recorded for {session_id} ({trigger})", exc)
        return None


def _record_usage(
    session_id: str,
    transcript: Path,
    cwd: Path | None,
    *,
    trigger: UsageTrigger,
    trace_id: str | None,
    ended: bool,
    agent: str | None,
) -> TurnUsage | None:
    # Lazy: services.project imports the store layer this module is imported by, and the
    # adapters load only for a record.
    from aisquare.core.agent_adapters import recorded_adapter
    from aisquare.services import project as project_service

    adapter = recorded_adapter(agent)
    reader = adapter.transcript_reader if adapter is not None else None
    if reader is None:
        return None  # an agent this install does not know, or no reader: nothing is read

    now = datetime.now(tz=UTC)
    with store_session() as store:
        existing = store.session_usage(session_id)
        if existing is None:
            # The project is resolved once, at the session's first record: it costs a
            # `git rev-parse`, and a session does not change projects.
            project = active_project(store, cwd)
            if not project_service.tracked(store, project):
                return None
            project_id = project.id
        else:
            project_id = existing.project_id
        began = time.perf_counter()
        scan = reader.usage(transcript, existing.state if existing else None)
        scan_ms = (time.perf_counter() - began) * 1000
        if scan is None:
            return None  # no token record in this agent's transcript: never a 0-token row
        delta = scan.delta
        if delta.is_empty() and not ended and existing is not None:
            return None
        total = usage_core.Usage.from_dict(existing.usage) if existing else usage_core.Usage()
        total.add(delta)
        session_totals = total.totals()
        turn_totals = delta.totals()
        turns = (existing.turns if existing else 0) + (0 if delta.is_empty() else 1)
        session = SessionUsage(
            session_id=session_id,
            project_id=project_id,
            transcript_path=str(transcript),
            state=scan.state,
            usage={**total.to_dict(), "first_edit": scan.first_edit.as_dict()},
            turns=turns,
            total_tokens=session_totals.total,
            weighted=session_totals.weighted,
            cost_usd=session_totals.cost_usd,
            started_at=existing.started_at if existing else now,
            updated_at=now,
            # A turn recorded after the session ended is the session running again (a
            # `claude --resume` of the same id): no longer ended (#45, n3). A record that is
            # not an end and adds nothing returned above.
            ended_at=now if ended else None,
        )
        turn = (
            None
            if delta.is_empty()
            else TurnUsage(
                id=new_usage_id(),
                session_id=session_id,
                project_id=project_id,
                trace_id=trace_id,
                seq=turns,
                trigger=trigger,
                recorded_at=now,
                usage=delta.to_dict(),
                total_tokens=turn_totals.total,
                weighted=turn_totals.weighted,
                cost_usd=turn_totals.cost_usd,
                scan_ms=round(scan_ms, 3),
            )
        )
        counts = (
            turn_totals.input + turn_totals.cache_write + turn_totals.cache_read,
            turn_totals.output,
            delta.tool_calls,
        )
        written = store.record_usage(
            session,
            turn,
            previous_state=existing.state if existing else None,
            metric_counts=counts,
        )
    return turn if written else None


def _log_failure(what: str, exc: BaseException) -> None:
    """Append one line to ``log/metrics.log``; silent if even that fails.

    Not stderr: a Stop hook's stderr reaches the operator's screen in verbose
    mode on every turn, and a metrics failure is not worth that. The file is
    kept under :data:`_LOG_LIMIT_BYTES` by rotating it to ``metrics.log.1``.
    """
    try:
        directory = paths.log_dir()
        directory.mkdir(parents=True, exist_ok=True)
        log = directory / "metrics.log"
        if log.exists() and log.stat().st_size > _LOG_LIMIT_BYTES:
            log.replace(directory / "metrics.log.1")
        stamp = datetime.now(tz=UTC).isoformat(timespec="seconds")
        with log.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {what}: {type(exc).__name__}: {exc}\n")
    except Exception:  # the log is the last resort; there is nothing behind it
        return


def session_usages(
    *, project_id: str | None = None, session_id: str | None = None
) -> list[SessionUsage]:
    """Recorded sessions' usage, most recently updated first."""
    with store_session() as store:
        return store.session_usages(project_id=project_id, session_id=session_id)


def summarize_tokens(
    sessions: list[SessionUsage],
    *,
    project_id: str | None = None,
    session_id: str | None = None,
) -> TokenSummary:
    """Sum recorded sessions into a :class:`~aisquare.models.TokenSummary`."""
    summary = TokenSummary(
        project_id=project_id,
        session_id=session_id,
        sessions=len(sessions),
        prices_as_of=pricing.PRICES_AS_OF,
        prices_source=pricing.PRICES_SOURCE,
    )
    total = usage_core.Usage()
    before_tokens: list[int] = []
    before_calls: list[int] = []
    for session in sessions:
        total.add(usage_core.Usage.from_dict(session.usage))
        summary.turns += session.turns
        first_edit = session.usage.get("first_edit")
        if isinstance(first_edit, dict) and first_edit.get("edited"):
            summary.sessions_with_edit += 1
            before_tokens.append(int(first_edit.get("tokens", 0)))
            before_calls.append(int(first_edit.get("tool_calls", 0)))
    for name, model in sorted(total.models.items()):
        summary.models[name] = model.as_dict()
        summary.requests += model.requests
        summary.input_tokens += model.input
        summary.cache_write_tokens += model.cache_write
        summary.cache_read_tokens += model.cache_read
        summary.output_tokens += model.output
        if model.priced:
            summary.weighted_tokens += model.weighted
            summary.cost_usd += model.cost_usd
        else:
            summary.unpriced_models.append(name)
            summary.unpriced_tokens += model.total
    summary.total_tokens = (
        summary.input_tokens
        + summary.cache_write_tokens
        + summary.cache_read_tokens
        + summary.output_tokens
    )
    summary.tools = dict(sorted(total.tools.items(), key=lambda item: (-item[1], item[0])))
    summary.tool_calls = total.tool_calls
    summary.tool_result_chars = total.tool_result_chars
    summary.lookup_calls = total.lookup_calls
    summary.injected_chars = total.injected_chars
    if before_tokens:
        summary.median_tokens_before_edit = int(median(before_tokens))
        summary.median_tool_calls_before_edit = int(median(before_calls))
    summary.price_lists = _price_lists(name for name, model in total.models.items() if model.priced)
    if len(summary.price_lists) == 1:
        [only] = summary.price_lists
        summary.prices_as_of, summary.prices_source = only["as_of"], only["source"]
    return summary


def _price_lists(models: Iterable[str]) -> list[dict[str, str]]:
    """The price list of every agent whose table prices one of ``models``, in adapter order:
    each agent's reader priced its own models off it (``AgentAdapter.price_table``)."""
    from aisquare.core.agent_adapters import adapters

    names = list(models)
    lists: list[dict[str, str]] = []
    for adapter in adapters():
        table = adapter.price_table
        if table is not None and any(table.canonical(name) in table.prices for name in names):
            lists.append({"agent": adapter.label, "as_of": table.as_of, "source": table.source})
    return lists
