"""The paint the click guard waits for, and the presses either side of it, for a test. Not a
test module.

A :class:`~aisquare.cli.ui.click_guard.GuardedButton` ignores a mouse press until the
change in its place has been painted, the terminal has read that frame and the app has
judged every input queued by then. Under ``Pilot`` that is over by the next pause, which
``Pilot.click`` takes before it sends its press, so a test about a press that lands while
the guard is up holds the paint back: :func:`install`, and the guard of every button that
changes stays up until :meth:`PaintHold.release`.

A press that reaches the app before a real paint, as on 2026-10-19, is made with
:func:`busy` and :func:`queue_press`: the app is kept busy, as it is while it awaits the
mount of a view, the press waits in its queue as the terminal's driver puts it there, and
the screen lays out and paints apart. :func:`next_frame` runs a step right after the next
frame, before the app reads another message: a press in the gap between a frame and the
next one. :func:`terminal_behind` gives the app a terminal that has not read what it was
sent: a press after the paint and before the terminal shows it. :func:`slow_after_refresh`
holds the screen's callbacks after the frame that shows a change, as a slow one queued
there does.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from collections.abc import Callable
from typing import Any

import pytest
from textual import events
from textual.app import App
from textual.drivers._writer_thread import WriterThread
from textual.geometry import Offset
from textual.screen import Screen

from aisquare.cli.ui import click_guard
from aisquare.cli.ui.click_guard import GuardedButton


class PaintHold:
    """The paints each :class:`GuardedButton` is waiting for, held until :meth:`release`."""

    def __init__(self) -> None:
        self.holding = True
        self.held: list[tuple[GuardedButton, int]] = []

    def release(self) -> None:
        """The paints come: every guard held up comes down as it would have, and none is
        held after."""
        self.holding = False
        held, self.held = self.held, []
        for button, change in held:
            _painted(button, change)

    def hold(self) -> None:
        """Hold the paints back again, from the next change on."""
        self.holding = True


_painted = GuardedButton._painted


def install(monkeypatch: pytest.MonkeyPatch) -> PaintHold:
    hold = PaintHold()

    def painted(button: GuardedButton, change: int) -> None:
        if hold.holding:
            hold.held.append((button, change))
        else:
            _painted(button, change)

    monkeypatch.setattr(GuardedButton, "_painted", painted)
    return hold


def busy(app: App[Any]) -> asyncio.Event:
    """Keep ``app`` busy from the message queued now on: nothing queued at it after this moves
    until the event it returns is set. Its screen lays out and paints all the same."""
    free = asyncio.Event()
    app.call_later(free.wait)
    return free


class NextFrame:
    """Steps to run once the current screen's next frame is out (:func:`next_frame`)."""

    def __init__(self) -> None:
        self.steps: list[Callable[[], object]] = []

    def then(self, step: Callable[[], object]) -> None:
        self.steps.append(step)


def next_frame(monkeypatch: pytest.MonkeyPatch) -> NextFrame:
    """Run each step handed to the result on the loop's turn after the current screen next
    paints (``Screen._compositor_refresh``): after the whole update that painted it, and
    before the app reads another message."""
    frame = NextFrame()
    compositor_refresh = Screen._compositor_refresh

    def painted(screen: Screen[Any]) -> None:
        compositor_refresh(screen)
        if screen.is_current:
            loop = asyncio.get_running_loop()
            while frame.steps:
                loop.call_soon(frame.steps.pop(0))

    monkeypatch.setattr(Screen, "_compositor_refresh", painted)
    return frame


