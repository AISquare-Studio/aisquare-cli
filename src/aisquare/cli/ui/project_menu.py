"""A project card's menu — ⋯ on the card, ``.`` or a right-click — and what its Remove asks.

The menu answers what was chosen and does nothing itself: the shell (``FleetApp``)
renames, pins, opens the Settings tab or removes, as it does for every gesture the
sidebar posts. Every name in it is a ``rich.text.Text``: a nickname is free text,
and ``[wip]`` is not markup.
"""

from __future__ import annotations

import shlex
from typing import ClassVar

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.geometry import Region
from textual.screen import ModalScreen
from textual.widgets import Button, OptionList, Static
from textual.widgets.option_list import Option

from aisquare.cli.ui.sidebar import SETTINGS_ICON
from aisquare.models import ProjectInfo
from aisquare.services.project import display_name

RENAME = "rename"
PIN = "pin"
SETTINGS = "settings"
REMOVE = "remove"

MENU_WIDTH = 26
"""The menu's columns, its border included: it fits the narrowest sidebar beside it. The one
source of it: the box is sized from it on mount, as it is placed."""


class ProjectMenu(ModalScreen[str | None]):
    """Rename, Pin / Unpin, Open settings, Remove from fleet… — under the card it is about.

    Answers :data:`RENAME`, :data:`PIN`, :data:`SETTINGS` or :data:`REMOVE`; Esc, or a
    click anywhere off the menu, answers ``None``.
    """

    CSS = """
    ProjectMenu { align: left top; background: $background 20%; }
    ProjectMenu #project-menu { height: auto; border: round $accent; background: $surface; }
    ProjectMenu #project-menu-title {
        height: 1; padding: 0 1; text-style: bold; text-wrap: nowrap; text-overflow: ellipsis;
    }
    ProjectMenu OptionList { height: auto; border: none; padding: 0; }
    """
    BINDINGS: ClassVar = [("escape", "close_menu", "close")]

    def __init__(self, project: ProjectInfo, anchor: Region) -> None:
        super().__init__()
        self.project = project
        self.near = anchor
        """Where the card is on screen: the menu opens under its header line."""

    def compose(self) -> ComposeResult:
        pin = "Unpin" if self.project.pinned_at is not None else "Pin"
        with Vertical(id="project-menu"):
            yield Static(Text(display_name(self.project)), id="project-menu-title")
            yield OptionList(
                Option("✎ Rename", id=RENAME),
                Option(f"📌 {pin}", id=PIN),
                Option(f"{SETTINGS_ICON} Open settings", id=SETTINGS),
                Option("✗ Remove from fleet…", id=REMOVE),
                id="project-menu-options",
            )

    def on_mount(self) -> None:
        box = self.query_one("#project-menu", Vertical)
        options = self.query_one(OptionList)
        height = options.option_count + 3  # the title and the border
        width, rows = self.app.size
        x = max(0, min(self.near.x + 2, width - MENU_WIDTH))
        y = max(0, min(self.near.y + 1, rows - height))
        box.styles.width = MENU_WIDTH
        box.styles.offset = (x, y)
        options.highlighted = 0
        options.focus()

    @on(OptionList.OptionSelected, "#project-menu-options")
    def _chosen(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(event.option.id)

    def on_click(self, event: events.Click) -> None:
        # A click that reaches the screen itself fell outside the menu: close, as Esc does.
        if event.widget is self:
            self.dismiss(None)

    def action_close_menu(self) -> None:
        self.dismiss(None)


class ConfirmRemove(ModalScreen[bool]):
    """ "Remove <name> from the fleet?" — Remove answers ``True``; Cancel or Esc ``False``.

    Cancel has the focus, so an Enter that came too fast removes nothing.
    """

    CSS = """
    ConfirmRemove { align: center middle; }
    ConfirmRemove #confirm-remove {
        width: 64; height: auto; border: heavy $error; background: $surface; padding: 1 2;
    }
    ConfirmRemove #confirm-remove-buttons { height: auto; margin-top: 1; align: right middle; }
    ConfirmRemove Button { margin-left: 2; }
    """
    BINDINGS: ClassVar = [("escape", "cancel_remove", "cancel")]

    def __init__(self, project: ProjectInfo) -> None:
        super().__init__()
        self.project = project

    def compose(self) -> ComposeResult:
        text = Text()
        text.append(f"Remove {display_name(self.project)} from the fleet?\n\n", style="bold")
        text.append("Its folder and history are kept; ")
        # Quoted as the toast after a removal quotes it: a root with a space is one argument.
        text.append(f"aisquare project onboard {shlex.quote(str(self.project.root))}", style="bold")
        text.append(" brings it back.")
        with Vertical(id="confirm-remove"):
            yield Static(text, id="confirm-remove-text")
            with Horizontal(id="confirm-remove-buttons"):
                yield Button("Remove", variant="error", id="confirm-remove-yes")
                yield Button("Cancel", id="confirm-remove-no")

    def on_mount(self) -> None:
        self.query_one("#confirm-remove-no", Button).focus()

    @on(Button.Pressed, "#confirm-remove-yes")
    def _yes(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(True)

    @on(Button.Pressed, "#confirm-remove-no")
    def _no(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(False)

    def action_cancel_remove(self) -> None:
        self.dismiss(False)
