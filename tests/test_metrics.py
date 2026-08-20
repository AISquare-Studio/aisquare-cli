"""Per-turn metrics: the row exists whether or not CI ran, and never breaks a turn.

The baseline case is the one that matters most here. Before the endpoint is
live every turn records ``disabled`` — and that is data, not a gap: it is the
noise floor the first comparative claim will have to clear.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.core.store import ContextStore, open_store
from aisquare.models import ProjectInfo, TurnMetric
from aisquare.services import hooks as hooks_service
from aisquare.services import metrics as metrics_service
from aisquare.services.ci_client import Call
from aisquare.services.ci_contract import Action, DegradationReason, HookResponse, Outcome

PROJECT = ProjectInfo(id="prj_metrics", root=Path("/tmp/metrics"), linked_repos=[])


@pytest.fixture
def store() -> ContextStore:
    opened = open_store()
    opened.ensure_project(PROJECT)
    return opened


def _call(action: Action, reason: DegradationReason, **kwargs: object) -> Call:
    response = HookResponse(contract=1, action=action, **kwargs)
    return Call(Outcome(response=response, reason=reason), round_trip_ms=120)


# --- the row ------------------------------------------------------------------


def test_a_turn_is_recorded_even_though_ci_never_ran(store: ContextStore) -> None:
    """The baseline case. This is the whole reason metrics land before CI does."""
    trace_id = metrics_service.open_turn(PROJECT.id, session_id="ses_1", store=store)
    assert trace_id is not None and trace_id.startswith("trc_")
    (turn,) = store.turn_metrics(project_id=PROJECT.id)
    assert turn.ci_action == "allow"
    assert turn.degradation_reason == "disabled"


def test_a_ci_call_records_its_action_and_reason_together(store: ContextStore) -> None:
    metrics_service.open_turn(
        PROJECT.id,
        session_id="ses_1",
        call=_call(Action.inject, DegradationReason.none, server_ms=40),
        injected_chars=512,
        store=store,
    )
    (turn,) = store.turn_metrics(project_id=PROJECT.id)
    assert turn.ci_action == "inject"
    assert turn.degradation_reason == "none"
    assert turn.server_ms == 40
    assert turn.round_trip_ms == 120
    assert turn.injected_chars == 512


def test_a_timeout_is_distinguishable_from_a_deliberate_allow(store: ContextStore) -> None:
    """The failure this column exists to prevent: both are ``allow``, and without
    the reason an endpoint failing every call reads as a clean baseline."""
    metrics_service.open_turn(
        PROJECT.id,
        session_id="ses_slow",
        call=_call(Action.allow, DegradationReason.backstop_exceeded),
        store=store,
    )
    metrics_service.open_turn(
        PROJECT.id,
        session_id="ses_ok",
        call=_call(Action.allow, DegradationReason.none),
        store=store,
    )
    turns = store.turn_metrics(project_id=PROJECT.id)
    assert {t.ci_action for t in turns} == {"allow"}
    assert {t.degradation_reason for t in turns} == {"backstop_exceeded", "none"}


def test_a_backstop_breach_is_counted_as_one(store: ContextStore) -> None:
    metrics_service.open_turn(
        PROJECT.id,
        session_id="ses_1",
        call=_call(Action.allow, DegradationReason.backstop_exceeded),
        store=store,
    )
    (turn,) = store.turn_metrics(project_id=PROJECT.id)
    assert turn.budget_breach is True


def test_token_columns_stay_null_rather_than_fabricated(store: ContextStore) -> None:
    """Hook payloads carry no token counts. A zero here would survive into a
    published comparison as if it had been measured."""
    metrics_service.open_turn(PROJECT.id, session_id="ses_1", store=store)
    (turn,) = store.turn_metrics(project_id=PROJECT.id)
    assert turn.tokens_in is None
    assert turn.tokens_out is None


def test_the_client_records_no_run_id_in_phase_one(store: ContextStore) -> None:
    metrics_service.open_turn(PROJECT.id, session_id="ses_1", store=store)
    (turn,) = store.turn_metrics(project_id=PROJECT.id)
    assert turn.run_id is None
    assert turn.arm is None


# --- closing ------------------------------------------------------------------


def test_stop_closes_the_newest_open_turn(store: ContextStore) -> None:
    metrics_service.open_turn(PROJECT.id, session_id="ses_1", store=store)
    metrics_service.close_turn("ses_1", store=store)
    (turn,) = store.turn_metrics(project_id=PROJECT.id)
    assert turn.ended_at is not None
    assert turn.wall_ms is not None and turn.wall_ms >= 0


def test_closing_twice_leaves_the_first_close_alone(store: ContextStore) -> None:
    metrics_service.open_turn(PROJECT.id, session_id="ses_1", store=store)
    metrics_service.close_turn("ses_1", store=store)
    (first,) = store.turn_metrics(project_id=PROJECT.id)
    metrics_service.close_turn("ses_1", store=store)
    (again,) = store.turn_metrics(project_id=PROJECT.id)
    assert again.ended_at == first.ended_at


def test_a_turn_that_never_stopped_stays_open(store: ContextStore) -> None:
    """A killed terminal leaves an open row rather than one closed with a
    fabricated end time; it is excluded from wall-clock aggregates instead."""
    metrics_service.open_turn(PROJECT.id, session_id="ses_1", store=store)
    metrics_service.open_turn(PROJECT.id, session_id="ses_1", store=store)
    metrics_service.close_turn("ses_1", store=store)
    turns = store.turn_metrics(project_id=PROJECT.id)
    assert sum(1 for t in turns if t.ended_at is None) == 1


def test_closing_an_unknown_session_is_silent(store: ContextStore) -> None:
    metrics_service.close_turn("ses_never_seen", store=store)


def test_sessions_close_their_own_turns(store: ContextStore) -> None:
    metrics_service.open_turn(PROJECT.id, session_id="ses_a", store=store)
    metrics_service.open_turn(PROJECT.id, session_id="ses_b", store=store)
    metrics_service.close_turn("ses_a", store=store)
    open_sessions = {t.session_id for t in store.turn_metrics() if t.ended_at is None}
    assert open_sessions == {"ses_b"}


# --- recording never breaks a turn --------------------------------------------


def test_a_broken_store_costs_the_row_not_the_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """A metrics write is the least important thing happening when a developer
    submits a prompt, and it is not allowed to be what breaks it."""

    class Exploding:
        def open_turn(self, metric: TurnMetric) -> TurnMetric:
            raise RuntimeError("disk is on fire")

        def close_turn(self, session_id: str, *, ended_at: datetime) -> None:
            raise RuntimeError("disk is still on fire")

    broken = Exploding()
    assert metrics_service.open_turn(PROJECT.id, session_id="s", store=broken) is None  # type: ignore[arg-type]
    metrics_service.close_turn("s", store=broken)  # type: ignore[arg-type]


def test_the_prompt_hook_still_returns_when_metrics_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        metrics_service, "open_turn", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope"))
    )
    assert hooks_service.prompt_submitted("hello", None, session_id=None) == ""


def test_the_prompt_hook_records_a_turn(isolated_home: Path, tmp_path: Path) -> None:
    hooks_service.prompt_submitted("why does the lock use msvcrt", tmp_path, session_id="ses_h")
    turns = metrics_service.recent(session_id="ses_h")
    assert len(turns) == 1
    assert turns[0].degradation_reason == "disabled"


def test_the_stop_hook_closes_the_turn(isolated_home: Path, tmp_path: Path) -> None:
    hooks_service.prompt_submitted("first", tmp_path, session_id="ses_h")
    hooks_service.turn_stopped(tmp_path, session_id="ses_h")
    (turn,) = metrics_service.recent(session_id="ses_h")
    assert turn.ended_at is not None


def test_an_empty_prompt_still_opens_a_turn(isolated_home: Path, tmp_path: Path) -> None:
    """A turn happened whether or not there was text worth capturing."""
    hooks_service.prompt_submitted("", tmp_path, session_id="ses_h")
    assert len(metrics_service.recent(session_id="ses_h")) == 1


# --- aggregation --------------------------------------------------------------


def _turn(**kwargs: object) -> TurnMetric:
    base: dict[str, object] = {
        "trace_id": f"trc_{kwargs.pop('n', 0)}",
        "project_id": PROJECT.id,
        "started_at": datetime.now(tz=UTC),
    }
    base.update(kwargs)
    return TurnMetric(**base)


def test_an_empty_summary_is_not_an_error() -> None:
    summary = metrics_service.summarize([])
    assert summary.turns == 0
    assert summary.median_wall_ms is None


def test_off_is_not_counted_as_a_degradation() -> None:
    """Otherwise a baseline run reports as one where every call failed."""
    turns = [_turn(n=i, degradation_reason="disabled") for i in range(5)]
    summary = metrics_service.summarize(turns)
    assert summary.degraded == 0
    assert summary.ci_consulted == 0


def test_real_failures_are_counted_as_degradations() -> None:
    turns = [
        _turn(n=0, degradation_reason="none"),
        _turn(n=1, degradation_reason="http_error"),
        _turn(n=2, degradation_reason="backstop_exceeded", budget_breach=True),
        _turn(n=3, degradation_reason="not_configured"),
    ]
    summary = metrics_service.summarize(turns)
    assert summary.ci_consulted == 1
    assert summary.degraded == 2
    assert summary.budget_breaches == 1


def test_percentiles_use_a_real_observed_value() -> None:
    turns = [
        _turn(n=i, degradation_reason="none", round_trip_ms=ms)
        for i, ms in enumerate([10, 20, 30, 400])
    ]
    summary = metrics_service.summarize(turns)
    assert summary.median_round_trip_ms in {20, 30}
    assert summary.p95_round_trip_ms == 400


def test_round_trip_stats_ignore_turns_ci_never_saw() -> None:
    turns = [
        _turn(n=0, degradation_reason="disabled"),
        _turn(n=1, degradation_reason="none", round_trip_ms=90),
    ]
    assert metrics_service.summarize(turns).median_round_trip_ms == 90


def test_missing_token_data_is_reported_as_missing() -> None:
    """A zero must read as "not measured yet", never as "no tokens were used"."""
    summary = metrics_service.summarize([_turn(n=0)])
    assert summary.turns_with_tokens == 0


# --- the CLI ------------------------------------------------------------------


def test_metrics_show_reports_the_token_gap(isolated_home: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    from aisquare.cli.app import app

    hooks_service.prompt_submitted("hello", tmp_path, session_id="ses_h")
    result = runner.invoke(app, ["metrics", "show"])
    assert result.exit_code == 0
    assert "no token counts recorded" in result.stdout


def test_metrics_show_json_is_machine_readable(isolated_home: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    from aisquare.cli.app import app

    hooks_service.prompt_submitted("hello", tmp_path, session_id="ses_h")
    result = runner.invoke(app, ["--json", "metrics", "show"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["turns"] == 1
    assert payload["by_reason"] == {"disabled": 1}


def test_metrics_list_json_is_an_array(isolated_home: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    from aisquare.cli.app import app

    hooks_service.prompt_submitted("hello", tmp_path, session_id="ses_h")
    result = runner.invoke(app, ["--json", "metrics", "list"])
    assert result.exit_code == 0
    assert len(json.loads(result.stdout)) == 1


def test_metrics_show_is_clean_with_nothing_recorded(isolated_home: Path) -> None:
    runner = CliRunner()
    from aisquare.cli.app import app

    result = runner.invoke(app, ["metrics", "show"])
    assert result.exit_code == 0
    assert "No turns recorded yet" in result.stdout


# --- hot path -----------------------------------------------------------------


def test_opening_a_turn_stays_under_the_hot_path_budget(store: ContextStore) -> None:
    """A single INSERT, aggregation deferred to read. The budget is 5 ms; this
    asserts an order of magnitude, not a stopwatch."""
    started = datetime.now(tz=UTC)
    for _ in range(20):
        metrics_service.open_turn(PROJECT.id, session_id="ses_perf", store=store)
    elapsed = datetime.now(tz=UTC) - started
    assert elapsed < timedelta(milliseconds=5 * 20 * 4)
