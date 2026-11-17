"""Textual's timers on Windows wait without holding a thread (``aisquare.cli.ui.timers``).

Off Windows nothing changes. On Windows (``tests.windows_timers``, on any OS) a timer still
fires at its time, and asks a thread only for the last ``CEILING`` of it.

From tester-rlx-calm's review of #526 (note 12799):

- reset mid-wait: Textual's own semantics (the wait in flight ends, then a whole interval),
  identical here, as Textual alone on Windows and as asq on Windows;
- pause: a paused timer does not fire until resumed, in all three;
- Windows-like clocks: Textual's clock on its own epoch (perf_counter on Windows, not
  asyncio's monotonic) and asyncio's first wait waking a coarse tick early or late
  (py3.12's Windows loop: ``_clock_resolution`` 15.6 ms). asq's timers still fire never
  early, and hold a thread for ``CEILING`` plus one tick at most. A deadline on asyncio's
  clock read against Textual's fired 34 to 64 ms early there (mutant M2);
- the real platform alone decides the install, with no ``WINDOWS`` monkeypatch: a module
  forced to install everywhere passed a pin that set ``WINDOWS`` itself (mutant M4).
"""

from __future__ import annotations

import asyncio
import sys
import time
import types
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
import textual._time as textual_time
import textual.timer as textual_timer

from aisquare.cli.ui import timers
from aisquare.cli.ui.clipboard import ClipboardApp
from tests import windows_timers


def test_the_real_platform_alone_decides_the_install(monkeypatch: pytest.MonkeyPatch) -> None:
    """macOS and Linux keep Textual's own sleep, asyncio's already, so the same callbacks come
    at the same times; Windows has asq's. No ``WINDOWS`` set here: the platform decides."""
    monkeypatch.setattr(textual_timer, "sleep", vars(textual_timer)["sleep"])  # undone after
    ClipboardApp()

    installed = vars(textual_timer)["sleep"] is timers.sleep
    assert installed is (sys.platform == "win32"), vars(textual_timer)["sleep"]
    if not installed:
        assert vars(textual_timer)["sleep"] is textual_time.sleep, "Textual's own, as it was"


def test_the_longest_hold_is_the_ceiling_and_two_ticks_of_asyncio_s_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """asyncio's wait may end up to two resolutions of its clock early: one lag, one by which
    its loop runs a timer before its time. windows-py3.12 (``GetTickCount64``, 15.625 ms) held
    a thread 71 ms (#526's run 44083919544); within this bound, never past it.

    On the running platform first, whatever its clock: windows-py3.12's own is 81.25 ms (a
    pin that took it for CEILING could not pass there, tester 12813). Then on windows-py3.12's
    monotonic clock alone: perf_counter's resolution, about 100 ns there, is not asyncio's
    (mutant M11)."""
    real = time.get_clock_info

    def windows_py312(name: Any) -> Any:
        if name != "monotonic":
            return real(name)
        return types.SimpleNamespace(**{**vars(real(name)), "resolution": 0.015625})

    here = timers.longest_hold()
    resolution = time.get_clock_info("monotonic").resolution
    monkeypatch.setattr(time, "get_clock_info", windows_py312)

    assert here == pytest.approx(timers.CEILING + 2 * resolution), "this platform's own"
    assert timers.longest_hold() == pytest.approx(0.08125)
    assert timers.longest_hold() >= 0.071, "the hold measured on windows-py3.12 is within it"


def test_a_textual_whose_timers_sleep_otherwise_keeps_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def other(secs: float) -> None:
        await asyncio.sleep(secs)

    monkeypatch.setattr(timers, "WINDOWS", True)
    monkeypatch.setattr(textual_timer, "sleep", other)

    assert timers.install() is False
    assert vars(textual_timer)["sleep"] is other


