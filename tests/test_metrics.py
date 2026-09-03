"""Per-turn metrics: the row exists whether or not CI ran, and never breaks a turn.

The baseline case is the one that matters most here. Before the endpoint is
live every turn records ``disabled`` — and that is data, not a gap: it is the
noise floor the first comparative claim will have to clear.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core.store import MAX_OPEN_TURN, ContextStore, open_store, store_session
from aisquare.models import ClientReason, ProjectInfo, TurnMetric
from aisquare.services import hooks as hooks_service
from aisquare.services import metrics as metrics_service
from aisquare.services import team as team_service

PROJECT = ProjectInfo(id="prj_metrics", root=Path("/tmp/metrics"), linked_repos=[])


@pytest.fixture
def store() -> Iterator[ContextStore]:
    """Closed on teardown: an open sqlite handle makes ``tmp_path`` teardown fail on Windows."""
    with store_session() as opened:
        opened.ensure_project(PROJECT)
        yield opened


def _turn(**kwargs: object) -> TurnMetric:
    base: dict[str, object] = {
        "trace_id": f"trc_{kwargs.pop('n', 0)}",
        "project_id": PROJECT.id,
        "session_id": "ses_1",
        "started_at": datetime.now(tz=UTC),
        "trigger": "prompt_submit",
    }
    base.update(kwargs)
    return TurnMetric(**base)


def _consulted(n: int, **kwargs: object) -> TurnMetric:
    fields: dict[str, object] = {
        "client_reason": ClientReason.none,
        "status": "served",
        "action": "inject",
        "round_trip_ms": 120,
        "server_ms": 40,
        "query_id": "qry_x",
    }
    fields.update(kwargs)
    return _turn(n=n, **fields)


# --- the row ------------------------------------------------------------------


def test_a_turn_is_recorded_even_though_ci_never_ran(store: ContextStore) -> None:
    """The baseline case. This is the whole reason metrics land before CI does."""
    metric = metrics_service.baseline_turn(
        PROJECT.id, session_id="ses_1", reason=ClientReason.disabled
    )
    assert metrics_service.open_turn(metric, store=store) == metric.trace_id
    (turn,) = store.turn_metrics(project_id=PROJECT.id)
    assert turn.client_reason is ClientReason.disabled
    assert turn.status is None and turn.action is None
    assert turn.trigger == "prompt_submit"


def test_a_consulted_turn_records_the_verdict_and_the_join_keys_together(
    store: ContextStore,
) -> None:
    metrics_service.open_turn(
        _consulted(
            1,
            briefing_id="brf_x",
            config_fingerprint="sha256:" + "a" * 64,
            error_codes=["deadline_exceeded"],
        ),
        store=store,
    )
    (turn,) = store.turn_metrics(project_id=PROJECT.id)
    assert turn.client_reason is ClientReason.none
    assert turn.status == "served" and turn.action == "inject"
    assert turn.query_id == "qry_x" and turn.briefing_id == "brf_x"
    assert turn.server_ms == 40 and turn.round_trip_ms == 120
    assert turn.error_codes == ["deadline_exceeded"]


def test_a_failure_and_an_empty_answer_are_distinguishable(store: ContextStore) -> None:
    """The failure this column exists to prevent: both inject nothing, and
    without the reason an endpoint failing every call reads as a clean baseline."""
    metrics_service.open_turn(
        _turn(n=1, client_reason=ClientReason.deadline_exceeded, deadline_breached=True),
        store=store,
    )
    metrics_service.open_turn(_consulted(2, status="empty", action="noop"), store=store)
    turns = store.turn_metrics(project_id=PROJECT.id)
    assert {t.injected_chars for t in turns} == {None}
    assert {t.client_reason for t in turns} == {ClientReason.deadline_exceeded, ClientReason.none}
    assert {t.deadline_breached for t in turns} == {True, None}


def test_token_columns_stay_null_rather_than_fabricated(store: ContextStore) -> None:
    metrics_service.open_turn(_consulted(1), store=store)
    (turn,) = store.turn_metrics(project_id=PROJECT.id)
    assert turn.tokens_in is None and turn.tokens_out is None and turn.tool_calls is None


def test_the_row_has_nowhere_to_put_an_arm() -> None:
    """The client never sees an arm; a column would be a place to leak one into."""
    for forbidden in ("arm", "architecture", "flags_hash", "source_kind", "reader"):
        assert forbidden not in TurnMetric.model_fields


def test_a_bad_vocabulary_value_is_refused_at_the_row(store: ContextStore) -> None:
    with pytest.raises(ValueError):
        _turn(n=1, client_reason="allow")
    with pytest.raises(ValueError):
        _turn(n=1, status="ok")


def test_a_duplicate_trace_id_raises_rather_than_replacing(store: ContextStore) -> None:
    import sqlite3

    store.open_turn(_turn(n=1))
    with pytest.raises(sqlite3.IntegrityError):
        store.open_turn(_turn(n=1))


# --- closing ------------------------------------------------------------------


def test_stop_closes_the_newest_open_turn_with_its_real_elapsed_time(store: ContextStore) -> None:
    started = datetime.now(tz=UTC) - timedelta(seconds=5)
    metrics_service.open_turn(_turn(n=1, started_at=started), store=store)
    metrics_service.close_turn("ses_1", store=store)
    (turn,) = store.turn_metrics(project_id=PROJECT.id)
    assert turn.ended_at is not None
    assert turn.wall_ms is not None and 4_500 <= turn.wall_ms <= 6_500


def test_closing_twice_leaves_the_first_close_alone(store: ContextStore) -> None:
    metrics_service.open_turn(_turn(n=1), store=store)
    metrics_service.close_turn("ses_1", store=store)
    (first,) = store.turn_metrics(project_id=PROJECT.id)
    time.sleep(0.01)
    metrics_service.close_turn("ses_1", store=store)
    (again,) = store.turn_metrics(project_id=PROJECT.id)
    assert again.ended_at == first.ended_at and again.wall_ms == first.wall_ms


def test_two_stops_racing_the_same_row_close_it_once(store: ContextStore) -> None:
    """Compare-and-set on ``ended_at IS NULL``: the loser reports None instead
    of overwriting a 10 s turn with a 40 s one."""
    metrics_service.open_turn(
        _turn(n=1, started_at=datetime.now(tz=UTC) - timedelta(seconds=10)), store=store
    )
    other = open_store()
    try:
        first = store.close_turn("ses_1", ended_at=datetime.now(tz=UTC))
        second = other.close_turn("ses_1", ended_at=datetime.now(tz=UTC) + timedelta(seconds=30))
    finally:
        other.close()
    assert first is not None and first.wall_ms is not None and first.wall_ms < 15_000
    assert second is None


def test_an_orphaned_row_older_than_the_bound_is_left_open(store: ContextStore) -> None:
    """A killed terminal's row must not absorb a three-day gap into the median."""
    stale = datetime.now(tz=UTC) - MAX_OPEN_TURN - timedelta(hours=1)
    metrics_service.open_turn(_turn(n=1, started_at=stale), store=store)
    assert store.close_turn("ses_1", ended_at=datetime.now(tz=UTC)) is None
    (turn,) = store.turn_metrics(project_id=PROJECT.id)
    assert turn.ended_at is None and turn.wall_ms is None


