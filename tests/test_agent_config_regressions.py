"""Launch and account regressions exercised at their consuming boundaries."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import agents, claude_accounts, harness
from aisquare.core.config import FleetRoleSettings, RoleLaunchProfile, load_config, save_config
from aisquare.core.orchestrator import team_project
from aisquare.core.store import store_session
from aisquare.models import TeamSession
from aisquare.services import agent_launch, fleet, mcp_server, native_telemetry, team
from aisquare.services import claude_accounts as accounts_service
from tests.test_fleet_service import FakeTmux


@pytest.mark.parametrize("binary_source", ["command", "role-env", "global-env", "config"])
@pytest.mark.parametrize("family_source", ["user", "project", "inherited"])
@pytest.mark.parametrize("family", ["claude-code", "codex"])
def test_wrapper_launch_respects_explicit_family_defaults(
    binary_source: str,
    family_source: str,
    family: str,
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    wrapper = "/fixture/coding-wrapper"
    args = ["launch", "coder"]
    if binary_source == "command":
        args += ["--command", wrapper]
    elif binary_source == "role-env":
        monkeypatch.setenv("AISQUARE_BIN_CODER", wrapper)
    elif binary_source == "global-env":
        monkeypatch.setenv("AISQUARE_AGENT_BIN", wrapper)
    else:
        config = load_config()
        config.team.profiles["coder"] = RoleLaunchProfile(bin=wrapper)
        save_config(config)
    if family_source == "inherited":
        monkeypatch.setenv("AISQUARE_CODING_AGENT", family)
    else:
        result = runner.invoke(
            app, ["agents", "use", family, *(["--project"] if family_source == "project" else [])]
        )
        assert result.exit_code == 0, result.output
    monkeypatch.setenv("AISQUARE_MODEL_CODER", "fixture-native-model")
    execute = Mock()
    monkeypatch.setattr(import_module("aisquare.cli.launch"), "_exec", execute)
    monkeypatch.setattr(agent_launch, "executable", lambda selected: wrapper)
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    binary, argv, env = execute.call_args.args
    assert binary == wrapper and env["AISQUARE_CODING_AGENT"] == family
    if family == "codex":
        assert argv[argv.index("--model") + 1] == "fixture-native-model"
    else:
        assert "--model" not in argv
    status = runner.invoke(app, ["--json", "team", "harness"])
    assert status.exit_code == 0, status.output
    rows = json.loads(status.stdout)["roles"]
    assert next(row for row in rows if row["role"] == "coder")["agent"] == family


@pytest.mark.parametrize("role", ["coder2", "reviewer2"])
@pytest.mark.parametrize("family", ["claude-code", "codex"])
def test_numbered_seats_do_not_inherit_base_worktrees_or_claude_flags(
    role: str,
    family: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_config()
    config.fleet.roles[harness.base_role(role)] = FleetRoleSettings(
        worktree=True,
        permission_mode="bypassPermissions",
        extra_args=["--restricted"],
        sandbox="read-only",
        approval_policy="on-request",
    )
    save_config(config)
    tmux = FakeTmux()
    monkeypatch.setattr(fleet, "server", lambda config: tmux)
    monkeypatch.setattr(agent_launch, "executable", lambda selected: "/fixture/agent")
    receipt = fleet.spawn(team_project(tmp_path), role, agent=family)
    assert receipt.agent.cwd == tmp_path
    command = tmux.spawned[-1]["command"]
    assert isinstance(command, list)
    assert "--restricted" not in command
    assert "bypassPermissions" not in command
    if family == "claude-code":
        assert command[command.index("--permission-mode") + 1] == "auto"
    else:
        assert command[command.index("--sandbox") + 1] == "read-only"
        assert command[command.index("--ask-for-approval") + 1] == "on-request"


@pytest.mark.parametrize("family,filename", [("claude-code", "CLAUDE.md"), ("codex", "AGENTS.md")])
def test_unreadable_instructions_leave_a_note_and_still_install_hooks(
    family: str,
    filename: str,
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unreadable = tmp_path / filename
    unreadable.touch()
    read = Path.read_text

    def read_text(path: Path, *args: object, **kwargs: object) -> str:
        if path == unreadable:
            raise PermissionError("fixture: permission denied")
        return read(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", read_text)
    result = runner.invoke(
        app, ["--json", "agents", "connect", family, "--config-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    connection = json.loads(result.stdout)
    assert connection["hooks_installed"] and connection["imported"] == 0
    assert str(unreadable) in connection["detail"] and "Skipped context" in connection["detail"]
    assert agents.hooks_installed(family, tmp_path)
    if family == "claude-code":
        account = claude_accounts.default_account({"CLAUDE_CONFIG_DIR": str(tmp_path)})
        assert accounts_service.complete_sign_in(account).hooks_installed


@pytest.mark.parametrize("timeout", [0, -1, 180])
def test_claude_hooks_without_a_spec_timeout_keep_the_native_default(
    timeout: int,
    tmp_path: Path,
) -> None:
    agents.install_hooks("claude-code", tmp_path)
    path = tmp_path / "settings.json"
    payload = json.loads(path.read_text())
    events = ("Stop", "SessionEnd", "Notification")
    for event in events:
        payload["hooks"][event][0]["hooks"][0]["timeout"] = timeout
    path.write_text(json.dumps(payload))
    agents.install_hooks("claude-code", tmp_path)
    actual = json.loads(path.read_text())
    assert all("timeout" not in actual["hooks"][event][0]["hooks"][0] for event in events)


def test_group_annotations_do_not_reset_readiness_but_execution_matchers_do(tmp_path: Path) -> None:
    agents.install_hooks("codex", tmp_path)
    agents.observe_hooks("codex", tmp_path)
    path = tmp_path / "hooks.json"
    payload = json.loads(path.read_text())
    payload["hooks"]["Stop"][0]["description"] = "reviewed by operator"
    path.write_text(json.dumps(payload))
    assert agents.integration_readiness("codex", tmp_path)[0] == "observed"
    payload["hooks"]["PreToolUse"][0]["matcher"] = "another_tool"
    path.write_text(json.dumps(payload))
    assert agents.integration_readiness("codex", tmp_path)[0] == "unverified"
    assert agents.remove_hooks("codex", tmp_path)
    assert not json.loads(path.read_text()).get("hooks")


@pytest.mark.parametrize("home_value", [None, " \t\n", " /fixture/codex \n"])
def test_codex_hook_runner_uses_the_same_home_as_detection(
    home_value: str | None,
    isolated_agent_home: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if home_value is not None:
        monkeypatch.setenv("CODEX_HOME", home_value)
    handle = Mock(return_value="")
    monkeypatch.setattr("aisquare.services.agent_events.handle_codex", handle)
    result = runner.invoke(app, ["hook", "codex"], input="{}")
    assert result.exit_code == 0, result.output
    assert handle.call_args.args[1] == agents.ambient_hook_dir("codex")
    if not home_value or not home_value.strip():
        assert handle.call_args.args[1] == isolated_agent_home / ".codex"
    explicit = isolated_agent_home / " deliberate spaces "
    runner.invoke(app, ["hook", "codex", "--config-dir", str(explicit)], input="{}")
    assert handle.call_args.args[1] == explicit


def test_mcp_tools_work_before_trust_and_switch_to_the_joined_native_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    project = team.activate()
    monkeypatch.setenv("AISQUARE_LAUNCH_ID", "pending-native-launch")
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", "inherited-fleet")
    monkeypatch.setenv("AISQUARE_SERVE_CLIENT", "codex")
    virtual = mcp_server._ensure_virtual_session()
    assert virtual.startswith("mcp:codex:")
    with store_session() as store:
        session = store.get_session(virtual)
        assert session is not None and session.project_id == project.id
        store.upsert_session(
            TeamSession(
                id="joined-native-session",
                project_id=project.id,
                role="coder",
                started_at=datetime.now(UTC),
                last_seen_at=datetime.now(UTC),
            )
        )
        store.set_meta("launch-session:pending-native-launch", "joined-native-session")
    assert mcp_server._ensure_virtual_session() == "joined-native-session"


def test_account_readers_share_the_effective_environment_and_home(
    isolated_agent_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolated_agent_home.mkdir()
    (isolated_agent_home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": "default@example.invalid"}})
    )
    role_home = tmp_path / "role-account"
    role_home.mkdir()
    (role_home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": "role@example.invalid"}})
    )
    role_env = {"CLAUDE_CONFIG_DIR": str(role_home)}
    account = claude_accounts.default_account(role_env)
    for env in (None, role_env):
        identity = claude_accounts.identity(account, env=env)
        assert identity is not None and identity.email == "role@example.invalid"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "unrelated-shell-account"))
    default = claude_accounts.default_account({})
    identity = claude_accounts.identity(default, env={})
    assert identity is not None and identity.email == "default@example.invalid"
    context = harness.ProbeContext("/fixture/claude", role_env)
    token = harness._PROBE_CONTEXT.set(context)
    try:
        before = harness.account_scope()
        (role_home / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": "new-role@example.invalid"}})
        )
        assert harness.account_scope() != before
    finally:
        harness._PROBE_CONTEXT.reset(token)


@pytest.mark.parametrize("field", ["subscriptionType", "rateLimitTier"])
def test_probe_cache_tracks_entitlements_without_tracking_token_refreshes(
    field: str,
    tmp_path: Path,
) -> None:
    credentials: dict[str, object] = {
        "accessToken": "fixture-only",
        "subscriptionType": "pro",
        "rateLimitTier": "pro",
    }
    path = tmp_path / ".credentials.json"
    path.write_text(json.dumps({"claudeAiOauth": credentials}))
    token = harness._PROBE_CONTEXT.set(
        harness.ProbeContext("/fixture/claude", {"CLAUDE_CONFIG_DIR": str(tmp_path)})
    )
    try:
        before = harness.account_scope()
        credentials.update(accessToken="rotated-fixture", expiresAt=2_000_000_000_000)
        path.write_text(json.dumps({"claudeAiOauth": credentials}))
        assert harness.account_scope() == before
        credentials[field] = "max"
        path.write_text(json.dumps({"claudeAiOauth": credentials}))
        assert harness.account_scope() != before
    finally:
        harness._PROBE_CONTEXT.reset(token)


@pytest.mark.parametrize(
    "settings",
    [
        {"env": {"ANTHROPIC_API_KEY": "fixture-key"}},
        {"env": {"ANTHROPIC_BASE_URL": "https://provider.invalid"}},
        {"apiKeyHelper": "fixture-credential-helper"},
    ],
)
def test_provider_settings_in_native_config_separate_probe_scopes(
    settings: dict[str, object],
    tmp_path: Path,
) -> None:
    token = harness._PROBE_CONTEXT.set(
        harness.ProbeContext("/fixture/claude", {"CLAUDE_CONFIG_DIR": str(tmp_path)})
    )
    try:
        before = harness.account_scope()
        (tmp_path / "settings.json").write_text(json.dumps(settings))
        assert harness.account_scope() != before
    finally:
        harness._PROBE_CONTEXT.reset(token)


@pytest.mark.parametrize(
    "args",
    [
        ["-c", "=x"],
        ["-c", "a b=1"],
        ["-c", "profile.name=x"],
        ["-p", "../evil"],
        ["--profile=sub/dir"],
        ["exec", "--json", "-please fix src/foo.py"],
        ["exec", "--json", "-print the plan"],
    ],
)
def test_native_launch_does_not_disable_tracing_for_prompts_or_unusable_config_options(
    args: list[str],
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_config()
    config.explainability.enabled = True
    config.explainability.ship = True
    save_config(config)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(native_telemetry, "SYSTEM_CONFIG", tmp_path / "system.toml")
    monkeypatch.setattr(agent_launch, "executable", lambda selected: "/fixture/codex")
    start = Mock(return_value=(["-c", "fixture.receiver=true"], "fixture receiver started"))
    monkeypatch.setattr(native_telemetry, "start", start)
    execute = Mock()
    monkeypatch.setattr(import_module("aisquare.cli.launch"), "_exec", execute)
    result = runner.invoke(app, ["launch", "coder", "--agent", "codex", "--", *args])
    assert result.exit_code == 0, result.output
    assert start.call_count == 1
    assert "fixture receiver started" in result.output
    assert "without native telemetry" not in result.output
    assert execute.call_args.args[1][-len(args) :] == args


@pytest.mark.parametrize("layer", ["system", "user", "profile"])
@pytest.mark.parametrize("damage", ["malformed", "directory", "permission", "invalid-utf8"])
def test_unusable_native_config_layers_do_not_disable_tracing(
    layer: str,
    damage: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = tmp_path / "system.toml"
    monkeypatch.setattr(native_telemetry, "SYSTEM_CONFIG", system)
    path = {
        "system": system,
        "user": tmp_path / "config.toml",
        "profile": tmp_path / "work.config.toml",
    }[layer]
    if damage == "directory":
        path.mkdir()
    elif damage == "permission":
        open_file = Path.open

        def open_path(candidate: Path, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            if candidate == path:
                raise PermissionError("fixture: unreadable configuration")
            return open_file(candidate, *args, **kwargs)  # type: ignore[call-overload]

        monkeypatch.setattr(Path, "open", open_path)
    else:
        path.write_bytes(b"not toml =" if damage == "malformed" else b"\xff")
    assert not native_telemetry.operator_configured(tmp_path, ["--profile", "work"])


def test_non_directory_home_and_table_profile_do_not_disable_tracing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(native_telemetry, "SYSTEM_CONFIG", tmp_path / "system.toml")
    home_file = tmp_path / "home-file"
    home_file.touch()
    assert not native_telemetry.operator_configured(home_file, [])
    (tmp_path / "config.toml").write_text('[profile]\nname="work"\n')
    assert not native_telemetry.operator_configured(tmp_path, [])


@pytest.mark.parametrize(
    "selection", [[], ["-p", "work"], ["--profile=work"], ["-c", "profile=work"]]
)
def test_profile_exporter_overrides_follow_the_selected_profile(
    selection: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(native_telemetry, "SYSTEM_CONFIG", tmp_path / "system.toml")
    overrides = [
        "-c",
        'profiles.work.otel.exporter="none"',
        "--config",
        "profiles.work.model=fixture-model",
    ]
    assert native_telemetry.operator_configured(tmp_path, [*overrides, *selection]) is bool(
        selection
    )
    (tmp_path / "config.toml").write_text('profile="work"\n')
    assert native_telemetry.operator_configured(tmp_path, overrides)
    assert not native_telemetry.operator_configured(tmp_path, [*overrides, "-p", "clean"])
