"""#20 regression net: bulk concurrent writes must not lose a confirmed write.

Single-call suites lie (the fixer notes on issue #19): the misrouting and
lying-success bugs only surfaced under many concurrent writers with reader
loops hammering the same store. This harness drives the REAL CLI as
subprocesses — 8 writers x 25 mixed writes against ONE shared isolated
``AISQUARE_HOME`` while while-read+timeout reader loops (the #19 repro
signature) run alongside — and holds the #20 delivery contract in bulk:

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
import shlex
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

import pytest

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


def _reader_loop(project: Path, env: dict[str, str], iterations: int) -> subprocess.Popen[bytes]:
    """The #19 repro signature: a while-read loop with per-call timeouts."""
    aisq = f"{shlex.quote(sys.executable)} -m aisquare"
    script = (
        f"seq {iterations} | while read -r _; do "
        f"timeout 5 {aisq} board >/dev/null 2>&1 || true; "
        f"timeout 5 {aisq} --json task list >/dev/null 2>&1 || true; "
        "done"
    )
    return subprocess.Popen(["bash", "-c", script], cwd=project, env=env)


class _ProbeUnavailable(RuntimeError):
    """This platform cannot answer "whose daemon is that?"."""


def _stdio_daemon_pids(home: Path) -> list[int]:
    """PIDs of ``aisquare serve --stdio`` daemons belonging to ``home``.

    The obvious version of this — ``pgrep -fc "aisquare serve --stdio"`` —
    counts the wrong things, in two ways that both fired in practice:

    1. It is MACHINE-GLOBAL. Another checkout running its own suite has its own
       daemons, and they are not this test's business. With several sessions on
       one box the count moves under you and the assertion fails for a reason
       that has nothing to do with the code under test.
    2. ``-f`` matches the whole command line as a STRING, so any process that
       merely mentions the phrase matches — including the shell that runs the
       probe. Measured: two matches with no daemon running at all.

    So both questions are asked properly. Is it really a daemon: ``serve`` and
    ``--stdio`` must appear as separate entries in argv, which a shell holding
    the phrase as one argument can never satisfy. And is it OURS: the process
    environment must carry this test's ``AISQUARE_HOME``.

    Both answers come from ``/proc``, so this is Linux-only; the caller skips
    rather than silently counting nothing, because an assertion that cannot
    observe its subject is worse than one that is merely awkward.
    """
    if not Path("/proc").is_dir():
        raise _ProbeUnavailable("/proc is required to tell our daemons from anyone else's")
    found: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = [a for a in (entry / "cmdline").read_bytes().split(b"\0") if a]
            if b"serve" not in argv or b"--stdio" not in argv:
                continue
            environ = (entry / "environ").read_bytes().split(b"\0")
        except OSError:
            continue  # the process exited between listdir and read; not ours to mourn
        if f"{HOME_ENV_VAR}={home}".encode() in environ:
            found.append(int(entry.name))
    return found


def test_bulk_concurrent_writes_never_lose_a_confirmed_write(tmp_path: Path) -> None:
    home = tmp_path / "shared-home"
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)
    env = _base_env(home)

    activated = _cli(["team", "on"], cwd=project, env=env)
    assert activated.returncode == 0, activated.stderr

    try:
        daemons_before = _stdio_daemon_pids(home)
    except _ProbeUnavailable as exc:
        pytest.skip(str(exc))
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

    # (c) the storm strands no stdio daemons OF OURS. Scoped to this test's
    # home, so a sibling checkout running its own suite cannot fail this.
    assert _stdio_daemon_pids(home) == daemons_before

    # (d) bounded runtime — the whole point is that this stays in CI.
    assert elapsed < TIME_BOX_SECONDS, f"bulk run took {elapsed:.1f}s"


# ── the probe itself ─────────────────────────────────────────────────────────
#
# A leak detector nobody trusts is worse than none: it costs a re-run to
# dismiss, and it teaches the team to dismiss failures. So the probe is tested
# from both ends — it must not see what is not ours, and it must still see a
# real leak.


def test_the_probe_ignores_a_shell_that_merely_mentions_the_daemon(tmp_path: Path) -> None:
    """The self-match that made the old probe flaky, reproduced deliberately.

    ``pgrep -f`` matches a command line as a string, so a shell holding the
    phrase — a grep, a comment, this very test's own harness — counted as a
    daemon. Requiring ``serve`` and ``--stdio`` as SEPARATE argv entries is
    what makes that impossible: a shell carries the whole phrase as one
    argument and can never satisfy it.
    """
    home = tmp_path / "home"
    home.mkdir()
    # `sleep 30; :` rather than `sleep 30`, and the trailing no-op is the whole
    # point. Given a script that is ONE command, dash execs it and the shell's
    # own argv — the part carrying the phrase — is replaced: /proc/<pid>/cmdline
    # reads `sleep 30`, `pgrep -f` cannot match it, and the decoy stops being a
    # false positive, so this test fails while asserting its own setup. Whether
    # it fails is a race between pgrep and the exec, which is why it passed on
    # CI and not on a developer's box. A second command makes the optimisation
    # illegal, so the shell survives holding its argv.
    decoy = subprocess.Popen(
        ["/bin/sh", "-c", "sleep 30; :  # aisquare serve --stdio"],
        env={**os.environ, HOME_ENV_VAR: str(home)},
    )
    try:
        # The old probe's own matcher would have found it; ours must not.
        naive = subprocess.run(
            ["pgrep", "-f", "aisquare serve --stdio"], capture_output=True, text=True
        )
        assert str(decoy.pid) in naive.stdout.split(), "the decoy must be a real false positive"

        assert _stdio_daemon_pids(home) == [], "a shell is not a daemon"
    finally:
        decoy.kill()
        decoy.wait(timeout=10)


def test_the_probe_still_catches_a_daemon_that_is_really_ours(tmp_path: Path) -> None:
    """And it must still fail the storm if a daemon genuinely leaks.

    A probe that can no longer detect the leak is worse than a flaky one — it
    reports green forever. So this starts a REAL ``aisquare serve --stdio``
    under this test's home and asserts the probe sees exactly it, then that it
    stops seeing it once the daemon is gone.
    """
    home = tmp_path / "home"
    home.mkdir()
    env = _base_env(home)
    daemon = subprocess.Popen(
        [sys.executable, "-m", "aisquare", "serve", "--stdio"],
        cwd=str(tmp_path),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:  # no sleep-to-green: poll the fact itself
            if _stdio_daemon_pids(home) == [daemon.pid]:
                break
        assert _stdio_daemon_pids(home) == [daemon.pid], "a real daemon of ours must be seen"

        # …and it is scoped: another home does not see our daemon.
        other = tmp_path / "someone-else"
        other.mkdir()
        assert _stdio_daemon_pids(other) == []
    finally:
        daemon.kill()
        daemon.wait(timeout=10)
    assert _stdio_daemon_pids(home) == [], "and it stops counting once the daemon is gone"