def test_a_backward_clock_step_records_null_not_zero(store: ContextStore) -> None:
    metrics_service.open_turn(
        _turn(n=1, started_at=datetime.now(tz=UTC) + timedelta(hours=1)), store=store
    )
    closed = store.close_turn("ses_1", ended_at=datetime.now(tz=UTC))
    assert closed is not None
    assert closed.ended_at is not None and closed.wall_ms is None


def test_a_row_closed_at_creation_is_never_picked_up_by_a_later_stop(store: ContextStore) -> None:
    now = datetime.now(tz=UTC)
    metrics_service.open_turn(
        _turn(n=1, trigger="session_start", started_at=now, ended_at=now), store=store
    )
    assert store.close_turn("ses_1", ended_at=datetime.now(tz=UTC)) is None


def test_a_turn_that_never_stopped_stays_open(store: ContextStore) -> None:
    metrics_service.open_turn(_turn(n=1), store=store)
    metrics_service.open_turn(_turn(n=2), store=store)
    metrics_service.close_turn("ses_1", store=store)
    turns = store.turn_metrics(project_id=PROJECT.id)
    assert sum(1 for t in turns if t.ended_at is None) == 1


def test_closing_an_unknown_session_is_silent(store: ContextStore) -> None:
    metrics_service.close_turn("ses_never_seen", store=store)
    assert store.turn_metrics() == []


