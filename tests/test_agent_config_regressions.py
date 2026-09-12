"""Launch and account regressions exercised at their consuming boundaries."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from click import unstyle
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import agents, claude_accounts, harness
from aisquare.core.config import FleetRoleSettings, RoleLaunchProfile, load_config, save_config
from aisquare.core.orchestrator import team_project
from aisquare.services import agent_launch, fleet, lifecycle, native_telemetry
from aisquare.services import claude_accounts as accounts_service
from tests.test_fleet_service import FakeTmux


@pytest.mark.parametrize("binary_source", ["command", "role-env", "global-env", "config"])
@pytest.mark.parametrize("family_source", ["user", "project", "inherited"])
@pytest.mark.parametrize("family", ["claude-code", "codex"])
def test_wrapper_launch_requires_a_declaration_even_with_family_defaults(
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
    assert result.exit_code != 0, result.output
    execute.assert_not_called()
    assert "--agent claude-code" in result.output and "--agent codex" in result.output
    declared = runner.invoke(app, ["team", "bind", "coder", "--agent", family, "--bin", wrapper])
    assert declared.exit_code == 0, declared.output
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    binary, argv, env = execute.call_args.args
    assert binary == wrapper and env["AISQUARE_CODING_AGENT"] == family
    if family == "codex":
        assert argv[argv.index("--model") + 1] == "fixture-native-model"
    else:
        assert "--model" not in argv
    monkeypatch.delenv("AISQUARE_AGENT_BIN", raising=False)
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
    read = Path.open

    def read_text(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path == unreadable:
            raise PermissionError("fixture: permission denied")
        return read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", read_text)
    result = runner.invoke(
        app, ["--json", "agents", "connect", family, "--config-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    connection = json.loads(result.stdout)
    assert connection["hooks_installed"] and connection["imported"] == 0
    assert str(unreadable) in connection["detail"] and "Skipped context" in connection["detail"]
    assert agents.hooks_installed(family, tmp_path)
    detected = agents.detect(family, tmp_path)
    assert detected is not None and unreadable in detected.config_paths
    assert str(unreadable) in detected.detail and "permission denied" in detected.detail


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
    agents.observe_hooks("codex", tmp_path, agents.hook_fingerprint("codex", tmp_path))
    path = tmp_path / "hooks.json"
    payload = json.loads(path.read_text())
    for key in ("description", "note", "comment"):
        payload["hooks"]["Stop"][0][key] = "reviewed by operator"
        payload["hooks"]["Stop"][0]["hooks"][0][key] = "reviewed command"
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
    else:
        monkeypatch.delenv("CODEX_HOME", raising=False)
    handle = Mock(return_value="")
    monkeypatch.setattr("aisquare.services.agent_events.handle_codex", handle)
    result = runner.invoke(app, ["hook", "codex"], input="{}")
    assert result.exit_code == 0, result.output
    assert handle.call_args.args[1] == agents.ambient_hook_dir("codex")
    if not home_value or not home_value.strip():
        assert handle.call_args.args[1] == isolated_agent_home / ".codex"
    empty = runner.invoke(app, ["hook", "codex", "--config-dir", ""], input="{}")
    assert empty.exit_code == 0, empty.output
    assert handle.call_args.args[1] == agents.ambient_hook_dir("codex")
    explicit = isolated_agent_home / " deliberate spaces "
    runner.invoke(app, ["hook", "codex", "--config-dir", str(explicit)], input="{}")
    assert handle.call_args.args[1] == explicit


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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claude_accounts, "keychain_platform", lambda: False)
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
        ["exec", "--json", "--", "-please fix src/foo.py"],
        ["exec", "--json", "--", "-print the plan"],
        ["-c", 'profile="work\\u0000"'],
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
def test_unusable_native_config_layers_stand_down_with_the_exact_path(
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
        path.write_text('[otel]\nexporter="none"\n')
        open_file = Path.open

        def open_path(candidate: Path, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            if candidate == path:
                raise PermissionError("fixture: unreadable configuration")
            return open_file(candidate, *args, **kwargs)  # type: ignore[call-overload]

        monkeypatch.setattr(Path, "open", open_path)
    else:
        path.write_bytes(b"not toml =" if damage == "malformed" else b"\xff")
    with pytest.raises(native_telemetry.NativeConfigError) as error:
        native_telemetry.operator_configured(tmp_path, ["--profile", "work"])
    assert str(path) in str(error.value) and layer in str(error.value)
    config = load_config()
    config.explainability.enabled = config.explainability.ship = True
    save_config(config)
    start = Mock()
    monkeypatch.setattr(native_telemetry, "start", start)
    selected = agent_launch.resolve(agent="codex", env_overrides={"CODEX_HOME": str(tmp_path)})
    injected, note = agent_launch.telemetry_args(selected, {}, ["--profile", "work"])
    assert injected == [] and str(path) in note and layer in note
    start.assert_not_called()


def test_non_directory_home_stands_down_and_table_profile_is_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(native_telemetry, "SYSTEM_CONFIG", tmp_path / "system.toml")
    home_file = tmp_path / "home-file"
    home_file.touch()
    with pytest.raises(native_telemetry.NativeConfigError, match="home-file"):
        native_telemetry.operator_configured(home_file, [])
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


def test_sign_in_installs_hooks_into_a_fresh_home_despite_unreadable_instructions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = claude_accounts.default_account({"CLAUDE_CONFIG_DIR": str(tmp_path)})
    instruction = tmp_path / "CLAUDE.md"
    instruction.touch()
    read = Path.open

    def read_text(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path == instruction:
            raise PermissionError("fixture: unreadable instructions")
        return read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", read_text)
    assert not accounts_service.describe(account).hooks_installed
    assert not (tmp_path / "settings.json").exists()
    completed = accounts_service.complete_sign_in(account)
    assert completed.hooks_installed and (tmp_path / "settings.json").exists()
    assert agents.hooks_installed("claude-code", tmp_path)
    for status in (completed, accounts_service.describe(account)):
        assert str(instruction) in status.detail and "unreadable instructions" in status.detail


@pytest.mark.parametrize(
    "family,filename,home_env",
    [
        ("claude-code", "CLAUDE.md", "CLAUDE_CONFIG_DIR"),
        ("codex", "AGENTS.override.md", "CODEX_HOME"),
    ],
)
def test_list_and_init_keep_failed_context_paths_and_explanations(
    family: str,
    filename: str,
    home_env: str,
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad = tmp_path / filename
    bad.mkdir()
    monkeypatch.setenv(home_env, str(tmp_path))
    if family == "codex":
        (tmp_path / "AGENTS.md").write_text("Usable fallback instructions")
    listed = runner.invoke(app, ["--json", "agents", "list"])
    assert listed.exit_code == 0, listed.output
    row = next(item for item in json.loads(listed.output) if item["name"] == family)
    assert str(bad) in row["config_paths"] and str(bad) in row["detail"]
    assert "not a regular file" in row["detail"]
    report = lifecycle.initialize(
        tmp_path,
        api_key=None,
        local=True,
        agents=[family],
        onboard=False,
        reinit=False,
        assume_yes=True,
        explainability=False,
    )
    assert agents.hooks_installed(family, tmp_path)
    assert any(str(bad) in note and "not a regular file" in note for note in report.notes)


@pytest.mark.skipif(os.name == "nt", reason="POSIX special file fixtures")
@pytest.mark.parametrize("kind", ["fifo", "device", "regular-symlink"])
def test_context_inspection_never_opens_special_files(kind: str, tmp_path: Path) -> None:
    instruction = tmp_path / "AGENTS.md"
    if kind == "fifo":
        os.mkfifo(instruction)
    elif kind == "device":
        instruction.symlink_to(os.devnull)
    else:
        target = tmp_path / "real.md"
        target.write_text("Readable instructions")
        instruction.symlink_to(target)
    code = """
