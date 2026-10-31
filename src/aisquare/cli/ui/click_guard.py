"""The click guard: a button that a click cannot press by landing on it as it appears.

User feedback (2026-10-19): "my mouse is either on stop or restart and that had
destroyed my manager session on more than one occasion". The screen changed
under a pointer that was resting where the user had aimed it: another view
came up, with its Stop and Restart right where the click landed. The no-jump
fix (``FleetApp.on_agent_restarted``) takes away the change that did it then.
This guard covers every other one: a view switched to, a button that appears,
a layout that moves a button, a dialog closing off it.

:class:`GuardedButton` ignores a mouse press that reaches the app before the frame
that shows its own changed place has reached the terminal (a second at most, when
the terminal cannot keep up), and the press does not take the focus either. Once
that frame is out, the next press goes through at once: there is no time to wait
out (user, 2026-10-30: the second it used to hold a button after it was drawn, and
the toast saying so, were in the way of someone who knows where the button is). An
ignored press says nothing. A key press (enter on the focused button) is a
deliberate gesture, and so is :meth:`Button.press`; neither is guarded.
"""

from __future__ import annotations

import asyncio
import struct
import sys
from typing import Any

from textual import events
from textual.app import App
from textual.geometry import NULL_REGION, Region
from textual.screen import Screen
from textual.widgets import Button

if sys.platform != "win32":
    import fcntl
    import termios

WRITTEN_POLL = 0.001
"""Seconds between looks at whether a frame has left for the terminal (:func:`written`). Not a
wait: a frame is usually out within one."""

WRITTEN_MAX = 1.0
"""Seconds after the paint the guard waits at most for the terminal: a ceiling, not a floor.
:func:`written` waits for every frame drawn so far to be read, and a terminal or link slower
than asq's output may not read them all for as long as a pane streams: Stop was dead for 9.4 s
with the terminal reading 64 KB/s (REVIEW 478 r3). The lag the UI tester saw was 270 ms."""


def written(app: App[Any]) -> bool:
    """Whether what reads asq's tty has read every frame ``app`` has drawn so far.

    Textual hands a frame to its driver's writer thread, which writes it to the tty, and the
    terminal reads it from there. Either can lag: in 2 of 74 switches the frame showing a view
    reached the terminal 270 ms after asq had drawn it (UI tester on #8, PR #478 at 695e19bb).
    What reads the tty need not be the screen: under tmux it is the tmux server, over SSH it is
    sshd, and the rest of the way is out of asq's sight.

    Each doubt answers yes, as the guard did at the paint before this: a driver with no writer
    thread writes as it is called, or not at all (headless, as under ``Pilot``); a tty that does
    not say what it holds unread (a Linux pty, :func:`_unread`) counts as read once the writer
    is done with it; and a writer this does not know answers yes. The writer thread and the
    queue's waiters are Textual's and CPython's private parts, and a change in either raised in
    the layout signal, where Textual logs it, and kept the guard up for the session (REVIEW 478
    r3).
    """
    try:
        writer = getattr(app._driver, "_writer_thread", None)
        if writer is None or not writer.is_alive():
            return True
        queue = writer._queue
        with queue.mutex:
            # It flushes when its queue runs dry, then waits on it: waiting on an empty queue,
            # it has written all it took. Its only waiter is itself (``put`` waits on not_full).
            if queue.queue or not queue.not_empty._waiters:
                return False
        fd = writer.fileno()
    except (AttributeError, TypeError, ValueError, OSError):
        return True
    return _unread(fd) == 0


def _unread(fd: int) -> int:
    """Bytes written to the tty at ``fd`` that the terminal has not read yet: a macOS pty holds
    1 KB, so a frame that fits leaves the writer idle before the terminal has it. 0 where the
    OS does not say (a Linux pty hands bytes to the terminal's side as they are written) or
    ``fd`` is no tty."""
    if sys.platform == "win32":
        return 0
    try:
        (count,) = struct.unpack("i", fcntl.ioctl(fd, termios.TIOCOUTQ, b"\0" * 4))
    except OSError:
        return 0
    return int(count)


