"""Exercise native boundaries through the paths that own their effects."""

from __future__ import annotations

import json
import shlex
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.cli.watch import _session_lines
from aisquare.core import agents, harness
from aisquare.core.agent_adapters.types import config_home
from aisquare.core.agent_sessions import adopt_local_session
from aisquare.core.config import (
    AgentModelSettings,
    FleetRoleSettings,
    RoleLaunchProfile,
    load_config,
    save_config,
)
from aisquare.core.orchestrator import team_project
from aisquare.core.store import SqliteStore, store_session
from aisquare.models import TeamSession
from aisquare.services import agent_events, agent_launch, fleet, mcp_server
from aisquare.services.agents import connect, disconnect
from tests.test_fleet_service import FakeTmux


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agents, "_home", lambda: tmp_path)
    monkeypatch.setenv("AISQUARE_HARNESS_PROBE", "0")
    monkeypatch.setenv("AISQUARE_TEAM", "1")
    monkeypatch.setenv("AISQUARE_ROLE", "coder")


@pytest.mark.parametrize(
    "family,filename", [("claude-code", "settings.json"), ("codex", "hooks.json")]
)
def test_connect_and_disconnect_retain_foreign_empty_groups(
    family: str, filename: str, tmp_path: Path
) -> None:
    path = tmp_path / filename
    foreign = {"hooks": {"Stop": [{"matcher": "Bash", "hooks": []}]}}
    path.write_text(json.dumps(foreign))
    original = path.read_bytes()
    assert not disconnect(family, tmp_path)
    assert path.read_bytes() == original
    assert connect(family, tmp_path).hooks_installed
    assert json.loads(path.read_text())["hooks"]["Stop"][0] == foreign["hooks"]["Stop"][0]
    assert disconnect(family, tmp_path)
    assert json.loads(path.read_text()) == foreign


