"""The staging override: a loud, recorded exception only a ``direct_api`` descriptor admits.

Against today's server the descriptor says ``direct_api`` for every run, so
the descriptor-gated hooks never call. ``AISQUARE_CI_DELIVERY_OVERRIDE`` stands
in for the delivery list the server will publish — and every place it acts
says so: the gate, every row, the join record, and ``doctor``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from aisquare.core import insights, outbox, paths
from aisquare.core.config import AppConfig, ExperimentSettings, save_config
from aisquare.models import CheckStatus, ClientReason, DoctorCheck, TurnMetric
from aisquare.services import ci_augment, ci_override, ci_recall
from aisquare.services import hooks as hooks_service
from aisquare.services import metrics as metrics_service
from aisquare.services.ci_contract import RECALL_TOOL, DeliveryDescriptor, wire_session_id
from aisquare.services.diagnostics import doctor
from tests.ci_schemas import assert_valid
from tests.ci_support import RUN, SESSION, wire
from tests.stub_ci_server import StubCI, live_descriptor, serve

SPEC = ci_override.EXAMPLE
DIRECT_API = live_descriptor(delivery=[{"kind": "direct_api"}])


@pytest.fixture
def stub() -> Iterator[StubCI]:
    yield from serve()


@pytest.fixture
def direct(stub: StubCI, monkeypatch: pytest.MonkeyPatch) -> StubCI:
    """Wired to a stub whose descriptor says what the staging server says today."""
    wire(monkeypatch, stub)
    stub.descriptor_json(DIRECT_API)
    return stub


@pytest.fixture
def overridden(direct: StubCI, monkeypatch: pytest.MonkeyPatch) -> StubCI:
    monkeypatch.setenv(ci_override.ENV_VAR, SPEC)
    return direct


def _turns() -> dict[str, TurnMetric]:
    turns = {t.trigger: t for t in metrics_service.recent(session_id=SESSION)}
    assert turns, "no rows recorded"
    return {str(k): v for k, v in turns.items()}


def _ci_checks() -> dict[str, DoctorCheck]:
    return {c.name: c for c in doctor() if c.name.startswith("ci ")}


# --- the descriptor rules unless told otherwise --------------------------------------


def test_without_the_variable_a_direct_api_descriptor_rules(
    direct: StubCI, isolated_home: Path, tmp_path: Path
) -> None:
    hooks_service.session_start_context(tmp_path, session_id=SESSION)
    assert hooks_service.prompt_submitted("q", tmp_path, session_id=SESSION) == ""
    turns = _turns()
    assert set(turns) == {"session_start", "prompt_submit"}
    for turn in turns.values():
        assert turn.client_reason is ClientReason.trigger_not_in_descriptor
        assert turn.delivery_source == "descriptor"
    assert direct.call_count == 0


def test_the_override_delivers_as_if_the_descriptor_had_said_so(
    overridden: StubCI, isolated_home: Path, tmp_path: Path
) -> None:
    context = hooks_service.session_start_context(tmp_path, session_id=SESSION)
    block = hooks_service.prompt_submitted(
        "why did the pool guard leak", tmp_path, session_id=SESSION
    )
    assert RECALL_TOOL in context, "mcp_pull in the spec announces the tool"
    assert "pool-reset" in block, "hook_push in the spec calls and injects"
    assert [r["trigger"] for r in overridden.requests] == ["session_start", "prompt_submit"]
    for body in overridden.requests:
        assert_valid("hook-request.experimental-v2", body)
        assert body["run_id"] == RUN and body["client_safety_ms"] == 60_000
    assert overridden.hooks[0].path == ci_override.HOOK_ENDPOINT
    turns = _turns()
    for turn in turns.values():
        assert turn.client_reason is ClientReason.none
        assert turn.delivery_source == "override", "never mistakable for the descriptor's ruling"
        assert turn.opaque_config_id == "cfg_public_7d41ba90c2e5", (
            "the rest of the descriptor stands"
        )


def test_the_override_is_never_written_to_the_descriptor_cache(
    overridden: StubCI, isolated_home: Path, tmp_path: Path
) -> None:
    hooks_service.prompt_submitted("q", tmp_path, session_id=SESSION)
    cached = json.loads(paths.ci_descriptor_path(RUN).read_text(encoding="utf-8"))
    assert cached["delivery"] == [{"kind": "direct_api"}], "the cache holds what the server said"
    assert overridden.descriptor_fetches == 1


def test_the_override_never_applies_when_the_descriptor_lists_real_modes(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path, tmp_path: Path
) -> None:
    """The vendored descriptor pushes on prompt_submit only. A spec that would
    add session_start must change nothing about what is called."""
    wire(monkeypatch, stub)
    monkeypatch.setenv(ci_override.ENV_VAR, "hook_push:session_start,prompt_submit;mcp_pull")
    hooks_service.session_start_context(tmp_path, session_id=SESSION)
    hooks_service.prompt_submitted("q", tmp_path, session_id=SESSION)
    assert [r["trigger"] for r in stub.requests] == ["prompt_submit"]
    turns = _turns()
    assert turns["session_start"].client_reason is ClientReason.trigger_not_in_descriptor
    assert turns["prompt_submit"].client_reason is ClientReason.none
    assert {t.delivery_source for t in turns.values()} == {"descriptor"}


@pytest.mark.parametrize(
    "spec",
    [
        "hook_push",  # no triggers
        "hook_push:pre_tool_use",  # not a trigger the contract knows
        "carrier_pigeon",  # not a kind
        "mcp_pull:something",  # takes no arguments
        "hook_push:prompt_submit;hook_push:session_start",  # one member per kind
        "hook_push:prompt_submit,prompt_submit",  # unique triggers
        ";",  # nothing
    ],
)
def test_a_malformed_override_is_ignored_and_says_why(
    direct: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path, tmp_path: Path, spec: str
) -> None:
    monkeypatch.setenv(ci_override.ENV_VAR, spec)
    ruling = ci_override.apply(DeliveryDescriptor.model_validate(DIRECT_API))
    assert not ruling.active and ruling.source == "descriptor"
    assert "ignored" in ruling.detail and ci_override.EXAMPLE in ruling.detail
    assert ruling.descriptor.delivery[0].kind == "direct_api"
    hooks_service.prompt_submitted("q", tmp_path, session_id=SESSION)
    turn = _turns()["prompt_submit"]
    assert turn.client_reason is ClientReason.trigger_not_in_descriptor
    assert turn.delivery_source == "descriptor" and direct.call_count == 0


def test_a_partial_spec_delivers_exactly_what_it_names(
    direct: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path, tmp_path: Path
) -> None:
    monkeypatch.setenv(ci_override.ENV_VAR, "hook_push:prompt_submit")
    context = hooks_service.session_start_context(tmp_path, session_id=SESSION)
    hooks_service.prompt_submitted("q", tmp_path, session_id=SESSION)
    assert RECALL_TOOL not in context, "no mcp_pull, no instruction"
    assert [r["trigger"] for r in direct.requests] == ["prompt_submit"]
    turns = _turns()
    assert turns["session_start"].client_reason is ClientReason.trigger_not_in_descriptor
    assert turns["session_start"].delivery_source == "override"
    assert turns["session_start"].instruction_version is None


# --- every record says which document ruled --------------------------------------------


def test_the_join_record_carries_the_delivery_source(
    overridden: StubCI, isolated_home: Path, tmp_path: Path
) -> None:
    config = AppConfig()
    config.explainability.ship = True
    config.explainability.gateway_url = "https://gateway.example"
    save_config(config)
    insights.reset_cache()
    hooks_service.prompt_submitted("q", tmp_path, session_id=SESSION)
    records = [json.loads(p.read_text(encoding="utf-8")) for p in outbox.pending()]
    (join,) = [r for r in records if r["kind"] == "ci_turn"]
    assert join["ci"]["delivery_source"] == "override"
    assert join["ci"]["trace_id"] == _turns()["prompt_submit"].trace_id


def test_a_baseline_row_has_no_delivery_source(isolated_home: Path, tmp_path: Path) -> None:
    hooks_service.prompt_submitted("q", tmp_path, session_id=SESSION)
    turn = _turns()["prompt_submit"]
    assert turn.client_reason is ClientReason.disabled and turn.delivery_source is None


def test_the_recall_tool_becomes_available_through_the_override(
    overridden: StubCI, isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert ci_recall.available() is True
    result = ci_recall.collective_intelligence_recall("q", wire_session_id(SESSION))
    assert result["status"] == "served"
    (turn,) = metrics_service.recent(session_id=SESSION)
    assert turn.trigger == "agent_request" and turn.delivery_source == "override"
    monkeypatch.delenv(ci_override.ENV_VAR)
    assert ci_recall.available() is False


def test_metrics_list_shows_the_source_beside_every_row(
    overridden: StubCI, isolated_home: Path, tmp_path: Path, runner: object
) -> None:
    from typer.testing import CliRunner

    from aisquare.cli.app import app

    assert isinstance(runner, CliRunner)
    hooks_service.prompt_submitted("q", tmp_path, session_id=SESSION)
    result = runner.invoke(app, ["metrics", "list", "--all"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "SOURCE" in result.stdout and "override" in result.stdout
    as_json = runner.invoke(app, ["--json", "metrics", "list", "--all"])
    assert json.loads(as_json.stdout)[0]["delivery_source"] == "override"


# --- doctor -------------------------------------------------------------------------------


def test_doctor_warns_on_its_own_line_while_the_override_is_active(
    overridden: StubCI, isolated_home: Path
) -> None:
    checks = _ci_checks()
    assert checks["ci descriptor"].status is CheckStatus.ok
    assert "direct_api only" in checks["ci descriptor"].detail, "the server's ruling, still shown"
    line = checks["ci delivery override"]
    assert line.status is CheckStatus.warn
    assert "active" in line.detail
    assert "hook_push on session_start, prompt_submit" in line.detail and "mcp_pull" in line.detail
    assert "delivery_source override" in line.detail
    assert line.fix and ci_override.ENV_VAR in line.fix


def test_doctor_names_a_set_override_even_before_a_token_or_run_exists(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    """The state this machine was in when the override landed: variable
    exported, token not yet fetched. The first doctor run must not be silent
    about it."""
    monkeypatch.setenv("AISQUARE_CI", "1")
    monkeypatch.setenv("AISQUARE_CI_URL", stub.url)
    monkeypatch.setenv(ci_override.ENV_VAR, SPEC)
    line = _ci_checks()["ci delivery override"]
    assert line.status is CheckStatus.warn and "no fetched descriptor" in line.detail
    monkeypatch.setenv("AISQUARE_CI_KEY", "k")  # still no run
    assert "ci delivery override" in _ci_checks()


def test_the_descriptor_line_does_not_promise_silence_the_override_breaks(
    overridden: StubCI, isolated_home: Path
) -> None:
    checks = _ci_checks()
    assert "the hooks will not call" not in checks["ci descriptor"].detail
    assert "direct_api only" in checks["ci descriptor"].detail, "the server's ruling stays visible"
    assert "overridden" in checks["ci descriptor"].detail


def test_a_huge_override_value_is_never_echoed_whole(
    direct: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    """doctor output is the most pasteable artefact there is; a value exported
    into the wrong variable must not come back verbatim at any length."""
    monkeypatch.setenv(ci_override.ENV_VAR, "z" * 5_000)
    ruling = ci_override.apply(DeliveryDescriptor.model_validate(DIRECT_API))
    assert not ruling.active and len(ruling.detail) < 400
    monkeypatch.setenv(ci_override.ENV_VAR, "mcp_pull:" + "y" * 5_000)
    assert len(ci_override.apply(DeliveryDescriptor.model_validate(DIRECT_API)).detail) < 400
    assert len(_ci_checks()["ci delivery override"].detail) < 400


def test_doctor_is_silent_about_the_override_when_it_is_unset(
    direct: StubCI, isolated_home: Path
) -> None:
    assert "ci delivery override" not in _ci_checks()


def test_doctor_says_when_the_override_is_set_but_ignored(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    wire(monkeypatch, stub)  # the vendored descriptor lists real modes
    monkeypatch.setenv(ci_override.ENV_VAR, SPEC)
    line = _ci_checks()["ci delivery override"]
    assert line.status is CheckStatus.warn and "ignored" in line.detail
    assert "real delivery modes" in line.detail


def test_doctor_names_the_fault_in_a_malformed_override(
    direct: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    monkeypatch.setenv(ci_override.ENV_VAR, "carrier_pigeon")
    line = _ci_checks()["ci delivery override"]
    assert line.status is CheckStatus.warn
    assert "carrier_pigeon" in line.detail and ci_override.EXAMPLE in line.detail


def test_doctor_says_when_there_is_no_descriptor_to_override(
    direct: StubCI, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    monkeypatch.setenv(ci_override.ENV_VAR, SPEC)
    direct.descriptor_json({"error": "no"}, status=401)
    line = _ci_checks()["ci delivery override"]
    assert line.status is CheckStatus.warn and "no fetched descriptor" in line.detail


# --- what must not change ------------------------------------------------------------------


def test_the_override_is_environment_only() -> None:
    for name in ExperimentSettings.model_fields:
        assert "override" not in name and "delivery" not in name


def test_the_override_costs_nothing_while_the_experiment_is_off(
    monkeypatch: pytest.MonkeyPatch, isolated_home: Path, tmp_path: Path
) -> None:
    monkeypatch.setenv(ci_override.ENV_VAR, SPEC)
    monkeypatch.setattr(
        ci_override, "apply", lambda *a, **k: (_ for _ in ()).throw(AssertionError("read"))
    )
    assert hooks_service.prompt_submitted("q", tmp_path, session_id=SESSION) == ""
    assert ci_augment.gate().reason is ClientReason.disabled
