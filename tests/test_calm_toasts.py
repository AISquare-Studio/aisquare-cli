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
from aisquare.cli.ui.terminal import SelectionHost
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


def shown(app: ClipboardApp[None]) -> list[CalmToast]:
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


# --- the tester's #517 findings -----------------------------------------------------------------


def _record_copies(app: ClipboardApp[None], monkeypatch: pytest.MonkeyPatch) -> list[str]:
    copied: list[str] = []
    real = app.copy_text

    def copy_text(text: str, *, cue: Any = None) -> None:
        copied.append(text)
        real(text, cue=cue)

    monkeypatch.setattr(app, "copy_text", copy_text)
    return copied


def test_a_drag_that_ends_in_the_dismiss_column_copies_and_keeps_the_toast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Selecting to the end of a line and overshooting by one cell lost the toast and the
    copy: the release was a click in the column (tester-rlx-calm, #517, blocker 2). The
    events come as the driver sends them: Textual makes the release a Click, as it did."""

    async def go(app: Host, pilot: Pilot[None]) -> tuple[list[str], int]:
        copied = _record_copies(app, monkeypatch)
        app.notify("select me to the very end", timeout=30)
        await settle(pilot)
        region = shown(app)[0].region
        end = (region.right - 2, region.y)  # the dismiss column
        app.post_message(events.MouseDown(app.screen, **_at(region.x + 1, region.y)))
        app.post_message(events.MouseMove(app.screen, **_at(*end)))
        app.post_message(events.MouseUp(app.screen, **_at(*end)))
        await settle(pilot)
        return copied, len(shown(app))

    copied, left = drive(90, go)
    assert left == 1, "a drag is no dismissal"
    assert len(copied) == 1 and "select me to the very end".startswith(copied[0].strip())


@pytest.mark.parametrize(
    ("to", "closes"),
    [((0, 0), True), ((-1, 0), True), ((0, 1), False), ((-DISMISS - 2, 0), False)],
    ids=["in place", "drifting on its row", "dragged to the next row", "dragged onto the text"],
)
def test_a_press_in_the_dismiss_column_dismisses_only_when_released_there_without_a_drag(
    to: tuple[int, int], closes: bool
) -> None:
    """Dismiss only when the press AND the release are in the column, and no drag (#517);
    a hand that drifts along the column on its row still clicks it."""

    async def go(app: Host, pilot: Pilot[None]) -> int:
        app.notify(LONG, timeout=30)  # rows to drag over
        await settle(pilot)
        region = shown(app)[0].region
        press = (region.right - 1, region.y)
        release = (press[0] + to[0], press[1] + to[1])
        app.post_message(events.MouseDown(app.screen, **_at(*press)))
        app.post_message(events.MouseMove(app.screen, **_at(*release)))
        app.post_message(events.MouseUp(app.screen, **_at(*release)))
        await settle(pilot)
        return len(shown(app))

    assert drive(90, go) == (0 if closes else 1)


def test_a_double_click_copies_the_word_once_and_a_third_the_whole_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The clicks come as the driver sends them, back to back: Textual counts the chain."""

    async def clicks(app: Host, at: tuple[int, int], times: int) -> None:
        for _ in range(times):
            app.post_message(events.MouseDown(app.screen, **_at(*at)))
            app.post_message(events.MouseUp(app.screen, **_at(*at)))

    async def go(app: Host, pilot: Pilot[None]) -> tuple[list[str], list[str]]:
        copied = _record_copies(app, monkeypatch)
        app.notify("renamed coder-1 to login-fix", timeout=30)
        await settle(pilot)
        region = shown(app)[0].region
        await clicks(app, (region.x + 1 + len("renamed c"), region.y), 2)  # in "coder-1"
        await settle(pilot)
        twice = list(copied)
        copied.clear()
        await clicks(app, (region.x + 1 + len("renamed co"), region.y), 3)  # a chain anew
        await settle(pilot)
        return twice, copied

    twice, thrice = drive(90, go)
    assert twice == ["coder-1"], "the word under the double-click, copied once"
    assert thrice[-1:] == ["renamed coder-1 to login-fix"]
    assert thrice.count("renamed coder-1 to login-fix") == 1, "the whole, copied once"


def test_a_drag_that_leaves_the_toast_stays_the_toast_s(monkeypatch: pytest.MonkeyPatch) -> None:
    """A drag begun on a toast's text that ran left onto the pane copied the PANE's text
    (#517, finding 5). It holds the pointer: its release is the toast's, and only the toast's
    text is selected and copied."""

    async def go(app: Host, pilot: Pilot[None]) -> tuple[list[str], list[str], bool]:
        copied = _record_copies(app, monkeypatch)
        app.notify("drag me off to the left", timeout=30)
        await settle(pilot)
        toast = shown(app)[0]
        region = toast.region
        await pilot.mouse_down(offset=(region.x + 10, region.y))
        captured = app.mouse_captured is toast
        await pilot.hover(offset=(5, region.y + 3))  # well into the pane
        await pilot.mouse_up(offset=(5, region.y + 3))
        await settle(pilot)
        selected = [type(widget).__name__ for widget in app.screen.selections]
        return copied, selected, captured

    copied, selected, captured = drive(90, go)
    assert captured, "the press on the text holds the pointer"
    assert copied and all("pane" not in text for text in copied), copied
    assert "drag me off to the left".find(copied[0].strip()) >= 0
    assert selected == ["CalmToast"], "the pane's part of the selection is dropped"


def test_the_router_leaves_the_panes_alone_while_a_toast_holds_the_drag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aisquare.cli.ui import terminal

    told: list[str] = []

    class App_:
        mouse_captured = CalmToast.__new__(CalmToast)

    monkeypatch.setattr(terminal, "_tell_panes", lambda *a, **k: told.append("panes"))
    terminal.route_selection_gesture(App_(), 1)  # type: ignore[arg-type]
    assert told == []


def _at(x: int, y: int, button: int = 1) -> dict[str, Any]:
    """A mouse event's fields at screen cell (x, y), as the driver gives them."""
    return {
        **{"x": x, "y": y, "delta_x": 0, "delta_y": 0, "button": button},
        **{"shift": False, "meta": False, "ctrl": False, "screen_x": x, "screen_y": y},
    }


def test_a_toast_drag_whose_release_is_lost_ends_as_the_toast_s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pointer left the terminal with the button down, and its first move back carries
    none (``SelectionHost``'s lost release). A toast's drag ends where it got to, as its own:
    its text copied, the pointer let go — held, every click in asq went to the toast — and
    the panes are not told a gesture of theirs ended (#517, finding 5)."""
    from aisquare.cli.ui import terminal

    routed: list[int | None] = []
    monkeypatch.setattr(
        terminal, "route_selection_gesture", lambda app, button, **_: routed.append(button)
    )

    async def run() -> tuple[bool, object, list[str], list[str]]:
        app = PaneHost()
        async with app.run_test(size=(90, 30), notifications=True) as pilot:
            await pilot.pause()
            copied = _record_copies(app, monkeypatch)
            app.notify("drag me out of the terminal", timeout=30)
            await settle(pilot)
            toast = shown(app)[0]
            region = toast.region
            app.post_message(events.MouseDown(app.screen, **_at(region.x + 10, region.y)))
            app.post_message(events.MouseMove(app.screen, **_at(5, region.y + 3)))
            await settle(pilot)
            held = app.mouse_captured is toast
            app.post_message(events.MouseMove(app.screen, **_at(5, region.y + 3, button=0)))
            await settle(pilot)
            selected = [type(widget).__name__ for widget in app.screen.selections]
            return held, app.mouse_captured, copied, selected

    held, captured, copied, selected = asyncio.run(run())
    assert held and captured is None, "it let go of the pointer"
    assert routed == [], "the panes were not told the gesture was theirs"
    assert len(copied) == 1 and "drag me out of the terminal".find(copied[0].strip()) >= 0
    assert selected == ["CalmToast"]


def test_a_toast_taken_away_mid_drag_lets_go_of_the_pointer() -> None:
    async def go(app: Host, pilot: Pilot[None]) -> tuple[bool, object]:
        app.notify("the oldest", timeout=30)
        await settle(pilot)
        toast = shown(app)[0]
        await pilot.mouse_down(offset=(toast.region.x + 2, toast.region.y))
        held = app.mouse_captured is toast
        for n in range(MAX_SHOWN):
            app.notify(f"newer {n}", timeout=30)  # the oldest waits: its toast leaves
        await settle(pilot)
        return held, app.mouse_captured

    held, captured = drive(90, go)
    assert held and captured is None


class PaneHost(SelectionHost):
    def compose(self) -> ComposeResult:
        yield Pane()


@pytest.mark.parametrize("calm", [True, False], ids=["asq's toasts", "Textual's stock toasts"])
def test_clicks_at_zero_ms_gaps_never_crash(calm: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    """5 clicks on one spot 0-20 ms apart crashed asq: the pressed toast was pruned, and
    Textual built the selection from its parent, gone (#517, finding 3; the stock toasts
    crashed the same way). asq invites the spam-click, so no click rate may crash it.

    Each press comes as the driver sends it, the moment the last click's toast has left the
    DOM and before the screen has redrawn its map (0 ms apart): the moment the crash needs.
    The pilot waits for the app to settle before each event, so it never meets it."""
    monkeypatch.setattr(toasts, "CALM", calm)

    async def run() -> tuple[int, bool]:
        app = PaneHost()
        async with app.run_test(size=(90, 30), notifications=True) as pilot:
            await pilot.pause()
            for n in range(6):
                app.notify(f"toast {n}", timeout=30)
            await pilot.pause(0.1)
            if calm:
                x, y = 89 - 1 - DISMISS // 2, 1  # the top toast's dismiss column
            else:  # a stock toast closes on a click anywhere on it: the lowest one's
                lowest = max(app.screen.query(Toast), key=lambda toast: toast.region.y)
                x, y = lowest.region.x + 2, lowest.region.y + 1
            for _ in range(5):
                under, _ = app.screen.get_widget_at(x, y)
                app.post_message(events.MouseDown(app.screen, **_at(x, y)))
                app.post_message(events.MouseUp(app.screen, **_at(x, y)))
                for _ in range(200):  # no pause: only until what it closed is pruned
                    await asyncio.sleep(0)
                    if not under.is_attached:
                        break
            await pilot.pause(0.3)
            return len(app._notifications), app.is_running

    left, running = asyncio.run(run())
    assert running, "asq is still up"
    assert left < 6, "the clicks closed toasts"


def test_a_closing_or_pruned_toast_offers_no_selection() -> None:
    """The toast's own guard: Textual builds a press's selection from a selectable widget's
    parent, so a toast that is closing, or gone, offers none (#517, finding 3)."""

    async def go(app: Host, pilot: Pilot[None]) -> tuple[bool, bool, bool]:
        app.notify("going", timeout=30)
        await settle(pilot)
        toast = shown(app)[0]
        before = toast.allow_select
        toast.close_now()
        closing = toast.allow_select
        await settle(pilot)
        return before, closing, toast.allow_select

    before, closing, gone = drive(90, go)
    assert before and not closing and not gone


def test_no_toast_keeps_a_timer_past_its_own_time() -> None:
    """Textual's ``Toast._on_mount`` sets a timer for the toast's ``_timeout``, which asq set to
    a year, beside the toast's own: on Windows a sleeping Textual timer holds a thread of
    asyncio's default executor, the one Textual's thread workers run on (eight on CI's
    runners), so every toast shown took one for good, and asq's workers starved — eight UI
    tests failed on windows-latest (#517, run 44026703595). Every timer a toast keeps ends
    by its own time, or the toast holds none while its time is stopped."""

    async def go(app: Host, pilot: Pilot[None]) -> list[tuple[float, float]]:
        for n in range(MAX_SHOWN + 1):
            app.notify(f"toast {n}", timeout=10)
        await settle(pilot)
        sleeping = []
        for toast in app.screen.query(CalmToast):
            for timer in list(toast._timers):
                if timer._task is not None and not timer._task.done():
                    sleeping.append((timer._interval, toast._notification.time_left))
        return sleeping

    sleeping = drive(90, go)
    assert sleeping, "the toasts that show keep their time"
    assert [(interval, left) for interval, left in sleeping if interval > left + 1] == []
