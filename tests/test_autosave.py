"""``cli.ui.autosave.Autosave`` — one writer per key, in order, off the event loop, joined at quit.

Driven with a bare Textual app as the host and ``update_state`` slowed or
failing on demand, so the window a real save leaves open (milliseconds on a
quiet SSD, seconds under a held lock) is wide enough to press into.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static
from textual.widgets._toast import Toast

from aisquare.cli.ui import autosave as autosave_mod
from aisquare.cli.ui.autosave import Autosave
from aisquare.core import state_file
from aisquare.core.atomic import write_replacing
from aisquare.core.state_file import StateUnwritableError, read_state, update_state


class _Host(App[None]):
    def compose(self) -> ComposeResult:
        yield Static("host")


class _Boom(BaseException):
    """Not an ``Exception``: what the drain's last line of defence is for."""


class _Writer:
    """An ``update_state`` that may sleep or fail per value, and records every call."""

    def __init__(
        self,
        *,
        delay: Callable[[object], float] = lambda value: 0.0,
        fail: Callable[[object, int], BaseException | None] = lambda value, attempt: None,
    ) -> None:
        self.delay = delay
        self.fail = fail
        self.calls: list[object] = []
        """Every value handed over, in order — landed or not."""
        self.started = threading.Event()
        self.active = 0
        self.peak = 0
        self._guard = threading.Lock()

    def __call__(self, key: str, value: object) -> None:
        with self._guard:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.calls.append(value)
            attempt = self.calls.count(value)
        try:
            self.started.set()
            time.sleep(self.delay(value))
            exc = self.fail(value, attempt)
            if exc is not None:
                raise exc
            update_state(key, value)
        finally:
            with self._guard:
                self.active -= 1


def _run(
    fn: Callable[[Autosave, App[None]], Awaitable[None]], *, notifications: bool = False
) -> None:
    async def go() -> None:
        async with _Host().run_test(size=(40, 5), notifications=notifications) as pilot:
            await pilot.pause()
            saver = Autosave(pilot.app, "sidebar_width", what="it", initial=None)
            await fn(saver, pilot.app)
            await asyncio.to_thread(saver.join, 10.0)

    asyncio.run(go())


async def _in_flight(writer: _Writer) -> None:
    """Return once the drain is inside a write (the debounce has fired, the sleep has begun)."""
    for _ in range(500):
        if writer.started.is_set():
            writer.started.clear()
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the drain never started writing")


async def _settled(saver: Autosave) -> None:
    assert await asyncio.to_thread(saver.settled, 10.0), "the saver never settled"


