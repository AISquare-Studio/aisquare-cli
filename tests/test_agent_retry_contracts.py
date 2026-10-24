"""Regression contracts for native ownership and durable OTLP retries."""

from __future__ import annotations

import contextlib
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
import urllib.parse
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
from aisquare.core.agent_adapters import codex_config
from aisquare.core.agent_adapters.types import model_overrides
from aisquare.core.config import (
    FleetRoleSettings,
    RoleLaunchProfile,
    load_config,
    save_config,
)
from aisquare.core.orchestrator import team_project
from aisquare.core.store import SCHEMA_VERSION, SqliteStore, store_session
from aisquare.services import agent_launch, diagnostics, fleet, native_telemetry
from tests.test_fleet_service import FakeTmux
from tests.test_store import _dogfooded_113


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
        kwargs={"agent": "codex", "owner_alive": lambda: not stop.is_set()},
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
            # A hang guard, not a latency contract (the receiver is POSIX-only; 512
            # records take 0.17 s on Linux). A request's receipts and board work is
            # fixed, but it spools a file per record: three recovery stats, a create
            # and a rename. On a Windows runner a 512-record request outran a flat
            # 5 s, then answered 200 to the closed socket (WinError 10053).
            records = sum(
                len(scope.get("logRecords") or [])
                for resource in payload.get("resourceLogs") or []
                for scope in (resource or {}).get("scopeLogs") or []
                if isinstance(scope, dict)
            )
            try:
                with urllib.request.urlopen(request, timeout=5 + 0.02 * records) as response:
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
    profile = [*saved, "-c", "model_reasoning_effort=ultracode"]
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
        assert model_overrides("codex", argv[1:])[1] == "max"


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
    monkeypatch.setattr(fleet, "_worktree_for", tree)
    # The repository's own `.git`, where the branch lock a spawn holds over its tree lives.
    common = tmp_path / "common.git"
    common.mkdir()
    monkeypatch.setattr(fleet, "_git_common_dir", lambda root: common)
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
            "model_reasoning_effort = ultracode",
        ],
    )
    assert result.exit_code == 0, result.output
    assert execute.call_args.args[1] == ["codex", '--config=model_reasoning_effort="max"']


def test_alias_equivalence_and_mapping_notes_are_preserved(runner: CliRunner) -> None:
    cfg = load_config()
    cfg.team.profiles["coder"] = RoleLaunchProfile(
        agent="codex",
        args=["-c", "model_reasoning_effort=ultracode"],
    )
    save_config(cfg)
    result = runner.invoke(app, ["--json", "team", "spawn", "coder", "--effort", "ultracode"])
    assert result.exit_code == 0, result.output
    notes = json.loads(result.stdout)["notes"]
    assert any("maps 'ultracode'" in note for note in notes)
    # Codex's own max is the same level, so neither takes precedence over the other.
    result = runner.invoke(app, ["--json", "team", "spawn", "coder", "--effort", "max"])
    assert result.exit_code == 0, result.output
    notes = json.loads(result.stdout)["notes"]
    assert not any("maps 'max'" in note for note in notes)
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
    # A fleet spawn resolves the role as `team spawn` does — Claude Code's ladder — so
    # its row says the same and is not shown apart; arguments of its own are.
    result = runner.invoke(app, ["--json", "team", "harness"])
    assert result.exit_code == 0, result.output
    coder = next(row for row in json.loads(result.stdout)["roles"] if row["role"] == "coder")
    fleet_row = coder["fleet"]
    assert (fleet_row["resolves_to"], fleet_row["source"]) == (
        coder["resolves_to"],
        coder["source"],
    )
    assert fleet_row["resolves_to"] == "sonnet" and fleet_row["effort"] == coder["effort"]
    config = load_config()
    config.fleet.roles["coder"] = FleetRoleSettings(extra_args=["--model", "opus"])
    save_config(config)
    result = runner.invoke(app, ["team", "harness"])
    assert result.exit_code == 0, result.output
    assert "fleet: opus [native]" in result.output
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

    # #113's own steps under its own stamps; the merges appended them as v26-v28, then
    # v33-v35, then v34-v36.
    database = _dogfooded_113(version)
    monkeypatch.setattr(
        module, "_MIGRATIONS", (*module._MIGRATIONS, "CREATE TABLE next_feature (id TEXT);")
    )
    with store_session() as store:
        assert isinstance(store, SqliteStore)
        assert store._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION + 1
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT * FROM next_feature").fetchall() == []
        # The steps this line stamped 15-17, restored by the presence pass.
        assert connection.execute("SELECT * FROM claude_account").fetchall() == []
        columns = {row[1] for row in connection.execute("PRAGMA table_info(team_session)")}
        assert {"agent", "native_session_id", "limit_resets_at"} <= columns


