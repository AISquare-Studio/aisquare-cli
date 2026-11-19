"""Textual's timers as they sleep on Windows, on any OS.

On Windows Textual's timer sleep (``textual._time.sleep``, which ``textual.timer`` binds)
is a blocking wait in asyncio's default executor (``textual._win_sleep``), and its cancel
sets an event from that executor too. The executor has 8 threads, a 4-vCPU runner's
``min(32, cpu + 4)``. So a timer holds a thread for as long as it waits there.

:func:`emulate` puts that sleep in place, both where Textual defines it and where its timers
read it, with an 8-thread default executor. With ``asq=True``, asq's app installs its own
sleep, as it does on Windows (``cli.ui.timers``). With ``asq=False`` it is Textual alone.
It returns the seconds each wait asked to hold a thread for, as asked.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from typing import Any

import pytest
import textual._time as textual_time
import textual.timer as textual_timer

from aisquare.cli.ui import timers


def emulate(monkeypatch: pytest.MonkeyPatch, *, asq: bool) -> list[float]:
    held: list[float] = []
    real_init = concurrent.futures.ThreadPoolExecutor.__init__

    def eight(
        self: Any, max_workers: Any = None, thread_name_prefix: str = "", *a: Any, **k: Any
    ) -> None:
        if max_workers is None and thread_name_prefix == "asyncio":
            max_workers = 8
        real_init(self, max_workers, thread_name_prefix, *a, **k)

    async def win_sleep(secs: float) -> None:
        sleep_for = max(0.0, secs - 0.001)
        if sleep_for < 0.0005:
            return
        held.append(sleep_for)
        cancel = threading.Event()
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, cancel.wait, sleep_for)
        except asyncio.CancelledError:
            await loop.run_in_executor(None, cancel.set)
            raise

    async def sleep(secs: float) -> None:
        await asyncio.create_task(win_sleep(secs))

    monkeypatch.setattr(concurrent.futures.ThreadPoolExecutor, "__init__", eight)
    monkeypatch.setattr(textual_time, "sleep", sleep)
    monkeypatch.setattr(textual_timer, "sleep", sleep)
    monkeypatch.setattr(timers, "WINDOWS", asq)
    return held
