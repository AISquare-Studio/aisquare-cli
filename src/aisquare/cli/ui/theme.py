"""Themes for the fleet UI: the board's stays-open picker and its autosave, reused.

``board -w`` already solved this (docs/plans/fleet-tui.md §4.3: "the theme
picker and its autosave in ``state.json`` are reused verbatim"): a modal that
applies every highlighted theme instantly and persists it under one key, so the
board and the fleet UI share a look and a user picks it once. The load/save
helpers are IMPORTED from ``cli.watch`` — one home for the state-file format —
and only the widget is rebuilt here, because the board's picker is a local
class inside its app factory and cannot be imported.
"""

from __future__ import annotations

from typing import Any, ClassVar

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from aisquare.cli.watch import _load_saved_theme, _save_theme


class ThemePicker(ModalScreen[None]):
    """Browse themes; every highlight applies (and autosaves) at once. Esc closes.

    Selection deliberately does NOT close the dialog: the point of a picker is
    to compare, and closing on the first pick forces a reopen per comparison.
    """

    CSS = """
    ThemePicker { align: center middle; }
    #themebox { width: 44; height: 70%; border: heavy $accent;
                background: $surface; padding: 1; }
    #themehint { height: 2; color: $text-muted; }
    #themelist { height: 1fr; }
    """
    BINDINGS: ClassVar = [("escape", "close_picker", "close")]

    def compose(self) -> ComposeResult:
        with Vertical(id="themebox"):
            yield Static(
                "browse themes — ↑/↓ or click applies instantly (autosaved) · Esc closes",
                id="themehint",
            )
            yield OptionList(id="themelist")

    def on_mount(self) -> None:
        picker = self.query_one("#themelist", OptionList)
        current = self.app.theme
        for index, name in enumerate(sorted(self.app.available_themes)):
            picker.add_option(Option(name, id=name))
            if name == current:
                picker.highlighted = index
        picker.focus()

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option.id is not None:
            self.app.theme = event.option.id  # applied live; the app's watcher autosaves

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        # Enter / click applies too — and the picker stays open on purpose.
        if event.option.id is not None:
            self.app.theme = event.option.id

    def action_close_picker(self) -> None:
        self.dismiss(None)


def restore_theme(app: App[Any]) -> bool:
    """Apply the autosaved theme to ``app``; ``False`` when there is none to apply.

    A saved name the running textual no longer ships is ignored rather than
    raised: a theme is a preference, and losing it costs a look, not the app.
    """
    saved = _load_saved_theme()
    if saved and saved in app.available_themes:
        app.theme = saved
        return True
    return False


def remember_theme(name: str) -> None:
    """Autosave ``name`` — every change is the save; there is no save step."""
    _save_theme(name)
