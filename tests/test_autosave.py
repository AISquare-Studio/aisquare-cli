"""``cli.ui.autosave.Autosave`` — one writer per key, in order, off the event loop, joined at quit.

Driven with a bare Textual app as the host and ``update_state`` slowed, so the
window a real save leaves open (milliseconds on a quiet SSD, seconds under a
held lock) is wide enough to press into.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static

from aisquare.cli.ui import autosave as autosave_mod
from aisquare.cli.ui.autosave import Autosave
from aisquare.core.state_file import StateUnwritableError, read_state, update_state


class _Host(App[None]):
    def compose(self) -> ComposeResult:
        yield Static("host")


class _SlowWriter:
    """An ``update_state`` that takes ``delay`` seconds and records what it saw."""

    def __init__(
        self, delay: float, *, fail: Callable[[object], BaseException | None] | None = None
    ):
        self.delay = delay
        self.fail = fail
        self.writes: list[object] = []
        self.started = threading.Event()
        self.active = 0
        self.peak = 0
        self._guard = threading.Lock()

    def __call__(self, key: str, value: object) -> None:
        with self._guard:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            self.started.set()
            time.sleep(self.delay)
            if self.fail is not None and (exc := self.fail(value)) is not None:
                raise exc
            update_state(key, value)
            self.writes.append(value)
        finally:
            with self._guard:
                self.active -= 1


def _run(fn: Callable[[Autosave], "asyncio.Future[None] | object"]) -> None:  # noqa: UP037
    async def go() -> None:
        async with _Host().run_test(size=(20, 5)) as pilot:
            await pilot.pause()
            saver = Autosave(pilot.app, "sidebar_width", what="it", initial=None)
            result = fn(saver)
            if asyncio.iscoroutine(result):
                await result
            await asyncio.to_thread(saver.wait, 10.0)

    asyncio.run(go())


async def _in_flight(writer: _SlowWriter, saver: Autosave) -> None:
    """Return once the drain is inside a write (the debounce has fired, the sleep has begun)."""
    for _ in range(200):
        if writer.started.is_set():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the drain never started writing")


def test_a_value_that_arrives_mid_write_lands_after_it_and_one_writer_runs_at_a_time(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = _SlowWriter(0.3)
    monkeypatch.setattr(autosave_mod, "update_state", writer)

    async def go(saver: Autosave) -> None:
        saver.remember(34)
        await _in_flight(writer, saver)
        saver.remember(38)  # while 34 is being written
        saver.remember(42)  # and again: the drain takes the latest when it comes round
        await asyncio.to_thread(saver.wait, 10.0)

    _run(go)
    assert writer.writes == [34, 42], "in order, the burst collapsed"
    assert writer.peak == 1, "one writer per key"
    assert read_state() == {"sidebar_width": 42}


def test_flush_at_quit_waits_for_the_write_in_flight_and_then_writes_the_newest(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`flush` used to write on the loop thread while the drain was polling the same lock;
    when flush won the race the drain's OLDER value landed last."""
    writer = _SlowWriter(0.3)
    monkeypatch.setattr(autosave_mod, "update_state", writer)

    async def go(saver: Autosave) -> None:
        saver.remember(34)
        await _in_flight(writer, saver)
        saver.remember(38)
        started = time.monotonic()
        await asyncio.to_thread(saver.flush)
        assert time.monotonic() - started < Autosave.JOIN_S, "bounded"

    _run(go)
    assert writer.writes == [34, 38]
    assert read_state() == {"sidebar_width": 38}


def test_back_to_what_the_file_says_during_a_write_is_written_after_it(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dedupe compared against the last CONFIRMED write: a step back while 34 was in flight
    found 30 == 30 and cancelled nothing, so the screen showed 30 and the file said 34."""
    update_state("sidebar_width", 30)
    writer = _SlowWriter(0.3)
    monkeypatch.setattr(autosave_mod, "update_state", writer)

    async def go(saver: Autosave) -> None:
        saver._latest = saver._on_file = 30  # what the file said at start
        saver.remember(34)
        await _in_flight(writer, saver)
        saver.remember(30)  # back — while 34 is still being written
        await asyncio.to_thread(saver.wait, 10.0)

    _run(go)
    assert writer.writes == [34, 30], "the step back lands after the write it could not cancel"
    assert read_state() == {"sidebar_width": 30}


def test_back_to_what_the_file_says_before_the_write_starts_writes_nothing(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    update_state("sidebar_width", 30)
    writer = _SlowWriter(0.0)
    monkeypatch.setattr(autosave_mod, "update_state", writer)

    async def go(saver: Autosave) -> None:
        saver._latest = saver._on_file = 30
        saver.remember(34)
        saver.remember(30)  # inside the debounce: the queued 34 is dropped, nothing to write
        await asyncio.sleep(Autosave.DEBOUNCE * 3)
        saver.remember(30)  # what the file says: nothing
        await asyncio.sleep(Autosave.DEBOUNCE * 3)

    _run(go)
    assert writer.writes == []


def test_the_drain_recovers_from_a_refusal_and_from_an_unexpected_error(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_running` was reset only when the drain exited normally; after any exception every later
    start returned early and the preference silently stopped saving for the session."""

    def fail(value: object) -> BaseException | None:
        if value == 1:
            return StateUnwritableError("state.json is not a JSON object")
        if value == 2:
            return RuntimeError("something nobody expected")
        return None

    writer = _SlowWriter(0.0, fail=fail)
    monkeypatch.setattr(autosave_mod, "update_state", writer)

    async def go(saver: Autosave) -> None:
        for value in (1, 2, 3):
            saver.remember(value)
            await asyncio.sleep(Autosave.DEBOUNCE * 3)
            await asyncio.to_thread(saver.wait, 10.0)

    _run(go)
    assert writer.writes == [3], "the third value is written after a refusal and after a crash"
    assert read_state() == {"sidebar_width": 3}
