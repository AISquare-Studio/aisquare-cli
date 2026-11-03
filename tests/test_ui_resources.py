"""Settings ▸ Agent resources: the way in, the table, the sorts, and sampling only while open.

The page is driven with a fake sampler (a ``TopReport`` built here, counted): what a sample
reads is ``tests/test_fleet_top.py``'s. The way in is driven through the whole fleet UI.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

import pytest
from textual.app import App, ComposeResult
from textual.geometry import Region
from textual.pilot import Pilot
from textual.widgets import Button, Static

from aisquare.cli.ui.app import FleetApp
from aisquare.cli.ui.sidebar import SETTINGS_KEY, SettingsRow
from aisquare.cli.ui.views import resources as resources_view
from aisquare.cli.ui.views.resources import SAMPLE_WORKER, ResourcesView
from aisquare.services.fleet_top import AgentUse, TopReport
from tests.test_ui_settings_hub import GOOD
from tests.ui_workers import settle_page

T = TypeVar("T")
SIZE = (140, 40)


def _agent(label: str, *, cpu: float, rss_kb: int, tokens: int) -> AgentUse:
    return AgentUse(
        agent_id=f"agt_{label}",
        project_id="prj_shop",
        project="shop",
        codename="brave-otter",
        label=label,
        role="coder",
        account="work",
        account_slot=2,
        session_id=f"ses-{label}",
        pane_id="%1",
        state="running",
        pid=500,
        cpu=cpu,
        rss_kb=rss_kb,
        processes=3,
        uptime=600,
        tokens=tokens,
        cost_usd=1.25,
    )


REPORT = TopReport(
    agents=[
        _agent("coder-busy", cpu=180.0, rss_kb=400_000, tokens=1_000),
        _agent("coder-fat", cpu=2.0, rss_kb=4_000_000, tokens=2_000),
        _agent("coder-chatty", cpu=0.5, rss_kb=300_000, tokens=90_000_000),
    ],
    sampled_at=datetime(2026, 10, 30, 12, 0, tzinfo=UTC),
    took_ms=42.0,
)


class Counted:
    """A sampler that answers :data:`REPORT` and counts its calls."""

    def __init__(self, report: TopReport = REPORT) -> None:
        self.report = report
        self.calls = 0

    def __call__(self) -> TopReport:
        self.calls += 1
        return self.report


def _text(widget: Static) -> str:
    plain = getattr(widget.visual, "plain", None)
    if isinstance(plain, str):
        return plain.rstrip()
    lines = widget.render_lines(Region(0, 0, max(widget.size.width, 1), max(widget.size.height, 1)))
    return "\n".join(line.text.rstrip() for line in lines).rstrip()


def _labels(view: ResourcesView) -> list[str]:
    """The agents in the table, top to bottom."""
    table = _text(view.query_one("#resources-table", Static))
    return [
        word for line in table.splitlines() for word in line.split() if word.startswith("coder-")
    ]


class Host(App[None]):
    def __init__(self, sampler: Any) -> None:
        super().__init__()
        self.sampler = sampler

    def compose(self) -> ComposeResult:
        yield ResourcesView(sampler=self.sampler, id="resources")


def _drive_page(sampler: Any, fn: Callable[[Pilot[None], ResourcesView], Awaitable[T]]) -> T:
    async def run() -> T:
        app = Host(sampler)
        async with app.run_test(size=SIZE) as pilot:
            await settle_page(app)
            return await fn(pilot, app.query_one(ResourcesView))

    return asyncio.run(run())


def _fleet_app() -> FleetApp:
    return FleetApp(
        refresh_seconds=3600,
        doctor=lambda: [],
        accounts=None,
        headroom=lambda accounts: GOOD,
        headroom_seconds=3600,
    )


# --- the way in ---------------------------------------------------------------------------------


def test_the_hub_opens_agent_resources_which_samples_while_shown_and_stops_when_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sampler = Counted()
    monkeypatch.setattr(resources_view, "default_sampler", lambda: sampler)

    async def go() -> dict[str, Any]:
        app = _fleet_app()
        seen: dict[str, Any] = {}
        async with app.run_test(size=SIZE) as pilot:
            await settle_page(app)
            await pilot.click(app.query_one(SettingsRow))
            await settle_page(app)
            await pilot.click("#hub-resources")
            await settle_page(app)
            page = app.query_one("#resources", ResourcesView)
            seen["open"] = (app.content.current, app.sidebar.selected_key, page.sampling)
            seen["labels"] = _labels(page)
            seen["status"] = _text(page.query_one("#resources-status", Static))
            seen["samples"] = sampler.calls
            await pilot.click(app.query_one(SettingsRow))  # back to the hub: the page hides
            await settle_page(app)
            seen["left"] = (app.content.current, page.sampling)
        return seen

    seen = asyncio.run(go())

    assert seen["open"] == ("resources", SETTINGS_KEY, True)
    assert seen["labels"] == ["coder-busy", "coder-fat", "coder-chatty"], "sorted by CPU"
    assert seen["status"].startswith("3 live agents · sampled in 0.04 s · sorted by CPU")
    assert seen["samples"] == 1
    assert seen["left"] == ("settings-hub", False), "hidden, the page's timer is stopped"


def test_a_hidden_page_samples_nothing_and_shown_again_resumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resources_view, "REFRESH_SECONDS", 0.05)
    sampler = Counted()

    async def go(pilot: Pilot[None], view: ResourcesView) -> tuple[int, int, int, int]:
        await pilot.pause(0.3)
        await settle_page(view.app)
        while_open = sampler.calls
        view.display = False
        await settle_page(view.app)
        hidden_at = sampler.calls
        await pilot.pause(0.3)
        hidden_later = sampler.calls
        view.display = True
        await pilot.pause(0.3)
        await settle_page(view.app)
        return while_open, hidden_at, hidden_later, sampler.calls

    while_open, hidden_at, hidden_later, shown_again = _drive_page(sampler, go)

    assert while_open >= 3, "every REFRESH_SECONDS while it shows"
    assert hidden_later == hidden_at, "nothing is sampled while the page is hidden"
    assert shown_again > hidden_later


# --- the table and its sorts ----------------------------------------------------------------------


def test_the_keys_sort_by_cpu_memory_and_tokens_without_reading_again() -> None:
    sampler = Counted()

    async def go(pilot: Pilot[None], view: ResourcesView) -> dict[str, Any]:
        orders: dict[str, Any] = {"first": _labels(view)}
        for key in ("2", "3", "1"):
            await pilot.press(key)
            await settle_page(view.app)
            header = _text(view.query_one("#resources-table", Static)).splitlines()[0]
            orders[key] = (view.sort, _labels(view), "▼" in header.split("CPU%")[1][:3])
        orders["calls"] = sampler.calls
        await pilot.press("r")
        await settle_page(view.app)
        orders["after r"] = sampler.calls
        return orders

    orders = _drive_page(sampler, go)

    assert orders["first"] == ["coder-busy", "coder-fat", "coder-chatty"]
    assert orders["2"][:2] == ("mem", ["coder-fat", "coder-busy", "coder-chatty"])
    assert orders["3"][:2] == ("tokens", ["coder-chatty", "coder-fat", "coder-busy"])
    assert orders["1"] == ("cpu", ["coder-busy", "coder-fat", "coder-chatty"], True)
    assert orders["calls"] == 1, "a sort repaints the last sample"
    assert orders["after r"] == 2, "r reads again"


def test_the_sort_buttons_show_which_sort_is_on_and_a_click_switches() -> None:
    async def go(pilot: Pilot[None], view: ResourcesView) -> tuple[list[str], list[str]]:
        def variants() -> list[str]:
            return [
                str(view.query_one(f"#resources-sort-{k}", Button).variant)
                for k in resources_view.KEYS
            ]

        before = variants()
        await pilot.click("#resources-sort-tokens")
        await settle_page(view.app)
        return before, variants()

    before, after = _drive_page(Counted(), go)

    assert before == ["primary", "default", "default"]
    assert after == ["default", "default", "primary"]


def test_with_no_live_agent_the_page_says_so() -> None:
    empty = TopReport(agents=[], sampled_at=REPORT.sampled_at, took_ms=1.0)

    async def go(pilot: Pilot[None], view: ResourcesView) -> str:
        return _text(view.query_one("#resources-table", Static))

    assert _drive_page(Counted(empty), go) == resources_view.EMPTY


def test_a_tick_while_a_sample_still_runs_starts_no_second_one() -> None:
    release = threading.Event()
    calls: list[int] = []

    def slow() -> TopReport:
        calls.append(1)
        release.wait(5)
        return REPORT

    async def go(pilot: Pilot[None], view: ResourcesView) -> tuple[bool, int, int]:
        await pilot.pause()
        busy = view.busy
        view.tick()
        view.tick()
        await pilot.pause()
        during = len(calls)
        release.set()
        await settle_page(view.app, group=SAMPLE_WORKER)
        return busy, during, len(_labels(view))

    async def run() -> tuple[bool, int, int]:
        app = Host(slow)
        async with app.run_test(size=SIZE) as pilot:
            return await go(pilot, app.query_one(ResourcesView))

    busy, during, painted = asyncio.run(run())

    assert busy and during == 1
    assert painted == 3


def test_a_sample_that_fails_says_why_and_keeps_what_was_shown() -> None:
    answers: list[TopReport | Exception] = [REPORT, RuntimeError("ps exploded")]

    def flaky() -> TopReport:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def go(pilot: Pilot[None], view: ResourcesView) -> tuple[str, list[str]]:
        await pilot.press("r")
        await settle_page(view.app)
        return _text(view.query_one("#resources-status", Static)), _labels(view)

    status, labels = _drive_page(flaky, go)

    assert status == "Could not read the agents: ps exploded"
    assert labels == ["coder-busy", "coder-fat", "coder-chatty"]
