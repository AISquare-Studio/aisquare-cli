"""A key stored on disk carries no deployment identity, so it cannot roam.

`~/.aisquare/explainability-key` is one unlabelled key. That is exactly right
for the single-deployment machine `init --explainability` produces, and exactly
wrong the moment an operator names a variable per deployment with
`enable --target prod --key-env PROD_KEY`.

The hazard is not theoretical — reproduced through the built binary. Follow the
CLI's own advice ("set $KEY or WRITE <key file>") while on staging, then switch
to prod with `PROD_KEY` absent from the shell, and the stored STAGING key was
handed to the PROD gateway. The other direction is worse: a stored PROD key sent
to a staging host is a credential disclosed to the wrong endpoint.

The rule: the stored file answers only when the active target has not named a
variable of its own. Naming one is a declaration that this deployment's key
lives THERE, and an unlabelled file cannot stand in for it.
"""

from __future__ import annotations

import pytest

from aisquare.core import insights
from aisquare.core.config import AppConfig, ExplainabilityTarget, save_config
from aisquare.services import explainability as service

STG = "https://stg.example"
PROD = "https://prod.example"


@pytest.fixture(autouse=True)
def _fresh() -> None:
    insights.reset_cache()


def _configure(*, targets: dict[str, ExplainabilityTarget] | None, active: str = "stg") -> None:
    config = AppConfig()
    config.explainability.ship = True
    config.explainability.gateway_url = STG
    config.explainability.target = active
    if targets is not None:
        config.explainability.targets = targets
    save_config(config)
    insights.reset_cache()


def test_a_stored_key_does_not_satisfy_a_target_that_named_its_own_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reproduced hazard: staging key on disk, prod target active."""
    _configure(
        targets={"prod": ExplainabilityTarget(gateway_url=PROD, api_key_env="PROD_KEY")},
        active="prod",
    )
    service.store_api_key("sk-staging-secret")
    monkeypatch.delenv("PROD_KEY", raising=False)

    _, key_env, key = service._active_deployment()

    assert key_env == "PROD_KEY"
    assert key is None, "an unlabelled stored key was handed to a deployment it was not issued for"


def test_shipping_refuses_and_names_the_variable_it_wants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(
        targets={"prod": ExplainabilityTarget(gateway_url=PROD, api_key_env="PROD_KEY")},
        active="prod",
    )
    service.store_api_key("sk-staging-secret")
    monkeypatch.delenv("PROD_KEY", raising=False)

    report = service.ship_once()

    assert report.sent == 0
    assert "PROD_KEY" in report.reason, report.reason


def test_the_target_variable_is_used_when_it_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refusing must not mean refusing everything — the named key still works."""
    _configure(
        targets={"prod": ExplainabilityTarget(gateway_url=PROD, api_key_env="PROD_KEY")},
        active="prod",
    )
    service.store_api_key("sk-staging-secret")
    monkeypatch.setenv("PROD_KEY", "pk-prod-secret")

    _, _, key = service._active_deployment()

    assert key == "pk-prod-secret"


def test_the_single_deployment_machine_still_uses_its_stored_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No target named a variable, so the unlabelled file is unambiguous.

    This is what `init --explainability` produces and it must keep working —
    a fix that locks out the common setup is not a fix.
    """
    _configure(targets=None)
    service.store_api_key("sk-only-one-deployment")
    monkeypatch.delenv("EXPLAINABILITY_API_KEY", raising=False)

    _, key_env, key = service._active_deployment()

    assert key_env == service.KEY_ENV_VAR
    assert key == "sk-only-one-deployment"


def test_a_target_that_kept_the_default_variable_still_uses_the_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A target exists but never named a variable — still unambiguous."""
    _configure(targets={"stg": ExplainabilityTarget(gateway_url=STG)}, active="stg")
    service.store_api_key("sk-only-one-deployment")
    monkeypatch.delenv("EXPLAINABILITY_API_KEY", raising=False)

    _, _, key = service._active_deployment()

    assert key == "sk-only-one-deployment"


def test_the_refusal_does_not_advise_writing_the_file_when_it_would_be_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Telling someone to write a file we will then refuse to read is worse than silence."""
    _configure(
        targets={"prod": ExplainabilityTarget(gateway_url=PROD, api_key_env="PROD_KEY")},
        active="prod",
    )
    monkeypatch.delenv("PROD_KEY", raising=False)

    reason = service.shipping_state().reason

    assert "PROD_KEY" in reason
    assert "explainability-key" not in reason, reason
