"""Codex CLI 0.162.1's contract: effort levels, approvals, --approve-for-me and hooks off.

Each fact was read from `codex --help`, `codex features list` and Codex's hooks and
configuration reference, and the flag conflicts from the binary itself (it refuses
`--approve-for-me` beside `--sandbox` or `--ask-for-approval`).
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import agents
from aisquare.core.agent_adapters import get_adapter
from aisquare.core.agent_adapters import get_adapter as _get_adapter
from aisquare.core.agent_adapters.codex import CodexAdapter
from aisquare.core.agent_adapters.types import (
    BadEffortError,
    PermissionIntent,
    WriteScope,
    store_dir,
)
from aisquare.core.config import (
    AgentModelSettings,
    FleetRoleSettings,
    RoleLaunchProfile,
    load_config,
    save_config,
)
from aisquare.core.orchestrator import team_project
from aisquare.core.store import store_session
from aisquare.models import CheckStatus
from aisquare.services import agent_events, agent_launch, diagnostics, fleet
from aisquare.services.agents import connect
from tests.test_fleet_service import FakeTmux

CODEX = _get_adapter("codex")

codex = CodexAdapter()


# --- reasoning effort ---------------------------------------------------------------


@pytest.mark.parametrize("level", ["minimal", "low", "medium", "high", "xhigh", "max", "ultra"])
def test_codex_levels_are_emitted_unchanged(level: str) -> None:
    assert codex.model_args(None, level) == ["-c", f'model_reasoning_effort="{level}"']
    assert codex.native_args(["-c", f"model_reasoning_effort={level}"]) == [
        "-c",
        f"model_reasoning_effort={level}",
    ]


@pytest.mark.parametrize("level", ["max", "ultra"])
def test_max_and_ultra_reach_the_launch_without_a_mapping_note(level: str) -> None:
    config = load_config()
    config.agents.models["codex"] = AgentModelSettings(effort=level)
    save_config(config)
    selected = agent_launch.resolve(agent="codex")
    resolution = agent_launch.model_for(selected, "coder", probe=False)
    assert resolution is not None and resolution.effort == level
    assert resolution.notes == []
    assert selected.adapter.model_args(None, resolution.effort) == [
        "-c",
        f'model_reasoning_effort="{level}"',
    ]


def test_ultracode_is_the_one_alias_and_is_noted() -> None:
    config = load_config()
    config.agents.models["codex"] = AgentModelSettings(effort="ultracode")
    save_config(config)
    selected = agent_launch.resolve(agent="codex")
    resolution = agent_launch.model_for(selected, "coder", probe=False)
    assert resolution is not None and resolution.effort == "max"
    assert resolution.notes == ["Codex maps 'ultracode' to native reasoning effort 'max'."]
    assert codex.native_args(["-c", "model_reasoning_effort=ultracode"]) == [
        "-c",
        'model_reasoning_effort="max"',
    ]


def test_an_unknown_level_is_passed_on_with_a_note() -> None:
    config = load_config()
    config.agents.models["codex"] = AgentModelSettings(effort="Turbo")
    save_config(config)
    selected = agent_launch.resolve(agent="codex")
    resolution = agent_launch.model_for(selected, "coder", probe=False)
    assert resolution is not None and resolution.effort == "turbo"
    assert any("'turbo' is not one AISquare knows" in note for note in resolution.notes)
    assert codex.model_args(None, "turbo") == ["-c", 'model_reasoning_effort="turbo"']


@pytest.mark.parametrize("effort", ["two words", 'high" model="x', "-high"])
def test_what_is_not_a_level_name_is_refused(effort: str) -> None:
    with pytest.raises(BadEffortError, match="level name"):
        codex.model_args(None, effort)


# --- approvals and --approve-for-me -------------------------------------------------


def test_untrusted_is_refused_naming_the_policies_codex_takes() -> None:
    with pytest.raises(ValueError, match="on-request or never, not 'untrusted'"):
        codex.fleet_args("coder", "coder-1", None, approval="untrusted")


#: Who answers Codex's approvals, pinned (``approvals_reviewer``).
HUMAN = ["-c", 'approvals_reviewer="user"']
REVIEWED = ["-c", 'approvals_reviewer="auto_review"']


def _store(write: WriteScope) -> list[str]:
    """Codex's store profile for ``write``, the aisquare home being this test's."""
    return codex.store_access(write, store_dir())


@pytest.mark.parametrize("approval", ["on-request", "never"])
def test_the_supported_policies_are_passed(approval: str) -> None:
    assert codex.fleet_args("coder", "coder-1", None, approval=approval) == [
        "--ask-for-approval",
        approval,
        *(HUMAN if approval == "on-request" else []),
        *_store("workspace"),
    ]


@pytest.mark.parametrize("role", ["coder", "manager", "tester", "validator", "coder2"])
def test_approve_for_me_is_the_default_for_every_role_but_a_reviewer(role: str) -> None:
    assert codex.fleet_args(role, role, None) == ["--approve-for-me", *_store("workspace")]


@pytest.mark.parametrize("role", ["reviewer", "reviewer2"])
def test_a_reviewer_stays_read_only_with_codexs_own_reviewer(role: str) -> None:
    """``read_only:auto``, as the intent maps it: on-request, answered by Codex's reviewer
    as every other role's ``--approve-for-me`` is, in the read-only sandbox."""
    assert codex.fleet_args(role, role, None) == [
        "--ask-for-approval",
        "on-request",
        *REVIEWED,
        *_store("read_only"),
    ]
    assert codex.fleet_args(role, role, None) == codex.permission_args(
        PermissionIntent.for_role(role), role
    )


