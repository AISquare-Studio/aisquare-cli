"""#20 regression net: bulk concurrent writes must not lose a confirmed write.

Single-call suites lie (the fixer notes on issue #19): the misrouting and
lying-success bugs only surfaced under many concurrent writers with reader
loops hammering the same store. This harness drives the REAL CLI as
subprocesses — 8 writers x 25 mixed writes against ONE shared isolated
``AISQUARE_HOME`` while time-boxed reader loops (the #19 repro signature)
run alongside — and holds the #20 delivery contract in bulk:

(a) every write that exited 0 and carried the success marker
    (``delivered: true``) is on the board EXACTLY once, via read-back;
(b) every dropped write exited nonzero with a machine-readable error and
    no success marker (at-least-once semantics: a failed confirm may still
    have committed, so absence is deliberately NOT asserted);
(c) zero ``aisquare serve --stdio`` daemons accumulate across the run;
(d) the whole storm stays inside a CI-friendly time box.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from aisquare.core.paths import HOME_ENV_VAR

WRITERS = 8
WRITES_PER_WRITER = 25
READERS = 2
TIME_BOX_SECONDS = 60.0
_CLI_TIMEOUT = 30.0  # any single call outliving this is already a failure


@dataclass
class WriteResult:
    """One CLI write: what it tried to put on the board and how it ended."""

    kind: str  # note | task_add | task_review
    marker: str  # unique note text / task title / reviewed task id
    exit_code: int
    stdout: str
    stderr: str
    delivered: bool  # the --json success marker
    payload: dict[str, Any] | None


def _json_or_none(text: str) -> dict[str, Any] | None:
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _base_env(home: Path) -> dict[str, str]:
    """The shared-subprocess environment: one home, hermetic knobs."""
    env = os.environ.copy()
    for knob in (
        "AISQUARE_TEAM",
        "AISQUARE_ROLE",
        "AISQUARE_TEAM_HUB",
        "AISQUARE_TEAM_DELTA",
        "AISQUARE_TEAM_LEASE_MIN",
        "AISQUARE_BRAIN",
        "AISQUARE_BRAIN_EMBED",
        "AISQUARE_BRAIN_EMBED_MODEL",
        "AISQUARE_DB_BUSY_MS",
        "AISQUARE_SERVE_CLOSE_AFTER",
        "CLAUDE_CONFIG_DIR",
    ):
        env.pop(knob, None)
    env[HOME_ENV_VAR] = str(home)
    env["AISQUARE_BRAIN"] = "0"  # no detached drain workers inside the storm
    env["NO_COLOR"] = "1"
    return env


def _cli(args: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "aisquare", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=_CLI_TIMEOUT,
    )


def _writer(index: int, *, project: Path, env: dict[str, str]) -> list[WriteResult]:
    """25 mixed writes: note → task add → review that task, repeating."""
    results: list[WriteResult] = []
    reviewable: str | None = None
    for turn in range(WRITES_PER_WRITER):
        shape = turn % 3
        if shape == 1:
            marker = f"bulk-task w{index} t{turn} {uuid.uuid4().hex[:8]}"
            proc = _cli(["--json", "task", "add", marker], cwd=project, env=env)
            kind = "task_add"
        elif shape == 2 and reviewable is not None:
            marker = reviewable
            proc = _cli(
                ["--json", "task", "review", reviewable, "--note", "bulk verify"],
                cwd=project,
                env=env,
            )
            kind = "task_review"
        else:
            marker = f"bulk-note w{index} t{turn} {uuid.uuid4().hex[:8]}"
            proc = _cli(["--json", "note", marker], cwd=project, env=env)
            kind = "note"
        payload = _json_or_none(proc.stdout)
        delivered = payload is not None and payload.get("delivered") is True
        if kind == "task_add":
            reviewable = str(payload["id"]) if delivered and payload is not None else None
        elif kind == "task_review":
            reviewable = None  # each added task is reviewed at most once
        results.append(
            WriteResult(
                kind=kind,
                marker=marker,
                exit_code=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
                delivered=delivered,
                payload=payload,
            )
        )
    return results


_READER_SCRIPT = """\
import subprocess
import sys

