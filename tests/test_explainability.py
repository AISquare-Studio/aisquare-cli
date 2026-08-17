"""Explainability wiring: default-off, fail-open, and the identity contract.

The properties under test are the launch-safety bar, not implementation
detail: a default config must wire nothing, and no failure anywhere in the
tracing path (dead proxy, wrong proxy, user-owned env vars, config typos) may
ever stop a session from launching — untraced-with-a-reason is the worst
allowed outcome.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import ClassVar

from aisquare.core.config import AppConfig, ExplainabilitySettings, load_config, save_config
from aisquare.services.explainability import ProxyProbe, probe_proxy, wire_session


def _settings(**overrides: object) -> ExplainabilitySettings:
    return ExplainabilitySettings(enabled=True, **overrides)


def _healthy(url: str) -> ProxyProbe:
    return ProxyProbe(True, "proxy healthy")


def _dead(url: str) -> ProxyProbe:
    return ProxyProbe(False, "proxy unreachable at test://health: refused")


class _HealthHandler(BaseHTTPRequestHandler):
    payload: ClassVar[dict[str, str]] = {}

    def do_GET(self) -> None:
        body = json.dumps(self.payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # keep pytest output clean
        return


def _serve(payload: dict[str, str]) -> tuple[HTTPServer, str]:
    handler = type("Handler", (_HealthHandler,), {"payload": payload})
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


# ── the opt-in gate ──────────────────────────────────────────────────────────


def test_default_config_is_off_and_wires_nothing() -> None:
    config = AppConfig()
    assert config.explainability.enabled is False
    wiring = wire_session(config.explainability, "coder", prober=_healthy)
    assert wiring.traced is False
    assert wiring.env == {}


def test_config_written_before_this_section_still_loads(tmp_path: Path) -> None:
    legacy = tmp_path / "config.toml"
    legacy.write_text('profile = "old"\n', encoding="utf-8")
    config = load_config(legacy)
    assert config.profile == "old"
    assert config.explainability == ExplainabilitySettings()


def test_round_trip_preserves_explainability(tmp_path: Path) -> None:
    config = AppConfig(
        explainability=ExplainabilitySettings(enabled=True, proxy_url="http://127.0.0.1:9190")
    )
    target = save_config(config, tmp_path / "config.toml")
    assert load_config(target) == config


# ── wiring the happy path ────────────────────────────────────────────────────


def test_wire_session_builds_the_identity_pair() -> None:
    wiring = wire_session(_settings(), "coder", prober=_healthy)
    assert wiring.traced is True
    assert wiring.env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9090"
    headers = wiring.env["ANTHROPIC_CUSTOM_HEADERS"]
    assert f"X-Agent-Name: {wiring.agent_name}" in headers
    assert f"X-Pipeline-Id: {wiring.pipeline_id}" in headers
    assert wiring.agent_name == "aisquare-coder"


def test_wire_session_keys_the_run_to_a_given_session_id() -> None:
    wiring = wire_session(_settings(), "planner", session_id="sess-42", prober=_healthy)
    assert wiring.pipeline_id == "sess-42"
    assert "X-Pipeline-Id: sess-42" in wiring.env["ANTHROPIC_CUSTOM_HEADERS"]


def test_two_launches_never_share_a_pipeline_id() -> None:
    """Distinct pipeline ids are what keep concurrent sessions from merging
    into one Run — the exact failure observed with back-to-back sessions in
    the stg spike."""
    first = wire_session(_settings(), "coder", prober=_healthy)
    second = wire_session(_settings(), "coder", prober=_healthy)
    assert first.pipeline_id != second.pipeline_id


# ── fail-open, in every direction ────────────────────────────────────────────


def test_dead_proxy_fails_open() -> None:
    wiring = wire_session(_settings(), "coder", prober=_dead)
    assert wiring.traced is False
    assert wiring.env == {}
    assert "unreachable" in wiring.reason


def test_user_owned_anthropic_vars_are_never_clobbered() -> None:
    base = {"ANTHROPIC_BASE_URL": "https://my-own-gateway.example"}
    wiring = wire_session(_settings(), "coder", base_env=base, prober=_healthy)
    assert wiring.traced is False
    assert wiring.env == {}
    assert "ANTHROPIC_BASE_URL" in wiring.reason
    assert base == {"ANTHROPIC_BASE_URL": "https://my-own-gateway.example"}


def test_bad_agent_name_template_fails_open() -> None:
    wiring = wire_session(_settings(agent_name_template="aisquare-{rol}"), "coder", prober=_healthy)
    assert wiring.traced is False
    assert "agent_name_template" in wiring.reason


def test_header_unsafe_role_fails_open() -> None:
    wiring = wire_session(_settings(), "coder\nX-Evil: 1", prober=_healthy)
    assert wiring.traced is False
    assert wiring.env == {}


# ── the probe itself, against real listeners ─────────────────────────────────


def test_probe_accepts_the_claude_code_proxy() -> None:
    server, url = _serve({"status": "ok", "service": "aisquare-proxy", "mode": "claude_code"})
    try:
        verdict = probe_proxy(url)
    finally:
        server.shutdown()
    assert verdict.healthy is True


def test_probe_rejects_a_creator_mode_proxy() -> None:
    """The org runs other proxy modes on well-known ports; recording Claude
    Code traffic under the wrong contract is worse than not recording."""
    server, url = _serve({"status": "ok", "service": "aisquare-proxy", "mode": "creator"})
    try:
        verdict = probe_proxy(url)
    finally:
        server.shutdown()
    assert verdict.healthy is False
    assert "creator" in verdict.reason


def test_probe_rejects_a_foreign_service() -> None:
    server, url = _serve({"status": "ok", "service": "something-else", "mode": "claude_code"})
    try:
        verdict = probe_proxy(url)
    finally:
        server.shutdown()
    assert verdict.healthy is False


def test_probe_reports_a_silent_port() -> None:
    server, url = _serve({})
    server.shutdown()
    server.server_close()  # port is now closed — the common "proxy not started" case
    verdict = probe_proxy(url)
    assert verdict.healthy is False
    assert "unreachable" in verdict.reason


# ── the CLI surface: aisquare explainability status / env ───────────────────


def test_status_reports_disabled_config_and_probe_truth(runner) -> None:  # type: ignore[no-untyped-def]
    save_config(
        AppConfig(
            explainability=ExplainabilitySettings(enabled=False, proxy_url="http://127.0.0.1:9")
        )
    )
    from aisquare.cli.app import app as cli_app

    result = runner.invoke(cli_app, ["explainability", "status"])

    assert result.exit_code == 0, result.output
    assert "enabled:  False" in result.output
    assert "unreachable" in result.output


def test_status_exits_nonzero_when_enabled_but_proxy_dead(runner) -> None:  # type: ignore[no-untyped-def]
    """Enabled + dead proxy is the state where launches silently go untraced —
    status is the command that must make that loud."""
    save_config(
        AppConfig(
            explainability=ExplainabilitySettings(enabled=True, proxy_url="http://127.0.0.1:9")
        )
    )
    from aisquare.cli.app import app as cli_app

    result = runner.invoke(cli_app, ["explainability", "status"])

    assert result.exit_code == 1


def test_env_refuses_when_disabled(runner) -> None:  # type: ignore[no-untyped-def]
    from aisquare.cli.app import app as cli_app

    result = runner.invoke(cli_app, ["explainability", "env", "coder"])

    assert result.exit_code == 1
    assert "disabled" in result.output


def test_env_emits_posix_quoted_exports(runner, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The emitted quoting must carry a REAL newline through eval — a glued
    header means X-Pipeline-Id never arrives and the run is misattributed.

    Text only; ``tests/test_explainability_env.py`` evaluates the same output
    in every installed shell, which is the half that counts. The previous
    ``$'…'`` form satisfied an assertion shaped exactly like this one while
    corrupting every value it emitted under /bin/sh."""
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    from aisquare.cli.app import app as cli_app

    server, url = _serve({"status": "ok", "service": "aisquare-proxy", "mode": "claude_code"})
    try:
        save_config(AppConfig(explainability=ExplainabilitySettings(enabled=True, proxy_url=url)))
        result = runner.invoke(
            cli_app, ["explainability", "env", "coder", "--session-id", "sess-9"]
        )
    finally:
        server.shutdown()

    assert result.exit_code == 0, result.output
    assert f"export ANTHROPIC_BASE_URL='{url}'" in result.output
    assert (
        "export ANTHROPIC_CUSTOM_HEADERS="
        "'X-Agent-Name: aisquare-coder\nX-Pipeline-Id: sess-9'" in result.output
    )