def test_locked_board_does_not_duplicate_spooled_or_already_shipped_events(shipping: None) -> None:
    payload = _payload()
    with sqlite3.connect(paths.db_path()) as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            native_telemetry.capture(payload, "locked", agent="codex")
        assert len(outbox.pending()) == 5
        for path in outbox.pending():
            claimed = outbox.claim(path)
            assert claimed is not None
            outbox.mark_sent(claimed)
        assert not outbox.pending()
        blocker.rollback()
    assert native_telemetry.capture(payload, "locked", agent="codex") == 0
    assert outbox.counts().sent == 5 and not outbox.pending()


def test_concurrent_receivers_publish_one_copy(
    shipping: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Both receivers write the board's dedup keys, one after the other: the second waits
    # out the first's commit on busy_timeout. The module's 50 ms knob is for the stores it
    # wedges on purpose; under a loaded make check the first commit outlasts it ("database
    # is locked", 2 in 50 under load), so these two wait as a receiver does.
    monkeypatch.delenv("AISQUARE_DB_BUSY_MS")
    barrier = threading.Barrier(2)

    def capture() -> int:
        barrier.wait(timeout=5)
        return native_telemetry.capture(_payload(32), "concurrent", agent="codex")

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


@pytest.mark.parametrize("failure", ["disk-full", "unserialisable"])
def test_a_primary_path_spool_failure_is_not_a_receiver_failure(
    failure: str, shipping: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a native capture clears last-error.json, so only the receiver may leave it:
    a Claude prompt capture's one ENOSPC warned about a Codex receiver forever."""
    record: dict[str, object] = {"kind": "prompt"}
    write = Path.write_text

    def full(path: Path, *args: Any, **kwargs: Any) -> int:
        if path.parent == outbox.queue_dir():
            raise OSError(errno.ENOSPC, "fixture full disk")
        return write(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        if failure == "unserialisable":
            record["self"] = record  # json.dumps: ValueError, circular reference
        else:
            patch.setattr(Path, "write_text", full)
        assert outbox.enqueue(record) is None
    assert diagnostics._check_outbox().status == "ok"
    # Control: the receiver's own write still reports its failure.
    native: dict[str, object] = {"kind": "native"}
    native["self"] = native
    assert outbox.enqueue_retryable(native) is None
    assert diagnostics._check_outbox().status != "ok"


def test_recovery_clears_the_previous_receiver_diagnostic(
    receiver: Callable[[dict[str, Any]], int],
) -> None:
    outbox.report_failure(OSError(errno.ENOSPC, "fixture"))
    assert diagnostics._check_outbox().status != "ok"
    assert receiver(_payload()) == 200
    assert diagnostics._check_outbox().status == "ok"


@pytest.mark.skipif(os.name != "posix", reason="Native receiver is POSIX/WSL only")
# SIGKILL by name, which mypy's win32 stubs lack: the test is POSIX-only (skipif above).
@pytest.mark.parametrize("death", [signal.SIGTERM, getattr(signal, "SIGKILL", signal.SIGTERM)])
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
native_telemetry.capture(json.loads(Path(sys.argv[1]).read_text()), 'crashed', agent='codex')
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
        assert native_telemetry.capture(_payload(6), "crashed", agent="codex") == 3
        assert len(outbox.pending()) + outbox.counts().sent == 6
        assert native_telemetry.capture(_payload(6), "crashed", agent="codex") == 0
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
        native_telemetry.capture(_payload(), "io", agent="codex")
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


def test_a_receiver_request_opens_the_receipts_once_whatever_its_size(
    receiver: Callable[[dict[str, Any]], int], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The receiver fixture's time budget grows per record on this premise: the
    # receipts' connection, schema check, retention delete and commit do not.
    opened = 0
    receipts = outbox._receipts

    @contextlib.contextmanager
    def counted() -> Iterator[sqlite3.Connection]:
        nonlocal opened
        opened += 1
        with receipts() as connection:
            yield connection

    monkeypatch.setattr(outbox, "_receipts", counted)
    assert receiver(_payload(512)) == 200
    assert opened == 1 and len(outbox.pending()) == 512


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
    assert native_telemetry.capture(payload, "nulls", agent="codex") == 2


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


def _endpoint(argv: list[str]) -> str:
    key = "otel.exporter.otlp-http.endpoint="
    return str(next(json.loads(arg.removeprefix(key)) for arg in argv if arg.startswith(key)))


@pytest.mark.skipif(os.name != "posix", reason="Native receiver is POSIX/WSL only")
def test_the_receivers_token_is_in_the_launch_environment_never_on_a_command_line(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shipping: None,
) -> None:
    """t8 smoke: the token was Codex's `-c otel.exporter.otlp-http.headers.Authorization=`,
    which every local user reads in ps; a process's environment is its owner's alone."""
    monkeypatch.setattr(codex_config, "SYSTEM_CONFIG", tmp_path / "system.toml")
    children: list[tuple[subprocess.Popen[bytes], list[str]]] = []
    popen = subprocess.Popen

    def receiver(argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        child = popen(argv, **kwargs)
        if "--ready" in argv:
            children.append((child, argv))
        return child

    monkeypatch.setattr(subprocess, "Popen", receiver)
    execute = Mock()
    monkeypatch.setattr(import_module("aisquare.cli.launch"), "_exec", execute)
    try:
        result = runner.invoke(app, ["launch", "coder", "--agent", "codex"])
        assert result.exit_code == 0, result.output
        assert "native telemetry enabled" in result.output, result.output
        _, argv, env = execute.call_args.args
        name, _, value = env[native_telemetry.HEADERS_ENV].partition("=")
        authorization = urllib.parse.unquote(value)
        token = authorization.removeprefix("Bearer ")
        assert name == "Authorization" and token.startswith(native_telemetry.TOKEN_PREFIX)
        ((_, receiver_argv),) = children
        assert not any(token in arg for arg in [*argv, *receiver_argv])

        def post(headers: dict[str, str]) -> int:
            request = urllib.request.Request(
                _endpoint(argv), data=json.dumps(_payload()).encode(), headers=headers
            )
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    return int(response.status)
            except urllib.error.HTTPError as exc:
                with exc:
                    return exc.code

        assert post({}) == 403
        assert post({"Authorization": authorization}) == 200
        spooled = [json.loads(path.read_text()) for path in outbox.pending()]
        assert any(record.get("kind") == "native_event" for record in spooled)
    finally:
        for child, _ in children:
            child.send_signal(signal.SIGINT)  # its own cleanup: the ready file and its folder
            child.wait(timeout=10)


@pytest.mark.skipif(os.name != "posix", reason="Native receiver is POSIX/WSL only")
def test_the_receivers_header_is_kept_out_of_the_tools_shells_with_the_operators_patterns(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shipping: None,
) -> None:
    """Review r2 of #113, finding 5: the header in Codex's environment reached every
    process its tools start, whose OpenTelemetry SDK sends the signal-specific variable
    in place of the app's own headers. It is excluded from the tools' shells, and since
    that ``-c`` replaces the operator's ``exclude`` (0.162.1, measured), theirs, from
    every layer, go in it too."""
    monkeypatch.setattr(codex_config, "SYSTEM_CONFIG", tmp_path / "system.toml")
    (tmp_path / "system.toml").write_text(
        '[shell_environment_policy]\nexclude = ["SYSTEM_*"]\n', encoding="utf-8"
    )
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text(
        '[shell_environment_policy]\nexclude = ["USER_*", "SYSTEM_*"]\n', encoding="utf-8"
    )
    project = tmp_path / "repo" / "sub"
    (tmp_path / "repo" / ".codex").mkdir(parents=True)
    (tmp_path / "repo" / ".codex" / "config.toml").write_text(
        '[shell_environment_policy]\nexclude = ["PROJECT_X"]\n', encoding="utf-8"
    )
    project.mkdir()
    monkeypatch.chdir(project)
    children: list[subprocess.Popen[bytes]] = []
    popen = subprocess.Popen

    def receiver(argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        child = popen(argv, **kwargs)
        if "--ready" in argv:
            children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", receiver)
    execute = Mock()
    monkeypatch.setattr(import_module("aisquare.cli.launch"), "_exec", execute)
    try:
        result = runner.invoke(app, ["launch", "coder", "--agent", "codex"])
        assert result.exit_code == 0, result.output
        assert "native telemetry enabled" in result.output, result.output
        _, argv, env = execute.call_args.args
        key = "shell_environment_policy.exclude="
        [excluded] = [json.loads(arg.removeprefix(key)) for arg in argv if arg.startswith(key)]
        assert excluded == ["SYSTEM_*", "USER_*", "PROJECT_X", native_telemetry.HEADERS_ENV]
        assert env[native_telemetry.HEADERS_ENV]
    finally:
        for child in children:
            child.send_signal(signal.SIGINT)
            child.wait(timeout=10)


def test_the_operators_own_tool_shell_exclude_or_header_leaves_native_tracing_off(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shipping: None,
) -> None:
    """An ``exclude`` the operator passes with ``-c`` comes after ours and replaces it, and
    a header variable of the operator's own the receiver's would replace for every tool:
    both keep tracing off rather than hand the token on, and the operator's value stays."""
    monkeypatch.setattr(codex_config, "SYSTEM_CONFIG", tmp_path / "system.toml")
    started = Mock(side_effect=AssertionError("a receiver started"))
    monkeypatch.setattr(native_telemetry, "start_receiver", started)
    for args in (
        ["-c", 'shell_environment_policy.exclude=["MINE"]'],
        ["-c", 'shell_environment_policy={inherit="all"}'],
    ):
        assert codex_config.tool_shell_excludes(tmp_path, args, tmp_path) is None
    assert (
        codex_config.tool_shell_excludes(
            tmp_path, ["-c", 'shell_environment_policy.inherit="core"'], tmp_path
        )
        == []
    ), "a sibling key merges with ours"
    selected = agent_launch.resolve(agent="codex")
    env = {"AISQUARE_LAUNCH_ID": "launch-1"}

    args, note = agent_launch.telemetry_args(
        selected, env, ["-c", 'shell_environment_policy.exclude=["MINE"]']
    )
    assert args == [] and "native tracing is off" in note

    theirs = "x-honeycomb-team=abc"
    env[native_telemetry.HEADERS_ENV] = theirs
    args, note = agent_launch.telemetry_args(selected, env, [])
    assert args == [] and native_telemetry.HEADERS_ENV in note
    assert env[native_telemetry.HEADERS_ENV] == theirs


def test_a_selected_profiles_tool_shell_exclude_joins_the_receivers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r3 of #113, finding 7: a ``-p`` profile file's ``exclude`` would have
    replaced ours had the profile come after ``-c``. Measured on 0.162.1, ``-c`` replaces
    the list every file sets, the profile's included (the live test pins it), so the
    profile's patterns join the receiver's, as the user config's do."""
    monkeypatch.setattr(codex_config, "SYSTEM_CONFIG", tmp_path / "system.toml")
    policy = '[shell_environment_policy]\nexclude = ["{}"]\n'
    (tmp_path / "config.toml").write_text(policy.format("USER_SECRET"), encoding="utf-8")
    (tmp_path / "p.config.toml").write_text(policy.format("PROFILE_SECRET"), encoding="utf-8")

    assert codex_config.tool_shell_excludes(tmp_path, ["-p", "p"], tmp_path) == [
        "USER_SECRET",
        "PROFILE_SECRET",
    ]
    assert codex_config.tool_shell_excludes(tmp_path, [], tmp_path) == ["USER_SECRET"]


@pytest.mark.parametrize("inherited", ["ours", "the operator's"])
def test_a_launch_drops_only_a_receiver_header_it_inherited(
    inherited: str,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A launch from a traced Codex's tool shell inherits its receiver's header: it is that
    launch's secret, and an exporter of the operator's would send it in place of theirs."""
    value = {
        "ours": f"Authorization=Bearer%20{native_telemetry.TOKEN_PREFIX}AbC-12_x",
        "the operator's": "Authorization=Bearer%20theirs,x-tenant=7",
    }[inherited]
    monkeypatch.setenv(native_telemetry.HEADERS_ENV, value)
    execute = Mock()
    monkeypatch.setattr(import_module("aisquare.cli.launch"), "_exec", execute)
    result = runner.invoke(app, ["launch", "coder", "--agent", "codex"])
    assert result.exit_code == 0, result.output
    env = execute.call_args.args[2]
    assert env.get(native_telemetry.HEADERS_ENV) == (None if inherited == "ours" else value)
