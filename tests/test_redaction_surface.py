"""An operator wiring prod must be able to SEE what is about to leave the machine.

``config.redaction.level`` was read by nothing until tonight, so the setting has
a history of lying: you could set ``strict`` and be no safer. Now that it is
honoured, the thing that makes it trustworthy is not the mechanism — it is being
able to read the active level in the same breath as the shipping counts, without
opening config.toml and hoping.

The wording carries a distinction that is easy to blur and expensive to get
wrong: redaction applies to what LEAVES. ``aisquare log`` and the board rows keep
exactly what was typed. A line that implied otherwise would send someone hunting
for their own prompts in a scrubbed local history.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import insights
from aisquare.core.config import AppConfig, ExplainabilityTarget, save_config
from aisquare.models import CheckStatus, RedactionLevel
from aisquare.services import explainability_ops as ops


def _configure(level: RedactionLevel = RedactionLevel.standard, *, ship: bool = True) -> None:
    """A machine that has been pointed at a deployment, with shipping on.

    The target matters: doctor keeps an untouched machine to ONE line, so a
    config with no target would test the silent path, not this one.
    """
    config = AppConfig()
    config.redaction.level = level
    config.explainability.ship = ship
    config.explainability.gateway_url = "https://gateway.example"
    config.explainability.targets = {
        config.explainability.target: ExplainabilityTarget(gateway_url="https://gateway.example")
    }
    save_config(config)
    insights.reset_cache()


@pytest.fixture(autouse=True)
def _fresh() -> None:
    insights.reset_cache()


# --- status names the level, next to the counts an operator already reads ---


def test_status_states_the_active_redaction_level(runner: CliRunner) -> None:
    _configure(RedactionLevel.strict)

    result = runner.invoke(app, ["explainability", "status"], catch_exceptions=False)

    assert "redaction:" in result.output
    assert "strict" in result.output


def test_status_puts_it_in_the_same_block_as_the_shipping_counts(runner: CliRunner) -> None:
    """Same block, because that is where someone checking "what am I sending" looks."""
    _configure()

    result = runner.invoke(app, ["explainability", "status"], catch_exceptions=False)

    lines = [line for line in result.output.splitlines() if line.strip()]
    spool = next(i for i, line in enumerate(lines) if line.startswith("spool:"))
    redaction = next(i for i, line in enumerate(lines) if line.startswith("redaction:"))
    assert abs(redaction - spool) <= 2, result.output


def test_status_says_the_scrub_applies_to_what_leaves_not_what_is_kept(
    runner: CliRunner,
) -> None:
    """The distinction is the whole point; a vague line here is a wrong answer."""
    _configure()

    result = runner.invoke(app, ["explainability", "status"], catch_exceptions=False)

    line = next(
        line for line in result.output.splitlines() if line.strip().startswith("redaction:")
    )
    assert "leav" in line.lower(), f"the line must say this is about what LEAVES: {line!r}"
    assert "local" in line.lower(), f"the line must say local capture is untouched: {line!r}"


@pytest.mark.parametrize("level", list(RedactionLevel))
def test_every_level_renders_as_a_stated_setting(level: RedactionLevel, runner: CliRunner) -> None:
    """Including ``off``: someone chose it, and a choice is not an error."""
    _configure(level)

    result = runner.invoke(app, ["explainability", "status"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert str(level) in result.output


# --- --json, because status is what a cutover gets scripted against ---


def test_status_honours_json(runner: CliRunner) -> None:
    """It printed human text under --json while its siblings returned JSON.

    ``status`` is THE command an operator scripts the cutover against, so a
    human-only status turns every check into a grep against prose.
    """
    _configure(RedactionLevel.strict)

    result = runner.invoke(app, ["--json", "explainability", "status"], catch_exceptions=False)

    payload = json.loads(result.output)
    assert payload["redaction"] == "strict"
    assert payload["enabled"] is False
    assert payload["shipping"]["queued"] == 0
    assert "reason" in payload["shipping"]


def test_json_carries_every_field_the_human_view_shows(runner: CliRunner) -> None:
    """Otherwise a scripted cutover check silently reads less than a human does."""
    _configure()

    human = runner.invoke(app, ["explainability", "status"], catch_exceptions=False).output
    payload = json.loads(
        runner.invoke(app, ["--json", "explainability", "status"], catch_exceptions=False).output
    )

    labels = {line.split(":", 1)[0] for line in human.splitlines() if ":" in line}
    # Three labels are carried under a different shape rather than dropped:
    # `key` splits into key_env/key_set, and `spool` nests under `shipping`
    # with its counts as numbers instead of a rendered sentence.
    restructured = {"key", "spool"}
    for label in labels - restructured:
        assert label in payload, f"the human view shows {label!r} and --json does not"
    assert {"key_env", "key_set"} <= payload.keys()
    assert {"queued", "sent", "dead"} <= payload["shipping"].keys()


# --- doctor states it too ---


def test_doctor_reports_the_redaction_level(runner: CliRunner) -> None:
    _configure()

    checks = ops.checks()

    redaction = next((c for c in checks if "redaction" in c.name), None)
    assert redaction is not None, [c.name for c in checks]
    assert "standard" in redaction.detail


def test_doctor_does_not_treat_off_as_a_failure() -> None:
    """``off`` is a configuration, not a broken machine — no red line for it."""
    _configure(RedactionLevel.off)

    checks = ops.checks()

    redaction = next(c for c in checks if "redaction" in c.name)
    assert redaction.status is not CheckStatus.fail
    assert "off" in redaction.detail


def test_doctor_says_plainly_that_local_capture_is_untouched() -> None:
    _configure()

    redaction = next(c for c in ops.checks() if "redaction" in c.name)

    assert "local" in redaction.detail.lower()
    assert "leav" in redaction.detail.lower()


def test_an_untouched_machine_still_gets_no_explainability_section() -> None:
    """A machine that never enabled tracing must not grow a section about it.

    The one-line rule is why the rest of doctor's output still gets read; a new
    check must not be the thing that breaks it.
    """
    save_config(AppConfig())
    insights.reset_cache()

    checks = ops.checks()

    assert len(checks) == 1, [c.name for c in checks]
