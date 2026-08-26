"""An unconfigured machine must not report a failure it does not have.

Cold, on the train, through the built binary::

    probe:    proxy unreachable at http://127.0.0.1:9090/health:
              <urlopen error [Errno 111] Connection refused>

Nothing is wrong with that machine. The default ``proxy_url`` points at
loopback and no proxy is running there, which is the correct state for an
install that has never asked for tracing. But it reads as broken, and the first
thing an operator does with a line like that at 08:00 is go debug a proxy that
was never meant to exist yet.

The distinction that has to survive: **not configured** (nobody asked for
tracing — informational) versus **configured and down** (someone enabled it and
it is unreachable — genuinely red, because launches will silently go untraced).

Every assertion reads the rendered line, not the value passed in. The default
``proxy_url`` is deliberately unreachable and that is not the bug — the wording
is — so nothing here changes the default or the exit-code rule.
"""

from __future__ import annotations

import socket

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core.config import AppConfig, ExplainabilityTarget, save_config
from aisquare.models import CheckStatus
from aisquare.services import explainability_ops as ops

#: Words that make an operator open a terminal and start debugging.
ALARMING = ("unreachable", "Connection refused", "error", "refused", "failed")


def _cold() -> None:
    """A machine that has never been told about tracing: stock config."""
    save_config(AppConfig())


def _configured(*, enabled: bool, proxy_url: str = "http://127.0.0.1:9199") -> None:
    config = AppConfig()
    config.explainability.enabled = enabled
    config.explainability.targets = {
        config.explainability.target: ExplainabilityTarget(
            gateway_url="https://gateway.example", proxy_url=proxy_url
        )
    }
    save_config(config)


def _probe_line(output: str) -> str:
    return next(line for line in output.splitlines() if line.startswith("probe:"))


# --- the cold machine ---


def test_a_cold_machine_reports_no_failure(runner: CliRunner) -> None:
    _cold()

    result = runner.invoke(app, ["explainability", "status"], catch_exceptions=False)

    assert result.exit_code == 0
    line = _probe_line(result.output)
    for word in ALARMING:
        assert word.lower() not in line.lower(), f"cold machine reads as broken: {line!r}"


def test_a_cold_machine_says_what_is_actually_true(runner: CliRunner) -> None:
    """Silence would be worse than a wrong line: say it is not configured."""
    _cold()

    line = _probe_line(runner.invoke(app, ["explainability", "status"]).output)

    assert "not configured" in line.lower(), line


def test_a_cold_machine_opens_no_socket(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing to probe means nothing to wait for — and nothing to hang on.

    A status that dials a default address the operator never chose is spending
    their time on a question nobody asked.
    """
    _cold()

    def _forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("status probed a proxy nobody configured")

    monkeypatch.setattr(socket, "socket", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)

    result = runner.invoke(app, ["explainability", "status"], catch_exceptions=False)

    assert result.exit_code == 0


def test_a_cold_doctor_has_nothing_but_ok(runner: CliRunner) -> None:
    _cold()

    checks = ops.checks()

    assert [c.name for c in checks if c.status is not CheckStatus.ok] == []


# --- configured and down: still unmistakably red ---


def test_tracing_on_with_a_dead_proxy_is_still_red(runner: CliRunner) -> None:
    _configured(enabled=True)

    result = runner.invoke(app, ["explainability", "status"])

    assert result.exit_code == 1, "the exit-code rule must not move"
    line = _probe_line(result.output)
    assert any(word.lower() in line.lower() for word in ALARMING), line


def test_the_red_line_still_says_what_to_do(runner: CliRunner) -> None:
    """A red line without a next step is a red line people learn to ignore."""
    _configured(enabled=True)

    line = _probe_line(runner.invoke(app, ["explainability", "status"]).output)

    assert "untraced" in line.lower() or "enable" in line.lower(), line


def test_doctor_still_fails_loudly_when_tracing_is_on_and_the_proxy_is_down() -> None:
    _configured(enabled=True)

    proxy = next(c for c in ops.checks() if "proxy" in c.name)

    assert proxy.status is CheckStatus.fail
    assert proxy.fix, "a failing check must carry its remediation"


# --- configured but not switched on: informational, not red ---


def test_a_configured_proxy_with_tracing_off_is_not_an_error(runner: CliRunner) -> None:
    """They set a URL but have not turned tracing on. Nothing is wrong yet."""
    _configured(enabled=False)

    result = runner.invoke(app, ["explainability", "status"], catch_exceptions=False)

    assert result.exit_code == 0
    line = _probe_line(result.output)
    assert "tracing is off" in line.lower(), line


# --- the machine-readable view has to agree with the human one ---


def test_json_reports_the_same_state(runner: CliRunner) -> None:
    import json

    _cold()

    payload = json.loads(
        runner.invoke(app, ["--json", "explainability", "status"], catch_exceptions=False).output
    )

    assert "not configured" in payload["probe"].lower()


def test_both_surfaces_render_one_sentence(runner: CliRunner) -> None:
    """status and doctor must not drift into describing one state differently."""
    _configured(enabled=True)

    line = _probe_line(runner.invoke(app, ["explainability", "status"]).output)
    proxy = next(c for c in ops.checks() if "proxy" in c.name)

    assert proxy.detail in line, f"status={line!r} doctor={proxy.detail!r}"