def test_a_chosen_sandbox_or_policy_turns_the_default_off() -> None:
    """Without ``--approve-for-me`` the human answers: ``approve_for_me = false`` says so,
    and config.toml's reviewer must not say otherwise."""
    assert codex.fleet_args("coder", "c", None, sandbox="workspace-write") == [
        *HUMAN,
        *_store("workspace"),
    ]
    assert codex.fleet_args("coder", "c", None, sandbox="") == HUMAN
    assert codex.fleet_args("coder", "c", None, sandbox="danger-full-access") == [
        *HUMAN,
        "--sandbox",
        "danger-full-access",
    ]
    assert codex.fleet_args("coder", "c", None, approve_for_me=False) == [
        *HUMAN,
        *_store("workspace"),
    ]
    assert codex.fleet_args("reviewer", "r", None, approve_for_me=True) == [
        "--approve-for-me",
        *_store("workspace"),
    ]
    assert codex.fleet_args("coder", "c", None, sandbox="workspace-write", approve_for_me=True) == [
        "--approve-for-me",
        *_store("workspace"),
    ]


@pytest.mark.parametrize(
    "kwargs, clash",
    [
        ({"sandbox": "read-only"}, "--sandbox read-only"),
        ({"approval": "never"}, "--ask-for-approval never"),
        ({"native": ["-a", "never"]}, "-a never"),
        ({"native": ["--yolo"]}, "--yolo"),
    ],
)
def test_approve_for_me_asked_for_beside_a_clash_is_refused(
    kwargs: dict[str, object], clash: str
) -> None:
    with pytest.raises(ValueError, match=f"cannot run with {clash}; set fleet.roles.coder"):
        codex.fleet_args("coder", "c", None, approve_for_me=True, **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "native, expected",
    [
        (["--no-alt-screen"], ["--approve-for-me", "STORE"]),
        (["-a", "never"], ["STORE"]),
        (["--ask-for-approval=on-request"], ["STORE"]),
        (["--sandbox", "danger-full-access"], []),
        (["-sread-only"], []),
        (["--approve-for-me"], ["STORE"]),
        (["--dangerously-bypass-approvals-and-sandbox"], []),
        (["--", "--approve-for-me"], ["--approve-for-me", "STORE"]),
        (["-c", 'sandbox_mode="read-only"'], ["--approve-for-me"]),
        (["-c", 'default_permissions="mine"'], ["--approve-for-me"]),
        (["--config=sandbox_workspace_write.network_access=true"], ["--approve-for-me"]),
    ],
)
def test_the_launch_arguments_own_choices_are_not_given_twice(
    native: list[str], expected: list[str]
) -> None:
    """A native ``--sandbox`` replaces any profile and a native sandbox setting cannot sit
    beside one (Codex: "cannot both be set"), so the store is left to them; beside a native
    ``--approve-for-me`` (workspace-write) the store's profile still goes."""
    store = _store("workspace")
    flags = [arg for flag in expected for arg in (store if flag == "STORE" else [flag])]
    assert codex.fleet_args("coder", "c", None, native=native) == flags