import json, sys
from pathlib import Path
from typing import Any
from aisquare.core import agents
from aisquare.services.agents import connect
home = Path(sys.argv[1])
info = agents.detect("codex", home)
connection = connect("codex", home)
print(json.dumps({
    "info": info.model_dump(mode="json"),
    "connected": connection.model_dump(mode="json"),
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    payload = json.loads(result.stdout)
    assert str(instruction) in payload["info"]["config_paths"]
    assert payload["connected"]["hooks_installed"]
    if kind == "regular-symlink":
        assert payload["connected"]["imported"] > 0
    else:
        assert payload["connected"]["imported"] == 0
        assert "not a regular file" in payload["info"]["detail"]


@pytest.mark.parametrize(
    "level,key,value",
    [
        ("group", "enabled", False),
        ("group", "future_control", "disabled"),
        ("handler", "timeout", 99),
        ("handler", "future_control", "disabled"),
    ],
)
def test_hook_execution_changes_invalidate_observed_readiness(
    level: str,
    key: str,
    value: object,
    tmp_path: Path,
) -> None:
    agents.install_hooks("codex", tmp_path)
    agents.observe_hooks("codex", tmp_path, agents.hook_fingerprint("codex", tmp_path))
    path = tmp_path / "hooks.json"
    payload = json.loads(path.read_text())
    group = payload["hooks"]["Stop"][0]
    definition = group if level == "group" else group["hooks"][0]
    definition[key] = value
    path.write_text(json.dumps(payload))
    assert agents.integration_readiness("codex", tmp_path)[0] == "unverified"


@pytest.mark.parametrize(
    "args,configured",
    [
        (['-cotel.exporter="none"'], True),
        (['-c=otel.exporter="none"'], True),
        (["-pwork"], True),
        (["-p=work"], True),
        (["exec", "--", "-pwork"], False),
        (["exec", "--", '-cotel.exporter="none"'], False),
    ],
)
def test_actual_launch_preserves_attached_options_and_native_prompt_terminators(
    args: list[str],
    configured: bool,
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_config()
    config.explainability.enabled = config.explainability.ship = True
    save_config(config)
    (tmp_path / "work.config.toml").write_text('[otel]\nexporter="none"\n')
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(native_telemetry, "SYSTEM_CONFIG", tmp_path / "system.toml")
    monkeypatch.setattr(agent_launch, "executable", lambda selected: "/fixture/codex")
    start = Mock(return_value=(["-c", "fixture.receiver=true"], "receiver started"))
    execute = Mock()
    monkeypatch.setattr(native_telemetry, "start", start)
    monkeypatch.setattr(import_module("aisquare.cli.launch"), "_exec", execute)
    result = runner.invoke(app, ["launch", "coder", "--agent", "codex", "--", *args])
    assert result.exit_code == 0, result.output
    assert start.call_count == (0 if configured else 1)
    assert execute.call_args.args[1][-len(args) :] == args


def test_nul_profile_from_native_file_does_not_break_telemetry_inspection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(native_telemetry, "SYSTEM_CONFIG", tmp_path / "system.toml")
    (tmp_path / "config.toml").write_text('profile="work\\u0000"\n')
    assert not native_telemetry.operator_configured(tmp_path, [])


@pytest.mark.parametrize("layer", ["system", "user", "profile"])
def test_readable_exporters_and_missing_layers_have_opposite_results(
    layer: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = tmp_path / "system.toml"
    monkeypatch.setattr(native_telemetry, "SYSTEM_CONFIG", system)
    assert not native_telemetry.operator_configured(tmp_path, ["-pwork"])
    path = {
        "system": system,
        "user": tmp_path / "config.toml",
        "profile": tmp_path / "work.config.toml",
    }[layer]
    path.write_text('[otel]\nexporter="none"\n')
    assert native_telemetry.operator_configured(tmp_path, ["-pwork"])


def test_keychain_entitlement_changes_use_explicit_refresh(
    tmp_path: Path,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claude_accounts, "keychain_platform", lambda: True)
    read_credentials = Mock(side_effect=AssertionError("No Keychain/token read for scope hashing"))
    monkeypatch.setattr(claude_accounts, "credentials", read_credentials)
    token = harness._PROBE_CONTEXT.set(
        harness.ProbeContext("/fixture/claude", {"CLAUDE_CONFIG_DIR": str(tmp_path)})
    )
    try:
        before = harness.account_scope()
        harness._save_cache(
            {
                "opus": harness.ProbeResult(
                    alias="opus",
                    available=False,
                    checked_at=datetime.now(UTC),
                )
            }
        )
        (tmp_path / ".credentials.json").write_text('{"claudeAiOauth":{"subscriptionType":"max"}}')
        assert harness.account_scope() == before
        assert harness.cached_probe("opus") is not None
        harness.clear_probe_cache()
        assert harness.cached_probe("opus") is None
        read_credentials.assert_not_called()
    finally:
        harness._PROBE_CONTEXT.reset(token)
    help_result = runner.invoke(app, ["team", "spawn", "--help"])
    # GitHub Actions forces Rich styling, including escapes inside option names.
    help_text = unstyle(help_result.output)
    assert "Keychain" in help_text and "--refresh" in help_text


def test_normal_probe_cache_writes_prune_expired_binary_scopes_and_keep_other_accounts(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "claude"
    binary.write_text("version one")
    context = harness.ProbeContext(str(binary), {"CLAUDE_CONFIG_DIR": str(tmp_path / "account")})
    token = harness._PROBE_CONTEXT.set(context)
    result = {
        "opus": harness.ProbeResult(alias="opus", available=True, checked_at=datetime.now(UTC))
    }
    try:
        harness._save_cache(result)
        expired = harness._cache_path()
        binary.write_text("version two, a newer binary")
        assert harness._cache_path() != expired
        harness._save_cache(result)
        current = harness._cache_path()
        context.env["CLAUDE_CONFIG_DIR"] = str(tmp_path / "other-account")
        harness._save_cache(result)
        other = harness._cache_path()
        assert other != current
        old = (datetime.now(UTC) - harness.CACHE_TTL - timedelta(hours=1)).timestamp()
        os.utime(expired, (old, old))
        context.env["CLAUDE_CONFIG_DIR"] = str(tmp_path / "account")
        harness._save_cache(result)  # ordinary successful probe, no --refresh
        assert not expired.exists() and current.exists() and other.exists()
    finally:
        harness._PROBE_CONTEXT.reset(token)
