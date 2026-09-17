"""The drag handle between two horizontal neighbours (#137).

docs/plans/fleet-tui.md §2 fixed the navigator at 30 columns. Project names,
worktree labels and agent rows truncate there, and an agent's pane wants every
column it can get on a laptop, so the partition is a one-column
:class:`Divider` the user drags — the way tmux, VS Code and every split-pane
TUI allow. It answers the keyboard too (:meth:`Divider.step` and
:meth:`Divider.reset`, called by the container that owns it) and a double
click (reset).

The widget knows its LEFT neighbour by selector and nothing else about the
layout it sits in. Its bounds are the neighbour's own ``min-width`` and
``max-width``: the stylesheet says the floor once (``Sidebar { min-width: 24
}``), and the container that lays out both sides keeps ``max-width`` current
from the columns the other side must keep (``app.Panes``), so Textual clamps
the neighbour on EVERY layout pass — a terminal that shrinks re-clamps with no
handler here, and the handle can never be pushed off screen. What a gesture
asked for is kept on the widget and every step starts from it, never from the
laid-out size, which is stale until the next layout pass: a key held at
autorepeat used to lose four steps in five.

Given a ``state_key`` the widget remembers every settled width under it in
``state.json`` (:mod:`aisquare.core.state_file` — the one reader and writer of
the file the theme and the pinned project share): restored at mount, saved once
a gesture settles, debounced so a held key is one write, skipped when the file
already says so. A reset FORGETS the key rather than saving a number, so the
next launch shows whatever the stylesheet says by then.

Textual 8.2.8 ships no splitter. Its ``ScrollBar`` is the reference for the
mouse protocol here: ``ALLOW_SELECT = False`` (a drag handle is not text, and
the screen opens a selection BEFORE it forwards the press, so ``event.stop()``
is too late); the capture let go on ``MouseRelease`` (posted when a screen is
pushed mid-drag) and on ``Hide``; and a release that never arrives — the button
let go outside the terminal — read off the first move with no button held.
"""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual import events
from textual.app import RenderResult
from textual.css.scalar import Scalar
from textual.message import Message
from textual.timer import Timer
from textual.widget import Widget

from aisquare.core import paths
from aisquare.core.state_file import read_state, update_state

LINE = "│"
"""Textual's ``solid`` border glyph, so the partition looks as the border it replaced did."""


def cells(scalar: Scalar | None) -> int | None:
    """A style's value in columns; ``None`` when unset or not in cells (%, fr, auto)."""
    return int(scalar.value) if scalar is not None and scalar.is_cells else None


