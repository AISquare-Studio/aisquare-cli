"""Operator errors must remain diagnosable and retryable across agent boundaries."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, Mock

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import agent_adapters, agents, harness, insights, outbox, paths
from aisquare.core.config import AgentModelSettings, FleetRoleSettings, load_config, save_config
from aisquare.core.orchestrator import team_project
from aisquare.core.store import store_session
from aisquare.models import CheckStatus
from aisquare.services import (
    agent_events,
    agent_launch,
    diagnostics,
    explainability,
    fleet,
    hooks,
    native_telemetry,
    team,
)
from aisquare.services.agents import connect
from tests.test_fleet_service import FakeTmux
from tests.test_insight_sweeper import FakeSDK, _configure


@pytest.fixture(autouse=True)
def isolated_agents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agents, "_home", lambda: tmp_path)
    monkeypatch.setenv("AISQUARE_TEAM", "1")
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    monkeypatch.setenv("AISQUARE_HARNESS_PROBE", "0")


@pytest.mark.parametrize("family", ["codex", "claude-code"])
def test_custom_fleet_args_stay_with_their_agent(
    family: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_config()
    config.fleet.roles["reviewer"] = FleetRoleSettings(
        extra_args=["--restricted", "--verbose"],
        agent_args={"codex": ["--no-alt-screen"], "claude-code": ["--chrome"]},
        worktree=False,
    )
    save_config(config)
    tmux = FakeTmux()
    monkeypatch.setattr(fleet, "server", lambda config: tmux)
    monkeypatch.setattr(agent_launch, "executable", lambda selected: "/fixture/agent")
    fleet.spawn(team_project(tmp_path), "reviewer", agent=family)
    command = tmux.spawned[-1]["command"]
    assert isinstance(command, list)
    for flag in ("--restricted", "--verbose", "--chrome"):
        assert (flag in command) == (family == "claude-code")
    assert ("--no-alt-screen" in command) == (family == "codex")


@pytest.mark.parametrize(
    "binary",
    ["claude.cmd", "claude.ps1", "CLAUDE.EXE", r"C:\Apps\CLAUDE.CMD", "codex.cmd", "CODEX.PS1"],
)
def test_native_windows_shims_launch_and_report_their_family(
    binary: str, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    family = "claude-code" if "claude" in binary.lower() else "codex"
    assert agent_adapters.adapter_for_binary(binary) == agent_adapters.get_adapter(family)
    assert harness.is_default_agent(binary) == (family == "claude-code")
    monkeypatch.setenv("AISQUARE_BIN_CODER", binary)
    execution = Mock()
    monkeypatch.setattr(import_module("aisquare.cli.launch"), "_exec", execution)
    monkeypatch.setattr(agent_launch, "executable", lambda selected: "/fixture/agent")
    result = runner.invoke(app, ["launch", "coder"])
    assert result.exit_code == 0, result.output
    assert execution.call_args.args[2]["AISQUARE_LAUNCH_AGENT"] == family
    result = runner.invoke(app, ["--json", "team", "harness"])
    row = next(row for row in json.loads(result.stdout)["roles"] if row["role"] == "coder")
    assert row["agent"] == family and row["binary"] == binary


@pytest.mark.parametrize(
    "effort, expected", [("max", "xhigh"), ("ultracode", "xhigh"), (" High ", "high")]
)
def test_codex_effort_is_normalized_in_cli_and_environment(
    effort: str, expected: str, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = agent_launch.resolve(agent="codex")
    monkeypatch.setenv("AISQUARE_EFFORT_CODER", effort)
    result = agent_launch.model_for(selected, "coder", probe=False)
    assert result and result.effort == expected and result.effort_source == "pinned"
    spawned = runner.invoke(
        app, ["--json", "team", "spawn", "coder", "--agent", "codex", "--effort", effort]
    )
    assert spawned.exit_code == 0, spawned.output
    assert f'model_reasoning_effort="{expected}"' in json.loads(spawned.stdout)["command"]


def test_bad_native_effort_cannot_create_a_worktree_window_or_live_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_EFFORT_CODER", "impossible")
    tmux = FakeTmux()
    monkeypatch.setattr(fleet, "server", lambda config: tmux)
    monkeypatch.setattr(agent_launch, "executable", lambda selected: "/fixture/agent")
    worktree = Mock(side_effect=AssertionError("worktree created before validation"))
    monkeypatch.setattr(fleet, "_ensure_worktree", worktree)
    project = team_project(tmp_path)
    with pytest.raises(fleet.FleetError, match="reasoning effort"):
        fleet.spawn(project, "coder", agent="codex", worktree=True)
    assert not tmux.spawned and not worktree.called
    with store_session() as store:
        assert not store.fleet_agents(project.id)


def test_blank_native_pins_fall_through_and_values_are_trimmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_config()
    config.agents.models["codex"] = AgentModelSettings(model=" configured-model ", effort=" High ")
    save_config(config)
    monkeypatch.setenv("AISQUARE_MODEL_CODER", " \t\n")
    monkeypatch.setenv("AISQUARE_EFFORT_CODER", " \t\n")
    selected = agent_launch.resolve(agent="codex")
    model = agent_launch.model_for(selected, "coder", probe=False)
    assert model and (model.model, model.effort, model.source) == (
        "configured-model",
        "high",
        "configured",
    )
    config.agents.models["codex"] = AgentModelSettings(model=" ", effort=" ")
    save_config(config)
    assert (
        agent_launch.resolved_model_args(
            selected, agent_launch.launch_model_for(selected, "coder", []), []
        )
        == []
    )
    monkeypatch.setenv("AISQUARE_MODEL_CODER", " chosen ")
    model = agent_launch.model_for(selected, "coder", probe=False)
    assert model and model.model == "chosen" and model.source == "pinned"


@pytest.mark.parametrize("mode", [[], ["--json"]])
def test_harness_keeps_healthy_rows_beside_a_bad_role(
    mode: list[str], runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_launch.use("codex")
    monkeypatch.setenv("AISQUARE_BIN_CODER", "unbound-wrapper")
    result = runner.invoke(app, [*mode, "team", "harness"])
    assert result.exit_code == 0, result.output
    if mode:
        rows = json.loads(result.stdout)["roles"]
        assert len(rows) == len(harness.ROLE_PROFILES)
        broken = next(row for row in rows if row["role"] == "coder")
        assert broken["error"] == "agent_configuration" and "team bind" in broken["fix"]
        assert all("agent" in row for row in rows if row["role"] != "coder")
    else:
        assert all(role in result.stdout for role in harness.ROLE_PROFILES)
        assert "unbound-wrapper" in result.stdout and "team bind" in result.stdout


def test_unused_codex_home_is_advisory_but_selected_or_connected_homes_are_checked(
    tmp_path: Path,
) -> None:
    home = tmp_path / ".codex"
    home.mkdir()
    checks = [check for check in diagnostics._check_other_agents(tmp_path) if check.name == "codex"]
    assert checks and all(check.status == CheckStatus.ok for check in checks)
    agent_launch.use("codex")
    checks = [check for check in diagnostics._check_other_agents(tmp_path) if check.name == "codex"]
    assert any(
        check.status == CheckStatus.warn and "connect" in (check.fix or "") for check in checks
    )
    agent_launch.use("claude-code")
    agents.install_hooks("codex", home)
    agents.set_connected("codex", True, home)
    checks = [check for check in diagnostics._check_other_agents(tmp_path) if check.name == "codex"]
    assert any(
        check.status == CheckStatus.warn
        and "/hooks" in check.detail
        and "agents connect codex" in (check.fix or "")
        for check in checks
    )


@pytest.mark.parametrize(
    "args, error",
    [
        (["team", "bind", "coder", "--agent", "gemini"], "agent_configuration"),
        (["launch", "coder", "--env", "not-a-pair"], "bad_env_pair"),
        (["team", "spawn", "coder", "--env", "not-a-pair"], "bad_env_pair"),
        (["team", "spawn", "coder", "--agent", "codex", "--effort", ""], "bad_effort"),
        (["team", "spawn", "coder", "--agent", "claude-code", "--effort", ""], "bad_effort"),
    ],
)
def test_operator_errors_have_stable_json_and_do_not_save_config(
    args: list[str], error: str, runner: CliRunner
) -> None:
    before = paths.config_path().read_bytes() if paths.config_path().exists() else None
    result = runner.invoke(app, ["--json", *args])
    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["error"] == error
    after = paths.config_path().read_bytes() if paths.config_path().exists() else None
    assert after == before


@pytest.mark.parametrize(
    "native",
    [
        ["-c", "model_reasoning_effort=high"],
        ["-cmodel_reasoning_effort=high"],
        ["--config=model_reasoning_effort=high"],
    ],
)
def test_native_config_alias_and_attached_model_survive_launch(
    native: list[str], runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_MODEL_CODER", "configured-model")
    execution = Mock()
    monkeypatch.setattr(import_module("aisquare.cli.launch"), "_exec", execution)
    monkeypatch.setattr(agent_launch, "executable", lambda selected: "/fixture/codex")
    result = runner.invoke(
        app, ["launch", "coder", "--agent", "codex", "-mexplicit-model", *native]
    )
    assert result.exit_code == 0, result.output
    argv = execution.call_args.args[1]
    assert "configured-model" not in argv and "-mexplicit-model" in argv
    assert (
        list(agent_adapters.types.option_values(argv, "-c", "--config"))[-1]
        == "model_reasoning_effort=high"
    )


def test_legacy_command_alias_and_delimited_prompt_keep_their_meaning(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    execution = Mock()
    monkeypatch.setattr(import_module("aisquare.cli.launch"), "_exec", execution)
    monkeypatch.setattr(agent_launch, "executable", lambda selected: "/fixture/agent")
    result = runner.invoke(
        app, ["launch", "coder", "-c", "claude", "--", "--", "-c", "literal=prompt"]
    )
    assert result.exit_code == 0, result.output
    assert execution.call_args.args[1][0] == "claude"
    assert execution.call_args.args[1][-3:] == ["--", "-c", "literal=prompt"]


@pytest.mark.parametrize("flag", ["--refresh", "--probe", "--no-probe"])
def test_codex_probe_flags_explain_native_model_selection(flag: str, runner: CliRunner) -> None:
    result = runner.invoke(app, ["--json", "team", "spawn", "coder", "--agent", "codex", flag])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["command"]
    assert "no AISquare availability cache" in result.stderr


def test_project_agent_selection_has_a_machine_readable_scope(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--json", "agents", "use", "codex", "--project"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "agent": "codex",
        "scope": "project",
        "project_id": team_project().id,
    }


def test_harness_reads_one_config_and_project_choice_per_matrix(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aisquare.core import config as config_module

    agent_launch.use("codex", project=True)
    save_config(load_config())
    reader = Mock(wraps=config_module._load_config_file)
    store_reader = Mock(wraps=store_session)
    monkeypatch.setattr(config_module, "_load_config_file", reader)
    monkeypatch.setattr(agent_launch, "store_session", store_reader)
    first = runner.invoke(app, ["--json", "team", "harness"])
    assert first.exit_code == 0, first.output
    assert reader.call_count == 1 and store_reader.call_count == 1
    config = load_config()
    config.agents.models["codex"] = AgentModelSettings(model="changed-model")
    save_config(config)
    second = runner.invoke(app, ["--json", "team", "harness"])
    assert second.exit_code == 0, second.output
    assert all(row["resolves_to"] == "changed-model" for row in json.loads(second.stdout)["roles"])


def test_observation_reuses_unchanged_settings_but_detects_an_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agents.install_hooks("codex", tmp_path)
    definition = agents.hook_fingerprint("codex", tmp_path)
    fingerprint = Mock(wraps=agents.hook_fingerprint)
    monkeypatch.setattr(agents, "hook_fingerprint", fingerprint)
    agents.observe_hooks("codex", tmp_path, definition)
    agents.observe_hooks("codex", tmp_path, definition)
    assert fingerprint.call_count == 1
    path = tmp_path / "hooks.json"
    settings = json.loads(path.read_text())
    settings["hooks"]["Stop"][0]["matcher"] = "changed"
    path.write_text(json.dumps(settings))
    assert agents.integration_readiness("codex", tmp_path)[0] == "unverified"
    agents.observe_hooks("codex", tmp_path, agents.hook_fingerprint("codex", tmp_path))
    assert agents.integration_readiness("codex", tmp_path)[0] == "observed"


def test_each_ladder_walk_fingerprints_the_account_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from aisquare.core import claude_accounts

    identity = Mock(wraps=claude_accounts.identity)
    monkeypatch.setattr(claude_accounts, "identity", identity)
    monkeypatch.setattr(
        harness,
        "probe_model",
        lambda alias: harness.ProbeResult(
            alias=alias, available=False, checked_at=datetime.now(UTC)
        ),
    )
    context = harness.ProbeContext("claude", dict(os.environ))
    harness.resolve_model("planner", probe=True, refresh=True, context=context)
    assert identity.call_count == 1
    harness.resolve_model("planner", probe=False, context=context)
    assert identity.call_count == 2


def test_failed_connect_does_not_import_memories_and_retry_imports_once(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("Instructions that should only be imported on success.")
    settings = tmp_path / "hooks.json"
    settings.write_text("{broken")
    with pytest.raises(agents.AgentSettingsError):
        connect("codex", tmp_path)
    with store_session() as store:
        assert not store.entries("user")
    assert not agents.connected_dirs("codex")
    settings.write_text("{}")
    assert connect("codex", tmp_path).imported == 1
    assert connect("codex", tmp_path).imported == 0


@pytest.mark.parametrize(
    "family, filename", [("claude-code", "settings.json"), ("codex", "hooks.json")]
)
def test_unreadable_hook_settings_have_a_repair_diagnostic(
    family: str, filename: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / (".claude" if family == "claude-code" else ".codex")
    agents.install_hooks(family, home)
    agents.set_connected(family, True, home)
    original = Path.read_text

    def denied(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == home / filename:
            raise PermissionError("fixture permission denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", denied)
    info = agents.detect(family, home)
    assert info and info.readiness == "unreadable" and filename in info.detail
    check = (
        diagnostics._check_claude_code()
        if family == "claude-code"
        else next(
            check for check in diagnostics._check_other_agents(tmp_path) if check.name == family
        )
    )
    assert check.status == CheckStatus.warn and "permission denied" in check.detail
    assert "Repair" in (check.fix or "") and "connect" not in (check.fix or "")


@pytest.mark.skipif(os.name == "nt", reason="POSIX filesystem semantics")
@pytest.mark.parametrize("kind", ["fifo", "device"])
def test_connect_rejects_special_settings_files_without_blocking(kind: str, tmp_path: Path) -> None:
    settings = tmp_path / "hooks.json"
    if kind == "fifo":
        os.mkfifo(settings)
    else:
        settings.symlink_to(os.devnull)
    code = (
        "from pathlib import Path; import sys; from aisquare.services.agents import connect; "
        "connect('codex', Path(sys.argv[1]))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True, timeout=5
    )
    assert result.returncode != 0 and "not a regular file" in result.stderr


@pytest.mark.skipif(os.name == "nt", reason="POSIX filesystem semantics")
def test_hook_writes_preserve_mode_and_other_hard_links(tmp_path: Path) -> None:
    settings = tmp_path / "hooks.json"
    settings.write_text("{}")
    settings.chmod(0o644)
    agents.install_hooks("codex", tmp_path)
    assert stat.S_IMODE(settings.stat().st_mode) == 0o644
    sibling = tmp_path / "shared.json"
    os.link(settings, sibling)
    original = settings.read_bytes()
    assert agents.remove_hooks("codex", tmp_path)
    assert sibling.read_bytes() == original
    assert settings.read_bytes() != original
    assert settings.stat().st_ino != sibling.stat().st_ino


def test_failed_stop_releases_claim_and_cli_reports_the_actual_cost(
    tmp_path: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / ".codex"
    payload = {
        "session_id": "native",
        "cwd": str(tmp_path),
        "hook_event_name": "Stop",
        "turn_id": "one",
    }
    handler = Mock(
        side_effect=[
            team.ManagerWakeupError(OSError("wakeup failed")),
            team.StopDecision("retry decision", cursor=1),
        ]
    )
    monkeypatch.setattr(hooks, "turn_stopped", handler)
    result = runner.invoke(
        app, ["hook", "codex", "--config-dir", str(home)], input=json.dumps(payload)
    )
    assert result.exit_code == 0 and "manager will not be woken" in result.stderr
    assert "event was not recorded" not in result.stderr
    with store_session() as store:
        assert not store.list_meta("agent-event:")
    output = agent_events.handle_codex(payload, home)
    assert output and json.loads(output)["reason"] == "retry decision"
    assert agent_events.handle_codex(payload, home) == output and handler.call_count == 2


@pytest.mark.parametrize("raises", [False, True])
def test_expired_callback_owner_cannot_overwrite_or_delete_a_new_claim(
    raises: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / ".codex"
    sid = agent_events.session_key("codex", home, "native")
    key = f"agent-event:{sid}:Stop:one:False"
    replacement = json.dumps("a newer callback completed")

    def replaced(*args: object, **kwargs: object) -> None:
        with store_session() as store:
            store.set_meta(key, replacement)
        if raises:
            raise RuntimeError("original callback failed")

    monkeypatch.setattr(hooks, "turn_stopped", replaced)
    payload = {"session_id": "native", "hook_event_name": "Stop", "turn_id": "one"}
    if raises:
        with pytest.raises(RuntimeError):
            agent_events.handle_codex(payload, home)
    else:
        agent_events.handle_codex(payload, home)
    with store_session() as store:
        assert store.get_meta(key) == replacement


def test_pending_callback_uses_monotonic_time_and_legacy_clock_rollback_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(time, "time", lambda: 100)
    monkeypatch.setattr(time, "monotonic", lambda: 500)
    assert agent_events._pending_fresh({"pending_at": 1000, "monotonic_at": 400})
    assert not agent_events._pending_fresh({"pending_at": 1000, "monotonic_at": 300})
    assert not agent_events._pending_fresh({"pending_at": 1000})


def test_unwritable_observation_cache_does_not_block_native_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / ".codex"
    agents.install_hooks("codex", home)
    monkeypatch.setattr(
        agents, "_write_settings", Mock(side_effect=PermissionError("read-only cache"))
    )
    payload = {"session_id": "native", "cwd": str(tmp_path), "hook_event_name": "SessionStart"}
    assert agent_events.handle_codex(payload, home)
    agent_events.handle_codex(
        {
            **payload,
            "hook_event_name": "UserPromptSubmit",
            "prompt": "keep working",
            "turn_id": "one",
        },
        home,
    )
    monkeypatch.setattr(
        hooks, "turn_stopped", lambda *args, **kwargs: team.StopDecision("continue", cursor=1)
    )
    output = agent_events.handle_codex(
        {**payload, "hook_event_name": "Stop", "turn_id": "one"}, home
    )
    assert output and json.loads(output)["decision"] == "block"
    with store_session() as store:
        assert store.get_session(agent_events.session_key("codex", home, "native"))
        assert store.recent_prompts(team_project().id)[0].text == "keep working"
    assert agents.integration_readiness("codex", home)[0] == "unverified"


def test_native_metadata_expires_without_removing_active_launches_or_the_outbox() -> None:
    with store_session() as store:
        for launch, seen in [("old", 1), ("active", time.time())]:
            store.set_meta(
                f"native-launch:{launch}", json.dumps({"seen_at": seen, "project_id": "fixture"})
            )
        for launch in ("old", "active", "legacy"):
            store.set_meta(f"native-event:{launch}:event", "1")
            store.set_meta(f"native-provider:{launch}:thread", "provider")
        store.set_meta("unrelated", "keep")
        assert outbox.enqueue(
            {"v": insights.RECORD_VERSION, "kind": "native_event", "run_key": "old"}
        )
        assert store.expire_native_launches(time.time() - native_telemetry.NATIVE_METADATA_TTL) == 5
        assert len(store.list_meta("native-")) == 3
        assert store.get_meta("native-event:active:event") == "1"
        assert store.clear_native_launch("active") == 3
        assert not store.list_meta("native-") and store.get_meta("unrelated") == "keep"
    assert len(outbox.pending()) == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_native_capture_recovers_after_config_permissions_are_repaired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_config()
    config.explainability.enabled = config.explainability.ship = True
    target = save_config(config)
    target.chmod(0o000)
    original_open = Path.open

    def checked_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path == target and not path.stat().st_mode & 0o444:
            raise PermissionError("configuration is unreadable")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", checked_open)
    payload = {"resourceLogs": [{"scopeLogs": [{"logRecords": [{"eventName": "codex.event"}]}]}]}
    assert native_telemetry.capture(payload, "permissions") == 0
    target.chmod(0o600)
    assert native_telemetry.capture(payload, "permissions") == 1


def test_project_purge_removes_unjoined_native_launch_metadata_and_preserves_other_projects(
    tmp_path: Path,
) -> None:
    project = team_project(tmp_path)
    other = team_project(tmp_path / "other")
    with store_session() as store:
        for current in (project, other):
            store.ensure_project(current)
            store.set_meta(
                f"native-launch:{current.id}",
                json.dumps({"seen_at": time.time(), "project_id": current.id}),
            )
            store.set_meta(f"native-event:{current.id}:digest", "1")
            store.set_meta(f"native-provider:{current.id}:thread", "provider")
        store.purge_project(project.id)
        assert len(store.list_meta("native-")) == 3
        assert store.get_meta(f"native-event:{other.id}:digest") == "1"


@pytest.mark.parametrize("bad", ["unknown", "1234.5", -1, True, {}, float("inf")])
def test_malformed_native_counts_do_not_poison_shipping_or_later_runs(
    bad: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure()
    fake = FakeSDK()
    llm = MagicMock()
    monkeypatch.setattr(fake, "LLMCallTracer", llm, raising=False)
    monkeypatch.setattr(explainability, "sdk_available", lambda: True)
    monkeypatch.setattr(explainability, "_init_sdk", lambda settings, key: fake)
    for run, tokens in [("poisoned", bad), ("later", "19")]:
        outbox.enqueue(
            {
                "v": insights.RECORD_VERSION,
                "kind": "native_event",
                "run_key": run,
                "native": {"input_token_count": tokens, "output_token_count": "3"},
            }
        )
    report = explainability.ship_once()
    assert report.sent == 2 and report.deferred == 0 and len(fake.runs) == 2
    calls = llm.return_value.__enter__.return_value.set_token_counts.call_args_list
    assert {call.kwargs["prompt"] for call in calls} == {0, 19}
    assert all(call.kwargs["completion"] == 3 for call in calls)
    assert not outbox.pending()
