"""The click guard: a button that a click cannot press by landing on it as it appears.

User feedback (2026-10-19): "my mouse is either on stop or restart and that had
destroyed my manager session on more than one occasion". The screen changed
under a pointer that was resting where the user had aimed it: another view
came up, with its Stop and Restart right where the click landed. The no-jump
fix (``FleetApp.on_agent_restarted``) takes away the change that did it then.
This guard covers every other one: a view switched to, a button that appears,
a layout that moves a button, a dialog closing off it.

:class:`GuardedButton` ignores a mouse press that reaches the app before its own
changed place on screen has been painted, and the press does not take the focus
either. Once that paint is out, the next press goes through at once: there is no
time to wait out (user, 2026-10-30: the second it used to hold a button after it was
drawn, and the toast saying so, were in the way of someone who knows where the button
is). An ignored press says nothing. A key press (enter on the focused button) is a
deliberate gesture, and so is :meth:`Button.press`; neither is guarded.
"""

from __future__ import annotations

from typing import Any

from textual import events
from textual.geometry import NULL_REGION, Region
from textual.screen import Screen
from textual.widgets import Button


class GuardedButton(Button):
    """A :class:`Button` that ignores a mouse press made before the screen showed it where it
    now is.

    It watches its own place on its screen. After each layout pass
    (``Screen.screen_layout_refresh_signal``) it compares the region it occupies with
    the last one. A view switched to, the button mounted or shown again, and a bar
    re-laid out around it all change that region, and each puts the guard up. So does its
    screen becoming the current one again (``App.screen_change_signal``), as when a
    dialog over it closes. The guard comes down once that change has been painted and
    every input the app had queued by then has been judged (:meth:`changed_under_pointer`).
    An ignored press posts nothing, says nothing and leaves the focus where it was. The
    verdict is made once per press, at its mouse-down, and stands for the focus and the
    click alike.

    A drop-in: the same constructor, and a press it lets through posts the same
    ``Button.Pressed``, so an ``@on(Button.Pressed, "#id")`` handler stays as it is.
    """

    _placed_at: Region = NULL_REGION
    _change: int = 0
    """How many times what is under the pointer has changed; only the latest change's paint
    takes the guard down (:meth:`_cleared`)."""
    _guarding: bool = False
    _down_guarded: bool | None = None
    """The verdict on the press in progress, made at its mouse-down (:meth:`focus_on_click`);
    ``None`` once its click has used it."""

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
        """Put the guard up until this change is painted: what is under the pointer just
        changed in a way the button's own place does not show."""
        self._change += 1
        self._guarding = True
        self.call_after_refresh(self._painted, self._change)

    def _painted(self, change: int) -> None:
        # The paint is out, but a press the app had queued before it was made on what the
        # screen showed before: the app busy mounting the view that put this button under
        # the pointer is the 2026-10-19 case. Taking the guard down here would let that
        # press through when the app reached it. Down at the back of the app's queue, every
        # input queued ahead of it is judged with the guard up first.
        self.app.call_later(self._cleared, change)

    def _cleared(self, change: int) -> None:
        if change == self._change:  # an older change's paint leaves a newer one's guard up
            self._guarding = False

    @property
    def guarding(self) -> bool:
        """Whether a mouse press now would be ignored."""
        return self._guarding

    def focus_on_click(self) -> bool:
        # Textual focuses on the mouse-down, before the click is dropped below. Enter is
        # never guarded, so a Stop focused by an ignored click was pressed by the next
        # Enter, and in ConfirmBulk that click took the focus off Cancel (review of #266,
        # round 2). Asked once per mouse-down on the button, so the verdict is kept here
        # for the click, which comes at the mouse-up: judged again there, a press held
        # while the guard went up or down got one verdict for the focus and the other for
        # the press, and a Stop focused by a click then ignored was pressed by the next
        # Enter all the same (review of #266, round 3).
        self._down_guarded = self.guarding
        return not self._down_guarded and super().focus_on_click()

    async def _on_click(self, event: events.Click) -> None:
        # No super() call: Textual dispatches Button._on_click itself after this one, and
        # ``prevent_default`` is what stops it (``MessagePump._get_dispatch_methods``).
        guarded = self.guarding if self._down_guarded is None else self._down_guarded
        self._down_guarded = None
        if guarded:
            event.prevent_default()
            event.stop()
