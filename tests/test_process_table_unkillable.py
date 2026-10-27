"""A tool that cannot be killed does not hold the listing (r25 of #113, finding 4; task #35, E).

``subprocess.run(..., timeout=5)`` kills a tool that runs out of time and then waits for it. An
``lsof`` stuck in an uninterruptible read on a dead mount ignores the kill, so ``run`` never
returned: no ``TimeoutExpired``, no ``_hung()``, so no back-off, and the listing was stopped
with it. ``process_table._run`` waits the timeout and a grace for it, and leaves it behind.
"""

from __future__ import annotations

import contextlib
import os
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from aisquare.core import process_table

#: What a ``run`` that never comes back waits on: set when the test is over.
release = threading.Event()


@pytest.fixture(autouse=True)
def short_waits(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The waits of the module are seconds; these are tenths. Whatever a test left stuck is let
    go of, and the module's memory is clean before and after."""
    monkeypatch.setattr(process_table, "_TIMEOUT", 0.3)
    monkeypatch.setattr(process_table, "_GRACE", 0.5)
    process_table._HELD.clear()
    process_table._BLIND_UNTIL[0] = 0.0
    process_table._STUCK.clear()
    yield
    release.set()
    for thread in list(process_table._STUCK):
        thread.join(5)
    process_table._STUCK.clear()
    process_table._HELD.clear()
    process_table._BLIND_UNTIL[0] = 0.0
    release.clear()


def _a_run_that_never_comes_back(calls: list[str]) -> Callable[..., object]:
    def run(argv: list[str], **kwargs: object) -> object:
        calls.append(argv[0])
        release.wait(30)
        return subprocess.CompletedProcess(argv, 0, stdout="7 1\n", stderr="")

    return run


def _within(seconds: float, call: Callable[[], object]) -> tuple[bool, object, float]:
    """``call`` on a thread of its own, so that a listing that does not come back fails a test
    instead of hanging the run: (whether it came back in time, what it returned, how long)."""
    box: list[object] = []
    started = time.monotonic()
    thread = threading.Thread(target=lambda: box.append(call()), daemon=True)
    thread.start()
    thread.join(seconds)
    return not thread.is_alive(), (box[0] if box else None), time.monotonic() - started


@pytest.mark.parametrize("tool", ["ps", "lsof"])
def test_a_tool_run_never_comes_back_from_is_left_behind_and_starts_the_back_off(
    monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(subprocess, "run", _a_run_that_never_comes_back(calls))
    ask: Callable[[], object] = (
        (lambda: process_table.descendants(7))
        if tool == "ps"
        else (lambda: process_table._from_lsof([7]))
    )

    came_back, answer, took = _within(10.0, ask)

    assert came_back, "the listing is held by a tool that cannot be killed"
    assert answer is None
    assert process_table._TIMEOUT <= took < process_table._TIMEOUT + process_table._GRACE + 2.0
    assert time.monotonic() < process_table._BLIND_UNTIL[0], "and the back-off has begun"
    assert len(process_table._STUCK) == 1 and calls == [tool]
    # Inside the back-off no pane asks again, for any tool.
    assert process_table.held_by_pane(7) is None and process_table.held_by_pane(70) is None
    assert calls == [tool]


def test_no_fifth_tool_is_started_while_four_are_stuck(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(subprocess, "run", _a_run_that_never_comes_back(calls))

    for _ask in range(process_table._STUCK_MAX):
        process_table._BLIND_UNTIL[0] = 0.0  # the back-off is over
        assert process_table._from_lsof([7]) is None
    assert len(process_table._STUCK) == process_table._STUCK_MAX

    process_table._BLIND_UNTIL[0] = 0.0
    came_back, answer, took = _within(10.0, lambda: process_table._from_lsof([7]))
    assert came_back and answer is None and took < process_table._TIMEOUT
    assert calls == ["lsof"] * process_table._STUCK_MAX, "the fifth was not started"

    release.set()  # the mount comes back: they end, and the tool is asked again
    for thread in list(process_table._STUCK):
        thread.join(5)
    process_table._BLIND_UNTIL[0] = 0.0
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout="p7\nn/x\n", stderr=""),
    )
    assert process_table._from_lsof([7]) == frozenset({"/x"})
    assert process_table._STUCK == []


def test_a_tool_that_answers_in_time_is_waited_for_and_not_left_behind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def slow(argv: list[str], **kwargs: object) -> object:
        time.sleep(0.15)
        return subprocess.CompletedProcess(argv, 0, stdout="7 1\n8 7\n", stderr="")

    monkeypatch.setattr(subprocess, "run", slow)

    assert process_table.descendants(7) == frozenset({7, 8})
    assert process_table._STUCK == []
    assert process_table._BLIND_UNTIL[0] == 0.0


def test_a_tool_killed_a_moment_after_its_timeout_is_not_left_behind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run`` raises ``TimeoutExpired`` once the kill has taken, a moment after the timeout: that
    is inside the grace, the usual hang, and it is the back-off and not a tool left behind."""

    def killed_late(argv: list[str], **kwargs: object) -> object:
        time.sleep(process_table._TIMEOUT + 0.15)
        raise subprocess.TimeoutExpired(argv, process_table._TIMEOUT)

    monkeypatch.setattr(subprocess, "run", killed_late)

    assert process_table._from_lsof([7]) is None
    assert process_table._STUCK == []
    assert time.monotonic() < process_table._BLIND_UNTIL[0]


def test_an_error_run_raises_is_the_listings_to_see(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not swallowed because it happened on a thread: a bug in the table is a bug."""

    def broken(argv: list[str], **kwargs: object) -> object:
        raise RuntimeError("a bug")

    monkeypatch.setattr(subprocess, "run", broken)

    with pytest.raises(RuntimeError, match="a bug"):
        process_table._from_lsof([7])


@pytest.mark.skipif(sys.platform == "win32", reason="a POSIX shell script is the fake lsof")
def test_an_lsof_that_ignores_sigterm_is_killed_at_the_timeout_and_the_back_off_begins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The real thing, as far as a test can make it: a script named ``lsof`` that ignores
    ``SIGTERM`` (an ignored signal stays ignored across ``exec``) and sleeps a minute. It is
    killed (``SIGKILL``) when its time runs out, the listing is told "cannot tell", and the
    back-off begins; it does not run on, nor is it left behind."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pid_file = tmp_path / "lsof.pid"
    fake = bin_dir / "lsof"
    fake.write_text(f"#!/bin/sh\necho $$ > \"{pid_file}\"\ntrap '' TERM\nexec sleep 60\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    # Long enough for a loaded machine to start a shell and let it write its pid: the script is
    # killed when this runs out, and a pid file it never wrote would tell nothing of the kill.
    monkeypatch.setattr(process_table, "_TIMEOUT", 3.0)
    monkeypatch.setattr(process_table, "_GRACE", 5.0)

    came_back, answer, took = _within(20.0, lambda: process_table._from_lsof([os.getpid()]))

    try:
        assert came_back and answer is None
        assert process_table._TIMEOUT <= took < process_table._TIMEOUT + process_table._GRACE
        assert time.monotonic() < process_table._BLIND_UNTIL[0]
        assert process_table._STUCK == [], "it was killed, not left behind"
        assert pid_file.exists(), "the script never started"
        pid = int(pid_file.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        if pid_file.exists():
            with contextlib.suppress(ProcessLookupError, ValueError):
                os.kill(int(pid_file.read_text()), 9)
