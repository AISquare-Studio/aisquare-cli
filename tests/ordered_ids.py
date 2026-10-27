"""Ids that sort in the order they were minted, however fast a test mints them. Not a test module.

``aisquare.core.ids`` makes ids time-sortable to the millisecond and random inside it, and the
store lists tasks and events ``ORDER BY id``. A test that adds two hundred tasks in a loop can
put two in one millisecond, and those list in either order. A test that asserts the order it
added in installs this first; the ids then differ by a millisecond each at least, the way they
would had each been added a moment apart. (The start pack's budget test did: it was red for
another reason too, which its own comment says.)
"""

from __future__ import annotations

import time

import pytest

from aisquare.core import ids


class _StrictlyForward:
    """``time`` as ``aisquare.core.ids`` reads it: the clock, never the same millisecond twice."""

    def __init__(self) -> None:
        self._last_ms = 0

    def time(self) -> float:
        self._last_ms = max(int(time.time() * 1000), self._last_ms + 1)
        return (self._last_ms + 0.5) / 1000  # mid-millisecond, so float rounding cannot step back


def install(monkeypatch: pytest.MonkeyPatch) -> None:
    """From here to the end of the test, every id minted sorts after the one before it."""
    monkeypatch.setattr(ids, "time", _StrictlyForward())
