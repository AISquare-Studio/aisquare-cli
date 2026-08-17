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
    assert target.agent_names == ("acme-planner",)
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
    assert not any(c.name == "explainability ingest" for c in checks)


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


def test_expected_sdk_noise_is_not_reported_as_a_finding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """agno and the gateway's OPENAI_API_KEY are not this lane's business;
    printing them red trains an operator to ignore the section."""
    rows = [
        ("delivery_backlog", "ok", "empty"),
        ("agno", "missing", "Install optional dependency: .[agno]"),
        ("openinference_agno", "missing", "Install optional dependency: .[agno]"),
        ("openai_api_key", "warning", "Set OPENAI_API_KEY"),
    ]
    monkeypatch.setattr(ops, "sdk_doctor", lambda **_: rows)
    names = [check.name for check in ops._sdk_checks()]
    assert names == ["sdk:delivery_backlog"]


def test_a_missing_sdk_is_advice_on_a_stock_box_and_a_warning_once_wired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SDK only matters to the lane that ships CLI insights, so it is not
    a finding until this machine is being wired for it."""
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
    assert ops.INSTALL_HINT in stock.detail

    wired = next(
        c
        for c in ops.checks(ExplainabilitySettings(enabled=True), env={})
        if c.name == "explainability sdk"
    )
    assert wired.status is CheckStatus.warn
    assert wired.fix is not None
    assert ops.INSTALL_HINT in wired.fix


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
    actions = ops.apply_fixes(confirm=lambda _: False)
    assert any(ops.INSTALL_HINT in action for action in actions)


def test_fix_installs_when_consent_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ops,
        "sdk_presence",
        lambda: ops.SdkPresence(importable=False, script=None, version=None, shadowing=False),
    )
    monkeypatch.setattr(ops, "install_sdk", lambda: (True, "installed aisquare[explainability]"))
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
    assert posted["body"] == {"agents": ["aisquare-planner", "aisquare-coder", "aisquare-runner"]}


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
