"""Explainability wiring: default-off, fail-open, and the identity contract.

The properties under test are the launch-safety bar, not implementation
detail: a default config must wire nothing, and no failure anywhere in the
tracing path (dead proxy, wrong proxy, user-owned env vars, config typos) may
ever stop a session from launching — untraced-with-a-reason is the worst
allowed outcome.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from aisquare.core import paths
from aisquare.core.config import AppConfig, ExplainabilitySettings, load_config, save_config
from aisquare.services.explainability import (
    ProxyProbe,
    disown_inherited_trace,
    join_records,
    plan_session_identity,
    probe_proxy,
    record_join,
    trace_marker,
    traced_by,
    wire_session,
)


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


# ── the correlation spine: which id keys the Run ─────────────────────────────


def test_plan_mints_an_id_and_tells_the_agent_to_use_it() -> None:
    """The join only exists if the LAUNCHER chooses the id: the board keys a
    session by the id the agent reports, so an id we merely trace under would
    name a Run nothing on the board can be matched to."""
    plan = plan_session_identity("claude", [])
    assert plan.session_id is not None
    assert plan.inject_args == ("--session-id", plan.session_id)
    assert plan.note == ""
    uuid.UUID(plan.session_id)  # claude's --session-id takes a uuid, nothing else


def test_plan_never_mints_the_same_id_twice() -> None:
    first = plan_session_identity("claude", [])
    second = plan_session_identity("claude", [])
    assert first.session_id != second.session_id


def test_plan_reads_a_session_id_the_caller_already_chose() -> None:
    """Both spellings, and no second --session-id on the command line: two of
    them is a launch error, and the caller's id is already the board's."""
    for args in (["--session-id", "sess-7"], ["--session-id=sess-7"]):
        plan = plan_session_identity("claude", args)
        assert plan.session_id == "sess-7"
        assert plan.inject_args == ()


def test_plan_reads_the_session_being_resumed() -> None:
    plan = plan_session_identity("claude", ["--resume", "sess-9", "--model", "opus"])
    assert plan.session_id == "sess-9"
    assert plan.inject_args == ()


def test_plan_leaves_a_run_time_session_choice_unjoined() -> None:
    """--continue and a bare --resume name a session that does not exist yet.
    Minting an id anyway would put a SECOND identity on a row the resumed
    session already owns; no join is the safe answer, and it is said out loud.
    """
    for args in (["--continue"], ["-c"], ["--resume"], ["-r", "--model", "opus"]):
        plan = plan_session_identity("claude", args)
        assert plan.session_id is None, args
        assert plan.inject_args == (), args
        assert plan.note, args


def test_plan_hands_the_flag_to_claude_and_to_nothing_else() -> None:
    """Only the ONE program verified to accept it.

    Since #57 a role can be bound to any executable, and an unknown flag is a
    dead launch — so this match is deliberately narrow. ``claude2`` and a
    wrapper merely NAMED after claude lose nothing by it: the hook seam joins
    them without a flag, so all the narrow match costs is the nicety of the
    two ids being identical rather than merely joined.
    """
    for binary in ("claude", "/usr/local/bin/claude"):
        assert plan_session_identity(binary, []).inject_args, binary
    for binary in ("claude2", "claude-next", "aider", "/opt/tools/wrapper"):
        plan = plan_session_identity(binary, [])
        assert plan.inject_args == (), binary
        assert plan.session_id is None, binary
        assert plan.note, binary


def test_pinning_can_be_turned_off_for_the_launch_it_breaks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An escape hatch that costs a join, never a launch — the same trade the
    rest of this module makes."""
    monkeypatch.setenv("AISQUARE_PIN_SESSION_ID", "0")
    plan = plan_session_identity("claude", [])
    assert plan.inject_args == ()
    assert plan.session_id is None


# ── the join record: board rows to Runs, without dashboard access ────────────


def test_record_join_appends_one_line_per_session(isolated_home: Path) -> None:
    """Both halves are always real now — the hook writes this, and it holds the
    board session id and the pipeline id at the same moment. There is no
    "unjoined row" case left to represent."""
    assert (
        record_join(
            session_id="board-1", pipeline_id="run-1", agent_name="aisquare-coder", role="coder"
        )
        is None
    )
    assert record_join(session_id="board-2", pipeline_id="run-2") is None

    records = join_records()
    assert [r["session_id"] for r in records] == ["board-1", "board-2"]
    assert records[0]["pipeline_id"] == "run-1"
    assert records[0]["agent_name"] == "aisquare-coder"
    assert records[0]["role"] == "coder"
    assert records[0]["started_at"]
    assert paths.explainability_joins_path().exists()