def test_sessions_close_their_own_turns(store: ContextStore) -> None:
    metrics_service.open_turn(_turn(n=1, session_id="ses_a"), store=store)
    metrics_service.open_turn(_turn(n=2, session_id="ses_b"), store=store)
    metrics_service.close_turn("ses_a", store=store)
    open_sessions = {t.session_id for t in store.turn_metrics() if t.ended_at is None}
    assert open_sessions == {"ses_b"}


def test_a_negative_limit_reads_one_row_not_the_whole_table(store: ContextStore) -> None:
    for n in range(3):
        metrics_service.open_turn(_turn(n=n), store=store)
    assert len(store.turn_metrics(limit=-1)) == 1
    assert len(store.turn_metrics(limit=0)) == 1
    assert len(store.turn_metrics(limit=2)) == 2


# --- recording never breaks a turn --------------------------------------------


def test_a_broken_store_costs_the_row_not_the_turn() -> None:
    class Exploding:
        def open_turn(self, metric: TurnMetric) -> TurnMetric:
            raise RuntimeError("disk is on fire")

        def close_turn(self, session_id: str, *, ended_at: datetime) -> None:
            raise RuntimeError("disk is still on fire")

    broken = Exploding()
    assert metrics_service.open_turn(_turn(n=1), store=broken) is None  # type: ignore[arg-type]
    metrics_service.close_turn("s", store=broken)  # type: ignore[arg-type]
    assert True, "close_turn swallowed the failure"


