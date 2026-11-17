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
would, and no timer holds a thread for more than :data:`CEILING`, whoever set it: asq,
Textual or a timer added later.

Off Windows, Textual's sleep is asyncio's already, and nothing is installed.
"""

from __future__ import annotations

import asyncio
import sys

import textual._time as textual_time
import textual.timer as textual_timer

WINDOWS = sys.platform == "win32"
"""Whether to install :func:`sleep` (:func:`install`): Textual's timers hold a thread only there."""

CEILING = 0.05
"""The longest a Textual timer waits in a thread: the last moments before its time, where
Textual's precise sleep keeps it on time. asyncio's own clock on Windows is coarse (about
15 ms), finer than this."""


async def sleep(secs: float) -> None:
    """Textual's timer sleep, holding no thread until :data:`CEILING` before the time."""
    deadline = textual_time.get_time() + secs
    if secs > CEILING:
        await asyncio.sleep(secs - CEILING)
    left = deadline - textual_time.get_time()
    if left > 0:
        await textual_time.sleep(left)  # read now, as Textual's own: the precise end


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
