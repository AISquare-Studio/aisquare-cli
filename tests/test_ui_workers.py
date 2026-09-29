"""``settle_page``: a test that reads the page waits for what the page has set in motion.

The helper is the tests' own (``tests/ui_workers.py``), shared by every UI
test module. These pin the two races it closes, and the reading that fails on
purpose, on an app small enough that nothing else is in flight: a reading held
far longer than one pause's idle check, started the way the fleet UI's pages
start theirs. The last pin keeps every module on this family.
"""

from __future__ import annotations

import ast
import asyncio
import time
from pathlib import Path

from textual.app import App
from textual.message import Message
from textual.worker import Worker, WorkerState

from tests.ui_workers import settle_page

HELD = 0.3
"""How long a reading takes: many times one pause's idle check, which counts the
sleeping thread as idle."""


class Page(App[None]):
    """Readings from thread workers, recorded by the state-change handler as a page paints them."""

    class Read(Message):
        """Start a reading. Posted, as Textual posts ``Show``, so its handler runs later."""

    def __init__(self, *, then: bool = False, fails: bool = False) -> None:
        super().__init__()
        self.readings: list[str] = []
        self.then = then
        """Whether the first reading's handler starts a second."""
        self.fails = fails
        """Whether the reading fails, as a scripted refusal does: the page shows it."""

    def on_page_read(self, event: Page.Read) -> None:
        self._read("first")

    def _read(self, name: str) -> None:
        def reading() -> str:
            time.sleep(HELD)
            if self.fails:
                raise OSError(f"{name}: unreadable")
            return name

        self.run_worker(reading, name=name, thread=True, exit_on_error=False)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state is WorkerState.ERROR:
            self.readings.append(f"{event.worker.name} failed: {event.worker.error}")
            return
        if event.state is not WorkerState.SUCCESS:
            return
        self.readings.append(event.worker.name)
        if event.worker.name == "first" and self.then:
            self._read("second")


def _settled_readings(page: Page) -> list[str]:
    async def run() -> list[str]:
        async with page.run_test() as pilot:
            await pilot.pause()
            page.post_message(Page.Read())
            await settle_page(page)
            return list(page.readings)

    return asyncio.run(run())


def test_a_worker_whose_starting_message_is_still_queued_is_waited_for() -> None:
    """The Accounts page on windows-latest (CI run 36078630575): its ``Show`` was
    queued when the test settled, so there was no worker to wait for yet, and a
    wait for the workers that existed returned with the reading still to come."""
    assert _settled_readings(Page()) == ["first"]


def test_a_worker_that_a_finished_workers_handler_starts_is_waited_for() -> None:
    """One snapshot of the workers misses the one a finishing worker's handler
    starts (review of the accounts stack's fold, round 2, F8)."""
    assert _settled_readings(Page(then=True)) == ["first", "second"]


def test_a_worker_that_fails_on_purpose_is_settled_and_its_failure_read() -> None:
    """A scripted refusal is the designed outcome, so a failed worker has ended: the
    settle returns and the page shows the failure. ``workers.wait_for_complete()``
    raised ``WorkerFailed`` instead whenever the worker was still registered when the
    wait began, which a reading held past the pause always is (card
    tsk_01m3pt9eme0h: test_ui_spawn's fleet-error tests failed so on windows-latest)."""
    assert _settled_readings(Page(fails=True)) == ["first failed: first: unreadable"]


def test_no_ui_test_waits_with_wait_for_complete() -> None:
    """Every test settles through ``tests/ui_workers``, never ``workers.wait_for_complete()``.

    That call raises ``WorkerFailed`` for a worker that failed on purpose while it is
    still registered, so a dialog test reading a scripted refusal failed on
    windows-latest whenever the worker's thread lost the race: test_ui_spawn's
    fleet-error tests at #234, and test_ui_shell's refusals at #230 and #232, before
    main-sync brought this family to the RC (card tsk_01m3pt9eme0h). A module that
    keeps its own copy drifts back to it, as the Project page's did; this names every
    call left.
    """
    tests = Path(__file__).parent
    calls = [
        f"{path.relative_to(tests).as_posix()}:{node.lineno}"
        for path in sorted(tests.rglob("*.py"))
        if "fixtures" not in path.relative_to(tests).parts
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "wait_for_complete"
    ]
    assert calls == []
