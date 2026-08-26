"""A timer wants to be told it shipped nothing; an interactive run does not.

§5b tells the operator to run `ship` on a timer. Three measured facts combine
into silent permanent data loss: cron has almost no environment so a naive line
has no key; `ship` exits 0 when it cannot ship; and crontab lines are
conventionally written with output discarded. Exit 0 plus discarded stdout is a
timer that reports healthy forever while the spool never drains — with the
proxy lane working perfectly throughout, which is what makes it invisible.

The default must not change. "No key or config implies nothing captured and
nothing logged as an error" is doctrine, and making `ship` exit non-zero would
spam cron for every operator who deliberately does not ship. The asymmetry is
real and it is the whole design: interactive wants quiet, a timer wants loud.
So the loudness is opt-in.

What `--strict` fires on is the line worth getting right. There are three
states where a run cannot ship AND the next run cannot either until a human
changes something — shipping off, no gateway, no key — plus the missing extra.
Those are configuration, and a timer should shout. A DEFERRAL is different: an
unreachable gateway leaves the records queued and the next tick retries, which
is the design working. Failing loudly on a transient outage would train the
operator to ignore the mail, which is how the quiet failure gets back in.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from aisquare.cli import explainability as ship_cli
from aisquare.cli.app import app
from aisquare.core.config import AppConfig, ExplainabilityTarget, save_config
from aisquare.services import explainability as svc

_KEY_VAR = "MY_KEY_VAR"


def _configured(*, ship: bool = True, gateway: str = "https://gw.invalid") -> None:
    config = AppConfig()
    config.explainability.ship = ship
    config.explainability.target = "stg"
    config.explainability.targets["stg"] = ExplainabilityTarget(
        gateway_url=gateway, api_key_env=_KEY_VAR
    )
    save_config(config)


@pytest.mark.parametrize(
    ("label", "ship", "gateway"),
    [("shipping off", False, "https://gw.invalid"), ("no gateway", True, "")],
)
def test_strict_fails_on_every_state_that_cannot_ship(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, label: str, ship: bool, gateway: str
) -> None:
    """Parametrised because a flag that catches one of three is worse than none.

    An operator who adds --strict and still loses data has been given a false
    receipt, which is the failure this whole section is about.
    """
    _configured(ship=ship, gateway=gateway)
    monkeypatch.delenv(_KEY_VAR, raising=False)

    default = runner.invoke(app, ["explainability", "ship"], catch_exceptions=False)
    strict = runner.invoke(app, ["explainability", "ship", "--strict"], catch_exceptions=False)

    assert default.exit_code == 0, f"{label}: the quiet default must not change"
    # Exactly 1, not merely non-zero: an unknown option exits 2, so `!= 0` would
    # pass before the flag existed. That is the vacuous shape this file is about.
    assert strict.exit_code == 1, f"{label}: a timer was told this run was healthy"
    assert "No such option" not in strict.output


def test_strict_fails_when_the_key_is_absent(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The state cron actually lands in: everything configured, no key in scope."""
    _configured()
    monkeypatch.delenv(_KEY_VAR, raising=False)

    default = runner.invoke(app, ["explainability", "ship"], catch_exceptions=False)
    strict = runner.invoke(app, ["explainability", "ship", "--strict"], catch_exceptions=False)

    assert default.exit_code == 0
    assert strict.exit_code == 1
    assert "No such option" not in strict.output
    assert _KEY_VAR in strict.output, "say which variable, so the cron mail is actionable"


def test_an_empty_spool_is_success_under_strict(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing to ship is not the same as cannot ship.

    A timer firing every five minutes will find an empty spool most of the time.
    If that were a failure the operator would mute the mail within an hour and
    the real signal would go with it.
    """
    _configured()
    monkeypatch.setenv(_KEY_VAR, "-".join(["not", "a", "real", "key"]))
    monkeypatch.setattr(svc, "sdk_available", lambda: True)

    result = runner.invoke(app, ["explainability", "ship", "--strict"], catch_exceptions=False)

    assert result.exit_code == 0, result.output


def test_a_deferral_stays_quiet_under_strict(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreachable gateway is the design working, and the timer is the retry.

    Distinguishing this from a configuration block is the point of the flag: one
    is fixed by the next tick, the other by a human.
    """
    _configured()
    monkeypatch.setenv(_KEY_VAR, "-".join(["not", "a", "real", "key"]))
    monkeypatch.setattr(svc, "sdk_available", lambda: True)
    monkeypatch.setattr(
        ship_cli,
        "ship_once",
        lambda **_kw: svc.ShipReport(deferred=3, reason="gateway unreachable"),
    )

    result = runner.invoke(app, ["explainability", "ship", "--strict"], catch_exceptions=False)

    assert result.exit_code == 0, "a transient deferral must not train the operator to ignore mail"


def test_dead_lettering_still_fails_without_the_flag(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The existing non-zero case is untouched by adding an opt-in one."""
    _configured()
    monkeypatch.setattr(
        ship_cli, "ship_once", lambda **_kw: svc.ShipReport(dead=1, reason="dropped")
    )

    result = runner.invoke(app, ["explainability", "ship"], catch_exceptions=False)

    assert result.exit_code == 1


@pytest.mark.parametrize(
    ("label", "ship", "gateway"),
    [("shipping off", False, "https://gw.invalid"), ("no gateway", True, "")],
)
def test_the_json_blocked_field_means_what_strict_does(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, label: str, ship: bool, gateway: str
) -> None:
    """`blocked` is what a timer wrapper reads, and it only parsed until now.

    The sweep in ``test_json_stdout_is_machine_readable.py`` asserts this
    command's stdout is JSON. A payload of ``{}`` would satisfy that. The field
    that matters is ``blocked``, which exists — its own comment says so — set
    rather than inferred "so no caller has to match on message text", and a
    wrapper keyed on it would misreport forever if it silently went missing:
    the same silent-spool failure §5b exists to prevent, arriving through the
    machine-readable door instead.

    Pinned against the BEHAVIOUR rather than as a constant: whatever `--strict`
    does in this state, `blocked` has to agree with it. A future state where
    both flip stays consistent; one where only one flips fails here.
    """
    _configured(ship=ship, gateway=gateway)
    monkeypatch.delenv(_KEY_VAR, raising=False)

    machine = runner.invoke(app, ["--json", "explainability", "ship"], catch_exceptions=False)
    human = runner.invoke(app, ["explainability", "ship"], catch_exceptions=False)
    strict = runner.invoke(app, ["explainability", "ship", "--strict"], catch_exceptions=False)

    payload = json.loads(machine.stdout)
    assert payload["blocked"] is (strict.exit_code != 0), (
        f"{label}: --strict exited {strict.exit_code} while the payload says "
        f"blocked={payload['blocked']} — a timer reading one and a human reading "
        "the other would disagree about the same run"
    )
    assert payload["reason"] == human.stdout.strip().splitlines()[0], (
        f"{label}: the two renderings give different reasons for the same run"
    )
    assert {"sent", "deferred", "dead", "runs"} <= payload.keys(), payload
