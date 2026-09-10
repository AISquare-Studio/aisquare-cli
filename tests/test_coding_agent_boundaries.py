"""Agent settings stay scoped to their owner across launch, hooks and telemetry."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.cli.ui.views.welcome import presence_lines
from aisquare.core import agents, harness, paths
from aisquare.core.agent_adapters import get_adapter
from aisquare.core.agent_adapters.types import config_home
from aisquare.core.config import FleetRoleSettings, RoleLaunchProfile, load_config, save_config
from aisquare.core.orchestrator import team_project
from aisquare.core.store import store_session
from aisquare.models import TeamSession
from aisquare.services import agent_launch, fleet, mcp_server, native_telemetry
from tests.test_fleet_service import FakeTmux


@pytest.mark.parametrize("role", ["coder", "reviewer", "reviewer2"])
def test_saved_codex_permissions_do_not_prevent_a_claude_spawn(
    role: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_config()
    config.fleet.roles[harness.base_role(role)] = FleetRoleSettings(
        permission_mode="plan", sandbox="read-only", approval_policy="on-request", worktree=False
    )
    save_config(config)
    tmux = FakeTmux()
    monkeypatch.setattr(fleet, "server", lambda config: tmux)
    monkeypatch.setattr(agent_launch, "executable", lambda selected: "/fixture/agent")
    project = team_project(tmp_path)
    receipt = fleet.spawn(project, role, agent="claude-code")
    assert receipt.agent.agent == "claude-code"
    command = tmux.spawned[-1]["command"]
    assert isinstance(command, list)
    assert command[command.index("--permission-mode") + 1] == "plan"
    assert "--sandbox" not in command and "--ask-for-approval" not in command
    with pytest.raises(fleet.FleetError, match="Claude Code uses"):
        fleet.spawn(project, role, agent="claude-code", sandbox="read-only")


@pytest.mark.parametrize("role", ["reviewer", "reviewer2", "reviewer12"])
def test_numbered_reviewers_inherit_read_only_defaults(role: str) -> None:
    adapter = get_adapter("codex")
    assert adapter.fleet_args(role, "seat", None) == ["--sandbox", "read-only"]
    config = load_config()
    config.fleet.roles["reviewer"].sandbox = "read-only"
    config.fleet.roles["reviewer"].approval_policy = "on-request"
    assert fleet.role_settings(role, config.fleet) == config.fleet.roles["reviewer"]
    config.fleet.roles[role] = FleetRoleSettings(sandbox="workspace-write")
    assert fleet.role_settings(role, config.fleet).sandbox == "workspace-write"


def test_mcp_tries_both_bindings_before_refusing_a_local_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = team_project(tmp_path)
    now = datetime.now(UTC)
    monkeypatch.setenv("AISQUARE_LAUNCH_ID", "pending-launch")
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", "bound-fleet")
    with store_session() as store:
        store.ensure_project(project)
        store.upsert_session(
            TeamSession(
                id="native-board-session",
                project_id=project.id,
                role="coder",
                started_at=now,
                last_seen_at=now,
            )
        )
        store.set_meta("fleet-session:bound-fleet", "native-board-session")
    assert mcp_server.client_session_id(project.id) == "native-board-session"
    with pytest.raises(ValueError, match="has not joined"):
        mcp_server.client_session_id("prj_another")
    monkeypatch.delenv("AISQUARE_FLEET_AGENT")
    with pytest.raises(ValueError, match="has not joined"):
        mcp_server.client_session_id(project.id)
    monkeypatch.delenv("AISQUARE_LAUNCH_ID")
    assert mcp_server.client_session_id(project.id).startswith("mcp:")


@pytest.mark.parametrize("damage", ["invalid-utf8", "unreadable"])
def test_codex_instructions_cannot_break_claude_detection(
    damage: str, isolated_agent_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = isolated_agent_home / ".codex"
    directory.mkdir(parents=True)
    path = directory / "AGENTS.md"
    path.write_bytes(b"global instructions: \xff")
    if damage == "unreadable":
        read = Path.read_text

        def inaccessible(self: Path, *args: object, **kwargs: object) -> str:
            if self == path:
                raise PermissionError("fixture refuses this file")
            return read(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "read_text", inaccessible)
    assert agents.detect("claude-code") is not None
    assert not agents.hooks_installed("claude-code")
    assert agents.install_hooks("claude-code")
    assert agents.remove_hooks("claude-code")
    assert agents.context_files("codex") == ([path] if damage == "invalid-utf8" else [])


def test_codex_context_precedence_is_evaluated_only_when_ingesting(tmp_path: Path) -> None:
    adapter = get_adapter("codex")
    override, normal = adapter.context_files(tmp_path)
    normal.write_text("normal instructions")
    override.write_text(" \n")
    assert agents.context_files("codex", tmp_path) == [normal]
    override.write_text("override instructions")
    assert agents.context_files("codex", tmp_path) == [override]


@pytest.mark.parametrize("agent", ["claude-code", "codex"])
def test_agent_detection_and_launch_share_the_default_home(
    agent: str, isolated_agent_home: Path
) -> None:
    selected = agent_launch.resolve(agent=agent)
    expected = isolated_agent_home / selected.adapter.home_name
    assert selected.config_dir == expected
    assert agents.ambient_hook_dir(agent) == expected


@pytest.mark.parametrize("agent", ["claude-code", "codex"])
@pytest.mark.parametrize("value", [" /some/account ", "/some/account\n", " \t\n"])
def test_config_home_normalizes_only_environment_whitespace(
    agent: str, value: str, tmp_path: Path
) -> None:
    adapter = get_adapter(agent)
    expected = Path(value.strip()) if value.strip() else tmp_path / adapter.home_name
    assert config_home(adapter, tmp_path, {adapter.home_env: value}) == expected
    explicit = tmp_path / " intentional space "
    assert config_home(adapter, tmp_path, {adapter.home_env: value}, explicit) == explicit


def test_parent_agent_environment_is_cleared_by_the_shared_fixture() -> None:
    for key in (
        "AISQUARE_CODING_AGENT",
        "AISQUARE_LAUNCH_ID",
        "AISQUARE_FLEET_AGENT",
        "CODEX_HOME",
    ):
        assert key not in os.environ


@pytest.mark.parametrize("source", ["user", "project", "inherited", "default"])
def test_unknown_wrappers_require_a_family_before_model_flags(
    source: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if source == "user":
        agent_launch.use("codex")
    elif source == "project":
        agent_launch.use("codex", project=True, cwd=tmp_path)
    elif source == "inherited":
        monkeypatch.setenv("AISQUARE_CODING_AGENT", "codex")
    with pytest.raises(ValueError, match="pass --agent"):
        agent_launch.resolve(binary="/fixture/claude-work", cwd=tmp_path)
    selected = agent_launch.resolve(
        agent="claude-code", binary="/fixture/claude-work", cwd=tmp_path
    )
    assert selected.adapter.id == "claude-code"
    assert agent_launch.native_model_args(selected, "coder", []) == []


@pytest.mark.parametrize("source", ["config", "environment", "binary-conflict"])
def test_welcome_displays_selection_errors(source: str, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_config()
    if source == "config":
        config.agents.default = "future-agent"
    elif source == "environment":
        monkeypatch.setenv("AISQUARE_CODING_AGENT", "Codex")
    else:
        config.team.profiles["coder"] = RoleLaunchProfile(agent="claude-code", bin="codex")
    save_config(config)
    message = presence_lines().plain
    assert "coding agent:" in message and "Settings" in message
    assert "tmux" in message and "gh" in message


def test_damaged_config_keeps_codex_launch_native_and_clears_parent_tracing(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths.config_path().parent.mkdir(parents=True, exist_ok=True)
    paths.config_path().write_text("broken config = [")
    for key, value in {
        "AISQUARE_PIPELINE_ID": "parent-run",
        "AISQUARE_TRACE_AGENT_NAME": "parent-agent",
        "ANTHROPIC_BASE_URL": "http://parent.invalid",
        "ANTHROPIC_CUSTOM_HEADERS": "X-Pipeline-Id: parent-run",
        "AISQUARE_MODEL_CODER": "explicit-native-model",
    }.items():
        monkeypatch.setenv(key, value)
    execute = Mock()
    monkeypatch.setattr(import_module("aisquare.cli.launch"), "_exec", execute)
    monkeypatch.setattr(agent_launch, "executable", lambda selected: "/fixture/codex")
    result = runner.invoke(app, ["launch", "coder", "--agent", "codex"])
    assert result.exit_code == 0, result.output
    _, argv, env = execute.call_args.args
    assert argv[argv.index("--model") + 1] == "explicit-native-model"
    assert "config unreadable" in result.output
    assert not any(
        key in env
        for key in (
            "AISQUARE_PIPELINE_ID",
            "AISQUARE_TRACE_AGENT_NAME",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_CUSTOM_HEADERS",
        )
    )
    claude = agent_launch.resolve(agent="claude-code")
    assert agent_launch.telemetry_args(claude, {}) == ([], "")


def test_probe_cache_ignores_session_churn_but_separates_logins_and_providers(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "account"
    directory.mkdir()
    native_identity = directory / ".claude.json"
    native_identity.write_text(
        json.dumps(
            {"oauthAccount": {"emailAddress": "first@example.invalid", "accountUuid": "one"}}
        )
    )
    binary = tmp_path / "claude"
    binary.write_text("fixture executable")
    context = harness.ProbeContext(str(binary), {"CLAUDE_CONFIG_DIR": str(directory)})
    token = harness._PROBE_CONTEXT.set(context)
    try:
        before = harness.account_scope()
        harness._save_cache(
            {
                "fable": harness.ProbeResult(
                    alias="fable",
                    available=True,
                    resolved_id="claude-fable-5",
                    checked_at=datetime.now(UTC),
                )
            }
        )
        for key in (
            "CLAUDE_CODE_SESSION_ID",
            "CLAUDE_PID",
            "CLAUDE_CODE_MESSAGING_SOCKET",
            "CLAUDE_CODE_MESSAGING_TOKEN",
        ):
            context.env[key] = "another-parent"
        (directory / ".credentials.json").write_text('{"refreshed": true}')
        (directory / "settings.json").write_text('{"hooks": {"Stop": []}}')
        assert harness.account_scope() == before
        assert harness.cached_probe("fable") is not None
        context.env["ANTHROPIC_BASE_URL"] = "http://other-provider.invalid"
        assert harness.account_scope() != before
        context.env.pop("ANTHROPIC_BASE_URL")
        native_identity.write_text(
            json.dumps(
                {"oauthAccount": {"emailAddress": "second@example.invalid", "accountUuid": "two"}}
            )
        )
        assert harness.account_scope() != before
        assert harness.cached_probe("fable") is None
    finally:
        harness._PROBE_CONTEXT.reset(token)


def test_refresh_clears_all_probe_scopes_and_only_probe_scopes() -> None:
    directory = paths.aisquare_home() / "cache"
    directory.mkdir(parents=True)
    for name in (
        "harness_models.old.json",
        "harness_models.default.json",
        "harness_models.json",
        "agent-hooks-keep.json",
    ):
        (directory / name).write_text("{}")
    harness.clear_probe_cache()
    assert [path.name for path in directory.iterdir()] == ["agent-hooks-keep.json"]


def test_reconnect_preserves_every_owned_timeout_and_trust_observation(tmp_path: Path) -> None:
    agents.install_hooks("codex", tmp_path)
    path = tmp_path / "hooks.json"
    payload = json.loads(path.read_text())
    for groups in payload["hooks"].values():
        groups[0]["hooks"][0]["timeout"] = 180
    path.write_text(json.dumps(payload, indent=2) + "\n")
    # Normalize once, then verify another reconnect writes nothing.
    agents.install_hooks("codex", tmp_path)
    agents.observe_hooks("codex", tmp_path)
    before, stamp = path.read_bytes(), path.stat().st_mtime_ns
    agents.install_hooks("codex", tmp_path)
    assert path.read_bytes() == before and path.stat().st_mtime_ns == stamp
    assert all(
        groups[0]["hooks"][0]["timeout"] == 180 for groups in json.loads(before)["hooks"].values()
    )
    assert agents.integration_readiness("codex", tmp_path)[0] == "observed"


@pytest.mark.parametrize("mixed", [False, True])
def test_other_handlers_do_not_reset_aisquare_readiness(tmp_path: Path, mixed: bool) -> None:
    agents.install_hooks("codex", tmp_path)
    agents.observe_hooks("codex", tmp_path)
    path = tmp_path / "hooks.json"
    payload = json.loads(path.read_text())
    foreign = {"type": "command", "command": "my-own-notifier"}
    if mixed:
        payload["hooks"]["Stop"][0]["hooks"].append(foreign)
    else:
        payload["hooks"]["Stop"].append({"hooks": [foreign]})
    path.write_text(json.dumps(payload))
    assert agents.integration_readiness("codex", tmp_path)[0] == "observed"
    payload["hooks"]["Stop"][0]["hooks"][0]["timeout"] = 90
    path.write_text(json.dumps(payload))
    assert agents.integration_readiness("codex", tmp_path)[0] == "unverified"


@pytest.mark.parametrize("agent", ["claude-code", "codex"])
@pytest.mark.parametrize("action", ["connect", "disconnect"])
def test_invalid_settings_are_preserved_with_an_actionable_cli_error(
    agent: str, action: str, tmp_path: Path, runner: CliRunner
) -> None:
    path = tmp_path / get_adapter(agent).settings_name
    original = '{"hooks": {},}'
    path.write_text(original)
    result = runner.invoke(app, ["--json", "agents", action, agent, "--config-dir", str(tmp_path)])
    assert result.exit_code == 1, result.output
    assert "agent_configuration" in result.output and str(path) in result.output
    assert "repair" in result.output.lower()
    assert path.read_text() == original


@pytest.mark.parametrize("agent", ["claude-code", "codex"])
def test_empty_native_settings_can_be_connected(agent: str, tmp_path: Path) -> None:
    (tmp_path / get_adapter(agent).settings_name).touch()
    assert agents.install_hooks(agent, tmp_path)
    assert agents.hooks_installed(agent, tmp_path)


@pytest.mark.parametrize(
    "args,configured",
    [
        (["--sandbox", "read-only", "--", "refactor hotel.py"], False),
        (["refactor motel.py and otel.io"], False),
        (["--", "-c", 'otel.exporter="none"'], False),
        (["-c", 'otel.exporter="none"'], True),
        (["--config", 'otel = {exporter="none"}'], True),
        (['--config=otel.exporter="none"'], True),
        (['-cotel.exporter="none"'], True),
        (["-c", 'model="hotel.py"'], False),
    ],
)
def test_telemetry_recognizes_only_native_exporter_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str], configured: bool
) -> None:
    monkeypatch.setattr(native_telemetry, "SYSTEM_CONFIG", tmp_path / "system.toml")
    assert native_telemetry.operator_configured(tmp_path, args) is configured


def test_exporters_are_scoped_to_the_effective_home_and_selected_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(native_telemetry, "SYSTEM_CONFIG", tmp_path / "system.toml")
    ancestor = tmp_path / ".codex"
    ancestor.mkdir()
    (ancestor / "config.toml").write_text('[otel]\nexporter="none"\n')
    repo = tmp_path / "project"
    (repo / ".git").mkdir(parents=True)
    (repo / ".codex").mkdir()
    (repo / ".codex" / "config.toml").write_text('[otel]\nexporter="none"\n')
    monkeypatch.chdir(repo)
    home = tmp_path / "other-account"
    home.mkdir()
    (home / "unused.config.toml").write_text('[otel]\nexporter="none"\n')
    assert not native_telemetry.operator_configured(home, [])
    assert not native_telemetry.operator_configured(home, ["--", "--profile", "unused"])
    assert native_telemetry.operator_configured(home, ["--profile", "unused"])
    assert native_telemetry.operator_configured(home, ["-punused"])
    (home / "config.toml").write_text('profile="unused"\n')
    assert native_telemetry.operator_configured(home, [])
    assert not native_telemetry.operator_configured(home, ["--profile", "clean"])
    (home / "config.toml").write_text('[otel]\nexporter="none"\n')
    assert native_telemetry.operator_configured(home, [])
