"""Textual's timers on Windows wait without holding a thread (``aisquare.cli.ui.timers``).

Off Windows nothing changes. On Windows (``tests.windows_timers``, on any OS) a timer still
fires at its time, and asks a thread only for the last ``CEILING`` of it.
"""

from __future__ import annotations

import asyncio

import pytest
import textual._time as textual_time
import textual.timer as textual_timer

from aisquare.cli.ui import timers
from aisquare.cli.ui.clipboard import ClipboardApp
from tests import windows_timers


def test_off_windows_textual_s_own_sleep_stands(monkeypatch: pytest.MonkeyPatch) -> None:
    """macOS and Linux: Textual's sleep is asyncio's already, holding no thread; the same
    callbacks come at the same times because nothing is changed."""
    monkeypatch.setattr(timers, "WINDOWS", False)
    before = vars(textual_timer)["sleep"]

    assert timers.install() is False
    ClipboardApp()

    assert vars(textual_timer)["sleep"] is before is textual_time.sleep


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
    assert held and max(held) <= timers.CEILING, sorted(held)[-3:]


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

    assert [secs for secs in held if secs > timers.CEILING] == []
