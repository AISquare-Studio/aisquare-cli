"""A project card's menu — ⋯ on the card, ``.`` or a right-click — and its Remove, which asks
in place.

The menu answers what was chosen and does nothing itself: the shell (``FleetApp``)
renames, pins, opens the Settings tab, pauses or resumes the project's agents
(``views.fleet_bulk``), starts the project fresh (``ui.fresh``) or removes, as it does for
every gesture the sidebar posts. Every name in it is a ``rich.text.Text``: a nickname is
free text, and ``[wip]`` is not markup.

*Remove from fleet* is its last row, a :class:`~aisquare.cli.ui.confirm_button.ConfirmButton`:
its first press arms it ("Remove? click again") and says under it what leaves the fleet and
what is kept; the second press on it answers :data:`REMOVE` (user, 2026-10-30: a double click
in place, not a dialog's button). The shell removes on that answer at once.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.geometry import Region
from textual.screen import ModalScreen
from textual.widgets import Button, OptionList, Static
from textual.widgets.option_list import Option

from aisquare.cli.ui.confirm_button import ConfirmButton
from aisquare.cli.ui.sidebar import SETTINGS_ICON
from aisquare.models import ProjectInfo
from aisquare.services.project import display_name

RENAME = "rename"
PIN = "pin"
SETTINGS = "settings"
PAUSE = "pause"
RESUME = "resume"
FRESH = "fresh"
REMOVE = "remove"

MENU_WIDTH = 26
"""The menu's columns, its border included: it fits the narrowest sidebar beside it. The one
source of it: the box is sized from it on mount, as it is placed."""

NOTE_WIDTH = MENU_WIDTH - 4
"""The columns of the line under Remove: the menu's, less its border and the line's padding."""


def remove_note(project: ProjectInfo) -> Text:
    """What an armed *Remove from fleet* says under it: what leaves the fleet, and what stays
    (the toast after the removal names the command that brings it back)."""
    text = Text(display_name(project), style="bold")
    text.append(" leaves the fleet; its folder and history are kept.")
    return text


class MenuOptions(OptionList):
    """The menu's options above Remove: ↓ from the last goes on to it, and ↑ from the first
    wraps round to it, as they would to one more option."""

    def action_cursor_down(self) -> None:
        if self.highlighted is not None and self.highlighted >= self.option_count - 1:
            self.screen.query_one(RemoveRow).focus()
        else:
            super().action_cursor_down()

    def action_cursor_up(self) -> None:
        if not self.highlighted:
            self.screen.query_one(RemoveRow).focus()
        else:
            super().action_cursor_up()


class RemoveRow(ConfirmButton):
    """*Remove from fleet*, the menu's last row: ↑ and ↓ go back to the options above."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "to_options(-1)", show=False),
        Binding("down", "to_options(0)", show=False),
    ]

    def action_to_options(self, index: int) -> None:
        options = self.screen.query_one(MenuOptions)
        options.highlighted = options.option_count - 1 if index < 0 else 0
        options.focus()


class ProjectMenu(ModalScreen[str | None]):
    """Rename, Pin / Unpin, Open settings, Pause agents…, Resume agents…, Start fresh…, Remove
    from fleet — under the card it is about.

    Answers :data:`RENAME`, :data:`PIN`, :data:`SETTINGS`, :data:`PAUSE`, :data:`RESUME`,
    :data:`FRESH`, or :data:`REMOVE` once Remove was pressed twice; Esc (with Remove armed, the
    first Esc disarms it), or a click anywhere off the menu, answers ``None``.
    ``can`` says whether a pause and a resume would act on any of its agents
    (``fleet_bulk.applicable``): the one that would not is shown greyed, never hidden, so the
    menu keeps its shape.
    """

    CSS = """
    ProjectMenu { align: left top; background: $background 20%; }
    ProjectMenu #project-menu { height: auto; border: round $accent; background: $surface; }
    ProjectMenu #project-menu-title {
        height: 1; padding: 0 1; text-style: bold; text-wrap: nowrap; text-overflow: ellipsis;
    }
    ProjectMenu OptionList { height: auto; border: none; padding: 0; }
    ProjectMenu #project-menu-remove {
        width: 100%; min-width: 0; height: 1; padding: 0; background: $surface;
        content-align: left middle; text-align: left; text-style: none;
        text-wrap: nowrap; text-overflow: ellipsis;
    }
    ProjectMenu #project-menu-remove:focus {
        background: $block-cursor-background; color: $block-cursor-foreground;
    }
    ProjectMenu #project-menu-remove.-armed { background: $error; color: $text; }
    ProjectMenu #project-menu-note { display: none; height: auto; padding: 0 1; }
    """
    BINDINGS: ClassVar = [("escape", "close_menu", "close")]

    def __init__(
        self, project: ProjectInfo, anchor: Region, can: Mapping[str, bool] | None = None
    ) -> None:
        super().__init__()
        self.project = project
        self.near = anchor
        """Where the card is on screen: the menu opens under its header line."""
        self.can = can if can is not None else {PAUSE: True, RESUME: True}

    def compose(self) -> ComposeResult:
        pin = "Unpin" if self.project.pinned_at is not None else "Pin"
        with Vertical(id="project-menu"):
            yield Static(Text(display_name(self.project)), id="project-menu-title")
            yield MenuOptions(
                Option("✎ Rename", id=RENAME),
                Option(f"📌 {pin}", id=PIN),
                Option(f"{SETTINGS_ICON} Open settings", id=SETTINGS),
                Option("⏸ Pause agents…", id=PAUSE, disabled=not self.can.get(PAUSE, True)),
                Option("▶ Resume agents…", id=RESUME, disabled=not self.can.get(RESUME, True)),
                Option("↺ Start fresh…", id=FRESH),
                id="project-menu-options",
            )
            yield RemoveRow(
                "✗ Remove from fleet",
                armed_label="✗ Remove? click again",
                note=remove_note(self.project),
                note_in="#project-menu-note",
                id="project-menu-remove",
                compact=True,
            )
            yield Static(id="project-menu-note")

    def on_mount(self) -> None:
        box = self.query_one("#project-menu", Vertical)
        options = self.query_one(OptionList)
        # The title, Remove and the border; and room for the note Remove shows when armed, so
        # that showing it moves nothing and is not cut by the screen's edge.
        note = remove_note(self.project).wrap(self.app.console, NOTE_WIDTH)
        height = options.option_count + 4 + len(note)
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

    @on(Button.Pressed, "#project-menu-remove")
    def _remove(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(REMOVE)  # the second press: the first armed it

    def on_click(self, event: events.Click) -> None:
        # A click that reaches the screen itself fell outside the menu: close, as Esc does.
        if event.widget is self:
            self.dismiss(None)

    def action_close_menu(self) -> None:
        self.dismiss(None)
