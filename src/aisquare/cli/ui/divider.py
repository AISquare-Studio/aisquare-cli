"""The drag handle between two neighbours, side by side or stacked (#137).

docs/plans/fleet-tui.md §2 fixed the navigator at 30 columns. Project names,
worktree labels and agent rows truncate there, and an agent's pane wants every
column it can get on a laptop, so the partition is a one-column
:class:`Divider` the user drags — the way tmux, VS Code and every split-pane
TUI allow. It answers the keyboard too (:meth:`Divider.step` and
:meth:`Divider.reset`, called by the container that owns it) and a double
click (reset).

The widget knows ONE neighbour by selector — its target, the sibling it sizes
— and nothing else about the layout it sits in. The selector is matched among
the divider's own siblings, never across the screen: the fleet UI keeps a
``ProjectView`` per project mounted at once, so a board's ``#board-side``
exists once per project, and a screen-wide query sized the FIRST project's
from every project's handle. A ``vertical`` divider is a column between
left and right neighbours and sets its target's width; a ``horizontal`` one
is a row between neighbours above and below and sets its target's height. The
target sits ``before`` the divider (left of it, or above it) or ``after`` it
(right, or below); dragging toward the target shrinks it either way.

Its bounds are the target's own ``min-width`` and ``max-width`` (``min-height``
and ``max-height`` for a row): the stylesheet says the floor once
(``Sidebar { min-width: 24 }``), and the container that lays out both sides
keeps the max current from the cells the other side must keep (``app.Panes``,
``board.BoardPanel``), so Textual clamps the target on EVERY layout pass — a
terminal that shrinks re-clamps with no handler here, and the handle can never
be pushed off screen. What a gesture asked for is kept on the widget and every
step starts from it, never from the laid-out size, which is stale until the
next layout pass: a key held at autorepeat used to lose four steps in five.

Given a ``state_key`` the widget remembers every settled size under it in
``state.json`` (:mod:`aisquare.core.state_file` — the one reader and writer of
the file the theme and the pinned project share): restored at mount, saved once
a gesture settles, debounced so a held key is one write, skipped when the file
already says so. A reset FORGETS the key rather than saving a number, so the
next launch shows whatever the stylesheet says by then. Below, "width" is the
size along the divider's axis — a height for a ``horizontal`` divider.

Textual 8.2.8 ships no splitter. Its ``ScrollBar`` is the reference for the
mouse protocol here: ``ALLOW_SELECT = False`` (a drag handle is not text, and
the screen opens a selection BEFORE it forwards the press, so ``event.stop()``
is too late); the capture let go on ``MouseRelease`` (posted when a screen is
pushed mid-drag) and on ``Hide``; and a release that never arrives — the button
let go outside the terminal — read off the first move with no button held.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from rich.text import Text
from textual import events
from textual.app import RenderResult
from textual.css.query import NoMatches
from textual.css.scalar import Scalar
from textual.widget import Widget

from aisquare.cli.ui.autosave import Autosave
from aisquare.core.state_file import read_state

LINE = "│"
"""Textual's ``solid`` border glyph, so the partition looks as the border it replaced did."""

LINE_ACROSS = "─"
"""The same border's glyph for a row: a ``horizontal`` divider."""

Orientation = Literal["vertical", "horizontal"]
"""Which way the LINE runs: ``vertical`` sits between left and right neighbours and sets a
width; ``horizontal`` sits between neighbours above and below and sets a height."""

Side = Literal["before", "after"]
"""Where the target sits: ``before`` the divider (left, or above) or ``after`` it (right, or
below)."""

Dimension = Literal["width", "height"]


WIDEST_ASK = 10_000
"""The one bound a REMEMBERED width gets before the layout's: wider than any
terminal, small enough for the style setter (``float(10**400)`` overflowed) and
for a layout with no ceiling. Everything under it is the layout's to clamp."""


def cells(scalar: Scalar | None) -> int | None:
    """A style's value in columns; ``None`` when unset or not in cells (%, fr, auto)."""
    return None if scalar is None else scalar.cells


def floor_of(widget: Widget, dimension: Dimension = "width") -> int:
    """``widget``'s floor in cells: its ``min-width`` (``min-height`` for ``height``; ``0``
    means 0); unset, or a unit that is not cells, means 1. One rule for the divider's bounds
    and the container's ceiling (``app.Panes``, ``board.BoardPanel``), so the two cannot
    disagree about where the floor is."""
    floor = cells(getattr(widget.styles, f"min_{dimension}"))
    return floor if floor is not None else 1


