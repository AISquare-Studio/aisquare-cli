"""The click guard: a button that a click cannot press by landing on it as it appears.

User feedback (2026-10-19): "my mouse is either on stop or restart and that had
destroyed my manager session on more than one occasion". The screen changed
under a pointer that was resting where the user had aimed it: another view
came up, with its Stop and Restart right where the click landed. The no-jump
fix (``FleetApp.on_agent_restarted``) takes away the change that did it then.
This guard covers every other one: a view switched to, a button that appears,
a layout that moves a button, a dialog closing off it.

:class:`GuardedButton` ignores a mouse press that lands within
:data:`CLICK_GUARD` seconds of its own place on screen changing. A key press
(enter on the focused button) is a deliberate gesture, and so is
:meth:`Button.press`; neither is guarded.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from textual import events
from textual.geometry import NULL_REGION, Region
from textual.screen import Screen
from textual.widgets import Button

CLICK_GUARD = 1.0
"""Seconds after a :class:`GuardedButton` was shown, moved or uncovered during which a
mouse press on it is ignored: longer than a click aimed at what was there just before,
shorter than a person's next deliberate one."""

clock: Callable[[], float] = time.monotonic
"""The guard's clock, read through the module so a test can step it."""


class GuardedButton(Button):
    """A :class:`Button` that ignores a mouse press within :data:`CLICK_GUARD` of the content
    under the pointer changing.

    It watches its own place on its screen. After each layout pass
    (``Screen.screen_layout_refresh_signal``) it compares the region it occupies with
    the last one. A view switched to, the button mounted or shown again, and a bar
    re-laid out around it all change that region, and each starts the window again.
    So does its screen becoming the current one again (``App.screen_change_signal``),
    as when a dialog over it closes. An ignored press posts nothing, and the first
    one in a window says so, quietly.

    A drop-in: the same constructor, and a press it lets through posts the same
    ``Button.Pressed``, so an ``@on(Button.Pressed, "#id")`` handler stays as it is.
    """

    _placed_at: Region = NULL_REGION
    _changed_at: float | None = None
    _told_at: float | None = None

    def on_mount(self) -> None:
        self.screen.screen_layout_refresh_signal.subscribe(self, self._laid_out, immediate=True)
        self.app.screen_change_signal.subscribe(self, self._screen_changed, immediate=True)

    def _laid_out(self, screen: Screen[Any]) -> None:
        region = self.region  # NULL_REGION while it, or the view it is on, is hidden
        if region != self._placed_at:
            self._placed_at = region
            self.changed_under_pointer()

    def _screen_changed(self, screen: Screen[Any]) -> None:
        if screen is self.screen:
            self.changed_under_pointer()

    def changed_under_pointer(self) -> None:
        """Start the window again from now: what is under the pointer just changed in a way
        the button's own place does not show."""
        self._changed_at = clock()

    @property
    def guarding(self) -> bool:
        """Whether a mouse press now would be ignored."""
        changed = self._changed_at
        return changed is not None and clock() - changed < CLICK_GUARD

    async def _on_click(self, event: events.Click) -> None:
        # No super() call: Textual dispatches Button._on_click itself after this one, and
        # ``prevent_default`` is what stops it (``MessagePump._get_dispatch_methods``).
        if not self.guarding:
            return
        event.prevent_default()
        event.stop()
        if self._told_at != self._changed_at:
            self._told_at = self._changed_at
            self.notify(
                f"ignored a click on {self.label} — the view had just changed under the pointer",
                timeout=4,
                markup=False,
            )
