"""The drag handle between the navigator and the content (#137), and the width it keeps.

docs/plans/fleet-tui.md §2 fixed the navigator at 30 columns. Project names,
worktree labels and agent rows truncate there, and an agent's pane wants every
column it can get on a laptop, so the partition is now a one-column
:class:`Divider` the user drags — the way tmux, VS Code and every split-pane
TUI allow — with a keyboard fallback for terminals without mouse reporting
(``<`` / ``>`` step the width, ``=`` resets it, while the sidebar has focus)
and a double click on the handle that resets it too.

The width is remembered in ``state.json`` under :data:`STATE_KEY`, beside the
board's theme (``cli.watch``'s file and format), and restored at the next
launch. Bounds are the same everywhere (:func:`clamp_width`): never narrower
than the sidebar's own minimum, never so wide that the content pane drops
under :data:`MIN_CONTENT` columns — a ``TerminalPane`` narrower than that is
not a terminal anyone can work in, and the pane forwards every width change to
tmux (``resize-window``, debounced), so the agent reflows to whatever is left.

Textual 8.2.8 ships no splitter; this widget takes its LEFT neighbour by
selector rather than hard-coding the sidebar, so a later horizontal split
inside a view can reuse it.
"""

from __future__ import annotations

import json

from textual import events
from textual.message import Message
from textual.widget import Widget

from aisquare.core import paths

DEFAULT_WIDTH = 30
"""The navigator's width when nothing was ever dragged — plan §2's number."""
MIN_WIDTH = 24
"""Narrower and the project cards lose their chips (the sidebar's own ``min-width``)."""
MIN_CONTENT = 40
"""The columns the content pane keeps whatever the drag: a terminal narrower than
this wraps every prompt line and Claude Code's own layout gives up."""
STEP = 4
"""Columns one keyboard step moves the partition."""
STATE_KEY = "sidebar_width"
"""The ``state.json`` key, beside ``board_theme``."""


def clamp_width(wanted: int, total: int) -> int:
    """The navigator width nearest ``wanted`` that the bounds allow in ``total`` columns.

    The divider itself takes one column. When the terminal is too narrow for
    both minimums the navigator's wins: a navigator you can read beats a pane
    you cannot, and the pane says so with its own placeholder.
    """
    upper = max(MIN_WIDTH, total - 1 - MIN_CONTENT)
    return max(MIN_WIDTH, min(int(wanted), upper))


def load_sidebar_width() -> int | None:
    """The remembered width, or ``None`` when there is none (or the file is unreadable)."""
    path = paths.state_path()
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get(STATE_KEY)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def save_sidebar_width(width: int) -> None:
    """Remember ``width`` — every change is the save, as with the theme.

    Tolerates a corrupt ``state.json`` and writes atomically (tmp + rename), so
    a crash mid-write can never leave the shared state file truncated. A
    failure to write costs the memory of the width, never the UI.
    """
    try:
        paths.ensure_home()
        path = paths.state_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data[STATE_KEY] = int(width)
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        temp.replace(path)
    except OSError:
        return


class SidebarResized(Message):
    """The partition moved and settled — a drag ended, a key stepped it, a reset."""

    def __init__(self, width: int) -> None:
        super().__init__()
        self.width = width


class ResizeSidebar(Message):
    """A request from the keyboard: step the partition by ``delta``, or reset with ``None``."""

    def __init__(self, delta: int | None) -> None:
        super().__init__()
        self.delta = delta


class Divider(Widget):
    """One column between two neighbours; drag it to resize the one on its left.

    On mouse-down the mouse is captured, so the drag and the release arrive
    wherever the pointer goes; every move sets the neighbour's width from the
    pointer's screen column; the release posts :class:`SidebarResized`, which
    the app persists. Hover and drag light the column with ``$accent``, the
    colour the focused sidebar's border already uses.
    """

    DEFAULT_CSS = """
    Divider { width: 1; height: 1fr; background: $primary 40%; }
    Divider:hover { background: $accent; }
    Divider.-dragging { background: $accent; }
    """

    def __init__(self, target: str = "#sidebar", *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._target = target
        """The selector of the neighbour whose width this handle sets."""
        self._dragging = False

    @property
    def target(self) -> Widget:
        return self.screen.query_one(self._target)

    @property
    def width(self) -> int:
        """The neighbour's current width in columns — the OUTER width, border included.

        ``Widget.size`` is the content area, one column short of a bordered
        sidebar's ``width`` style (measured: ``styles.width=30`` → ``size=29``,
        ``outer_size=30``); stepping from it would shrink the partition by a
        column per press.
        """
        return self.target.outer_size.width

    def resize_to(self, wanted: int) -> int:
        """Set the neighbour's width to ``wanted``, within the bounds; returns what was set."""
        width = clamp_width(wanted, self.screen.size.width)
        self.target.styles.width = width
        return width

    def step(self, delta: int | None) -> int:
        """Move the partition by ``delta`` columns (``None`` resets) and announce it."""
        width = self.resize_to(DEFAULT_WIDTH if delta is None else self.width + delta)
        self.post_message(SidebarResized(width))
        return width

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button != 1:
            return
        event.stop()
        self._dragging = True
        self.add_class("-dragging")
        self.capture_mouse()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if not self._dragging:
            return
        event.stop()
        # The pointer's column IS the partition: the neighbour starts at its
        # region's left edge, so the width is the distance from there.
        self.resize_to(event.screen_x - self.target.region.x)

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if not self._dragging:
            return
        event.stop()
        self._dragging = False
        self.remove_class("-dragging")
        self.release_mouse()
        width = self.resize_to(event.screen_x - self.target.region.x)
        self.post_message(SidebarResized(width))

    async def _on_click(self, event: events.Click) -> None:
        """A double click puts the partition back where the plan drew it."""
        if event.chain == 2 and event.button == 1:
            event.stop()
            self.step(None)
