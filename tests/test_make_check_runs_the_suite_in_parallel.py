"""``make check`` runs the suite across every core, and loses nothing for it (#129 d).

Serial, the suite took 339.6 s on a 12-core machine (#129), and every coder in a fleet
runs ``make check`` many times per task. pytest-xdist spreads the tests over one worker
per core. Each test already gets a home, a tmux socket folder and a ``claude``
stand-in of its own (``tests/conftest.py``), so the workers share nothing a test writes.

What a worker run could lose is the conftest's own report: what a test left running
wrote outside every test's home. Each worker notes it in its own process, and only the
controller prints the terminal summary, so without the hand-off below a parallel run
reported nothing at all, which reads as "nothing leaked".
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.private_fleets import ORPHAN_PREFIX, private_folder, sweep_orphans, uid

ROOT = Path(__file__).resolve().parents[1]

#: Set in the child run only: the probe below writes outside its own home when set.
_PROBE = "AISQUARE_SUITE_XDIST_PROBE"


def _recipe(target: str) -> str:
    """The recipe lines of one Makefile target."""
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    found = re.search(rf"^{target}:.*\n((?:\t.*\n)+)", text, re.MULTILINE)
    assert found, f"the Makefile has no `{target}` target"
    return found.group(1)


def test_make_test_runs_pytest_on_every_core() -> None:
    assert "-m pytest -n auto" in _recipe("test")


def test_xdist_is_a_dev_dependency_and_locked() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev = pyproject["project"]["optional-dependencies"]["dev"]
    assert any(re.match(r"pytest-xdist\b", spec) for spec in dev), dev
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    assert '\nname = "pytest-xdist"\n' in lock, "uv.lock does not pin pytest-xdist"


def test_every_ci_test_step_runs_pytest_on_every_core() -> None:
    """Each shard of every leg, the two ambient legs' included (owner decision 2026-10-25)."""
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    test = workflow[workflow.index("\n  test:\n") : workflow.index("\n  shards:\n")]
    commands = [
        line.strip().removeprefix("run: ")
        for line in test.splitlines()
        if re.match(r"\s*(run: )?pytest\b", line)
    ]
    assert len(commands) == 2, f"expected the plain and the ambient step's pytest: {commands}"
    for command in commands:
        assert re.match(r"pytest -n auto\b", command), command


def test_probe_writes_outside_its_own_home(home_outside_a_test: Path, isolated_home: Path) -> None:
    """The child run below sets the switch; the report it makes is what that test checks."""
    if os.environ.get(_PROBE) != "1":
        pytest.skip("runs only inside test_a_parallel_run_still_reports_a_write_outside_a_home")
    assert not home_outside_a_test.is_relative_to(isolated_home), "the write must leave its home"
    (home_outside_a_test / "left-by-the-probe").write_text("", encoding="utf-8")


requires_tmux = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("tmux") is None, reason="needs tmux"
)


def _dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", ""])
    child.wait()
    return child.pid


def _server_in(folder: Path) -> Path:
    """A real tmux server, a pane running in it, in ``folder`` used as a TMUX_TMPDIR."""
    sockets = folder / f"tmux-{uid()}"
    sockets.mkdir(mode=0o700, parents=True)
    socket = sockets / "asq-test-orphan"
    tmux = shutil.which("tmux")
    assert tmux is not None
    subprocess.run(
        [tmux, "-S", str(socket), "-f", "/dev/null", "new-session", "-d", "sleep", "600"],
        check=True,
        timeout=10,
    )
    return socket


def _answers(socket: Path) -> bool:
    tmux = shutil.which("tmux")
    assert tmux is not None
    probe = subprocess.run(
        [tmux, "-S", str(socket), "list-sessions"], capture_output=True, timeout=10, check=False
    )
    return probe.returncode == 0


def _end(folder: Path) -> None:
    """What a failing test left: every server under ``folder``, then the folder."""
    for socket in folder.glob(f"**/tmux-{uid()}/*"):
        subprocess.run(
            ["tmux", "-S", str(socket), "kill-server"], capture_output=True, check=False, timeout=10
        )
    shutil.rmtree(folder, ignore_errors=True)


@pytest.fixture
def base() -> Iterator[Path]:
    """A folder of this (live) process to sweep, so the test never sweeps /tmp itself."""
    folder = private_folder()
    try:
        yield folder
    finally:
        _end(folder)


@requires_tmux
def test_a_dead_runs_server_is_ended_and_its_folder_removed(base: Path) -> None:
    orphan = base / f"{ORPHAN_PREFIX}{_dead_pid()}-left"
    socket = _server_in(orphan)
    assert _answers(socket)

    assert sweep_orphans(base) == [orphan]
    assert not _answers(socket)
    assert not orphan.exists()


@requires_tmux
def test_a_live_runs_folder_is_never_touched(base: Path) -> None:
    mine = base / f"{ORPHAN_PREFIX}{os.getpid()}-running"
    socket = _server_in(mine)

    assert sweep_orphans(base) == []
    assert _answers(socket), "a run still going lost its server"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX only: the sweep sweeps nothing on Windows, which has no /tmp",
)
def test_a_folder_not_named_for_a_process_is_never_touched(base: Path) -> None:
    other = base / "asq-someone-elses"
    other.mkdir()
    assert sweep_orphans(base) == []
    assert other.exists()


@requires_tmux
def test_a_parallel_run_sweeps_what_a_killed_run_left() -> None:
    orphan = Path(f"/tmp/{ORPHAN_PREFIX}{_dead_pid()}-killed")
    try:
        socket = _server_in(orphan)
        run = _parallel_probe()
        assert run.returncode == 0, run.stdout + run.stderr
        assert not _answers(socket), "the killed run's server outlived the next run"
        assert not orphan.exists()
    finally:
        _end(orphan)


def _parallel_probe() -> subprocess.CompletedProcess[str]:
    """This file's probe, under two xdist workers, from a shell with no home named."""
    env = {k: v for k, v in os.environ.items() if k != "AISQUARE_HOME"}
    env[_PROBE] = "1"
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-n",
            "2",
            "-p",
            "no:cacheprovider",
            f"{__file__}::test_probe_writes_outside_its_own_home",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        # A hang guard, not a bound on the run: it takes a few seconds, and on a shard busy
        # with the whole suite's other workers its interpreter starts, imports and brings up
        # two workers of its own beside them. What it must catch is a child that never ends.
        timeout=600,
        check=False,
    )


def test_a_parallel_run_still_reports_a_write_outside_a_home() -> None:
    run = _parallel_probe()
    out = run.stdout + run.stderr
    assert run.returncode == 0, out
    assert "written outside every test's own home" in out, out
    assert "left-by-the-probe" in out, out
