"""The click guard's clock, stepped by a test instead of slept through. Not a test module.

``aisquare.cli.ui.click_guard`` reads its clock through the module, so a test installs
one of these and steps it past :data:`~aisquare.cli.ui.click_guard.CLICK_GUARD` where a
person would have waited a moment before clicking.
"""

from __future__ import annotations

import pytest

from aisquare.cli.ui import click_guard


class GuardClock:
    """A monotonic clock that moves only when :meth:`step` moves it."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def step(self, seconds: float | None = None) -> None:
        """Move on ``seconds``, by default just past the guard's window."""
        self.now += click_guard.CLICK_GUARD + 0.01 if seconds is None else seconds


def install(monkeypatch: pytest.MonkeyPatch) -> GuardClock:
    clock = GuardClock()
    monkeypatch.setattr(click_guard, "clock", clock)
    return clock
