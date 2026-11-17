"""RLX R16 (#149): which pane has the keyboard is obvious at a glance.

The owner (2026-11-16): the active pane's cue was there but faint. While the sidebar holds the
keyboard, its highlighted row (a project, an agent, ⚙ Settings, any row) is a coloured bar,
the theme's ``primary`` with text that reads on it. While it does not, the row is exactly as
before: the neutral ``$primary 35%`` under the fade. Nothing else changes: the fade, the
product mark out of it, the faded buttons' labels, every size.

Measured as drawn in the real ``FleetApp``: the highlighted row's colours in both states under
rlx, textual-dark, textual-light and a floored theme (flexoki, whose primary does not read as
text on its own background); the highlight following the keyboard both ways; and a flip
changing nothing on screen but that one row.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.color import Color as RichColor
from rich.segment import Segment
from textual.color import Color
from textual.geometry import Region
from textual.pilot import Pilot
from textual.theme import BUILTIN_THEMES, Theme
from textual.widget import Widget

from aisquare.cli.ui.app import FleetApp
from aisquare.cli.ui.sidebar import Activatable, ProjectSelected
from aisquare.cli.ui.terminal import EscapeToSidebar
from aisquare.cli.ui.theme import (
    HIGHLIGHT_TEXT_VARIABLE,
    HIGHLIGHT_VARIABLE,
    KEYBOARD_CLASS,
    RLX_BLUE,
    RLX_GROUND,
    RLX_THEME,
    highlight_colours,
)
from tests import test_ui_shell as ui_suite
from tests.test_ui_agent_mark_contrast import contrast
from tests.test_ui_shell import Script, drive, fleet_app, seed, settle

no_real_tmux = ui_suite.no_real_tmux
script = ui_suite.script

TEXT = 4.5
THEMES = ["rlx", "textual-dark", "textual-light", "flexoki"]
TERMINALS = frozenset({"ansi-dark", "ansi-light"})


def _rgb(colour: RichColor | None) -> tuple[int, int, int]:
    assert colour is not None
    triplet = colour.get_truecolor()
    return (triplet.red, triplet.green, triplet.blue)


def _selected(app: FleetApp) -> Activatable:
    (row,) = [row for row in app.sidebar.query(Activatable) if row.has_class("selected")]
    return row


def _label(app: FleetApp) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """The highlighted row's name as drawn: its ink and its ground."""
    row = _selected(app)
    (strip,) = row.render_lines(Region(0, 0, row.size.width, 1))
    segment = next(s for s in strip if s.text.strip() and "proj" in s.text)
    assert segment.style is not None
    return _rgb(segment.style.color), _rgb(segment.style.bgcolor)


async def _keyboard_on(app: FleetApp, where: str) -> None:
    if where == "sidebar":
        app.sidebar.focus()
    else:
        shown = app.content.visible_content
        assert shown is not None
        next(w for w in shown.query("*") if w.focusable and w.display).focus()
    await settle(app)


async def _select(app: FleetApp) -> None:
    """The project's row highlighted, its view (tabs, *Start manager*) on the right."""
    app.post_message(ProjectSelected("prj_a"))
    await settle(app)


@pytest.mark.parametrize("theme", THEMES)
def test_the_highlighted_row_is_a_coloured_bar_only_while_the_sidebar_holds_the_keyboard(
    theme: str, tmp_path: Path, script: Script
) -> None:
    seed(tmp_path, ("prj_a", "proj", None))

    async def body(pilot: Pilot[None]) -> dict[str, tuple[tuple[int, int, int], ...]]:
        app = fleet_app(pilot)
        app.theme = theme
        await _select(app)
        seen = {}
        for where in ("sidebar", "content", "sidebar"):
            await _keyboard_on(app, where)
            assert app.sidebar.has_class(KEYBOARD_CLASS) is (where == "sidebar")
            seen[where] = _label(app)
        variables = app.get_css_variables()
        bar = Color.parse(variables[HIGHLIGHT_VARIABLE]).rgb
        ink = Color.parse(variables[HIGHLIGHT_TEXT_VARIABLE]).rgb
        app.screen.set_focus(None)  # nobody holds it: no fade, no bar, the plain highlight
        await settle(app)
        assert not app.sidebar.has_class(KEYBOARD_CLASS)
        return {**seen, "theme": (ink, bar), "nobody": _label(app)}

    seen = drive(body)
    ink, bar = seen["theme"]
    assert seen["sidebar"] == (ink, bar), "the keyboard's side: the theme's coloured bar"
    assert seen["content"][1] != bar, "the other side: the neutral highlight, as before"
    # The neutral highlight under the fade, not the bar under it: nearer the plain highlight.
    drawn, neutral = seen["content"][1], seen["nobody"][1]
    assert neutral != bar

    def far(a: tuple[int, int, int], b: tuple[int, int, int]) -> int:
        return sum(abs(x - y) for x, y in zip(a, b, strict=True))

    assert far(drawn, neutral) < far(drawn, bar), (drawn, neutral, bar)
    for where in ("sidebar", "content"):
        fg, bg = seen[where]
        assert contrast(fg, bg) >= TEXT, (theme, where, fg, bg)