def test_ask_and_auto_pin_who_answers_unless_the_launch_says() -> None:
    """``ask`` is the human, ``auto`` Codex's reviewer, on a spawn, a restart and a resume
    alike (a resumed thread keeps its reviewer unless told: t8 smoke on 0.162.1)."""
    ask = codex.permission_args(PermissionIntent("workspace", "ask"), "coder")
    assert ask[:4] == ["--ask-for-approval", "on-request", *HUMAN]
    auto = codex.permission_args(PermissionIntent("read_only", "auto"), "reviewer")
    assert auto[:4] == ["--ask-for-approval", "on-request", *REVIEWED]
    never = codex.permission_args(PermissionIntent("workspace", "never"), "coder")
    assert "approvals_reviewer" not in " ".join(never)
    mine = ["-c", 'approvals_reviewer="auto_review"']
    assert "user" not in " ".join(
        codex.permission_args(PermissionIntent("workspace", "ask"), "coder", mine)
    )


def test_the_store_profile_keeps_what_runs_outside_the_sandbox_read_only(tmp_path: Path) -> None:
    """Codex's built-in profile for the scope with the aisquare home writable and, within
    it, every account home, the credentials and the config read-only: a hook or a setting
    written there would run outside the sandbox (measured on 0.162.1: a blanket
    ``--add-dir`` of the home let a sandboxed command rewrite its own CODEX_HOME)."""
    home = tmp_path / "aisquare home"
    scopes: tuple[tuple[WriteScope, str], ...] = (
        ("workspace", ":workspace"),
        ("read_only", ":read-only"),
    )
    for write, built_in in scopes:
        args = codex.store_access(write, home)
        assert args[:4] == [
            "-c",
            'default_permissions="aisquare"',
            "-c",
            f'permissions.aisquare.extends="{built_in}"',
        ]
        key, _, table = args[5].partition("=")
        assert args[4] == "-c" and key == "permissions.aisquare.filesystem"
        entries = tomllib.loads(f"t = {table}")["t"]
        assert entries[str(home)] == "write"
        for name in ("codex-accounts", "claude-accounts", "credentials", "config.toml"):
            assert entries[str(home / name)] == "read", name
        # The digests of the launch specs a replay runs (review r2 of #113, finding 3);
        # never a path denied reading, with which Codex did not start on a GitHub runner.
        assert entries[str(home / "launch-specs")] == "read"
        assert set(entries.values()) == {"write", "read"}
    assert codex.store_access("full", home) == []


def _sandbox_of(command: list[str]) -> str | None:
    """The Codex sandbox a launch chooses: ``--sandbox``'s value, or the built-in profile
    the store's profile extends (``:read-only``, ``:workspace``)."""
    if "--sandbox" in command:
        return command[command.index("--sandbox") + 1]
    for arg in command:
        if arg.startswith("permissions.aisquare.extends="):
            return str(json.loads(arg.partition("=")[2]))
    return None


def _spawned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str, **kwargs: object
) -> list[str]:
    tmux = FakeTmux()
    monkeypatch.setattr(fleet, "server", lambda config: tmux)
    monkeypatch.setattr(agent_launch, "executable", lambda selected: "/fixture/codex")
    fleet.spawn(team_project(tmp_path), role, agent="codex", worktree=False, **kwargs)  # type: ignore[arg-type]
    command = tmux.spawned[-1]["command"]
    assert isinstance(command, list)
    return command


