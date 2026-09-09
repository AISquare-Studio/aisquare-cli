"""Coding-agent contracts over real config/store services and fake terminal seams."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypeVar
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import agent_adapters, agents, harness, insights, outbox
from aisquare.core.agent_adapters.codex import CodexAdapter
from aisquare.core.agent_adapters.types import config_home
from aisquare.core.config import AgentModelSettings, RoleLaunchProfile, load_config, save_config
from aisquare.core.orchestrator import team_project
from aisquare.core.store import store_session
from aisquare.models import FleetAgent
from aisquare.services import agent_events, agent_launch, hooks, mcp_server, native_telemetry, team
from aisquare.services.agents import connect, disconnect

T = TypeVar("T")


def present(value: T | None) -> T:
    assert value is not None
    return value


@pytest.fixture(autouse=True)
def isolated_agents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agents, "_home", lambda: tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    monkeypatch.setenv("AISQUARE_TEAM", "1")
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    monkeypatch.setenv("AISQUARE_HARNESS_PROBE", "0")
    for key in (agent_launch.ACTIVE_AGENT_ENV, "AISQUARE_LAUNCH_ID", "AISQUARE_FLEET_AGENT"):
        monkeypatch.delenv(key, raising=False)


def test_selection_precedence_and_mixed_roles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert agent_launch.resolve().adapter.id == "claude-code"
    agent_launch.use("codex")
    assert agent_launch.resolve().source == "user"
    monkeypatch.setenv(agent_launch.ACTIVE_AGENT_ENV, "claude-code")
    assert agent_launch.resolve().source == "inherited"
    agent_launch.use("codex", project=True, cwd=tmp_path)
    assert agent_launch.resolve().source == "project"
    config = load_config()
    config.team.profiles["reviewer"] = RoleLaunchProfile(agent="claude-code")
    save_config(config)
    assert agent_launch.resolve("reviewer").adapter.id == "claude-code"
    assert agent_launch.resolve("reviewer", agent="codex").source == "flag"
    assert agent_launch.resolve("coder").adapter.id == "codex"


def test_wrappers_declare_family_and_conflicts_fail(tmp_path: Path) -> None:
    assert agent_launch.resolve(binary="/opt/bin/codex").adapter.id == "codex"
    config = load_config()
    config.team.profiles["coder"] = RoleLaunchProfile(
        agent="codex", bin="/opt/wrapper", env={"CODEX_HOME": str(tmp_path / "second")}
    )
    save_config(config)
    selected = agent_launch.resolve()
    assert selected.binary.binary == "/opt/wrapper"
    assert selected.config_dir == tmp_path / "second"
    with pytest.raises(ValueError, match="runs claude-code"):
        agent_launch.resolve(agent="codex", binary="claude")


def test_third_adapter_reuses_selection_models_and_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Third(CodexAdapter):
        id = "terminal-fixture"
        binary = "terminal-fixture"
        home_env = "FIXTURE_AGENT_HOME"
        home_name = ".fixture"

    monkeypatch.setattr(agent_adapters, "_ADAPTERS", dict(agent_adapters._ADAPTERS))
    agent_adapters.register(Third())
    agent_launch.use(Third.id)
    selected = agent_launch.resolve()
    assert selected.binary.binary == Third.binary
    assert (
        present(agent_launch.model_for(selected, "coder", probe=False)).source == "native-default"
    )
    assert config_home(selected.adapter, tmp_path, {}) == tmp_path / ".fixture"


@pytest.mark.parametrize("effort", ["minimal", "low", "medium", "high", "xhigh"])
def test_codex_model_policy_uses_native_flags_without_probing(
    effort: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(harness, "probe_model", Mock(side_effect=AssertionError("paid probe")))
    monkeypatch.setenv("CLAUDE_EFFORT", "ultracode")
    config = load_config()
    config.agents.models["codex"] = AgentModelSettings(model="test-model", effort=effort)
    save_config(config)
    selected = agent_launch.resolve(agent="codex")
    resolution = agent_launch.model_for(selected, "coder", probe=True)
    assert resolution and resolution.model == "test-model" and resolution.effort == effort
    assert selected.adapter.model_args(resolution.model, resolution.effort) == [
        "--model",
        "test-model",
        "-c",
        f'model_reasoning_effort="{effort}"',
    ]
    with pytest.raises(ValueError, match="reasoning effort"):
        agent_launch.model_for(selected, "coder", effort="ultracode")


@pytest.mark.parametrize("agent", ["claude-code", "codex"])
def test_hook_merge_preserves_other_handlers_and_disconnect_removes_only_ours(
    agent: str, tmp_path: Path
) -> None:
    directory = tmp_path / agent
    directory.mkdir()
    adapter = agent_adapters.get_adapter(agent)
    path = directory / adapter.settings_name
    unrelated = {"type": "command", "command": "run-my-hook", "timeout": 5}
    path.write_text(
        json.dumps({"custom": True, "hooks": {"Stop": [{"matcher": "*", "hooks": [unrelated]}]}})
    )
    assert agents.install_hooks(agent, directory)
    first = path.read_bytes()
    stamp = path.stat().st_mtime_ns
    assert agents.install_hooks(agent, directory)
    assert path.read_bytes() == first and path.stat().st_mtime_ns == stamp
    assert agents.hooks_installed(agent, directory)
    assert agents.remove_hooks(agent, directory)
    assert json.loads(path.read_text()) == {
        "custom": True,
        "hooks": {"Stop": [{"matcher": "*", "hooks": [unrelated]}]},
    }


def test_connect_respects_override_and_reports_native_trust(tmp_path: Path) -> None:
    directory = tmp_path / ".codex"
    directory.mkdir()
    (directory / "AGENTS.md").write_text("wrong global instructions")
    (directory / "AGENTS.override.md").write_text("effective global instructions")
    receipt = connect("codex", directory)
    assert receipt.imported == 1 and receipt.readiness == "unverified"
    assert "/hooks" in receipt.detail
    with store_session() as store:
        assert [entry.text for entry in store.entries("user")] == ["effective global instructions"]
    agents.observe_hooks("codex", directory)
    assert agents.integration_readiness("codex", directory)[0] == "observed"
    path = directory / "hooks.json"
    data = json.loads(path.read_text())
    data["hooks"]["Stop"][0]["hooks"][0]["command"] += " --different"
    path.write_text(json.dumps(data))
    assert agents.integration_readiness("codex", directory)[0] != "observed"
    disconnect("codex", directory)
    assert (directory / "AGENTS.override.md").read_text() == "effective global instructions"


def test_malformed_hook_config_is_never_overwritten(tmp_path: Path) -> None:
    directory = tmp_path / ".codex"
    directory.mkdir()
    path = directory / "hooks.json"
    path.write_text("{broken")
    with pytest.raises(ValueError):
        agents.install_hooks("codex", directory)
    assert path.read_text() == "{broken"
    assert not agents.hooks_installed("cursor")


def test_native_identity_lifecycle_early_fleet_binding_and_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / ".codex"
    monkeypatch.setenv("AISQUARE_LAUNCH_ID", "launch-one")
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", "fleet-one")
    payload = {
        "session_id": "native-one",
        "cwd": str(tmp_path),
        "hook_event_name": "SessionStart",
        "source": "startup",
    }
    agent_events.handle_codex(payload, directory)
    session_id = agent_events.session_key("codex", directory, "native-one")
    assert session_id != agent_events.session_key("codex", tmp_path / "other", "native-one")
    project = team_project(tmp_path)
    with store_session() as store:
        session = store.get_session(session_id)
        assert session and session.agent == "codex" and session.native_session_id == "native-one"
        assert store.get_meta("fleet-session:fleet-one") == session_id
        store.upsert_fleet_agent(
            FleetAgent(
                id="fleet-one",
                project_id=project.id,
                role="coder",
                label="coder-1",
                binary="codex",
                agent="codex",
                tmux_socket="test",
                pane_id="%1",
                cwd=tmp_path,
                created_at=datetime.now(UTC),
            )
        )
        assert present(store.get_fleet_agent("fleet-one")).session_id == session_id
    assert mcp_server.client_session_id(project.id) == session_id
    prompt = {
        **payload,
        "hook_event_name": "UserPromptSubmit",
        "turn_id": "turn-one",
        "prompt": "implement acceptance",
    }
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda _: agent_events.handle_codex(prompt, directory), range(2)))
    with store_session() as store:
        captured = store.recent_prompts(project.id)
        assert len(captured) == 1 and captured[0].source == "codex"
    task, _ = team.add_task("Verify acceptance", cwd=tmp_path)
    with store_session() as store:
        assert store.claim_task(task.id, session_id, datetime.now(UTC) + timedelta(minutes=5))
    for event, state in [
        ("PermissionRequest", "attention"),
        ("PostToolUse", "working"),
        ("Interrupt", "waiting"),
    ]:
        agent_events.handle_codex({**payload, "hook_event_name": event}, directory)
        with store_session() as store:
            assert present(store.get_session(session_id)).state == state
    agent_events.handle_codex({**payload, "hook_event_name": "SessionEnd"}, directory)
    with store_session() as store:
        assert present(store.get_session(session_id)).ended_at is not None
        assert present(store.get_task(task.id)).claimed_by is None
    agent_events.handle_codex({**payload, "source": "resume"}, directory)
    with store_session() as store:
        assert present(store.get_session(session_id)).ended_at is None


def test_continuations_are_deduplicated_and_attributed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    native = {"session_id": "native", "cwd": str(tmp_path), "hook_event_name": "SessionStart"}
    directory = tmp_path / ".codex"
    agent_events.handle_codex(native, directory)
    guard = Mock(return_value=team.StopDecision("follow the fresh board decision", cursor=4))
    monkeypatch.setattr(hooks, "turn_stopped", guard)
    stop = {**native, "hook_event_name": "Stop", "turn_id": "turn-one"}
    result = agent_events.handle_codex(stop, directory)
    assert result == agent_events.handle_codex(stop, directory)
    assert guard.call_count == 1
    assert json.loads(present(result))["decision"] == "block"
    agent_events.handle_codex(
        {
            **native,
            "hook_event_name": "UserPromptSubmit",
            "turn_id": "continuation",
            "prompt": "follow the fresh board decision",
        },
        directory,
    )
    with store_session() as store:
        assert store.recent_prompts(team_project(tmp_path).id)[0].source == "codex:continuation"


def test_native_telemetry_is_opt_in_redacted_and_replayable(tmp_path: Path) -> None:
    payload = {
        "resourceLogs": [
            {
                "scopeLogs": [
                    {
                        "logRecords": [
                            {
                                "eventName": "codex.sse_event",
                                "timeUnixNano": "0",
                                "observedTimeUnixNano": "1",
                                "attributes": [
                                    {"key": key, "value": {"stringValue": value}}
                                    for key, value in {
                                        "model": "local-model",
                                        "input_token_count": "19",
                                        "output_token_count": "3",
                                        "prompt": "DO_NOT_SPOOL",
                                        "authorization": "DO_NOT_SPOOL",
                                    }.items()
                                ],
                            }
                        ]
                    }
                ]
            }
        ]
    }
    assert native_telemetry.capture(payload, "launch") == 0
    config = load_config()
    config.explainability.enabled = config.explainability.ship = True
    save_config(config)
    insights.reset_cache()
    assert native_telemetry.capture(payload, "launch") == 1
    assert native_telemetry.capture(payload, "launch") == 0
    record = json.loads(outbox.pending()[0].read_text())
    assert "DO_NOT_SPOOL" not in json.dumps(record)
    assert record["native"]["model"] == "local-model"
    assert record["native"]["native_time"] == "1"
    # Distinct requests with identical usage are not retries. Codex's native
    # logger supplies an observed timestamp when the event timestamp is zero.
    payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]["observedTimeUnixNano"] = "2"
    assert native_telemetry.capture(payload, "launch") == 1
    assert native_telemetry.capture(payload, "launch") == 0
    assert len(outbox.pending()) == 2
    # The native span stream describes those calls again; only logs account
    # for model usage in the replay.
    assert native_telemetry.capture({"resourceSpans": [{}]}, "launch") == 0
    config.explainability.enabled = False
    save_config(config)
    assert native_telemetry.capture(payload, "second") == 0


def test_native_exporter_preserves_operator_configuration(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text('[otel]\nexporter="none"\n')
    assert native_telemetry.operator_configured(tmp_path, [])
    assert native_telemetry.operator_configured(tmp_path / "other", ["-c", 'otel.exporter="none"'])


def test_new_cli_selection_and_native_model_rendering(runner: CliRunner) -> None:
    chosen = runner.invoke(app, ["agents", "use", "codex"])
    assert chosen.exit_code == 0, chosen.output
    spawned = runner.invoke(app, ["--json", "team", "spawn", "coder", "--no-probe"])
    assert spawned.exit_code == 0, spawned.output
    result = json.loads(spawned.stdout)
    assert result["agent"] == "codex" and result["source"] == "native-default"
    assert "--effort" not in result["command"] and "--agent codex" in result["command"]
    status = runner.invoke(app, ["--json", "team", "harness"])
    assert status.exit_code == 0, status.output
    assert all(
        row["agent"] == "codex" and not row["ladder"] for row in json.loads(status.stdout)["roles"]
    )


def test_native_records_replay_through_the_real_optional_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    from aisquare.services import explainability

    sdk = pytest.importorskip("aisquare.explainability")
    tracing = importlib.import_module("opentelemetry.sdk.trace")
    exporting = importlib.import_module("opentelemetry.sdk.trace.export")
    in_memory = importlib.import_module("opentelemetry.sdk.trace.export.in_memory_span_exporter")
    tracer_module = importlib.import_module("aisquare.explainability.tracers")
    exporter = in_memory.InMemorySpanExporter()
    provider = tracing.TracerProvider()
    provider.add_span_processor(exporting.SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracer_module, "get_tracer", lambda: provider.get_tracer("fixture"))
    with sdk.AgentRunTracer(agent_name="aisquare-coder", run_id="native-launch"):
        explainability._emit_span(
            sdk,
            {
                "kind": "native_event",
                "native": {
                    "event.name": "codex.sse_event",
                    "model": "fixture-model",
                    "input_token_count": 19,
                    "output_token_count": 3,
                },
            },
        )
        explainability._emit_span(
            sdk,
            {
                "kind": "native_event",
                "native": {
                    "event.name": "codex.tool_result",
                    "tool_name": "exec_command",
                    "success": True,
                },
            },
        )
    spans = exporter.get_finished_spans()
    kinds = {span.attributes.get("openinference.span.kind"): span for span in spans}
    assert kinds["LLM"].attributes["llm.token_count.prompt"] == 19
    assert kinds["LLM"].attributes["llm.model_name"] == "fixture-model"
    assert kinds["TOOL"].attributes["tool.name"] == "exec_command"
    assert len({span.context.trace_id for span in spans}) == 1
    provider.shutdown()
