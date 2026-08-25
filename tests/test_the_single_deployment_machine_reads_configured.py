"""The shape `init --explainability` produces must not read as unconfigured.

`init --explainability` writes the top-level `gateway_url` and the machine-local
key file and creates NO target. That is the shape most machines are in, and the
operational surfaces called it unconfigured while the shipping path shipped from
it perfectly.

MEASURED at 3e575f8, one config, one `status --json` payload:

    "gateway": ""                                  <- resolve_target
    "gateway_source": "unset"
    "key_set": false                               <- resolve_target
    "shipping": {"gateway": "https://stg…"}        <- _active_deployment

So one command reported the gateway as both unset and set, and the key as both
absent and present. `doctor` agreed with the wrong half, and `register` refused
to run until the operator re-exported credentials the machine already had.

WHY IT SURVIVED REVIEW. Two docstrings said the fallback was already there —
`_active_deployment`'s ("`resolve_target` falls back to the top-level values
when no target was ever created") and `shipping_offer`'s. It was not:
`resolve_target` read the target then `$EXPLAINABILITY_GATEWAY_URL` and stopped,
and `_active_deployment` was quietly compensating with an
`or settings.gateway_url` and a key-file read of its own. A reader checking
whether the fallback existed found a sentence saying yes.

Both fallbacks now live in `resolve_target`, which is the one function both AST
guards in this directory permit to resolve either, and `_active_deployment` is a
projection of it. This file pins the agreement from the outside — through the
public surfaces an operator actually reads — so a future divergence fails here
rather than at 08:00 on a cutover.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core.config import load_config, save_config
from aisquare.services import explainability as service
from aisquare.services import explainability_ops as ops

_GATEWAY = "https://single-deployment.example"
_FILE_KEY = "-".join(["not", "a", "real", "file", "key"])


@pytest.fixture
def single_deployment(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """Top-level gateway plus the key file, and no targets at all.

    Built by writing what `init --explainability` writes rather than by calling
    `enable --target`, because a target is precisely what this machine does not
    have — creating one would test the path that already worked.
    """
    runner.invoke(app, ["init", "--yes"], catch_exceptions=False)
    monkeypatch.delenv(service.GATEWAY_ENV_VAR, raising=False)
    monkeypatch.delenv(service.KEY_ENV_VAR, raising=False)
    monkeypatch.delenv(ops.TARGET_ENV_VAR, raising=False)

    config = load_config()
    config.explainability.enabled = True
    config.explainability.ship = True
    config.explainability.gateway_url = _GATEWAY
    config.explainability.targets = {}
    save_config(config)
    service.store_api_key(_FILE_KEY)


def test_the_resolver_names_the_gateway_the_shipping_path_uses(
    single_deployment: None,
) -> None:
    """THE defect, at the seam: two resolvers, one machine, two answers."""
    resolved = ops.resolve_target(load_config().explainability)
    shipping_gateway, _key_env, _key = service._active_deployment()

    assert resolved.gateway_url == shipping_gateway
    assert resolved.gateway_url == _GATEWAY
    assert resolved.gateway_source == "config", (
        "a gateway that came from config must not be reported as unset or as env"
    )


def test_the_resolver_finds_the_key_the_shipping_path_finds(
    single_deployment: None,
) -> None:
    """The other half. `register` refuses to run without this one."""
    resolved = ops.resolve_target(load_config().explainability)
    _gateway, _key_env, shipping_key = service._active_deployment()

    assert resolved.api_key == shipping_key
    assert resolved.api_key == _FILE_KEY


def test_status_does_not_contradict_itself(single_deployment: None, runner: CliRunner) -> None:
    """The operator-facing form: one payload, one answer.

    Asserted on `--json` rather than the prose because this is the surface the
    cutover runbook greps, and because the contradiction was legible in one line
    of it.
    """
    result = runner.invoke(app, ["explainability", "status", "--json"], catch_exceptions=False)

    payload = json.loads(result.output)

    assert payload["gateway"] == _GATEWAY
    assert payload["gateway_source"] == "config"
    assert payload["key_set"] is True
    assert payload["shipping"]["gateway"] == payload["gateway"], (
        "the same command named two deployments: "
        f"{payload['gateway']!r} and {payload['shipping']['gateway']!r}"
    )


def test_a_named_target_still_wins_over_the_top_level(
    single_deployment: None, runner: CliRunner
) -> None:
    """The precedence this must not disturb, or the fix trades one bug for worse.

    The top-level value is the LAST fallback, not an alternative. A machine that
    moved to a prod target must resolve prod — the split-brain incident
    `_active_deployment` records is a staging value reaching a prod deployment,
    and re-opening it from this direction would be the same defect wearing the
    fix's clothes.
    """
    runner.invoke(
        app,
        [
            "explainability",
            "enable",
            "--target",
            "prod",
            "--gateway-url",
            "https://prod.example",
        ],
        catch_exceptions=False,
    )

    resolved = ops.resolve_target(load_config().explainability)

    assert resolved.gateway_url == "https://prod.example"
    assert resolved.gateway_url != _GATEWAY


def test_the_key_file_still_cannot_satisfy_a_target_that_named_its_own_variable(
    single_deployment: None, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """The safety property the key fallback must not spend to buy convenience.

    The file holds ONE UNLABELLED key. Following the CLI's own "or write <key
    file>" advice on staging and then switching to prod sent the STAGING key to
    the PROD gateway — the incident behind
    `tests/test_key_never_crosses_deployments.py`. So the file answers only for
    a target naming the DEFAULT variable, and teaching `resolve_target` the
    fallback had to preserve that rather than notice it afterwards.
    """
    runner.invoke(
        app,
        [
            "explainability",
            "enable",
            "--target",
            "prod",
            "--gateway-url",
            "https://prod.example",
            "--key-env",
            "MY_OWN_KEY_VAR",
        ],
        catch_exceptions=False,
    )
    monkeypatch.delenv("MY_OWN_KEY_VAR", raising=False)

    resolved = ops.resolve_target(load_config().explainability)

    assert resolved.api_key is None, (
        "the unlabelled key file satisfied a target that named its own variable"
    )


def test_the_key_source_names_the_file_when_the_file_won(single_deployment: None) -> None:
    """Provenance, not just presence.

    `resolve_target` carried `api_key_env` as the key's origin, which WAS the
    provenance while the environment was the only place a key could come from.
    The key-file fallback broke that silently: `api_key` filled from the file
    while `api_key_env` still named a variable nobody had set.
    """
    resolved = ops.resolve_target(load_config().explainability)

    assert resolved.api_key == _FILE_KEY
    assert resolved.key_source == "file"
    assert resolved.key_origin == str(service.key_path())
    assert "$" not in resolved.key_origin, "a file path must not be rendered as a variable"


def test_the_key_source_names_the_variable_when_the_variable_won(
    single_deployment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control. A `key_source` stuck on "file" would pass the test above."""
    monkeypatch.setenv(service.KEY_ENV_VAR, "-".join(["not", "a", "real", "env", "key"]))

    resolved = ops.resolve_target(load_config().explainability)

    assert resolved.key_source == "env"
    assert resolved.key_origin == f"${service.KEY_ENV_VAR}"


