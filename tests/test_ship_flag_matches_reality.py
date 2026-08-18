"""`ship=True` on a machine that cannot ship is a spool that fills forever.

``configure_shipping`` states the invariant itself:

    ship is the single predicate the primary path consults, so it must never be
    True in a state that would buffer forever. Both halves are therefore
    resolved BEFORE it is written … which is what makes "no key/config ⇒
    nothing captured" true BY CONSTRUCTION rather than by vigilance.

It resolved neither half through the one resolver, so both directions were
wrong. Measured at 564eb49, temp home, fake values:

    target names MY_KEY_VAR (UNSET) + unlabelled key file + top-level gateway
      resolve_api_key()        -> a key      (fixed var, then THE FILE)
      _active_deployment() key -> None       (through the TARGET's variable)
      configure_shipping()     -> ship = TRUE
      shipping_state().has_key -> False
      ship_once()              -> "no workspace key — set $MY_KEY_VAR"

REACHABLE BY FOLLOWING OUR OWN INSTRUCTIONS. §5 of the cutover runbook has the
operator export ``$EXPLAINABILITY_GATEWAY_URL``, which ``configure_shipping``
writes into the top-level ``gateway_url``. Then a target with its own key
variable. Then the CLI's own advice — "set $VAR or write <key file>" — produces
the unlabelled file. Three documented steps, and shipping is on and permanently
buffering while §5b's timer reports healthy.

THE OPPOSITE FAILURE HAS THE SAME CAUSE. The flag was decided from the
TOP-LEVEL ``gateway_url``, not the resolved one, so a machine whose gateway
lives only in a target — what ``explainability enable --target X --gateway-url
Y`` writes — could never turn shipping on at all, and ``init --explainability``
looked like it did nothing.

Both halves now resolve through ``_active_deployment``, the resolver
@8dd460fb established for the gateway and I extended to the key in
``shipping_offer``. This is the writer, so the order matters: store what the
operator passed FIRST, then ask whether the machine can now actually ship.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core.config import load_config, save_config
from aisquare.services import explainability as service

_FILE_KEY = "-".join(["not", "a", "real", "file", "key"])
_ENV_KEY = "-".join(["not", "a", "real", "env", "key"])
_CUSTOM_VAR = "MY_KEY_VAR"


@pytest.fixture(autouse=True)
def _no_ambient_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shell this suite runs in must not supply a key by accident."""
    for name in (_CUSTOM_VAR, service.KEY_ENV_VAR, service.GATEWAY_ENV_VAR):
        monkeypatch.delenv(name, raising=False)


def _target_with_its_own_key_var(runner: CliRunner) -> None:
    runner.invoke(app, ["init", "--yes"], catch_exceptions=False)
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
            _CUSTOM_VAR,
        ],
        catch_exceptions=False,
    )


def _set_top_level_gateway(url: str) -> None:
    """What `configure_shipping` writes when $EXPLAINABILITY_GATEWAY_URL is set."""
    config = load_config()
    config.explainability.gateway_url = url
    save_config(config)


def test_ship_is_not_turned_on_by_a_key_the_target_does_not_name(
    isolated_home: Path, runner: CliRunner
) -> None:
    """THE defect: ship=True, has_key False, and the spool fills forever."""
    _target_with_its_own_key_var(runner)
    _set_top_level_gateway("https://top-level.example")
    service.store_api_key(_FILE_KEY)

    service.configure_shipping()

    assert load_config().explainability.ship is False, (
        "shipping was turned on from an unlabelled key file that the target's "
        "variable does not name — nothing will ever drain the spool"
    )


def test_the_flag_agrees_with_what_shipping_reports(isolated_home: Path, runner: CliRunner) -> None:
    """Stated as agreement, so a fix cannot satisfy it by breaking the other side."""
    _target_with_its_own_key_var(runner)
    _set_top_level_gateway("https://top-level.example")
    service.store_api_key(_FILE_KEY)

    service.configure_shipping()

    assert load_config().explainability.ship is service.shipping_state().has_key


def test_a_target_only_gateway_can_still_turn_shipping_on(
    isolated_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The opposite failure, same cause: the flag read the TOP-LEVEL gateway.

    `explainability enable --target X --gateway-url Y` leaves the top-level
    value empty — measured — so this machine could never turn shipping on even
    with a perfectly good key, and `init --explainability` looked inert.
    """
    _target_with_its_own_key_var(runner)
    monkeypatch.setenv(_CUSTOM_VAR, _ENV_KEY)
    assert load_config().explainability.gateway_url == "", "fixture premise: top-level empty"

    service.configure_shipping()

    assert load_config().explainability.ship is True, (
        "a machine whose gateway lives in its target could not turn shipping on"
    )


def test_the_no_target_machine_still_turns_on_from_the_key_file(
    isolated_home: Path, runner: CliRunner
) -> None:
    """The boundary, inherited twice now: `init --explainability`'s own machine.

    It names no target, so routing through the resolver must not stop the key
    FILE counting — `_active_deployment` falls back to it when the target names
    the DEFAULT variable, which is what a bare init produces.
    """
    runner.invoke(app, ["init", "--yes"], catch_exceptions=False)
    _set_top_level_gateway("https://only-source.example")
    service.store_api_key(_FILE_KEY)

    service.configure_shipping()

    assert load_config().explainability.ship is True


def test_the_no_target_machine_still_turns_on_from_the_default_variable(
    isolated_home: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Other half of that boundary: the exported default variable."""
    runner.invoke(app, ["init", "--yes"], catch_exceptions=False)
    _set_top_level_gateway("https://only-source.example")
    monkeypatch.setenv(service.KEY_ENV_VAR, _ENV_KEY)

    service.configure_shipping()

    assert load_config().explainability.ship is True


def test_a_missing_half_still_leaves_the_flag_alone(isolated_home: Path, runner: CliRunner) -> None:
    """The clause the docstring calls "true by construction"."""
    runner.invoke(app, ["init", "--yes"], catch_exceptions=False)
    _set_top_level_gateway("https://only-source.example")

    service.configure_shipping()

    assert load_config().explainability.ship is False, "no key, yet shipping was enabled"