def test_the_marker_is_what_carries_the_run_into_the_agent() -> None:
    wiring = wire_session(_settings(), "coder", session_id="sess-1", prober=_healthy)
    assert trace_marker(wiring) == {
        "AISQUARE_PIPELINE_ID": "sess-1",
        "AISQUARE_TRACE_AGENT_NAME": "aisquare-coder",
    }
    assert trace_marker(wire_session(_settings(), "coder", prober=_dead)) == {}, (
        "a stale marker would have the agent's hook record a join that is not true"
    )


def test_our_marker_never_writes_the_variable_the_sdk_routes_on() -> None:
    """``AISQUARE_AGENT_NAME`` belongs to the SDK and to the operator.

    It is the routing identity the Explainability SDK reads and the operator
    sets in their env file — this module already names it as
    ``AGENT_NAME_ENV_VAR``, beside the gateway URL and the API key. Writing it
    from the launcher would silently override the operator's routing, which is
    exactly what the reserved-var guard refuses to do for ``ANTHROPIC_*``. Our
    marker is internal plumbing and keeps its own name.

    Not an observed failure — a read of the SDK's documented contract against
    this module's diff, pinned so it cannot become one.
    """
    from aisquare.services import explainability as service

    assert service.TRACE_AGENT_NAME_ENV_VAR != service.AGENT_NAME_ENV_VAR
    assert service.AGENT_NAME_ENV_VAR == "AISQUARE_AGENT_NAME", "the SDK's, unchanged"

    wiring = wire_session(_settings(), "coder", session_id="sess-1", prober=_healthy)
    assert service.AGENT_NAME_ENV_VAR not in trace_marker(wiring)
    assert service.AGENT_NAME_ENV_VAR not in wiring.env


def test_an_operators_sdk_routing_identity_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The end-to-end version of the same promise, at the env seam."""
    from aisquare.services import explainability as service

    monkeypatch.setenv("AISQUARE_AGENT_NAME", "their-registered-identity")
    wiring = wire_session(_settings(), "coder", session_id="sess-1", prober=_healthy)
    marker = trace_marker(wiring)

    assert marker["AISQUARE_TRACE_AGENT_NAME"] == "aisquare-coder"
    assert "AISQUARE_AGENT_NAME" not in marker
    assert os.environ["AISQUARE_AGENT_NAME"] == "their-registered-identity"
    assert service.traced_by({**os.environ, **marker}) == ("sess-1", "aisquare-coder")


def test_traced_by_reads_the_marker_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AISQUARE_PIPELINE_ID", raising=False)
    assert traced_by() is None, "an ordinary session leaves after one lookup"

    monkeypatch.setenv("AISQUARE_PIPELINE_ID", "run-7")
    monkeypatch.setenv("AISQUARE_TRACE_AGENT_NAME", "aisquare-coder")
    assert traced_by() == ("run-7", "aisquare-coder")

    monkeypatch.setenv("AISQUARE_PIPELINE_ID", "   ")
    assert traced_by() is None, "a blank marker is not a Run"


def test_disowning_takes_only_what_is_ours(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole safety of nested tracing rests on this discrimination."""
    ours = {
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:9190",
        "ANTHROPIC_CUSTOM_HEADERS": "X-Pipeline-Id: parent",
        "AISQUARE_PIPELINE_ID": "parent",
        "AISQUARE_TRACE_AGENT_NAME": "aisquare-planner",
        "KEEP": "1",
    }
    assert disown_inherited_trace(ours) == "parent"
    assert ours == {"KEEP": "1"}

    theirs = {"ANTHROPIC_BASE_URL": "https://my-own-gateway.example", "KEEP": "1"}
    assert disown_inherited_trace(theirs) is None
    assert theirs == {"ANTHROPIC_BASE_URL": "https://my-own-gateway.example", "KEEP": "1"}

    nothing = {"KEEP": "1"}
    assert disown_inherited_trace(nothing) is None
    assert nothing == {"KEEP": "1"}


def test_record_join_fails_open_when_it_cannot_be_written(isolated_home: Path) -> None:
    """The log is a convenience; the session it annotates is not."""
    blocker = paths.explainability_dir()
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_text("not a directory", encoding="utf-8")

    reason = record_join(session_id="board-4", pipeline_id="run-4")

    assert reason is not None and "join record" in reason


def test_join_records_survive_a_half_written_line(isolated_home: Path) -> None:
    record_join(session_id="s-5", pipeline_id="s-5")
    with paths.explainability_joins_path().open("a", encoding="utf-8") as handle:
        handle.write('{"session_id": "s-6", trunca\n')
    record_join(session_id="s-7", pipeline_id="s-7")

    assert [r["session_id"] for r in join_records()] == ["s-5", "s-7"]


def test_join_records_is_empty_before_anything_is_launched(isolated_home: Path) -> None:
    assert join_records() == []


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