def test_no_surface_claims_an_unset_variable_holds_the_key(
    single_deployment: None, runner: CliRunner
) -> None:
    """The operator-facing form, on all three surfaces at once.

    Measured before the fix, with the variable unset and the file populated:
    `status` printed "key: $EXPLAINABILITY_API_KEY is set" and `doctor` printed
    "key from $EXPLAINABILITY_API_KEY". Both false, and both send someone to
    rotate or debug a credential source that is not in play.
    """
    status = runner.invoke(app, ["explainability", "status"], catch_exceptions=False).output
    payload = json.loads(
        runner.invoke(app, ["explainability", "status", "--json"], catch_exceptions=False).output
    )
    doctor = runner.invoke(app, ["doctor"], catch_exceptions=False).output

    key_file = str(service.key_path())
    assert f"${service.KEY_ENV_VAR} is set" not in status, status
    assert key_file in status, status
    assert payload["key_source"] == "file"
    assert payload["key_origin"] == key_file
    assert f"key from ${service.KEY_ENV_VAR}" not in doctor, doctor


def test_key_set_still_answers_the_question_a_script_was_asking(
    single_deployment: None, runner: CliRunner
) -> None:
    """`key_source` is added, `key_set` is not repurposed.

    The runbook greps this payload, so a field changing meaning is worse than a
    field being added: `key_set` keeps meaning "a key was resolved", which is
    what it has always been asked.
    """
    payload = json.loads(
        runner.invoke(app, ["explainability", "status", "--json"], catch_exceptions=False).output
    )

    assert payload["key_set"] is True
    assert payload["key_env"] == service.KEY_ENV_VAR, (
        "key_env must keep naming the variable the TARGET names, set or not"
    )
