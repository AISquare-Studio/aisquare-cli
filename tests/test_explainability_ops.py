"""The operator surface: targets, the doctor section, register, and --fix.

The properties pinned here are the ones a human depends on at 08:00 with a
cutover to run, not implementation detail:

* an unwired machine reads as guidance and never as an error, and every line
  that is not ok carries the exact command that fixes it;
* a key is read from the environment variable the config names and is never
  printed, stored, or logged by any command;
* the authenticated calls send ``X-API-KEY`` and no ``Authorization`` header —
  the gateway's fronting layer rejects the whole call when both arrive;
* nothing installs, ships, or writes without the operator asking for it.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, ClassVar

import pytest
from typer.testing import CliRunner

from aisquare.core.config import (
    AppConfig,
    ExplainabilitySettings,
    ExplainabilityTarget,
    load_config,
    save_config,
)
from aisquare.models import CheckStatus
from aisquare.services import explainability_ops as ops
from aisquare.services.explainability import ProxyProbe

SECRET = "wk_live_do_not_print_me"


# ── a gateway that records what reached it ───────────────────────────────────


class _GatewayHandler(BaseHTTPRequestHandler):
    """Fake gateway: canned statuses per path, plus a log of what arrived."""

    routes: ClassVar[dict[str, tuple[int, dict[str, Any]]]] = {}
    seen: ClassVar[list[dict[str, Any]]] = []

    def _respond(self, body: Any) -> None:
        status, payload = self.routes.get(self.path, (404, {"detail": "no route"}))
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
        self.seen.append(
            {
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body,
            }
        )

    def do_GET(self) -> None:
        self._respond(None)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        try:
            body = json.loads(raw)
        except ValueError:
            body = raw
        self._respond(body)

    def log_message(self, *args: object) -> None:  # keep pytest output clean
        return


def _gateway(routes: dict[str, tuple[int, dict[str, Any]]]) -> tuple[HTTPServer, str, list[Any]]:
    seen: list[dict[str, Any]] = []
    handler = type("Handler", (_GatewayHandler,), {"routes": routes, "seen": seen})
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}", seen


_ACCEPTED = (202, {"status": "accepted", "trace_id": "t", "span_count": 1})
_READY = (200, {"status": "ready"})
#: The same fake can answer as the local claude_code proxy, which keeps the
#: green-path tests hermetic — the real default port carries a creator-mode
#: proxy on some machines and nothing at all on others.
_PROXY_HEALTHY = (200, {"status": "ok", "service": "aisquare-proxy", "mode": "claude_code"})
_LIVE_ROUTES: dict[str, tuple[int, dict[str, Any]]] = {
    "/ready": _READY,
    "/v1/traces/ingest": _ACCEPTED,
    "/health": _PROXY_HEALTHY,
}

#: Port 9 (discard) refuses instantly, so "no proxy" costs a test no wall clock
#: and never depends on what happens to be listening on this box.
_NO_PROXY = "http://127.0.0.1:9"


def _wired(gateway_url: str, **overrides: Any) -> ExplainabilitySettings:
    """Config for a machine pointed at ``gateway_url`` with tracing on."""
    overrides.setdefault("proxy_url", _NO_PROXY)
    return ExplainabilitySettings(
        enabled=True,
        targets={"stg": ExplainabilityTarget(gateway_url=gateway_url, **overrides)},
    )


def _env(**extra: str) -> dict[str, str]:
    return {"EXPLAINABILITY_API_KEY": SECRET, **extra}


# ── target resolution ────────────────────────────────────────────────────────


def test_a_stock_machine_resolves_to_an_unconfigured_target() -> None:
    target = ops.resolve_target(ExplainabilitySettings(), env={})
    assert target.name == "stg"
    assert target.gateway_url == ""
    assert target.gateway_source == "unset"
    assert target.api_key is None
    assert target.configured is False


def test_a_target_folds_its_overrides_onto_the_defaults() -> None:
    settings = ExplainabilitySettings(
        target="prod",
        targets={
            "prod": ExplainabilityTarget(
                gateway_url="https://prod.example/",
                api_key_env="PROD_KEY",
                proxy_url="http://127.0.0.1:9191",
                agent_name_template="acme-{role}",
                roles=["planner"],
            )
        },
    )
    target = ops.resolve_target(settings, env={"PROD_KEY": SECRET})
    assert target.gateway_url == "https://prod.example"  # trailing slash dropped
    assert target.proxy_url == "http://127.0.0.1:9191"
    # "acme-cli" is FALLBACK_ROLE through this target's own template, not a
    # hardcoded name: the ship path emits it whenever the board cannot say
    # whose a Run is, so the roster has to carry it or those spans 409.
    assert target.agent_names == ("acme-planner", "acme-cli")
    assert target.api_key == SECRET


def test_two_targets_live_side_by_side_without_a_code_change() -> None:
    """The whole point of the table: stg today, prod at 08:00, same binary."""
    settings = ExplainabilitySettings(
        targets={
            "stg": ExplainabilityTarget(gateway_url="https://stg.example"),
            "prod": ExplainabilityTarget(
                gateway_url="https://prod.example", api_key_env="PROD_KEY"
            ),
        }
    )
    assert ops.resolve_target(settings, env={}).gateway_url == "https://stg.example"
    assert ops.resolve_target(settings, "prod", env={}).gateway_url == "https://prod.example"
    assert ops.resolve_target(settings, "prod", env={}).api_key_env == "PROD_KEY"


def test_the_target_env_var_switches_deployment_for_one_command() -> None:
    settings = ExplainabilitySettings(
        targets={"prod": ExplainabilityTarget(gateway_url="https://prod.example")}
    )
    target = ops.resolve_target(settings, env={ops.TARGET_ENV_VAR: "prod"})
    assert target.name == "prod"
    assert target.gateway_url == "https://prod.example"


def test_configured_gateway_beats_the_ambient_environment() -> None:
    """A shell that sourced staging must not redirect a prod-configured box."""
    settings = ExplainabilitySettings(
        targets={"stg": ExplainabilityTarget(gateway_url="https://from-config.example")}
    )
    target = ops.resolve_target(settings, env={ops.GATEWAY_ENV_VAR: "https://from-shell.example"})
    assert target.gateway_url == "https://from-config.example"
    assert target.gateway_source == "config"


def test_the_environment_fills_the_gap_when_config_is_silent() -> None:
    target = ops.resolve_target(
        ExplainabilitySettings(), env={ops.GATEWAY_ENV_VAR: "https://from-shell.example"}
    )
    assert target.gateway_url == "https://from-shell.example"
    assert target.gateway_source == "env"


def test_an_unrenderable_identity_template_yields_no_names_not_an_exception() -> None:
    settings = ExplainabilitySettings(agent_name_template="aisquare-{rol}")
    assert ops.resolve_target(settings, env={}).agent_names == ()


# ── the doctor section ───────────────────────────────────────────────────────


def _statuses(checks: list[Any]) -> set[CheckStatus]:
    return {check.status for check in checks}


def test_an_untouched_machine_gets_one_guidance_line_and_no_errors() -> None:
    """Nobody who never asked for tracing should read a wall of yellow."""
    checks = ops.checks(ExplainabilitySettings(), env={})
    assert len(checks) == 1
    assert checks[0].status is CheckStatus.ok
    assert "aisquare explainability enable" in checks[0].detail


def test_a_half_wired_machine_warns_and_still_never_fails() -> None:
    settings = ExplainabilitySettings(
        targets={"stg": ExplainabilityTarget(gateway_url="https://stg.example")}
    )
    checks = ops.checks(settings, env={})
    assert CheckStatus.fail not in _statuses(checks)
    switch = checks[0]
    assert switch.status is CheckStatus.warn
    assert switch.fix is not None
    assert "aisquare explainability enable" in switch.fix


def test_switching_tracing_on_promotes_a_missing_gateway_to_a_failure() -> None:
    """Once an operator opts in, "no gateway" is the difference between traced
    and silently untraced — a warning would be a lie."""
    checks = ops.checks(ExplainabilitySettings(enabled=True), env={})
    config = next(c for c in checks if c.name == "explainability config")
    assert config.status is CheckStatus.fail
    assert config.fix is not None
    assert "--gateway-url" in config.fix


def test_a_missing_key_names_the_variable_it_wants_and_not_a_file() -> None:
    settings = _wired("https://stg.example", api_key_env="ACME_KEY")
    checks = ops.checks(settings, env={})
    config = next(c for c in checks if c.name == "explainability config")
    assert "$ACME_KEY" in config.detail
    assert config.fix is not None
    assert "ACME_KEY" in config.fix


@pytest.mark.parametrize(
    "settings",
    [
        ExplainabilitySettings(),
        ExplainabilitySettings(targets={"stg": ExplainabilityTarget(gateway_url="https://x.test")}),
        ExplainabilitySettings(enabled=True),
        _wired("https://x.test"),
        ExplainabilitySettings(enabled=True, agent_name_template="broken-{rol}"),
    ],
)
def test_every_line_that_is_not_ok_carries_its_remediation(
    settings: ExplainabilitySettings,
) -> None:
    """A red check with no next action is half a doctor — the issue's words."""
    for check in ops.checks(settings, env={}):
        if check.status is not CheckStatus.ok:
            assert check.fix, f"{check.name} has no fix"