def test_a_base_url_we_cannot_use_costs_the_trace_not_the_launch() -> None:
    """``ANTHROPIC_BASE_URL`` is the one value here that costs a LAUNCH.

    The agent parses it before it can report anything, so a malformed one does
    not degrade to untraced — it dies at the first request with "API Error:
    Invalid URL" and exit 1 (runner receipt, under dash). Every other failure
    in this module already fails open; this makes the value itself do the same.
    """
    for bad in ("", "   ", "127.0.0.1:9190", "$http://127.0.0.1:9190", "file:///tmp/x", "http://"):
        wiring = wire_session(_settings(proxy_url=bad), "coder", prober=_healthy)
        assert wiring.traced is False, bad
        assert wiring.env == {}, bad
        assert "proxy_url" in wiring.reason, bad


def test_the_check_refuses_the_value_rather_than_repairing_it() -> None:
    """No silent rewriting. "$http://…" is one keystroke from usable and we
    could strip it — but a value we invented is a value nobody configured, and
    the operator would never learn their config was wrong."""
    wiring = wire_session(_settings(proxy_url="$http://127.0.0.1:9190"), "coder", prober=_healthy)
    assert "$http://127.0.0.1:9190" in wiring.reason, "the reason names what was rejected"
    assert wiring.env == {}


def test_a_usable_base_url_still_traces_normally() -> None:
    """The guard must not become a new way to lose a trace. Shapes an operator
    legitimately configures all still pass."""
    for good in (
        "http://127.0.0.1:9190",
        "https://proxy.example.com",
        "http://localhost:9190/",
        "https://proxy.example.com:8443/base",
    ):
        wiring = wire_session(_settings(proxy_url=good), "coder", prober=_healthy)
        assert wiring.traced is True, good
        assert wiring.env["ANTHROPIC_BASE_URL"] == good, good


def test_the_operators_own_routing_is_judged_by_them_not_by_us() -> None:
    """We validate what WE would set, and never police what they set. Their
    var makes us stand down — the reason is the stand-down, not a verdict on
    our config."""
    base = {"ANTHROPIC_BASE_URL": "https://my-own-gateway.example"}
    wiring = wire_session(_settings(), "coder", base_env=base, prober=_healthy)
    assert wiring.traced is False
    assert wiring.env == {}
    assert "already set" in wiring.reason
    assert "proxy_url" not in wiring.reason
    assert "WARNING" not in wiring.reason, "a usable value of theirs is not our business"


def test_an_unusable_value_of_theirs_is_named_but_never_overridden() -> None:
    """The vector that actually kills launches, and the most we may do about it.

    A corrupt ANTHROPIC_BASE_URL already in the environment — a stale shell
    from before the emitter fix, a wrapper, a typo — is not ours to remove:
    overriding the operator's routing is forbidden, and we cannot know it is
    wrong FOR THEM. But the agent is about to die with "API Error: Invalid
    URL" and exit 1, and nothing in that message points at the cause. Saying
    so costs nothing, changes nothing, and is the difference between a
    two-minute fix and an hour.
    """
    base = {"ANTHROPIC_BASE_URL": "$http://127.0.0.1:9190"}
    wiring = wire_session(_settings(), "coder", base_env=base, prober=_healthy)

    assert wiring.traced is False
    assert wiring.env == {}, "we still stand down — nothing is overridden"
    assert "already set" in wiring.reason
    assert "WARNING" in wiring.reason
    assert "$http://127.0.0.1:9190" in wiring.reason, "name the value, so it can be found"
    assert base == {"ANTHROPIC_BASE_URL": "$http://127.0.0.1:9190"}, "and never mutated"


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


def test_env_exports_survive_a_posix_shell(runner, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The header pair must reach the agent with a REAL newline in ANY shell.

    This output is eval'd wherever it is pasted, and printed spawn commands
    get run through ``/bin/sh`` — which on Debian and Ubuntu is dash. Bash's
    ``$'…'`` form means nothing there: the value arrives with a literal ``$``
    in front and a literal backslash-n where the separator should be, so the
    proxy reads ONE glued header, never sees ``X-Pipeline-Id``, and files the
    run under its default identity. That is the exact misattribution this
    command exists to prevent, which is why the assertion is the round trip
    through a real shell and not the quoting style.
    """
    import subprocess

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
    read_back = 'printf "%s" "$ANTHROPIC_BASE_URL|$ANTHROPIC_CUSTOM_HEADERS|$AISQUARE_PIPELINE_ID"'
    echoed = subprocess.run(
        ["/bin/sh", "-c", f"{result.output}\n{read_back}"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert echoed.returncode == 0, echoed.stderr
    assert echoed.stdout == f"{url}|X-Agent-Name: aisquare-coder\nX-Pipeline-Id: sess-9|sess-9"
