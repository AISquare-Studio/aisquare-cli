"""An agent's menu in the sidebar (R17): ⋯ on its row, a right-click, or ``.`` on it.

The owner (2026-11-16): "in agents on left pane, have 3 dots for agents as well for the same
of pause stop restart rename … if user has selected dont show again then it just does the
interaction". Its entries are the agent view's header buttons, and the rename the row
already offers in place:

- *Pause* while the agent has a process (not on the captain's row), *Resume* in its place on
  a paused row;
- *Stop* where the header's Stop shows (``STOP_STATES``);
- *Restart* on every row but a paused one (restarting a paused row IS its resume);
- *Rename*, always listed, and greyed with why under it when the row offers none (the manager,
  the captain, a paused or an ended agent): ``sidebar.rename_refusal_words``, the service's
  rule in words that name no command.

The menu answers what was chosen and does nothing itself, as a project card's menu does: the
shell (``FleetApp.on_open_agent_menu``) runs the header's own press when the agent's view is
open (``AgentView.act``), and otherwise the same box (``confirm_action``, *Don't ask again*
included) and the same service call, pinned to the row. Every name in it is data.
"""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.geometry import Region
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from aisquare.cli.ui.dont_ask import PAUSE, RESTART, STOP
from aisquare.cli.ui.sidebar import STOP_STATES
from aisquare.models import FleetAgentStatus

RESUME = "resume"
RENAME = "rename"

MENU_WIDTH = 26
"""The menu's columns, its border included, as a project card's menu: it fits the narrowest
sidebar beside it."""


def agent_menu_options(
    status: FleetAgentStatus, *, pausable: bool, rename_refusal: str | None
) -> list[Option]:
    """The entries for ``status``'s row, in the header's order, Rename last."""
    paused = status.state == "paused"
    options: list[Option] = []
    if paused:
        options.append(Option("▶ Resume", id=RESUME))
    elif pausable:
        options.append(Option("⏸ Pause", id=PAUSE))
    if status.state in STOP_STATES:
        options.append(Option("■ Stop", id=STOP))
    if not paused:
        options.append(Option("↺ Restart", id=RESTART))
    options.append(Option("✎ Rename", id=RENAME, disabled=rename_refusal is not None))
    return options


class AgentMenu(ModalScreen[str | None]):
    """Pause or Resume, Stop, Restart and Rename — under the agent's row.

    Answers the entry's id (``pause``, ``resume``, ``stop``, ``restart``, ``rename``); Esc, or
    a click anywhere off the menu, answers ``None``."""

    CSS = """
    AgentMenu { align: left top; background: $background 20%; }
    AgentMenu #agent-menu { height: auto; border: round $accent; background: $surface; }
    AgentMenu #agent-menu-title {
        height: 1; padding: 0 1; text-style: bold; text-wrap: nowrap; text-overflow: ellipsis;
    }
    AgentMenu OptionList { height: auto; border: none; padding: 0; }
    AgentMenu #agent-menu-note { height: auto; padding: 0 1; color: $text-muted; }
    """
    BINDINGS: ClassVar = [("escape", "close_menu", "close")]

    def __init__(
        self,
        status: FleetAgentStatus,
        anchor: Region,
        *,
        pausable: bool,
        rename_refusal: str | None = None,
    ) -> None:
        super().__init__()
        self.status = status
        self.near = anchor
        """Where the row is on screen: the menu opens under it."""
        self.pausable = pausable
        self.rename_refusal = rename_refusal

    def compose(self) -> ComposeResult:
        with Vertical(id="agent-menu"):
            yield Static(Text(self.status.agent.label), id="agent-menu-title")
            yield OptionList(
                *agent_menu_options(
                    self.status, pausable=self.pausable, rename_refusal=self.rename_refusal
                ),
                id="agent-menu-options",
            )
            note = Static(id="agent-menu-note")
            if self.rename_refusal is not None:
                note.update(Text(f"Rename: {self.rename_refusal}"))
            note.display = self.rename_refusal is not None
            yield note

    def on_mount(self) -> None:
        box = self.query_one("#agent-menu", Vertical)
        options = self.query_one(OptionList)
        note_lines = (
            len(Text(f"Rename: {self.rename_refusal}").wrap(self.app.console, MENU_WIDTH - 4))
            if self.rename_refusal is not None
            else 0
        )
        height = options.option_count + 3 + note_lines
        width, rows = self.app.size
        x = max(0, min(self.near.x + 2, width - MENU_WIDTH))
        y = max(0, min(self.near.y + 1, rows - height))
        box.styles.width = MENU_WIDTH
        box.styles.offset = (x, y)
        options.highlighted = 0
        options.focus()

    @on(OptionList.OptionSelected, "#agent-menu-options")
    def _chosen(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(event.option.id)

    def on_click(self, event: events.Click) -> None:
        # A click that reaches the screen itself fell outside the menu: close, as Esc does.
        if event.widget is self:
            self.dismiss(None)

    def action_close_menu(self) -> None:
        self.dismiss(None)