def test_a_value_that_arrives_mid_write_lands_after_it_and_one_writer_runs_at_a_time(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = _Writer(delay=lambda value: 0.3)
    monkeypatch.setattr(autosave_mod, "update_state", writer)

    async def go(saver: Autosave, app: App[None]) -> None:
        saver.remember(34)
        await _in_flight(writer)
        saver.remember(38)  # while 34 is being written
        saver.remember(42)  # and again: the drain takes the latest when it comes round
        await _settled(saver)

    _run(go)
    assert writer.calls == [34, 42], "in order, the burst collapsed"
    assert writer.peak == 1, "one writer per key"
    assert read_state() == {"sidebar_width": 42}


def test_flush_at_quit_waits_for_the_write_in_flight_and_then_writes_the_newest(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`flush` used to write on the loop thread while the drain was polling the same lock;
    when flush won the race the drain's OLDER value landed last. Only the in-flight value is
    slow here, so a flush that wrote on its own thread would overtake it."""
    writer = _Writer(delay=lambda value: 0.3 if value == 34 else 0.0)
    monkeypatch.setattr(autosave_mod, "update_state", writer)

    async def go(saver: Autosave, app: App[None]) -> None:
        saver.remember(34)
        await _in_flight(writer)
        saver.remember(38)
        started = time.monotonic()
        unsaved = saver.flush()  # on the loop, where production calls it
        assert time.monotonic() - started < Autosave.JOIN_S, "bounded"
        assert unsaved is None

    _run(go)
    assert writer.calls == [34, 38]
    assert read_state() == {"sidebar_width": 38}


def test_back_to_what_the_file_says_during_a_write_is_written_after_it(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dedupe against the last CONFIRMED write found 30 == 30 while 34 was in flight and
    cancelled nothing, so the screen showed 30 and the file said 34."""
    update_state("sidebar_width", 30)
    writer = _Writer(delay=lambda value: 0.3)
    monkeypatch.setattr(autosave_mod, "update_state", writer)

    async def go(saver: Autosave, app: App[None]) -> None:
        saver.remember(34)
        await _in_flight(writer)
        saver.remember(30)  # back — while 34 is still being written
        await _settled(saver)

    _run(go)
    assert writer.calls == [34, 30], "the step back lands after the write it could not cancel"
    assert read_state() == {"sidebar_width": 30}


def test_what_the_file_already_says_is_handed_over_but_not_rewritten(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The "nothing to write" decision is `update_state`'s, under the lock: this process's
    belief about a key another process also writes is not the truth (a pick that came back to
    the value read at start was dropped after `board -w` had changed the file underneath)."""
    update_state("sidebar_width", 30)
    writer = _Writer()
    monkeypatch.setattr(autosave_mod, "update_state", writer)
    rewrites: list[str] = []

    def spy(target: Path, body: str, *, keep_mode: bool = True, durable: bool = True) -> None:
        rewrites.append(body)
        write_replacing(target, body, keep_mode=keep_mode, durable=durable)

    monkeypatch.setattr(state_file, "write_replacing", spy)

    async def go(saver: Autosave, app: App[None]) -> None:
        saver.remember(34)
        saver.remember(30)  # inside the debounce: the file already says 30
        await _settled(saver)
        # Another process changed the file underneath; the same pick must land now.
        write_replacing(isolated_home / "state.json", '{"sidebar_width": 99}\n')
        saver.remember(30)
        await _settled(saver)

    _run(go)
    assert writer.calls == [30, 30], "handed over both times"
    assert len(rewrites) == 1 and '"sidebar_width": 30' in rewrites[0], "written only when needed"
    assert read_state() == {"sidebar_width": 30}


def test_a_refused_value_is_retried_by_the_next_remember_and_by_the_quit_flush(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal left `latest` holding a value the file never got, and the same value
    remembered again was dropped as already saved — silently, since the toast had been said."""
    writer = _Writer(
        fail=lambda value, attempt: (
            StateUnwritableError("state.json.lock is held by another process")
            if attempt == 1
            else None
        )
    )
    monkeypatch.setattr(autosave_mod, "update_state", writer)

    async def go(saver: Autosave, app: App[None]) -> None:
        saver.remember(34)
        await _settled(saver)  # refused: dirty, with the reason
        assert (
            saver.dirty
            and saver.unsaved == "it was not saved: state.json.lock is held by another process"
        )
        saver.remember(34)  # the same value again: not "already saved"
        await _settled(saver)
        assert not saver.dirty
        saver.remember(38)
        await _settled(saver)  # refused
        assert saver.flush() is None, "the lock is free by quit: it lands"

    _run(go)
    assert writer.calls == [34, 34, 38, 38]
    assert read_state() == {"sidebar_width": 38}


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_the_drain_recovers_from_a_refusal_an_unexpected_error_and_a_base_exception(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_running` was reset only when the drain exited normally; after any exception every later
    start returned early and the preference silently stopped saving for the session."""

    def fail(value: object, attempt: int) -> BaseException | None:
        if attempt > 1:
            return None
        if value == 1:
            return StateUnwritableError("state.json is not a JSON object")
        if value == 2:
            return RuntimeError("something nobody expected")
        if value == 3:
            return _Boom()
        return None

    writer = _Writer(fail=fail)
    monkeypatch.setattr(autosave_mod, "update_state", writer)

    async def go(saver: Autosave, app: App[None]) -> None:
        for value in (1, 2, 3, 4):
            saver.remember(value)
            await _settled(saver)

    _run(go)
    assert writer.calls == [1, 2, 3, 4], "each value was handed over; each failure stopped nothing"
    assert read_state() == {"sidebar_width": 4}


def test_an_unexpected_error_is_a_toast_that_names_it(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = _Writer(fail=lambda value, attempt: RuntimeError("disk on fire"))
    monkeypatch.setattr(autosave_mod, "update_state", writer)
    seen: list[str] = []

    async def go(saver: Autosave, app: App[None]) -> None:
        saver.remember(34)
        await _settled(saver)
        await asyncio.sleep(0.05)  # the toast is posted to the loop after the refusal
        seen.extend(toast.render().plain for toast in app.screen.query(Toast))

    _run(go, notifications=True)
    assert len(seen) == 1
    assert "could not be saved: RuntimeError('disk on fire')" in seen[0]
    assert "it could not be saved; it will be retried" in seen[0]


def test_a_quit_that_runs_out_of_time_says_so_and_starts_no_further_write(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The join's timeout was ignored: the value was lost without a word and a write cut off at
    interpreter exit left a temp file and no `state.json`."""
    writer = _Writer(delay=lambda value: 0.6)
    monkeypatch.setattr(autosave_mod, "update_state", writer)

    async def go(saver: Autosave, app: App[None]) -> None:
        saver.remember(34)
        await _in_flight(writer)
        unsaved = saver.flush(0.1)
        assert unsaved == "it was not saved: the write of 34 did not finish before the app closed"
        saver.remember(38)
        saver.wake()  # closed: nothing more may start
        await asyncio.to_thread(saver.join, 10.0)

    _run(go)
    assert writer.calls == [34], "the in-flight write finished; no new one began"
    assert read_state() == {"sidebar_width": 34}


def test_flush_all_joins_every_saver_against_one_deadline(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flushed one after the other, two slow savers froze the quit twice over. Called on the
    loop, as `on_unmount` does, with the debounce timers out of the way: only `flush_all` may
    wake the drains, or the timers would start both and hide a sequential flush."""
    writer = _Writer(delay=lambda value: 0.4)
    monkeypatch.setattr(autosave_mod, "update_state", writer)
    monkeypatch.setattr(Autosave, "DEBOUNCE", 5.0)

    async def go() -> None:
        async with _Host().run_test(size=(40, 5)) as pilot:
            await pilot.pause()
            width = Autosave(pilot.app, "sidebar_width", what="the width")
            theme = Autosave(pilot.app, "board_theme", what="the theme")
            width.remember(34)
            theme.remember("nord")
            started = time.monotonic()
            unsaved = Autosave.flush_all(pilot.app)
            elapsed = time.monotonic() - started
            assert unsaved == []
            assert elapsed < 0.7, f"two 0.4 s writes joined together, not in turn: {elapsed:.2f}s"

    asyncio.run(go())
    assert sorted(map(str, writer.calls)) == ["34", "nord"]
    assert read_state() == {"sidebar_width": 34, "board_theme": "nord"}


def test_flush_all_closes_a_saver_that_misses_the_deadline_and_names_what_was_cut_off(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = _Writer(delay=lambda value: 0.8 if value == 34 else 0.0)
    monkeypatch.setattr(autosave_mod, "update_state", writer)
    monkeypatch.setattr(Autosave, "DEBOUNCE", 5.0)
    savers: list[Autosave] = []

    async def go() -> None:
        async with _Host().run_test(size=(40, 5)) as pilot:
            await pilot.pause()
            width = Autosave(pilot.app, "sidebar_width", what="the width")
            theme = Autosave(pilot.app, "board_theme", what="the theme")
            savers.extend((width, theme))
            width.remember(34)
            theme.remember("nord")
            unsaved = Autosave.flush_all(pilot.app, timeout=0.2)
            assert unsaved == [
                "the width was not saved: the write of 34 did not finish before the app closed"
            ]
            width.remember(38)
            width.wake()  # closed: nothing more may start
            await asyncio.to_thread(width.join, 10.0)

    asyncio.run(go())
    assert writer.calls.count(38) == 0, "closed at the deadline: no new write began"
    assert read_state() == {"sidebar_width": 34, "board_theme": "nord"}


class _StallingGuard:
    """The saver's guard, sleeping after the drain thread's ``n``th release — the window between
    "found nothing to do" and a later reset of ``_running`` that round 6 #3 closed."""

    def __init__(self, inner: threading.Lock, *, stall_after_release: int, stall: float) -> None:
        self._inner = inner
        self._n = stall_after_release
        self._stall = stall
        self.releases = 0

    def __enter__(self) -> _StallingGuard:
        self._inner.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self._inner.release()
        if threading.current_thread().name.startswith("autosave:"):
            self.releases += 1
            if self.releases == self._n:
                time.sleep(self._stall)


def test_a_value_queued_as_the_drain_finds_nothing_to_do_is_not_stranded(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The drain takes the guard three times for one value: to take it, to clear the in-flight
    mark, and to find nothing left. `_running` is cleared inside that third section; cleared
    under a fourth acquisition instead, a `remember` + `flush` in between saw a drain "running"
    that was about to exit and the last step was lost at quit."""
    writer = _Writer()
    monkeypatch.setattr(autosave_mod, "update_state", writer)

    async def go(saver: Autosave, app: App[None]) -> None:
        guard = _StallingGuard(saver._guard, stall_after_release=3, stall=0.4)
        saver._guard = guard  # type: ignore[assignment]
        saver.remember(34)
        for _ in range(500):  # until the drain is inside the stall
            if guard.releases >= 3:
                break
            await asyncio.sleep(0.01)
        assert guard.releases >= 3, "the drain never reached its exit"
        saver.remember(38)
        assert saver.flush() is None

    _run(go)
    assert writer.calls == [34, 38]
    assert read_state() == {"sidebar_width": 38}


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_value_a_crash_interrupted_is_kept_for_the_next_wake(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = _Writer(fail=lambda value, attempt: _Boom() if attempt == 1 else None)
    monkeypatch.setattr(autosave_mod, "update_state", writer)

    async def go(saver: Autosave, app: App[None]) -> None:
        saver.remember(3)
        await _settled(saver)  # the thread died; the value is dirty again, with the reason
        assert saver.dirty and (saver.unsaved or "").startswith("it was not saved: sidebar_width")
        saver.wake()  # no new value: the kept one is what is written
        await _settled(saver)

    _run(go)
    assert writer.calls == [3, 3]
    assert read_state() == {"sidebar_width": 3}


def test_an_app_and_its_savers_are_collectable_once_gone(isolated_home: Path) -> None:
    """The registry was a `WeakKeyDictionary` whose values (a list of savers, each holding its
    host) kept the key alive: every app ever built stayed in memory — one per `asq` process,
    hundreds per test run."""
    import gc
    import weakref

    app = _Host()
    Autosave(app, "board_theme", what="the theme")
    ref = weakref.ref(app)
    del app
    gc.collect()
    assert ref() is None
