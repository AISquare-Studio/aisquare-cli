"""``TerminalPane`` — a tmux pane rendered inside the fleet UI, keys forwarded.

Phase 0/4 (docs/plans/fleet-tui.md §6) implements the render loop
(``capture-pane`` → ``Text.from_ansi`` → Strips, diffed), key forwarding through
``core.keys``, paste via ``paste-buffer -p``, scrollback, resize sync and the
escape hatch. This skeleton fixes the contract the views compose against.
"""

from __future__ import annotations

from rich.text import Text
from textual.message import Message
from textual.widgets import Static

from aisquare.core.tmux import TmuxServer


class EscapeToSidebar(Message):
    """The user pressed the escape hatch: focus goes back to the sidebar."""


class TerminalPane(Static):
    """One tmux pane, live. ``attach(pane_id)`` switches what it shows."""

    DEFAULT_CSS = """
    TerminalPane { height: 1fr; width: 1fr; }
    """

    can_focus = True

    def __init__(
        self,
        pane_id: str | None = None,
        *,
        server: TmuxServer | None = None,
        escape_key: str = "f12",
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.pane_id = pane_id
        self.server = server
        self.escape_key = escape_key

    def attach(self, pane_id: str | None) -> None:
        """Show ``pane_id`` (``None`` clears the pane)."""
        self.pane_id = pane_id
        self.update(self._placeholder())

    def on_mount(self) -> None:
        self.update(self._placeholder())

    def _placeholder(self) -> Text:
        if self.pane_id is None:
            return Text("(no agent selected)", style="dim")
        return Text(f"(terminal pane {self.pane_id} — rendering lands in Phase 4)", style="dim")
