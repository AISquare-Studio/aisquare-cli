"""Regression contracts for native ownership and durable OTLP retries."""

from __future__ import annotations

import errno
import json
import os
import shlex
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from typer.main import get_command
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import agents, insights, outbox, paths
from aisquare.core.agent_adapters.types import model_overrides
from aisquare.core.config import (
    FleetRoleSettings,
    RoleLaunchProfile,
    load_config,
    save_config,
)
from aisquare.core.orchestrator import team_project
from aisquare.core.store import SqliteStore, store_session
from aisquare.services import agent_launch, diagnostics, fleet, native_telemetry
from tests.test_fleet_service import FakeTmux
from tests.test_store import _at_version


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agents, "_home", lambda: tmp_path)
    monkeypatch.setenv("AISQUARE_TEAM", "1")
    monkeypatch.setenv("AISQUARE_HARNESS_PROBE", "0")
    monkeypatch.setenv("AISQUARE_DB_BUSY_MS", "50")
    monkeypatch.setattr(agent_launch, "executable", lambda selected: selected.binary.binary)


def _payload(size: int = 5) -> dict[str, Any]:
    return {
        "resourceLogs": [
            {
                "scopeLogs": [
                    {
                        "logRecords": [
                            {
                                "timeUnixNano": str(i + 1),
                                "eventName": "codex.sse_event",
                                "attributes": [
                                    {"key": "input_token_count", "value": {"intValue": i}},
                                ],
                            }
                            for i in range(size)
                        ]
                    }
                ]
            }
        ]
    }


@pytest.fixture
def shipping() -> None:
    cfg = load_config()
    cfg.explainability.enabled = cfg.explainability.ship = True
    save_config(cfg)
    insights.reset_cache()
    with store_session():
        pass


@pytest.fixture
def receiver(tmp_path: Path, shipping: None) -> Iterator[Callable[[dict[str, Any]], int]]:
    stop = threading.Event()
    directory = tmp_path / "receiver"
    directory.mkdir()
    ready = directory / "ready.json"
    thread = threading.Thread(
        target=native_telemetry.serve,
        args=(ready, os.getpid(), "http"),
        kwargs={"owner_alive": lambda: not stop.is_set()},
        daemon=True,
    )
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        state = json.loads(ready.read_text())

        def post(payload: dict[str, Any]) -> int:
            request = urllib.request.Request(
                f"http://127.0.0.1:{state['port']}/v1/logs",
                data=json.dumps(payload).encode(),
                headers={"Authorization": f"Bearer {state['token']}"},
            )
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    return int(response.status)
            except urllib.error.HTTPError as exc:
                with exc:
                    return exc.code

        yield post
    finally:
        stop.set()
        thread.join(timeout=10)
        # A fixture teardown reports a cleanup error separately from any body
        # failure, rather than replacing it in the body's finally block.
        assert not thread.is_alive()
        assert not directory.exists()


