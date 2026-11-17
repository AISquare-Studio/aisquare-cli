"""RLX R6 (#139): under rlx, the text of every widget state reads at 4.5:1, as Textual draws it.

The palette table (``tests/test_ui_rlx_theme.py``) measures the colours rlx sets. It missed
what Textual derives from them at paint time: a primary button's fill is darkened on hover
(``-darken-2``) and on a press (a 30% tint of the background), and the near-black label on it
fell to 3.06:1 and 2.92:1 (rlx-release manager, measured from #508's screenshot). So here
the cells are measured as composited, under the real ``FleetApp`` with its CSS:

- each button variant the app uses (default, primary, success, warning, error) at rest,
  focused, hovered, hovered with focus, pressed, and disabled;
- the tabs (inactive, active, and the active one with focus), a list's highlighted row with
  focus and without, an input's placeholder with focus and without;
- the footer's keys, and a footer key under the mouse.

Each is drawn on a page's ground (``$background``) and a dialog's (``$surface``). Every cell
with a character is measured, its ink against its own background (WCAG's ratio).

Out of scope: the side that does not hold the keyboard fades to 82% (``Panes > .-unfocused``)
under every theme, as a cue. Its text is measured here with the keyboard on its side.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from rich.style import Style
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.pilot import Pilot
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Button, Footer, Input, OptionList, Static, Tabs

from aisquare.cli.ui.app import FleetApp
from tests import test_ui_shell as ui_suite
from tests.test_ui_agent_mark_contrast import contrast
from tests.test_ui_shell import Script, drive, fleet_app, seed, settle

no_real_tmux = ui_suite.no_real_tmux
script = ui_suite.script

TEXT = 4.5
VARIANTS = ("default", "primary", "success", "warning", "error")
GROUNDS = ("background", "surface")


class Gallery(Screen[None]):
    """One of each widget the floor covers, on ``ground``."""

    def __init__(self, ground: str) -> None:
        super().__init__()
        self.ground = ground

    def compose(self) -> ComposeResult:
        with Vertical(id="gallery"):
            yield Static("rest", id="rest")  # somewhere for the mouse to be
            for variant in VARIANTS:
                yield Button(f"{variant} label", variant=variant, id=f"button-{variant}")  # type: ignore[arg-type]
            yield Tabs("Manager", "Board", "Settings", id="tabs")
            yield OptionList("first row", "second row", "third row", id="rows")
            yield Input(placeholder="or type a folder: ~/Code/your-repo", id="field")

    def on_mount(self) -> None:
        self.query_one("#gallery").styles.background = self.app.theme_variables[self.ground]
        self.query_one("#rows", OptionList).highlighted = 1


def _rgb(style: Style, attribute: str) -> tuple[int, int, int]:
    colour = getattr(style, attribute)
    assert colour is not None, f"a cell drawn with no {attribute}: {style}"
    triplet = colour.get_truecolor()
    return (triplet.red, triplet.green, triplet.blue)


def cells(app: FleetApp, widget: Widget) -> list[tuple[str, float]]:
    """Every character drawn in ``widget``'s content, with its ink's ratio to its ground."""
    region = widget.content_region
    strips = app.screen._compositor.render_full_update(simplify=True).strips
    seen = []
    for y in range(region.y, region.bottom):
        (strip,) = strips[y]
        x = 0
        for segment in strip:
            for char in segment.text:
                if region.x <= x < region.right and char.strip() and segment.style:
                    ink, ground = _rgb(segment.style, "color"), _rgb(segment.style, "bgcolor")
                    seen.append((char, contrast(ink, ground)))
                x += 1
    assert seen, f"nothing drawn in {widget!r} at {region}: a measurement of nothing passes"
    return seen


def _footer_key(app: FleetApp) -> Widget:
    footer = app.screen.query_one(Footer)
    return next(
        key
        for key in footer.query("FooterKey")
        if key.region.area and not key.has_class("-grouped")
    )


@dataclass(frozen=True)
class Reading:
    widget: str
    state: str
    ground: str
    worst: float


async def _state(pilot: Pilot[None], set_up: Callable[[], Any], hover: Widget | None) -> None:
    app = fleet_app(pilot)
    app.screen.set_focus(None)
    set_up()
    rest = app.screen.query("#rest") or app.screen.query("#fleet-title")
    await pilot.hover(hover if hover is not None else rest.first())
    await settle(app)


async def _readings(pilot: Pilot[None], ground: str) -> list[Reading]:
    app = fleet_app(pilot)
    assert app.theme == "rlx"
    out = []

    def read(widget: Widget, name: str, state: str) -> None:
        out.append(Reading(name, state, ground, min(r for _, r in cells(app, widget))))

    gallery = Gallery(ground)
    await app.push_screen(gallery)
    await settle(app)
    for variant in VARIANTS:
        button = gallery.query_one(f"#button-{variant}", Button)
        name = f"button {variant}"
        for state, set_up, hover in (
            ("at rest", lambda: None, None),
            ("focused", button.focus, None),
            ("hovered", lambda: None, button),
            ("hovered and focused", button.focus, button),
            ("pressed", lambda b=button: (b.focus(), b.add_class("-active")), button),
        ):
            await _state(pilot, set_up, hover)
            read(button, name, state)
            button.remove_class("-active")
        button.disabled = True
        await _state(pilot, lambda: None, None)
        read(button, name, "disabled")
        await _state(pilot, lambda: None, button)
        read(button, name, "disabled, hovered")
        button.disabled = False
    tabs = gallery.query_one(Tabs)
    await _state(pilot, lambda: None, None)
    for tab in tabs.query("Tab"):
        read(tab, "tab", "active" if tab.has_class("-active") else "inactive")
    await _state(pilot, tabs.focus, None)
    read(tabs.active_tab or tabs, "tab", "active, focused")
    rows = gallery.query_one("#rows", OptionList)
    await _state(pilot, lambda: None, None)
    read(rows, "list rows", "highlighted row, list blurred")
    await _state(pilot, rows.focus, None)
    read(rows, "list rows", "highlighted row, list focused")
    field = gallery.query_one("#field", Input)
    await _state(pilot, lambda: None, None)
    read(field, "input placeholder", "blurred")
    await _state(pilot, field.focus, None)
    read(field, "input placeholder", "focused")
    app.pop_screen()
    await settle(app)
    await _state(pilot, lambda: None, None)
    # The footer rebuilds its keys when the focus moves: each is found once its state is set.
    read(_footer_key(app), "footer key", "at rest")
    await _state(pilot, lambda: None, _footer_key(app))
    read(_footer_key(app), "footer key", "hovered")
    return out


def readings(tmp_path: Path, ground: str) -> list[Reading]:
    seed(tmp_path, ("prj_a", "alpha", None))
    return drive(lambda pilot: _readings(pilot, ground), size=(120, 60))


@pytest.mark.parametrize("ground", GROUNDS)
def test_every_widget_state_reads_at_text_contrast_under_rlx(
    ground: str, tmp_path: Path, script: Script
) -> None:
    seen = readings(tmp_path, ground)
    for reading in seen:  # the PR's table, with -s
        print(f"| {reading.widget} | {reading.state} | {ground} | {reading.worst:.2f}:1 |")
    states = {(reading.widget, reading.state) for reading in seen}
    assert len(states) >= 5 * 7 + 6 and all(
        (f"button {variant}", "pressed") in states for variant in VARIANTS
    ), "a state not measured passes nothing"
    below = [f"{r.widget}, {r.state}: {r.worst:.2f}:1" for r in seen if r.worst < TEXT]
    assert not below, f"on {ground}:\n" + "\n".join(below)


# --- the faded side, and no size changes -------------------------------------------------------

FADED_FLOOR = {"primary": TEXT, "success": TEXT, "warning": TEXT, "error": 3.0}
"""The manager's ruling on #508's screenshot: in the side without the keyboard, the blue and
coral buttons take the light text colour, and the fade leaves them at 4.56:1 and 3.54:1; the
coral can do no better, so it holds 3:1 (WCAG's floor for a control's marks), the rest 4.5:1."""


async def _probe_buttons(pilot: Pilot[None], *, compact: bool = False) -> list[Button]:
    """One button of each variant, and a disabled one of each, on the content side (Welcome)."""
    app = fleet_app(pilot)
    page = app.query_one("#welcome")
    made = []
    for variant in VARIANTS:
        for disabled in (False, True):
            button = Button(
                f"{variant} label",
                variant=variant,  # type: ignore[arg-type]
                compact=compact,
                disabled=disabled,
                id=f"probe-{variant}-{'off' if disabled else 'on'}-{'c' if compact else 'n'}",
            )
            made.append(button)
    await page.mount_all(made)
    await settle(app)
    return made


def test_the_faded_sides_buttons_read_as_ruled(tmp_path: Path, script: Script) -> None:
    """At launch the keyboard is on the sidebar, so *Start manager* is drawn on the faded side:
    there its label read 2.61:1 (#508's screenshot). Measured faded, at rest and under the mouse."""
    seed(tmp_path, ("prj_a", "alpha", None))

    async def body(pilot: Pilot[None]) -> dict[tuple[str, str], float]:
        app = fleet_app(pilot)
        buttons = await _probe_buttons(pilot)
        app.sidebar.focus()
        await settle(app)
        seen = {}
        for button in buttons:
            if button.disabled or button.variant == "default":
                continue
            button.scroll_visible(animate=False)
            await pilot.hover(app.query_one("#fleet-title"))
            await settle(app)
            assert app.content.has_class("-unfocused"), "the content side is not faded"
            seen[(str(button.variant), "faded")] = min(r for _, r in cells(app, button))
            await pilot.hover(button)
            await settle(app)
            seen[(str(button.variant), "faded, hovered")] = min(r for _, r in cells(app, button))
        return seen

    seen = drive(body, size=(120, 60))
    assert len(seen) == 8, seen
    below = {key: f"{ratio:.2f}" for key, ratio in seen.items() if ratio < FADED_FLOOR[key[0]]}
    assert not below, below
    assert seen[("primary", "faded")] >= 4.5 and seen[("error", "faded")] >= 3.0


@pytest.mark.parametrize("compact", [False, True], ids=["full", "compact"])
def test_rlx_changes_no_buttons_size_in_any_state(
    compact: bool, tmp_path: Path, script: Script
) -> None:
    """Colours only (the task's rule 5). A border in rlx's disabled rule gave a compact disabled
    button two rows, and the Accounts table's last button left the screen (the full suite on
    c5d60fb1a). Every variant, enabled and disabled, on the focused side and the faded one, is
    the size it is under textual-dark."""
    seed(tmp_path, ("prj_a", "alpha", None))

    async def body(pilot: Pilot[None]) -> dict[str, dict[str, tuple[int, int]]]:
        app = fleet_app(pilot)
        buttons = await _probe_buttons(pilot, compact=compact)
        sizes: dict[str, dict[str, tuple[int, int]]] = {}
        for theme in ("textual-dark", "rlx"):
            app.theme = theme
            for side in ("focused", "faded"):
                if side == "focused":
                    buttons[0].focus()
                else:
                    app.sidebar.focus()
                await settle(app)
                for button in buttons:
                    key = f"{button.id} {side}"
                    size = button.outer_size
                    sizes.setdefault(key, {})[theme] = (size.width, size.height)
        return sizes

    sizes = drive(body, size=(120, 60))
    changed = {key: by for key, by in sizes.items() if by["rlx"] != by["textual-dark"]}
    assert len(sizes) == 20 and not changed, changed


def test_a_buttons_label_follows_the_fade_both_ways(tmp_path: Path, script: Script) -> None:
    """The fade restyles its side alone (``Panes._fade``), so a rule keyed to it reached a
    button only when something else restyled it: the light label stayed on the full blue fill
    after focus came back, at 3.84:1 (the screenshot of f691592b1). Buttons put on screen with
    the keyboard on their side, then the fade on, off and on again: each time they read."""
    seed(tmp_path, ("prj_a", "alpha", None))

    async def body(pilot: Pilot[None]) -> list[tuple[str, str, float, bool]]:
        app = fleet_app(pilot)
        buttons = [b for b in await _probe_buttons(pilot) if not b.disabled]
        focus_on_content = buttons[0]
        seen = []
        for side in ("content", "faded", "content", "faded", "content"):
            (focus_on_content if side == "content" else app.sidebar).focus()
            await pilot.hover(app.query_one("#fleet-title"))
            await settle(app)
            assert app.content.has_class("-unfocused") is (side == "faded")
            for button in buttons:
                if str(button.variant) not in ("primary", "error"):
                    continue
                button.scroll_visible(animate=False)
                await settle(app)
                ratios = cells(app, button)
                style = button.rich_style
                light = (
                    style.color is not None
                    and style.color.triplet is not None
                    and (sum(style.color.triplet) > 3 * 128)
                )
                seen.append((str(button.variant), side, min(r for _, r in ratios), light))
        return seen

    seen = drive(body, size=(120, 60))
    assert len(seen) == 10, seen
    for variant, side, ratio, light in seen:
        assert light is (side == "faded"), (variant, side, "light" if light else "dark")
        assert ratio >= FADED_FLOOR[variant] if side == "faded" else ratio >= TEXT, (
            variant,
            side,
            f"{ratio:.2f}",
        )
