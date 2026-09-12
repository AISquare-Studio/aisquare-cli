"""Ownership and failure boundaries shared by terminal-agent entry points."""

from __future__ import annotations

import json
import os
import shlex
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import agents, insights, outbox
from aisquare.core.agent_sessions import (
    METADATA_PRUNE_INTERVAL,
    NATIVE_METADATA_TTL,
    adopt_local_session,
    bind_launch_session,
    provisional_id,
    prune_metadata,
)
from aisquare.core.config import (
    AgentModelSettings,
    FleetRoleSettings,
    RoleLaunchProfile,
    load_config,
    save_config,
)
from aisquare.core.orchestrator import team_project
from aisquare.core.store import SqliteStore, open_store, store_session
from aisquare.models import TeamEvent, TeamSession
from aisquare.services import agent_launch, diagnostics, fleet, native_telemetry
from tests.test_fleet_service import FakeTmux
from tests.test_store import _at_version


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agents, "_home", lambda: tmp_path)
    monkeypatch.setenv("AISQUARE_TEAM", "1")
    monkeypatch.setenv("AISQUARE_HARNESS_PROBE", "0")
    monkeypatch.setattr(agent_launch, "executable", lambda selected: selected.binary.binary)


@pytest.mark.parametrize("attached", [False, True])
def test_aisquare_short_options_keep_spaces_and_equals(
    attached: bool, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.core import TyperGroup
    from typer.main import get_command

    root = get_command(app)
    assert isinstance(root, TyperGroup)
    command = root.commands["launch"]
    pairs = [
        ("-c", "/opt/my wrappers/claude=v2"),
        ("-e", "GREETING=hello world"),
        ("-a", "alice smith"),
    ]
    args = (
        [option + value for option, value in pairs]
        if attached
        else [part for pair in pairs for part in pair]
    )
    with command.make_context("launch", ["coder", *args]) as ctx:
        assert ctx.params["command"] == pairs[0][1]
        assert ctx.params["env_pairs"] == (pairs[1][1],)
        assert ctx.params["account"] == pairs[2][1]
        assert ctx.args == []
    execute = Mock()
    monkeypatch.setattr(import_module("aisquare.cli.launch"), "_exec", execute)
    result = runner.invoke(
        app, ["launch", "coder", "--agent", "claude-code", *args[: -1 if attached else -2]]
    )
    assert result.exit_code == 0, result.output
    assert execute.call_args.args[1] == [pairs[0][1]]
    assert execute.call_args.args[2]["GREETING"] == "hello world"


@pytest.mark.parametrize(
    "prompt", ["-list every file", "-listfiles", "-lsfiles", "--label=from-native"]
)
def test_fleet_native_command_owns_all_following_tokens(
    prompt: str, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    operation = Mock(side_effect=fleet.FleetError("captured before tmux"))
    monkeypatch.setattr(fleet, "spawn", operation)
    result = runner.invoke(
        app, ["fleet", "spawn", "coder", "--agent", "codex", "-lmy pane", "exec", prompt]
    )
    assert result.exit_code != 0
    assert operation.call_args.kwargs["label"] == "my pane"
    assert operation.call_args.kwargs["agent_args"] == ["exec", prompt]


@pytest.mark.parametrize("parent_family", ["claude-code", "codex"])
@pytest.mark.parametrize("entry", ["launch", "team", "fleet"])
def test_legacy_wrapper_is_the_same_inside_a_managed_pane(
    parent_family: str,
    entry: str,
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = load_config()
    cfg.team.profiles["coder2"] = RoleLaunchProfile(bin="legacy-wrapper")
    cfg.fleet.roles["coder2"] = FleetRoleSettings(worktree=False)
    save_config(cfg)
    baseline = agent_launch.resolve("coder2")
    monkeypatch.setenv("AISQUARE_CODING_AGENT", parent_family)
    monkeypatch.setenv("AISQUARE_LAUNCH_ID", "parent-launch")
    nested = agent_launch.resolve("coder2")
    assert nested.adapter.id == baseline.adapter.id == "claude-code"
    execute = Mock()
    monkeypatch.setattr(import_module("aisquare.cli.launch"), "_exec", execute)
    tmux = FakeTmux()
    monkeypatch.setattr(fleet, "server", lambda config: tmux)
    args = ["launch", "coder2"] if entry == "launch" else [entry, "spawn", "coder2"]
    result = runner.invoke(app, ["--json", *args])
    assert result.exit_code == 0, result.output
    if entry == "launch":
        assert execute.call_args.args[1][0] == "legacy-wrapper"
    elif entry == "team":
        assert json.loads(result.stdout)["binary"] == "legacy-wrapper"
    else:
        command = tmux.spawned[-1]["command"]
        assert isinstance(command, list) and "legacy-wrapper" in command


@pytest.mark.parametrize("native_effort", ['"turbo"', '""', "123", '"max"'])
def test_native_config_effort_is_forwarded_without_aisquare_validation(
    native_effort: str, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = load_config()
    cfg.agents.models["codex"] = AgentModelSettings(model="configured", effort="invalid-default")
    cfg.fleet.roles["coder"] = FleetRoleSettings(worktree=False)
    save_config(cfg)
    native = ["-c", "model=operator", "-c", f"model_reasoning_effort={native_effort}"]
    execute = Mock()
    monkeypatch.setattr(import_module("aisquare.cli.launch"), "_exec", execute)
    result = runner.invoke(app, ["launch", "coder", "--agent", "codex", "--", *native])
    assert result.exit_code == 0, result.output
    assert execute.call_args.args[1] == ["codex", *native]
    result = runner.invoke(
        app,
        [
            "--json",
            "team",
            "spawn",
            "coder",
            "--agent",
            "codex",
            "--effort",
            "high",
            *[part for value in native for part in ("--arg", value)],
        ],
    )
    assert result.exit_code == 0, result.output
    row = json.loads(result.stdout)
    assert row["source"] == row["effort_source"] == "native"
    assert row["notes"] and "takes precedence" in result.stderr
    assert "invalid-default" not in row["command"]
    tmux = FakeTmux()
    monkeypatch.setattr(fleet, "server", lambda config: tmux)
    fleet.spawn(team_project(tmp_path), "coder", agent="codex", agent_args=native)
    command = tmux.spawned[-1]["command"]
    assert isinstance(command, list) and command[-len(native) :] == native


@pytest.mark.parametrize(
    "family,args",
    [
        ("claude-code", ["--model=opus"]),
        ("codex", ["-c", "model=gpt-5-codex", "-c", "model_reasoning_effort=turbo"]),
    ],
)
def test_harness_and_spawn_report_the_same_native_selection(
    family: str, args: list[str], runner: CliRunner
) -> None:
    cfg = load_config()
    cfg.team.profiles["coder"] = RoleLaunchProfile(agent=family, args=args)
    save_config(cfg)
    matrix = runner.invoke(app, ["--json", "team", "harness"])
    launched = runner.invoke(app, ["--json", "team", "spawn", "coder"])
    assert matrix.exit_code == launched.exit_code == 0, matrix.output + launched.output
    row = next(row for row in json.loads(matrix.stdout)["roles"] if row["role"] == "coder")
    spawn = json.loads(launched.stdout)
    assert (
        row["resolves_to"]
        == spawn["model"]
        == ("opus" if family == "claude-code" else "gpt-5-codex")
    )
    assert row["source"] == spawn["source"] == "native"
    assert row["effort"] == spawn["effort"]


def test_dash_prefixed_native_input_is_not_reported_as_a_pinned_model(runner: CliRunner) -> None:
    result = runner.invoke(
        app,
        ["--json", "team", "spawn", "coder", "--agent", "codex", "--arg", "-migrate the schema"],
    )
    assert result.exit_code == 0, result.output
    row = json.loads(result.stdout)
    assert row["model"] == "" and row["source"] == "native"
    assert "'-migrate the schema'" in row["command"]


def _payload() -> dict[str, object]:
    return {
        "resourceLogs": [
            {
                "scopeLogs": [
                    {
                        "logRecords": [
                            {
                                "timeUnixNano": str(i),
                                "eventName": "codex.sse_event",
                                "attributes": [
                                    {"key": "input_token_count", "value": {"intValue": i}}
                                ],
                            }
                            for i in range(1, 4)
                        ]
                    }
                ]
            }
        ]
    }


@pytest.mark.parametrize("failure", ["spool", "sanitize", "enqueue-none"])
def test_capture_retry_keeps_the_durable_prefix_and_does_not_lock_the_spool(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = load_config()
    cfg.explainability.enabled = cfg.explainability.ship = True
    save_config(cfg)
    insights.reset_cache()
    enqueue = outbox.enqueue
    outbound = insights._outbound
    calls = 0

    def spool(record: dict[str, object]) -> Path | None:
        nonlocal calls
        calls += 1
        # A separate writer must remain available while a file is queued.
        with store_session() as other:
            other.set_meta("test:independent-writer", str(calls))
        if failure == "spool" and calls == 2:
            raise OSError("fixture: out of space")
        if failure == "enqueue-none" and calls == 2:
            return None
        return enqueue(record)

    def sanitize(value: str) -> str:
        if failure == "sanitize" and calls == 1:
            raise ValueError("fixture: malformed event")
        return outbound(value)

    monkeypatch.setattr(outbox, "enqueue", spool)
    monkeypatch.setattr(insights, "_outbound", sanitize)
    with pytest.raises((OSError, ValueError), match=r"fixture|could not be queued"):
        native_telemetry.capture(_payload(), "batch")
    assert len(outbox.pending()) == 1
    monkeypatch.setattr(outbox, "enqueue", enqueue)
    monkeypatch.setattr(insights, "_outbound", outbound)
    assert native_telemetry.capture(_payload(), "batch") == 2
    assert native_telemetry.capture(_payload(), "batch") == 0
    assert len(outbox.pending()) == 3
    with store_session() as store:
        assert len(store.list_meta("native-event:batch:")) == 3
        assert store.get_meta("native-launch:batch") is not None


def test_settings_aliases_share_invalidation(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    original = {"foreign": "before"}
    updated = {"foreign": "after"}
    path = real / "settings.json"
    path.write_text(json.dumps(original))
    with agents.inspection_snapshot():
        assert agents._read_settings(alias / path.name) == original
        agents._write_settings(real / ".." / "real" / path.name, updated)
        assert agents._read_settings(alias / path.name) == updated
        assert agents._read_settings(path) == updated


def test_receiver_requests_retry_for_partial_spool_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = load_config()
    cfg.explainability.enabled = cfg.explainability.ship = True
    save_config(cfg)
    insights.reset_cache()
    stop = threading.Event()
    kill = os.kill

    def owner_alive(pid: int, signal: int) -> None:
        if pid != -1234:
            return kill(pid, signal)
        if stop.is_set():
            raise ProcessLookupError

    monkeypatch.setattr(os, "kill", owner_alive)
    enqueue = outbox.enqueue
    calls = 0

    def spool(record: dict[str, object]) -> Path | None:
        nonlocal calls
        calls += 1
        return None if calls == 2 else enqueue(record)

    monkeypatch.setattr(outbox, "enqueue", spool)
    directory = tmp_path / "receiver"
    directory.mkdir()
    ready = directory / "ready.json"
    thread = threading.Thread(
        target=native_telemetry.serve, args=(ready, -1234, "retry"), daemon=True
    )
    thread.start()
    try:
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        state = json.loads(ready.read_text())
        request = urllib.request.Request(
            f"http://127.0.0.1:{state['port']}/v1/logs",
            data=json.dumps(_payload()).encode(),
            headers={
                "Authorization": f"Bearer {state['token']}",
                "Content-Type": "application/json",
            },
        )
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=3)
        assert error.value.code == 503
        error.value.close()
        assert len(outbox.pending()) == 1
        with urllib.request.urlopen(request, timeout=3) as response:
            assert response.status == 200
        assert len(outbox.pending()) == 3 and calls == 4
    finally:
        stop.set()
        thread.join(timeout=3)
    assert not thread.is_alive() and not directory.exists()


def test_reordered_hook_options_remain_owned_and_reconnect_without_duplicates(
    tmp_path: Path,
) -> None:
    agents.install_hooks("codex", tmp_path)
    fingerprint = agents.hook_fingerprint("codex", tmp_path)
    path = tmp_path / "hooks.json"
    settings = json.loads(path.read_text())
    for groups in settings["hooks"].values():
        for group in groups:
            handler = group["hooks"][0]
            parts = shlex.split(handler["command"])
            parts[-4:] = [*parts[-2:], *parts[-4:-2]]
            handler["command"] = shlex.join(parts)
            assert agents.hook_binary(handler["command"]) is not None
    path.write_text(json.dumps(settings))
    assert agents.hook_fingerprint("codex", tmp_path) == fingerprint
    agents.install_hooks("codex", tmp_path)
    assert all(len(groups) == 1 for groups in json.loads(path.read_text())["hooks"].values())
    assert agents.remove_hooks("codex", tmp_path)
    assert "hooks" not in json.loads(path.read_text())


def _native(project_id: str, name: str) -> TeamSession:
    return TeamSession(
        id=name, project_id=project_id, started_at=datetime.now(UTC), last_seen_at=datetime.now(UTC)
    )


def test_ended_provisional_work_can_be_adopted_only_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_LAUNCH_ID", "adoption")
    project = team_project(tmp_path)
    provisional = provisional_id(project.id, "launch", "adoption")
    with store_session() as store:
        store.ensure_project(project)
        store.upsert_session(_native(project.id, provisional))
        store.upsert_session(_native(project.id, "native"))
        store.add_team_event(
            TeamEvent(
                id="event",
                project_id=project.id,
                kind="note",
                text="pre-hook work",
                session_id=provisional,
                created_at=datetime.now(UTC),
            )
        )
        store.end_session(provisional)
        adopt_local_session(store, _native(project.id, "native"))
        assert store.get_meta(f"session-alias:{provisional}") == "native"
        assert store.recent_events(project.id)[0].session_id == "native"
        store.upsert_session(_native(project.id, "later"))
        assert isinstance(store, SqliteStore)
        queries: list[str] = []
        store._conn.set_trace_callback(queries.append)
        adopt_local_session(store, _native(project.id, "later"))
        assert not any(sql.startswith("BEGIN") for sql in queries)
        assert store.get_meta(f"session-alias:{provisional}") == "native"


def test_tokenless_sessions_skip_maintenance_and_launched_sessions_share_its_cadence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[float] = []
    original = SqliteStore.expire_native_launches

    def record(store: SqliteStore, before: float) -> int:
        calls.append(before)
        return original(store, before)

    monkeypatch.setattr(SqliteStore, "expire_native_launches", record)
    moment = time.time()
    monkeypatch.setattr("aisquare.core.agent_sessions.time.time", lambda: moment)
    with store_session() as store:
        assert isinstance(store, SqliteStore)
        queries: list[str] = []
        store._conn.set_trace_callback(queries.append)
        bind_launch_session(store, "plain", started=True)
        assert not queries and not calls
    monkeypatch.setenv("AISQUARE_LAUNCH_ID", "managed")
    for session_id in ("first", "second"):
        with store_session() as store:
            bind_launch_session(store, session_id, started=True)
    assert len(calls) == 1
    with store_session() as store:
        assert isinstance(store, SqliteStore)
        queries = []
        store._conn.set_trace_callback(queries.append)
        prune_metadata(store)
        assert not any(sql.startswith("BEGIN") for sql in queries)
    moment += METADATA_PRUNE_INTERVAL + 1
    with store_session() as store:
        bind_launch_session(store, "third", started=True)
    assert len(calls) == 2


@pytest.mark.parametrize("version", [15, 16])
def test_upgrade_gives_existing_metadata_a_full_retention_period(version: int) -> None:
    db = _at_version(version)
    with sqlite3.connect(db) as raw:
        raw.execute(
            "INSERT INTO team_meta (key, value) VALUES ('fleet-session:weekend', 'weekend')"
        )
        if version == 16:
            raw.execute("UPDATE team_meta SET updated_at = 0")
    before = int(time.time())
    store = open_store()
    try:
        assert isinstance(store, SqliteStore)
        stamp = store._conn.execute(
            "SELECT updated_at FROM team_meta WHERE key = 'fleet-session:weekend'"
        ).fetchone()[0]
        assert stamp >= before
        store.expire_native_launches(time.time() - NATIVE_METADATA_TTL)
        assert store.get_meta("fleet-session:weekend") == "weekend"
    finally:
        store.close()


@pytest.mark.parametrize("parent_launch", ["", "inherited-launch"])
@pytest.mark.parametrize("pipeline", ["", "inherited-pipeline"])
def test_pasted_spawn_and_launch_agree_on_fleet_bootstrap(
    parent_launch: str, pipeline: str, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_LAUNCH_ID", parent_launch)
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", "fleet-pane")
    monkeypatch.setenv("AISQUARE_PIPELINE_ID", pipeline)
    result = runner.invoke(app, ["--json", "team", "spawn", "coder", "--no-probe"])
    assert result.exit_code == 0, result.output
    command = json.loads(result.stdout)["command"]
    prelude = command[: command.index("AISQUARE_ROLE=")]
    child = subprocess.run(["sh", "-c", prelude + "env"], capture_output=True, text=True, timeout=3)
    assert child.returncode == 0
    child_env = dict(line.split("=", 1) for line in child.stdout.splitlines() if "=" in line)
    env = dict(os.environ)
    agent_launch.launch_identity(env, agent_launch.resolve(agent="claude-code"), None)
    assert (
        child_env.get("AISQUARE_FLEET_AGENT")
        == env.get("AISQUARE_FLEET_AGENT")
        == (None if parent_launch else "fleet-pane")
    )
    assert "AISQUARE_PIPELINE_ID" not in child_env


def test_doctor_uses_the_selected_project_and_one_preference_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected_project = tmp_path / "selected-project"
    selected_project.mkdir()
    (tmp_path / ".claude").mkdir()
    agent_launch.use("codex", project=True, cwd=selected_project)
    assert agent_launch.resolve().adapter.id == "claude-code"
    reads: list[str] = []
    original = SqliteStore.get_meta

    def meta(store: SqliteStore, key: str) -> str | None:
        if key.startswith("coding-agent:"):
            reads.append(key)
        return original(store, key)

    monkeypatch.setattr(SqliteStore, "get_meta", meta)
    # Keep the project's real adapter gate and snapshot in the full doctor
    # orchestration. Other diagnostics are unrelated and may spawn subprocesses.
    for name in vars(diagnostics):
        if name.startswith("_check_") and name != "_check_claude_code":
            monkeypatch.setattr(
                diagnostics, name, lambda *args, **kwargs: diagnostics._ok("fixture", "isolated")
            )
    monkeypatch.setattr(diagnostics, "_check_other_agents", lambda *args: [])
    monkeypatch.setattr(diagnostics, "_claude_accounts_checks", lambda: [])
    monkeypatch.setattr(diagnostics, "_experiment_checks", lambda: [])
    monkeypatch.setattr("aisquare.services.explainability_ops.checks", lambda **kwargs: [])
    checks = diagnostics.doctor(cwd=selected_project)
    claude = next(check for check in checks if check.name == "claude-code")
    assert claude.detail == "Claude Code detected but not selected"
    assert reads == [f"coding-agent:{team_project(selected_project).id}"]


def test_legacy_unverified_hooks_get_a_working_reconnect_instruction(tmp_path: Path) -> None:
    directory = tmp_path / ".codex"
    agents.install_hooks("codex", directory)
    path = directory / "hooks.json"
    settings = json.loads(path.read_text())
    for groups in settings["hooks"].values():
        for group in groups:
            handler = group["hooks"][0]
            handler["command"] = shlex.join(shlex.split(handler["command"])[:-2])
    path.write_text(json.dumps(settings))
    agent_launch.use("codex")
    check = next(
        check
        for check in diagnostics._check_other_agents(tmp_path)
        if check.name == "codex" and "unverified" in check.detail
    )
    assert check.fix and f"agents connect codex --config-dir {directory}" in check.fix
    assert "/hooks" in check.fix
    agents.install_hooks("codex", directory)
    settings = json.loads(path.read_text())
    parts = shlex.split(settings["hooks"]["SessionStart"][0]["hooks"][0]["command"])
    agents.observe_hooks("codex", directory, parts[parts.index("--definition") + 1])
    assert agents.integration_readiness("codex", directory)[0] == "observed"


def test_codex_compatibility_effort_mapping_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    cfg = load_config()
    cfg.agents.default = "codex"
    cfg.agents.models["codex"] = AgentModelSettings(effort="max")
    cfg.fleet.roles["coder"] = FleetRoleSettings(worktree=False)
    save_config(cfg)
    monkeypatch.setattr(import_module("aisquare.cli.launch"), "_exec", Mock())
    for args in (["launch", "coder"], ["team", "spawn", "coder"], ["team", "harness"]):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.output
        assert "maps 'max' to native reasoning effort 'xhigh'" in result.output
    tmux = FakeTmux()
    monkeypatch.setattr(fleet, "server", lambda config: tmux)
    receipt = fleet.spawn(team_project(tmp_path), "coder")
    assert any("maps 'max'" in note for note in receipt.notes)