@pytest.mark.parametrize(
    "tail",
    [
        ["write tests", "-a", "never"],
        ["explain", "-e", "K=V"],
        ["do the thing", "-c"],
        ["exec", "--", "-listfiles"],
        ["exec", "--sandbox", "danger-full-access"],
    ],
)
def test_native_positional_owns_the_remaining_launch_arguments(
    tail: list[str],
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execute = Mock()
    monkeypatch.setattr(import_module("aisquare.cli.launch"), "_exec", execute)
    result = runner.invoke(app, ["launch", "coder", "--agent", "codex", *tail])
    assert result.exit_code == 0, result.output
    assert execute.call_args.args[1] == ["codex", *tail]
    assert "K" not in execute.call_args.args[2]


@pytest.mark.parametrize("native", [["--model", "opus"], ["--add-dir", "/repo"]])
def test_native_value_then_prompt_opens_boundary_only_at_the_prompt(native: list[str]) -> None:
    command = get_command(app).commands["launch"]  # type: ignore[attr-defined]
    with command.make_context(
        "launch",
        ["coder", *native, "--agent", "codex", "exec", "--", "-c", "literal"],
    ) as ctx:
        assert ctx.params["agent"] == "codex"
        assert ctx.args == [*native, "exec", "--", "-c", "literal"]


def test_unknown_native_arity_cannot_silently_choose_the_wrong_binary(runner: CliRunner) -> None:
    result = runner.invoke(app, ["launch", "coder", "--future-option", "value", "--agent", "codex"])
    assert result.exit_code == 2
    assert "put AISquare options before it" in result.output


@pytest.mark.parametrize("entry", ["launch", "team", "fleet"])
@pytest.mark.parametrize("saved", [[], ["--", "literal prompt"]])
def test_complete_fleet_arguments_preserve_aliases_and_the_literal_boundary(
    entry: str,
    saved: list[str],
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg = load_config()
    profile = [*saved, "-c", "model_reasoning_effort=max"]
    cfg.team.profiles["coder"] = RoleLaunchProfile(agent="codex", args=profile)
    cfg.fleet.roles["coder"] = FleetRoleSettings(worktree=False)
    save_config(cfg)
    tmux = FakeTmux()
    monkeypatch.setattr(fleet, "server", lambda config: tmux)
    execute = Mock()
    monkeypatch.setattr(import_module("aisquare.cli.launch"), "_exec", execute)
    if entry == "fleet":
        fleet.spawn(team_project(tmp_path), "coder")
        command = tmux.spawned[-1]["command"]
        assert isinstance(command, list) and "--no-bound-args" in command
        result = runner.invoke(app, command[command.index("launch") :])
    elif entry == "team":
        printed = runner.invoke(app, ["--json", "team", "spawn", "coder"])
        assert printed.exit_code == 0, printed.output
        command = shlex.split(json.loads(printed.stdout)["command"])
        result = runner.invoke(app, command[command.index("launch") :])
    else:
        result = runner.invoke(app, ["launch", "coder"])
    assert result.exit_code == 0, result.output
    argv = execute.call_args.args[1]
    if saved:
        assert argv[argv.index("--") :] == profile
    else:
        assert model_overrides("codex", argv[1:])[1] == "xhigh"


def test_fleet_does_not_validate_fragments_after_creating_a_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = load_config()
    cfg.team.profiles["coder"] = RoleLaunchProfile(args=["--", "do the thing"])
    cfg.fleet.roles["coder"] = FleetRoleSettings(extra_args=["--effort", "turbo"])
    save_config(cfg)
    tmux = FakeTmux()
    monkeypatch.setattr(fleet, "server", lambda config: tmux)
    monkeypatch.setattr(fleet, "is_git_project", lambda root: True)
    tree = Mock(return_value=tmp_path)
    monkeypatch.setattr(fleet, "_ensure_worktree", tree)
    fleet.spawn(team_project(tmp_path), "coder", worktree=True)
    assert tree.call_count == 1 and len(tmux.spawned) == 1
    command = tmux.spawned[0]["command"]
    assert isinstance(command, list)
    native = command[command.index("--") + 1 :]
    assert native[native.index("--") :] == ["--", "do the thing", "--effort", "turbo"]
    assert native.index("--session-id") < native.index("--")
    tree.reset_mock()
    with pytest.raises(fleet.FleetError, match="sandbox/approval"):
        fleet.spawn(team_project(tmp_path), "coder", worktree=True, sandbox="read-only")
    assert not tree.called and len(tmux.spawned) == 1


@pytest.mark.parametrize("family", ["claude-code", "codex"])
def test_unbound_custom_role_printed_and_executed_commands_agree(
    family: str,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execute = Mock()
    monkeypatch.setattr(import_module("aisquare.cli.launch"), "_exec", execute)
    printed = runner.invoke(app, ["--json", "team", "spawn", "qa-lead", "--agent", family])
    assert printed.exit_code == 0, printed.output
    args = shlex.split(json.loads(printed.stdout)["command"])
    pasted = runner.invoke(app, args[args.index("launch") :])
    assert pasted.exit_code == 0, pasted.output
    argv = execute.call_args.args[1]
    monkeypatch.setattr(os, "execvpe", execute)
    spawned = runner.invoke(app, ["team", "spawn", "qa-lead", "--agent", family, "--exec"])
    assert spawned.exit_code == 0, spawned.output
    assert execute.call_args.args[1] == argv
    refused = runner.invoke(app, ["launch", "codr"])
    assert refused.exit_code == 1 and "unknown role" in refused.output


@pytest.mark.parametrize(
    "native",
    [
        ["--effort", "minimal"],
        ["--effort", "bogus", "--effort", "high"],
    ],
)
def test_native_claude_efforts_are_forwarded_and_last_value_wins(
    native: list[str],
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = load_config()
    cfg.team.profiles["coder"] = RoleLaunchProfile(args=["--effort", "obsolete"])
    save_config(cfg)
    execute = Mock()
    monkeypatch.setattr(import_module("aisquare.cli.launch"), "_exec", execute)
    result = runner.invoke(app, ["launch", "coder", "--", *native])
    assert result.exit_code == 0, result.output
    assert execute.call_args.args[1][-len(native) :] == native
    cfg.team.profiles["coder"].args = ["--effort", "bogus", "--effort", "high"]
    save_config(cfg)
    result = runner.invoke(app, ["--json", "team", "spawn", "coder"])
    assert result.exit_code == 0 and json.loads(result.stdout)["effort"] == "high"


@pytest.mark.parametrize("operator,owned", [("claude-code", "codex"), ("codex", "claude-code")])
def test_owned_family_precedes_operator_preference(
    operator: str,
    owned: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(agent_launch.ACTIVE_AGENT_ENV, operator)
    assert agent_launch.resolve().source == "operator"
    monkeypatch.setenv(agent_launch.LAUNCH_AGENT_ENV, owned)
    selected = agent_launch.resolve("reviewer")
    assert (selected.adapter.id, selected.source) == (owned, "inherited")


def test_explicit_claude_user_default_keeps_legacy_wrappers() -> None:
    cfg = load_config()
    cfg.team.profiles["coder"] = RoleLaunchProfile(bin="legacy-wrapper")
    save_config(cfg)
    before = agent_launch.resolve()
    agent_launch.use("claude-code")
    after = agent_launch.resolve()
    assert after.adapter.id == before.adapter.id == "claude-code"
    assert after.binary == before.binary
    agent_launch.use("codex")
    with pytest.raises(agent_launch.UnknownWrapperError):
        agent_launch.resolve()


def test_native_assignment_whitespace_is_normalized_in_real_launch(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execute = Mock()
    monkeypatch.setattr(import_module("aisquare.cli.launch"), "_exec", execute)
    result = runner.invoke(
        app,
        [
            "launch",
            "coder",
            "--agent",
            "codex",
            "-c",
            "model_reasoning_effort = max",
        ],
    )
    assert result.exit_code == 0, result.output
    assert execute.call_args.args[1] == ["codex", '--config=model_reasoning_effort="xhigh"']


def test_alias_equivalence_and_mapping_notes_are_preserved(runner: CliRunner) -> None:
    cfg = load_config()
    cfg.team.profiles["coder"] = RoleLaunchProfile(
        agent="codex",
        args=["-c", "model_reasoning_effort=ultracode"],
    )
    save_config(cfg)
    result = runner.invoke(app, ["--json", "team", "spawn", "coder", "--effort", "max"])
    assert result.exit_code == 0, result.output
    notes = json.loads(result.stdout)["notes"]
    assert any("maps 'max'" in note for note in notes)
    assert any("maps 'ultracode'" in note for note in notes)
    assert not any("takes precedence" in note for note in notes)


def test_deep_toml_is_left_for_native_validation(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    execute = Mock()
    monkeypatch.setattr(import_module("aisquare.cli.launch"), "_exec", execute)
    value = "model=" + "[" * 600 + "]" * 600
    result = runner.invoke(app, ["launch", "coder", "--agent", "codex", "--", "-c", value])
    assert result.exit_code == 0, result.output
    assert execute.call_args.args[1] == ["codex", "-c", value]


def test_harness_shows_differing_fleet_defaults_and_stable_error_shape(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = runner.invoke(app, ["team", "harness"])
    assert result.exit_code == 0, result.output
    assert "fleet: native default [native-default]" in result.output
    monkeypatch.setattr(fleet, "role_settings", Mock(side_effect=ValueError("bad fleet config")))
    result = runner.invoke(app, ["--json", "team", "harness"])
    assert result.exit_code == 0, result.output
    for row in json.loads(result.stdout)["roles"]:
        assert set(row["fleet"]) == {
            "agent_args",
            "resolves_to",
            "source",
            "effort",
            "effort_source",
            "notes",
            "error",
            "fix",
        }
        assert row["fleet"]["error"] == "bad fleet config"


@pytest.mark.parametrize("version", [15, 16, 17])
def test_dogfooded_schemas_reach_the_next_unused_migration(
    version: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aisquare.core import store as module

    database = _at_version(version)
    monkeypatch.setattr(
        module, "_MIGRATIONS", (*module._MIGRATIONS, "CREATE TABLE next_feature (id TEXT);")
    )
    with store_session() as store:
        assert isinstance(store, SqliteStore)
        assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 18
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT * FROM next_feature").fetchall() == []


def test_locked_board_does_not_duplicate_spooled_or_already_shipped_events(shipping: None) -> None:
    payload = _payload()
    with sqlite3.connect(paths.db_path()) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            native_telemetry.capture(payload, "locked")
        assert len(outbox.pending()) == 5
        for path in outbox.pending():
            claimed = outbox.claim(path)
            assert claimed is not None
            outbox.mark_sent(claimed)
        assert not outbox.pending()
        blocker.rollback()
    assert native_telemetry.capture(payload, "locked") == 0
    assert outbox.counts().sent == 5 and not outbox.pending()


def test_concurrent_receivers_publish_one_copy(shipping: None) -> None:
    barrier = threading.Barrier(2)

    def capture() -> int:
        barrier.wait(timeout=5)
        return native_telemetry.capture(_payload(32), "concurrent")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(capture), pool.submit(capture)]
        assert sorted(future.result(timeout=10) for future in futures) == [0, 32]
    assert len(outbox.pending()) == 32


@pytest.mark.parametrize(
    "prompt",
    [
        ["--session-id", "literal-id"],
        ["--continue"],
        ["--resume", "literal-id"],
    ],
)
def test_prompt_text_cannot_choose_the_managed_session_identity(prompt: list[str]) -> None:
    from aisquare.services.explainability import plan_session_identity

    identity = plan_session_identity("claude", ["--", *prompt])
    assert identity.session_id not in (None, "literal-id")
    assert identity.inject_args == ("--session-id", identity.session_id)


def test_readonly_receipt_store_is_permanent_and_reported(
    receiver: Callable[[dict[str, Any]], int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        outbox,
        "retry_batch",
        Mock(side_effect=sqlite3.OperationalError("attempt to write a readonly database")),
    )
    assert receiver(_payload()) == 200
    assert diagnostics._check_outbox().status != "ok"


def test_recovery_clears_the_previous_receiver_diagnostic(
    receiver: Callable[[dict[str, Any]], int],
) -> None:
    outbox.report_failure(OSError(errno.ENOSPC, "fixture"))
    assert diagnostics._check_outbox().status != "ok"
    assert receiver(_payload()) == 200
    assert diagnostics._check_outbox().status == "ok"


@pytest.mark.skipif(os.name != "posix", reason="Native receiver is POSIX/WSL only")
@pytest.mark.parametrize("death", [signal.SIGTERM, signal.SIGKILL])
@pytest.mark.parametrize("ship_before_retry", [False, True])
def test_killed_receiver_prefix_is_not_republished(
    death: int,
    ship_before_retry: bool,
    tmp_path: Path,
    shipping: None,
) -> None:
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps(_payload(6)))
    checkpoint = tmp_path / "three-spooled"
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            """import json, sys, time
from pathlib import Path
from aisquare.core import outbox
from aisquare.services import native_telemetry
original = outbox.enqueue_retryable
count = 0
def enqueue(record, **kwargs):
    global count
    result = original(record, **kwargs)
    count += 1
    if count == 3:
        Path(sys.argv[2]).touch()
        time.sleep(30)
    return result
outbox.enqueue_retryable = enqueue
native_telemetry.capture(json.loads(Path(sys.argv[1]).read_text()), 'crashed')
""",
            str(payload),
            str(checkpoint),
        ]
    )
    try:
        deadline = time.monotonic() + 5
        while not checkpoint.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert checkpoint.exists() and len(outbox.pending()) == 3
        process.send_signal(death)
        process.wait(timeout=3)
        if ship_before_retry:
            for path in outbox.pending():
                claimed = outbox.claim(path)
                assert claimed is not None
                outbox.mark_sent(claimed)
        assert native_telemetry.capture(_payload(6), "crashed") == 3
        assert len(outbox.pending()) + outbox.counts().sent == 6
        assert native_telemetry.capture(_payload(6), "crashed") == 0
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)


def test_spool_failure_remains_primary_when_board_checkpoint_also_fails(
    monkeypatch: pytest.MonkeyPatch,
    shipping: None,
) -> None:
    def enqueue(record: dict[str, object], **kwargs: object) -> Path:
        raise OSError(errno.ENOSPC, "fixture spool full")

    monkeypatch.setattr(outbox, "enqueue_retryable", enqueue)
    monkeypatch.setattr(
        SqliteStore, "set_meta", Mock(side_effect=sqlite3.OperationalError("disk I/O error"))
    )
    with pytest.raises(OSError) as raised:
        native_telemetry.capture(_payload(), "io")
    assert raised.value.errno == errno.ENOSPC


@pytest.mark.parametrize(
    "error",
    [
        sqlite3.OperationalError("disk I/O error"),
        sqlite3.OperationalError("unable to open database file"),
        sqlite3.DatabaseError("database disk image is malformed"),
        RuntimeError("unexpected observer bug"),
    ],
)
def test_receiver_retries_and_reports_unexpected_capture_failures(
    error: Exception,
    receiver: Callable[[dict[str, Any]], int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(native_telemetry, "capture", Mock(side_effect=error))
    assert receiver(_payload()) == 503
    diagnostic = diagnostics._check_outbox()
    assert type(error).__name__ in diagnostic.detail
    assert "last-error.json" in (diagnostic.fix or "")


@pytest.mark.parametrize("malformed", [None, [7], 5, [None, {"key": "foreign", "value": {}}]])
def test_bad_attributes_do_not_discard_valid_neighboring_records(
    malformed: object,
    receiver: Callable[[dict[str, Any]], int],
) -> None:
    payload = _payload(512)
    payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"][256]["attributes"] = malformed
    assert receiver(payload) == 200
    assert len(outbox.pending()) == 512


@pytest.mark.parametrize(
    "extra",
    [
        {"resource": None},
        {"scopeLogs": None},
        {"scopeLogs": [{"logRecords": None}]},
        {"resource": {"attributes": None}},
    ],
)
def test_null_otlp_fields_are_absent_not_batch_failures(
    extra: dict[str, Any], shipping: None
) -> None:
    payload = _payload(2)
    payload["resourceLogs"].insert(0, extra)
    assert native_telemetry.capture(payload, "nulls") == 2


@pytest.mark.parametrize("failure", [errno.EACCES, errno.EROFS])
def test_permanent_spool_failure_costs_one_attempt_per_request_and_is_diagnosed(
    failure: int,
    receiver: Callable[[dict[str, Any]], int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write = Path.write_text
    attempts = 0

    def fail(path: Path, *args: Any, **kwargs: Any) -> int:
        nonlocal attempts
        if path.parent == outbox.queue_dir():
            attempts += 1
            raise OSError(failure, "fixture permanent spool failure")
        return write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail)
    for _ in range(3):
        assert receiver(_payload(512)) == 200
    assert attempts == 3 and not outbox.pending()
    assert diagnostics._check_outbox().status != "ok"


def test_doctor_probes_spool_permissions(monkeypatch: pytest.MonkeyPatch, shipping: None) -> None:
    monkeypatch.setattr(
        Path, "write_bytes", Mock(side_effect=PermissionError(errno.EACCES, "fixture"))
    )
    check = diagnostics._check_outbox()
    assert str(outbox.queue_dir()) in check.detail
    assert "Restore write access" in (check.fix or "")


def test_errno_classifier_is_portable_and_does_not_guess_unknown_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(errno, "EDQUOT", raising=False)
    assert outbox.temporary_write_error(OSError(errno.ENOSPC, "disk full"))
    assert not outbox.temporary_write_error(OSError("unknown cause"))
    assert not outbox.temporary_write_error(OSError(errno.EACCES, "permissions"))