def test_the_prompt_hook_still_delivers_the_team_delta_when_metrics_fail(
    isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one named behaviour change of the metrics lane: a store failure used
    to cost the session its teammate delta too; now it costs only the record."""
    monkeypatch.setattr(team_service, "hook_prompt_heartbeat", lambda *a, **k: "TEAM DELTA")
    monkeypatch.setattr(
        metrics_service, "open_turn", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope"))
    )
    assert hooks_service.prompt_submitted("hello", tmp_path, session_id="ses_h") == "TEAM DELTA"


def test_a_prompt_that_cannot_be_recorded_says_so_on_stderr(
    isolated_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The branch swallowed the store failure and returned "", so the regression
    _cost_of_failing_open was written to end — a damaged store, silently — was
    back for the prompt half. The teammate delta still flows; stderr says what
    was lost; stdout stays the agent's."""
    import sqlite3

    def broken() -> object:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(hooks_service, "store_session", broken)
    monkeypatch.setattr(team_service, "hook_prompt_heartbeat", lambda *a, **k: "TEAM DELTA")
    out = hooks_service.prompt_submitted("q", tmp_path, session_id="ses_1")
    assert out == "TEAM DELTA"
    captured = capsys.readouterr()
    assert "prompt not recorded" in captured.err and "database is locked" in captured.err
    assert captured.out == ""


def test_the_prompt_hook_records_a_turn(isolated_home: Path, tmp_path: Path) -> None:
    hooks_service.prompt_submitted("why does the lock use msvcrt", tmp_path, session_id="ses_h")
    (turn,) = metrics_service.recent(session_id="ses_h")
    assert turn.client_reason is ClientReason.disabled


def test_the_stop_hook_closes_the_turn(isolated_home: Path, tmp_path: Path) -> None:
    hooks_service.prompt_submitted("first", tmp_path, session_id="ses_h")
    hooks_service.turn_stopped(tmp_path, session_id="ses_h")
    (turn,) = metrics_service.recent(session_id="ses_h")
    assert turn.ended_at is not None


def test_an_empty_prompt_still_opens_a_turn(isolated_home: Path, tmp_path: Path) -> None:
    hooks_service.prompt_submitted("", tmp_path, session_id="ses_h")
    assert len(metrics_service.recent(session_id="ses_h")) == 1


# --- aggregation --------------------------------------------------------------


def test_an_empty_summary_is_not_an_error() -> None:
    summary = metrics_service.summarize([])
    assert summary.turns == 0 and summary.median_wall_ms is None


def test_the_three_reason_groups_are_never_summed() -> None:
    turns = [
        _turn(n=0, client_reason=ClientReason.disabled),
        _turn(n=1, client_reason=ClientReason.not_configured),
        _turn(n=2, client_reason=ClientReason.no_run),
        _turn(n=3, client_reason=ClientReason.no_prompt),
        _turn(n=4, client_reason=ClientReason.trigger_not_in_descriptor),
        _turn(n=5, client_reason=ClientReason.http_error),
        _turn(n=6, client_reason=ClientReason.deadline_exceeded, deadline_breached=True),
        _consulted(7),
    ]
    summary = metrics_service.summarize(turns)
    assert (summary.baseline, summary.skipped, summary.failed, summary.consulted) == (3, 2, 2, 1)
    assert summary.deadline_breaches == 1
    assert summary.by_reason["disabled"] == 1 and summary.by_status == {"served": 1}
    assert summary.by_trigger == {"prompt_submit": 8}


def test_every_reason_belongs_to_exactly_one_group() -> None:
    from aisquare.models import BASELINE_REASONS, BY_DESIGN_REASONS, FAILURE_REASONS

    groups = [BASELINE_REASONS, BY_DESIGN_REASONS, FAILURE_REASONS, {ClientReason.none}]
    for reason in ClientReason:
        assert sum(reason in group for group in groups) == 1, reason


def test_percentiles_pin_the_nearest_rank_exactly() -> None:
    turns = [_consulted(i, round_trip_ms=ms) for i, ms in enumerate([10, 20, 30, 400])]
    summary = metrics_service.summarize(turns)
    assert summary.median_round_trip_ms == 20
    assert summary.p95_round_trip_ms == 400


def test_round_trip_stats_cover_consulted_rows_only() -> None:
    """A row that made no round trip has no round trip; its zero would make a
    300 ms endpoint read as instant."""
    turns = [
        _turn(n=0, client_reason=ClientReason.not_configured, round_trip_ms=0),
        _turn(n=1, client_reason=ClientReason.transport_error, round_trip_ms=0),
        _turn(n=2, client_reason=ClientReason.deadline_exceeded, round_trip_ms=60_000),
        _consulted(3, round_trip_ms=300),
        _consulted(4, round_trip_ms=320),
    ]
    summary = metrics_service.summarize(turns)
    assert summary.median_round_trip_ms == 300
    assert summary.p95_round_trip_ms == 320


def test_override_rows_are_counted_apart_and_kept_out_of_the_round_trip_figures() -> None:
    """A latency quoted from the staging override is a number the experiment
    never measured (live-wiring handoff §4)."""
    turns = [
        _consulted(0, round_trip_ms=300, delivery_source="descriptor"),
        _consulted(1, round_trip_ms=320, delivery_source="descriptor"),
        _consulted(2, round_trip_ms=2, delivery_source="override"),
        _consulted(3, round_trip_ms=3, delivery_source="override"),
        _turn(n=4, client_reason=ClientReason.disabled),
    ]
    summary = metrics_service.summarize(turns)
    assert summary.override_turns == 2
    assert summary.by_delivery_source == {"descriptor": 2, "override": 2}
    assert summary.consulted == 4, "what happened is still counted"
    assert (summary.median_round_trip_ms, summary.p95_round_trip_ms) == (300, 320)


def test_metrics_show_says_when_override_rows_are_present(
    isolated_home: Path, runner: CliRunner
) -> None:
    with store_session() as store:
        store.ensure_project(PROJECT)
        store.open_turn(_consulted(0, round_trip_ms=5, delivery_source="override"))
    result = runner.invoke(app, ["metrics", "show", "--all"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "staging delivery override" in result.stdout and "measure nothing" in result.stdout


def test_missing_token_data_is_reported_as_missing() -> None:
    assert metrics_service.summarize([_turn(n=0)]).turns_with_tokens == 0


def test_the_percentile_helper_ignores_rows_without_a_value() -> None:
    assert metrics_service.percentile([None, None], 50) is None
    assert metrics_service.percentile([None, 7], 50) == 7
    assert metrics_service.percentile([1, 2, 3, 4, 5], 50) == 3
    assert metrics_service.percentile([1, 2, 3, 4, 5], 95) == 5


# --- the CLI ------------------------------------------------------------------


def _prompt(tmp_path: Path, session: str = "ses_h") -> None:
    hooks_service.prompt_submitted("hello", tmp_path, session_id=session)


def test_metrics_show_reports_the_token_gap(
    isolated_home: Path, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _prompt(tmp_path)
    result = runner.invoke(app, ["metrics", "show"])
    assert result.exit_code == 0, result.output
    assert "no token counts recorded" in result.stdout
    assert "baseline (never asked)" in result.stdout


def test_metrics_show_json_is_machine_readable_and_grouped(
    isolated_home: Path, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _prompt(tmp_path)
    result = runner.invoke(app, ["--json", "metrics", "show"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["turns"] == 1 and payload["baseline"] == 1
    assert payload["by_reason"] == {"disabled": 1}
    assert payload["project_id"] is not None


def test_metrics_show_is_scoped_to_the_current_project_unless_told_otherwise(
    isolated_home: Path, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    here = tmp_path / "here"
    there = tmp_path / "there"
    here.mkdir()
    there.mkdir()
    _prompt(here, "ses_here")
    _prompt(there, "ses_there")
    monkeypatch.chdir(here)
    scoped = json.loads(runner.invoke(app, ["--json", "metrics", "show"]).stdout)
    everything = json.loads(runner.invoke(app, ["--json", "metrics", "show", "--all"]).stdout)
    named = json.loads(
        runner.invoke(app, ["--json", "metrics", "show", "--project", "there"]).stdout
    )
    assert scoped["turns"] == 1 and everything["turns"] == 2 and named["turns"] == 1
    assert named["project_id"] != scoped["project_id"]


def test_metrics_show_refuses_an_unknown_project(isolated_home: Path, runner: CliRunner) -> None:
    result = runner.invoke(app, ["metrics", "show", "--project", "nope"])
    assert result.exit_code == 2
    assert "no project matches" in result.output


def test_metrics_limit_must_be_positive(isolated_home: Path, runner: CliRunner) -> None:
    result = runner.invoke(app, ["metrics", "show", "-n", "0"])
    assert result.exit_code == 2


def test_metrics_list_json_is_an_array_with_full_trace_ids(
    isolated_home: Path, tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _prompt(tmp_path)
    result = runner.invoke(app, ["--json", "metrics", "list"])
    assert result.exit_code == 0, result.output
    (row,) = json.loads(result.stdout)
    assert row["trace_id"].startswith("trc_") and row["client_reason"] == "disabled"
    table = runner.invoke(app, ["metrics", "list"])
    assert row["trace_id"] in table.stdout, "a trace id is printed whole, or it cannot be looked up"


def test_metrics_show_is_clean_with_nothing_recorded(
    isolated_home: Path, runner: CliRunner
) -> None:
    result = runner.invoke(app, ["metrics", "show"])
    assert result.exit_code == 0
    assert "No turns recorded yet" in result.stdout


# --- hot path -----------------------------------------------------------------


def test_opening_a_turn_stays_under_the_hot_path_budget(store: ContextStore) -> None:
    """A single INSERT, aggregation deferred to read. Asserts an order of
    magnitude, not a stopwatch: 20 inserts in well under a second."""
    started = time.perf_counter()
    for n in range(20):
        metrics_service.open_turn(_turn(n=n, session_id="ses_perf"), store=store)
    assert time.perf_counter() - started < 1.0
