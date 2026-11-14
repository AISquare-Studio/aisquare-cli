"""Each agent's mark reads at 3:1 or more on the surface it is drawn on, theme by theme (R16-C).

The R16-C verify (seq 11914) measured the marks as drawn and found the picker's below 3:1 on
nine built-in themes: the pin then measured each colour against the theme's ``background``,
while the picker draws its rows on the list's own surface, and the page blends a Rich colour
into its background at the theme's text alpha. One colour per agent cannot read on every
theme's surfaces (catppuccin-frappe's picker needs a lighter ink than textual-light's page
allows), so each mark has a colour for a dark theme and one for a light theme
(``AgentMark.on_dark``, ``on_light``), of one hue.

Here every built-in theme but the two ``ansi`` ones (they paint the terminal's own
background, which no test can know) is put on screen, after the page has painted, so the
repaint a theme change brings is the one measured: the Accounts page and the sidebar rows of a
fleet that mixes agents, then the account picker with each agent's row off the highlight.
Every mark cell's ink is measured against the background drawn under it (WCAG's ratio).
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest
from rich.style import Style
from textual.app import App
from textual.theme import BUILTIN_THEMES
from textual.widgets import OptionList

from aisquare.cli.ui.app import FleetApp
from aisquare.cli.ui.attach import AttachTargetScreen
from aisquare.core.orchestrator import team_project
from aisquare.models import FleetAgent, FleetAgentStatus
from aisquare.services import claude_accounts as accounts_service
from aisquare.services import fleet as fleet_service
from tests.test_ui_accounts import NOW, _mixed_frame, _table_usage, open_accounts, painted
from tests.test_ui_target_picker_agents import mixed as picker_frame
from tests.ui_workers import settle_page, settle_until, settle_until_held

#: The themes whose background is the terminal's own: not measured, and the only ones not.
TERMINALS = frozenset({"ansi-dark", "ansi-light"})
THEMES = sorted(set(BUILTIN_THEMES) - TERMINALS)
FLOOR = 3.0
"""WCAG 2's ratio for a graphical object that must be told apart (1.4.11)."""


def test_every_built_in_theme_is_measured_but_the_terminals_own() -> None:
    assert set(BUILTIN_THEMES) >= TERMINALS and len(THEMES) >= 19
    assert {"textual-dark", "textual-light", "catppuccin-frappe", "nord"} <= set(THEMES)


def _luminance(rgb: tuple[int, int, int]) -> float:
    def channel(value: int) -> float:
        share = value / 255
        return share / 12.92 if share <= 0.03928 else ((share + 0.055) / 1.055) ** 2.4

    red, green, blue = rgb
    return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue)


def contrast(one: tuple[int, int, int], other: tuple[int, int, int]) -> float:
    light, dark = sorted((_luminance(one), _luminance(other)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


Seen = list[tuple[str, int, float]]
"""Each mark cell drawn: its glyph, its x and its ink's ratio to the background under it."""


def _rgb(style: Style, attribute: str) -> tuple[int, int, int]:
    colour = getattr(style, attribute)
    assert colour is not None, f"a mark drawn with no {attribute}: {style}"
    triplet = colour.get_truecolor()
    return (triplet.red, triplet.green, triplet.blue)


def marks_drawn(app: App[Any]) -> Seen:
    """Every ``✻`` and ``>_`` on screen in a colour of its own: a row under a highlight bar
    draws its mark in the bar's colours (bold), which is the bar's to read, not the mark's."""
    seen: Seen = []
    for (strip,) in app.screen._compositor.render_full_update(simplify=True).strips:
        cells = [(char, segment.style) for segment in strip for char in segment.text]
        for x, (char, style) in enumerate(cells):
            glyph = "✻" if char == "✻" else ">_" if cells[x : x + 2] and char == ">" else None
            if glyph == ">_" and (x + 1 >= len(cells) or cells[x + 1][0] != "_"):
                glyph = None
            if glyph is None or style is None or style.bold:
                continue
            seen.append((glyph, x, contrast(_rgb(style, "color"), _rgb(style, "bgcolor"))))
    return seen


def _fleet(project: str) -> list[FleetAgentStatus]:
    def row(label: str, agent: str, slot: int) -> FleetAgentStatus:
        return FleetAgentStatus(
            agent=FleetAgent(
                id=f"agt_{label}",
                project_id=project,
                label=label,
                role="coder",
                pane_id="%4",
                cwd=Path("/tmp/proj"),
                created_at=NOW,
                account_slot=slot,
                agent=agent,
            ),
            state="working",
        )

    return [row("coder-claude", "claude-code", 1), row("coder-codex", "codex", 4)]


class PickerHost(App[None]):
    def __init__(self, project: Any) -> None:
        super().__init__()
        self.project = project

    def on_mount(self) -> None:
        self.push_screen(
            AttachTargetScreen(self.project, persona=None, intent="new", accounts=picker_frame)
        )


def _below(seen: Iterable[tuple[str, int, float]], where: str) -> list[str]:
    return [f"{where} {glyph} at x={x}: {ratio:.2f}:1" for glyph, x, ratio in seen if ratio < FLOOR]


@pytest.mark.parametrize("theme", THEMES)
def test_each_mark_reads_at_three_to_one_on_the_surface_it_is_drawn_on(
    theme: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(accounts_service, "usage", lambda account, **_: _table_usage(account))
    monkeypatch.setattr(fleet_service, "list_agents", lambda project, **_: [])
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: f"/usr/bin/{name}")
    project = team_project(tmp_path)

    async def page_and_sidebar() -> tuple[Seen, int]:
        app = FleetApp(refresh_seconds=3600, doctor=lambda: [], accounts=_mixed_frame)
        async with app.run_test(size=(200, 50)) as pilot:
            await settle_page(app)
            view = await open_accounts(pilot)
            await settle_until(app, lambda: painted(view, 1, 2, 4))
            app.sidebar.show_projects([project], {project.id: _fleet(project.id)})
            await settle_page(app)
            app.theme = theme  # after the paint: the repaint a theme change brings is measured
            await settle_page(app)
            return marks_drawn(app), app.sidebar.outer_size.width

    async def picker() -> tuple[Seen, Seen]:
        host = PickerHost(project)
        async with host.run_test(size=(120, 40)):
            await settle_page(host)
            host.theme = theme
            listing = host.screen.query_one("#picker-list", OptionList)
            await settle_until_held(
                host, lambda: listing.get_option("account:5") is not None, what="the rows"
            )
            first = marks_drawn(host)  # the Claude row under the bar: the Codex row's mark
            listing.highlighted = listing.get_option_index("account:5")
            await settle_page(host)
            return first, marks_drawn(host)  # and now the Claude row's

    (on_page, sidebar_width), (picker_first, picker_moved) = (
        asyncio.run(page_and_sidebar()),
        asyncio.run(picker()),
    )
    sidebar = [mark for mark in on_page if mark[1] < sidebar_width]
    page = [mark for mark in on_page if mark[1] >= sidebar_width]
    picked = picker_first + picker_moved
    # Every surface drew both agents' marks: a measurement of nothing passes nothing.
    for where, seen in (("page", page), ("sidebar", sidebar), ("picker", picked)):
        assert {glyph for glyph, _, _ in seen} == {"✻", ">_"}, (theme, where, seen)
    below = _below(page, "page") + _below(sidebar, "sidebar") + _below(picked, "picker")
    assert not below, f"{theme}: {below}"
