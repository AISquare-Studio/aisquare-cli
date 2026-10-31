"""The paint the click guard waits for, and the presses either side of it, for a test. Not a
test module.

A :class:`~aisquare.cli.ui.click_guard.GuardedButton` ignores a mouse press until the
change in its place has been painted and the app has judged every input queued by then.
Under ``Pilot`` that is over by the next pause, which ``Pilot.click`` takes before it sends
its press, so a test about a press that lands while the guard is up holds the paint back:
:func:`install`, and the guard of every button that changes stays up until
:meth:`PaintHold.release`.

A press that reaches the app before a real paint, as on 2026-10-19, is made with
:func:`busy` and :func:`queue_press`: the app is kept busy, as it is while it awaits the
mount of a view, the press waits in its queue as the terminal's driver puts it there, and
the screen lays out and paints apart; :func:`painted` waits for that paint.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from textual import events
from textual.app import App
from textual.geometry import Offset
from textual.widget import Widget

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


async def painted(widget: Widget) -> None:
    """Return once the screen has been refreshed after every wait for a refresh ``widget`` has
    asked for so far: a :class:`GuardedButton`'s own wait for its paint among them."""
    done = asyncio.Event()
    widget.call_after_refresh(done.set)
    await asyncio.wait_for(done.wait(), 10)


def queue_press(app: App[Any], where: Offset) -> None:
    """A press at ``where``, queued at ``app`` as the terminal's driver queues it: the down and
    the up, and the app makes the click of them when it reaches them (``Pilot.click`` hands
    all three to the screen at once instead)."""
    x, y = where
    for kind in (events.MouseDown, events.MouseUp):
        app.post_message(kind(None, x, y, 0, 0, 1, False, False, False, screen_x=x, screen_y=y))
