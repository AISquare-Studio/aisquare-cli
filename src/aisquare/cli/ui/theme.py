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

import os
from collections.abc import Callable
from typing import Any, ClassVar

from textual.app import App, ComposeResult
from textual.color import Color
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.visual import VisualType
from textual.widget import Widget
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from aisquare.cli.ui.autosave import Autosave
from aisquare.cli.watch import _THEME_KEY, _load_saved_theme

RLX_BLUE = "#2F74F0"
"""The rlx brand colour: the wordmark's on aisquare.studio/rlx, and the mark's under ``rlx``."""

MARK_VARIABLE = "rlx-mark"
"""The theme variable the product mark is drawn in: the sidebar's logo, ``r\\x`` (R23), and
Welcome's heading, ``rlx``.

``rlx`` sets it to :data:`RLX_BLUE`. Every other theme leaves it unset, and
:func:`theme_variable_defaults` gives it that theme's ``primary``, or its ``foreground`` where
the primary does not read as text (:func:`mark_colour`), so the mark changes colour with the
theme that ``t`` picks.
"""

TEXT_CONTRAST = 4.5
"""WCAG 2's floor for text (1.4.3): the mark is the product's name or logo, and it must read."""

MARK_STYLE = f"${MARK_VARIABLE}"
"""The mark's style, for a ``textual.content.Content`` span.

The variable is looked up when the span is drawn, so a theme change repaints the mark
with no rebuild. An app that does not define the variable draws the mark in its plain
text colour. It does not raise.
"""

RLX_GROUND = "#08090B"
"""``rlx``'s background, near-black. #2F74F0 on it is 4.63:1, above text's 4.5:1."""

HIGHLIGHT_VARIABLE = "rlx-highlight"
"""The sidebar's highlighted row while the sidebar holds the keyboard (R16, #149): a coloured
bar, the theme's ``primary`` (:func:`highlight_colours`), where the row is the neutral
``$primary 35%`` otherwise. The contrast between the two is the cue for which pane has the
keyboard."""

HIGHLIGHT_TEXT_VARIABLE = "rlx-highlight-text"
"""The text on :data:`HIGHLIGHT_VARIABLE`'s bar, which reads at 4.5:1 or more on it."""

KEYBOARD_CLASS = "-keyboard"
"""The class the shell's panes put on the side that holds the keyboard (``Panes``), beside the
``-unfocused`` the other side takes."""

FADE_OPACITY = 0.82
"""The opacity of the side that does not hold the keyboard (``Panes > .-unfocused``): its text
is drawn at 82% over its ground. The product mark is not (:class:`MarkStatic`)."""

RLX_THEME = Theme(
    name="rlx",
    # #2F74F0 with a teal beside it (the owner's pick, 2026-11-17, for the pink), a lime, and
    # its complement (amber) around it. Nothing else is blue: the teal's hue, 173°, sits just
    # outside blue's. tests/test_ui_rlx_theme.py measures every colour against the grounds
    # it is drawn on.
    primary=RLX_BLUE,
    secondary="#E8FF3A",
    accent="#14B8A6",
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
        HIGHLIGHT_VARIABLE: RLX_BLUE,
        HIGHLIGHT_TEXT_VARIABLE: RLX_GROUND,  # 4.63:1 on the blue, as its buttons' labels
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
        # An input's placeholder and disabled text: Textual's 38% of the text colour is about
        # 3.5:1 here. 52% reads at 4.5:1 or more on every ground rlx draws.
        "text-disabled": "auto 52%",
    },
)
"""Our own theme, and the default one (:data:`DEFAULT_THEME`)."""

RLX_CSS = """
/* rlx's buttons (#139). Every label in a coloured button is the near-black ground (the theme's
   button-color-foreground) on a bright fill, so no state may darken that fill: Textual darkens
   it on hover (-darken-2) and on a press (a 30% tint of the background), which took a label to
   3:1. Under rlx, a hover brightens the fill and a press keeps it. A disabled button reads too
   (4.5:1 or more): the muted text colour on the surface, at full opacity, where Textual
   dims the whole button. Colours only, never a border or a size: a compact button has no
   border, and one given here grew a row in the Accounts table. */
App.-theme-rlx Button.-style-default {
    &.-primary:hover { background: $primary-lighten-1; }
    &.-success:hover { background: $success-lighten-1; }
    &.-warning:hover { background: $warning-lighten-1; }
    &.-error:hover { background: $error-lighten-1; }
    &.-active { tint: transparent; }
    &:disabled, &:disabled:hover {
        opacity: 1;
        text-opacity: 1;
        color: $text-muted;
        background: $surface;
    }
}
/* The side without the keyboard fades (Panes > .-unfocused, 82%), and Textual fades a button's
   fill twice, its label once: a dark label on the blue fill fell to 2.61:1 there, and on the
   coral one to 3.36:1. In the faded side those two take the light text colour instead, which
   the fade leaves at 4.56:1 and 3.54:1 (the most the coral allows; the manager's ruling on
   #508). Under that light label a hover darkens the fill, as Textual's own does, where the
   brighter fill above would take it to 3.79:1. A disabled one stays muted. Green and amber keep
   their dark label (6.09, 4.70). */
App.-theme-rlx Panes > .-unfocused Button.-style-default {
    &.-primary, &.-error { color: $foreground; }
    &.-primary:hover { background: $primary-darken-1; }
    &.-error:hover { background: $error-darken-1; }
    &:disabled, &:disabled:hover { color: $text-muted; background: $surface; }
}
"""
"""The rules ``rlx`` needs beyond its colours. Both apps add them to their own CSS; they
apply only while the theme is ``rlx`` (Textual puts ``-theme-rlx`` on the app)."""

