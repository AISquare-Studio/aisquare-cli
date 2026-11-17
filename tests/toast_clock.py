"""A clock only the test moves, for a notification's time: when it was raised, how much it
has left, and where a stopped one starts again.

A toast's time is wall time to Textual, so a test that raised a 0.4 s toast and read it
after a 0.05 s settle raced it: on a busy runner the toast closed by its own time first
(``IndexError`` at ``shown(app)[0]``, #524's matrix, py3.12-captain-off shard 4/4). On
this clock the time passes only when the test says (:meth:`ToastClock.advance`).

Three readers take it:

- ``Notification.raised_at``, stamped when Textual's ``App.notify`` makes one: a
  ``Notification`` whose stamp is this clock's, in ``textual.app``'s namespace;
- ``textual.notifications.time``, which ``time_left`` and the reaping of the expired read;
- ``aisquare.cli.ui.toasts.time``, which restarts a stopped toast (``toasts.release``).

The toast's own timer still runs on Textual's clock; what it decides when it fires, it reads
from this one. A toast whose time has not passed here re-arms and stays.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from textual import app as textual_app
from textual import notifications as textual_notifications
from textual.notifications import Notification

from aisquare.cli.ui import toasts


class ToastClock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def toast_clock(monkeypatch: pytest.MonkeyPatch) -> ToastClock:
    clock = ToastClock()

    @dataclass
    class ClockedNotification(Notification):
        raised_at: float = field(default_factory=clock)

    monkeypatch.setattr(textual_app, "Notification", ClockedNotification)
    monkeypatch.setattr(textual_notifications, "time", clock)
    monkeypatch.setattr(toasts, "time", SimpleNamespace(time=clock))
    return clock
