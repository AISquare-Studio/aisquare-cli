"""``disable`` flips config; the operator's shell is not ours to flip.

§5 has the operator export ``ANTHROPIC_BASE_URL``. §7 then says rollback
"returns every session to untraced, changes nothing else". In that shell it
does neither: config goes off while the exported variable keeps routing model
traffic through the proxy — and §7's next step stops the proxy, leaving a shell
where every launch points at a dead port. Reproduced with a dead port: the
child still received the URL and a request to it exited 7.

The launcher is not the bug and must not change. ``disown_inherited_trace``
strips only OUR marked identity, deliberately: an ``ANTHROPIC_*`` with no
marker beside it is a gateway the operator set up and is theirs to keep. And
the whole tracing block is skipped when config is off, so the default launch
stays byte-identical — a property with a comment defending it. There is no
correct place in ``launch`` to say this.

So ``disable`` says it, and only says it. A child process cannot unset a
variable in its parent's shell; anything other than telling would be theatre.

The condition is narrow on purpose. It fires only when the ambient value
EQUALS the proxy this machine is configured to use AND that proxy was chosen
rather than defaulted. Without the second half it would fire on the shipped
default ``127.0.0.1:9090`` — the port this project documents as belonging to
someone else's long-running proxy — and tell an operator to unset a variable
pointing at their own service.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core.config import AppConfig, ExplainabilityTarget, save_config

_PROXY = "http://127.0.0.1:9190"


def _configured(proxy: str | None = _PROXY) -> None:
    config = AppConfig()
    config.explainability.enabled = True
    config.explainability.target = "stg"
    config.explainability.targets["stg"] = ExplainabilityTarget(gateway_url="https://gw.invalid")
    if proxy is not None:
        config.explainability.proxy_url = proxy
    save_config(config)


def test_disable_names_the_variables_when_the_shell_still_routes(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The line that closes the gap between what §7 says and what it does."""
    _configured()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", _PROXY)

    result = runner.invoke(app, ["explainability", "disable"], catch_exceptions=False)

    assert result.exit_code == 0
    assert "ANTHROPIC_BASE_URL" in result.output
    assert "ANTHROPIC_CUSTOM_HEADERS" in result.output, (
        "the header pair routes identity and is exported by the same step; "
        "naming only one leaves the shell half-configured"
    )
    assert "unset" in result.output


def test_disable_says_nothing_when_the_shell_is_clean(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The common case must stay one line. A note nobody needs is noise."""
    _configured()
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)

    result = runner.invoke(app, ["explainability", "disable"], catch_exceptions=False)

    assert "unset" not in result.output, result.output


def test_disable_does_not_advise_unsetting_a_gateway_the_operator_owns(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ambient URL that is not the configured proxy is not ours to comment on.

    Same rule the launcher applies when it stands down from routing it owns.
    """
    _configured()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.example.internal")

    result = runner.invoke(app, ["explainability", "disable"], catch_exceptions=False)

    assert "unset" not in result.output, result.output


def test_the_shipped_default_proxy_does_not_trigger_the_advice(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The false positive this condition exists to avoid.

    ``127.0.0.1:9090`` is the shipped default AND the address this project
    documents as a long-running proxy that is not ours. An operator with
    something of their own there, who never chose a proxy, must not be told to
    unset it.
    """
    _configured(proxy=None)
    default = AppConfig().explainability.proxy_url
    monkeypatch.setenv("ANTHROPIC_BASE_URL", default)

    result = runner.invoke(app, ["explainability", "disable"], catch_exceptions=False)

    assert "unset" not in result.output, result.output