class Divider(Widget):
    """One column between two neighbours; drag it to resize the one on its left.

    Mouse-down captures the mouse, so the drag and the release arrive wherever
    the pointer goes; every move with the button held sets the neighbour's width
    from the pointer's screen column; whatever ends the drag settles the width
    (:class:`Divider.Resized`, and the save when there is a ``state_key``).
    Hover tints the column, a drag fills it, and ``-neighbour-focused`` — set by
    the container, which sees ``DescendantFocus`` — lights it ``$accent`` while
    the neighbour has focus, the signal the neighbour's own border used to give.
    """

    ALLOW_SELECT: ClassVar[bool] = False
    """A drag handle is not text (Textual's own ``ScrollBar`` says the same)."""

    DEFAULT_CSS = """
    Divider { width: 1; height: 1fr; color: $primary; pointer: ew-resize; }
    Divider.-neighbour-focused { color: $accent; }
    Divider:hover { background: $accent 40%; }
    Divider.-dragging { background: $accent; color: $accent; }
    """

    SAVE_DEBOUNCE: float = 0.1
    """Seconds a settled width waits to be written: a held key is one write, not thirty a second."""

    class Resized(Message):
        """The partition settled — a drag ended, a key stepped it, a reset.

        ``width`` is what the neighbour was set to; ``None`` means the reset put
        the stylesheet's width back.
        """

        def __init__(self, width: int | None) -> None:
            super().__init__()
            self.width = width

    def __init__(self, target: str, *, state_key: str | None = None, id: str | None = None) -> None:
        super().__init__(id=id)
        self._target = target
        """The selector of the neighbour whose width this handle sets."""
        self._state_key = state_key
        """The ``state.json`` key the width is remembered under; ``None`` remembers nothing."""
        self._width: int | None = None
        """What the last gesture asked for; ``None`` until one did, and after a reset."""
        self._dragging = False
        self._moved = False
        """Whether the pointer moved during the gesture now running — a drag, not a click."""
        self._last_click_still = False
        """Whether the previous click was a click (no pointer movement) — half of a double click."""
        self._remembered: int | None = None
        """The width on disk, as far as this widget knows."""
        self._pending: int | None = None
        self._save_due = False
        self._save_timer: Timer | None = None
        self._refused = False
        """Whether the file has already refused a save this session — it is said once."""

    # --- the neighbour and its bounds ------------------------------------------------

    @property
    def target(self) -> Widget:
        """The neighbour whose width this handle sets."""
        return self.screen.query_one(self._target)

    def bounds(self) -> tuple[int, int | None]:
        """The neighbour's ``min-width`` and ``max-width`` in columns, or ``None`` for no bound."""
        styles = self.target.styles
        return cells(styles.min_width) or 1, cells(styles.max_width)

    def clamp(self, wanted: int) -> int:
        """The width nearest ``wanted`` within the bounds; the floor wins when they cross."""
        lower, upper = self.bounds()
        width = max(lower, int(wanted))
        return width if upper is None else min(width, max(lower, upper))

    @property
    def width(self) -> int:
        """The neighbour's intended width, within the bounds in force now.

        The last gesture's ask when there was one, else what the stylesheet
        declares (else what the layout gave). Never the laid-out size after a
        gesture: that is stale until the next layout pass, and stepping from it
        lost every key of a burst but the first.
        """
        if self._width is not None:
            return self.clamp(self._width)
        declared = cells(self.target.styles.base.width)
        return self.clamp(declared if declared is not None else self.target.outer_size.width)

    def resize_to(self, wanted: int) -> int:
        """Set the neighbour's width to ``wanted`` within the bounds; returns what was set."""
        width = self.clamp(wanted)
        self._width = width
        self.target.styles.width = width
        return width

    def step(self, delta: int) -> int:
        """Move the partition by ``delta`` columns and settle there."""
        width = self.resize_to(self.width + delta)
        self._settle(width)
        return width

    def reset(self) -> None:
        """Back to the stylesheet's width, and no remembered one."""
        self._width = None
        self.target.styles.width = None
        self._settle(None)

    def _settle(self, width: int | None) -> None:
        self.post_message(self.Resized(width))
        self._remember(width)

    # --- painting -----------------------------------------------------------------------

    def render(self) -> RenderResult:
        return Text("\n".join([LINE] * max(1, self.size.height)))

    # --- the mouse ----------------------------------------------------------------------

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button != 1:
            return
        event.stop()
        self._dragging = True
        self._moved = False
        self.add_class("-dragging")
        self.capture_mouse()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if not self._dragging:
            return
        event.stop()
        if event.button == 0:
            # The release never reached us — let go outside the terminal, or the
            # driver dropped it — and the first move with no button held says so.
            # Left captured, EVERY mouse event in the app would come here and
            # the neighbour would follow the bare pointer.
            self._end_drag()
            return
        self._moved = True
        # The pointer's column IS the partition: the neighbour starts at its
        # region's left edge, so the width is the distance from there.
        self.resize_to(event.screen_x - self.target.region.x)

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if not self._dragging or event.button != 1:
            return  # a right click mid-drag is a paste gesture, not the end of this one
        event.stop()
        self.resize_to(event.screen_x - self.target.region.x)
        self._end_drag()

    def on_mouse_release(self, event: events.MouseRelease) -> None:
        """The app took the capture away (a screen was pushed mid-drag): the drag is over."""
        self._end_drag()

    def on_hide(self, event: events.Hide) -> None:
        self._end_drag()

    def _end_drag(self) -> None:
        """Settle the width the drag reached and let the mouse go — once, whatever ended it."""
        if not self._dragging:
            return
        self._dragging = False
        self.remove_class("-dragging")
        if self.app.mouse_captured is self:
            self.release_mouse()
        self._settle(self.width)

    def on_click(self, event: events.Click) -> None:
        """A double click resets — two clicks that were clicks.

        Textual counts a drag's release as a click (the handle follows the
        pointer, so the release lands on the widget the press did), and a tap on
        the handle within half a second of it arrives as ``chain == 2``: the
        drag would be thrown away and the reset saved. So a click that moved
        does not count, and neither does the one right after it.
        """
        still = not self._moved
        double = event.chain >= 2 and still and self._last_click_still
        self._last_click_still = still
        if double and event.button == 1:
            event.stop()
            self.reset()

    # --- the memory ---------------------------------------------------------------------

    def on_mount(self) -> None:
        if self._state_key is None:
            return
        saved = read_state().get(self._state_key)
        if isinstance(saved, int) and not isinstance(saved, bool):
            self._remembered = saved
            self.resize_to(saved)

    def on_unmount(self) -> None:
        self._dragging = False
        self._flush_save()

    def _remember(self, width: int | None) -> None:
        """Queue the write: one per burst, and none when the file already says ``width``."""
        if self._state_key is None:
            return
        if self._save_timer is not None:
            self._save_timer.stop()
        if width == self._remembered:
            self._save_due = False  # back where the file already is: a queued save would lie
            return
        self._pending, self._save_due = width, True
        self._save_timer = self.set_timer(self.SAVE_DEBOUNCE, self._flush_save, name="divider-save")

    def _flush_save(self) -> None:
        if not self._save_due or self._state_key is None:
            return
        self._save_due = False
        width = self._pending
        if update_state(self._state_key, width):
            self._remembered = width
        elif not self._refused:
            self._refused = True
            self.notify(
                f"{paths.state_path()} could not be updated (not a JSON object, or not writable) — "
                "the navigator's width will not be remembered",
                severity="warning",
                timeout=8,
                markup=False,
            )
