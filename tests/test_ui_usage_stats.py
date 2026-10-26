"""Settings ▸ Usage & stats: the page, each range, the empty state, and the way in from the hub.

The page is driven in a host app of its own with a loader that reads the real service over a
machine built on disk (``tests/usage_stats_support.py``), so what it draws is what
``aisquare metrics stats`` prints; the way in is driven through the whole fleet UI.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import Any, TypeVar

import pytest
from textual.app import App, ComposeResult
from textual.geometry import Region
from textual.pilot import Pilot
from textual.widgets import Button, Static

from aisquare.cli import usage_format as fmt
from aisquare.cli.ui.app import FleetApp
from aisquare.cli.ui.sidebar import SETTINGS_KEY, SettingsRow
from aisquare.cli.ui.views.usage_stats import (
    DEFAULT_RANGE,
    EMPTY,
    KEYS,
    LOAD_WORKER,
    UsageStatsView,
    chart_text,
)
from aisquare.core.store import store_session
from aisquare.models import USAGE_RANGES, UsageRange, UsageStats
from aisquare.services import usage_stats
from tests.test_ui_settings_hub import GOOD
from tests.ui_workers import settle_page, settle_until
from tests.usage_stats_support import IST, NOW, Machine, prompt, request

T = TypeVar("T")
SIZE = (120, 60)
HELD = "no-such-group"
"""A worker group nothing runs in: ``settle_until`` then waits for the messages alone, not for the
read the test is holding on purpose."""


def _day(offset: int, hour: int = 10, minute: int = 0):  # type: ignore[no-untyped-def]
    return NOW.replace(hour=hour, minute=minute) + timedelta(days=offset)


def _history(m: Machine) -> None:
    """Two accounts, three repositories, two models, a week and more of days."""
    m.account(1, "team")
    m.account(2, "max")
    alpha, beta, gamma = m.repo("alpha"), m.repo("beta"), m.repo("gamma")
    for offset in range(0, 40, 3):
        m.session(
            1 if offset % 2 == 0 else 2,
            (alpha, beta, gamma)[offset % 3],
            f"s{offset}",
            prompt(_day(-offset, 9), cwd=alpha),
            request(_day(-offset, 9, 1), f"a{offset}", cwd=alpha, cache_read=1000 * (offset + 1)),
            request(
                _day(-offset, 14, 30),
                f"b{offset}",
                cwd=alpha,
                model="claude-sonnet-5-5",
                output=900,
            ),
        )
    usage_stats.sweep(tz=IST)
    with store_session() as store:
        store.add_feature_uses(
            {("2026-10-26", "cli.fleet.spawn"): 4, ("2026-10-25", "tui.launch"): 1}
        )


def _painted(view: UsageStatsView, range_: UsageRange) -> bool:
    return view.stats is not None and view.stats.range == range_


def real_loader(calls: list[tuple[UsageRange, bool, bool]] | None = None):  # type: ignore[no-untyped-def]
    """The loader the page is handed: the service's ``stats`` on the stats clock."""

    def load(range_: UsageRange, sweep: bool, progress: Any) -> UsageStats:
        if calls is not None:
            calls.append((range_, sweep, progress is not None))
        return usage_stats.stats(range_, now=NOW, tz=IST)

    return load


class Host(App[None]):
    def __init__(self, loader: Any) -> None:
        super().__init__()
        self.loader = loader

    def compose(self) -> ComposeResult:
        yield UsageStatsView(loader=self.loader, id="usage")


def _drive_page(
    loader: Any,
    fn: Callable[[Pilot[None], UsageStatsView], Awaitable[T]],
    *,
    size: tuple[int, int] = SIZE,
    settle: bool = True,
) -> T:
    async def run() -> T:
        app = Host(loader)
        async with app.run_test(size=size) as pilot:
            if settle:
                await settle_page(app)
            return await fn(pilot, app.query_one(UsageStatsView))

    return asyncio.run(run())


def _text(widget: Static) -> str:
    """What ``widget`` shows: its text where it was handed text, else (a table) its lines as
    laid out."""
    plain = getattr(widget.visual, "plain", None)
    if isinstance(plain, str):
        return plain.rstrip()
    lines = widget.render_lines(Region(0, 0, max(widget.size.width, 1), max(widget.size.height, 1)))
    return "\n".join(line.text.rstrip() for line in lines).rstrip()