class Divider(Widget):
    """One cell between two neighbours; drag it to resize the one it names, its target.

    Mouse-down captures the mouse, so the drag and the release arrive wherever
    the pointer goes; every move with the button held sets the target's width
    from the pointer's screen column (row, for a ``horizontal`` divider);
    whatever ends a drag that moved settles the width (the save, when there is
    a ``state_key``). Hover tints the line, a drag fills it, and
    ``-neighbour-focused`` — set by the container, which sees
    ``DescendantFocus`` — lights it ``$accent`` while the neighbour has focus,
    the signal the neighbour's own border used to give.
    """

    ALLOW_SELECT: ClassVar[bool] = False
    """A drag handle is not text (Textual's own ``ScrollBar`` says the same)."""

    DEFAULT_CSS = """
    Divider { width: 1; height: 1fr; color: $primary; pointer: ew-resize; }
    Divider.-horizontal { width: 1fr; height: 1; pointer: ns-resize; }
    Divider.-neighbour-focused { color: $accent; }
    Divider:hover { background: $accent 40%; }
    Divider.-dragging { background: $accent; color: $accent; }
    """

    def __init__(
        self,
        target: str,
        *,
        orientation: Orientation = "vertical",
        side: Side = "before",
        state_key: str | None = None,
        what: str = "a pane's size",
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self._target = target
        """The selector of the sibling whose width this handle sets."""
        self._vertical = orientation == "vertical"
        """A column that sets a width; else a row that sets a height."""
        self._dimension: Dimension = "width" if self._vertical else "height"
        self._after = side == "after"
        """The target is right of (or below) the handle, not left of (or above) it."""
        self._state_key = state_key
        """The ``state.json`` key the width is remembered under; ``None`` remembers nothing."""
        self._what = what
        """How a refused save names the preference: "the navigator's width"."""
        self._ask: int | None = None
        """The ASK: what the last gesture asked for, or what the file remembered — not
        what is shown. The layout's ceiling bounds the display; a wider terminal
        later gets the ask back. ``None`` until there is one, and after a reset."""
        self._dragging = False
        self._moved = False
        """Whether the gesture now running changed the width — a drag, not a click.

        Set by a change of width, not by a motion event: a one-cell hand drift
        along the handle between press and release is routine and moves
        nothing.
        """
        self._last_click_still = False
        """Whether the previous click was a LEFT click that was a click (the width did not
        change). Another button, or a drag, breaks the chain."""
        self._drag_ceiling: int | None = None
        """The ceiling the drag now running last met, for a quit before its release
        (:meth:`on_unmount`): the target whose styles give it may be pruned by then."""
        self._autosave: Autosave | None = None
        """The debounced, off-loop save under ``state_key``; ``None`` when there is no key. Its
        ``latest`` — the width this process last asked the file to hold, or read from it — is
        what a gesture is compared against; every number it hands out was capped at mount."""
        if not self._vertical:
            self.add_class("-horizontal")

    # --- the target and its bounds ---------------------------------------------------

    @property
    def target(self) -> Widget:
        """The sibling whose width this handle sets — looked up beside the handle, never
        across the screen, where another view's widget may carry the same id."""
        parent = self.parent
        if parent is None:
            raise NoMatches(f"{self!r} has no parent to find {self._target!r} in")
        return parent.query_children(self._target).first()

    @property
    def asked(self) -> bool:
        """Whether a gesture (or the file) asked for a width; ``False`` shows the stylesheet's."""
        return self._ask is not None

    def _style(self, target: Widget, prefix: str = "") -> Scalar | None:
        value: Scalar | None = getattr(target.styles, f"{prefix}{self._dimension}")
        return value

    def _set_style(self, target: Widget, value: int | None) -> None:
        setattr(target.styles, self._dimension, value)

    def bounds(self) -> tuple[int, int]:
        """The target's floor and ceiling in cells.

        The floor is its ``min-width`` (:func:`floor_of`). The ceiling is its
        ``max-width`` — the container's (``app.Panes``) — and, until one is
        written, the room the handle's container has less this cell (the
        terminal's, before that container has been laid out): a target is never
        wider than what holds it, whatever a file says. The floor wins when the
        two cross.
        """
        target = self.target
        lower = floor_of(target, self._dimension)
        upper = cells(self._style(target, "max_"))
        if upper is None:
            upper = self._room() - 1
        return lower, max(lower, upper)

    def _room(self) -> int:
        """The cells along this handle's axis that its container has, or the terminal's."""
        parent = self.parent
        room = getattr(parent.size, self._dimension) if isinstance(parent, Widget) else 0
        return room if room > 0 else getattr(self.app.size, self._dimension)

    def clamp(self, wanted: int) -> int:
        """The width nearest ``wanted`` within the bounds."""
        lower, upper = self.bounds()
        return max(lower, min(int(wanted), upper))

    @property
    def extent(self) -> int:
        """The target's intended width, within the bounds in force now.

        The last gesture's ask when there was one, else what the stylesheet
        declares (else what the layout gave). Never the laid-out size after a
        gesture: that is stale until the next layout pass, and stepping from it
        lost every key of a burst but the first.
        """
        if self._ask is not None:
            return self.clamp(self._ask)
        target = self.target
        declared = cells(getattr(target.styles.base, self._dimension))
        return self.clamp(
            declared if declared is not None else getattr(target.outer_size, self._dimension)
        )

    def resize_to(self, wanted: int) -> int:
        """Set the target's width to ``wanted`` within the bounds; returns what was set."""
        width = self.clamp(wanted)
        self._ask = width
        self._set_style(self.target, width)
        return width

    def step(self, delta: int) -> int:
        """Grow the target by ``delta`` cells (shrink, when negative) and settle there —
        unless the bounds ate it.

        On a terminal narrower than the remembered width the ceiling shows less
        than the file says; a step the clamp swallows changed nothing on screen
        and must not write the clamped number over the remembered one.
        """
        before = self.extent
        width = self.clamp(before + delta)
        if width == before:
            return width  # swallowed by the bounds: the ask stands, and nothing is written
        self.resize_to(width)
        self._remember(width)
        return width

    def reset(self) -> None:
        """Back to the stylesheet's width, and no remembered one."""
        self._ask = None
        self._set_style(self.target, None)
        self._remember(None)

    # --- painting -----------------------------------------------------------------------

    def render(self) -> RenderResult:
        if self._vertical:
            return Text("\n".join([LINE] * max(1, self.size.height)))
        return Text(LINE_ACROSS * max(1, self.size.width))

    # --- the mouse ----------------------------------------------------------------------

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button != 1:
            return
        event.stop()
        if self._dragging:
            # Button 1 pressed while a press of it still holds the drag: that
            # release was lost, on a terminal that reports no motion without a
            # button, so no buttonless move ended the drag (``on_mouse_move``
            # does where one is reported; ``DragHandle.on_mouse_down`` is the
            # sidebar's side of the same case). This press reached the divider
            # only because it still held the mouse. The old drag settles where
            # it got to, the mouse is let go, and this press starts nothing:
            # its release is a click on whatever is under the pointer.
            # Re-armed here, the divider kept the mouse and the release set the
            # width: a click on a title opened nothing and saved the floor as
            # the navigator's width (final review of #203, F3).
            self._end_drag()
            return
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
            # the target would follow the bare pointer.
            self._end_drag()
            return
        self._follow(event)

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if not self._dragging or event.button != 1:
            return  # a right click mid-drag is a paste gesture, not the end of this one
        event.stop()
        self._follow(event)
        self._end_drag()

    def _follow(self, event: events.MouseEvent) -> None:
        """Put the partition at the pointer's cell; a change of width is what makes this a drag.

        The pointer's cell IS the partition. A target before the handle starts at
        its region's left (top) edge, so its width is the distance from there to
        the pointer; one after it ends at its region's right (bottom) edge — the
        edge that stays put while it resizes — so its width is the distance from
        the pointer's next cell to there.
        """
        pointer = event.screen_x if self._vertical else event.screen_y
        region = self.target.region
        if self._after:
            wanted = (region.right if self._vertical else region.bottom) - pointer - 1
        else:
            wanted = pointer - (region.x if self._vertical else region.y)
        before = self.extent
        if self.resize_to(wanted) != before:
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
            self._remember(self.extent)

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
        self._autosave = Autosave(self, self._state_key, what=self._what, initial=remembered)
        if remembered is not None:
            # After the first layout, not during mount: by then the container
            # has written the ceiling, so the layout bounds what is shown from
            # the first frame the ask reaches. Applied at mount, a saved 500
            # gave the content pane one frame at zero columns — a size the
            # agent's pane forwards to tmux.
            self.call_after_refresh(self._restore, remembered)

    def _restore(self, ask: int) -> None:
        """Apply the remembered width as the ASK, not as what is shown.

        A drag keeps its ask while the max clamps the display, and gets it
        back when the terminal has room again; a width restored on a narrow
        terminal must behave the same, or the same preference has two
        behaviours depending on when the terminal was narrow — and the next
        step writes the clamped number over it. So no clamping here beyond the
        cap ``on_mount`` applied; the layout clamps the display, and
        :attr:`extent` clamps the read.
        """
        self._ask = ask
        self._set_style(self.target, ask)

    def on_unmount(self) -> None:
        if self._dragging and self._moved and self._autosave is not None and self._ask is not None:
            # Quit with the button still held: the drag reached a width the user
            # asked for, settled as its release would have settled it — the
            # ceiling rule included, or a narrow terminal's ceiling replaced a
            # wider width on file (review of the #167 fold, F4). Judged against
            # the ceiling the drag last met, not the target's styles: Textual
            # prunes the siblings together, so the target may be gone already.
            remembered = self._remembered_width()
            if remembered is None or not self._ceiling_under(
                self._ask, remembered, self._drag_ceiling
            ):
                self._autosave.remember(self._ask)
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
            self._ask = remembered
            self._set_style(self.target, remembered)
            return
        self._autosave.remember(width)

    def _ceiling_under(
        self, width: int | None, remembered: int, ceiling: int | None = None
    ) -> bool:
        """Whether ``width`` is this terminal's ceiling with a wider width on file (or on its way).

        The ceiling bounds what is SHOWN, not what is remembered: a laptop
        clamps a monitor's 90 to 39, and a gesture that lands on 39 there is
        "as wide as this screen allows", not a new number to carry back.
        ``ceiling`` stands in for the bounds' when the target cannot be
        asked (a quit mid-drag).
        """
        upper = self.bounds()[1] if ceiling is None else ceiling
        return width is not None and width >= upper and remembered > width
