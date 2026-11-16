"""RLX R6 (#139): our own theme, ``rlx``, is the default, and the product mark follows the theme.

The owner's ask (2026-11-16): a palette built around the brand's #2F74F0 without being shades of
blue; the wordmark ``rlx`` in #2F74F0; the mark's colour a theme variable, so ``t`` recolours it
with the theme; ``rlx`` the start theme while ``state.json`` holds no pick, a saved pick kept;
every built-in theme still offered. Both apps that offer and restore the shared theme are
measured: the fleet UI and ``board -w``.

The contrast table (:func:`table`) is the one the PR carries. Every colour the theme sets is
measured against each ground it is drawn on, as Textual resolves both: 4.5:1 for text, 3:1 for
a border or a mark. A palette edit that falls under a floor fails here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
from rich.segment import Segment
from textual.app import App
from textual.color import Color
from textual.geometry import Region
from textual.pilot import Pilot
from textual.theme import BUILTIN_THEMES, Theme
from textual.widget import Widget
from textual.widgets import OptionList, Static

from aisquare.cli import watch as watch_mod
from aisquare.cli.ui.app import FleetApp
from aisquare.cli.ui.theme import (
    DEFAULT_THEME,
    MARK_STYLE,
    MARK_VARIABLE,
    RLX_BLUE,
    RLX_THEME,
    theme_variable_defaults,
)
from aisquare.cli.ui.views.welcome import WelcomeView
from aisquare.services import team as team_service
from tests import test_ui_shell as ui_suite
from tests.projects import add_project
from tests.test_ui_agent_mark_contrast import contrast
from tests.test_ui_shell import Script, _state, _write_state, drive, fleet_app, seed, settle
from tests.ui_workers import settle_page

no_real_tmux = ui_suite.no_real_tmux
script = ui_suite.script

TERMINALS = frozenset({"ansi-dark", "ansi-light"})
"""The themes whose colours are the terminal's own ANSI ones: no hex to compare the mark with."""


# --- the theme ----------------------------------------------------------------------------


def test_rlx_is_dark_and_its_mark_is_the_brand_blue_and_its_primary() -> None:
    assert RLX_THEME.name == "rlx" == DEFAULT_THEME
    assert RLX_THEME.dark
    assert RLX_THEME.variables[MARK_VARIABLE] == RLX_BLUE == "#2F74F0" == RLX_THEME.primary
    assert MARK_STYLE == "$rlx-mark"


def _chroma(colour: str) -> int:
    red, green, blue = Color.parse(colour).rgb
    return max(red, green, blue) - min(red, green, blue)


def _hue(colour: str) -> float:
    hue, _, _ = Color.parse(colour).hsl
    return hue * 360


def test_the_palette_is_built_around_the_blue_without_being_shades_of_it() -> None:
    """The owner: "built around #2F74F0 but NOT shades of blue". The primary is the one blue.
    Every other colour is a near-neutral (the grounds and the text) or sits outside blue's
    hues (180° to 260°, cyan to violet)."""
    coloured: dict[str, str] = {}
    for field in ("secondary", "accent", "warning", "error", "success"):
        value = getattr(RLX_THEME, field)
        assert value is not None, f"rlx leaves {field} to Textual: that colour is then a blue"
        coloured[field] = value
    for field in ("foreground", "background", "surface", "panel"):
        value = getattr(RLX_THEME, field)
        assert value is not None and _chroma(value) <= 10, (field, value)
    blue = {name: value for name, value in coloured.items() if 180 <= _hue(value) <= 260}
    assert not blue, f"shades of blue beside the primary: {blue}"
    hues = sorted(_hue(value) for value in coloured.values())
    gaps = [b - a for a, b in pairwise(hues)] + [360 - hues[-1] + hues[0]]
    assert min(gaps) >= 25, f"two accents a glance cannot tell apart: {coloured}"


# --- the contrast table ---------------------------------------------------------------------

TEXT = 4.5
"""WCAG 2's ratio for text (1.4.3)."""
MARK = 3.0
"""WCAG 2's ratio for a border or a graphical mark (1.4.11)."""


