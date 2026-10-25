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

import asyncio
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
    offered,
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


def _signed_in(slot: int, root: Path, token: str, *, expired: bool = False) -> AccountsOverview:
    """A frame with slot ``slot`` signed in, its credentials on disk under ``root`` — their
    token already ``expired``, as ``describe`` then says, when asked."""
    status = _status(
        slot, f"slot{slot}@example.com", root=root, token_state="expired" if expired else "ok"
    )
    path = core.credentials_path(status.account)
    path.parent.mkdir(parents=True, exist_ok=True)
    expires = int((NOW + timedelta(days=-1 if expired else 30)).timestamp() * 1000)
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


def test_a_tick_the_cache_answers_never_says_refreshing(
    no_network: dict[str, Any], clock: Clock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The minute tick's read is the cache's and asks nothing while the readings are fresh,
    yet the line said "refreshing…" and the button went off for it, every minute (review of
    #261, a nit). Held open here, as a busy store holds it: the line and the button stay."""
    no_network["usage"] = _answering(clock)
    frame = _signed_in(2, tmp_path, "tok-two")
    real = accounts_service.cached_usage_with_trends
    hold, entered = threading.Event(), threading.Event()
    hold.set()

    def held(*args: Any, **kwargs: Any) -> Any:
        if not hold.is_set():
            entered.set()
            assert hold.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(accounts_service, "cached_usage_with_trends", held)

    async def go(pilot: Pilot[None]) -> tuple[tuple[bool, str], int]:
        app = fleet_app(pilot)
        view = await open_accounts(pilot)
        await settle_until(app, lambda: len(no_network["usage_calls"]) >= 1)
        await settle(app)
        clock.advance(minutes=1)
        hold.clear()
        view.refresh_readings()  # the minute tick: the cache's read, held open
        assert await asyncio.to_thread(entered.wait, 5), "the tick's read started"
        await pilot.pause()
        during = (view.query_one("#usage-refresh", Button).disabled, _age(view))
        hold.set()
        await settle(app)
        return during, len(no_network["usage_calls"])

    during, calls = drive(go, overview=frame)
    assert during == (False, "last refreshed 1m ago")
    assert calls == 1, "the cache answered the tick"


def test_the_status_line_reads_the_cache_again_when_the_page_reads_usage(
    no_network: dict[str, Any], clock: Clock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After *↻ Refresh usage* showed 95 % used, the sidebar's dot stayed green until its
    next minute: only ``AccountsChanged`` re-read it (review of #261, m3). Each usage read
    the page makes now re-reads it, from the cache the page just filled: no request more."""
    from aisquare.cli.ui.app import FleetApp

    asked: list[int] = []
    real = FleetApp.refresh_headroom

    def counting(self: FleetApp) -> None:
        asked.append(len(no_network["usage_calls"]))
        real(self)

    monkeypatch.setattr(FleetApp, "refresh_headroom", counting)
    no_network["usage"] = _answering(clock, 95.0)
    frame = _signed_in(2, tmp_path, "tok-two")

    async def go(pilot: Pilot[None]) -> tuple[int, int, int]:
        app = fleet_app(pilot)
        view = await open_accounts(pilot)
        await settle_until(app, lambda: len(no_network["usage_calls"]) >= 1)
        await settle(app)
        opened = len(asked)
        await pilot.click(view.query_one("#usage-refresh", Button))
        await settle_until(app, lambda: len(no_network["usage_calls"]) >= 2)
        await settle(app)
        return opened, len(asked), len(no_network["usage_calls"])

    opened, after, requests = drive(go, overview=frame)
    assert opened >= 2, "the page's first read asked the dot again"
    assert after > opened, "the refresh's read asked it again"
    assert requests == 2, "the dot's reads were the cache's: the page's two requests only"


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

    def pace(view: AccountsView) -> str:
        # The session cell's tooltip: the pace has no column of its own (#8 on the board).
        return str(view.query_one("#account-cell-session-2").tooltip)

    async def go(pilot: Pilot[None]) -> tuple[str, str, int]:
        app = fleet_app(pilot)
        view = await open_accounts(pilot)
        await settle_until(app, lambda: len(no_network["usage_calls"]) >= 1)
        await settle(app)
        fetched = pace(view)
        clock.advance(minutes=1)
        view = await _away_and_back(pilot)
        await settle(app)
        return fetched, pace(view), len(no_network["usage_calls"])

    fetched, served, calls = drive(go, overview=frame)
    assert "to the limit" in fetched, fetched
    assert "to the limit" in served and calls == 1, served


def test_the_page_ticks_each_minute_and_asks_at_most_every_five() -> None:
    assert accounts_view.TICK_SECONDS == 60.0
    assert accounts_view.USAGE_SECONDS == accounts_service.USAGE_MAX_AGE_SECONDS == 300.0


def _unreachable(account: ClaudeAccount) -> ClaudeUsage:
    """The scripted endpoint, down: what ``usage`` answers when the request cannot be made."""
    return ClaudeUsage(available=False, reason="could not reach the usage endpoint (URLError)")


def test_a_failed_try_after_a_good_read_is_not_called_a_refresh(
    no_network: dict[str, Any], clock: Clock, tmp_path: Path
) -> None:
    """The tester, verifying ff2070e1: with the endpoint unreachable the line read 'last
    refreshed just now' although nothing was read. It ages from the last reading actually
    READ, and says when a later try failed."""
    endpoint: dict[str, Callable[[ClaudeAccount], ClaudeUsage]] = {"up": _answering(clock)}
    no_network["usage"] = lambda account: endpoint["up"](account)
    frame = _signed_in(2, tmp_path, "tok-two")

    async def go(pilot: Pilot[None]) -> dict[str, object]:
        app = fleet_app(pilot)
        view = await open_accounts(pilot)
        await settle_until(app, lambda: len(no_network["usage_calls"]) >= 1)
        await settle(app)
        seen: dict[str, object] = {"read": _age(view)}
        endpoint["up"] = _unreachable
        clock.advance(minutes=6)
        view.refresh_readings()  # due, and the endpoint is down
        await settle_until(app, lambda: len(no_network["usage_calls"]) >= 2)
        await settle(app)
        seen["failed"] = _age(view)
        seen["row"] = line(view, 2)
        clock.advance(minutes=2)
        view.refresh_readings()  # the failure is served, not retried inside five minutes
        await settle(app)
        seen["aged"] = (len(no_network["usage_calls"]), _age(view))
        endpoint["up"] = _answering(clock)
        await pilot.click(view.query_one("#usage-refresh", Button))
        await settle_until(app, lambda: len(no_network["usage_calls"]) >= 3)
        await settle(app)
        seen["back"] = _age(view)
        return seen

    seen = drive(go, overview=frame)
    assert seen["read"] == "last refreshed just now"
    assert seen["failed"] == "last refreshed 6m ago · last try failed just now"
    assert "could not reach the usage endpoint" in str(seen["row"])
    assert seen["aged"] == (2, "last refreshed 8m ago · last try failed 2m ago")
    assert seen["back"] == "last refreshed just now", "a good read clears the failure"


def test_nothing_read_yet_says_never_and_the_failure_beside_the_oldest_good_read(
    no_network: dict[str, Any], clock: Clock, tmp_path: Path
) -> None:
    """Slot 2's endpoint is down from the first try: 'never'. Slot 3 then reads: the line
    ages from slot 3's reading — the only one READ — and still says slot 2's try failed."""
    answer = _answering(clock)
    no_network["usage"] = lambda account: (
        _unreachable(account) if account.slot == 2 else answer(account)
    )
    frame = _signed_in(2, tmp_path, "tok-two")

    async def go(pilot: Pilot[None]) -> tuple[str, str]:
        app = fleet_app(pilot)
        view = await open_accounts(pilot)
        await settle_until(app, lambda: len(no_network["usage_calls"]) >= 1)
        await settle(app)
        never = _age(view)
        clock.advance(minutes=1)
        second = _signed_in(3, tmp_path, "tok-three").accounts
        view.show(_overview(*frame.accounts, *second))
        await settle_until(app, lambda: len(no_network["usage_calls"]) >= 2)
        await settle(app)
        return never, _age(view)

    never, some = drive(go, overview=frame)
    assert never == "last refreshed never · last try failed just now"
    assert some == "last refreshed just now · last try failed 1m ago"


#: What ``usage`` answers for an account that needs a login, or whose token the CLI cannot
#: read — and the state ``describe`` gives its row then.
NEEDS_LOGIN: dict[str, tuple[str, ClaudeUsage]] = {
    "expired": (
        "expired",
        ClaudeUsage(
            available=False,
            reason="the stored token has expired — open a session on this account to refresh it",
            needs_login=True,
        ),
    ),
    "rejected": (
        "ok",
        ClaudeUsage(
            available=False,
            reason="the token was rejected — open a session on this account to refresh it",
            needs_login=True,
        ),
    ),
    "keychain": (
        "unreadable",
        ClaudeUsage(available=False, reason=core.KEYCHAIN_LOCKED),
    ),
}


def test_an_expired_account_beside_a_good_one_adds_nothing_to_the_line_for_ten_minutes(
    no_network: dict[str, Any], clock: Clock, tmp_path: Path
) -> None:
    """The human keeps an account whose token stays expired (slot 4). Its row says so and
    offers Log in; the line beside Refresh usage is about the readings, and stays calm:
    no '· last try failed just now' on every tick. A tick each 61 s, so no reading is ever
    exactly five minutes old: slot 2 is read again on the fifth and the tenth."""
    good = _answering(clock)
    expired_answer = NEEDS_LOGIN["expired"][1]
    no_network["usage"] = lambda account: expired_answer if account.slot == 4 else good(account)
    two = _signed_in(2, tmp_path, "tok-two").accounts
    four = _signed_in(4, tmp_path, "tok-four", expired=True).accounts
    frame = _overview(*two, *four)

    async def go(pilot: Pilot[None]) -> tuple[list[str], int, str, bool]:
        app = fleet_app(pilot)
        view = await open_accounts(pilot)
        await settle_until(app, lambda: no_network["usage_calls"].count(2) >= 1)
        await settle(app)
        said = [_age(view)]
        for _ in range(10):
            clock.advance(seconds=61)
            view.refresh_readings()
            await settle(app)
            said.append(_age(view))
        return said, no_network["usage_calls"].count(2), line(view, 4), offered(view, 4, "login")

    said, read, row, log_in = drive(go, overview=frame)
    ages = ["just now", "1m ago", "2m ago", "3m ago", "4m ago"]
    assert said == [f"last refreshed {age}" for age in [*ages, *ages, "just now"]]
    assert read == 3, "slot 2 read on opening, and again at 5m05s and 10m10s"
    assert "token expired" in row and log_in, "the row says why, and offers Log in"


@pytest.mark.parametrize("kind", sorted(NEEDS_LOGIN))
def test_an_account_that_needs_a_login_counts_in_neither_half_of_the_line(
    kind: str, no_network: dict[str, Any], clock: Clock, tmp_path: Path
) -> None:
    """Slot 4 was read, then needs a login (or its Keychain is locked): its old
    reading no longer sets the age, and its answer is not a failed try. Slot 2's endpoint
    going down still is."""
    token_state, answer = NEEDS_LOGIN[kind]
    good = _answering(clock)
    endpoint: dict[int, Callable[[ClaudeAccount], ClaudeUsage]] = {2: good, 4: good}
    no_network["usage"] = lambda account: endpoint[account.slot](account)
    two = _signed_in(2, tmp_path, "tok-two").accounts
    four = _signed_in(4, tmp_path, "tok-four").accounts
    frame = _overview(*two, *four)

    async def go(pilot: Pilot[None]) -> list[str]:
        app = fleet_app(pilot)
        view = await open_accounts(pilot)
        await settle_until(app, lambda: len(no_network["usage_calls"]) >= 2)
        await settle(app)
        said = [_age(view)]
        clock.advance(minutes=6)
        endpoint[4] = lambda account: answer
        status = four[0].model_copy(update={"token_state": token_state})
        view.show(_overview(*two, status))
        view.refresh_readings()  # both due: slot 2 reads, slot 4 needs a login
        await settle_until(app, lambda: len(no_network["usage_calls"]) >= 4)
        await settle(app)
        said.append(_age(view))
        clock.advance(minutes=6)
        endpoint[2] = _unreachable
        view.refresh_readings()  # both due again: slot 2's endpoint is down
        await settle_until(app, lambda: len(no_network["usage_calls"]) >= 6)
        await settle(app)
        said.append(_age(view))
        return said

    said = drive(go, overview=frame)
    assert said == [
        "last refreshed just now",
        "last refreshed just now",
        "last refreshed 6m ago · last try failed just now",
    ]


def _503(account: ClaudeAccount) -> ClaudeUsage:
    """The scripted endpoint, answering with a server error: nothing said of the token."""
    return ClaudeUsage(available=False, reason="HTTP 503 from the usage endpoint")


def test_a_refresh_that_fails_inside_the_window_keeps_the_figure_and_says_the_try_failed(
    no_network: dict[str, Any], clock: Clock, tmp_path: Path
) -> None:
    """The tester's repro (reopen of the m4/m5 fix): 40 % at T0, the endpoint at 503, ↻
    Refresh usage a minute later put '503' on the row; the next tick put 40 % back and
    dropped '· last try failed'; the row went 40 % → 503 → 40 % → 503. The row keeps its
    figure while the figure is in the window, and the line says the try failed throughout.
    Once the figure is out of the window, the failure is the newest word."""
    endpoint: dict[str, Callable[[ClaudeAccount], ClaudeUsage]] = {"up": _answering(clock)}
    no_network["usage"] = lambda account: endpoint["up"](account)
    frame = _signed_in(2, tmp_path, "tok-two")

    async def go(pilot: Pilot[None]) -> dict[object, tuple[str, str, int]]:
        app = fleet_app(pilot)
        view = await open_accounts(pilot)
        await settle_until(app, lambda: len(no_network["usage_calls"]) >= 1)
        await settle(app)
        seen: dict[object, tuple[str, str, int]] = {}
        endpoint["up"] = _503
        clock.advance(minutes=1)
        await pilot.click(view.query_one("#usage-refresh", Button))
        await settle_until(app, lambda: len(no_network["usage_calls"]) >= 2)
        await settle(app)
        seen["refreshed"] = (_age(view), line(view, 2), len(no_network["usage_calls"]))
        for minute in (2, 3):
            clock.advance(minutes=1)
            view.refresh_readings()  # the tick: served, not retried inside five minutes
            await settle(app)
            seen[minute] = (_age(view), line(view, 2), len(no_network["usage_calls"]))
        clock.advance(minutes=2, seconds=30)  # 5m30s: the figure is out, the failure is not
        view.refresh_readings()
        await settle(app)
        seen["aged"] = (_age(view), line(view, 2), len(no_network["usage_calls"]))
        return seen

    seen = drive(go, overview=frame)
    said, row, _calls = seen["refreshed"]
    assert said == "last refreshed 1m ago · last try failed just now"
    assert "40%" in row and "503" not in row, row
    for minute, age, tried in ((2, "2m ago", "1m ago"), (3, "3m ago", "2m ago")):
        said, row, calls = seen[minute]
        assert said == f"last refreshed {age} · last try failed {tried}", minute
        assert "40%" in row and calls == 2, (minute, row)
    said, row, calls = seen["aged"]
    assert said == "last refreshed 5m ago · last try failed 4m ago"
    assert "503" in row and calls == 2, row
