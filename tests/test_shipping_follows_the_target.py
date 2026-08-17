"""Both explainability lanes must point at the same deployment.

The proxy lane resolves a TARGET — `enable --target prod --gateway-url … --key-env
PROD_KEY` — and `status` and `doctor` report what that target resolves to. The
client lane (the spool and `explainability ship`) did not: it read the top-level
`gateway_url` and a hardcoded `EXPLAINABILITY_API_KEY`, ignoring the active
target entirely.

That splits at exactly the moment it costs most. Configure shipping while a
staging shell is sourced — which is what the cutover runbook has you do — then
switch the proxy lane to prod. Model traffic goes to prod, CLI insights keep
going to staging, and `status` prints the prod gateway because the line an
operator reads resolves the target. Nobody is told; both halves look healthy.

Reproduced on the train before the fix: after
`enable --target prod --gateway-url https://prod.example --key-env PROD_KEY`,
`status` reported `gateway: https://prod.example` and `key: $PROD_KEY is set`
while `shipping_state()` reported `gateway_url=''` and `has_key=False`.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import insights
from aisquare.core.config import AppConfig, ExplainabilityTarget, load_config, save_config
from aisquare.services import explainability as service

PROD_GATEWAY = "https://prod.example"
STG_GATEWAY = "https://stg.example"


@pytest.fixture(autouse=True)
def _fresh() -> None:
    insights.reset_cache()


def _two_targets(active: str) -> None:
    config = AppConfig()
    config.explainability.target = active
    config.explainability.gateway_url = STG_GATEWAY  # the old top-level default
    config.explainability.targets = {
        "stg": ExplainabilityTarget(gateway_url=STG_GATEWAY, api_key_env="STG_KEY"),
        "prod": ExplainabilityTarget(gateway_url=PROD_GATEWAY, api_key_env="PROD_KEY"),
    }
    config.explainability.ship = True
    save_config(config)
    insights.reset_cache()


def test_shipping_reads_the_active_targets_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole defect: prod is active, so prod is where insights go."""
    _two_targets("prod")
    monkeypatch.setenv("PROD_KEY", "pk-test")

    state = service.shipping_state()

    assert state.gateway_url == PROD_GATEWAY, (
        "shipping is still using the top-level gateway while the operator, "
        "status and doctor are all on prod"
    )


def test_shipping_reads_the_key_the_target_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--key-env PROD_KEY` names the variable; shipping must read THAT one."""
    _two_targets("prod")
    monkeypatch.delenv("EXPLAINABILITY_API_KEY", raising=False)
    monkeypatch.setenv("PROD_KEY", "pk-test")

    assert service.shipping_state().has_key, "shipping ignored the target's api_key_env"


def test_the_stg_key_does_not_satisfy_a_prod_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reading the wrong variable would ship prod sessions with a staging key."""
    _two_targets("prod")
    monkeypatch.delenv("PROD_KEY", raising=False)
    monkeypatch.setenv("STG_KEY", "sk-test")
    monkeypatch.setenv("EXPLAINABILITY_API_KEY", "sk-test")

    assert not service.shipping_state().has_key


def test_switching_target_moves_shipping_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """One switch, both lanes. That is the property the cutover depends on."""
    monkeypatch.setenv("STG_KEY", "sk-test")
    monkeypatch.setenv("PROD_KEY", "pk-test")

    _two_targets("stg")
    assert service.shipping_state().gateway_url == STG_GATEWAY

    config = load_config()
    config.explainability.target = "prod"
    save_config(config)
    insights.reset_cache()

    assert service.shipping_state().gateway_url == PROD_GATEWAY


def test_ship_refuses_rather_than_using_the_wrong_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No key for the active target means stop, not fall back to another one."""
    _two_targets("prod")
    monkeypatch.delenv("PROD_KEY", raising=False)
    monkeypatch.setenv("EXPLAINABILITY_API_KEY", "sk-wrong-deployment")

    report = service.ship_once()

    assert report.sent == 0
    assert "PROD_KEY" in report.reason, report.reason


def test_status_names_where_insights_are_going(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The line that was silently wrong must say the destination out loud.

    Counts alone cannot reveal a split brain: `2 sent` looks identical whether
    it went to prod or staging.
    """
    _two_targets("prod")
    monkeypatch.setenv("PROD_KEY", "pk-test")

    result = runner.invoke(app, ["explainability", "status"], catch_exceptions=False)

    shipping = next(line for line in result.output.splitlines() if line.startswith("shipping:"))
    assert PROD_GATEWAY in shipping, shipping


def test_json_reports_the_shipping_destination(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scripted cutover check must be able to assert the destination."""
    import json

    _two_targets("prod")
    monkeypatch.setenv("PROD_KEY", "pk-test")

    payload = json.loads(
        runner.invoke(app, ["--json", "explainability", "status"], catch_exceptions=False).output
    )

    assert payload["shipping"]["gateway"] == PROD_GATEWAY


def test_a_machine_with_no_targets_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """The single-deployment setup that `init --explainability` produces.

    Resolving through the target must not break the machine that never made
    one — `resolve_target` falls back to the top-level values.
    """
    config = AppConfig()
    config.explainability.ship = True
    config.explainability.gateway_url = STG_GATEWAY
    save_config(config)
    insights.reset_cache()
    monkeypatch.setenv("EXPLAINABILITY_API_KEY", "sk-test")

    state = service.shipping_state()

    assert state.gateway_url == STG_GATEWAY
    assert state.has_key