@dataclass(frozen=True)
class Pair:
    ink: str
    ground: tuple[str, ...]
    """The layers under the ink, bottom first: a variable, or ``name@35`` for it at 35%."""
    floor: float
    where: str


PAIRS: tuple[Pair, ...] = (
    Pair(MARK_VARIABLE, ("background",), TEXT, "the product mark: the sidebar's rlx, Welcome's"),
    Pair("primary", ("background",), MARK, "borders: cards, sections, the divider, focus"),
    Pair("primary", ("surface",), MARK, "a dialog's border"),
    Pair("primary", ("panel",), MARK, "a border beside an odd project card"),
    Pair("button-color-foreground", ("primary",), TEXT, "a primary button's label"),
    Pair("block-cursor-foreground", ("block-cursor-background",), TEXT, "a list's highlighted row"),
    Pair("text", ("background", "primary@35"), TEXT, "the sidebar's selected row"),
    Pair("accent", ("background",), TEXT, "+, the filter, the instances chip"),
    Pair("accent", ("background", "boost"), TEXT, "Spawn agent, Start captain"),
    Pair("accent", ("surface",), MARK, "every dialog's border"),
    Pair("text", ("background", "boost"), TEXT, "a group's header, a focused account row"),
    Pair("text", ("surface", "block-hover-background"), TEXT, "a list row under the mouse"),
    Pair("footer-key-foreground", ("footer-background",), TEXT, "the footer's keys"),
    Pair("secondary", ("background",), MARK, "the board's feed border"),
    Pair("text", ("background", "secondary@30"), TEXT, "a marked project card's header"),
    Pair("success", ("background",), TEXT, "the remote chip, a done step's border"),
    Pair("success", ("surface",), TEXT, "a success line in a dialog"),
    Pair("warning", ("background",), TEXT, "notices, the instances chip when they differ"),
    Pair("warning", ("surface",), TEXT, "a warning in a dialog"),
    Pair("error", ("background",), TEXT, "errors on a page"),
    Pair("error", ("surface",), TEXT, "an error in a dialog, a dialog's error border"),
    Pair("text", ("error",), TEXT, "an armed Remove or Stop button"),
    Pair("button-color-foreground", ("success",), TEXT, "a success button's label"),
    Pair("button-color-foreground", ("warning",), TEXT, "a warning button's label"),
    Pair("button-color-foreground", ("error",), TEXT, "an error button's label"),
    Pair("foreground", ("background",), TEXT, "the page's text"),
    Pair("foreground", ("surface",), TEXT, "a dialog's text"),
    Pair("foreground", ("panel",), TEXT, "an odd project card, the footer"),
    Pair("text-muted", ("background",), TEXT, "dim lines"),
    Pair("text-muted", ("surface",), TEXT, "a dialog's hint"),
    Pair("text-muted", ("panel",), TEXT, "an odd card's subtitle"),
)
"""Each colour ``rlx`` sets, on each ground it is drawn on (the CSS under ``src/aisquare``)."""


def variables(theme: Any = RLX_THEME) -> dict[str, str]:
    """A theme's variables as Textual builds them: its colour system, then its own variables."""
    return {**theme.to_color_system().generate(), **theme.variables}


def _layer(under: Color, name: str, values: dict[str, str]) -> Color:
    """``name`` painted over ``under``: a colour with alpha blends, ``auto N%`` contrasts."""
    variable, _, percent = name.partition("@")
    value = values[variable]
    if value.startswith("auto"):
        alpha = float(value.split()[1].rstrip("%")) / 100 if " " in value else 1.0
        return under + under.get_contrast_text(alpha)
    colour = Color.parse(value)
    if percent:
        colour = colour.with_alpha(colour.a * int(percent) / 100)
    return under + colour


@dataclass(frozen=True)
class Row:
    pair: Pair
    ink: Color
    ground: Color
    ratio: float