class GuardedButton(Button):
    """A :class:`Button` that ignores a mouse press that reached the app before the terminal
    showed it where it now is.

    It watches its own place on its screen. After each layout pass
    (``Screen.screen_layout_refresh_signal``) it compares the region it occupies with
    the last one. A view switched to, the button mounted or shown again, and a bar
    re-laid out around it all change that region, and each puts the guard up. So does its
    screen becoming the current one again (``App.screen_change_signal``), as when a
    dialog over it closes (:meth:`changed_under_pointer`). The guard comes down once a
    layout pass has painted the change, the terminal has read that frame (:func:`written`,
    :data:`WRITTEN_MAX` at most) and the app has judged every input it had queued by then
    (:meth:`_laid_out`), and not a frame later. A hidden button is not waited for: nothing
    can click it.
    An ignored press posts nothing, says nothing and leaves the focus where it was. The
    verdict is made once per press, at its mouse-down, and stands for the focus and the
    click alike.

    A drop-in: the same constructor, and a press it lets through posts the same
    ``Button.Pressed``, so an ``@on(Button.Pressed, "#id")`` handler stays as it is.
    """

    _placed_at: Region = NULL_REGION
    _change: int = 0
    """How many times what is under the pointer has changed; only the latest change's paint
    takes the guard down (:meth:`_painted`)."""
    _guarding: bool = False
    _unpainted: bool = False
    """A change is waiting for a layout pass that paints it: one the button's own place does
    not show (:meth:`changed_under_pointer`), or one laid out while Textual batched updates
    (:meth:`_laid_out`)."""
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
            self._put_up()
        elif not self._unpainted:
            return
        if self.app._batch_count:
            # Textual is batching updates: it publishes this pass now and paints it on the
            # screen's next message, and drops that frame if the batch is still on by then.
            # A press could reach the app before anything is painted (UI tester on #8, PR
            # #478 at 695e19bb). The guard waits for a pass made with no batch on, which
            # paints before it publishes; this asks for one.
            self._unpainted = True
            self.screen.refresh(layout=True)
            return
        self._unpainted = False
        # ``Screen._refresh_layout`` has just painted this pass and handed the frame to the
        # driver. Once the terminal has read it, a press the app had queued by then was
        # made on what the screen showed before: the app busy mounting the view that put
        # this button under the pointer is the 2026-10-19 case. So the guard comes down
        # from the back of the app's queue, and every input ahead of it is judged with the
        # guard up first. Not ``call_after_refresh``: that runs after the screen's NEXT
        # refresh, behind every callback queued there, and on the Settings hub that was the
        # restyle of the focus moving to its first link, up to a second later (UI tester on
        # #8, PR #478 at 41569b99).
        self._when_written(self._change, asyncio.get_running_loop().time() + WRITTEN_MAX)

    def _when_written(self, change: int, deadline: float) -> None:
        if change != self._change or not self.is_attached:
            return  # a newer change waits for its own frame
        loop = asyncio.get_running_loop()
        # Hidden (the view switched away from), it cannot be clicked: no look at the terminal.
        if self._placed_at == NULL_REGION or written(self.app) or loop.time() >= deadline:
            self.app.call_later(self._painted, change)
        else:
            loop.call_later(WRITTEN_POLL, self._when_written, change, deadline)

    def _screen_changed(self, screen: Screen[Any]) -> None:
        if screen is self.screen:
            self.changed_under_pointer()

    def changed_under_pointer(self) -> None:
        """Put the guard up until the next layout pass has painted the screen: what is under
        the pointer just changed in a way the button's own place does not show."""
        self._put_up()
        self._unpainted = True
        # That pass, asked of the screen itself, now: nothing may have moved. Its own flag is
        # set at once, so the frame the screen paints next (its resume's repaint, when a
        # dialog over it closed) is that pass. Asked of the button instead, the request
        # reached the screen as a message, often after that frame, and the guard stayed up
        # 0.1-0.36 s after the dialog had visibly gone (UI tester on #8, 26643fa7).
        self.screen.refresh(layout=True)

    def _put_up(self) -> None:
        self._change += 1
        self._guarding = True

    def _painted(self, change: int) -> None:
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
