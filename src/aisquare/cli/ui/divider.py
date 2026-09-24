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
from textual.widget import Widget

from aisquare.cli.ui.autosave import Autosave
from aisquare.core.state_file import read_state

LINE = "│"
"""Textual's ``solid`` border glyph, so the partition looks as the border it replaced did."""


WIDEST_ASK = 10_000
"""The one bound a REMEMBERED width gets before the layout's: wider than any
terminal, small enough for the style setter (``float(10**400)`` overflowed) and
for a layout with no ceiling. Everything under it is the layout's to clamp."""


def cells(scalar: Scalar | None) -> int | None:
    """A style's value in columns; ``None`` when unset or not in cells (%, fr, auto)."""
    return None if scalar is None else scalar.cells


class Divider(Widget):
    """One column between two neighbours; drag it to resize the one on its left.

    Mouse-down captures the mouse, so the drag and the release arrive wherever
    the pointer goes; every move with the button held sets the neighbour's width
    from the pointer's screen column; whatever ends a drag that moved settles
    the width (the save, when there is a ``state_key``). Hover tints the
    column, a drag fills it, and ``-neighbour-focused`` — set by
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

    def __init__(self, target: str, *, state_key: str | None = None, id: str | None = None) -> None:
        super().__init__(id=id)
        self._target = target
        """The selector of the neighbour whose width this handle sets."""
        self._state_key = state_key
        """The ``state.json`` key the width is remembered under; ``None`` remembers nothing."""
        self._width: int | None = None
        """The ASK: what the last gesture asked for, or what the file remembered — not
        what is shown. The layout's ceiling bounds the display; a wider terminal
        later gets the ask back. ``None`` until there is one, and after a reset."""
        self._dragging = False
        self._moved = False
        """Whether the gesture now running changed the width — a drag, not a click.

        Set by a column change, not by a motion event: a one-row hand drift
        between press and release on this tall handle is routine and moves
        nothing.
        """
        self._last_click_still = False
        """Whether the previous click was a LEFT click that was a click (the width did not
        change). Another button, or a drag, breaks the chain."""
        self._drag_ceiling: int | None = None
        """The ceiling the drag now running last met, for a quit before its release
        (:meth:`on_unmount`): the neighbour whose styles give it may be pruned by then."""
        self._autosave: Autosave | None = None
        """The debounced, off-loop save under ``state_key``; ``None`` when there is no key. Its
        ``latest`` — the width this process last asked the file to hold, or read from it — is
        what a gesture is compared against; every number it hands out was capped at mount."""

    # --- the neighbour and its bounds ------------------------------------------------

    @property
    def target(self) -> Widget:
        """The neighbour whose width this handle sets."""
        return self.screen.query_one(self._target)

    def bounds(self) -> tuple[int, int]:
        """The neighbour's floor and ceiling in columns.

        The floor is its ``min-width`` (``0`` means 0; unset, or a unit that is
        not cells, means 1). The ceiling is its ``max-width`` — the container's
        (``app.Panes``) — and, until one is written, the terminal's width less
        this column: a neighbour is never wider than the screen, whatever a
        file says. The floor wins when the two cross.
        """
        styles = self.target.styles
        floor = cells(styles.min_width)
        lower = floor if floor is not None else 1
        upper = cells(styles.max_width)
        if upper is None:
            upper = self.app.size.width - 1
        return lower, max(lower, upper)

    def clamp(self, wanted: int) -> int:
        """The width nearest ``wanted`` within the bounds."""
        lower, upper = self.bounds()
        return max(lower, min(int(wanted), upper))

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
        """Move the partition by ``delta`` columns and settle there — unless the bounds ate it.

        On a terminal narrower than the remembered width the ceiling shows less
        than the file says; a step the clamp swallows changed nothing on screen
        and must not write the clamped number over the remembered one.
        """
        before = self.width
        width = self.clamp(before + delta)
        if width == before:
            return width  # swallowed by the bounds: the ask stands, and nothing is written
        self.resize_to(width)
        self._remember(width)
        return width

    def reset(self) -> None:
        """Back to the stylesheet's width, and no remembered one."""
        self._width = None
        self.target.styles.width = None
        self._remember(None)

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
        self._follow(event.screen_x)

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if not self._dragging or event.button != 1:
            return  # a right click mid-drag is a paste gesture, not the end of this one
        event.stop()
        self._follow(event.screen_x)
        self._end_drag()

    def _follow(self, screen_x: int) -> None:
        """Put the partition at the pointer's column; a change of width is what makes this a drag.

        The pointer's column IS the partition: the neighbour starts at its
        region's left edge, so the width is the distance from there.
        """
        before = self.width
        if self.resize_to(screen_x - self.target.region.x) != before:
            self._moved = True
        self._drag_ceiling = self.bounds()[1]

    def on_mouse_release(self, event: events.MouseRelease) -> None:
        """The app took the capture away (a screen was pushed mid-drag): the drag is over."""
        self._end_drag()

    def on_hide(self, event: events.Hide) -> None:
        self._end_drag()

    def _end_drag(self) -> None:
        """Let the mouse go — once, whatever ended the drag — and settle the width if it moved.

        A press that never moved (a click; a press cut short by a pushed screen)
        asked for nothing, and settling it would write a preference the user
        never expressed — on a terminal narrower than the remembered width, the
        ceiling over that width.
        """
        if not self._dragging:
            return
        self._dragging = False
        self.remove_class("-dragging")
        if self.app.mouse_captured is self:
            self.release_mouse()
        if self._moved:
            self._last_click_still = False  # a drag breaks the chain, Click or no Click
            self._remember(self.width)

    def on_click(self, event: events.Click) -> None:
        """A double click resets — two LEFT clicks that were clicks, at one cell, close together.

        Textual's chain counter says "same cell, within half a second" and
        counts every button: a drag's release is a click to it (the handle
        follows the pointer, so the release lands where the press did), and so
        is a right or middle click, so a tap after a drag — or a left click
        after a middle-click paste — arrived as ``chain == 2`` and threw the
        width away. So a click that moved the width does not count, and another
        button BREAKS the chain: with that, ``chain >= 2`` and "the previous
        click was a still left one" together mean exactly a double left click.
        """
        if event.button != 1:
            self._last_click_still = False  # a paste gesture: it neither resets nor counts
            return
        still = not self._moved
        double = event.chain >= 2 and still and self._last_click_still
        self._last_click_still = still
        if double:
            event.stop()
            self.reset()

    # --- the memory ---------------------------------------------------------------------

    def on_mount(self) -> None:
        if self._state_key is None:
            return
        saved = read_state().get(self._state_key)
        remembered: int | None = None
        if isinstance(saved, int) and not isinstance(saved, bool):
            # Capped ONCE, here: this is the number every later path may hand
            # the style setter — the restore, and the ceiling rule putting the
            # ask back — and ``float(10**400)`` overflows there.
            remembered = max(0, min(saved, WIDEST_ASK))
        self._autosave = Autosave(
            self, self._state_key, what="the navigator's width", initial=remembered
        )
        if remembered is not None:
            # After the first layout, not during mount: by then the container
            # has written the ceiling, so the layout bounds what is shown from
            # the first frame the ask reaches. Applied at mount, a saved 500
            # gave the content pane one frame at zero columns — a size the
            # agent's pane forwards to tmux.
            self.call_after_refresh(self._restore, remembered)

    def _restore(self, ask: int) -> None:
        """Apply the remembered width as the ASK, not as what is shown.

        A drag keeps its ask while ``max-width`` clamps the display, and gets it
        back when the terminal has room again; a width restored on a narrow
        terminal must behave the same, or the same preference has two
        behaviours depending on when the terminal was narrow — and the next
        step writes the clamped number over it. So no clamping here beyond the
        cap ``on_mount`` applied; the layout clamps the display, and
        :attr:`width` clamps the read.
        """
        self._width = ask
        self.target.styles.width = ask

    def on_unmount(self) -> None:
        if (
            self._dragging
            and self._moved
            and self._autosave is not None
            and self._width is not None
        ):
            # Quit with the button still held: the drag reached a width the user
            # asked for, settled as its release would have settled it — the
            # ceiling rule included, or a narrow terminal's ceiling replaced a
            # wider width on file (review of the #167 fold, F4). Judged against
            # the ceiling the drag last met, not the neighbour's styles: Textual
            # prunes the siblings together, so the neighbour may be gone already.
            remembered = self._remembered_width()
            if remembered is None or not self._ceiling_under(
                self._width, remembered, self._drag_ceiling
            ):
                self._autosave.remember(self._width)
        self._dragging = False
        if self._autosave is not None:
            # Start the drain (a stopped timer would never fire it); the app joins
            # every saver against one deadline at its own unmount.
            self._autosave.wake()

    def _remembered_width(self) -> int | None:
        """The width this process last asked the file to hold (or read from it at start).

        Not a promise about the file: a refused save leaves it a value the file
        does not hold yet — retried at the next change and at quit, and reported
        then if it still does not land.
        """
        latest = None if self._autosave is None else self._autosave.latest
        return latest if isinstance(latest, int) else None

    def _remember(self, width: int | None) -> None:
        """Hand the width to the saver — one write per burst, off the event loop — or keep the
        remembered one when the ceiling rule says so. Whether the file already says so is
        ``update_state``'s to decide, under its lock."""
        if self._autosave is None:
            return
        remembered = self._remembered_width()
        if remembered is not None and self._ceiling_under(width, remembered):
            # "As wide as this screen allows" — so the ask stays the remembered
            # width too, and comes back on screen when the room does, exactly as
            # the file will give it back at the next launch.
            self._width = remembered
            self.target.styles.width = remembered
            return
        self._autosave.remember(width)

    def _ceiling_under(
        self, width: int | None, remembered: int, ceiling: int | None = None
    ) -> bool:
        """Whether ``width`` is this terminal's ceiling with a wider width on file (or on its way).

        The ceiling bounds what is SHOWN, not what is remembered: a laptop
        clamps a monitor's 90 to 39, and a gesture that lands on 39 there is
        "as wide as this screen allows", not a new number to carry back.
        ``ceiling`` stands in for the bounds' when the neighbour cannot be
        asked (a quit mid-drag).
        """
        upper = self.bounds()[1] if ceiling is None else ceiling
        return width is not None and width >= upper and remembered > width
