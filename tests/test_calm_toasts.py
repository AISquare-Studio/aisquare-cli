"""asq's notifications (R13, #146): small, top-right, three at a time, quick to clear.

The owner, verbatim: "make them appear from top right, no more than 3 active at a given
moment, all with automatic time to auto dismiss ... the right most block is 'x' to dismiss
it instantly ... on clicking and dragging on the text portion allows user to select it
which automatically copies it ... the goal of right most region being dismiss is user can
spam click at the first X and the next one will keep moving up in its place".

Driven through a real ``ClipboardApp`` with Textual's notifications on, as asq and
``board -w`` run them; what triggers a toast is Textual's own ``notify``.
"""

from __future__ import annotations

import asyncio
import itertools
import math
import re
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import pytest
from textual import events
from textual.app import ComposeResult
from textual.geometry import Region
from textual.pilot import Pilot
from textual.widgets import Static
from textual.widgets._toast import Toast, ToastRack

from aisquare.cli.ui import toasts
from aisquare.cli.ui.clipboard import ClipboardApp
from aisquare.cli.ui.toasts import (
    DISMISS,
    MAX_SHOWN,
    RESUME_AT_LEAST,
    WIDTH,
    CalmToast,
    ClickGuard,
    MoreToasts,
)
from aisquare.core import clipboard

T = TypeVar("T")
WIDTHS = (38, 45, 60, 90, 200)
LONG = "the agent's pane is gone: tmux no longer has it, so its row is ended and its claims go back"


class Pane(Static):
    """What a toast covers: counts the clicks that reach it."""

    def __init__(self) -> None:
        super().__init__("pane " * 200, id="pane")
        self.clicks = 0

    def on_click(self, event: events.Click) -> None:
        self.clicks += 1


class Host(ClipboardApp[None]):
    def compose(self) -> ComposeResult:
        yield Pane()


def drive(width: int, fn: Callable[[Host, Pilot[None]], Awaitable[T]], *, height: int = 30) -> T:
    async def run() -> T:
        app = Host()
        async with app.run_test(size=(width, height), notifications=True) as pilot:
            await pilot.pause()
            return await fn(app, pilot)

    return asyncio.run(run())


def shown(app: Host) -> list[CalmToast]:
    """The toasts on screen, top first."""
    return sorted(app.screen.query(CalmToast), key=lambda toast: toast.region.y)


def first_row(toast: CalmToast) -> str:
    size = toast.outer_size
    line = toast.render_lines(Region(0, 0, size.width, size.height))[0]
    return "".join(segment.text for segment in line)


async def settle(pilot: Pilot[None]) -> None:
    await pilot.pause(0.05)
    await pilot.pause()


# --- where they are -------------------------------------------------------------------------


@pytest.mark.parametrize("width", WIDTHS)
def test_toasts_are_top_right_one_width_newest_first_with_no_padding_rows(width: int) -> None:
    async def go(app: Host, pilot: Pilot[None]) -> list[tuple[str, Region]]:
        app.notify("first", timeout=30)
        app.notify(LONG, title="second", timeout=30)
        app.notify("third", timeout=30)
        await settle(pilot)
        return [(toast.render().plain, toast.region) for toast in shown(app)]

    rows = drive(width, go)
    texts = [text for text, _ in rows]
    regions = [region for _, region in rows]
    assert texts == ["third", f"second\n{LONG}", "first"], "newest on top"
    assert all(region.width == min(WIDTH, width) for region in regions)
    assert all(region.right == width for region in regions), "flush with the right edge"
    assert regions[0].y == 1, "row 0 stays the agent bar's (Stop, Restart)"
    assert regions[0].height == 1, "a one-line toast is one row: no padding rows"
    for above, below in itertools.pairwise(regions):
        assert below.y == above.bottom + 1, "one blank row between toasts"


