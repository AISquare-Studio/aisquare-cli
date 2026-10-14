"""The sidebar's second section and the Settings hub it opens.

The human: "I want there to just be 2 sections, 1 the fleet projects one, the other the
"others" type section … within that you can see for now just one option - Settings …
and below that green dot or red dot small with aisquare written next to it based on login
status and claude account headroom status"; "doctor and all is completely removed from
left pane but within settings, there would be links to various things"; and later:
"the line items below it of the red/green dots clicking on that should directly take me
to accounts page but still that line wont be highlighted/in focus, still settings would
be in focus".
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any, TypeVar

import pytest
from textual.pilot import Pilot
from textual.widgets import Button, Static

from aisquare.cli.ui.app import FleetApp
from aisquare.cli.ui.sidebar import (
    SETTINGS_KEY,
    TIMES,
    OthersSection,
    SettingsRow,
    StatusLine,
    headroom_dot,
    status_line_text,
)
from aisquare.cli.ui.views.app_settings import AppSettingsView
from aisquare.cli.ui.views.settings_hub import SettingsHubView
from aisquare.models import ClaudeAccount, HeadroomSummary
from tests.ui_workers import settle_page

T = TypeVar("T")
SIZE = (140, 40)
GOOD = HeadroomSummary(level="good", percent_left=80.0, usable=3)


def _drive(
    fn: Callable[[Pilot[None]], Awaitable[T]],
    *,
    headroom: Callable[[Sequence[ClaudeAccount] | None], HeadroomSummary] | None = None,
    refresh_seconds: float = 3600,
) -> T:
    """``fn`` against a ``FleetApp`` with no accounts reader and a scripted headroom."""

    async def run() -> T:
        app = FleetApp(
            refresh_seconds=refresh_seconds,
            doctor=lambda: [],
            accounts=None,
            headroom=headroom or (lambda accounts: GOOD),
            headroom_seconds=3600,
        )
        async with app.run_test(size=SIZE) as pilot:
            await settle_page(app)
            return await fn(pilot)

    return asyncio.run(run())


def _app(pilot: Pilot[None]) -> FleetApp:
    app = pilot.app
    assert isinstance(app, FleetApp)
    return app


def _shown(widget: Static) -> str:
    plain = getattr(widget.visual, "plain", None)
    assert isinstance(plain, str)
    return plain


# --- the sidebar ------------------------------------------------------------------------


def test_the_sidebar_has_the_projects_then_settings_and_its_status_and_nothing_else() -> None:
    async def go(pilot: Pilot[None]) -> tuple[list[str], list[str], str, bool]:
        app = _app(pilot)
        sidebar = app.sidebar
        sections = [child.id or type(child).__name__ for child in sidebar.children]
        others = app.query_one(OthersSection)
        inside = [type(child).__name__ for child in others.children]
        return (
            sections,
            inside,
            _shown(app.query_one(SettingsRow)),
            app.query_one(StatusLine).can_focus,
        )

    sections, inside, settings, focusable = _drive(go)
    assert sections == ["fleet-header", "projects-notice", "projects", "others"]
    assert inside == ["SettingsRow", "StatusLine"]
    assert settings == "⚙︎ Settings"  # ⚙ in its text (monotone) presentation
    assert not focusable


def test_settings_opens_the_hub_and_each_link_opens_its_page_under_settings() -> None:
    async def go(pilot: Pilot[None]) -> list[tuple[str | None, str | None]]:
        app = _app(pilot)
        seen: list[tuple[str | None, str | None]] = []
        await pilot.click(app.query_one(SettingsRow))
        await settle_page(app)
        seen.append((app.content.current, app.sidebar.selected_key))
        for link in ("#hub-app-settings", "#hub-accounts", "#hub-doctor"):
            await pilot.click(app.query_one(SettingsRow))
            await settle_page(app)
            await pilot.click(link)
            await settle_page(app)
            seen.append((app.content.current, app.sidebar.selected_key))
        assert isinstance(app.query_one("#app-settings"), AppSettingsView)
        return seen

    assert _drive(go) == [
        ("settings-hub", SETTINGS_KEY),
        ("app-settings", SETTINGS_KEY),
        ("accounts", SETTINGS_KEY),
        ("doctor", SETTINGS_KEY),
    ]


def test_the_hubs_doctor_link_runs_the_checks() -> None:
    runs: list[int] = []

    async def go(pilot: Pilot[None]) -> tuple[int, int]:
        app = _app(pilot)
        app._doctor = lambda: runs.append(1) or []  # type: ignore[func-returns-value]
        before = len(runs)
        await pilot.click(app.query_one(SettingsRow))
        await settle_page(app)
        await pilot.click("#hub-doctor")
        await settle_page(app)
        return before, len(runs)

    before, after = _drive(go)
    assert after == before + 1


def test_the_status_line_opens_accounts_and_settings_stays_the_highlighted_row() -> None:
    async def go(pilot: Pilot[None]) -> tuple[str | None, str | None, bool, bool]:
        app = _app(pilot)
        await pilot.click(app.query_one(StatusLine))
        await settle_page(app)
        settings = app.query_one(SettingsRow)
        line = app.query_one(StatusLine)
        return (
            app.content.current,
            app.sidebar.selected_key,
            settings.has_class("selected"),
            app.focused is line or line.has_class("selected"),
        )

    current, selected, highlighted, line_lit = _drive(go)
    assert current == "accounts"
    assert selected == SETTINGS_KEY and highlighted
    assert not line_lit


def test_the_keyboard_reaches_settings_and_walks_the_hubs_links_but_never_the_status_line() -> None:
    async def go(pilot: Pilot[None]) -> tuple[list[str], str | None, str | None, str | None]:
        app = _app(pilot)
        app.sidebar.focus()
        await pilot.pause()
        keys = [row.selection_key for row in app.sidebar._rows()]
        for _ in range(len(keys)):
            await pilot.press("down")  # to the last row: Settings
        await pilot.press("enter")
        await settle_page(app)
        first = app.focused.id if app.focused is not None else None
        await pilot.press("down")
        second = app.focused.id if app.focused is not None else None
        await pilot.press("enter")
        await settle_page(app)
        return keys, first, second, app.content.current

    keys, first, second, current = _drive(go)
    assert keys[-1] == SETTINGS_KEY and "" not in keys  # the status line is no row
    assert first == "hub-app-settings"  # opened from the keyboard: the first link has it
    assert second == "hub-accounts"
    assert current == "accounts"


def test_the_hub_is_titled_settings_and_says_what_each_link_opens() -> None:
    async def go(pilot: Pilot[None]) -> tuple[str, list[str], list[str]]:
        app = _app(pilot)
        await pilot.click(app.query_one(SettingsRow))
        await settle_page(app)
        hub = app.query_one(SettingsHubView)
        return (
            _shown(hub.query_one("#settings-title", Static)),
            [str(button.label) for button in hub.query(Button).results(Button)],
            [_shown(about) for about in hub.query(".hub-about").results(Static)],
        )

    title, links, about = _drive(go)
    assert title.endswith("Settings")
    assert links == ["App settings", "Accounts", "Doctor"]
    assert len(about) == 3 and all(about)


# --- the status line --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("summary", "dot", "colour"),
    [
        (HeadroomSummary(level="good", percent_left=80.0, usable=3), "●", "green"),
        (HeadroomSummary(level="fair", percent_left=40.0, usable=2), "●", "yellow"),
        (HeadroomSummary(level="low", percent_left=10.0, usable=2), "●", "dark_orange"),
        (HeadroomSummary(level="critical", percent_left=2.0, usable=1), "●", "red"),
        (HeadroomSummary(level="empty", percent_left=0.0, usable=2), "💀", ""),
        # Offline, a Keychain login, no usable account: nothing READ — grey, not a skull.
        (HeadroomSummary(level="empty"), "●", "grey50"),
        (HeadroomSummary(level="unknown"), "●", "grey50"),
        (None, "●", "grey50"),
    ],
    ids=["good", "fair", "low", "critical", "empty", "unread", "unknown", "before-the-first"],
)
def test_the_claude_dot_follows_the_headroom_level(
    summary: HeadroomSummary | None, dot: str, colour: str
) -> None:
    assert headroom_dot(summary) == (dot, colour)
    text = status_line_text(True, summary)
    assert text.plain.split("  ")[1].startswith(dot)
    if summary is not None:
        assert f"{summary.usable}{TIMES} claude" in text.plain


@pytest.mark.parametrize(
    ("aisquare", "colour"), [(True, "green"), (False, "red"), (None, "grey50")]
)
def test_the_aisquare_dot_is_the_sign_in(aisquare: bool | None, colour: str) -> None:
    text = status_line_text(aisquare, GOOD)
    assert text.plain.startswith("● aisquare")
    first = text.spans[0]
    assert text.plain[first.start : first.end] == "● " and str(first.style) == colour


def test_a_login_due_puts_a_warning_on_the_line() -> None:
    due = HeadroomSummary(level="fair", percent_left=30.0, usable=1, needs_login=1)
    assert status_line_text(True, due).plain == f"● aisquare  ● 1{TIMES} claude ⚠"
    assert "⚠" not in status_line_text(True, GOOD).plain


def test_the_line_paints_the_injected_headroom() -> None:
    critical = HeadroomSummary(level="critical", percent_left=3.0, usable=2, needs_login=1)

    async def go(pilot: Pilot[None]) -> str:
        return _shown(_app(pilot).query_one(StatusLine))

    assert _drive(go, headroom=lambda accounts: critical) == f"● aisquare  ● 2{TIMES} claude ⚠"


def test_the_headroom_is_read_at_start_and_never_on_the_refresh_tick() -> None:
    """The usage cache answers it, at most one network read per account per five minutes —
    and the two-second tick never asks: ticks come and go and the reader is not called."""
    calls: list[datetime] = []

    def reader(accounts: Sequence[ClaudeAccount] | None) -> HeadroomSummary:
        calls.append(datetime.now(UTC))
        return GOOD

    async def go(pilot: Pilot[None]) -> tuple[int, int]:
        app = _app(pilot)
        at_start = len(calls)
        for _ in range(5):
            app.refresh_data()  # what the tick runs
        await asyncio.sleep(0.3)  # and the real tick, at 50 ms, a few times over
        await settle_page(app)
        return at_start, len(calls)

    at_start, after = _drive(go, headroom=reader, refresh_seconds=0.05)
    assert at_start == 1
    assert after == 1


def test_a_page_change_to_the_accounts_reads_the_headroom_again() -> None:
    """``AccountsChanged`` (a slot added, removed or signed in) re-reads the dot at once."""
    from aisquare.cli.ui.views.accounts import AccountsChanged

    calls: list[Any] = []

    async def go(pilot: Pilot[None]) -> int:
        app = _app(pilot)
        before = len(calls)
        app.post_message(AccountsChanged())
        await settle_page(app)
        return len(calls) - before

    assert _drive(go, headroom=lambda accounts: calls.append(accounts) or GOOD) == 1  # type: ignore[func-returns-value]