# The #19 repro signature, in Python rather than `seq | while read` + `timeout`:
# separate short-lived processes hammering one store, each call time-boxed, each
# failure swallowed (the shell's `|| true`). Driving it from Python keeps the
# repro identical on every platform instead of only where coreutils exists.
iterations = int(sys.argv[1])
for _ in range(iterations):
    for args in (["board"], ["--json", "task", "list"]):
        try:
            subprocess.run(
                [sys.executable, "-m", "aisquare", *args],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except (subprocess.TimeoutExpired, OSError):
            pass
"""


def _reader_loop(project: Path, env: dict[str, str], iterations: int) -> subprocess.Popen[bytes]:
    """The #19 repro signature: a read loop with per-call timeouts."""
    return subprocess.Popen(
        [sys.executable, "-c", _READER_SCRIPT, str(iterations)],
        cwd=project,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _stdio_daemon_count() -> int:
    """How many ``aisquare serve --stdio`` daemons are alive right now."""
    if sys.platform == "win32":
        # No pgrep; Win32_Process is the only place a full command line lives.
        proc = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "@(Get-CimInstance Win32_Process | "
                "Where-Object { $_.CommandLine -like '*aisquare serve --stdio*' }).Count",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    else:
        proc = subprocess.run(
            ["pgrep", "-fc", "aisquare serve --stdio"],
            capture_output=True,
            text=True,
            check=False,
        )
    if proc.returncode != 0:
        return 0
    return int(proc.stdout.strip() or "0")


def test_bulk_concurrent_writes_never_lose_a_confirmed_write(tmp_path: Path) -> None:
    home = tmp_path / "shared-home"
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)
    env = _base_env(home)

    activated = _cli(["team", "on"], cwd=project, env=env)
    assert activated.returncode == 0, activated.stderr

    daemons_before = _stdio_daemon_count()
    started = time.monotonic()
    readers = [_reader_loop(project, env, iterations=20) for _ in range(READERS)]
    try:
        with ThreadPoolExecutor(max_workers=WRITERS) as pool:
            batches = list(pool.map(partial(_writer, project=project, env=env), range(WRITERS)))
    finally:
        for reader in readers:
            reader.wait(timeout=60)
    elapsed = time.monotonic() - started

    results = [result for batch in batches for result in batch]
    assert len(results) == WRITERS * WRITES_PER_WRITER

    # The success marker and the exit code must always agree — an exit-0
    # write without a receipt (or a receipt on a failed write) is exactly
    # the lying-success class this harness exists to catch.
    for result in results:
        assert (result.exit_code == 0) == result.delivered, (
            result.kind,
            result.marker,
            result.exit_code,
            result.stdout,
            result.stderr,
        )

    confirmed = [result for result in results if result.exit_code == 0]
    dropped = [result for result in results if result.exit_code != 0]

    # (b) dropped writes failed loudly: machine-readable error, no marker.
    for result in dropped:
        failure = _json_or_none(result.stdout)
        assert failure is not None and "error" in failure, (result.kind, result.stdout)
        assert "✓" not in result.stdout + result.stderr

    # Read back the whole board through fresh CLI calls.
    log = _cli(["--json", "team", "log", "--limit", "5000"], cwd=project, env=env)
    assert log.returncode == 0, log.stderr
    events: list[dict[str, Any]] = json.loads(log.stdout)
    note_texts = [event["payload"]["text"] for event in events if event["kind"] == "team.note"]
    reviewed_ids = [
        event["payload"]["task_id"] for event in events if event["kind"] == "team.task_review"
    ]
    listing = _cli(["--json", "task", "list"], cwd=project, env=env)
    assert listing.returncode == 0, listing.stderr
    tasks: list[dict[str, Any]] = json.loads(listing.stdout)
    titles = [task["title"] for task in tasks]
    status_by_id = {task["id"]: task["status"] for task in tasks}

    # (a) every confirmed write is on the board EXACTLY once.
    for result in confirmed:
        if result.kind == "note":
            assert note_texts.count(result.marker) == 1, result.marker
        elif result.kind == "task_add":
            assert titles.count(result.marker) == 1, result.marker
        else:  # task_review
            assert reviewed_ids.count(result.marker) == 1, result.marker
            assert status_by_id[result.marker] == "review"

    # (c) the storm strands no stdio daemons.
    assert _stdio_daemon_count() <= daemons_before

    # (d) bounded runtime — the whole point is that this stays in CI.
    assert elapsed < TIME_BOX_SECONDS, f"bulk run took {elapsed:.1f}s"
