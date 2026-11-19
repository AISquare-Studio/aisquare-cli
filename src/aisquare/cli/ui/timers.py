"""Textual's timers on Windows, waiting without holding a thread.

On Windows, Textual sleeps every timer in a thread of asyncio's default executor
(``textual._win_sleep``: a high-resolution waitable timer), for the timer's whole interval.
Its cancel takes a thread too. A repeating timer re-arms as it fires, so it holds a thread
almost for good. That executor is the one Textual's thread workers and ``asyncio.to_thread``
run on, eight threads on CI's 4-vCPU runners, and asq's own timers filled it:

- the refresh every 2 s;
- the account headroom every 60 s;
- the housekeeping and Remote's tunnel check every 30 s;
- the Accounts page's usage every 60 s and its sign-in poll;
- each toast its 4 to 12 s.

A worker, or the quit, then waited behind them. `test_quitting_mid_sign_in_cancels_the_device_flow`
failed on windows-py3.12: R17's run 44051094043 and the rc/rlx gate run 44072523971.

:func:`sleep` waits on asyncio's own clock, which holds no thread, until :data:`CEILING`
before the time, and only then on Textual's precise sleep. The callback comes when Textual's
would, read on Textual's own clock (``perf_counter`` on Windows, not asyncio's). No timer
holds a thread for longer than :func:`longest_hold`, whoever set it: asq, Textual, or a
timer added later. That is :data:`CEILING` plus however early asyncio's own wait may end.

Off Windows, Textual's sleep is asyncio's already, and nothing is installed.
"""

from __future__ import annotations

import asyncio
import sys
import time

import textual._time as textual_time
import textual.timer as textual_timer

WINDOWS = sys.platform == "win32"
"""Whether to install :func:`sleep` (:func:`install`): Textual's timers hold a thread only there."""

CEILING = 0.05
"""How much of its time a Textual timer waits in a thread: the last moments before it fires,
where Textual's precise sleep keeps it on time. asyncio's own wait may end early, and then a
little more is left for the thread (:func:`longest_hold`)."""


async def sleep(secs: float) -> None:
    """Textual's timer sleep, holding no thread until :data:`CEILING` before the time.

    The deadline and what is left of it are both read on Textual's clock, which its timer
    keeps time by: on Windows that is ``perf_counter``, whose epoch is not asyncio's. Read
    on asyncio's, the deadline fired 34 to 64 ms early there (tester's mutant M2, #526)."""
    deadline = textual_time.get_time() + secs
    if secs > CEILING:
        await asyncio.sleep(secs - CEILING)
    left = deadline - textual_time.get_time()
    if left > 0:
        await textual_time.sleep(left)  # read now, as Textual's own: the precise end


def longest_hold() -> float:
    """The longest a timer waits in a thread: :data:`CEILING`, and as much again as asyncio's
    wait before it may end early.

    asyncio's loop runs a timer up to one resolution of its clock before the timer's time
    (``_clock_resolution``), and reads that clock (``time.monotonic``), which lags real time
    by up to one more. On Windows before Python 3.13 the clock is ``GetTickCount64``, at
    15.6 ms, so a wait ends up to 31 ms early, and up to about 81 ms is left for the
    thread. windows-py3.12 measured 71 ms (#526's run 44083919544). Elsewhere it is
    :data:`CEILING`, to the nanosecond."""
    return CEILING + 2 * time.get_clock_info("monotonic").resolution


def install() -> bool:
    """Have every Textual timer sleep as :func:`sleep` does, on Windows. Whether it does.

    It is done once per process; a Textual whose timer no longer sleeps by
    ``textual._time.sleep`` keeps its own."""
    if not WINDOWS:
        return False
    if getattr(textual_timer, "sleep", None) is sleep:
        return True
    if getattr(textual_timer, "sleep", None) is not getattr(textual_time, "sleep", None):
        return False
    textual_timer.sleep = sleep  # type: ignore[attr-defined]
    return True