def test_rlxs_bar_is_its_blue_with_its_ground_as_text() -> None:
    assert RLX_THEME.variables[HIGHLIGHT_VARIABLE] == RLX_BLUE
    assert RLX_THEME.variables[HIGHLIGHT_TEXT_VARIABLE] == RLX_GROUND
    ratio = contrast(Color.parse(RLX_GROUND).rgb, Color.parse(RLX_BLUE).rgb)
    assert round(ratio, 2) == 4.63


@pytest.mark.parametrize("theme", sorted(set(BUILTIN_THEMES) - TERMINALS))
def test_every_themes_bar_is_its_primary_and_its_text_reads(theme: str) -> None:
    bar, text = highlight_colours(BUILTIN_THEMES[theme])
    primary = Color.parse(BUILTIN_THEMES[theme].to_color_system().generate()["primary"])
    assert Color.parse(bar) == primary
    assert contrast(Color.parse(text).rgb, primary.rgb) >= TEXT, (theme, bar, text)


def _drawn(app: FleetApp) -> tuple[RichColor, RichColor]:
    """The highlighted row's name as drawn, in the terminal's own colours: its ink and ground."""
    row = _selected(app)
    (strip,) = row.render_lines(Region(0, 0, row.size.width, 1))
    segment = next(s for s in strip if s.text.strip() and "proj" in s.text)
    assert segment.style is not None
    assert segment.style.color is not None and segment.style.bgcolor is not None
    return segment.style.color, segment.style.bgcolor


@pytest.mark.parametrize("theme", sorted(TERMINALS))
def test_under_a_terminal_theme_the_bar_is_not_the_neutral_rows_colour(
    theme: str, tmp_path: Path, script: Script
) -> None:
    """#518's tester (seq 12727): under ansi-light the bar and the neutral row were the same
    terminal blue (SGR 44), only the text changing. Under every ANSI theme the bar's ground is
    neither the neutral row's (another pane holds the keyboard) nor the plain highlight's
    (nobody does), and its text reads on it: reverse video of the terminal's own text."""
    seed(tmp_path, ("prj_a", "proj", None))

    async def body(pilot: Pilot[None]) -> dict[str, tuple[RichColor, RichColor]]:
        app = fleet_app(pilot)
        app.theme = theme
        await _select(app)
        seen = {}
        for where in ("sidebar", "content"):
            await _keyboard_on(app, where)
            seen[where] = _drawn(app)
        app.screen.set_focus(None)
        await settle(app)
        seen["nobody"] = _drawn(app)
        return seen

    seen = drive(body)
    (ink, bar), (_, neutral), (_, plain) = seen["sidebar"], seen["content"], seen["nobody"]
    assert bar.number is not None, f"the bar is one of the terminal's colours: {bar}"
    assert bar.number != neutral.number and bar.number != plain.number, (bar, neutral, plain)
    assert ink.number is not None and ink.number != bar.number, (ink, bar)
    # In the standard palette, as no terminal's own can be known.
    assert contrast(_rgb(ink), _rgb(bar)) >= TEXT, (theme, ink, bar)


