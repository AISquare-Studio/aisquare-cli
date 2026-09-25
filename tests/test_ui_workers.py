"""``settle_page``: a test that reads the page waits for what the page has set in motion.

The helper is the tests' own (``tests/ui_workers.py``), shared by the Accounts
and Project pages' tests. These pin the two races it closes, on an app small
enough that nothing else is in flight: a reading held far longer than one
pause's idle check, started the way the fleet UI's pages start theirs.
"""

from __future__ import annotations

import asyncio
import time

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

    def __init__(self, *, then: bool = False) -> None:
        super().__init__()
        self.readings: list[str] = []
        self.then = then
        """Whether the first reading's handler starts a second."""

    def on_page_read(self, event: Page.Read) -> None:
        self._read("first")

    def _read(self, name: str) -> None:
        def reading() -> str:
            time.sleep(HELD)
            return name

        self.run_worker(reading, name=name, thread=True)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
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