@pytest.mark.parametrize("role", ["coder", "manager", "tester", "validator"])
def test_a_fleet_spawn_runs_codex_with_approve_for_me(
    role: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = _spawned(tmp_path, monkeypatch, role)
    assert "--approve-for-me" in command
    assert "--sandbox" not in command and "--ask-for-approval" not in command


@pytest.mark.parametrize("role", ["reviewer", "reviewer2"])
def test_a_fleet_reviewer_runs_codex_read_only(
    role: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = _spawned(tmp_path, monkeypatch, role)
    assert "--approve-for-me" not in command
    assert _sandbox_of(command) == ":read-only"
    assert command[command.index("--ask-for-approval") + 1] == "on-request"


def test_the_role_setting_and_a_spawn_choice_turn_it_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_config()
    config.fleet.roles["coder"] = FleetRoleSettings(approve_for_me=False)
    save_config(config)
    command = _spawned(tmp_path, monkeypatch, "coder")
    assert "--approve-for-me" not in command
    assert _sandbox_of(command) == ":workspace"
    # A numbered seat inherits the base role's choice, as it does the sandbox's.
    assert fleet.role_settings("coder2", config.fleet).approve_for_me is False
    config.fleet.roles["coder"] = FleetRoleSettings(approve_for_me=True)
    save_config(config)
    command = _spawned(tmp_path, monkeypatch, "coder", approval_policy="never")
    assert "--approve-for-me" not in command
    assert command[command.index("--ask-for-approval") + 1] == "never"
    # Codex reads a spawn's --permission-mode as its sandbox, a choice of this spawn too.
    command = _spawned(tmp_path, monkeypatch, "coder", permission_mode="read-only")
    assert "--approve-for-me" not in command
    assert _sandbox_of(command) == ":read-only"


def test_a_role_policy_codex_refuses_refuses_the_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_config()
    config.fleet.roles["coder"] = FleetRoleSettings(approval_policy="untrusted")
    save_config(config)
    with pytest.raises(fleet.FleetError, match="on-request or never"):
        _spawned(tmp_path, monkeypatch, "coder")


# --- hooks switched off -------------------------------------------------------------


def _home(tmp_path: Path, features: str = "") -> Path:
    home = tmp_path / ".codex"
    home.mkdir(exist_ok=True)
    if features:
        (home / "config.toml").write_text(f"[features]\n{features}\n", encoding="utf-8")
    return home


@pytest.mark.parametrize("key", ["hooks", "codex_hooks"])
def test_hooks_off_in_config_toml(key: str, tmp_path: Path) -> None:
    home = _home(tmp_path, f"{key} = false")
    switch = agents.hooks_switch("codex", home)
    assert switch is not None and switch.path == home / "config.toml"
    assert switch.setting == f"`{key} = false` under [features]"
    assert agents.hooks_disabled("codex", home)
    assert agents.hooks_off("codex", home) == home / "config.toml"


@pytest.mark.parametrize(
    "features",
    ["", "hooks = true", 'hooks = "false"', "hooks = true\ncodex_hooks = false", "other = false"],
)
def test_hooks_on(features: str, tmp_path: Path) -> None:
    home = _home(tmp_path, features)
    assert agents.hooks_switch("codex", home) is None
    assert agents.hooks_off("codex", home) is None


def test_the_canonical_key_wins_over_the_alias_in_any_layer(tmp_path: Path) -> None:
    home = _home(tmp_path, "hooks = true")
    assert agents.hooks_switch("codex", home, ["-c", "features.codex_hooks=false"]) is None
    home = _home(tmp_path, "codex_hooks = true")
    switch = agents.hooks_switch("codex", home, ["-c", "features.hooks=false"])
    assert switch is not None and str(switch) == (
        "`-c features.hooks=false` in the launch arguments"
    )


@pytest.mark.parametrize(
    "args, spelled",
    [
        (["--disable", "hooks"], "--disable hooks"),
        (["--disable=hooks"], "--disable hooks"),
        (["--disable", "codex_hooks"], "--disable codex_hooks"),
        (["-c", "features.hooks=false"], "-c features.hooks=false"),
        (["-cfeatures.hooks = false"], "-c features.hooks = false"),
        (["--config=features.codex_hooks=false"], "--config features.codex_hooks=false"),
    ],
)
def test_hooks_off_in_the_launch_arguments(args: list[str], spelled: str, tmp_path: Path) -> None:
    home = _home(tmp_path)
    switch = agents.hooks_switch("codex", home, args)
    assert switch is not None and switch.path is None and switch.setting == f"`{spelled}`"
    # The directory itself is not switched off: `agents status` names no file.
    assert agents.hooks_off("codex", home) is None


def test_a_later_layer_wins(tmp_path: Path) -> None:
    home = _home(tmp_path, "hooks = false")
    assert agents.hooks_switch("codex", home, ["--enable", "hooks"]) is None
    assert agents.hooks_switch("codex", home, ["--enable", "hooks", "--disable", "hooks"])
    assert agents.hooks_switch("codex", home, ["--", "--enable", "hooks"]) is not None
    (home / "work.config.toml").write_text("[features]\nhooks = true\n", encoding="utf-8")
    assert agents.hooks_switch("codex", home, ["-p", "work"]) is None
    _home(tmp_path, "hooks = true")
    (home / "work.config.toml").write_text("[features]\nhooks = false\n", encoding="utf-8")
    switch = agents.hooks_switch("codex", home, ["--profile", "work"])
    assert switch is not None and switch.path == home / "work.config.toml"


def test_an_unreadable_config_switches_nothing_off(tmp_path: Path) -> None:
    home = _home(tmp_path)
    (home / "config.toml").write_text("[features\nhooks = false", encoding="utf-8")
    assert agents.hooks_switch("codex", home) is None


def test_claude_codes_switch_is_unchanged(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text(json.dumps({"disableAllHooks": True}))
    switch = agents.hooks_switch("claude-code", tmp_path, ["--disable", "hooks"])
    assert switch is not None
    assert str(switch) == f'"disableAllHooks": true in {tmp_path / "settings.json"}'


def _codex_row(tmp_path: Path, home: Path) -> list[str]:
    connect("codex", home)
    agent_launch.use("codex")
    return [
        check.detail
        for check in diagnostics._check_other_agents(tmp_path)
        if check.name == "codex" and check.status == CheckStatus.warn
    ]


@pytest.mark.parametrize("key", ["hooks", "codex_hooks"])
def test_the_doctor_row_says_hooks_are_off(
    key: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path, f"{key} = false")
    monkeypatch.setenv("CODEX_HOME", str(home))
    rows = _codex_row(tmp_path, home)
    assert any(
        f"hooks are switched off (`{key} = false` under [features] in {home / 'config.toml'})"
        in row
        for row in rows
    ), rows


def test_the_doctor_row_names_roles_whose_arguments_switch_hooks_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(home))
    config = load_config()
    config.team.profiles["coder"] = RoleLaunchProfile(agent="codex", args=["--disable", "hooks"])
    config.fleet.roles["tester"] = FleetRoleSettings(
        agent_args={"codex": ["-c", "features.hooks=false"]}
    )
    save_config(config)
    rows = _codex_row(tmp_path, home)
    assert any(
        "hooks are switched off for coder (`--disable hooks` in the launch arguments)" in row
        for row in rows
    ), rows
    assert any(
        "hooks are switched off for tester (`-c features.hooks=false`" in row for row in rows
    ), rows
    assert not any("hooks are switched off (" in row for row in rows), rows


def test_connect_names_codexs_own_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    home = _home(tmp_path, "hooks = false")
    monkeypatch.setenv("CODEX_HOME", str(home))
    result = runner.invoke(app, ["agents", "connect", "codex", "--config-dir", str(home)])
    assert result.exit_code == 0, result.output
    assert "switched off" in result.output
    assert "`features.hooks = false`, so Codex runs none of its hooks" in result.output
    assert "disableAllHooks" not in result.output


# --- hook events --------------------------------------------------------------------


@pytest.mark.parametrize(
    "event", ["SubagentStart", "SubagentStop", "PreCompact", "PostCompact", "FutureEvent"]
)
def test_events_aisquare_does_not_install_are_ignored_quietly(
    event: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = {"session_id": "native-1", "hook_event_name": event, "cwd": str(tmp_path)}
    assert agent_events.handle(CODEX, payload, tmp_path) == ""
    with store_session() as store:
        assert store.get_session(agent_events.session_key(CODEX, tmp_path, "native-1")) is None
    assert capsys.readouterr() == ("", "")


def test_the_hook_command_prints_nothing_for_an_unknown_event(
    tmp_path: Path, runner: CliRunner
) -> None:
    payload = json.dumps({"session_id": "native-1", "hook_event_name": "SubagentStart"})
    result = runner.invoke(app, ["hook", "codex", "--config-dir", str(tmp_path)], input=payload)
    assert result.exit_code == 0
    assert result.output == ""


def test_the_adapter_installs_its_event_set() -> None:
    events = {hook.event for hook in get_adapter("codex").capabilities.hooks}
    assert events == {
        "SessionStart",
        "UserPromptSubmit",
        "Stop",
        "SessionEnd",
        "Interrupt",
        "PermissionRequest",
        "PreToolUse",
        "PostToolUse",
    }
    # Codex allows SessionEnd and Interrupt three seconds at most.
    assert all(
        hook.timeout is not None and hook.timeout <= 3
        for hook in get_adapter("codex").capabilities.hooks
        if hook.event in {"SessionEnd", "Interrupt"}
    )
