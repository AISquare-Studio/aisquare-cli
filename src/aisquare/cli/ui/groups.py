"""The sidebar's grouping gestures (#140): messages, the group picker, the drag state.

Every gesture — a key, a drag, a click — becomes one of the messages below and
the app applies it through ``services.project_groups``, so the sidebar never
touches the store and every change lands on the same undo stack the CLI's
state would. The picker is a small modal: the existing groups, *New group…*
and *Ungroup*; it answers with a token the app turns into a move.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Literal

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from aisquare.models import ProjectGroup

Kind = Literal["project", "group"]


class MoveRow(Message):
    """Step a project or a group one place inside its scope (``delta`` ±1)."""

    def __init__(self, kind: Kind, ident: str, delta: int) -> None:
        super().__init__()
        self.kind: Kind = kind
        self.ident = ident
        self.delta = delta


class TogglePin(Message):
    def __init__(self, kind: Kind, ident: str) -> None:
        super().__init__()
        self.kind: Kind = kind
        self.ident = ident


class ToggleCollapse(Message):
    def __init__(self, group_id: str) -> None:
        super().__init__()
        self.group_id = group_id


class GroupProjects(Message):
    """Open the picker for these projects (the cursor's, or the multi-selection)."""

    def __init__(self, project_ids: list[str]) -> None:
        super().__init__()
        self.project_ids = project_ids


class DropProject(Message):
    """A drag ended: put ``project_ids`` into ``scope`` before ``before`` (or at the end)."""

    def __init__(self, project_ids: list[str], *, scope: str | None, before: str | None) -> None:
        super().__init__()
        self.project_ids = project_ids
        self.scope = scope
        """A group id, or ``None`` for the top level."""
        self.before = before


class DropGroup(Message):
    """A group header was dragged before another group (``None`` = to the end)."""

    def __init__(self, group_id: str, *, before: str | None) -> None:
        super().__init__()
        self.group_id = group_id
        self.before = before


class UndoLayout(Message):
    pass


@dataclass
class DragState:
    """What a press-and-hold on a card or a group header turned into."""

    kind: Kind
    ident: str
    project_ids: list[str] = field(default_factory=list)
    started: bool = False
    """True once the pointer moved past the press row — a plain click is not a drag."""
    origin_y: int = 0


NEW_GROUP = "__new__"
UNGROUP = "__ungroup__"


class GroupPicker(ModalScreen[str | None]):
    """Pick a group for the selected projects: an existing one, a new one, or none.

    Answers ``"group:<id>"``, ``"new:<name>"`` or ``"ungroup"``; Esc answers
    ``None``. Enter on *New group…* moves focus to the name field; Enter there
    submits.
    """

    CSS = """
    GroupPicker { align: center middle; }
    #groupbox { width: 48; height: auto; max-height: 70%; border: heavy $accent;
                background: $surface; padding: 1; }
    #grouphint { height: 2; color: $text-muted; }
    #grouplist { height: auto; max-height: 12; }
    #groupname { margin-top: 1; }
    """
    BINDINGS: ClassVar = [("escape", "close_picker", "close")]

    def __init__(self, groups: list[ProjectGroup], count: int) -> None:
        super().__init__()
        self._groups = groups
        self._count = count

    def compose(self) -> ComposeResult:
        noun = "project" if self._count == 1 else "projects"
        with Vertical(id="groupbox"):
            yield Static(
                f"group {self._count} {noun} — ↑/↓ then Enter · Esc cancels",
                id="grouphint",
            )
            picker = OptionList(id="grouplist")
            yield picker
            yield Input(placeholder="new group name…", id="groupname")

    def on_mount(self) -> None:
        picker = self.query_one("#grouplist", OptionList)
        for group in self._groups:
            # A Text, never a markup string: a group name is the operator's text, and
            # `client [/api]` parsed as markup raised MarkupError on every `g`, taking
            # the whole TUI down (review of #203).
            picker.add_option(Option(Text(f"📁 {group.name}"), id=f"group:{group.id}"))
        picker.add_option(Option("+ New group…", id=NEW_GROUP))
        picker.add_option(Option("⤴ Ungroup (back to the top level)", id=UNGROUP))
        picker.highlighted = 0
        picker.focus()

    @on(OptionList.OptionSelected, "#grouplist")
    def _chosen(self, event: OptionList.OptionSelected) -> None:
        choice = event.option.id
        if choice == NEW_GROUP:
            self.query_one("#groupname", Input).focus()
            return
        if choice == UNGROUP:
            self.dismiss("ungroup")
            return
        self.dismiss(choice)

    @on(Input.Submitted, "#groupname")
    def _named(self, event: Input.Submitted) -> None:
        name = event.value.strip()
        if name:
            self.dismiss(f"new:{name}")

    def action_close_picker(self) -> None:
        self.dismiss(None)