FADE_RELABELLED = frozenset({"primary", "error"})
"""The button variants whose label :data:`RLX_CSS` changes on the faded side. The shell's fade
matches the CSS again for these, and for nothing else below a side (``Panes._fade``)."""

DEFAULT_THEME = RLX_THEME.name
"""The theme both apps start in while neither ``state.json`` nor ``TEXTUAL_THEME`` names one
they can show (:func:`restore_theme`)."""


def register_themes(app: App[Any]) -> None:
    """Make ``rlx`` one of ``app``'s themes, beside Textual's built-in ones.

    Call it in the app's ``__init__``, before :func:`restore_theme`, and in every app that
    offers or restores the saved theme. The fleet UI and ``board -w`` share that theme, and
    each must be able to show a pick made in the other.
    """
    app.register_theme(RLX_THEME)


def _luminance(colour: Color) -> float:
    def channel(value: int) -> float:
        share = value / 255
        return share / 12.92 if share <= 0.03928 else ((share + 0.055) / 1.055) ** 2.4

    red, green, blue = colour.rgb
    return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue)


def contrast_ratio(one: Color, other: Color) -> float:
    """WCAG 2's contrast ratio of two opaque colours, 1 to 21."""
    light, dark = sorted((_luminance(one), _luminance(other)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def faded(colour: Color, ground: Color) -> Color:
    """``colour`` as the faded side draws it: at :data:`FADE_OPACITY` over ``ground``."""
    return ground.blend(colour, FADE_OPACITY)


def mark_colour(theme: Theme) -> str:
    """The mark's colour under ``theme`` when it sets none: its ``primary``, read as text.

    Where the primary is under :data:`TEXT_CONTRAST` on the theme's own background (6 of
    Textual's built-ins, textual-dark's #0178D4 at 4.14:1 among them), the mark takes the
    theme's ``foreground``, the colour its text is drawn in, which reads. The mark is drawn at
    that colour on either side, the faded one included (:class:`MarkStatic`). A theme of the
    terminal's own ANSI colours keeps its primary: its real colours are the terminal's, and
    no ratio can be known.
    """
    colours = theme.to_color_system().generate()
    primary = Color.parse(colours["primary"])
    if theme.ansi or primary.ansi is not None:
        return theme.primary
    background = Color.parse(colours["background"])
    if contrast_ratio(primary, background) >= TEXT_CONTRAST:
        return colours["primary"]
    return colours["foreground"]


def highlight_colours(theme: Theme) -> tuple[str, str]:
    """The sidebar's highlighted row under ``theme`` while the sidebar holds the keyboard: the
    bar, the theme's ``primary``, and the text on it, black or white, whichever reads better
    (WCAG: one of the two always reads at 4.58:1 or more, on any colour). A theme of the
    terminal's own ANSI colours, whose ratios no one can know, takes :func:`_terminal_bar`."""
    colours = theme.to_color_system().generate()
    primary = Color.parse(colours["primary"])
    if theme.ansi or primary.ansi is not None:
        return _terminal_bar(colours)
    black, white = Color.parse("#000000"), Color.parse("#FFFFFF")
    text = max((black, white), key=lambda ink: contrast_ratio(ink, primary))
    return colours["primary"], text.hex


def _terminal_bar(colours: dict[str, str]) -> tuple[str, str]:
    """An ANSI theme's bar: reverse video of the terminal's own text, its ``ansi-foreground``
    as the bar and its ``ansi-background`` as the text, the pair the terminal draws its text
    in. The neutral row is the theme's ``primary`` (``$primary 35%``: ANSI has no alpha), and
    ansi-light's focused cursor is that same blue, so its pair left the two rows one colour,
    only the text changing (#518's tester, seq 12727). ansi-dark's cursor pair IS that reverse
    video, unchanged. A theme without the two takes its cursor's pair, reversed should its
    ground be the neutral row's."""
    neutral = Color.parse(colours["primary"])
    ink, ground = colours.get("ansi-foreground"), colours.get("ansi-background")
    if ink and ground and Color.parse(ink) != neutral:
        return ink, ground
    bar, text = colours["block-cursor-background"], colours["block-cursor-foreground"]
    return (text, bar) if Color.parse(bar) == neutral else (bar, text)


def theme_variable_defaults(app: App[Any]) -> dict[str, str]:
    """The variables our apps add to every theme: the mark's (:data:`MARK_VARIABLE`, as
    :func:`mark_colour`) and the sidebar's highlight while it holds the keyboard
    (:data:`HIGHLIGHT_VARIABLE`, :data:`HIGHLIGHT_TEXT_VARIABLE`, as :func:`highlight_colours`).

    Merge it into the app's ``get_theme_variable_defaults``, Textual's place for an app's
    own variables. A theme that sets one (``rlx`` sets all three) overrides it there.
    """
    theme = app.current_theme
    bar, text = highlight_colours(theme)
    return {
        MARK_VARIABLE: mark_colour(theme),
        HIGHLIGHT_VARIABLE: bar,
        HIGHLIGHT_TEXT_VARIABLE: text,
    }


def on_faded_side(widget: Widget) -> bool:
    """Whether ``widget`` is on the side that does not hold the keyboard (``-unfocused``)."""
    return any(node.has_class("-unfocused") for node in widget.ancestors_with_self)


class MarkStatic(Static):
    """A ``Static`` that draws the product mark, which the fade never reaches.

    The manager's ruling on #508's F1. The side without the keyboard is drawn at
    :data:`FADE_OPACITY`, and the mark faded with it: #2F74F0 became #2760C6, 3.39:1 under
    rlx, and ten other themes' marks fell under 4.5:1. Textual applies opacity widget by
    widget as it paints (``Widget.opacity``, its ancestors' product), so this one reports
    full strength and is never blended. While its side is faded, the rest of its text is
    handed, through ``build(dim)``, the colour the fade would have drawn it in
    (:meth:`faded_ink`): only the mark is left out of the fade.

    It looks again whenever its styles change (``notify_style_update``): the shell's fade
    tells everything below the side it fades, and a theme change restyles everything.
    :meth:`rebuild` takes new content (the sidebar's live count)."""

    def __init__(self, build: Callable[[str | None], VisualType], **kwargs: Any) -> None:
        self._build = build
        self._dim: str | None = None
        super().__init__(build(None), **kwargs)

    @property
    def opacity(self) -> float:
        """Full strength while it is shown at all: the fade is not the mark's."""
        return 1.0 if super().opacity > 0 else 0.0

    def faded_ink(self) -> str | None:
        """The colour the fade would draw this widget's text in, or ``None`` when its side is
        not faded: its text colour at :data:`FADE_OPACITY` over its ground."""
        if not on_faded_side(self):
            return None
        style = self.rich_style
        if style.color is None or style.bgcolor is None:
            return None
        ground = Color.from_rich_color(style.bgcolor)
        return faded(Color.from_rich_color(style.color), ground).hex

    def rebuild(self) -> None:
        self._dim = self.faded_ink()
        self.update(self._build(self._dim))

    def on_mount(self) -> None:
        self.rebuild()

    def notify_style_update(self) -> None:
        super().notify_style_update()
        if self.is_mounted and self.faded_ink() != self._dim:
            self.rebuild()


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


THEME_ENV = "TEXTUAL_THEME"
"""Textual's own setting for the start theme, which a user may have exported before ``rlx``
became the default. It still ranks above :data:`DEFAULT_THEME`, and below a saved pick."""


def _environment_theme(app: App[Any]) -> str | None:
    """The first theme :data:`THEME_ENV` names that ``app`` offers, as Textual reads it: one
    name, or several separated by commas."""
    for name in os.environ.get(THEME_ENV, "").split(","):
        if name.strip() in app.available_themes:
            return name.strip()
    return None


def restore_theme(app: App[Any]) -> bool:
    """Apply the start theme to ``app``; ``True`` only when it is the autosaved one.

    The first of these that ``app`` offers: the theme saved in ``state.json``, the one
    ``TEXTUAL_THEME`` names (:data:`THEME_ENV`), then :data:`DEFAULT_THEME`. A name the
    running textual no longer ships is passed over rather than raised: a theme is a
    preference, and losing it costs a look, not the app.
    """
    saved = _load_saved_theme()
    if saved and saved in app.available_themes:
        app.theme = saved
        return True
    app.theme = _environment_theme(app) or DEFAULT_THEME
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
