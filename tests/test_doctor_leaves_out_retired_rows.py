"""``aisquare doctor`` carries no row for what was retired from it.

The project brain (``brain``), the CI test bed (``ci test bed``, ``ci …``) and
explainability (``explainability…``) are unmaintained and unused, and a row each in every
report was noise. The brain is deleted. The other two are dormant: explainability's section
is ``aisquare explainability doctor`` now (tests/test_explainability_doctor_command.py),
the test bed's rows are ``services.ci_doctor.checks()`` (tests/test_ci_doctor.py).

What holds here is the absence, on the three surfaces a report comes out of, and on the two
machines that would differ: an untouched one, and one that has switched both on, whose
doctor used to grow a row each.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core.config import load_config, save_config
from aisquare.services import ci_client, diagnostics

RETIRED = ("brain", "ci ", "explainability")


def _retired(names: list[str]) -> list[str]:
    return [name for name in names if name == "brain" or name.startswith(RETIRED[1:])]


def _untouched(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    del runner, monkeypatch


def _switched_on(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """``AISQUARE_CI=1`` and explainability enabled in the config, with a target."""
    runner.invoke(app, ["init", "--no-onboard", "--no-explainability"], catch_exceptions=False)
    config = load_config()
    config.explainability.enabled = True
    config.explainability.target = "stg"
    save_config(config)
    monkeypatch.setenv(ci_client.ENABLED_ENV_VAR, "1")


MACHINES: dict[str, Callable[[CliRunner, pytest.MonkeyPatch], None]] = {
    "untouched": _untouched,
    "switched-on": _switched_on,
}


@pytest.mark.parametrize("machine", MACHINES)
@pytest.mark.parametrize("live", [False, True], ids=["offline", "live"])
def test_the_json_report_has_no_retired_row(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, machine: str, live: bool
) -> None:
    MACHINES[machine](runner, monkeypatch)

    result = runner.invoke(app, ["--json", "doctor", *(["--live"] if live else [])])

    names = [row["name"] for row in json.loads(result.stdout)]
    assert "python" in names and "home" in names, "the control: it is a doctor report"
    assert _retired(names) == [], names


@pytest.mark.parametrize("machine", MACHINES)
def test_the_text_report_has_no_retired_row(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, machine: str
) -> None:
    MACHINES[machine](runner, monkeypatch)

    output = runner.invoke(app, ["doctor", "--live"]).output

    assert "python" in output, "the control: it is a doctor report"
    for line in output.splitlines():
        assert not line.lstrip(" ✓⚠✗").startswith(("brain:", "ci ", "explainability")), line


@pytest.mark.parametrize("machine", MACHINES)
def test_the_service_returns_no_retired_row(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, machine: str
) -> None:
    """What the fleet UI's Doctor page and the sidebar read, which is the service's list."""
    MACHINES[machine](runner, monkeypatch)

    assert _retired([check.name for check in diagnostics.doctor(live=True)]) == []


def test_doctor_no_longer_takes_the_explainability_options(runner: CliRunner) -> None:
    for option in (["--target", "stg"], ["--project", "x"], ["-P", "x"]):
        result = runner.invoke(app, ["doctor", *option])
        assert result.exit_code == 2, (option, result.output)
    helped = runner.invoke(app, ["doctor", "--help"]).output
    assert "explainab" not in helped.lower(), helped
    assert "--target" not in helped and "--project" not in helped