@pytest.mark.parametrize("family,binary", [("claude-code", "claude"), ("codex", "codex")])
def test_connect_fresh_binary_before_first_native_launch(
    family: str, binary: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "unused-account"
    monkeypatch.setattr(
        "aisquare.services.agents.shutil.which",
        lambda name: f"/bin/{binary}" if name == binary else None,
    )
    assert connect(family, directory).hooks_installed
    assert agents.hooks_installed(family, directory)


def _definition(directory: Path) -> str:
    settings = json.loads((directory / "hooks.json").read_text())
    command = shlex.split(settings["hooks"]["SessionStart"][0]["hooks"][0]["command"])
    return command[command.index("--definition") + 1]


def test_only_the_executed_definition_can_verify_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    agents.install_hooks("codex", tmp_path)
    first = _definition(tmp_path)
    monkeypatch.setattr(agent_events, "_dispatch_codex", lambda *args: None)
    payload = json.dumps({"hook_event_name": "SessionStart", "session_id": "thread"})

    def fire(definition: str) -> None:
        result = runner.invoke(
            app,
            ["hook", "codex", "--config-dir", str(tmp_path), "--definition", definition],
            input=payload,
        )
        assert result.exit_code == 0, result.output

    fire(first)
    assert agents.integration_readiness("codex", tmp_path)[0] == "observed"
    # An upgrade changes the command while an older native session stays open.
    monkeypatch.setattr(agents, "_aisquare_command", lambda: "/upgraded/bin/aisquare")
    agents.install_hooks("codex", tmp_path)
    second = _definition(tmp_path)
    assert second != first
    fire(first)
    assert agents.integration_readiness("codex", tmp_path)[0] == "unverified"
    fire(second)
    assert agents.integration_readiness("codex", tmp_path)[0] == "observed"
    before = (tmp_path / "hooks.json").read_bytes()
    agents.install_hooks("codex", tmp_path)
    assert (tmp_path / "hooks.json").read_bytes() == before
    assert disconnect("codex", tmp_path)


def test_home_alias_reconnect_preserves_definition_and_observation(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    connect("codex", real)
    agents.observe_hooks("codex", real, _definition(real))
    before = (real / "hooks.json").read_bytes()
    connect("codex", alias)
    assert (real / "hooks.json").read_bytes() == before
    assert agents.integration_readiness("codex", alias)[0] == "observed"
    selected = agent_launch.resolve(agent="codex", env_overrides={"CODEX_HOME": str(alias)})
    assert config_home(selected.adapter, tmp_path, {}, alias) == selected.config_dir == real


@pytest.mark.parametrize("family", ["claude-code", "codex"])
def test_effective_role_pins_are_shared(family: str) -> None:
    selected = agent_launch.resolve(
        agent=family,
        env_overrides={"AISQUARE_MODEL_CODER": "pinned-model", "AISQUARE_EFFORT_CODER": "xhigh"},
    )
    resolved = agent_launch.model_for(selected, "coder", probe=False)
    assert resolved and resolved.model == "pinned-model" and resolved.effort == "xhigh"


@pytest.mark.parametrize(
    "native",
    [
        ["-c", 'model="operator"', "-c", 'model_reasoning_effort="low"'],
        ["-moperator", "--config=model_reasoning_effort=low"],
    ],
)
def test_native_overrides_survive_invalid_defaults_through_launch_and_spawn(
    native: list[str], runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # This test intercepts exec; it must not require a local Codex installation.
    # An empty PATH proves the argument test does not borrow this workstation's binary.
    monkeypatch.setenv("PATH", str(tmp_path / "no-executables"))
    monkeypatch.setattr(agent_launch, "executable", lambda selected: selected.binary.binary)
    cfg = load_config()
    cfg.agents.models["codex"] = AgentModelSettings(model="configured", effort="invalid")
    save_config(cfg)
    seen: list[tuple[list[str], dict[str, str]]] = []
    monkeypatch.setattr(
        import_module("aisquare.cli.launch"),
        "_exec",
        lambda binary, argv, env: seen.append((argv, env)),
    )
    result = runner.invoke(app, ["launch", "coder", "--agent", "codex", "--", *native])
    assert result.exit_code == 0, result.output
    assert seen[0][0] == ["codex", *native]
    result = runner.invoke(
        app,
        [
            "--json",
            "team",
            "spawn",
            "coder",
            "--agent",
            "codex",
            *[arg for token in native for arg in ("--arg", token)],
        ],
    )
    assert result.exit_code == 0, result.output
    row = json.loads(result.stdout)
    assert row["model"] == "operator" and row["effort"] == "low"
    assert "configured" not in row["command"] and "invalid" not in row["command"]


def test_seat_configuration_falls_back_per_field() -> None:
    cfg = load_config()
    cfg.agents.models["codex"] = AgentModelSettings.model_validate(
        {
            "model": "default",
            "roles": {"coder": {"model": "base", "effort": "high"}, "coder2": {"model": "seat"}},
        }
    )
    save_config(cfg)
    result = agent_launch.model_for(agent_launch.resolve("coder2", agent="codex"), "coder2")
    assert result and (result.model, result.effort) == ("seat", "high")


def test_binary_short_option_does_not_steal_a_path_with_equals(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_launch, "executable", lambda selected: selected.binary.binary)
    seen: list[list[str]] = []
    monkeypatch.setattr(
        import_module("aisquare.cli.launch"), "_exec", lambda binary, argv, env: seen.append(argv)
    )
    for option in ("-c", "--command"):
        result = runner.invoke(
            app, ["launch", "coder", "--agent", "claude-code", option, "/opt/claude=v2"]
        )
        assert result.exit_code == 0, result.output
    assert seen == [["/opt/claude=v2"], ["/opt/claude=v2"]]


@pytest.mark.parametrize("source", ["flag", "config", "env"])
def test_legacy_wrapper_without_agent_defaults_still_launches(
    source: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    kwargs: dict[str, Any] = {}
    if source == "flag":
        kwargs["binary"] = "legacy-wrapper"
    elif source == "env":
        monkeypatch.setenv("AISQUARE_BIN_CODER", "legacy-wrapper")
    else:
        cfg = load_config()
        cfg.team.profiles["coder"] = RoleLaunchProfile(bin="legacy-wrapper")
        save_config(cfg)
    assert agent_launch.resolve(**kwargs).binary.binary == "legacy-wrapper"


def test_native_prompt_boundary_is_preserved(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = load_config()
    cfg.agents.models["codex"] = AgentModelSettings(model="configured")
    save_config(cfg)
    monkeypatch.setattr(agent_launch, "executable", lambda selected: selected.binary.binary)
    seen: list[list[str]] = []
    monkeypatch.setattr(
        import_module("aisquare.cli.launch"), "_exec", lambda binary, argv, env: seen.append(argv)
    )
    result = runner.invoke(
        app, ["launch", "coder", "--agent", "codex", "--", "--", "-migrate the schema"]
    )
    assert result.exit_code == 0, result.output
    assert seen == [["codex", "--model", "configured", "--", "-migrate the schema"]]


def test_fleet_keeps_bound_tokens_out_of_tmux_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = load_config()
    cfg.team.profiles["coder"] = RoleLaunchProfile(
        agent="codex", env={"ANTHROPIC_AUTH_TOKEN": "fixture-secret"}
    )
    cfg.fleet.roles["coder"] = FleetRoleSettings(worktree=False)
    save_config(cfg)
    tmux = FakeTmux()
    monkeypatch.setattr(fleet, "server", lambda config: tmux)
    monkeypatch.setattr(agent_launch, "executable", lambda selected: "/fixture/codex")
    fleet.spawn(team_project(tmp_path), "coder")
    assert "fixture-secret" not in json.dumps(tmux.spawned, default=str)


def test_untraced_pasted_claude_spawn_cannot_rebind_its_parent(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_LAUNCH_ID", "parent-launch")
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", "parent-fleet")
    result = runner.invoke(app, ["--json", "team", "spawn", "coder", "--no-probe"])
    assert result.exit_code == 0, result.output
    command = json.loads(result.stdout)["command"]
    execute = Mock()
    monkeypatch.setattr(import_module("aisquare.cli.launch"), "_exec", execute)
    monkeypatch.setattr(agent_launch, "executable", lambda selected: "claude")
    argv = shlex.split(command)
    launched = runner.invoke(app, argv[argv.index("launch") :])
    assert launched.exit_code == 0, launched.output
    env = execute.call_args.args[2]
    assert env["AISQUARE_LAUNCH_ID"] != "parent-launch"
    assert "AISQUARE_FLEET_AGENT" not in env


def test_codex_new_session_rebinds_board_and_mcp_without_late_old_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_LAUNCH_ID", "launch")
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", "fleet")
    project = team_project(tmp_path)
    for native in ("first", "second", "first"):
        agent_events.handle_codex(
            {"hook_event_name": "SessionStart", "session_id": native, "cwd": str(tmp_path)},
            tmp_path,
        )
    second = agent_events.session_key("codex", tmp_path, "second")
    with store_session() as store:
        assert store.get_meta("fleet-session:fleet") == second
        assert store.get_meta("launch-session:launch") == second
    assert mcp_server.client_session_id(project.id) == second


def _session(tmp_path: Path, name: str = "native") -> TeamSession:
    return TeamSession(
        id=name,
        project_id=team_project(tmp_path).id,
        role="coder2",
        started_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
    )


def test_session_readback_failure_rolls_back_the_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _session(tmp_path)
    with store_session() as store:
        store.ensure_project(team_project(tmp_path))
        real = SqliteStore.get_session

        def fail_readback(self: SqliteStore, ref: str) -> TeamSession | None:
            assert self._conn.in_transaction
            raise RuntimeError("fault after insert, before read-back")

        monkeypatch.setattr(SqliteStore, "get_session", fail_readback)
        with pytest.raises(RuntimeError, match="read-back"):
            store.upsert_session(row)
        monkeypatch.setattr(SqliteStore, "get_session", real)
        assert store.get_session(row.id) is None


def test_no_adoption_needed_does_not_acquire_writer_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_LAUNCH_ID", "launch")
    row = _session(tmp_path)
    with store_session() as store:
        store.ensure_project(team_project(tmp_path))
        store.upsert_session(row)
        assert isinstance(store, SqliteStore)
        statements: list[str] = []
        store._conn.set_trace_callback(statements.append)
        assert adopt_local_session(store, row).id == row.id
        assert not any(statement.startswith("BEGIN") for statement in statements)


def test_ephemeral_retention_keeps_active_bindings_and_durable_data(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    old = now - timedelta(days=2)
    with store_session() as store:
        store.ensure_project(team_project(tmp_path))
        for name, seen in (("active", now), ("abandoned", old)):
            store.upsert_session(_session(tmp_path, name).model_copy(update={"last_seen_at": seen}))
            store.set_meta(f"launch-session:{name}", name)
            store.set_meta(f"launch-seen:{name}:{name}", "1")
            store.set_meta(f"agent-event:{name}:Stop:turn:False", '"done"')
        store.set_meta("coding-agent:unrelated-project", "codex")
        assert isinstance(store, SqliteStore)
        store._conn.execute("UPDATE team_meta SET updated_at = ?", (old.timestamp(),))
        store._conn.commit()
        assert store.expire_native_launches((now - timedelta(days=1)).timestamp()) == 3
        assert store.get_meta("launch-session:active") == "active"
        assert store.get_meta("coding-agent:unrelated-project") == "codex"
        assert store.get_meta("agent-event:abandoned:Stop:turn:False") is None


def test_pending_claim_is_not_fresh_after_suspend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "time", lambda: 30000.0)
    monkeypatch.setattr(time, "monotonic", lambda: 11.0)
    assert not agent_events._pending_fresh({"pending_at": 1000, "monotonic_at": 10})
    assert agent_events._pending_fresh({"pending_at": 29999, "monotonic_at": 10})
    assert agent_events._pending_fresh({"pending_at": 30001, "monotonic_at": 10})


def test_watch_assesses_the_agent_and_base_role(tmp_path: Path) -> None:
    row = _session(tmp_path).model_copy(update={"model": "gpt-native", "agent": "codex"})
    assert "off-ladder" not in _session_lines([row]).plain
    assert "off-ladder" in _session_lines([row.model_copy(update={"agent": "claude-code"})]).plain


def test_stalled_receiver_recovers_and_exits_with_owner(tmp_path: Path) -> None:
    directory = tmp_path / "receiver"
    directory.mkdir()
    ready = directory / "ready.json"
    owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    receiver = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "aisquare.services.native_telemetry",
            "--ready",
            str(ready),
            "--owner-pid",
            str(owner.pid),
            "--launch-id",
            "socket-test",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    stalled: socket.socket | None = None
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline and receiver.poll() is None:
            time.sleep(0.02)
        assert ready.exists(), "receiver never became ready"
        state = json.loads(ready.read_text())
        stalled = socket.create_connection(("127.0.0.1", state["port"]), timeout=2)
        stalled.sendall(
            (
                "POST /v1/logs HTTP/1.1\r\nHost: localhost\r\n"
                f"Authorization: Bearer {state['token']}"
                "\r\nContent-Length: 500\r\n\r\n{"
            ).encode()
        )
        request = urllib.request.Request(
            f"http://127.0.0.1:{state['port']}/v1/logs",
            data=b"{}",
            headers={"Authorization": f"Bearer {state['token']}"},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            assert response.status == 200
        owner.terminate()
        owner.wait(timeout=3)
        receiver.wait(timeout=3)
        assert not directory.exists()
    finally:
        if stalled is not None:
            stalled.close()
        for process in (owner, receiver):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)


def test_account_hook_failure_keeps_the_successful_login(tmp_path: Path) -> None:
    from aisquare.core.claude_accounts import default_account
    from aisquare.services import claude_accounts

    settings = tmp_path / "settings.json"
    settings.write_text('{"hooks": []}')
    (tmp_path / ".claude.json").write_text(
        '{"oauthAccount": {"emailAddress": "fixture@example.com"}}'
    )
    status = claude_accounts.complete_sign_in(default_account({"CLAUDE_CONFIG_DIR": str(tmp_path)}))
    assert not status.hooks_installed
    assert "Signed in; hook setup failed" in status.detail
    assert "Repair" in status.detail or "repair" in status.detail
    assert settings.read_text() == '{"hooks": []}'
    assert (tmp_path / ".claude.json").exists()


def test_failed_nested_write_rolls_back_even_if_the_caller_catches_it(tmp_path: Path) -> None:
    with store_session() as store, store.transaction():
        store.set_meta("outer", "keep")
        with pytest.raises(RuntimeError), store.transaction():
            store.set_meta("inner", "rollback")
            raise RuntimeError("nested failure")
    with store_session() as store:
        assert store.get_meta("outer") == "keep"
        assert store.get_meta("inner") is None


def test_harness_matrix_reuses_account_and_cache_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import Mock

    from aisquare.core import claude_accounts

    identity = Mock(wraps=claude_accounts.identity)
    cache = Mock(wraps=harness._read_cache)
    monkeypatch.setattr(claude_accounts, "identity", identity)
    monkeypatch.setattr(harness, "_read_cache", cache)
    with agent_launch.selection_snapshot():
        for role in harness.ROLE_PROFILES:
            agent_launch.model_for(
                agent_launch.resolve(role, agent="claude-code"), role, probe=False
            )
    assert identity.call_count == 1
    assert cache.call_count == 1
    # The next command sees any account/config changes.
    with agent_launch.selection_snapshot():
        agent_launch.model_for(agent_launch.resolve(agent="claude-code"), "coder", probe=False)
    assert identity.call_count == 2


def test_doctor_parses_each_native_settings_file_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import Mock

    from aisquare.services import diagnostics

    home = tmp_path / ".codex"
    home.mkdir()
    connect("codex", home)
    agent_launch.use("codex")
    reader = Mock(wraps=agents._read_settings_file)
    monkeypatch.setattr(agents, "_read_settings_file", reader)
    diagnostics._check_other_agents(tmp_path)
    assert sum(call.args[0] == home / "hooks.json" for call in reader.call_args_list) == 1


def test_fleet_native_prompt_does_not_become_a_label(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import Mock

    # Record the real CLI dispatch before any tmux mutation.
    operation = Mock(side_effect=fleet.FleetError("recorded"))
    monkeypatch.setattr(fleet, "spawn", operation)
    result = runner.invoke(
        app, ["fleet", "spawn", "coder", "--agent", "codex", "exec", "-list every file"]
    )
    assert result.exit_code != 0
    assert operation.call_count == 1
    assert operation.call_args.kwargs["label"] is None
    assert operation.call_args.kwargs["agent_args"] == ["exec", "-list every file"]


@pytest.mark.parametrize("groups", [None, 42, {}, "foreign"])
def test_malformed_native_hook_groups_are_preserved(groups: object, tmp_path: Path) -> None:
    settings = tmp_path / "hooks.json"
    settings.write_text(json.dumps({"hooks": {"Stop": groups}}))
    original = settings.read_bytes()
    for operation in (connect, disconnect):
        with pytest.raises(agents.AgentSettingsError, match="must be an array"):
            operation("codex", tmp_path)
        assert settings.read_bytes() == original
    assert agents.integration_readiness("codex", tmp_path)[0] == "unreadable"


def test_session_start_prunes_orphans_without_model_shipping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aisquare.core.agent_sessions import bind_launch_session

    with store_session() as store:
        store.set_meta("agent-event:orphan:Stop:old:False", '"complete"')
        assert isinstance(store, SqliteStore)
        store._conn.execute("UPDATE team_meta SET updated_at = 1")
        store._conn.commit()
        bind_launch_session(store, "new-session", started=True)
        assert store.get_meta("agent-event:orphan:Stop:old:False") is None


def test_active_pane_keeps_seen_history_for_its_previous_threads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aisquare.core.agent_sessions import bind_launch_session

    monkeypatch.setenv("AISQUARE_LAUNCH_ID", "active-pane")
    with store_session() as store:
        store.ensure_project(team_project(tmp_path))
        store.upsert_session(_session(tmp_path, "current"))
        bind_launch_session(store, "previous", started=True)
        bind_launch_session(store, "current", started=True)
        assert isinstance(store, SqliteStore)
        store._conn.execute("UPDATE team_meta SET updated_at = 1")
        store._conn.commit()
        store.expire_native_launches(time.time() - 86400)
        bind_launch_session(store, "previous", started=True)
        assert store.get_meta("launch-session:active-pane") == "current"


def test_probe_notice_waits_for_validation_and_an_actual_probe(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import Mock

    probe = Mock(
        return_value=harness.ProbeResult(
            alias="sonnet", available=True, checked_at=datetime.now(UTC)
        )
    )
    monkeypatch.setattr(harness, "probe_model", probe)
    failed = runner.invoke(app, ["team", "spawn", "coder", "--probe", "--effort", "turbo"])
    assert failed.exit_code != 0 and "use one of" in failed.output
    assert "probing model" not in failed.output
    probe.assert_not_called()
    passed = runner.invoke(app, ["team", "spawn", "coder", "--probe"])
    assert passed.exit_code == 0, passed.output
    assert "probing model" in passed.stderr
    assert probe.call_count == 1


def test_fleet_carries_the_resolved_family_into_a_legacy_wrapper_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    from unittest.mock import Mock

    config = load_config()
    config.team.profiles["coder"] = RoleLaunchProfile(bin="legacy-wrapper")
    config.fleet.roles["coder"] = FleetRoleSettings(worktree=False)
    save_config(config)
    tmux = FakeTmux()
    monkeypatch.setattr(fleet, "server", lambda config: tmux)
    monkeypatch.setattr(agent_launch, "executable", lambda selected: selected.binary.binary)
    fleet.spawn(team_project(tmp_path), "coder")
    window = tmux.spawned[-1]
    argv = window["command"]
    env = window["env"]
    assert isinstance(argv, list) and isinstance(env, dict)
    execute = Mock()
    monkeypatch.setattr(import_module("aisquare.cli.launch"), "_exec", execute)
    result = runner.invoke(app, argv[argv.index("launch") :], env=env)
    assert result.exit_code == 0, result.output
    assert execute.call_count == 1
    assert execute.call_args.args[1][0] == "legacy-wrapper"