def test_a_dead_proxy_fails_loudly_once_tracing_is_on() -> None:
    settings = _wired("https://stg.example", proxy_url="http://127.0.0.1:9")
    checks = ops.checks(settings, env=_env())
    proxy = next(c for c in checks if c.name == "explainability proxy")
    assert proxy.status is CheckStatus.fail
    assert "UNTRACED" in proxy.detail


def test_the_proxy_is_not_probed_at_all_while_tracing_is_off() -> None:
    settings = ExplainabilitySettings(
        targets={"stg": ExplainabilityTarget(gateway_url="https://stg.example")},
        proxy_url="http://127.0.0.1:9",
    )
    proxy = next(c for c in ops.checks(settings, env={}) if c.name == "explainability proxy")
    assert proxy.status is CheckStatus.ok


def _probes(gateway: str | None) -> Callable[[str], ProxyProbe]:
    """A prober for a proxy that is ALIVE and reports ``gateway`` (or reports none)."""
    return lambda _url: ProxyProbe(True, "proxy healthy", gateway=gateway)


@pytest.mark.parametrize(
    ("reported", "configured", "same"),
    [
        ("https://g.example", "https://g.example", True),
        ("https://g.example/", "https://g.example", True),
        ("https://g.example:443", "https://g.example", True),
        ("http://127.0.0.1:8000/", "http://127.0.0.1:8000", True),
        ("http://g.example", "https://g.example", False),
        ("https://g.example:8443", "https://g.example", False),
        ("https://other.example", "https://g.example", False),
        ("http://127.0.0.1:8000", "http://127.0.0.1:9000", False),
        ("https://g.example:99999", "https://g.example", False),
        ("", "https://g.example", False),
    ],
    ids=[
        "identical",
        "trailing-slash",
        "explicit-default-port",
        "loopback-slash",
        "scheme-differs",
        "port-differs",
        "host-differs",
        "loopback-ports-differ",
        "unparseable-port",
        "empty",
    ],
)
def test_same_deployment_compares_scheme_host_and_port(
    reported: str, configured: str, same: bool
) -> None:
    """Directly, because the review found it reachable only through ``checks``.

    The equivalences are the ones a proxy actually produces by normalising its
    own URL, and the differences are each a real misroute. ``urlsplit`` raises
    on a malformed authority — an out-of-range port is the reachable case — and
    a proxy reporting nonsense must not take ``doctor`` down, nor be shown to
    agree with a target it cannot be compared to.
    """
    assert ops._same_deployment(reported, configured) is same