def test_a_terminal_theme_without_its_text_colours_takes_its_cursor_off_the_neutral_row() -> None:
    """A terminal theme that names no ``ansi-foreground`` and ``ansi-background`` takes its
    focused cursor's pair, reversed when the cursor's ground is the neutral row's (its
    primary), as ansi-light's is."""
    cursor = {"block-cursor-background": "ansi_blue", "block-cursor-foreground": "ansi_white"}
    blue = Theme(name="t-ansi-blue", ansi=True, primary="ansi_blue", variables=cursor)
    assert highlight_colours(blue) == ("ansi_white", "ansi_blue")
    green = Theme(
        name="t-ansi-green",
        ansi=True,
        primary="ansi_blue",
        variables={**cursor, "block-cursor-background": "ansi_green"},
    )
    assert highlight_colours(green) == ("ansi_green", "ansi_white")


def test_the_highlight_follows_the_keyboard_both_ways(tmp_path: Path, script: Script) -> None:
    """Tab and Shift+Tab, F12, a click on either pane, a dialog opened and closed: the bar
    is there exactly while the sidebar holds the keyboard."""
    seed(tmp_path, ("prj_a", "proj", None))

    async def body(pilot: Pilot[None]) -> list[tuple[str, bool, bool]]:
        app = fleet_app(pilot)
        await _select(app)
        seen: list[tuple[str, bool, bool]] = []

        def look(step: str) -> None:
            bar = Color.parse(app.get_css_variables()[HIGHLIGHT_VARIABLE]).rgb
            holds = app.sidebar.has_focus_within
            seen.append((step, holds, _label(app)[1] == bar))

        await _keyboard_on(app, "sidebar")
        look("sidebar focused")
        await pilot.press("tab")
        await settle(app)
        look("tab")
        await pilot.press("shift+tab")
        await settle(app)
        look("shift+tab")
        await _keyboard_on(app, "content")
        look("content focused")
        # F12 in an agent's pane: the pane posts this, and the shell gives the sidebar the keyboard.
        app.post_message(EscapeToSidebar())
        await settle(app)
        look("F12 from a pane")
        # The page's active tab: focusable (prose is not), and clicking it changes no view.
        await pilot.click(app.content, offset=(2, 0))
        await settle(app)
        look("a click on the page")
        await pilot.click(_selected(app))
        await settle(app)
        look("a click on the row")
        app.action_help()
        await settle(app)
        app.pop_screen()
        await settle(app)
        look("a dialog closed")
        return seen

    seen = drive(body)
    # Where each step leaves the keyboard: the sidebar (True) or the page (False).
    assert [holds for _, holds, _ in seen] == [True, False, True, False, True, False, True, True]
    wrong = [step for step, holds, bar in seen if holds is not bar]
    assert not wrong, seen


def _screen(app: FleetApp) -> list[list[Segment]]:
    return [
        list(strip) for (strip,) in app.screen._compositor.render_full_update(simplify=True).strips
    ]


def test_a_flip_changes_nothing_on_screen_but_the_highlighted_row(
    tmp_path: Path, script: Script
) -> None:
    """The keyboard's mark on a side, taken off and put back without moving the keyboard: the
    only cells that change are the highlighted row's (the sidebar), and none (the page)."""
    seed(tmp_path, ("prj_a", "proj", None), ("prj_b", "other", None))

    async def body(pilot: Pilot[None]) -> tuple[set[int], int, set[int]]:
        app = fleet_app(pilot)
        await _select(app)
        await _keyboard_on(app, "sidebar")
        row = _selected(app).region.y

        async def toggled(side: Widget) -> set[int]:
            before = _screen(app)
            side.set_class(False, KEYBOARD_CLASS)
            app.stylesheet.update_nodes([side, *side.query("Activatable.selected")])
            await settle(app)
            after = _screen(app)
            side.set_class(True, KEYBOARD_CLASS)
            app.stylesheet.update_nodes([side, *side.query("Activatable.selected")])
            await settle(app)
            return {y for y, (a, b) in enumerate(zip(before, after, strict=True)) if a != b}

        sidebar_rows = await toggled(app.sidebar)
        await _keyboard_on(app, "content")
        content_rows = await toggled(app.content)
        return sidebar_rows, row, content_rows

    sidebar_rows, row, content_rows = drive(body)
    assert sidebar_rows == {row}, (sidebar_rows, row)
    assert content_rows == set()
