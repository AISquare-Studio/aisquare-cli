"""The wall clock, for the tests that tell two moments apart by it."""

from __future__ import annotations

import time
from datetime import UTC, datetime

_sleep = time.sleep


def after_the_tick() -> None:
    """Return once the wall clock reads later than it did on entry.

    A Windows clock ticks about every 15 ms, and since the board stopped waiting for the disk
    (``conftest.the_board_does_not_wait_for_the_disk``) two writes in a row can land in one
    tick. A row made in it is not OLDER than a cutoff of "now", an event is not BEFORE a
    ``last_seen_at`` of the same instant, a scan stamped twice reads as no scan, and two rows
    that tie are listed in the order of their random ids (run 40450023264, #29). A test that
    tells moments apart by the clock calls this between them. On a clock that reads to the
    microsecond it returns at once: no wait of a length chosen by the test.
    """
    start = datetime.now(tz=UTC)
    deadline = time.monotonic() + 2.0
    while datetime.now(tz=UTC) <= start:
        if time.monotonic() > deadline:
            raise AssertionError("the wall clock did not move in 2 s")
        _sleep(0.001)