def _fired(delays: dict[str, float], interval: float, ticks: int) -> dict[str, list[float]]:
    """Each one-shot timer's and one repeating timer's firing times, after it was set, in an
    asq app (Textual's clock)."""
    seen: dict[str, list[float]] = {name: [] for name in [*delays, "every"]}

    async def run() -> None:
        app: ClipboardApp[None] = ClipboardApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            start = textual_time.get_time()
            for name, delay in delays.items():
                app.set_timer(
                    delay, lambda name=name: seen[name].append(textual_time.get_time() - start)
                )
            app.set_interval(
                interval, lambda: seen["every"].append(textual_time.get_time() - start)
            )
            while len(seen["every"]) < ticks or any(not seen[name] for name in delays):
                await asyncio.sleep(0.01)

    asyncio.run(run())
    return seen


def test_under_asq_s_sleep_a_timer_fires_at_its_time_never_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wait on asyncio's clock ends CEILING before the time, and Textual's precise sleep
    takes it the rest of the way: never early, and each in a thread for CEILING at most."""
    held = windows_timers.emulate(monkeypatch, asq=True)

    seen = _fired({"short": 0.03, "long": 0.4}, interval=0.12, ticks=4)

    assert vars(textual_timer)["sleep"] is timers.sleep, "installed by the app, as on Windows"
    assert seen["short"][0] >= 0.03 - 0.002 and seen["long"][0] >= 0.4 - 0.002, seen
    assert [at >= 0.12 * (n + 1) - 0.002 for n, at in enumerate(seen["every"][:4])] == [True] * 4
    assert held and max(held) <= timers.longest_hold(), sorted(held)[-3:]


def test_a_long_timer_stopped_before_its_time_never_took_a_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = windows_timers.emulate(monkeypatch, asq=True)

    async def run() -> None:
        app: ClipboardApp[None] = ClipboardApp()
        async with app.run_test() as pilot:
            timer = app.set_timer(3600, lambda: None)
            await pilot.pause(0.2)
            timer.stop()
            await pilot.pause()

    asyncio.run(run())

    assert [secs for secs in held if secs > timers.longest_hold()] == []


MODES = ["here", "textual on windows", "asq on windows"]


def _mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> list[float]:
    if mode == "here":
        return []
    return windows_timers.emulate(monkeypatch, asq=mode == "asq on windows")


def _run(body: Callable[[ClipboardApp[None]], Awaitable[None]]) -> None:
    async def run() -> None:
        app: ClipboardApp[None] = ClipboardApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await body(app)

    asyncio.run(run())


async def _until(cond: Callable[[], bool], limit: float = 3.0) -> None:
    end = time.monotonic() + limit
    while not cond() and time.monotonic() < end:
        await asyncio.sleep(0.005)


def _early(mode: str) -> float:
    """How early a reset timer may fire, by its sleep.

    - Off Windows, "here" is Textual's own sleep, which is asyncio's. Its wait may end up to
      two resolutions of its clock early (``timers.longest_hold``), and a reset fires after
      two waits: nothing on a precise clock, 62.5 ms under windows-py3.12's.
    - Textual's on Windows, the emulated one and asq's all end on a precise sleep: 2 ms at
      most, as Textual's own allows.
    """
    slack = 0.002
    if mode == "here" and sys.platform != "win32":
        slack += 2 * 2 * time.get_clock_info("monotonic").resolution
    return slack


@pytest.mark.parametrize("mode", MODES)
def test_a_timer_reset_mid_wait_fires_at_textual_s_new_time(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    held = _mode(monkeypatch, mode)
    once: list[float] = []
    every: list[float] = []

    async def body(app: ClipboardApp[None]) -> None:
        start = textual_time.get_time()
        one = app.set_timer(0.3, lambda: once.append(textual_time.get_time() - start))
        rep = app.set_interval(0.2, lambda: every.append(textual_time.get_time() - start))
        await asyncio.sleep(0.1)
        one.reset()
        rep.reset()
        await _until(lambda: bool(once) and len(every) >= 2)
        await asyncio.sleep(0.15)

    _run(body)

    # Textual's reset: the wait in flight runs out (0.3 / 0.2), then a whole interval.
    early = _early(mode)
    assert len(once) == 1 and 0.6 - early <= once[0] < 0.6 + 0.12, once
    assert 0.4 - early <= every[0] < 0.4 + 0.12, every
    assert 0.6 - early <= every[1] < 0.6 + 0.12, every
    if mode == "asq on windows":
        assert held and max(held) <= timers.longest_hold(), sorted(held)[-3:]


@pytest.mark.parametrize("mode", MODES)
def test_a_paused_timer_does_not_fire_until_resumed(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    held = _mode(monkeypatch, mode)
    once: list[float] = []
    every: list[float] = []
    marks: dict[str, float] = {}

    async def body(app: ClipboardApp[None]) -> None:
        start = textual_time.get_time()
        one = app.set_timer(0.2, lambda: once.append(textual_time.get_time() - start))
        rep = app.set_interval(0.15, lambda: every.append(textual_time.get_time() - start))
        await asyncio.sleep(0.05)
        one.pause()
        await _until(lambda: len(every) >= 1)
        rep.pause()
        marks["paused"] = textual_time.get_time() - start
        await asyncio.sleep(0.6)
        marks["resumed"] = textual_time.get_time() - start
        marks["once_during"] = len(once)
        marks["every_during"] = len([at for at in every if at > marks["paused"]])
        one.resume()
        rep.resume()
        await _until(lambda: bool(once) and any(at > marks["resumed"] for at in every))

    _run(body)

    assert marks["once_during"] == 0 and marks["every_during"] == 0, (marks, once, every)
    assert once and marks["resumed"] <= once[0] < marks["resumed"] + 0.12, (marks, once)
    assert any(marks["resumed"] <= at < marks["resumed"] + 0.3 for at in every), (marks, every)
    if mode == "asq on windows":
        assert held and max(held) <= timers.longest_hold(), sorted(held)[-3:]


TICK = 0.0156  # py3.12's Windows asyncio: time.get_clock_info("monotonic").resolution


def test_windows_like_clocks_never_early_and_brief(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = windows_timers.emulate(monkeypatch, asq=True)
    perf = time.perf_counter
    monkeypatch.setattr(textual_time, "get_time", lambda: perf() + 1000.0)
    flips = {"n": 0}

    async def coarse_sleep(secs: float, *a: Any) -> None:
        # asyncio's own wait on Windows ends within a coarse tick of its time, either way
        flips["n"] += 1
        skew = TICK * 0.95 * (1 if flips["n"] % 2 else -1)
        await asyncio.sleep(max(0.0, secs + skew))

    monkeypatch.setattr(
        timers,
        "asyncio",
        types.SimpleNamespace(sleep=coarse_sleep, get_running_loop=asyncio.get_running_loop),
    )
    delays = [0.15, 0.2, 0.25, 0.3, 0.35, 0.4]
    fired: dict[float, list[float]] = {d: [] for d in delays}
    every: list[float] = []

    async def body(app: ClipboardApp[None]) -> None:
        start = textual_time.get_time()
        for d in delays:
            app.set_timer(d, lambda d=d: fired[d].append(textual_time.get_time() - start))
        app.set_interval(0.12, lambda: every.append(textual_time.get_time() - start))
        await _until(lambda: all(fired.values()) and len(every) >= 5)

    _run(body)

    assert flips["n"] >= 8, flips
    late = {d: round((fired[d][0] - d) * 1000, 2) for d in delays}
    late_every = [round((at - 0.12 * (n + 1)) * 1000, 2) for n, at in enumerate(every[:5])]
    assert all(ms >= -2 for ms in [*late.values(), *late_every]), (late, late_every)
    assert held and max(held) <= timers.longest_hold() + TICK, sorted(held)[-3:]