def table(theme: Any = RLX_THEME) -> list[Row]:
    """Every pair of :data:`PAIRS`, resolved and measured under ``theme``."""
    values = variables(theme)
    rows = []
    for pair in PAIRS:
        ground = Color.parse(values["background"])
        for layer in pair.ground:
            ground = _layer(ground, layer, values)
        ink = _layer(ground, pair.ink, values)
        rows.append(Row(pair, ink, ground, contrast(ink.rgb, ground.rgb)))
    return rows


def markdown_table(theme: Any = RLX_THEME) -> str:
    """The PR's table: ``python -c 'from tests.test_ui_rlx_theme import markdown_table; ...'``."""
    lines = [
        "| ink | on | ink | ground | ratio | floor | where |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in table(theme):
        lines.append(
            f"| `{row.pair.ink}` | `{' + '.join(row.pair.ground)}` | {row.ink.hex} "
            f"| {row.ground.hex} | {row.ratio:.2f}:1 | {row.pair.floor}:1 | {row.pair.where} |"
        )
    return "\n".join(lines)


def test_rlx_lifts_rows_and_hovers_as_textual_dark_does() -> None:
    """Textual derives ``$boost`` (and the hover from it) only for a panel it derives: with
    rlx's own panel both were transparent, and Spawn agent's lift and a list's hover were gone."""
    dark = variables(BUILTIN_THEMES["textual-dark"])
    for name in ("boost", "block-hover-background"):
        assert Color.parse(variables()[name]) == Color.parse(dark[name]), name
        assert Color.parse(variables()[name]).a > 0, name


def test_every_colour_rlx_sets_is_measured_on_a_ground() -> None:
    """The table covers the palette: a colour added without a row fails here, not on screen."""
    measured = {pair.ink for pair in PAIRS} | {
        layer.partition("@")[0] for pair in PAIRS for layer in pair.ground
    }
    fields = {
        name
        for name in (
            "primary",
            "secondary",
            "accent",
            "warning",
            "error",
            "success",
            "foreground",
            "background",
            "surface",
            "panel",
        )
        if getattr(RLX_THEME, name) is not None
    }
    assert fields | set(RLX_THEME.variables) <= measured


@pytest.mark.parametrize("row", table(), ids=lambda row: f"{row.pair.ink}-on-{row.pair.ground}")
def test_each_colour_reads_on_the_ground_it_is_drawn_on(row: Row) -> None:
    assert row.ratio >= row.pair.floor, (
        f"{row.pair.ink} {row.ink.hex} on {row.pair.ground} {row.ground.hex}: "
        f"{row.ratio:.2f}:1 < {row.pair.floor}:1 ({row.pair.where})"
    )


def test_the_brand_blue_on_rlxs_ground_is_text_grade() -> None:
    """The task's own figures: 4.57:1 on #0a0b0d, 4.88:1 on black; rlx's ground sits between."""
    blue = Color.parse(RLX_BLUE).rgb
    assert round(contrast(blue, (10, 11, 13)), 2) == 4.57
    assert round(contrast(blue, (0, 0, 0)), 2) == 4.88
    ground = Color.parse(variables()["background"]).rgb
    assert TEXT <= contrast(blue, ground) <= 4.88


# --- the mark's variable, under every theme --------------------------------------------------

ALL_THEMES = sorted({*BUILTIN_THEMES, RLX_THEME.name})


def _expected_mark(theme: str) -> Color:
    return Color.parse(RLX_BLUE if theme == RLX_THEME.name else BUILTIN_THEMES[theme].primary)


def test_every_theme_gives_the_mark_a_colour_in_both_apps(
    tmp_path: Path, script: Script, board_dir: Path
) -> None:
    """``$rlx-mark`` resolves under every theme, the built-ins' to their ``primary``: Textual
    fails a stylesheet that names a variable no theme or default defines."""

    async def fleet(pilot: Pilot[None]) -> dict[str, str]:
        app = fleet_app(pilot)
        seen = {}
        for theme in ALL_THEMES:
            app.theme = theme
            seen[theme] = app.get_css_variables()[MARK_VARIABLE]
        return seen

    for theme, value in drive(fleet).items():
        assert Color.parse(value) == _expected_mark(theme), theme
    for theme, value in _on_the_board(_board_marks).items():
        assert Color.parse(value) == _expected_mark(theme), ("board", theme)


def test_the_default_is_the_themes_primary_and_rlxs_own_value_wins() -> None:
    class Host(App[None]):
        pass

    host = Host()
    host.register_theme(RLX_THEME)
    host.theme = "nord"
    assert theme_variable_defaults(host) == {MARK_VARIABLE: BUILTIN_THEMES["nord"].primary}
    host.theme = "rlx"
    assert theme_variable_defaults(host) == {MARK_VARIABLE: RLX_BLUE}  # its primary, too
    assert variables()[MARK_VARIABLE] == RLX_BLUE


# --- the start theme ---------------------------------------------------------------------------


@pytest.fixture
def board_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """What ``board -w`` reads: a project in the cwd, as ``tests/test_watch.py`` gives it."""
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    add_project(work)
    team_service.activate()
    return work


def _on_the_board(body: Any) -> Any:
    async def run() -> Any:
        app_cls = watch_mod._build_app_class(interval=60.0)
        async with app_cls().run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await settle_page(pilot.app)
            return await body(pilot)

    return asyncio.run(run())


async def _board_marks(pilot: Pilot[None]) -> dict[str, str]:
    seen = {}
    for theme in ALL_THEMES:
        pilot.app.theme = theme
        seen[theme] = pilot.app.get_css_variables()[MARK_VARIABLE]
    return seen


async def _theme(pilot: Pilot[None]) -> str:
    return pilot.app.theme


STARTS = [
    pytest.param(None, "rlx", id="nothing saved: rlx"),
    pytest.param("nord", "nord", id="a saved pick wins"),
    pytest.param("textual-dark", "textual-dark", id="the old default, saved, is kept"),
    pytest.param("a-theme-since-gone", "rlx", id="a saved name no longer offered: rlx"),
]


@pytest.mark.parametrize(("saved", "starts"), STARTS)
def test_the_fleet_ui_starts_in_rlx_unless_a_pick_it_can_show_is_saved(
    saved: str | None, starts: str, tmp_path: Path, script: Script, isolated_home: Path
) -> None:
    seed(tmp_path, ("prj_a", "alpha", None))
    if saved is not None:
        _write_state(isolated_home, {"board_theme": saved})
    assert drive(_theme) == starts
    # The default is not written as a pick: a later default still reaches a home that never
    # chose, and a stale name stays until the user picks again.
    assert _state(isolated_home).get("board_theme") == saved


@pytest.mark.parametrize(("saved", "starts"), STARTS)
def test_the_board_starts_in_rlx_unless_a_pick_it_can_show_is_saved(
    saved: str | None, starts: str, board_dir: Path, isolated_home: Path
) -> None:
    if saved is not None:
        _write_state(isolated_home, {"board_theme": saved})
    assert _on_the_board(_theme) == starts
    assert _state(isolated_home).get("board_theme") == saved


ENVIRONMENT = [
    pytest.param("dracula", None, "dracula", id="TEXTUAL_THEME over rlx"),
    pytest.param("dracula", "nord", "nord", id="a saved pick over TEXTUAL_THEME"),
    pytest.param("dracula", "a-theme-since-gone", "dracula", id="a stale pick: TEXTUAL_THEME"),
    pytest.param("no-such-theme", None, "rlx", id="TEXTUAL_THEME names none: rlx"),
    pytest.param("no-such-theme, gruvbox", None, "gruvbox", id="Textual's list: the first offered"),
]


@pytest.mark.parametrize(("environment", "saved", "starts"), ENVIRONMENT)
def test_textual_theme_still_picks_the_start_theme_below_a_saved_pick(
    environment: str,
    saved: str | None,
    starts: str,
    tmp_path: Path,
    script: Script,
    board_dir: Path,
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user who exported Textual's ``TEXTUAL_THEME`` before rlx was the default keeps it, in
    both apps: saved pick, then ``TEXTUAL_THEME``, then rlx. It is not saved as a pick."""
    seed(tmp_path, ("prj_a", "alpha", None))
    monkeypatch.setenv("TEXTUAL_THEME", environment)
    if saved is not None:
        _write_state(isolated_home, {"board_theme": saved})
    assert (drive(_theme), _on_the_board(_theme)) == (starts, starts)
    assert _state(isolated_home).get("board_theme") == saved


def _listed(app: App[Any]) -> list[str]:
    listing = app.screen.query_one("#themelist", OptionList)
    return [str(listing.get_option_at_index(i).prompt) for i in range(listing.option_count)]


def test_both_pickers_list_rlx_beside_every_built_in_theme(
    tmp_path: Path, script: Script, board_dir: Path
) -> None:
    async def fleet(pilot: Pilot[None]) -> tuple[list[str], str | None]:
        app = fleet_app(pilot)
        await pilot.press("t")
        await settle(app)
        listing = app.screen.query_one("#themelist", OptionList)
        highlighted = listing.highlighted_option
        return _listed(app), None if highlighted is None else highlighted.id

    async def board(pilot: Pilot[None]) -> list[str]:
        await pilot.press("t")
        await settle_page(pilot.app)
        return _listed(pilot.app)

    listed, highlighted = drive(fleet)
    assert listed == sorted({*BUILTIN_THEMES, "rlx"}), listed
    assert highlighted == "rlx", "the picker opens on the theme in use"
    assert _on_the_board(board) == listed


def test_a_pick_of_rlx_is_saved_like_any_other(
    tmp_path: Path, script: Script, isolated_home: Path
) -> None:
    _write_state(isolated_home, {"board_theme": "nord"})

    async def go(pilot: Pilot[None]) -> None:
        fleet_app(pilot).theme = "rlx"  # and out, before the debounce fires

    drive(go)
    assert _state(isolated_home)["board_theme"] == "rlx"


# --- the mark on screen ------------------------------------------------------------------------


def _ink(segment: Segment) -> tuple[Color, Color]:
    style = segment.style
    assert style is not None and style.color is not None and style.bgcolor is not None, segment
    return (
        Color.from_rich_color(style.color),
        Color.from_rich_color(style.bgcolor),
    )


def _title(app: FleetApp) -> list[Segment]:
    title = app.query_one("#fleet-title", Static)
    (strip,) = title.render_lines(Region(0, 0, title.size.width, 1))
    return [segment for segment in strip if segment.text.strip()]


def _heading(app: FleetApp) -> Segment:
    intro = app.query_one(WelcomeView).query_one("#welcome-intro", Static)
    first = intro.render_lines(Region(0, 0, intro.size.width, 1))[0]
    return next(segment for segment in first if segment.text.strip())


Marks = dict[str, tuple[str, Color, Color, str, Color, Color]]
"""Per theme: the title's mark (text, ink, ground), and Welcome's heading the same way."""


async def _focus(app: FleetApp, side: Widget) -> None:
    """Put the keyboard on ``side``: the side without it fades (``Panes > .-unfocused``), and
    a mark is read where it is drawn at full strength."""
    target = side if side.focusable else next(w for w in side.query("*") if w.focusable)
    target.focus()
    await settle(app)


async def _marks(pilot: Pilot[None], themes: list[str]) -> Marks:
    app = fleet_app(pilot)
    app.sidebar.show_live_total(3)
    await settle(app)
    seen: Marks = {}
    for theme in themes:
        app.theme = theme  # what `t`'s picker does on each highlight
        await settle(app)
        await _focus(app, app.sidebar)
        mark = _title(app)[0]
        await _focus(app, app.query_one(WelcomeView))
        heading = _heading(app)
        seen[theme] = (mark.text, *_ink(mark), heading.text, *_ink(heading))
    return seen


def test_the_mark_is_the_brand_blue_under_rlx_and_follows_t_to_textual_dark_and_light(
    tmp_path: Path, script: Script
) -> None:
    """The owner's three looks, as drawn: rlx's #2F74F0, then each theme's own primary as the
    theme changes under the running app, then rlx again."""
    seed(tmp_path, ("prj_a", "alpha", None))
    themes = ["rlx", "textual-dark", "textual-light", "rlx"]

    async def body(pilot: Pilot[None]) -> tuple[Marks, list[str]]:
        seen = await _marks(pilot, themes)
        return seen, [segment.text for segment in _title(fleet_app(pilot))]

    seen, words = drive(body)
    assert "".join(words).strip() == "rlx · 3 live", words
    expected = {"rlx": "#2F74F0", "textual-dark": "#0178D4", "textual-light": "#004578"}
    for theme, (text, ink, _, heading, heading_ink, _) in seen.items():
        assert text == "rlx", (theme, text)
        assert ink.hex == expected[theme], (theme, ink.hex)
        assert heading.startswith("aisquare fleet") or heading.startswith("rlx"), heading
        assert heading_ink.hex == expected[theme], ("Welcome's heading", theme, heading_ink.hex)
    _, rlx_ink, rlx_ground, *_ = seen["rlx"]
    assert contrast(rlx_ink.rgb, rlx_ground.rgb) >= TEXT, rlx_ground.hex


def test_the_count_beside_the_mark_keeps_the_headers_text_colour(
    tmp_path: Path, script: Script
) -> None:
    """Only the name is the mark: ``· 3 live`` stays in the plain weight and the text's colour."""
    seed(tmp_path, ("prj_a", "alpha", None))

    async def body(pilot: Pilot[None]) -> list[tuple[str, Color]]:
        app = fleet_app(pilot)
        app.sidebar.show_live_total(3)
        await settle(app)
        return [(segment.text, _ink(segment)[0]) for segment in _title(app)]

    (mark, mark_ink), *count = drive(body)
    assert mark == "rlx" and mark_ink.hex == RLX_BLUE
    assert count and all(ink.hex != RLX_BLUE for _, ink in count), count


def test_the_mark_takes_every_themes_own_colour(tmp_path: Path, script: Script) -> None:
    """Every theme ``t`` offers, the terminal's own two aside (their colours are ANSI ones)."""
    seed(tmp_path, ("prj_a", "alpha", None))
    themes = [theme for theme in ALL_THEMES if theme not in TERMINALS]
    seen = drive(lambda pilot: _marks(pilot, themes))
    assert set(seen) == set(themes) and len(themes) >= 20, "a measurement of nothing passes"
    wrong = {
        theme: ink.hex
        for theme, (_, ink, _, _, heading_ink, _) in seen.items()
        if not (ink == heading_ink == _expected_mark(theme))
    }
    assert not wrong, wrong


def test_a_theme_that_sets_the_mark_colours_it_apart_from_its_primary(
    tmp_path: Path, script: Script
) -> None:
    """The mark reads the variable, not ``$primary``: under rlx and the built-ins the two are
    one colour, so only a theme that sets the variable apart tells them apart."""
    seed(tmp_path, ("prj_a", "alpha", None))
    own = Theme(name="own-mark", primary="#0178D4", variables={MARK_VARIABLE: "#E0A030"})

    async def body(pilot: Pilot[None]) -> Marks:
        fleet_app(pilot).register_theme(own)
        return await _marks(pilot, [own.name])

    (text, ink, _, heading, heading_ink, _) = drive(body)[own.name]
    assert (text, ink.hex) == ("rlx", "#E0A030")
    assert heading_ink.hex == "#E0A030", heading


def _cells(app: FleetApp) -> Iterator[str]:
    for (strip,) in app.screen._compositor.render_full_update(simplify=True).strips:
        yield "".join(segment.text for segment in strip)


def test_the_terminal_themes_draw_the_mark_too(tmp_path: Path, script: Script) -> None:
    """ansi-dark and ansi-light set ``primary`` to an ANSI colour: the mark is drawn in it."""
    seed(tmp_path, ("prj_a", "alpha", None))

    async def body(pilot: Pilot[None]) -> dict[str, str]:
        app = fleet_app(pilot)
        seen = {}
        for theme in sorted(TERMINALS):
            app.theme = theme
            await settle(app)
            seen[theme] = app.get_css_variables()[MARK_VARIABLE]
            assert any("rlx" in line for line in _cells(app))
        return seen

    assert drive(body) == {theme: BUILTIN_THEMES[theme].primary for theme in TERMINALS}
