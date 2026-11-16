"""Themes for the fleet UI: the board's stays-open picker and its autosave, reused.

``board -w`` already solved this (docs/plans/fleet-tui.md §4.3: "the theme
picker and its autosave in ``state.json`` are reused verbatim"): a modal that
applies every highlighted theme instantly and persists it under one key, so the
board and the fleet UI share a look and a user picks it once. The loader and
the theme's key are IMPORTED from ``cli.watch`` (the file itself has one reader
and writer in ``core.state_file``; the save is ``cli.ui.autosave``'s), and only
the widget is rebuilt here, because the board's picker is a local class inside
its app factory and cannot be imported.

Both apps also register our own theme, ``rlx`` (:data:`RLX_THEME`), start in it
while nothing is saved (:func:`restore_theme`), and give every theme the
product mark's colour (:data:`MARK_VARIABLE`, :func:`theme_variable_defaults`).
"""

from __future__ import annotations

from typing import Any, ClassVar

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from aisquare.cli.ui.autosave import Autosave
from aisquare.cli.watch import _THEME_KEY, _load_saved_theme

RLX_BLUE = "#2F74F0"
"""The rlx brand colour: the wordmark's on aisquare.studio/rlx, and the mark's under ``rlx``."""

MARK_VARIABLE = "rlx-mark"
"""The theme variable the product mark is drawn in: the sidebar's ``rlx`` and Welcome's heading.

``rlx`` sets it to :data:`RLX_BLUE`. Every other theme leaves it unset, and
:func:`theme_variable_defaults` gives it that theme's ``primary``, so the mark changes
colour with the theme that ``t`` picks.
"""

MARK_STYLE = f"${MARK_VARIABLE}"
"""The mark's style, for a ``textual.content.Content`` span.

The variable is looked up when the span is drawn, so a theme change repaints the mark
with no rebuild. An app that does not define the variable draws the mark in its plain
text colour. It does not raise.
"""

RLX_GROUND = "#08090B"
"""``rlx``'s background, near-black. #2F74F0 on it is 4.63:1, above text's 4.5:1."""

RLX_THEME = Theme(
    name="rlx",
    # #2F74F0 with its triad (magenta, lime) and its complement (amber) around it.
    # Nothing else is blue. tests/test_ui_rlx_theme.py measures every colour against the
    # grounds it is drawn on.
    primary=RLX_BLUE,
    secondary="#E8FF3A",
    accent="#FF4FD8",
    warning="#FF9F1C",
    error="#FF5A5F",
    success="#2BF07A",
    foreground="#F1F2F4",
    background=RLX_GROUND,
    surface="#121317",
    panel="#1A1B20",
    dark=True,
    variables={
        MARK_VARIABLE: RLX_BLUE,
        # Over #2F74F0, white text is 4.31:1 and this ground is 4.63:1. A primary button's label
        # and a list's highlighted row take the ground, as nord's and gruvbox's do. Every other
        # fill is bright enough that Textual's automatic text on it is dark already.
        "button-color-foreground": RLX_GROUND,
        "block-cursor-foreground": RLX_GROUND,
        # Textual lifts a dark theme's rows by 4% white (``$boost``: Spawn agent, a group's
        # header, a focused account) and a hovered list row by 10%. It does so only when
        # it derives the panel, and this panel is our own neutral one, not a blue blend.
        # Both are set as textual-dark draws them, or neither lift would show.
        "boost": "#FFFFFF0A",
        "block-hover-background": "#FFFFFF19",
    },
)
"""Our own theme, and the default one (:data:`DEFAULT_THEME`)."""

DEFAULT_THEME = RLX_THEME.name
"""The theme both apps start in while ``state.json`` holds no theme they can show."""


def register_themes(app: App[Any]) -> None:
    """Make ``rlx`` one of ``app``'s themes, beside Textual's built-in ones.

    Call it in the app's ``__init__``, before :func:`restore_theme`, and in every app that
    offers or restores the saved theme. The fleet UI and ``board -w`` share that theme, and
    each must be able to show a pick made in the other.
    """
    app.register_theme(RLX_THEME)


def theme_variable_defaults(app: App[Any]) -> dict[str, str]:
    """The variables our apps add to every theme: :data:`MARK_VARIABLE`, as its ``primary``.

    Merge it into the app's ``get_theme_variable_defaults``, Textual's place for an app's
    own variables. A theme that sets one (``rlx`` sets the mark) overrides it there.
    """
    return {MARK_VARIABLE: app.current_theme.primary}


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
    """Apply the autosaved theme to ``app``, else :data:`DEFAULT_THEME`; ``False`` for the default.

    A saved name the running textual no longer ships is ignored rather than
    raised: a theme is a preference, and losing it costs a look, not the app.
    It gets the default, like a home with no theme saved.
    """
    saved = _load_saved_theme()
    if saved and saved in app.available_themes:
        app.theme = saved
        return True
    app.theme = DEFAULT_THEME
    return False


def theme_autosave(app: App[Any]) -> Autosave:
    """The theme's saver for ``app``: debounced and off the event loop, under the board's key.

    Every picker highlight is a theme change, at key autorepeat, and each save
    is a lock, a read, a write, an fsync and a rename. ``Autosave`` collapses a
    burst into one write on a worker thread and says a refusal once — the
    picker has already shown the theme applied, and silence would promise a
    memory the file has refused. Flush it at unmount: a pick inside the debounce
    before ``q`` is a preference the user expressed.

    The fleet UI and ``board -w`` both build it here. It reads nothing: its
    ``latest`` is consulted by nobody (whether the file already says so is
    ``update_state``'s to decide, under its lock), and reading the file for it
    was a second read at every start-up beside :func:`restore_theme`'s
    (review of the #167 fold, F8).
    """
    return Autosave(app, _THEME_KEY, what="the theme")