def test_the_dismiss_glyph_is_drawn_in_the_right_padding_and_the_text_is_textual_s() -> None:
    async def go(app: Host, pilot: Pilot[None]) -> tuple[str, str, int]:
        app.notify("renamed coder-1 to login-fix", timeout=30)
        await settle(pilot)
        toast = shown(app)[0]
        return toast.render().plain, first_row(toast), toast.outer_size.width

    plain, row, width = drive(90, go)
    assert plain == "renamed coder-1 to login-fix", "what it says is Textual's render, unchanged"
    assert len(row) == width and row[width - 1 - DISMISS // 2] == "✕"
    assert row.rstrip().endswith("✕") and "✕" not in row[: width - DISMISS]


# --- the spam-click -------------------------------------------------------------------------


@pytest.mark.parametrize("width", (38, 90, 200))
def test_clicking_one_spot_closes_toast_after_toast_whatever_their_heights(width: int) -> None:
    """The owner's spam-click: the ``✕`` of the top toast is at (width-2, 1); closing it moves
    the next toast's first row, and its ``✕``, there. Heights 2, 4 and 1, and one more
    waiting: four clicks at one spot close all four, and a fifth reaches nothing."""
    spot = (width - 1 - DISMISS // 2, 1)

    async def go(app: Host, pilot: Pilot[None]) -> tuple[list[list[int]], list[int], int, int]:
        app.notify("the one that waits", timeout=60)
        app.notify("x " * 6 + "\nsecond row", timeout=60)  # 2 rows
        app.notify("a\nb\nc\nd", timeout=60)  # 4 rows
        app.notify("one row", timeout=60)
        await settle(pilot)
        tops: list[list[int]] = [[toast.region.y for toast in shown(app)]]
        heights = [toast.region.height for toast in shown(app)]
        for _ in range(4):
            await pilot.click(offset=spot)
            await settle(pilot)
            tops.append([toast.region.y for toast in shown(app)])
        guarded = len(app.screen.query(ClickGuard))
        await pilot.click(offset=spot)  # the hand clicks once more
        await settle(pilot)
        return tops, heights, guarded, app.query_one(Pane).clicks

    tops, heights, guarded, reached = drive(width, go)
    assert heights[0] == 1 and heights[1] == 4
    assert [len(row) for row in tops] == [3, 3, 2, 1, 0], (
        "one closed per click; the waiting one came"
    )
    assert all(row[0] == 1 for row in tops[:-1]), "each next toast's first row moved to the spot"
    assert guarded == 1 and reached == 0, "the click after the last one never reached the pane"


def test_a_click_on_the_text_does_not_dismiss_and_the_dismiss_column_does_on_every_row() -> None:
    async def go(app: Host, pilot: Pilot[None]) -> tuple[int, int, int]:
        app.notify("a\nb\nc", timeout=60)
        await settle(pilot)
        region = shown(app)[0].region
        await pilot.click(offset=(region.x + 2, region.y + 1))
        await settle(pilot)
        on_text = len(shown(app))
        await pilot.click(offset=(region.right - 1, region.y + 2))  # the column, its last row
        await settle(pilot)
        return on_text, len(shown(app)), len(app._notifications)

    on_text, after, kept = drive(90, go)
    assert on_text == 1, "a click on the text kept it (Textual's toast went at any click)"
    assert (after, kept) == (0, 0)


# --- three at a time ------------------------------------------------------------------------


def test_a_fourth_makes_the_oldest_wait_its_time_stopped_under_more_and_nothing_is_lost() -> None:
    async def go(app: Host, pilot: Pilot[None]) -> dict[str, Any]:
        for n in range(5):
            app.notify(f"toast {n}", timeout=30)
        await settle(pilot)
        waiting = [n for n in app._notifications][:2]
        more = app.screen.query_one(MoreToasts)
        seen = {
            "shown": [toast.render().plain for toast in shown(app)],
            "store": len(app._notifications),
            "more": str(more.render()),
            "stopped": [math.isinf(n.timeout) for n in waiting],
        }
        await pilot.click(offset=(app.size.width - 2, 1))  # one shown goes
        await settle(pilot)
        back = next(n for n in app._notifications if n.message == "toast 1")
        seen["after"] = [toast.render().plain for toast in shown(app)]
        seen["back_running"] = math.isfinite(back.timeout) and 25 < back.time_left <= 30
        return seen

    seen = drive(90, go)
    assert seen["shown"] == ["toast 4", "toast 3", "toast 2"]
    assert seen["store"] == 5, "app._notifications keeps every one; only the showing is capped"
    assert seen["more"] == "+2 more" and seen["stopped"] == [True, True]
    assert seen["after"] == ["toast 3", "toast 2", "toast 1"], "the newest waiting came back"
    assert seen["back_running"], "it came back with the time it had, running again"
    assert MAX_SHOWN == 3


def test_a_toast_that_waited_longer_than_its_time_still_gets_its_time_when_it_comes_back() -> None:
    """Its time stopped while it waited: a 1 s toast that waited 1.5 s is shown for its 1 s,
    not reaped the moment it comes back."""

    async def go(app: Host, pilot: Pilot[None]) -> tuple[list[str], float]:
        app.notify("brief, and waiting", timeout=1.0)
        for n in range(3):
            app.notify(f"toast {n}", timeout=30)
        await settle(pilot)
        await pilot.pause(1.5)
        await pilot.click(offset=(app.size.width - 2, 1))
        await settle(pilot)
        back = [n for n in app._notifications if n.message == "brief, and waiting"]
        return [toast.render().plain for toast in shown(app)], back[0].time_left if back else -1.0

    texts, left = drive(90, go)
    assert texts[-1] == "brief, and waiting", "it came back, at the bottom"
    assert 0.5 < left <= 1.0


def test_more_dismisses_those_that_wait() -> None:
    async def go(app: Host, pilot: Pilot[None]) -> tuple[list[str], int]:
        for n in range(5):
            app.notify(f"toast {n}", timeout=30)
        await settle(pilot)
        more = app.screen.query_one(MoreToasts)
        await pilot.click(offset=(more.region.x + 2, more.region.y))
        await settle(pilot)
        return [toast.render().plain for toast in shown(app)], len(app.screen.query(MoreToasts))

    left, mores = drive(90, go)
    assert left == ["toast 4", "toast 3", "toast 2"] and mores == 0


# --- time -----------------------------------------------------------------------------------


def test_time_stops_while_the_pointer_is_over_a_toast_and_resumes_with_at_least_two_seconds() -> (
    None
):
    async def go(app: Host, pilot: Pilot[None]) -> tuple[bool, float, int]:
        app.notify("hover me", timeout=0.4)
        await settle(pilot)
        toast = shown(app)[0]
        await pilot.hover(offset=(toast.region.x + 2, toast.region.y))
        await pilot.pause(0.8)  # twice its timeout, under the pointer
        stopped = math.isinf(toast._notification.time_left)
        still = len(shown(app))
        await pilot.hover(offset=(0, 20))
        await settle(pilot)
        return stopped and still == 1, toast._notification.time_left, still

    stopped, left, _ = drive(90, go)
    assert stopped, "it outlived its time under the pointer"
    assert RESUME_AT_LEAST - 0.5 < left <= RESUME_AT_LEAST


def test_a_toast_goes_by_itself_when_its_time_is_up() -> None:
    async def go(app: Host, pilot: Pilot[None]) -> tuple[int, int]:
        app.notify("brief", timeout=0.3)
        await settle(pilot)
        before = len(shown(app))
        await pilot.pause(0.8)
        return before, len(shown(app))

    assert drive(90, go) == (1, 0)


# --- the text copies ------------------------------------------------------------------------


@pytest.mark.parametrize(("fate", "cue"), [("accepts", "✓"), ("unknown", "↗"), ("drops", "!")])
def test_a_drag_over_the_text_copies_it_by_the_one_copy_and_cues_what_became_of_it(
    fate: str, cue: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(clipboard, "osc52_terminal", lambda env: fate)

    async def go(app: Host, pilot: Pilot[None]) -> tuple[str, str, int, bool]:
        writes: list[str] = []
        assert app._driver is not None
        monkeypatch.setattr(app._driver, "write", writes.append)
        app.notify("could not stop coder-3: it is paused", timeout=30)
        await settle(pilot)
        toast = shown(app)[0]
        region = toast.region
        await pilot.mouse_down(offset=(region.x + 1, region.y))
        await pilot.hover(offset=(region.x + 15, region.y))
        await pilot.mouse_up(offset=(region.x + 15, region.y))
        await settle(pilot)
        osc52s = [write for write in writes if "]52;" in write]
        return app.clipboard, first_row(toast), len(osc52s), toast.tooltip is None

    copied, row, osc52s, untipped = drive(90, go)
    assert copied and "could not stop coder-3: it is paused".startswith(copied.strip())
    assert osc52s == 1, "through the app's one copy: OSC 52 at the gesture"
    assert row.rstrip().endswith(cue)
    assert untipped == (cue == "✓"), "a copy that is not one says why"


def test_the_cue_gives_way_to_the_dismiss_glyph(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(toasts, "CUE_SECONDS", 0.2)

    async def go(app: Host, pilot: Pilot[None]) -> tuple[str, str]:
        app.notify("cue me", timeout=30)
        await settle(pilot)
        toast = shown(app)[0]
        toast.cue(clipboard.CopyResult("copied", "osc52"))
        cued = first_row(toast)
        await pilot.pause(0.5)
        return cued, first_row(toast)

    cued, later = drive(90, go)
    assert cued.rstrip().endswith("✓") and later.rstrip().endswith("✕")


# --- what stays -----------------------------------------------------------------------------


def test_with_the_hooks_gone_textual_s_own_toasts_stand(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(toasts, "CALM", False)

    async def go(app: Host, pilot: Pilot[None]) -> tuple[int, int]:
        app.notify("stock", timeout=30)
        await settle(pilot)
        stock = [toast for toast in app.screen.query(Toast) if type(toast) is Toast]
        return len(stock), len(app.screen.query(CalmToast))

    assert drive(90, go) == (1, 0)


def test_the_hooks_it_reads_are_there_on_the_textual_installed() -> None:
    """A Textual that moves them falls back to its own toasts (:data:`toasts.CALM`); this
    says so here, before a user's asq quietly shows the old ones."""
    assert toasts.CALM
    assert hasattr(ToastRack, "_toast_id")


def test_every_colour_is_the_theme_s() -> None:
    """Dark, light and rlx follow: the styles name theme variables, never a colour."""
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgb\(", toasts.CSS)
    assert "$text-muted" in toasts.CSS


@pytest.mark.parametrize("theme", ["textual-dark", "textual-light"])
def test_toasts_paint_on_the_built_in_themes(theme: str) -> None:
    async def go(app: Host, pilot: Pilot[None]) -> str:
        app.theme = theme
        app.notify("themed", severity="warning", timeout=30)
        await settle(pilot)
        return first_row(shown(app)[0])

    assert drive(90, go).rstrip().endswith("✕")