def test_a_live_proxy_shipping_to_another_deployment_is_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The measured bug (#131): three green checks while the Runs land elsewhere.

    ``gateway`` and ``ingest`` prove the CLI's path; the proxy carries the model
    traffic down a different one. Answering ``/health`` never meant it agreed
    about the destination, and nothing compared them.
    """
    monkeypatch.setattr(ops, "probe_proxy", _probes("http://127.0.0.1:8000"))
    settings = _wired("https://stg.example", proxy_url="http://127.0.0.1:9090")
    proxy = next(c for c in ops.checks(settings, env=_env()) if c.name == "explainability proxy")

    assert proxy.status is CheckStatus.fail
    assert "http://127.0.0.1:8000" in proxy.detail, "says where it actually ships"
    assert "https://stg.example" in proxy.detail, "and where it was supposed to"
    assert proxy.fix


def test_a_live_proxy_shipping_to_the_target_is_green_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ops, "probe_proxy", _probes("https://stg.example"))
    settings = _wired("https://stg.example", proxy_url="https://stg.example:9443")
    proxy = next(c for c in ops.checks(settings, env=_env()) if c.name == "explainability proxy")

    assert proxy.status is CheckStatus.ok
    assert "stg" in proxy.detail


@pytest.mark.parametrize(
    "reported",
    ["https://stg.example/", "https://stg.example:443"],
    ids=["trailing-slash", "explicit-default-port"],
)
def test_one_deployment_written_two_ways_is_not_a_mismatch(
    reported: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Compared on scheme/host/port, never as strings — else every proxy that
    normalises its own URL differently reads as misrouted."""
    monkeypatch.setattr(ops, "probe_proxy", _probes(reported))
    settings = _wired("https://stg.example", proxy_url="https://stg.example:9443")
    proxy = next(c for c in ops.checks(settings, env=_env()) if c.name == "explainability proxy")

    assert proxy.status is CheckStatus.ok


def test_a_local_proxy_that_names_no_gateway_is_flagged_as_unverifiable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The combination that stranded traffic, on a proxy too old to answer for
    itself: a sidecar takes its destination from whoever started it."""
    monkeypatch.setattr(ops, "probe_proxy", _probes(None))
    settings = _wired("https://stg.example", proxy_url="http://127.0.0.1:9090")
    proxy = next(c for c in ops.checks(settings, env=_env()) if c.name == "explainability proxy")

    assert proxy.status is CheckStatus.warn, "neither green nor red — it is unknown"
    assert "cannot be checked" in proxy.detail
    assert proxy.fix


@pytest.mark.parametrize(
    ("proxy_url", "gateway_url"),
    [
        ("https://stg.example:9443", "https://stg.example"),
        ("http://127.0.0.1:9090", "http://127.0.0.1:8000"),
    ],
    ids=["hosted-proxy", "wholly-local"],
)
def test_the_topologies_that_cannot_disagree_stay_silent(
    proxy_url: str, gateway_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The negative half. A hosted proxy is addressed AT the deployment, and a
    loopback pair is the self-hosted topology working — neither earns a warning
    for a field it did not send."""
    monkeypatch.setattr(ops, "probe_proxy", _probes(None))
    settings = _wired(gateway_url, proxy_url=proxy_url)
    proxy = next(c for c in ops.checks(settings, env=_env()) if c.name == "explainability proxy")

    assert proxy.status is CheckStatus.ok
    assert "cannot be checked" not in proxy.detail


def test_doctor_survives_a_malformed_gateway_url() -> None:
    """The review's blocker #1: a regression this branch introduced.

    ``proxy_state`` calls ``is_loopback(target.gateway_url)``, and an operator
    can type ``http://[::1`` into config. The ``ValueError`` propagated
    ``proxy_state`` -> ``_check_proxy`` -> ``checks`` -> ``run_checks`` and
    tracebacked out of ``aisquare doctor``, where only the config LOAD was
    wrapped. ``main`` returns its checks normally for the same input.
    """
    settings = _wired("http://[::1", proxy_url=_NO_PROXY)
    checks = ops.checks(settings, env=_env())  # must not raise
    assert any(c.name == "explainability proxy" for c in checks)


@pytest.mark.parametrize(
    ("url", "said"),
    [
        ("http://[::1", "cannot be parsed"),
        ("  http://[::1  ", "cannot be parsed"),
        ("gateway.example", "needs an http:// or https:// scheme"),
    ],
    ids=["ipv6-bracket", "padded", "schemeless"],
)
def test_a_request_to_a_malformed_url_is_a_verdict_naming_the_fault(url: str, said: str) -> None:
    """Review of #203: ``_request`` kept a private ``try`` around ``urlsplit``
    beside its import of ``split_url``, the one guarded parser. Through the
    helper now, and stripped first so the parse and the request read the same
    text — a padded value passed the old check and then failed ``Request`` as
    "unreachable", which sends the operator to the network, not the config."""
    verdict = ops._request(url)
    assert verdict.ok is False and verdict.status is None
    assert said in verdict.detail, verdict.detail


def test_an_unset_gateway_is_not_reported_as_a_misroute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review #5. ``resolve_target`` legitimately yields ``gateway_url == ''``.

    An empty string equals no deployment, so the strict comparison called every
    such machine misrouted -- printing a sentence with a blank where a URL goes,
    and making ``explainability status`` exit 1. Nothing is misrouted; the CLI
    simply has no second value to compare against.
    """
    monkeypatch.setattr(ops, "probe_proxy", _probes("https://g.example"))
    settings = ExplainabilitySettings(
        enabled=True,
        targets={"stg": ExplainabilityTarget(gateway_url="", proxy_url="https://p.example:9443")},
    )
    proxy = next(c for c in ops.checks(settings, env=_env()) if c.name == "explainability proxy")

    assert proxy.status is CheckStatus.warn, "unknown, not wrong"
    assert "no gateway is configured" in proxy.detail
    assert " is  " not in proxy.detail, "never a sentence with a blank where a URL goes"
    assert proxy.fix


def test_a_hosted_proxy_on_another_host_is_no_longer_waved_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review #6: the failure class this PR exists to close, still open in it.

    A hosted proxy that does not report its gateway fell past the amber branch
    (which required a LOOPBACK proxy) to the bare green return. "A hosted proxy
    is addressed at the deployment, so it cannot disagree" was an assumption
    about the operator's typing -- and ``hosted_proxy_for`` is this module's own
    statement that the two share a host, so the comparison was available.
    """
    monkeypatch.setattr(ops, "probe_proxy", _probes(None))
    settings = _wired("https://g.example", proxy_url="https://WRONG.example:9443")
    proxy = next(c for c in ops.checks(settings, env=_env()) if c.name == "explainability proxy")

    assert proxy.status is CheckStatus.warn
    assert "cannot be checked" in proxy.detail
    assert proxy.fix


def test_a_hosted_proxy_on_the_gateways_host_stays_green_and_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The negative half of #6: the real hosted topology must not go amber."""
    monkeypatch.setattr(ops, "probe_proxy", _probes(None))
    settings = _wired("https://g.example", proxy_url="https://g.example:9443")
    proxy = next(c for c in ops.checks(settings, env=_env()) if c.name == "explainability proxy")

    assert proxy.status is CheckStatus.ok
    assert "cannot be checked" not in proxy.detail


def test_a_hosted_proxy_naming_its_gateway_from_its_own_host_is_not_a_misroute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#132 follow-up 1. The deployment's own proxy sits beside its gateway and
    talks to it over loopback (the SDK's ``.env`` default is
    ``http://127.0.0.1:8000``), so the day it reports that field the strict
    comparison would have turned the real hosted topology RED and made
    ``status`` exit 1 for a proxy shipping to exactly the right place."""
    monkeypatch.setattr(ops, "probe_proxy", _probes("http://127.0.0.1:8000"))
    settings = _wired("https://g.example", proxy_url="https://g.example:9443")
    proxy = next(c for c in ops.checks(settings, env=_env()) if c.name == "explainability proxy")

    assert proxy.status is CheckStatus.ok
    assert "beside it" in proxy.detail and "127.0.0.1:8000" in proxy.detail


def test_a_remote_proxy_naming_a_loopback_gateway_is_unverifiable_not_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of follow-up 1: a proxy on ANOTHER host whose gateway is
    local to it cannot be compared with the target from here — amber with the
    two levers, not the red that says "model traffic lands on the other
    deployment", which nobody here can know."""
    monkeypatch.setattr(ops, "probe_proxy", _probes("http://127.0.0.1:8000"))
    settings = _wired("https://g.example", proxy_url="https://p.example:9443")
    proxy = next(c for c in ops.checks(settings, env=_env()) if c.name == "explainability proxy")

    assert proxy.status is CheckStatus.warn
    assert "cannot be checked from here" in proxy.detail
    assert proxy.fix


def test_the_unset_gateway_amber_never_offers_a_proxys_local_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#132 follow-up 4. With no gateway configured, the amber offered
    ``--gateway-url <what the proxy reported>`` verbatim — for a remote proxy
    that is a URL from ITS network, and an operator who pasted it had a
    loopback gateway on a machine with nothing on 8000."""
    monkeypatch.setattr(ops, "probe_proxy", _probes("http://127.0.0.1:8000"))
    settings = ExplainabilitySettings(
        enabled=True,
        targets={"stg": ExplainabilityTarget(gateway_url="", proxy_url="https://p.example:9443")},
    )
    proxy = next(c for c in ops.checks(settings, env=_env()) if c.name == "explainability proxy")

    assert proxy.status is CheckStatus.warn
    assert "its own local view" in proxy.detail
    assert "--gateway-url <url>" in (proxy.fix or ""), "a placeholder, never the local address"
    assert "--gateway-url http://127.0.0.1:8000" not in (proxy.fix or "")


def test_the_loopback_pair_says_what_it_assumes(monkeypatch: pytest.MonkeyPatch) -> None:
    """#132 follow-up 3. Loopback proxy, loopback gateway: green — the operator
    runs both — but a local proxy ships wherever ``EXPLAINABILITY_GATEWAY_URL``
    pointed when it was started, the same mechanism the remote-gateway amber
    names, so the summary states the assumption instead of "by construction"."""
    monkeypatch.setattr(ops, "probe_proxy", _probes(None))
    settings = _wired("http://127.0.0.1:8000", proxy_url="http://127.0.0.1:9090")
    proxy = next(c for c in ops.checks(settings, env=_env()) if c.name == "explainability proxy")

    assert proxy.status is CheckStatus.ok
    assert "EXPLAINABILITY_GATEWAY_URL" in proxy.detail
    assert "taken to be http://127.0.0.1:8000" in proxy.detail


def test_a_schemeless_reported_gateway_is_still_a_misroute(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review of the fold. The local-view rule was written with ``is_loopback``,
    whose empty-host-is-local reading made ANY schemeless or malformed report
    (``other.example:8000``, ``unknown``) pass for the proxy's own loopback, and
    a red misroute went green on the target's host. The rule is a well-formed
    loopback URL, nothing looser."""
    monkeypatch.setattr(ops, "probe_proxy", _probes("other.example:8000"))
    settings = _wired("https://g.example", proxy_url="https://g.example:9443")
    proxy = next(c for c in ops.checks(settings, env=_env()) if c.name == "explainability proxy")

    assert proxy.status is CheckStatus.fail
    assert "other.example:8000" in proxy.detail


def test_a_local_proxys_loopback_report_is_this_machines_and_is_offered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of the fold. The unset-gateway amber withheld a loopback report
    from a LOCAL proxy too — but a local proxy's ``127.0.0.1`` is this box's,
    and that is the self-hosted operator's own gateway, exactly the address
    the amber used to offer correctly."""
    monkeypatch.setattr(ops, "probe_proxy", _probes("http://127.0.0.1:8000"))
    settings = ExplainabilitySettings(
        enabled=True,
        targets={"stg": ExplainabilityTarget(gateway_url="", proxy_url="http://127.0.0.1:9090")},
    )
    proxy = next(c for c in ops.checks(settings, env=_env()) if c.name == "explainability proxy")

    assert proxy.status is CheckStatus.warn
    assert "its own local view" not in proxy.detail
    assert "--gateway-url http://127.0.0.1:8000" in (proxy.fix or "")


def test_a_remote_proxys_loopback_report_never_matches_a_loopback_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of the fold. With a LOCAL target gateway and a REMOTE proxy that
    reports ``http://127.0.0.1:8000``, ``_same_deployment`` compared the two
    loopbacks as one deployment and said green — but the proxy's 127.0.0.1 is
    its host's, not this box's. The local-view rule is judged first."""
    monkeypatch.setattr(ops, "probe_proxy", _probes("http://127.0.0.1:8000"))
    settings = _wired("http://127.0.0.1:8000", proxy_url="https://p.example:9443")
    proxy = next(c for c in ops.checks(settings, env=_env()) if c.name == "explainability proxy")

    assert proxy.status is CheckStatus.warn
    assert "cannot be checked from here" in proxy.detail


def test_the_unset_gateway_amber_offers_only_a_url_the_writer_would_take(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of the fold. With no gateway configured the amber pasted whatever
    the proxy reported into ``--gateway-url``, so a schemeless or garbage report
    (``other.example:8000``, ``unknown``) became a command ``configure_target``
    refuses with "needs a scheme". Offered only when it would be taken."""
    monkeypatch.setattr(ops, "probe_proxy", _probes("other.example:8000"))
    settings = ExplainabilitySettings(
        enabled=True,
        targets={"stg": ExplainabilityTarget(gateway_url="", proxy_url="https://p.example:9443")},
    )
    proxy = next(c for c in ops.checks(settings, env=_env()) if c.name == "explainability proxy")

    assert proxy.status is CheckStatus.warn
    assert "--gateway-url <url>" in (proxy.fix or "")
    assert "other.example:8000" not in (proxy.fix or ""), "never a command that will be refused"


def test_a_loopback_report_with_an_unusable_port_is_not_a_local_view() -> None:
    """Review of the fold. ``_loopback_url`` parsed on its own and never read the
    port, so ``http://127.0.0.1:99999`` passed as the proxy's loopback view
    while ``url_problem`` called it unusable — the two-parsers drift #132 had
    just removed from ``_usable_base_url``. One judge."""
    assert ops._loopback_url("http://127.0.0.1:8000") is True
    assert ops._loopback_url("http://127.0.0.1:99999") is False
    assert ops._loopback_url("other.example:8000") is False
    assert ops._loopback_url("https://g.example") is False


def test_the_verdict_cannot_contradict_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review #7. Three independent booleans could express ``problem`` AND
    ``caution`` together, and only ``_check_proxy`` read the third -- so an
    amber rendered green on ``status`` and in the fleet tab. One severity
    cannot be half-read."""
    monkeypatch.setattr(ops, "probe_proxy", _probes("http://elsewhere.example"))
    target = ops.resolve_target(_wired("https://g.example", proxy_url="https://p.example:9443"))
    state = ops.proxy_state(target, on=True)

    assert state.severity is CheckStatus.fail
    assert state.problem is True, "the old name still answers, for the surfaces that read it"
    assert isinstance(state.severity, CheckStatus)


@pytest.mark.parametrize(
    ("reported", "severity"),
    [("http://elsewhere.example", CheckStatus.fail), (None, CheckStatus.warn)],
    ids=["misroute", "unverifiable"],
)
def test_a_non_green_verdict_carries_its_next_command(
    reported: str | None, severity: CheckStatus, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review #8, against this module's own rule: a line that is not ok without
    its next command is half a doctor."""
    monkeypatch.setattr(ops, "probe_proxy", _probes(reported))
    settings = _wired("https://g.example", proxy_url="http://127.0.0.1:9090")
    proxy = next(c for c in ops.checks(settings, env=_env()) if c.name == "explainability proxy")

    assert proxy.status is severity
    assert proxy.fix, "every non-ok verdict names what to do"


def test_a_broken_config_degrades_to_one_warning_rather_than_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom() -> None:
        raise ValueError("config.toml is not valid TOML")

    monkeypatch.setattr(ops, "load_config", _boom)
    checks = ops.checks(env={})
    assert len(checks) == 1
    assert checks[0].status is CheckStatus.warn
    assert checks[0].fix


# ── --live: the round-trip that proves the whole path ────────────────────────


def test_live_green_path_ships_a_real_span_and_reads_the_202() -> None:
    """Acceptance: on a wired machine, green includes a real ingest round-trip
    rather than a ping."""
    server, url, _ = _gateway(_LIVE_ROUTES)
    try:
        checks = ops.checks(_wired(url, proxy_url=url), live=True, env=_env())
    finally:
        server.shutdown()

    ingest = next(c for c in checks if c.name == "explainability ingest")
    assert ingest.status is CheckStatus.ok
    assert "202" in ingest.detail
    assert CheckStatus.fail not in _statuses(checks)


def test_the_probe_span_is_shaped_the_way_routing_reads_it() -> None:
    """Routing resolves the studio from ``agent.name`` on the ROOT span; a probe
    that skipped it would exercise a path no real trace takes."""
    server, url, seen = _gateway({"/ready": _READY, "/v1/traces/ingest": _ACCEPTED})
    try:
        ops.checks(_wired(url), live=True, env=_env())
    finally:
        server.shutdown()

    posted = next(item for item in seen if item["path"] == "/v1/traces/ingest")
    span = posted["body"]["spans"][0]
    assert span["parent_span_id"] is None
    assert span["attributes"]["agent.name"] == "aisquare-planner"
    assert span["name"] == "AgentRun:aisquare-planner"
    assert posted["body"]["trace_id"] == span["trace_id"]


def test_authenticated_calls_send_the_key_header_and_never_an_authorization() -> None:
    """Verified against the gateway: a fronting layer tries to verify any
    Authorization header as a JWT and fails the WHOLE call, so the wrong-auth
    shape must be unreachable, not merely unused."""
    server, url, seen = _gateway({"/ready": _READY, "/v1/traces/ingest": _ACCEPTED})
    try:
        ops.checks(_wired(url), live=True, env=_env())
    finally:
        server.shutdown()

    posted = next(item for item in seen if item["path"] == "/v1/traces/ingest")
    assert posted["headers"]["x-api-key"] == SECRET
    assert "authorization" not in posted["headers"]


def test_an_unregistered_identity_points_at_the_register_command() -> None:
    routes = {
        "/ready": _READY,
        "/v1/traces/ingest": (409, {"detail": "no_agent_identity: nothing carries agent.name"}),
    }
    server, url, _ = _gateway(routes)
    try:
        checks = ops.checks(_wired(url), live=True, env=_env())
    finally:
        server.shutdown()

    ingest = next(c for c in checks if c.name == "explainability ingest")
    assert ingest.status is CheckStatus.fail
    assert ingest.fix is not None
    assert "aisquare explainability register" in ingest.fix


def test_a_rejected_key_says_so_instead_of_blaming_the_identity() -> None:
    routes = {"/ready": _READY, "/v1/traces/ingest": (403, {"detail": "invalid api key"})}
    server, url, _ = _gateway(routes)
    try:
        checks = ops.checks(_wired(url), live=True, env=_env())
    finally:
        server.shutdown()

    ingest = next(c for c in checks if c.name == "explainability ingest")
    assert ingest.status is CheckStatus.fail
    assert ingest.fix is not None
    assert "WORKSPACE key" in ingest.fix


def test_an_unreachable_gateway_never_reports_a_green_round_trip() -> None:
    server, url, _ = _gateway({})
    server.shutdown()
    server.server_close()  # the port is closed now: the "deployment is down" case
    checks = ops.checks(_wired(url), live=True, env=_env())
    gateway = next(c for c in checks if c.name == "explainability gateway")
    assert gateway.status is CheckStatus.fail
    # This used to assert the ingest row was ABSENT. The test's own name is
    # "never reports a GREEN round trip", and absence was a narrower claim than
    # that — it also made the row's silence indistinguishable from a pass it
    # never attempted, which is the defect tsk_01m0ak70 fixed. The row is now
    # reported as skipped; what must never happen is it reading ok.
    ingest = next(c for c in checks if c.name == "explainability ingest")
    assert ingest.status is not CheckStatus.ok, ingest.detail
    assert ingest.detail.startswith("skipped"), ingest.detail


def test_a_green_round_trip_still_flags_ungoverned_runs() -> None:
    """Traces landing is not the same as runs being governed, and attaching a
    rule book is the operator's next action — so it is a line, not silence."""
    server, url, _ = _gateway({"/ready": _READY, "/v1/traces/ingest": _ACCEPTED})
    try:
        checks = ops.checks(_wired(url), live=True, env=_env())
    finally:
        server.shutdown()

    governance = next(c for c in checks if c.name == "explainability governance")
    assert governance.status is CheckStatus.warn
    assert governance.fix is not None
    assert "rule book" in governance.fix


def test_live_on_an_unconfigured_machine_guides_instead_of_probing() -> None:
    checks = ops.checks(ExplainabilitySettings(), live=True, env={})
    gateway = next(c for c in checks if c.name == "explainability gateway")
    assert gateway.status is CheckStatus.warn
    assert gateway.fix is not None
    assert "aisquare explainability enable" in gateway.fix


# ── the SDK, consumed rather than reimplemented ──────────────────────────────


def test_the_sdk_doctors_table_is_read_back_into_rows() -> None:
    output = (
        "AISquare Explainability SDK Doctor\n"
        "========================================\n"
        "sdk_version      [\x1b[92m  OK   \x1b[0m] 1.0.6\n"
        "delivery_backlog [\x1b[91m ERROR \x1b[0m] dead_letter=2\n"
        "agno             [\x1b[93mMISSING\x1b[0m] Install optional dependency: .[agno]\n"
        "========================================\n"
    )
    assert ops._parse_sdk_table(output) == [
        ("sdk_version", "ok", "1.0.6"),
        ("delivery_backlog", "error", "dead_letter=2"),
        ("agno", "missing", "Install optional dependency: .[agno]"),
    ]


@pytest.mark.parametrize("degrade", [ops._fail, ops._warn])
def test_expected_sdk_noise_is_not_reported_as_a_finding(
    monkeypatch: pytest.MonkeyPatch, degrade: ops.Degrade
) -> None:
    """agno and the gateway's OPENAI_API_KEY are not this lane's business;
    printing them red trains an operator to ignore the section.

    Run under both degrades: WHICH rows are reported is a filter and must not
    depend on whether tracing is on, which only decides how loud a reported
    row is. Parametrised rather than pinned to one, so the two concerns stay
    provably separate."""
    rows = [
        ("delivery_backlog", "ok", "empty"),
        ("agno", "missing", "Install optional dependency: .[agno]"),
        ("openinference_agno", "missing", "Install optional dependency: .[agno]"),
        ("openai_api_key", "warning", "Set OPENAI_API_KEY"),
    ]
    monkeypatch.setattr(ops, "sdk_doctor", lambda **_: rows)
    names = [check.name for check in ops._sdk_checks(degrade=degrade)]
    assert names == ["sdk:delivery_backlog"]


def test_a_missing_sdk_is_advice_on_a_stock_box_and_a_warning_once_wired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SDK only matters to the lane that ships CLI insights, so it is not
    a finding until this machine is being wired for it."""
    from aisquare.services.explainability import install_hint as explainability_install_hint

    monkeypatch.setattr(
        ops,
        "sdk_presence",
        lambda: ops.SdkPresence(importable=False, script=None, version=None, shadowing=False),
    )
    stock = next(
        c
        for c in ops.checks(ExplainabilitySettings(), live=True, env={})
        if c.name == "explainability sdk"
    )
    assert stock.status is CheckStatus.ok
    # `install_hint()`, not the bare constant: both rows below are reachable from
    # an editable checkout, where installing the extra SHADOWS this package
    # instead of merging with it, and the raw hint would be the one command that
    # breaks the machine while claiming to fix it. That is the same ruling
    # `test_the_remedy_does_not_tell_an_editable_checkout_to_install_the_extra`
    # already pins for the script-only row; these two had been left on a stale
    # module-local constant that also named the SDK rather than the CLI.
    assert explainability_install_hint() in stock.detail

    wired = next(
        c
        for c in ops.checks(ExplainabilitySettings(enabled=True), env={})
        if c.name == "explainability sdk"
    )
    assert wired.status is CheckStatus.warn
    assert wired.fix is not None
    assert explainability_install_hint() in wired.fix


def test_a_checks_detail_is_data_not_markup(capsys: pytest.CaptureFixture[str]) -> None:
    """Observed against the real gateway: the SDK reports a configured key as
    "[present]", Rich read the brackets as a style tag, and the line rendered
    as an empty detail — indistinguishable from a missing key."""
    from aisquare.cli.common import emit_doctor
    from aisquare.models import DoctorCheck

    emit_doctor(
        [
            DoctorCheck(
                name="sdk:api_key",
                status=CheckStatus.warn,
                detail="[present]",
                fix="export [VAR]",
            )
        ]
    )

    output = capsys.readouterr().out
    assert "[present]" in output
    assert "[VAR]" in output


def test_a_shadowed_package_root_is_surfaced_with_its_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SDK ships a package named ``aisquare`` too, so installing it
    overwrites this CLI's package root. We survive it; the operator is still
    told, because the SDK's own facade does not."""
    monkeypatch.setattr(
        ops,
        "sdk_presence",
        lambda: ops.SdkPresence(importable=True, script=None, version="1.0.6", shadowing=True),
    )
    sdk = next(
        c
        for c in ops.checks(ExplainabilitySettings(enabled=True), env={})
        if c.name == "explainability sdk"
    )
    assert sdk.status is CheckStatus.warn
    assert sdk.fix is not None
    assert "force-reinstall" in sdk.fix


# ── --fix ────────────────────────────────────────────────────────────────────


def test_fix_turns_tracing_on_and_says_so() -> None:
    actions = ops.apply_fixes(confirm=lambda _: False)
    assert any("enabled" in action for action in actions)
    assert load_config().explainability.enabled is True


def test_fix_never_installs_without_being_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    """--fix reaches the network and mutates the environment the CLI runs in.
    Declining leaves the machine exactly as it was, holding the command."""
    monkeypatch.setattr(
        ops,
        "sdk_presence",
        lambda: ops.SdkPresence(importable=False, script=None, version=None, shadowing=False),
    )
    monkeypatch.setattr(
        ops,
        "install_sdk",
        lambda: pytest.fail("installed without consent"),
    )
    # This file runs FROM a checkout, so the editable refusal would answer first
    # and this test would pass without ever reaching the consent it is about.
    monkeypatch.setattr(ops, "running_editable", lambda: False)
    actions = ops.apply_fixes(confirm=lambda _: False)
    assert any(ops.INSTALL_HINT in action for action in actions)


def test_fix_installs_when_consent_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ops,
        "sdk_presence",
        lambda: ops.SdkPresence(importable=False, script=None, version=None, shadowing=False),
    )
    monkeypatch.setattr(ops, "install_sdk", lambda: (True, "installed aisquare[explainability]"))
    monkeypatch.setattr(ops, "running_editable", lambda: False)
    actions = ops.apply_fixes(assume_yes=True)
    assert any("installed" in action for action in actions)


def test_fix_can_select_the_target_it_enables() -> None:
    ops.apply_fixes(target="prod", confirm=lambda _: False)
    assert load_config().explainability.target == "prod"


# ── the CLI surface ──────────────────────────────────────────────────────────


def test_doctor_runs_everything_else_when_the_sdk_is_absent(runner: CliRunner) -> None:
    """Acceptance: without the extra installed, doctor is unaffected."""
    from aisquare.cli.app import app as cli_app

    runner.invoke(cli_app, ["init", "--no-onboard"])
    result = runner.invoke(cli_app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "✓ python" in result.output
    assert "✓ home" in result.output
    assert "explainability" in result.output


def test_the_group_is_listed_in_help(runner: CliRunner) -> None:
    """An operator surface nobody can find is not an operator surface."""
    from aisquare.cli.app import app as cli_app

    result = runner.invoke(cli_app, ["--help"])
    assert result.exit_code == 0
    assert "explainability" in result.output


def test_enable_is_the_one_command_that_turns_tracing_on(runner: CliRunner) -> None:
    from aisquare.cli.app import app as cli_app

    result = runner.invoke(
        cli_app,
        [
            "explainability",
            "enable",
            "--target",
            "prod",
            "--gateway-url",
            "https://prod.example/",
            "--key-env",
            "PROD_KEY",
        ],
    )

    assert result.exit_code == 0, result.output
    settings = load_config().explainability
    assert settings.enabled is True
    assert settings.target == "prod"
    assert settings.targets["prod"].gateway_url == "https://prod.example"
    assert settings.targets["prod"].api_key_env == "PROD_KEY"
    assert "aisquare doctor --live" in result.output


def test_disable_keeps_the_targets_it_was_given(runner: CliRunner) -> None:
    from aisquare.cli.app import app as cli_app

    runner.invoke(cli_app, ["explainability", "enable", "--gateway-url", "https://stg.example"])
    result = runner.invoke(cli_app, ["explainability", "disable"])

    assert result.exit_code == 0, result.output
    settings = load_config().explainability
    assert settings.enabled is False
    assert settings.targets["stg"].gateway_url == "https://stg.example"


def test_register_prints_each_identity_with_its_publication_id(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    roster = (
        200,
        {
            "agents": [
                {"name": "aisquare-planner", "publication_id": 169},
                {"name": "aisquare-coder", "publication_id": 170},
                {"name": "aisquare-runner", "publication_id": 171},
            ]
        },
    )
    server, url, seen = _gateway({"/v1/agents/register-roster": roster})
    monkeypatch.setenv("EXPLAINABILITY_API_KEY", SECRET)
    from aisquare.cli.app import app as cli_app

    try:
        save_config(AppConfig(explainability=_wired(url)))
        result = runner.invoke(cli_app, ["explainability", "register"])
    finally:
        server.shutdown()

    assert result.exit_code == 0, result.output
    assert "aisquare-planner: publication_id 169" in result.output
    assert "aisquare-runner: publication_id 171" in result.output
    posted = seen[0]
    assert posted["headers"]["x-api-key"] == SECRET
    assert "authorization" not in posted["headers"]
    # The fourth name is the point: `register` must declare every identity the
    # CLI can emit, and `_agent_name_for` falls back to FALLBACK_ROLE for any
    # Run the board cannot attribute. Registering is idempotent, so adding it
    # costs nothing and its absence costs the spans permanently (409
    # no_agent_identity is not retryable).
    # The fleet roles (manager, tester, reviewer, validator) joined the default
    # roster so every identity a fleet session can emit is registered too.
    assert posted["body"] == {
        "agents": [
            "aisquare-planner",
            "aisquare-coder",
            "aisquare-runner",
            "aisquare-manager",
            "aisquare-tester",
            "aisquare-reviewer",
            "aisquare-validator",
            "aisquare-ui-tester",
            "aisquare-cli",
        ]
    }


def test_register_refuses_before_it_reaches_the_network(runner: CliRunner) -> None:
    from aisquare.cli.app import app as cli_app

    save_config(AppConfig(explainability=_wired("https://unreachable.invalid")))
    result = runner.invoke(cli_app, ["explainability", "register"])

    assert result.exit_code == 1
    assert "EXPLAINABILITY_API_KEY" in result.output


def test_no_command_ever_prints_the_key(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """The single boundary that cannot be walked back: a key echoed into a
    terminal scrollback (or a CI log) has leaked."""
    monkeypatch.setenv("EXPLAINABILITY_API_KEY", SECRET)
    server, url, _ = _gateway({"/ready": _READY, "/v1/traces/ingest": _ACCEPTED})
    from aisquare.cli.app import app as cli_app

    try:
        save_config(AppConfig(explainability=_wired(url)))
        outputs = [
            runner.invoke(cli_app, ["explainability", "status"]).output,
            runner.invoke(cli_app, ["explainability", "enable"]).output,
            runner.invoke(cli_app, ["doctor", "--live"]).output,
            runner.invoke(cli_app, ["--json", "doctor", "--live"]).output,
        ]
    finally:
        server.shutdown()

    for output in outputs:
        assert SECRET not in output
    assert "EXPLAINABILITY_API_KEY" in outputs[0]  # the NAME is shown, not the value


def test_doctor_live_reports_the_round_trip_to_the_operator(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXPLAINABILITY_API_KEY", SECRET)
    server, url, _ = _gateway({"/ready": _READY, "/v1/traces/ingest": _ACCEPTED})
    from aisquare.cli.app import app as cli_app

    try:
        save_config(AppConfig(explainability=_wired(url, proxy_url="http://127.0.0.1:9")))
        result = runner.invoke(cli_app, ["doctor", "--live"])
    finally:
        server.shutdown()

    assert "test span accepted" in result.output
    # The dead proxy is a real failure once tracing is on, so doctor exits 1 —
    # and still printed the gateway verdict above it rather than bailing early.
    assert result.exit_code == 1


def test_register_renders_the_same_verdict_in_both_forms(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--json register`` answered in JSON on five failure branches and in
    prose on the one that succeeds.

    Every guard clause in ``register`` goes through the shared ``fail`` helper —
    unconfigured, no key, bad template, no agents, refused — so the command
    looked like a good citizen of the machine-readable contract from every
    angle an operator could test it before §1 actually worked. This is the same
    shape ``explainability env`` had, and it survived the sweep for the same
    reason: a success path behind a gateway is one no sweep can reach without
    standing one up.

    Asserted as AGREEMENT between the renderings, and the roster deliberately
    answers for only TWO of the three agents, so the null case — registered,
    no publication id — is exercised rather than assumed.
    """
    from aisquare.cli.app import app as cli_app

    roster = {
        "agents": [
            {"name": "aisquare-planner", "publication_id": 101},
            {"name": "aisquare-coder", "publication_id": 102},
        ]
    }
    server, url, _seen = _gateway({"/v1/agents/register-roster": (200, roster)})
    try:
        # Not credential-shaped: a literal that looked like a key was rejected
        # by push protection on this repo once, and the stub accepts anything.
        monkeypatch.setenv("AISQUARE_LOCAL_STUB_KEY", "local-stub")
        save_config(
            AppConfig(
                explainability=ExplainabilitySettings(
                    enabled=True,
                    targets={
                        "tst": ExplainabilityTarget(
                            gateway_url=url, api_key_env="AISQUARE_LOCAL_STUB_KEY"
                        )
                    },
                    target="tst",
                )
            )
        )
        human = runner.invoke(cli_app, ["explainability", "register"])
        machine = runner.invoke(cli_app, ["--json", "explainability", "register"])
    finally:
        server.shutdown()

    assert human.exit_code == 0, human.output
    assert machine.exit_code == 0, machine.output

    payload = json.loads(machine.stdout)
    assert payload["target"] == "tst"
    # aisquare-cli joins the null case the roster deliberately does not answer
    # for — which is the right side of this test to land on, since the fallback
    # identity is exactly the one an operator is most likely to have missed.
    assert payload["publications"] == {
        "aisquare-planner": "101",
        "aisquare-coder": "102",
        "aisquare-runner": None,
        "aisquare-manager": None,
        "aisquare-tester": None,
        "aisquare-reviewer": None,
        "aisquare-validator": None,
        "aisquare-ui-tester": None,
        "aisquare-cli": None,
    }, payload

    # The human rendering says the same two things, in its own words.
    assert "aisquare-planner: publication_id 101" in human.stdout, human.stdout
    assert "aisquare-runner: registered" in human.stdout, human.stdout


def test_enable_renders_the_same_facts_in_both_forms(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`enable`'s payload only PARSED until now, and parsing is not correctness.

    The sweep asserts this command's stdout is JSON; ``{}`` would satisfy it.
    Every field here is instead checked against the HUMAN rendering of the same
    run, so a dropped or renamed field fails while a field added later does not
    — the opposite of pinning a literal payload, which rots on the next
    addition.

    `key_set` is checked through the human view's own words rather than by
    re-deriving it: the ✓ block prints "(NOT set)" when the named variable is
    absent, and the two must not be able to disagree about the same shell.
    """
    from aisquare.cli.app import app as cli_app

    monkeypatch.delenv("AISQUARE_AGREEMENT_KEY", raising=False)
    argv = [
        "explainability",
        "enable",
        "--target",
        "tst",
        "--gateway-url",
        "https://gw.invalid",
        "--key-env",
        "AISQUARE_AGREEMENT_KEY",
    ]
    human = runner.invoke(cli_app, argv)
    machine = runner.invoke(cli_app, ["--json", *argv])

    assert human.exit_code == 0, human.output
    assert machine.exit_code == 0, machine.output

    payload = json.loads(machine.stdout)
    assert payload["enabled"] is True, payload

    # `identity` is MACHINE-ONLY, and writing this test is how I learned it: the
    # human block prints the EXPANDED agent names, never the template they came
    # from. That is the better human rendering and the field is still worth
    # publishing — `status` publishes it too — so it is checked against its
    # SOURCE rather than dropped from the rule or the rule weakened to fit.
    # And it publishes the RESOLVED value, not the raw per-target field: this
    # target overrides nothing, so its own `agent_name_template` is None while
    # the identity actually in use is the top-level default. Comparing against
    # the raw field failed here, which is the payload being right and my first
    # source being wrong — a caller wants what WILL be used, which is what
    # `status` publishes too.
    resolved = ops.resolve_target(load_config().explainability, "tst")
    assert payload["identity"] == resolved.agent_name_template

    for field in ("target", "gateway", "key_env", "proxy"):
        # NON-EMPTY FIRST, and that check is here because its absence made this
        # loop vacuous: `"" in anything` is True, so a field that degenerated to
        # the empty string satisfied the substring test everywhere. Found by
        # sabotage — replacing the gateway with "" left all 46 tests green.
        # Every one of these is non-empty by construction in this fixture.
        assert payload[field], f"{field} came back empty: {payload}"
        assert str(payload[field]) in human.stdout, (
            f"{field}={payload[field]!r} is in the JSON and nowhere in the human "
            f"rendering of the same run:\n{human.stdout}"
        )
    for agent_name in payload["agents"]:
        assert agent_name in human.stdout, f"{agent_name} missing from the human view"
    assert payload["key_set"] is ("(NOT set)" not in human.stdout), (
        f"key_set={payload['key_set']} disagrees with the human view about the "
        f"same shell:\n{human.stdout}"
    )


# ── the second gate: the shared layer under the surfaces ─────────────────────


def test_a_gateway_that_is_not_a_url_is_not_a_loopback_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review blocker A, at the discriminator.

    ``stg.example`` parses with the whole string as the PATH: no scheme, no
    host. ``is_loopback`` counts an empty host as local, so with a loopback
    proxy the pair-exemption fired and the lane read GREEN over a gateway
    nothing can reach — configured, green and stranded, through the primary
    documented path. The writer refuses it now; a hand-edited config or
    ``$EXPLAINABILITY_GATEWAY_URL`` still deliver one, and this is what they
    get: amber in the proxy lane (a live proxy is not to blame for a config
    value), red in the config lane, whose fact it is.
    """
    monkeypatch.setattr(ops, "probe_proxy", _probes(None))
    settings = _wired("stg.example", proxy_url="http://127.0.0.1:9090")
    checks = {c.name: c for c in ops.checks(settings, env=_env())}

    proxy = checks["explainability proxy"]
    assert proxy.status is CheckStatus.warn, "green was the bug; red would blame the proxy"
    assert "scheme" in proxy.detail and "cannot be compared" in proxy.detail
    assert proxy.fix
    config = checks["explainability config"]
    assert config.status is CheckStatus.fail
    assert "stg.example" in config.detail and "scheme" in config.detail
    assert "--gateway-url" in (config.fix or "")


def test_enable_refuses_a_schemeless_gateway_and_stores_nothing(runner: CliRunner) -> None:
    """Review blocker A, at the door the first round left open.

    The form refused ``stg.example``; ``aisquare explainability enable
    --gateway-url stg.example`` — the runbook command four characters short —
    stored it and switched tracing on around it.
    """
    from aisquare.cli.app import app as cli_app

    result = runner.invoke(
        cli_app, ["explainability", "enable", "--target", "stg", "--gateway-url", "stg.example"]
    )

    assert result.exit_code == 1, result.output
    assert "scheme" in result.output and "https://stg.example" in result.output
    settings = load_config().explainability
    assert settings.targets == {}, "nothing stored"
    assert settings.enabled is False, "and tracing was not switched on around it"


def test_the_unset_gateway_amber_names_where_the_proxy_ships(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review follow-up K. The proxy DID say where it ships, and that is the
    most useful sentence available to an operator with nothing configured —
    so it is the one printed, with the command that adopts it."""
    monkeypatch.setattr(ops, "probe_proxy", _probes("https://g.example"))
    settings = ExplainabilitySettings(
        enabled=True,
        targets={"stg": ExplainabilityTarget(gateway_url="", proxy_url="https://p.example:9443")},
    )
    proxy = next(c for c in ops.checks(settings, env=_env()) if c.name == "explainability proxy")

    assert proxy.status is CheckStatus.warn
    assert "https://g.example" in proxy.detail
    assert "--gateway-url https://g.example" in (proxy.fix or "")


def test_the_local_proxy_amber_names_the_mechanism(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review follow-up L. "Not on the gateway's host" reads as a wrong address
    for a deliberately-local sidecar. The actionable fact is WHY it might ship
    elsewhere: it took its destination from the environment it was started
    with. The fix names that variable and the deployment's own proxy."""
    monkeypatch.setattr(ops, "probe_proxy", _probes(None))
    settings = _wired("https://stg.example", proxy_url="http://127.0.0.1:9090")
    proxy = next(c for c in ops.checks(settings, env=_env()) if c.name == "explainability proxy")

    assert proxy.status is CheckStatus.warn
    assert "EXPLAINABILITY_GATEWAY_URL" in proxy.detail and "started" in proxy.detail
    fix = proxy.fix or ""
    assert "EXPLAINABILITY_GATEWAY_URL=https://stg.example" in fix
    assert "https://stg.example:9443" in fix, "the deployment's own proxy, spelled out"


def test_an_off_host_proxy_is_not_ordered_to_repoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review follow-up E. A gateway at api.example with its proxy at
    proxy.example:9443 — an ordinary LB/CNAME split — is a correct deployment
    this lane cannot verify from here. Amber is honest; an imperative to
    repoint is not. The remediation asks the question and gives both answers,
    and stays amber rather than green because a field the proxy did not send
    is still unknown."""
    monkeypatch.setattr(ops, "probe_proxy", _probes(None))
    settings = _wired("https://api.example", proxy_url="https://proxy.example:9443")
    proxy = next(c for c in ops.checks(settings, env=_env()) if c.name == "explainability proxy")

    assert proxy.status is CheckStatus.warn
    assert "cannot be checked" in proxy.detail
    fix = proxy.fix or ""
    assert "nothing is wrong" in fix
    assert "EXPLAINABILITY_GATEWAY_URL=https://api.example" in fix
    assert "https://api.example:9443" in fix
    assert not fix.startswith("Point this CLI"), "no bare imperative for a state that may be right"


def test_the_verdict_is_one_field() -> None:
    """Review follow-up G. ``healthy`` survived one round as a second,
    independently settable encoding of the verdict, so
    ``ProxyState(healthy=True, severity=fail)`` was constructible and two
    surfaces branched on it. Pinned structurally: one field, one derivation."""
    from dataclasses import fields

    assert {f.name for f in fields(ops.ProxyState)} == {"summary", "severity", "remediation"}
    assert ops.ProxyState("x", CheckStatus.fail).problem is True
    assert ops.ProxyState("x", CheckStatus.warn).problem is False
    assert ops.ProxyState("x").problem is False


def test_chosen_proxy_is_the_targets_then_a_deliberate_top_level_one_then_nothing() -> None:
    """Review follow-up F. The form asked the per-target value only, so a
    deliberate top-level ``[explainability] proxy_url`` — which ``_proxy_source``
    reports as ``config`` for exactly this reason — was shadowed by the hosted
    suggestion. The shipped default is the one value nobody chose."""
    assert ops.chosen_proxy(ExplainabilitySettings()) is None

    top_level = ExplainabilitySettings(proxy_url="http://127.0.0.1:9190")
    assert ops.chosen_proxy(top_level) == "http://127.0.0.1:9190"
    assert ops.chosen_proxy(top_level, "prod") == "http://127.0.0.1:9190", "no entry needed"

    own = ExplainabilitySettings(
        proxy_url="http://127.0.0.1:9190",
        targets={"stg": ExplainabilityTarget(proxy_url="https://stg.example:9443")},
    )
    assert ops.chosen_proxy(own, "stg") == "https://stg.example:9443"
    assert ops.chosen_proxy(own, "prod") == "http://127.0.0.1:9190"
