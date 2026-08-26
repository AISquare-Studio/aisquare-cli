"""``--live`` is the flag that promises network calls; it should keep the promise.

With tracing off, plain ``status`` and plain ``doctor`` deliberately do not dial
the proxy — a machine that is not tracing cannot have a broken tracing proxy,
and dialling a default address the operator never chose wastes their time. That
decision stands and is not revisited here.

But it left mid-cutover with no way to answer "is the proxy I just started
answering?" before enabling tracing, and ``--live`` is precisely the flag whose
meaning is "make the calls". Skipping it there makes ``--live`` quietly less
than it says.

The line this must not cross: an UNCONFIGURED default is still never dialled,
``--live`` or not. Nobody asked about that address.
"""

from __future__ import annotations

import socket
from collections.abc import Callable

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core.config import AppConfig, ExplainabilityTarget, save_config
from aisquare.models import CheckStatus, DoctorCheck
from aisquare.services import explainability_ops as ops
from aisquare.services.explainability import ProxyProbe

CONFIGURED_PROXY = "http://127.0.0.1:9199"


def _cold() -> None:
    save_config(AppConfig())


def _configured(*, enabled: bool = False) -> None:
    config = AppConfig()
    config.explainability.enabled = enabled
    config.explainability.targets = {
        config.explainability.target: ExplainabilityTarget(
            gateway_url="https://gateway.example", proxy_url=CONFIGURED_PROXY
        )
    }
    save_config(config)


def _answers(healthy: bool) -> Callable[[str], ProxyProbe]:
    reason = "proxy healthy" if healthy else f"proxy unreachable at {CONFIGURED_PROXY}/health"

    def _prober(url: str) -> ProxyProbe:
        return ProxyProbe(healthy, reason)

    return _prober


def _proxy_check(checks: list[DoctorCheck]) -> DoctorCheck:
    return next(check for check in checks if "proxy" in check.name)


# --- what --live now answers ---


def test_live_probes_a_configured_proxy_that_is_up(monkeypatch: pytest.MonkeyPatch) -> None:
    _configured()
    monkeypatch.setattr(ops, "probe_proxy", _answers(True))

    check = _proxy_check(ops.checks(live=True))

    assert check.status is CheckStatus.ok
    assert "answered" in check.detail.lower(), check.detail
    assert "tracing is off" in check.detail.lower(), (
        "it answered, but nothing is being traced — an operator must not read this as done"
    )


def test_live_says_so_when_a_configured_proxy_is_down_without_calling_it_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing is traced, so nothing is broken — but they asked, so answer."""
    _configured()
    monkeypatch.setattr(ops, "probe_proxy", _answers(False))

    check = _proxy_check(ops.checks(live=True))

    assert check.status is not CheckStatus.fail, check.detail
    assert "unreachable" in check.detail.lower()


def test_the_down_line_tells_the_operator_what_it_means(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A line that reports a fact without its consequence is a line to guess at."""
    _configured()
    monkeypatch.setattr(ops, "probe_proxy", _answers(False))

    check = _proxy_check(ops.checks(live=True))

    assert "enable" in check.detail.lower() or "enable" in (check.fix or "").lower(), check


# --- the line this must not cross ---


def test_live_still_never_dials_an_unconfigured_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nobody asked about that address. --live does not change who asked."""
    _cold()

    def _forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("--live dialled a default proxy nobody configured")

    monkeypatch.setattr(socket, "socket", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)

    proxy = _proxy_check(ops.checks(live=True))

    # Scoped to the PROXY line on purpose: --live also expands gateway and
    # config checks, which warn on an unconfigured machine for their own
    # reasons and are not this task's to change.
    assert proxy.status is CheckStatus.ok
    assert "not configured" in proxy.detail.lower()


# --- nothing else moves ---


def test_plain_doctor_is_unchanged_for_a_configured_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configured()

    def _forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("plain doctor probed a proxy while tracing is off")

    monkeypatch.setattr(socket, "socket", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)

    check = _proxy_check(ops.checks())

    assert check.detail == f"not consulted while tracing is off ({CONFIGURED_PROXY})"


def test_plain_status_is_unchanged_for_a_configured_proxy(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """status has no --live; it must not have quietly gained the behaviour."""
    _configured()

    def _forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("status probed a proxy while tracing is off")

    monkeypatch.setattr(socket, "socket", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)

    result = runner.invoke(app, ["explainability", "status"], catch_exceptions=False)

    assert result.exit_code == 0
    assert "not consulted while tracing is off" in result.output


def test_tracing_on_is_untouched_by_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """The red case is the one that must not move. It probes with or without --live."""
    _configured(enabled=True)
    monkeypatch.setattr(ops, "probe_proxy", _answers(False))

    plain = _proxy_check(ops.checks())
    live = _proxy_check(ops.checks(live=True))

    assert plain.status is CheckStatus.fail
    assert live.status is CheckStatus.fail
    assert plain.detail == live.detail
    assert "UNTRACED" in plain.detail


def test_live_does_not_change_the_exit_code(runner: CliRunner) -> None:
    """--live reports more; it does not make an unconfigured machine fail."""
    _cold()

    result = runner.invoke(app, ["doctor", "--live"])

    assert result.exit_code == 0, result.output
