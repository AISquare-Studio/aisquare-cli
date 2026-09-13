"""Ownership and failure boundaries shared by terminal-agent entry points."""

from __future__ import annotations

import errno
import json
import os
import shlex
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
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


@pytest.mark.parametrize("parent_launch", [None, "", "parent-launch"])
@pytest.mark.parametrize("parent_family", ["claude-code", "codex"])
@pytest.mark.parametrize("entry", ["launch", "team", "fleet"])
def test_legacy_wrapper_is_the_same_inside_a_managed_pane(
    parent_family: str,
    parent_launch: str | None,
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
    monkeypatch.setenv("AISQUARE_LAUNCH_AGENT", parent_family)
    if parent_launch is not None:
        monkeypatch.setenv("AISQUARE_LAUNCH_ID", parent_launch)
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
    expected_native = agent_launch.resolve(agent="codex").adapter.native_args(native)
    assert execute.call_args.args[1] == ["codex", *expected_native]
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
    assert isinstance(command, list) and command[-len(native) :] == expected_native


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


def test_native_separator_keeps_dash_prefixed_prompt_and_configured_pin(runner: CliRunner) -> None:
    cfg = load_config()
    cfg.agents.models["codex"] = AgentModelSettings(model="configured")
    save_config(cfg)
    result = runner.invoke(
        app,
        [
            "--json",
            "team",
            "spawn",
            "coder",
            "--agent",
            "codex",
            "--arg",
            "--",
            "--arg",
            "-migrate the schema",
        ],
    )
    assert result.exit_code == 0, result.output
    row = json.loads(result.stdout)
    assert row["model"] == "configured" and row["source"] == "configured"
    assert "--model configured" in row["command"]
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


@pytest.mark.parametrize("failure", ["spool", "sanitize"])
def test_capture_retry_keeps_the_durable_prefix_and_does_not_lock_the_spool(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = load_config()
    cfg.explainability.enabled = cfg.explainability.ship = True
    save_config(cfg)
    insights.reset_cache()
    enqueue = outbox.enqueue_retryable
    outbound = insights._outbound
    calls = 0

    def spool(record: dict[str, object], *, filename: str | None = None) -> Path | None:
        nonlocal calls
        calls += 1
        # A separate writer must remain available while a file is queued.
        with store_session() as other:
            other.set_meta("test:independent-writer", str(calls))
        if failure == "spool" and calls == 2:
            raise OSError("fixture: out of space")
        return enqueue(record, filename=filename)

    def sanitize(value: str) -> str:
        if failure == "sanitize" and calls == 1:
            raise ValueError("fixture: malformed event")
        return outbound(value)

    monkeypatch.setattr(outbox, "enqueue_retryable", spool)
    monkeypatch.setattr(insights, "_outbound", sanitize)
    with pytest.raises((OSError, ValueError), match=r"fixture|could not be queued"):
        native_telemetry.capture(_payload(), "batch")
    assert len(outbox.pending()) == 1
    monkeypatch.setattr(outbox, "enqueue_retryable", enqueue)
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
    enqueue = outbox.enqueue_retryable
    calls = 0

    def spool(record: dict[str, object], *, filename: str | None = None) -> Path | None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("fixture: out of space")
        return enqueue(record, filename=filename)

    monkeypatch.setattr(outbox, "enqueue_retryable", spool)
    directory = tmp_path / "receiver"
    directory.mkdir()
    ready = directory / "ready.json"
    thread = threading.Thread(
        target=native_telemetry.serve,
        args=(ready, os.getpid(), "retry"),
        kwargs={"owner_alive": lambda: not stop.is_set()},
        daemon=True,
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
        thread.join(timeout=10)
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


def test_all_native_sessions_share_the_maintenance_cadence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[float] = []
    original = SqliteStore.expire_native_launches

    def record(store: SqliteStore, before: float) -> int:
        calls.append(before)
        return original(store, before)

    monkeypatch.setattr(SqliteStore, "expire_native_launches", record)
    moment = time.time()
    monkeypatch.setattr("aisquare.core.agent_sessions._now", lambda: moment)
    with store_session() as store:
        assert isinstance(store, SqliteStore)
        queries: list[str] = []
        store._conn.set_trace_callback(queries.append)
        bind_launch_session(store, "plain", started=True)
        assert len(calls) == 1
        queries.clear()
        bind_launch_session(store, "plain2", started=True)
        assert len(queries) == 1 and not any(sql.startswith("BEGIN") for sql in queries)
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
    execute = Mock()
    monkeypatch.setattr(import_module("aisquare.cli.launch"), "_exec", execute)
    argv = shlex.split(command)
    launched = runner.invoke(app, argv[argv.index("launch") :])
    assert launched.exit_code == 0, launched.output
    child_env = execute.call_args.args[2]
    assert child_env.get("AISQUARE_FLEET_AGENT") == (None if parent_launch else "fleet-pane")
    assert child_env["AISQUARE_LAUNCH_ID"] != parent_launch
    assert child_env["AISQUARE_LAUNCH_AGENT"] == "claude-code"
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
    assert "/hooks" in check.detail
    assert subprocess.run(["sh", "-n", "-c", check.fix], timeout=3).returncode == 0
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


@pytest.mark.parametrize(
    "native",
    [
        ["--model", "opus"],
        ["--add-dir", "/repo"],
        ["--settings", "/settings"],
        ["--cd", "/repo"],
        ["--image", "/image.png"],
        ["--profile", "work"],
    ],
)
def test_native_option_values_cannot_swallow_aisquare_options(native: list[str]) -> None:
    from typer.core import TyperGroup
    from typer.main import get_command

    root = get_command(app)
    assert isinstance(root, TyperGroup)
    command = root.commands["launch"]
    with command.make_context(
        "launch",
        [
            "coder",
            *native,
            "-c",
            "/wrapper",
            "--agent",
            "claude-code",
            "--account",
            "2",
            "-e",
            "SECRET=fixture",
        ],
    ) as ctx:
        assert ctx.params["command"] == "/wrapper"
        assert ctx.params["agent"] == "claude-code"
        assert ctx.params["account"] == "2"
        assert ctx.params["env_pairs"] == ("SECRET=fixture",)
        assert ctx.args == native
    fleet_group = root.commands["fleet"]
    assert isinstance(fleet_group, TyperGroup)
    with fleet_group.commands["spawn"].make_context(
        "spawn", ["coder", *native, "--label", "mine"]
    ) as ctx:
        assert ctx.params["label"] == "mine"
        assert ctx.args == native


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("bad", ["", "bogus", "   "])
def test_claude_effort_validation_is_independent_of_spelling(
    native: bool,
    bad: str,
    runner: CliRunner,
) -> None:
    args = ["--arg", "--effort", "--arg", bad] if native else ["--effort", bad]
    result = runner.invoke(app, ["--json", "team", "spawn", "coder", *args])
    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["error"] == "bad_effort"


@pytest.mark.parametrize("effort", ["", "bogus"])
def test_native_codex_override_does_not_hide_invalid_explicit_effort(
    effort: str,
    runner: CliRunner,
) -> None:
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
            effort,
            "--arg=-c",
            "--arg=model_reasoning_effort=low",
        ],
    )
    assert result.exit_code == 1 and json.loads(result.stdout)["error"] == "bad_effort"


@pytest.mark.parametrize("alias", ["max", "ultracode"])
@pytest.mark.parametrize("option", ["-c", "--config", "--config=", "-cattached"])
def test_codex_native_effort_aliases_reach_the_executable(
    alias: str,
    option: str,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execute = Mock()
    monkeypatch.setattr(import_module("aisquare.cli.launch"), "_exec", execute)
    assignment = f'model_reasoning_effort="{alias}"'
    args = (
        ["-c" + assignment]
        if option == "-cattached"
        else [option + assignment]
        if option.endswith("=")
        else [option, assignment]
    )
    result = runner.invoke(app, ["launch", "coder", "--agent", "codex", "--", *args])
    assert result.exit_code == 0, result.output
    assert f"maps '{alias}'" in result.output
    from aisquare.core.agent_adapters.types import model_overrides

    assert model_overrides("codex", execute.call_args.args[1][1:]) == (None, "xhigh")


def test_native_selection_has_no_probe_credit_or_blank_effort_banner(runner: CliRunner) -> None:
    result = runner.invoke(
        app, ["--json", "team", "spawn", "planner", "--arg=--model", "--arg=opus"]
    )
    assert result.exit_code == 0, result.output
    row = json.loads(result.stdout)
    assert row["source"] == "native" and row["skipped"] == []
    result = runner.invoke(
        app,
        [
            "--json",
            "team",
            "spawn",
            "coder",
            "--agent=codex",
            "--arg=-c",
            '--arg=model_reasoning_effort="   "',
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["effort"] == ""


def test_launch_family_never_overwrites_operator_preference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(agent_launch.ACTIVE_AGENT_ENV, "codex")
    selected = agent_launch.resolve(agent="claude-code")
    env = dict(os.environ)
    agent_launch.launch_identity(env, selected, None)
    assert env[agent_launch.ACTIVE_AGENT_ENV] == "codex"
    assert env[agent_launch.LAUNCH_AGENT_ENV] == "claude-code"
    for launch_id in ("", "owned"):
        monkeypatch.setenv("AISQUARE_LAUNCH_ID", launch_id)
        monkeypatch.setenv(agent_launch.LAUNCH_AGENT_ENV, "claude-code")
        with pytest.raises(agent_launch.UnknownWrapperError):
            agent_launch.resolve(binary="legacy-wrapper")


def test_resumed_binding_survives_pruning_and_old_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AISQUARE_LAUNCH_ID", "resume")
    with store_session() as store:
        bind_launch_session(store, "old", started=True)
        bind_launch_session(store, "current", started=True)
        assert isinstance(store, SqliteStore)
        store._conn.execute("UPDATE team_meta SET updated_at = 1")
        store._conn.commit()
        store.delete_meta("native-maintenance:pruned-at")
        bind_launch_session(store, "current", started=True)
        assert store.get_meta("launch-session:resume") == "current"
        # Retained replay history must not steal the refreshed current binding.
        assert store.get_meta("launch-seen:resume:old") == "1"
        bind_launch_session(store, "old", started=True)
        assert store.get_meta("launch-session:resume") == "current"
        store.delete_meta("launch-session:resume")
        bind_launch_session(store, "current", started=True)
        assert store.get_meta("launch-session:resume") == "current"


def test_unknown_retention_timestamps_are_not_the_epoch() -> None:
    with store_session() as store:
        for key in ("launch-session:pane", "fleet-session:pane", "agent-event:restored"):
            store.set_meta(key, "restored")
        assert isinstance(store, SqliteStore)
        store._conn.execute("UPDATE team_meta SET updated_at = 0")
        store._conn.commit()
        store.expire_native_launches(time.time() - NATIVE_METADATA_TTL)
        assert store.get_meta("launch-session:pane") == "restored"
        assert store.get_meta("fleet-session:pane") == "restored"
        assert store.get_meta("agent-event:restored") == "restored"


def test_waiting_pruner_samples_time_again_after_the_writer_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aisquare.core import agent_sessions

    waiting, release = threading.Event(), threading.Event()
    sampled = False
    moment = time.time()
    calls: list[float] = []
    original = SqliteStore.expire_native_launches

    def now() -> float:
        nonlocal sampled
        if threading.current_thread().name.startswith("delayed") and not sampled:
            sampled = True
            waiting.set()
            assert release.wait(5)
            return moment
        return moment + 1

    def prune(store: SqliteStore, before: float) -> int:
        calls.append(before)
        return original(store, before)

    def maintain() -> None:
        with store_session() as store:
            prune_metadata(store)

    with store_session():
        pass  # Complete migrations before the contenders start.
    monkeypatch.setattr(agent_sessions, "_now", now)
    monkeypatch.setattr(SqliteStore, "expire_native_launches", prune)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="delayed") as pool:
        future = pool.submit(maintain)
        try:
            assert waiting.wait(5)
            maintain()
        finally:
            release.set()
        future.result(timeout=5)
    assert calls == [moment + 1 - NATIVE_METADATA_TTL]


def test_capture_batches_durable_metadata_and_provider_updates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = load_config()
    cfg.explainability.enabled = cfg.explainability.ship = True
    save_config(cfg)
    insights.reset_cache()
    logs = [
        {
            "timeUnixNano": str(i),
            "attributes": [
                {"key": "provider_name", "value": {"stringValue": "local"}},
                {"key": "conversation.id", "value": {"stringValue": "thread"}},
            ],
        }
        for i in range(512)
    ]
    payload = {"resourceLogs": [{"scopeLogs": [{"logRecords": logs}]}]}
    statements: list[str] = []
    original = SqliteStore.__init__

    def connect(store: SqliteStore, *args: object, **kwargs: object) -> None:
        original(store, *args, **kwargs)  # type: ignore[arg-type]
        store._conn.set_trace_callback(statements.append)

    with store_session():
        pass
    monkeypatch.setattr(SqliteStore, "__init__", connect)
    assert native_telemetry.capture(payload, "batch") == 512
    assert statements.count("COMMIT") == 1
    # SQLite traces trigger invocations too; count API writes to the provider separately.
    assert len(outbox.pending()) == 512
    with store_session() as store:
        assert store.list_meta("native-provider:batch:") == {
            "native-provider:batch:thread": "local"
        }
    statements.clear()
    assert native_telemetry.capture(payload, "batch") == 0
    assert statements.count("COMMIT") == 1
    assert not any("INSERT" in sql and "native-provider:" in sql for sql in statements)


@pytest.mark.parametrize(
    "bad",
    [
        {"resourceLogs": 5},
        {"resourceLogs": [[]]},
    ],
)
def test_malformed_otlp_is_rejected_before_any_spool_write(bad: dict[str, object]) -> None:
    with pytest.raises(native_telemetry.InvalidPayload):
        native_telemetry.capture(bad, "invalid")
    assert outbox.pending() == []


@pytest.mark.parametrize("failure", [errno.EACCES, errno.EROFS, errno.ENOSPC])
def test_receiver_distinguishes_permanent_and_temporary_spool_failures(
    failure: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = load_config()
    cfg.explainability.enabled = cfg.explainability.ship = True
    save_config(cfg)
    original = Path.write_text

    def write(path: Path, *args: object, **kwargs: object) -> int:
        if path.parent == outbox.queue_dir():
            raise OSError(failure, "fixture spool failure")
        return original(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", write)
    stop = threading.Event()
    directory = tmp_path / "http"
    directory.mkdir()
    ready = directory / "ready.json"
    thread = threading.Thread(
        target=native_telemetry.serve,
        args=(ready, os.getpid(), "permanent"),
        kwargs={"owner_alive": lambda: not stop.is_set()},
        daemon=True,
    )
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        state = json.loads(ready.read_text())

        def post(payload: dict[str, object]) -> int:
            request = urllib.request.Request(
                f"http://127.0.0.1:{state['port']}/v1/logs",
                data=json.dumps(payload).encode(),
                headers={"Authorization": f"Bearer {state['token']}"},
            )
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    return int(response.status)
            except urllib.error.HTTPError as error:
                with error:
                    return error.code

        assert post(_payload()) == (503 if failure == errno.ENOSPC else 200)
        assert post({"resourceLogs": 5}) == 400
        assert post({"resourceLogs": [[]]}) == 400
        assert outbox.pending() == []
    finally:
        stop.set()
        thread.join(timeout=10)
    assert not thread.is_alive() and not directory.exists()


def test_harness_separately_reports_fleet_native_arguments(runner: CliRunner) -> None:
    cfg = load_config()
    cfg.fleet.roles["coder"] = FleetRoleSettings(agent_args={"claude-code": ["--model", "opus"]})
    save_config(cfg)
    result = runner.invoke(app, ["--json", "team", "harness"])
    assert result.exit_code == 0, result.output
    row = next(row for row in json.loads(result.stdout)["roles"] if row["role"] == "coder")
    assert row["resolves_to"] == "sonnet"
    assert row["fleet"]["resolves_to"] == "opus" and row["fleet"]["source"] == "native"


@pytest.mark.parametrize(
    "family,args",
    [
        ("codex", ["--", "-c", "model_reasoning_effort=max"]),
        ("claude-code", ["--", "--effort", "bogus"]),
    ],
)
def test_native_literal_boundary_excludes_validation_and_mapping(
    family: str,
    args: list[str],
) -> None:
    selected = agent_launch.resolve(agent=family)
    assert selected.adapter.native_args(args) == args
    resolved = agent_launch.model_for(selected, "coder", probe=False, raw_args=args)
    assert resolved and resolved.effort_source != "native"


@pytest.mark.parametrize("family", ["claude-code", "codex"])
@pytest.mark.parametrize("profile_args", [["--", "-migrate the schema"], ["--no-alt-screen"]])
def test_printed_spawn_preserves_the_executed_argument_order(
    family: str,
    profile_args: list[str],
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = load_config()
    cfg.team.profiles["coder"] = RoleLaunchProfile(agent=family, args=profile_args)
    cfg.agents.models["codex"] = AgentModelSettings(model="configured", effort="high")
    cfg.agents.mcp = True
    save_config(cfg)
    printed = runner.invoke(app, ["--json", "team", "spawn", "coder"])
    assert printed.exit_code == 0, printed.output
    command = shlex.split(json.loads(printed.stdout)["command"])
    execute = Mock()
    monkeypatch.setattr(import_module("aisquare.cli.launch"), "_exec", execute)
    launched = runner.invoke(app, command[command.index("launch") :])
    assert launched.exit_code == 0, launched.output
    launched_args = execute.call_args.args[1]
    native_exec = Mock()
    monkeypatch.setattr(os, "execvpe", native_exec)
    result = runner.invoke(app, ["team", "spawn", "coder", "--exec"])
    assert result.exit_code == 0, result.output
    assert launched_args == native_exec.call_args.args[1]
    assert launched_args.count("--model") == 1