def slow_after_refresh(monkeypatch: pytest.MonkeyPatch, shows: Callable[[], bool]) -> asyncio.Event:
    """At the first frame the current screen paints with ``shows()`` true, queue a callback after
    that refresh that runs until the event returned is set. Textual runs a screen's callbacks
    after a refresh (``call_after_refresh``) one by one, in its own queue, so each one asked
    for after this frame waits behind it. On the Settings hub that was the restyle of the focus
    moving to its first link, up to a second (UI tester on #8, PR #478 at 41569b99).

    It is queued as the frame goes out, before the layout signal sent with it, and before any
    widget can ask for a callback after it. The frame and the signal go out as ever, and the
    app's own queue moves as ever."""
    done = asyncio.Event()
    compositor_refresh = Screen._compositor_refresh
    queued = False

    async def restyle() -> None:
        await done.wait()

    def painted(screen: Screen[Any]) -> None:
        nonlocal queued
        compositor_refresh(screen)
        if not queued and screen.is_current and shows():
            queued = True
            screen._invoke_later(restyle, screen)

    monkeypatch.setattr(Screen, "_compositor_refresh", painted)
    return done


def queue_press(app: App[Any], where: Offset, *, at: float | None = None) -> float:
    """A press at ``where``, queued at ``app`` as the terminal's driver queues it: the down and
    the up, and the app makes the click of them when it reaches them (``Pilot.click`` hands
    all three to the screen at once instead), counting it in a run of clicks on that cell as
    Textual does (``Click.chain``). Returns the time the press is stamped with, on Textual's
    clock: now, or ``at``. Stamped, a press is as far from the one before as the stamps say,
    however late a loaded runner queues it: two presses on one cell stamped closer than
    ``App.CLICK_CHAIN_TIME_THRESHOLD`` are a double-click."""
    x, y = where
    stamp = 0.0
    for kind in (events.MouseDown, events.MouseUp):
        event = kind(None, x, y, 0, 0, 1, False, False, False, screen_x=x, screen_y=y)
        if at is not None:
            event.time = at
        stamp = event.time
        app.post_message(event)
    return stamp


class TerminalBehind:
    """A terminal that has not read what the app sent it (:func:`terminal_behind`): a pty, so
    not on Windows, where the tests that use it skip."""

    writer: WriterThread
    _terminal: int
    _unread: int

    def __init__(self, unread: int) -> None:
        if sys.platform == "win32":  # what mypy narrows; the tests that use it skip there
            pytest.skip("a pty for the terminal")
        self._terminal, tty = os.openpty()
        self.writer = WriterThread(os.fdopen(tty, "w"))
        self.writer.start()
        self.writer.write("x" * unread)
        self._unread = unread

    async def catch_up(self) -> None:
        """The terminal reads all it was sent."""
        await asyncio.to_thread(self._read, self._unread)
        self._unread = 0

    def _read(self, count: int) -> None:
        while count > 0:
            count -= len(os.read(self._terminal, 1 << 16))

    def close(self) -> None:
        if sys.platform == "win32":  # what mypy narrows; never made there (__init__)
            return
        stop = threading.Thread(target=self.writer.stop)  # it ends once it has written all
        stop.start()
        os.set_blocking(self._terminal, False)
        while stop.is_alive():
            try:
                os.read(self._terminal, 1 << 16)
            except BlockingIOError:
                stop.join(0.005)
        self.writer._file.close()
        os.close(self._terminal)


def terminal_behind(
    monkeypatch: pytest.MonkeyPatch, app: App[Any], unread: int, *, ceiling: float = 60.0
) -> TerminalBehind:
    """Send ``app``'s output the way Textual's terminal driver does, through its writer thread
    to a tty, and have the terminal at its other end read none of it until
    :meth:`TerminalBehind.catch_up`. ``unread`` bytes are on their way already: the frame,
    as far as the guard can know, that shows what the test changes next. More than the tty
    holds and the writer is still writing it; less (on macOS, whose pty holds 1 KB) and it
    sits in the tty. ``Pilot``'s own frames go nowhere, as ever. Close it after.

    The guard stops waiting for a terminal ``ceiling`` seconds after the paint
    (:data:`~aisquare.cli.ui.click_guard.WRITTEN_MAX`): a minute by default, so that a test
    about a press while the terminal is behind is not raced by a slow runner past the real
    one second."""
    monkeypatch.setattr(click_guard, "WRITTEN_MAX", ceiling)
    behind = TerminalBehind(unread)
    monkeypatch.setattr(app._driver, "_writer_thread", behind.writer, raising=False)
    return behind