def _page(view: UsageStatsView) -> dict[str, str]:
    ids = (
        "title", "status", "empty", "tile-tokens", "tile-io", "tile-input", "tile-cost",
        "tile-sessions", "tile-turns", "tile-days", "tile-repos", "breakdown", "chart", "when",
        "repos", "accounts", "busiest", "models", "features",
    )  # fmt: skip
    return {name: _text(view.query_one(f"#usage-{name}", Static)) for name in ids}


# --- the way in ----------------------------------------------------------------------------


def test_the_hub_has_a_usage_and_stats_link_that_opens_the_page_under_settings(
    machine: Machine,
) -> None:
    _history(machine)

    async def go() -> tuple[str | None, str | None, bool, str]:
        app = FleetApp(
            refresh_seconds=3600,
            doctor=lambda: [],
            accounts=None,
            headroom=lambda accounts: GOOD,
            headroom_seconds=3600,
        )
        async with app.run_test(size=(140, 50)) as pilot:
            await settle_page(app)
            await pilot.click(app.query_one(SettingsRow))
            await settle_page(app)
            await pilot.click("#hub-usage-stats")
            await settle_page(app)
            page = app.query_one("#usage-stats", UsageStatsView)
            return (
                app.content.current,
                app.sidebar.selected_key,
                page.stats is not None and page.stats.turns > 0,
                _text(app.query_one("#usage-tile-tokens", Static)),
            )

    current, selected, painted, tile = asyncio.run(go())
    assert (current, selected) == ("usage-stats", SETTINGS_KEY)
    assert painted, "the page reads on opening, sweeping the transcripts first"
    assert tile.splitlines()[-1] == "tokens" and tile.splitlines()[0] != "…"
    with store_session() as store:  # the page's own read folded the spool into the store
        counted = {name for _day, name, _count in store.feature_uses(None, None)}
    assert {"tui.launch", "tui.view.settings_hub", "tui.view.usage_stats"} <= counted


def _fleet_app() -> FleetApp:
    return FleetApp(
        refresh_seconds=3600,
        doctor=lambda: [],
        accounts=None,
        headroom=lambda accounts: GOOD,
        headroom_seconds=3600,
    )


