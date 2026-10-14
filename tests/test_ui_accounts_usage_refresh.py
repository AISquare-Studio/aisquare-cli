"""The Accounts page asks for usage at most once every five minutes, and says how old it is.

The human: "in accounts view the usage status is being refreshed too often, make it refresh
max once every 5 mins with a manual refresh usages button somewhere which refreshes all with
on click and next to that refresh button a small last refreshed x mins ago".

The page reads through the shared cache (``cached_usage``), so these tests count what
reaches the usage endpoint — ``accounts_service.usage``, scripted by ``no_network`` — on a
clock the test moves (``accounts_service._now``). The slots carry a real credentials file:
the cache serves a reading only to the token that took it.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from textual.pilot import Pilot
from textual.widgets import Button, Static

from aisquare.cli.ui.views import accounts as accounts_view
from aisquare.cli.ui.views.accounts import AccountsView
from aisquare.core import claude_accounts as core
from aisquare.core.store import store_session
from aisquare.models import AccountsOverview, ClaudeAccount, ClaudeUsage, UsageSample
from aisquare.services import claude_accounts as accounts_service
from tests.test_ui_accounts import (
    NOW,
    _overview,
    _status,
    drive,
    fleet_app,
    line,
    open_accounts,
    settle,
    shown,
)
from tests.test_ui_accounts import no_network as _no_network
from tests.test_ui_accounts import no_real_tmux as _no_real_tmux
from tests.ui_workers import settle_until

#: Re-exported so pytest collects them here: both are autouse in test_ui_accounts, and here.
no_network = _no_network
no_real_tmux = _no_real_tmux


class Clock:
    """What ``accounts_service._now`` answers: moved by the test, read by the cache and the line."""

    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    moved = Clock()
    monkeypatch.setattr(accounts_service, "_now", moved)
    return moved


def _signed_in(slot: int, root: Path, token: str) -> AccountsOverview:
    """A frame with slot ``slot`` signed in, its credentials on disk under ``root``."""
    status = _status(slot, f"slot{slot}@example.com", root=root)
    path = core.credentials_path(status.account)
    path.parent.mkdir(parents=True, exist_ok=True)
    expires = int((NOW + timedelta(days=30)).timestamp() * 1000)
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": token, "expiresAt": expires}}))
    return _overview(status)


def _answering(clock: Clock, percent: float = 40.0) -> Callable[[ClaudeAccount], ClaudeUsage]:
    """The scripted endpoint's answer: ``percent`` used, read at the clock's time."""

    def usage(account: ClaudeAccount) -> ClaudeUsage:
        return ClaudeUsage(
            available=True,
            session_percent=percent,
            session_resets_at=NOW + timedelta(hours=3),
            fetched_at=clock(),
        )

    return usage


def _age(view: AccountsView) -> str:
    return shown(view.query_one("#usage-age", Static))


async def _away_and_back(pilot: Pilot[None]) -> AccountsView:
    """Leave the page for Welcome (``w``), then open it again from the sidebar."""
    app = fleet_app(pilot)
    app.sidebar.focus()
    await pilot.press("w")
    await settle(app)
    return await open_accounts(pilot)


def test_opening_the_page_again_inside_five_minutes_asks_nothing(
    no_network: dict[str, Any], clock: Clock, tmp_path: Path
) -> None:
    no_network["usage"] = _answering(clock)
    frame = _signed_in(2, tmp_path, "tok-two")

    async def go(pilot: Pilot[None]) -> dict[str, object]:
        app = fleet_app(pilot)
        view = await open_accounts(pilot)
        await settle_until(app, lambda: len(no_network["usage_calls"]) >= 1)
        await settle(app)
        seen: dict[str, object] = {"first": _age(view)}
        clock.advance(minutes=2)
        view = await _away_and_back(pilot)  # closed and reopened: served from the cache
        seen["reopened"] = (len(no_network["usage_calls"]), _age(view))
        clock.advance(minutes=2)
        view.refresh_readings()  # the minute tick, four minutes on
        await settle(app)
        seen["ticked"] = (len(no_network["usage_calls"]), _age(view))
        clock.advance(minutes=1, seconds=1)
        view.refresh_readings()  # five minutes and a second on: due
        await settle_until(app, lambda: len(no_network["usage_calls"]) >= 2)
        await settle(app)
        seen["due"] = (len(no_network["usage_calls"]), _age(view))
        seen["row"] = line(view, 2)
        return seen

    seen = drive(go, overview=frame)
    assert seen["first"] == "last refreshed just now"
    assert seen["reopened"] == (1, "last refreshed 2m ago"), "reopening asked nothing"
    assert seen["ticked"] == (1, "last refreshed 4m ago"), "the tick asked nothing either"
    assert seen["due"] == (2, "last refreshed just now")
    assert "40%" in str(seen["row"])


def test_refresh_usage_asks_every_account_now_and_is_off_while_it_runs(
    no_network: dict[str, Any], clock: Clock, tmp_path: Path
) -> None:
    held = threading.Event()
    held.set()
    answer = _answering(clock, 40.0)

    def usage(account: ClaudeAccount) -> ClaudeUsage:
        assert held.wait(10)
        return answer(account)

    no_network["usage"] = usage
    frame = _signed_in(2, tmp_path, "tok-two")

    async def go(pilot: Pilot[None]) -> dict[str, object]:
        app = fleet_app(pilot)
        view = await open_accounts(pilot)
        await settle_until(app, lambda: len(no_network["usage_calls"]) >= 1)
        await settle(app)
        clock.advance(minutes=1)
        view.refresh_readings()  # the minute tick: the line ages, the cache serves
        await settle(app)
        before = _age(view)
        held.clear()
        button = view.query_one("#usage-refresh", Button)
        await pilot.click(button)
        await pilot.pause()
        running = (button.disabled, _age(view))
        for _ in range(250):  # the forced request is out, and held (no settle: it waits)
            if len(no_network["usage_calls"]) >= 2:
                break
            await pilot.pause(0.02)
        view.refresh_readings()  # a tick meanwhile: owed, not a second request beside it
        held.set()
        await settle(app)
        return {
            "before": before,
            "running": running,
            "after": (button.disabled, _age(view)),
            "calls": len(no_network["usage_calls"]),
            "label": str(button.label),
        }

    seen = drive(go, overview=frame)
    assert seen["label"] == "↻ Refresh usage"
    assert seen["before"] == "last refreshed 1m ago"
    assert seen["running"] == (True, "refreshing…"), "off while a refresh runs"
    assert seen["after"] == (False, "last refreshed just now")
    # One forced request, inside the five minutes; the tick's owed read was served.
    assert seen["calls"] == 2


def test_the_line_says_never_until_a_reading_and_the_oldest_one_after(
    no_network: dict[str, Any], clock: Clock, tmp_path: Path
) -> None:
    signed_out = _overview(_status(2, None, root=tmp_path))
    no_network["usage"] = _answering(clock)

    async def go(pilot: Pilot[None]) -> tuple[str, str]:
        app = fleet_app(pilot)
        view = await open_accounts(pilot)
        never = _age(view)
        # Two slots read three minutes apart: the line says the OLDER one's age.
        first = _signed_in(2, tmp_path, "tok-two").accounts
        view.show(_overview(*first))
        await settle_until(app, lambda: len(no_network["usage_calls"]) >= 1)
        await settle(app)
        clock.advance(minutes=3)
        second = _signed_in(3, tmp_path, "tok-three").accounts
        view.show(_overview(*first, *second))
        await settle_until(app, lambda: len(no_network["usage_calls"]) >= 2)
        await settle(app)
        return never, _age(view)

    never, oldest = drive(go, overview=signed_out)
    assert never == "last refreshed never"
    assert oldest == "last refreshed 3m ago"


def test_a_reading_served_from_the_cache_still_says_where_the_window_is_heading(
    no_network: dict[str, Any], clock: Clock, tmp_path: Path
) -> None:
    """The trend is read from the recorded history, whether the reading was fetched or
    served: twenty minutes at 20 % then 40 % is 60 % an hour, an hour to the limit."""
    with store_session() as store:
        store.add_usage_sample(
            UsageSample(
                slot=2,
                fetched_at=NOW - timedelta(minutes=20),
                session_percent=20.0,
                session_resets_at=NOW + timedelta(hours=3),
            )
        )
    no_network["usage"] = _answering(clock, 40.0)
    frame = _signed_in(2, tmp_path, "tok-two")

    async def go(pilot: Pilot[None]) -> tuple[str, str, int]:
        app = fleet_app(pilot)
        view = await open_accounts(pilot)
        await settle_until(app, lambda: len(no_network["usage_calls"]) >= 1)
        await settle(app)
        fetched = line(view, 2)
        clock.advance(minutes=1)
        view = await _away_and_back(pilot)
        await settle(app)
        return fetched, line(view, 2), len(no_network["usage_calls"])

    fetched, served, calls = drive(go, overview=frame)
    assert "to the limit" in fetched, fetched
    assert "to the limit" in served and calls == 1, served


def test_the_page_ticks_each_minute_and_asks_at_most_every_five() -> None:
    assert accounts_view.TICK_SECONDS == 60.0
    assert accounts_view.USAGE_SECONDS == accounts_service.USAGE_MAX_AGE_SECONDS == 300.0