def test_the_housekeeping_tick_sweeps_when_due_then_every_ten_minutes_and_adds_no_timer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On Windows every pending Textual timer holds a thread of the default executor, and a
    runner has eight: the sweep rides the tick that is there (``test_ui_accounts`` holds the
    line on timers), starting 45 s after start and then every ``SWEEP_EVERY``."""
    swept: list[int] = []

    def fake_sweep(**kwargs: Any) -> usage_stats.SweepReport:
        swept.append(1)
        return usage_stats.SweepReport(sessions_new=1)

    monkeypatch.setattr(usage_stats, "maybe_sweep", fake_sweep)

    async def go() -> tuple[int, int, int, float]:
        app = _fleet_app()
        async with app.run_test(size=(140, 50)):
            await settle_page(app)
            app._housekeeping()
            await settle_page(app)
            too_early = len(swept)
            app._usage_sweep_at = 0.0
            app._housekeeping()
            await settle_page(app)
            due = len(swept)
            app._housekeeping()
            await settle_page(app)
            return too_early, due, len(swept), app._usage_sweep_at - time.monotonic()

    too_early, due, again, next_in = asyncio.run(go())

    assert (too_early, due, again) == (0, 1, 1)
    assert 590 < next_in <= usage_stats.SWEEP_EVERY.total_seconds()


def test_a_sweep_that_recorded_sessions_makes_the_open_page_read_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads: list[UsageRange] = []
    real = usage_stats.stats

    def counted(range_: UsageRange, **kwargs: Any) -> UsageStats:
        reads.append(range_)
        return real(range_, **kwargs)

    monkeypatch.setattr(usage_stats, "stats", counted)
    monkeypatch.setattr(
        usage_stats, "maybe_sweep", lambda **kw: usage_stats.SweepReport(sessions_new=2)
    )

    async def go() -> tuple[int, int]:
        app = _fleet_app()
        async with app.run_test(size=(140, 50)) as pilot:
            await settle_page(app)
            await pilot.click(app.query_one(SettingsRow))
            await settle_page(app)
            await pilot.click("#hub-usage-stats")
            await settle_page(app)
            opened = len(reads)
            app._usage_sweep_at = 0.0
            app._housekeeping()
            await settle_page(app)
            return opened, len(reads)

    opened, after = asyncio.run(go())

    assert opened >= 1 and after == opened + 1


def test_the_page_names_itself_and_has_the_sharing_slot_above_the_range_switcher() -> None:
    async def go(pilot: Pilot[None], view: UsageStatsView) -> tuple[list[str], int, str]:
        order = [child.id or "" for child in view.children]
        slot = view.query_one("#usage-sharing-slot")
        return order, len(slot.children), _text(view.query_one("#usage-title", Static))

    order, slot_children, title = _drive_page(real_loader(), go)

    assert title == "Usage & stats"
    assert order[:4] == ["usage-title", "usage-sharing-slot", "usage-ranges", "usage-status"]
    assert slot_children == 0, "empty until the sharing task fills it; nothing here sends anything"


# --- each range ----------------------------------------------------------------------------------


@pytest.mark.parametrize("range_", USAGE_RANGES)
def test_each_range_renders_its_figures(machine: Machine, range_: UsageRange) -> None:
    _history(machine)
    expected = usage_stats.stats(range_, now=NOW, tz=IST)

    async def go(pilot: Pilot[None], view: UsageStatsView) -> dict[str, str]:
        await pilot.press(KEYS[range_])
        await settle_page(view.app)
        await settle_until(view.app, lambda: view.stats is not None and view.stats.range == range_)
        return {**_page(view), "empty-shown": str(view.query_one("#usage-empty").display)}

    page = _drive_page(real_loader(), go)

    assert page["empty-shown"] == "False" and expected.turns > 0
    assert page["tile-tokens"].splitlines() == [fmt.tokens(expected.tokens.total), "tokens"]
    assert page["tile-io"].splitlines()[0] == fmt.tokens(expected.tokens.input_output)
    assert page["tile-input"].splitlines()[0] == fmt.tokens(expected.tokens.input)
    assert page["tile-cost"].splitlines()[0] == fmt.money(expected.cost_usd)
    assert page["tile-sessions"].splitlines()[0] == f"{expected.sessions:,}"
    assert page["tile-turns"].splitlines()[0] == f"{expected.turns:,}"
    assert page["tile-days"].splitlines()[0] == f"{expected.active_days:,}"
    assert page["tile-repos"].splitlines()[0] == f"{expected.repos.active:,}"
    assert f"cache read {fmt.tokens(expected.tokens.cache_read)}" in page["breakdown"]
    chart_note = "turns per hour of the day" if range_ == "today" else "tokens per day"
    assert chart_note in page["chart"] or (range_ == "lifetime" and "tokens per" in page["chart"])
    assert "█" in page["chart"] or "▁" in page["chart"] or range_ == "today"
    assert "Top repositories" in page["repos"] and str(expected.repos.active) in page["repos"]
    for repo in expected.repos.rows:
        assert repo.name in page["repos"]
    assert "Accounts" in page["accounts"]
    for account in expected.accounts.rows:
        assert account.label in page["accounts"] and account.subscription in page["accounts"]
    assert "★" in page["accounts"] and "plain claude" in page["accounts"]
    for day in expected.busiest_days:
        assert fmt.short_date(day.date) in page["busiest"]
    assert "claude-opus-5-5" in page["models"] and "claude-sonnet-5-5" in page["models"]
    assert "weekday" in page["when"] and "hour" in page["when"]
    if range_ in ("today", "7d", "lifetime"):  # the days the two features were counted on
        assert "cli.fleet.spawn" in page["features"]
    assert page["status"].startswith("Updated ")


def test_the_range_buttons_show_which_range_is_on_and_a_click_switches(machine: Machine) -> None:
    _history(machine)
    calls: list[tuple[UsageRange, bool, bool]] = []

    async def go(
        pilot: Pilot[None], view: UsageStatsView
    ) -> tuple[list[str], list[str], UsageRange]:
        def variants() -> list[str]:
            return [str(view.query_one(f"#usage-range-{r}", Button).variant) for r in USAGE_RANGES]

        before = variants()
        await pilot.click("#usage-range-30d")
        await settle_page(view.app)
        return before, variants(), view.range

    before, after, now_on = _drive_page(real_loader(calls), go)

    assert before == ["default", "primary", "default", "default"]  # 7 days, the default
    assert after == ["default", "default", "primary", "default"]
    assert now_on == "30d" and DEFAULT_RANGE == "7d"
    assert calls[0] == ("7d", True, True)  # opening reads, sweeping first and saying how far
    assert calls[-1] == ("30d", False, False)  # a switch only reads what is recorded


def test_the_keys_are_one_to_four_and_r_reads_again_with_the_sweep(machine: Machine) -> None:
    _history(machine)
    calls: list[tuple[UsageRange, bool, bool]] = []

    async def go(pilot: Pilot[None], view: UsageStatsView) -> None:
        for key in ("1", "2", "3", "4", "r"):
            await pilot.press(key)
            await settle_page(view.app)

    _drive_page(real_loader(calls), go)

    assert [(r, sweep) for r, sweep, _ in calls[1:]] == [
        ("today", False), ("7d", False), ("30d", False), ("lifetime", False), ("lifetime", True),
    ]  # fmt: skip
    assert KEYS == {"today": "1", "7d": "2", "30d": "3", "lifetime": "4"}


def test_an_answer_for_a_range_left_since_is_not_painted(machine: Machine) -> None:
    _history(machine)
    release = threading.Event()
    first = threading.Event()

    def loader(range_: UsageRange, sweep: bool, progress: Any) -> UsageStats:
        if range_ == "7d":
            first.set()
            release.wait(timeout=10)
        return usage_stats.stats(range_, now=NOW, tz=IST)

    async def go(pilot: Pilot[None], view: UsageStatsView) -> UsageRange | None:
        await settle_until(view.app, lambda: first.is_set(), group=HELD)
        await pilot.press("4")  # lifetime, while 7d is still being read
        await settle_until(view.app, lambda: view.stats is not None, group=HELD)
        release.set()
        await settle_page(view.app)
        return view.stats.range if view.stats else None

    assert _drive_page(loader, go, settle=False) == "lifetime"


def test_how_far_the_sweep_has_got_is_said_while_it_reads(machine: Machine) -> None:
    _history(machine)
    release = threading.Event()
    reached = threading.Event()

    def loader(range_: UsageRange, sweep: bool, progress: Any) -> UsageStats:
        if sweep and progress is not None:
            progress(2, 5)
            reached.set()
            release.wait(timeout=10)
        return usage_stats.stats(range_, now=NOW, tz=IST)

    async def go(pilot: Pilot[None], view: UsageStatsView) -> tuple[str, str]:
        await settle_until(view.app, lambda: reached.is_set(), group=HELD)
        await settle_until(
            view.app, lambda: "2/5" in _text(view.query_one("#usage-status", Static)), group=HELD
        )
        reading = _text(view.query_one("#usage-status", Static))
        release.set()
        await settle_page(view.app)
        return reading, _text(view.query_one("#usage-status", Static))

    reading, done = _drive_page(loader, go, settle=False)

    assert reading == "Reading Claude Code's transcripts… 2/5"
    assert done.startswith("Updated ")


def test_the_page_reads_off_the_ui_thread() -> None:
    from textual.worker import get_current_worker

    seen: list[tuple[str, str]] = []

    def loader(range_: UsageRange, sweep: bool, progress: Any) -> UsageStats:
        seen.append((threading.current_thread().name, get_current_worker().name))
        return UsageStats(range=range_, until="2026-10-26")

    async def go(pilot: Pilot[None], view: UsageStatsView) -> None:
        return None

    _drive_page(loader, go)

    assert seen and all(thread != threading.main_thread().name for thread, _ in seen)
    assert {worker for _, worker in seen} == {LOAD_WORKER}


# --- nothing to show, and no answer ---------------------------------------------------


def test_with_nothing_recorded_the_page_says_so_and_how_to_record_it() -> None:
    async def go(pilot: Pilot[None], view: UsageStatsView) -> tuple[str, bool, bool]:
        return (
            _text(view.query_one("#usage-empty", Static)),
            view.query_one("#usage-empty").display,
            view.query_one("#usage-body").display,
        )

    empty, empty_shown, body_shown = _drive_page(real_loader(), go)

    assert empty_shown and not body_shown
    assert "No usage recorded yet." in empty and "aisquare metrics backfill" in empty
    assert EMPTY.splitlines()[0] == "No usage recorded yet."


def test_each_range_with_nothing_in_it_is_the_empty_state_not_a_page_of_zeros(
    machine: Machine,
) -> None:
    machine.account(1, "team")
    repo = machine.repo("old")
    machine.session(
        1, repo, "s", prompt(_day(-20, 9), cwd=repo), request(_day(-20, 9, 1), "m", cwd=repo)
    )  # twenty days ago: in 30 days and lifetime, not in today or 7 days
    usage_stats.sweep(tz=IST)

    async def go(pilot: Pilot[None], view: UsageStatsView) -> dict[str, bool]:
        shown: dict[str, bool] = {}
        for range_ in USAGE_RANGES:
            await pilot.press(KEYS[range_])
            await settle_page(view.app)
            await settle_until(view.app, partial(_painted, view, range_))
            shown[range_] = view.query_one("#usage-body").display
        return shown

    assert _drive_page(real_loader(), go) == {
        "today": False, "7d": False, "30d": True, "lifetime": True,
    }  # fmt: skip


def test_a_read_that_fails_says_why_and_keeps_what_was_shown(machine: Machine) -> None:
    _history(machine)
    fail = threading.Event()

    def loader(range_: UsageRange, sweep: bool, progress: Any) -> UsageStats:
        if fail.is_set():
            raise RuntimeError("the store is locked")
        return usage_stats.stats(range_, now=NOW, tz=IST)

    async def go(pilot: Pilot[None], view: UsageStatsView) -> tuple[str, bool, int]:
        painted = view.stats.turns if view.stats else 0
        fail.set()
        await pilot.press("r")
        await settle_page(view.app)
        return (
            _text(view.query_one("#usage-status", Static)),
            view.query_one("#usage-body").display,
            painted,
        )

    status, body, painted = _drive_page(loader, go)

    assert status == "Could not read usage: the store is locked"
    assert body and painted > 0


def test_the_page_fits_a_small_terminal(machine: Machine) -> None:
    _history(machine)

    async def go(pilot: Pilot[None], view: UsageStatsView) -> tuple[int, int]:
        widest = max(
            len(line)
            for name, text in _page(view).items()
            if name in ("chart", "repos", "accounts", "busiest", "models", "when")
            for line in text.splitlines()
        )
        return widest, view.size.width

    widest, width = _drive_page(real_loader(), go, size=(80, 24))

    assert widest <= width


# --- the chart and the figures' formats ------------------------------------------------


def test_the_chart_is_a_column_a_day_and_a_column_an_hour_for_today(machine: Machine) -> None:
    _history(machine)
    week = usage_stats.stats("7d", now=NOW, tz=IST)
    today = usage_stats.stats("today", now=NOW, tz=IST)
    lifetime = usage_stats.stats("lifetime", now=NOW, tz=IST)

    daily = chart_text(week).plain.splitlines()
    hourly = chart_text(today).plain.splitlines()
    long = chart_text(lifetime, width=40).plain.splitlines()

    assert daily[0].startswith("tokens per day  (peak ")
    assert daily[-1].split() == [week.days[0].date, week.days[-1].date]
    assert len(daily) == 1 + 7 + 1  # the note, seven rows, the dates
    assert hourly[0].startswith("turns per hour of the day") and hourly[-1].split() == ["0h", "23h"]
    assert long[0].startswith("tokens per 7 days") or long[0].startswith("tokens per 30 days")


def test_the_figures_are_written_the_same_in_the_page_and_the_command() -> None:
    assert [fmt.tokens(n) for n in (0, 842, 12_400, 999_999, 1_380_000, 2_100_000_000)] == [
        "0", "842", "12.4K", "1.0M", "1.4M", "2.1B",
    ]  # fmt: skip
    assert fmt.money(0.5) == "$0.50" and fmt.money(4415.9) == "$4,416"
    assert fmt.percent(0.004) == "<1%" and fmt.percent(0) == "0%" and fmt.percent(0.456) == "46%"
    assert fmt.short_date("2026-10-25") == "Sun 25 Oct"
    assert fmt.bar(8, 8, 4) == "████" and fmt.bar(1, 8, 4) == "▌" and fmt.bar(0, 8, 4) == ""
    assert fmt.spark([0, 1, 8]) == " ▁█" and fmt.spark([0, 0]) == "  "
    assert fmt.rebucket([1] * 10, 60) == ([1] * 10, 1)
    assert fmt.rebucket([1] * 70, 60) == ([7] * 10, 7)
    assert fmt.rebucket([1] * 400, 10) == ([90, 90, 90, 90, 40], 90)
    assert fmt.column_chart([1, 4], height=2, gap=0) == [" █", "▄█"]
    assert fmt.column_chart([0, 0], height=2) == ["", ""]


@pytest.mark.parametrize("path", ["src/aisquare/cli/ui/views/usage_stats.py"])
def test_the_sharing_slot_is_marked_in_the_source_for_the_next_task(path: str) -> None:
    source = (Path(__file__).resolve().parents[1] / path).read_text(encoding="utf-8")
    assert "SHARING SLOT" in source and 'id="usage-sharing-slot"' in source
