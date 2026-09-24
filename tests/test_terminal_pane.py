"""``TerminalPane`` and ``AgentView``, driven headless against a fake tmux.

The fake (``tests/pane_harness.py``, shared with the shell tests) is a
:data:`aisquare.core.tmux.Runner` — it answers the argv the real ``TmuxServer``
builds the way tmux 3.7c would (``capture-pane`` rows, then the
``display-message`` line, or ``can't find pane`` on exit 1) and records every
``send-keys`` / ``load-buffer`` / ``paste-buffer`` / ``resize-window``. So the
widget is tested through the real ``TmuxServer`` plumbing, with tmux itself the
only thing replaced; the one test at the end puts a real tmux behind the same
widget. Mouse gestures are posted to the app the way the driver posts them
(the harness says why ``Pilot``'s are not enough).

Every claim has a negative half (CONTRIBUTING, "Writing a guard that still
guards"): a row that did not change is NOT repainted, a key that is not the
escape hatch IS forwarded, a pane with no history does NOT scroll, and so on.
Assertions read what reaches the screen — the Strips Textual composites — not
the strings the widget was handed.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import os
import re
import shutil
import time
from collections.abc import Callable, Coroutine, Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

import pytest
from rich.cells import cell_len, split_graphemes
from rich.style import Style
from textual import Logger, events
from textual.app import App, ComposeResult
from textual.dom import NoScreen
from textual.geometry import Offset, Region, Size
from textual.notifications import SeverityLevel
from textual.pilot import Pilot
from textual.screen import ModalScreen
from textual.selection import Selection
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import ContentSwitcher, Footer, Input, Static

from aisquare.cli.ui import terminal as terminal_module
from aisquare.cli.ui.terminal import (
    _MOUNTED_PANES,
    NO_PANE,
    PANE_GONE,
    TMUX_UNAVAILABLE,
    DisplayedRow,
    EscapeToSidebar,
    SelectionHost,
    Shown,
    TerminalPane,
    _extract,
    route_gesture_start,
    route_selection_gesture,
)
from aisquare.cli.ui.views.agent import AgentView, header_text
from aisquare.core.tmux import BUNDLED_CONF, TmuxError, TmuxServer
from aisquare.models import FleetAgent, FleetAgentStatus
from tests.pane_harness import (
    FakePane,
    FakeTmux,
    click,
    drag,
    mouse_event,
    move,
    press,
    release,
)

T = TypeVar("T")

# --- the host app -----------------------------------------------------------------------


class Host(SelectionHost):
    """The pane, optionally under a neighbour, with notices recorded.

    ``with_header`` is what ``AgentView`` actually builds: a plain ``Static``
    above the pane. ``with_input`` is an ``Input``, which calls
    ``capture_mouse()`` — so a drag begun there never delivers its release to
    the pane at all. Any test about a gesture crossing the pane's edge has to
    use the header, or it measures a path the app does not have (review of
    #120, round 6, which is exactly how a broken cross-widget copy passed).

    A ``SelectionHost``, exactly as ``FleetApp`` is, so the two cannot drift:
    this harness once mirrored the shell's gesture handlers by hand and fell
    behind, leaving twenty gesture tests exercising an end-of-gesture path
    production did not have (review of #120, round 9). It adds nothing of its
    own to the gesture path; ``tests/test_ui_shell.py`` still drives the real
    app end to end.
    """

    def __init__(
        self,
        server: TmuxServer,
        pane_id: str | None,
        *,
        escape_key: str = "f12",
        with_input: bool = False,
        with_header: bool = False,
        with_footer: bool = False,
    ) -> None:
        super().__init__()
        self._server = server
        self._pane_id = pane_id
        self._escape_key = escape_key
        self._with_input = with_input
        self._with_header = with_header
        self._with_footer = with_footer
        self.escapes = 0
        self.notices: list[str] = []

    def compose(self) -> ComposeResult:
        if self._with_input:
            yield Input(id="other")
        if self._with_header:
            yield Static("agent header line", id="other")
        yield TerminalPane(
            self._pane_id, server=self._server, escape_key=self._escape_key, id="pane"
        )
        if self._with_footer:
            # ``ALLOW_SELECT = False``, so Textual leaves a standing selection
            # alone when a gesture happens here — the shape of an unrelated
            # release that used to re-copy it.
            yield Footer()

    @property
    def pane(self) -> TerminalPane:
        return self.query_one("#pane", TerminalPane)

    def on_escape_to_sidebar(self, message: EscapeToSidebar) -> None:
        self.escapes += 1

    def notify(
        self,
        message: str,
        *,
        title: str = "",
        severity: SeverityLevel = "information",
        timeout: float | None = None,
        markup: bool = True,
    ) -> None:
        self.notices.append(message)


class SwitcherHost(SelectionHost):
    """Two panes in a ``ContentSwitcher`` — the shell's own shape for hidden tabs."""

    def __init__(self, server: TmuxServer) -> None:
        super().__init__()
        self._server = server

    def compose(self) -> ComposeResult:
        with ContentSwitcher(initial="first", id="tabs"):
            yield TerminalPane("%1", server=self._server, id="first")
            yield TerminalPane("%2", server=self._server, id="second")

    @property
    def tabs(self) -> ContentSwitcher:
        return self.query_one("#tabs", ContentSwitcher)


class PairHost(SelectionHost):
    """Two panes stacked, BOTH visible — the one shape in which two highlights can
    stand at once, since Textual's own drag replaces every selection and a click
    clears them all; the tests that need two write them through the screen."""

    def __init__(self, server: TmuxServer) -> None:
        super().__init__()
        self._server = server

    def compose(self) -> ComposeResult:
        yield TerminalPane("%1", server=self._server, id="first")
        yield TerminalPane("%2", server=self._server, id="second")


def run(coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def rows(pane: TerminalPane) -> list[Strip]:
    """What Textual composites for the pane: its rendered Strips, top to bottom."""
    width, height = pane.content_size
    return pane.render_lines(Region(0, 0, width, height))


_MARKER = re.compile(r"\s*\[\u2191\d+/\d+\]$")


def screen_text(pane: TerminalPane) -> list[str]:
    """The pane's rows as text — with the ``[↑k/history]`` marker removed from
    row 0 ONLY while the pane is scrolled, which is the one place it may be.

    Tests that use this ask WHICH rows are shown. Scoped rather than blanket:
    a marker painted on any other row, on a live pane, or left behind after
    the view returns to live must stay visible to every caller, and a row of
    agent output that happens to end in ``[↑3/5]`` must not be rewritten.
    """
    texts = [strip.text.rstrip() for strip in rows(pane)]
    if pane.scrollback and texts:
        texts[0] = _MARKER.sub("", texts[0])
    return texts


def style_at(strip: Strip, x: int) -> Style:
    segments = list(strip.crop(x, x + 1))
    assert segments, f"no cell at x={x}"
    return segments[0].style or Style()


def reverse_anywhere(strip: Strip, width: int) -> bool:
    return any(style_at(strip, x).reverse for x in range(width))


def synced(pane: TerminalPane) -> bool:
    """The frame on screen was captured at the widget's own size: the resize landed."""
    facts = pane.facts
    return facts is not None and (facts.width, facts.height) == tuple(pane.content_size)


async def wait_until(
    pilot: Pilot[None], predicate: Callable[[], bool], timeout: float = 3.0
) -> None:
    """Poll ``predicate`` between event-loop turns; fail loudly on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await pilot.pause(0.02)
        if predicate():
            return
    raise AssertionError(f"condition not met within {timeout:.1f}s")


def scroll_event(pane: TerminalPane, *, up: bool) -> events.MouseScrollUp | events.MouseScrollDown:
    cls = events.MouseScrollUp if up else events.MouseScrollDown
    return cls(pane, 1, 1, 0, -1 if up else 1, 0, False, False, False)


@pytest.fixture
def fake() -> FakeTmux:
    tmux = FakeTmux()
    tmux.panes["%1"] = FakePane(
        screen=["\x1b[31mred\x1b[0m plain", "second row", "third row"],
        cursor=(2, 0),
    )
    return tmux


# --- rendering ----------------------------------------------------------------------------


def test_renders_text_and_sgr_colour(fake: FakeTmux, tmp_path: Path) -> None:
    async def drive() -> tuple[list[str], Style, Style, Style]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            await pilot.pause()
            strips = rows(pane)
            ours = style_at(pane.render_line(0), 0)
            return screen_text(pane), ours, style_at(strips[0], 0), style_at(strips[0], 4)

    text, ours, red_cell, plain_cell = run(drive())
    assert text[0] == "red plain"
    assert text[1] == "second row"
    assert text[2] == "third row"
    assert text[3:] == ["", "", ""]  # a short pane pads to the widget, no stray rows
    # Our Strip carries the SGR colour by name; Textual then maps ANSI red to the
    # theme's truecolor before compositing, so the screen cell is a red-dominant
    # triplet that differs from the plain cell beside it.
    assert ours.color is not None and ours.color.number == 1  # ANSI red, as SGR 31 says
    assert red_cell.color is not None and red_cell.color.triplet is not None
    red, green, blue = red_cell.color.triplet
    assert red > green and red > blue
    # The negative: the un-escaped part of the same row carries no red.
    assert plain_cell.color != red_cell.color


def test_repaints_only_the_rows_that_changed(fake: FakeTmux, tmp_path: Path) -> None:
    async def drive() -> tuple[tuple[int, int], tuple[int, int], bool, bool, list[str]]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            await pilot.pause()
            rows(pane)  # settle: everything rendered once
            painted, rendered = pane.rows_repainted, pane.lines_rendered
            # Negative control: an unchanged frame repaints nothing.
            unchanged = pane.refresh_frame()
            await pilot.pause()
            rows(pane)
            same = (pane.rows_repainted - painted, pane.lines_rendered - rendered)
            painted, rendered = pane.rows_repainted, pane.lines_rendered
            # One row changes (the cursor stays on row 0): exactly one row repaints.
            fake.panes["%1"].screen[2] = "third row, edited"
            changed = pane.refresh_frame()
            await pilot.pause()
            text = screen_text(pane)
            one = (pane.rows_repainted - painted, pane.lines_rendered - rendered)
            return same, one, unchanged, changed, text

    same, one, unchanged, changed, text = run(drive())
    assert (unchanged, changed) == (False, True)
    assert same == (0, 0)
    assert one[0] == 1, "only the edited row may be marked dirty"
    assert one[1] == 1, "Textual asked render_line for exactly the dirty row"
    assert text[2] == "third row, edited" and text[0] == "red plain"


def test_a_whole_screen_change_repaints_every_row(fake: FakeTmux, tmp_path: Path) -> None:
    """The control for the counter above: it can count more than one."""

    async def drive() -> int:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            await pilot.pause()
            before = pane.rows_repainted
            fake.panes["%1"].screen = [f"new {y}" for y in range(6)]
            pane.refresh_frame()
            return pane.rows_repainted - before

    assert run(drive()) == 6


def test_cursor_is_a_reverse_video_cell_only_when_live_and_visible(
    fake: FakeTmux, tmp_path: Path
) -> None:
    async def drive() -> tuple[bool, bool, bool, bool, bool]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            pane.focus()
            await wait_until(pilot, lambda: synced(pane))
            await pilot.pause()
            strips = rows(pane)
            at_cursor = style_at(strips[0], 2).reverse is True
            beside = style_at(strips[0], 1).reverse is True
            # Hidden cursor: no reverse cell anywhere on the row.
            fake.panes["%1"].cursor_visible = False
            pane.refresh_frame()
            await pilot.pause()
            hidden = reverse_anywhere(rows(pane)[0], 9)
            # Visible again but scrolled into history: the cursor is not drawn.
            fake.panes["%1"].cursor_visible = True
            fake.panes["%1"].history = ["old"] * 5
            pane.refresh_frame()  # the widget learns the history size from a frame
            pane.scroll_history(2)
            await pilot.pause()
            scrolled = reverse_anywhere(rows(pane)[0], 9)
            pane.scroll_history(-2)
            await pilot.pause()
            back = style_at(rows(pane)[0], 2).reverse is True
            return at_cursor, beside, hidden, scrolled, back

    at_cursor, beside, hidden, scrolled, back = run(drive())
    assert at_cursor and not beside
    assert not hidden
    assert not scrolled
    assert back


def test_a_pane_taller_than_the_widget_shows_its_last_rows(fake: FakeTmux, tmp_path: Path) -> None:
    """Between a ``Resize`` and its debounced ``resize-window`` the pane is taller than us."""
    fake.apply_resize = False
    fake.panes["%1"].screen = [f"row{y}" for y in range(24)]
    fake.panes["%1"].cursor = (0, 23)

    async def drive() -> tuple[list[str], bool]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 4)) as pilot:
            pane = host.pane
            pane.focus()
            # Wait for the resize to be ASKED for; this tmux does not obey it.
            await wait_until(pilot, lambda: ("resize-window", "%1", "40", "4") in fake.input)
            pane.refresh_frame()
            await pilot.pause()
            return screen_text(pane), style_at(rows(pane)[3], 0).reverse is True

    text, cursor_on_last = run(drive())
    assert text == ["row20", "row21", "row22", "row23"]
    assert cursor_on_last  # the cursor moved up with the rows it belongs to


# --- the render loop ------------------------------------------------------------------------


def test_render_loop_runs_fast_while_streaming_and_backs_off_when_idle(
    fake: FakeTmux, tmp_path: Path
) -> None:
    ticks = {"n": 0, "frozen": False}

    def stream(pane: FakePane) -> None:
        if not ticks["frozen"]:
            ticks["n"] += 1
            pane.screen = [f"line {ticks['n']}"]

    fake.before_capture = stream

    async def drive() -> tuple[float, int, float, int]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await pilot.pause(0.4)
            streaming_interval, streamed = pane.interval, pane.frames
            ticks["frozen"] = True
            await pilot.pause(0.7)
            idle_interval, idle_frames = pane.interval, pane.frames
            await pilot.pause(0.6)
            return streaming_interval, streamed, idle_interval, pane.frames - idle_frames

    streaming_interval, streamed, idle_interval, idle_delta = run(drive())
    assert streaming_interval == TerminalPane.FAST_INTERVAL
    assert streamed >= 4  # 0.4 s at 50 ms is ~8 frames; well above the idle rate
    assert idle_interval == TerminalPane.IDLE_INTERVAL
    assert 1 <= idle_delta <= 2  # 0.6 s at 500 ms: one frame, two at the edge


def test_a_hidden_pane_is_not_captured_and_catches_up_when_shown(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """The docstring's claim: a pane that is not on screen is not captured at all.

    It is what lets the shell keep one view per agent alive without forking N
    tmux processes 20 times a second (the shell reuses views across selections:
    ``assert views == 2  # one view per project, reused``). No test ever hid a
    pane, so nothing here covered it; the visible pane is the control.

    Measured while writing this: the property has TWO guards in the shell's own
    shape, and a test that names only one would mislead. ``_tick``'s
    ``is_on_screen`` refuses first, and a hidden ``ContentSwitcher`` child also
    has a ``content_size`` of 0x0, which ``refresh_frame`` refuses on its own —
    so removing either alone still captures nothing. Both are asserted.
    """
    fake.panes["%2"] = FakePane(screen=["second pane"])

    def taken(pane_id: str) -> int:
        return len([capture for capture in fake.captures if capture[0] == pane_id])

    async def drive() -> tuple[int, int, int, int, tuple[int, int], list[str]]:
        host = SwitcherHost(fake.server(tmp_path))
        async with host.run_test(size=(40, 6)) as pilot:
            await pilot.pause(0.6)
            second = host.query_one("#second", TerminalPane)
            shown_first, hidden_first = taken("%1"), taken("%2")
            hidden_state = (second.is_on_screen, second.content_size.area)
            host.tabs.current = "second"
            await pilot.pause(0.6)
            return (
                shown_first,
                hidden_first,
                taken("%1") - shown_first,
                taken("%2") - hidden_first,
                hidden_state,
                screen_text(second),
            )

    shown_first, hidden_first, first_after, second_after, hidden_state, text = run(drive())
    assert shown_first >= 2, "the VISIBLE pane must keep being captured (the control)"
    assert hidden_first == 0, "a hidden tab's pane is not captured at all"
    assert hidden_state == (False, 0)  # both guards say no while it is hidden
    assert first_after == 0  # …and the one that just went hidden stops
    assert second_after >= 2  # …while the one shown picks up where it was
    assert text[0] == "second pane"  # it caught up: on_show refreshed the frame


# --- input ----------------------------------------------------------------------------------


def test_escape_key_posts_the_message_and_is_never_forwarded(
    fake: FakeTmux, tmp_path: Path
) -> None:
    async def drive() -> tuple[int, list[tuple[str, ...]], int]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            pane.focus()
            await pilot.pause()
            await pilot.press("f12")
            await pilot.pause()
            escapes_after_hatch = host.escapes
            sent_after_hatch = fake.sent()
            # The negative: an ordinary key is forwarded and posts nothing.
            await pilot.press("enter")
            await pilot.pause()
            return escapes_after_hatch, sent_after_hatch, host.escapes

    escapes, sent, escapes_later = run(drive())
    assert escapes == 1
    assert sent == []
    assert escapes_later == 1
    assert fake.sent() == [("Enter",)]


def test_escape_key_is_configurable(fake: FakeTmux, tmp_path: Path) -> None:
    async def drive() -> int:
        host = Host(fake.server(tmp_path), "%1", escape_key="f9")
        async with host.run_test(size=(40, 6)) as pilot:
            host.pane.focus()
            await pilot.pause()
            await pilot.press("f12", "f9")
            await pilot.pause()
            return host.escapes

    assert run(drive()) == 1
    assert fake.sent() == [("F12",)]  # F12 is just a key once it is not the hatch


def test_alt_p_reaches_the_agent_as_meta_p_not_as_the_letter(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """Claude Code's alt+p switches the model; in the pane it typed ``p``.

    The event is posted the way Textual's parser builds it for ``ESC p`` —
    ``Key("alt+p", "p")``, character set — because ``pilot.press`` does not
    carry the character and would not reproduce the bug."""

    async def drive() -> None:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            host.pane.focus()
            await pilot.pause()
            host.pane.post_message(events.Key("alt+p", "p"))
            await pilot.pause()

    run(drive())
    assert fake.sent() == [("M-p",)], fake.sent()


def test_keys_are_forwarded_in_tmux_vocabulary(fake: FakeTmux, tmp_path: Path) -> None:
    async def drive() -> None:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            host.pane.focus()
            await pilot.pause()
            await pilot.press("a", "A", "space", "ctrl+c", "shift+tab", "up", "escape", "-")
            await pilot.pause()

    run(drive())
    assert fake.sent() == [
        ("-l", "--", "a"),
        ("-l", "--", "A"),
        ("-l", "--", " "),
        ("C-c",),
        ("BTab",),
        ("Up",),
        ("Escape",),
        ("-l", "--", "-"),  # literal text may start with '-'; the '--' protects it
    ]


def test_an_untranslatable_key_is_dropped_with_one_notice_per_key_name(
    fake: FakeTmux, tmp_path: Path
) -> None:
    async def drive() -> list[str]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            host.pane.focus()
            await pilot.pause()
            await pilot.press("f13", "f13", "f13")
            await pilot.press("ctrl+comma")  # a second unknown key: its own notice
            await pilot.press("enter")  # a known key: no notice
            await pilot.pause()
            return host.notices

    notices = run(drive())
    assert len(notices) == 2
    assert notices[0].startswith("f13") and notices[1].startswith("ctrl+comma")
    assert fake.sent() == [("Enter",)]  # nothing was mistyped into the agent


def test_keys_go_nowhere_while_the_pane_is_not_focused(fake: FakeTmux, tmp_path: Path) -> None:
    async def drive() -> tuple[bool, str]:
        host = Host(fake.server(tmp_path), "%1", with_input=True)
        async with host.run_test(size=(40, 8)) as pilot:
            other = host.query_one("#other", Input)
            other.focus()
            await pilot.pause()
            await pilot.press("a", "b")
            await pilot.pause()
            return host.pane.has_focus, other.value

    focused, typed = run(drive())
    assert not focused and typed == "ab"
    assert fake.sent() == []


def test_a_click_focuses_the_pane(fake: FakeTmux, tmp_path: Path) -> None:
    async def drive() -> tuple[bool, bool]:
        host = Host(fake.server(tmp_path), "%1", with_input=True)
        async with host.run_test(size=(40, 8)) as pilot:
            host.query_one("#other", Input).focus()
            await pilot.pause()
            before = host.pane.has_focus
            await click(pilot, host.pane, (1, 1))
            return before, host.pane.has_focus

    before, after = run(drive())
    assert not before and after


def test_paste_goes_through_the_paste_buffer_not_send_keys(fake: FakeTmux, tmp_path: Path) -> None:
    async def drive() -> None:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            pane.focus()
            await pilot.pause()
            pane.post_message(events.Paste("one\ntwo\n"))
            pane.post_message(events.Paste(""))  # nothing to paste: nothing sent
            await pilot.pause()

    run(drive())
    pasted = [call for call in fake.input if call[0] in ("load-buffer", "paste-buffer")]
    assert pasted == [("load-buffer", "one\ntwo\n"), ("paste-buffer", "%1")]
    assert fake.sent() == []  # the negative: no Enter per line


def test_a_literal_ending_in_the_separator_takes_the_paste_path(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """tmux reads an argument ending in ';' as a command separator (measured: sends nothing)."""

    async def drive() -> None:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            host.pane.focus()
            await pilot.pause()
            await pilot.press(";", "a")
            await pilot.pause()

    run(drive())
    assert ("load-buffer", ";") in fake.input and ("paste-buffer", "%1") in fake.input
    assert fake.sent() == [("-l", "--", "a")]


def test_wheel_scrolls_history_clamped_and_any_key_returns_to_live(
    fake: FakeTmux, tmp_path: Path
) -> None:
    fake.panes["%1"].history = [f"old {n}" for n in range(5)]

    async def drive() -> tuple[int, int, int, int, list[str]]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            pane.focus()
            await wait_until(pilot, lambda: synced(pane))
            pane.post_message(scroll_event(pane, up=True))
            await pilot.pause()
            one_notch = pane.scrollback
            text = screen_text(pane)
            for _ in range(3):
                pane.post_message(scroll_event(pane, up=True))
            await pilot.pause()
            clamped = pane.scrollback
            pane.post_message(scroll_event(pane, up=False))
            await pilot.pause()
            down = pane.scrollback
            await pilot.press("a")
            await pilot.pause()
            return one_notch, clamped, down, pane.scrollback, text

    one_notch, clamped, down, live, text = run(drive())
    assert one_notch == TerminalPane.WHEEL_LINES == 3
    assert text[:3] == ["old 2", "old 3", "old 4"]  # three history rows above the screen
    assert clamped == 5  # history_size, not 12
    assert down == 2
    assert live == 0
    assert ("%1", 3) in fake.captures and ("%1", 5) in fake.captures
    assert fake.captures[-1] == ("%1", 0)


def test_a_scrolled_frame_is_bounded_to_one_screen(fake: FakeTmux, tmp_path: Path) -> None:
    """§6: offset ``k`` asks for ``-S -k -E (H-1-k)``, not history-to-bottom.

    The claim is about the rows that cross the subprocess boundary and get
    split, so that is what is asserted — with the same frame fetched unbounded
    as the control, since a counter that can only report one number proves
    nothing.
    """
    fake.panes["%1"].history = [f"old {n}" for n in range(5000)]

    async def drive() -> tuple[list[int], list[str], int]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            fake.capture_rows.clear()  # the pre-resize frames are a 24-row pane
            pane.scroll_history(3000)
            await pilot.pause(0.2)  # several ticks, all of them deep in history
            return list(fake.capture_rows), screen_text(pane), pane.scrollback

    rows_per_capture, text, scrollback = run(drive())
    assert scrollback == 3000
    assert len(rows_per_capture) >= 2  # the loop kept capturing while scrolled
    assert set(rows_per_capture) == {6}  # every frame: one screen, not 3006 rows
    assert text[:2] == ["old 2000", "old 2001"]  # …and it is the RIGHT screen

    # The control: the same frame with no height hint — what the widget asked
    # for before the bound — pipes scrollback + height rows for the same screen.
    unbounded = fake.server(tmp_path).capture("%1", scrollback=3000)
    assert fake.capture_rows[-1] == 3000 + fake.panes["%1"].height == 3006
    assert unbounded.lines[:2] == text[:2]


def test_a_stale_height_hint_still_shows_a_full_screen(fake: FakeTmux, tmp_path: Path) -> None:
    """The bound's failure mode: the pane grew since the last frame.

    A hint smaller than the pane makes tmux answer short; ``core.tmux.capture``
    notices and refetches unbounded, so the user never sees a truncated screen —
    at the cost of one extra tmux process on that one frame.
    """
    fake.panes["%1"].history = [f"old {n}" for n in range(50)]

    async def drive() -> tuple[list[int], list[str]]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            assert pane.facts is not None
            pane.facts = replace(pane.facts, height=4)  # the hint goes stale-small
            fake.capture_rows.clear()
            pane.scroll_history(10)
            await pilot.pause()
            return list(fake.capture_rows), screen_text(pane)

    rows_per_capture, text = run(drive())
    assert rows_per_capture[:2] == [4, 16]  # the short answer, then the refetch
    assert text == ["old 40", "old 41", "old 42", "old 43", "old 44", "old 45"]


def _notch(widget: TerminalPane, *, up: bool, x: int = 4, y: int = 2) -> None:
    cls = events.MouseScrollUp if up else events.MouseScrollDown
    widget.post_message(cls(widget, x, y, 0, -1 if up else 1, 0, False, False, False))


def test_the_wheel_reaches_a_program_that_tracks_the_mouse_as_its_own_event(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """Claude Code's fullscreen TUI (``?1000`` + ``?1006`` + ``?1049``) scrolls its
    own transcript on the wheel. Reported 2026-09-08 as "scroll not working":
    this widget scrolled tmux's history — empty on the alternate screen — and
    the program never saw a notch. Now it gets the SGR events it asked for, at
    the pointer's cell, notches within one flush in ONE tmux call, and the
    history offset does not move."""
    pane = fake.panes["%1"]
    pane.alternate_on = pane.mouse_on = pane.mouse_sgr = True
    pane.history = [f"old {n}" for n in range(5)]

    async def drive() -> tuple[int, list[tuple[str, ...]]]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            widget = host.pane
            await wait_until(pilot, lambda: synced(widget))
            _notch(widget, up=True)
            _notch(widget, up=False)
            await pilot.pause(0.1)
            return widget.scrollback, [call for call in fake.input if call[0] == "send-keys"]

    scrollback, sent = run(drive())
    assert scrollback == 0, "the history offset is not what a mouse-tracking program wants"
    assert sent == [("send-keys", "%1", "-l", "--", "\x1b[<64;5;3M\x1b[<65;5;3M")], sent


def test_the_x10_encoding_goes_as_raw_bytes_because_a_string_cannot_carry_it(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """``chr(32 + column)`` is above 0x7f past column 95, and ``send-keys -l``
    re-emits a string as UTF-8 — measured: the byte arrived as two, the row byte
    became the column and the real row was typed as text. ``-H`` carries bytes."""
    pane = fake.panes["%1"]
    pane.alternate_on = pane.mouse_on = True
    pane.mouse_sgr = False
    pane.width = 200

    async def drive() -> list[tuple[str, ...]]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(140, 6)) as pilot:
            widget = host.pane
            await wait_until(pilot, lambda: synced(widget))
            _notch(widget, up=True, x=119, y=2)  # column 120: 32 + 120 = 0x98
            await pilot.pause(0.1)
            return [call for call in fake.input if call[0] == "send-keys"]

    sent = run(drive())
    assert sent == [("send-keys", "%1", "-H", "1b", "5b", "4d", "60", "98", "23")], sent


def test_the_wheel_on_a_plain_alternate_screen_sends_nothing_and_says_why(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """A fullscreen program that does not track the mouse has no history behind
    it, and arrow keys would land in its prompt — Claude Code's ``Up`` recalls a
    previous prompt. Nothing is sent; the user is told once."""
    pane = fake.panes["%1"]
    pane.alternate_on, pane.mouse_on = True, False

    async def drive() -> tuple[int, list[tuple[str, ...]], list[str]]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            widget = host.pane
            await wait_until(pilot, lambda: synced(widget))
            _notch(widget, up=True)
            _notch(widget, up=True)
            await pilot.pause(0.1)
            return widget.scrollback, fake.sent(), list(host.notices)

    scrollback, sent, notices = run(drive())
    assert scrollback == 0 and sent == []
    assert len([n for n in notices if "fullscreen" in n]) == 1, notices


def test_a_scrolled_view_comes_back_with_the_wheel_whatever_the_program_wants(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """History the widget has scrolled into is the widget's own: the wheel
    always returns it, or a Claude Code pane scrolled with the keyboard would
    freeze — every notch forwarded, the view never moving."""
    pane = fake.panes["%1"]
    pane.mouse_on = pane.mouse_sgr = True
    pane.history = [f"old {n}" for n in range(9)]

    async def drive() -> tuple[int, int, list[tuple[str, ...]]]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            widget = host.pane
            await wait_until(pilot, lambda: synced(widget))
            widget.scroll_history(6)
            await pilot.pause()
            scrolled = widget.scrollback
            _notch(widget, up=False)
            await pilot.pause(0.1)
            return scrolled, widget.scrollback, fake.sent()

    scrolled, after, sent = run(drive())
    assert scrolled == 6 and after == 3
    assert sent == [], "nothing was forwarded while the view was in history"


def test_the_wheel_in_tmux_copy_mode_uses_history_not_the_program(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """Forwarding a mouse event into copy mode cancels the mode and delivers
    nothing (measured on 3.7c); the pane belongs to tmux for the moment."""
    pane = fake.panes["%1"]
    pane.mouse_on = pane.mouse_sgr = pane.in_mode = True
    pane.history = [f"old {n}" for n in range(5)]

    async def drive() -> tuple[int, list[tuple[str, ...]]]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            widget = host.pane
            await wait_until(pilot, lambda: synced(widget))
            _notch(widget, up=True)
            await pilot.pause(0.1)
            return widget.scrollback, fake.sent()

    scrollback, sent = run(drive())
    assert scrollback == 3 and sent == []


def test_a_wheel_over_a_pane_that_just_died_fails_open(fake: FakeTmux, tmp_path: Path) -> None:
    """The module's contract: a pane that vanishes never takes the app down.
    The forwarding path reports ``(pane gone)`` like every other tmux call here."""
    pane = fake.panes["%1"]
    pane.alternate_on = pane.mouse_on = pane.mouse_sgr = True

    async def drive() -> tuple[str | None, list[str]]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            widget = host.pane
            await wait_until(pilot, lambda: synced(widget))
            pane.gone = True
            _notch(widget, up=True)
            await pilot.pause(0.1)
            return widget.notice, list(host.notices)

    notice, notices = run(drive())
    assert notice == "(pane gone)"
    assert any("pane gone" in n for n in notices)


def test_scroll_keys_move_history_and_never_reach_the_agent(fake: FakeTmux, tmp_path: Path) -> None:
    """The wheel is not a given (reported from WSL2 + Windows Terminal: "scroll
    not working"). shift+PgUp is what most terminals use for their own
    scrollback and some never forward it, so alt+PgUp does the same job."""
    fake.panes["%1"].history = [f"old {n}" for n in range(20)]

    async def drive() -> list[int]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            pane.focus()
            await wait_until(pilot, lambda: synced(pane))
            seen: list[int] = []
            for key in (
                "shift+pageup",
                "shift+home",
                "shift+pagedown",
                "alt+pagedown",
                "alt+pageup",
                "shift+end",
            ):
                await pilot.press(key)
                seen.append(pane.scrollback)
            return seen

    assert run(drive()) == [5, 20, 15, 10, 15, 0]
    assert fake.sent() == [], "none of the scroll keys was forwarded"


def test_scroll_keys_go_to_a_program_that_owns_its_own_transcript(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """On a Claude Code pane the keys must not pull stale pre-launch shell lines
    over the transcript: they take the wheel's route — the one scroll
    vocabulary such a program is known to speak — and history does not move."""
    pane = fake.panes["%1"]
    pane.alternate_on = pane.mouse_on = pane.mouse_sgr = True
    pane.history = [f"pre-launch shell line {n}" for n in range(20)]

    async def drive() -> tuple[int, list[tuple[str, ...]], list[str]]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            widget = host.pane
            widget.focus()
            await wait_until(pilot, lambda: synced(widget))
            await pilot.press("shift+pageup")
            await pilot.pause(0.1)
            return widget.scrollback, list(fake.input), screen_text(widget)

    scrollback, calls, text = run(drive())
    assert scrollback == 0, "tmux history is not this program's transcript"
    sent = [c for c in calls if c[0] == "send-keys"]
    assert sent == [("send-keys", "%1", "-l", "--", "\x1b[<64;21;4M")], sent
    assert not any("pre-launch" in row for row in text)


def test_a_scrolled_pane_shows_its_position_in_the_corner_and_keeps_it_current(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """A scrolled pane looked identical to a quiet live one. tmux's own marker,
    in tmux's own corner — and it tracks history that keeps growing under a
    frozen view (the denominator moves), then leaves with the offset."""
    pane = fake.panes["%1"]
    pane.history = [f"old {n}" for n in range(5)]

    async def drive() -> tuple[str, str, str]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            widget = host.pane
            widget.focus()
            await wait_until(pilot, lambda: synced(widget))
            widget.post_message(scroll_event(widget, up=True))
            await pilot.pause()
            scrolled = rows(widget)[0].text
            pane.history.append("old 5")  # the agent kept printing
            await wait_until(pilot, lambda: rows(widget)[0].text.endswith("[↑3/6]"))
            grown = rows(widget)[0].text
            await pilot.press("a")
            await pilot.pause()
            return scrolled, grown, rows(widget)[0].text

    scrolled, grown, live = run(drive())
    assert scrolled.endswith("[↑3/5]"), scrolled
    assert grown.endswith("[↑3/6]"), grown
    assert "[↑" not in live


def test_drag_select_highlights_the_rows_and_copies_on_release(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """Reported: "not able to select and copy text" — an agent printed a command
    and there was no way to take it. The drag is the request to copy."""

    async def drive() -> tuple[str | None, str, Style, Style, list[str]]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            # The cell under the pointer at release is included, as in every
            # terminal: a drag from column 0 to column 5 takes six characters.
            await drag(pilot, pane, (0, 1), (5, 1))
            row = rows(pane)[1]
            return (
                pane.selected_text(),
                host.clipboard,
                style_at(row, 2),
                style_at(row, 8),
                list(host.notices),
            )

    selected, clipboard, inside, outside, notices = run(drive())
    assert selected == "second"
    assert clipboard == "second", "copied on release, without a key"
    assert inside.bgcolor != outside.bgcolor, "the selected span is painted, the rest is not"
    # Reported 2026-09-10 with a screenshot: a solid block where the word was.
    # The theme's selection style resolves foreground == background; only its
    # background may be applied, and the text must stay the colour it was.
    assert inside.color == outside.color, "selecting must tint behind the text, not recolour it"
    assert inside.color != inside.bgcolor, "the selected text is still legible"
    assert any(n.startswith("copied 6 characters") for n in notices), notices


def test_ctrl_c_copies_a_selection_and_interrupts_the_agent_otherwise(
    fake: FakeTmux, tmp_path: Path
) -> None:
    async def drive() -> tuple[str, list[tuple[str, ...]], list[tuple[str, ...]]]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            await drag(pilot, pane, (0, 2), (4, 2))
            pane.focus()
            await pilot.press("ctrl+c")
            await pilot.pause()
            with_selection = list(fake.sent())
            copied = host.clipboard
            assert pane.text_selection is None, "ctrl+c copied and cleared the selection"
            await pilot.press("ctrl+c")
            await pilot.pause()
            return copied, with_selection, list(fake.sent())

    copied, with_selection, after = run(drive())
    assert copied == "third"
    assert with_selection == [], "with text selected, ctrl+c is copy, not the agent's interrupt"
    assert after == [("C-c",)], "without a selection it reaches the agent as before"


def test_a_run_of_spaces_the_user_selected_is_copied(fake: FakeTmux, tmp_path: Path) -> None:
    """The mirror of the blank-row guard, and the reason it tests emptiness
    rather than blankness. Column-aligned agent output is full of real
    whitespace — a gap in `ls -l`, an indent, a diff gutter — and refusing it
    left the highlight painted, no toast at all, and the user's follow-up ctrl+c
    killing the agent instead of copying (review of the tenth version)."""
    fake.panes["%1"].screen = ["col1      col2", "x", "y"]

    async def drive() -> tuple[str, int, list[tuple[str, ...]]]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            await drag(pilot, pane, (4, 0), (9, 0))  # the gap between the columns
            pane.focus()
            await pilot.press("ctrl+c")
            await pilot.pause()
            return host.clipboard, len(host.notices), list(fake.sent())

    clipboard, toasts, sent = run(drive())
    assert clipboard == "      ", f"six spaces, selected and copied: {clipboard!r}"
    assert toasts == 2, "the drag copied, and ctrl+c copied again"
    assert sent == [], "ctrl+c was the copy, not the interrupt, while it stood"


@pytest.mark.parametrize(
    ("start", "end", "button"),
    [
        ((1, 4), (10, 6), 1),  # three rows nothing printed on — round 11's own
        ((12, 1), (20, 1), 1),  # the blank past the end of "second row"
        ((1, 4), (10, 6), 3),  # the same rows, right button: not a copy either way
    ],
)
def test_a_drag_over_nothing_but_blank_cells_copies_nothing_and_leaves_no_highlight(
    fake: FakeTmux, tmp_path: Path, start: tuple[int, int], end: tuple[int, int], button: int
) -> None:
    """Review of #120, round 11: a drag over rows nothing was printed on was
    tinted full width, copied nothing and said nothing — and the ctrl+c the
    highlight invited went to the agent as its interrupt. The highlight now goes
    at the release and a left drag is told "nothing to copy", so whatever
    stands is something ctrl+c copies.

    The paint is read off the rendered strips, against an unselected baseline:
    ``test_what_is_copied_is_exactly_what_is_painted`` compares the TEXT under
    the tint, which is ``""`` on both sides here, and cannot see this. Several
    blank rows, not one: two extract as ``"\\n"`` before the trailing newlines
    go, and the copy once reported success over empty space and swallowed
    ctrl+c (review of the ninth version) — the interrupt is still asserted."""

    async def drive() -> tuple[list[int], list[int], bool, str, list[str], list[tuple[str, ...]]]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 8)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            width = pane.content_size.width
            base = [[style_at(strip, x).bgcolor for x in range(width)] for strip in rows(pane)]

            def tinted() -> list[int]:
                return [
                    y
                    for y, strip in enumerate(rows(pane))
                    if any(style_at(strip, x).bgcolor != base[y][x] for x in range(width))
                ]

            await press(pilot, pane, start, button=button)
            await move(pilot, pane, end, button=button)
            during = tinted()
            await release(pilot, pane, end, button=button)
            await pilot.pause()
            after = tinted()
            standing = pane.text_selection is not None
            pane.focus()
            await pilot.press("ctrl+c")
            await pilot.pause()
            return during, after, standing, host.clipboard, list(host.notices), list(fake.sent())

    during, after, standing, clipboard, notices, sent = run(drive())
    assert during == list(range(start[1], end[1] + 1)), "the premise: the drag tints them"
    assert after == [] and not standing, "nothing under it to copy, so no highlight stands"
    assert clipboard == "", "nothing was copied"
    if button == 1:
        assert len(notices) == 1 and notices[0].startswith("nothing to copy"), notices
    else:
        assert notices == [], "a right-button drag asked for no copy, and is told of none"
    assert sent == [("C-c",)], "ctrl+c after it is the agent's interrupt"


def test_the_scroll_marker_is_measured_in_cells_not_characters(
    fake: FakeTmux, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``cut`` and ``gap`` are cell counts, fed to ``Strip.crop`` and to the
    text composition. Measuring the marker in characters agreed by luck — ``↑``
    is one cell — and would overflow the corner on any marker whose two measures
    differ, splitting the paint from the copy on row 0 (review of the ninth)."""
    monkeypatch.setattr(TerminalPane, "SCROLL_MARKER_TEMPLATE", "[日本{scrollback}/{history}]")
    pane_fake = fake.panes["%1"]
    pane_fake.history = [f"old {n}" for n in range(5)]

    async def drive() -> tuple[int, str, tuple[int, int] | None, str]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            widget = host.pane
            widget.focus()
            await wait_until(pilot, lambda: synced(widget))
            widget.post_message(scroll_event(widget, up=True))
            await wait_until(pilot, lambda: widget.scrollback > 0)
            text = widget._displayed_row(0).text
            base = [style_at(rows(widget)[0], x).bgcolor for x in range(40)]
            widget.screen.selections = {widget: Selection(Offset(30, 0), Offset(40, 0))}
            await pilot.pause()
            strip = rows(widget)[0]
            cells = [x for x in range(40) if style_at(strip, x).bgcolor != base[x]]
            span = (min(cells), max(cells) + 1) if cells else None
            return strip.cell_length, text, span, widget.selected_text() or ""

    painted_cells, text, tinted, copied = run(drive())
    assert painted_cells == 40, "the row still fills the pane exactly"
    assert cell_len(text) == 40, f"and the text lines up cell for cell: {text!r}"
    # The harm this guards is a paint/copy split ON ROW 0, so the test has to
    # select there. A marker composed at a character offset lands in the wrong
    # cells and these two stop agreeing (review of the tenth version).
    assert tinted == (30, 40), f"the last ten cells are tinted: {tinted}"
    assert text.endswith(copied), f"and what is copied is that tail of the row: {copied!r}"
    assert cell_len(copied) == 10, f"ten cells tinted, ten cells copied: {copied!r}"
    assert "日本" in copied, "including the wide glyphs the marker is made of"


@pytest.mark.parametrize("width", [8, 9], ids=["between-its-characters-and-cells", "its-cells"])
def test_a_marker_the_pane_has_no_room_for_is_not_drawn(
    fake: FakeTmux, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, width: int
) -> None:
    """Review of #120, round 11: the guard in ``_marker_layout`` survived both
    mutations — ``marker_cells > width`` and ``len(marker) >= width`` — because
    the wide marker was only ever drawn on a 40-cell pane, where both measures
    are below the width. ``[日本3/5]`` is 7 characters and 9 cells. Counted in
    characters, an 8-cell pane composed it at a negative gap, a 13-cell row text
    over an 8-cell strip: the paint and the copy split on row 0. At exactly 9
    cells it would be the whole row. Either way it is not drawn, and row 0 is
    the row, painted and copied the same."""
    monkeypatch.setattr(TerminalPane, "SCROLL_MARKER_TEMPLATE", "[日本{scrollback}/{history}]")
    pane_fake = fake.panes["%1"]
    pane_fake.history = [f"old {n}" for n in range(5)]

    async def drive() -> tuple[str, int, str, str]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(width, 6)) as pilot:
            widget = host.pane
            widget.focus()
            await wait_until(pilot, lambda: synced(widget))
            widget.post_message(scroll_event(widget, up=True))
            await wait_until(pilot, lambda: widget.scrollback > 0)
            marker = TerminalPane.SCROLL_MARKER_TEMPLATE.format(
                scrollback=widget.scrollback, history=widget.history_size
            )
            text = widget._displayed_row(0).text
            widget.screen.selections = {widget: Selection(Offset(0, 0), Offset(width, 0))}
            await pilot.pause()
            return marker, rows(widget)[0].cell_length, text, widget.selected_text() or ""

    marker, painted_cells, text, copied = run(drive())
    assert len(marker) < width <= cell_len(marker), f"the premise: {marker!r} on {width} cells"
    assert "日本" not in text and text.startswith("old "), f"the row, not the marker: {text!r}"
    assert painted_cells == width, "the row still fills the pane exactly"
    assert copied == text, f"row 0 copies what it shows: {copied!r} vs {text!r}"


def test_a_span_starting_outside_the_row_paints_and_copies_the_same_cells(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """Review of #120, round 11 asked whether ``min(max(start, 0), width)`` in
    ``_with_selection`` could ever bind. The upper half binds on a selection left
    from a wider pane and changed nothing — the row is skipped either way — and
    the lower half binds only on a negative column, which Textual's compositor
    never writes and ``Strip.crop`` starts at 0 regardless. The clamp is gone;
    this pins what it stood for, both shapes: paint and copy agree."""

    async def drive() -> list[tuple[list[int], str]]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            base = [style_at(rows(pane)[1], x).bgcolor for x in range(40)]
            seen = []
            for selection in (
                Selection(Offset(-3, 1), Offset(4, 1)),  # before the first cell
                Selection(Offset(45, 1), Offset(50, 1)),  # past the widget's width
                Selection(Offset(45, 1), Offset(6, 2)),  # past it, then the next row
            ):
                pane.screen.selections = {pane: selection}
                await pilot.pause()
                strip = rows(pane)[1]
                tinted = [x for x in range(40) if style_at(strip, x).bgcolor != base[x]]
                seen.append((tinted, pane.selected_text() or ""))
            return seen

    before_the_row, past_the_row, past_then_down = run(drive())
    assert before_the_row == ([0, 1, 2, 3], "seco"), before_the_row
    assert past_the_row == ([], ""), "a span past the width tints nothing and copies nothing"
    assert past_then_down == ([], "third "), "the row it starts past contributes nothing"


def test_a_drag_below_the_output_neither_crashes_nor_selects_everything(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """Reproduced in review: ``Selection.extract`` re-splits with ``splitlines``,
    drops the trailing empty row, and a drag in the blank area under three rows
    of output raised IndexError out of the mouse handler — the whole UI down.
    The selection is read before the release, which drops a highlight with
    nothing under it (review of #120, round 11)."""

    async def drive() -> tuple[str | None, str, Selection | None]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            await press(pilot, pane, (0, 5))
            await move(pilot, pane, (4, 5), button=1)
            selected, selection = pane.selected_text(), pane.text_selection
            await release(pilot, pane, (4, 5))
            return selected, host.clipboard, selection

    selected, clipboard, selection = run(drive())
    assert selected is None and clipboard == ""
    assert selection is not None and selection.start is not None, "not a select-all"


def test_double_click_selects_a_word_and_triple_click_nothing(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """Textual's defaults select the whole widget on a double click and the whole
    container on a triple; the next ctrl+c — the agent's interrupt — would then
    copy the entire screen (reproduced in review). A word is what was meant."""

    async def drive() -> tuple[str | None, str, str | None]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            await click(pilot, pane, (8, 1), times=2)
            word, clip = pane.selected_text(), host.clipboard
            await click(pilot, pane, (1, 0), times=3)
            return word, clip, pane.selected_text()

    word, clip, after_triple = run(drive())
    assert word == "row" and clip == "row"
    assert after_triple is None


def test_a_drag_into_the_notice_row_stays_a_drag(fake: FakeTmux, tmp_path: Path) -> None:
    """Every row the compositor asks ``render_line`` for is offset-stamped: a
    drag that ends on the ``(exited)`` row used to resolve to select-all
    because that row was not."""
    pane = fake.panes["%1"]
    pane.dead, pane.dead_status = True, 0

    async def drive() -> tuple[Selection | None, str | None]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            widget = host.pane
            # Synced too: a highlight made over the blank rows of a not-yet-resized
            # pane is dropped by the frame that fills them (class docstring, rule 3).
            await wait_until(pilot, lambda: synced(widget) and widget.notice == "(exited 0)")
            await drag(pilot, widget, (0, 1), (3, 5))
            return widget.text_selection, widget.selected_text()

    selection, text = run(drive())
    assert selection is not None and selection.start == Offset(0, 1)
    assert text is not None and text.startswith("second row") and text.endswith("(exi")


def test_wide_glyphs_paint_and_copy_the_same_cells(fake: FakeTmux, tmp_path: Path) -> None:
    """Offsets are characters, cells are cells: on ``日本語abcdef`` the highlight
    was shifted a column per wide glyph (reproduced in review)."""
    fake.panes["%1"].screen = ["日本語abcdef", "second row"]

    async def drive() -> tuple[str | None, list[bool]]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane) and "abcdef" in rows(pane)[0].text)
            await drag(pilot, pane, (6, 0), (11, 0))
            row = rows(pane)[0]
            tint = pane._selection_bg
            assert tint is not None
            return pane.selected_text(), [
                style_at(row, x).bgcolor == tint.bgcolor for x in range(12)
            ]

    selected, painted = run(drive())
    assert selected == "abcdef"
    assert painted == [False] * 6 + [True] * 6, painted


def test_the_copy_and_the_highlight_always_agree(fake: FakeTmux, tmp_path: Path) -> None:
    """Round 1 asked for the rows to be frozen when a drag began, so a copy could
    not pick up output printed during the gesture, and this test pinned that.

    THAT SNAPSHOT IS GONE, deliberately. It could not hold the property it was
    for: the strip is always built from the live rows, so a frozen text made the
    copy DISAGREE with the paint rather than agree with it. Both sides read the
    live rows, so a drag over a printing agent copies the row as painted at
    release — the text the user can see highlighted at that moment.

    What happens AFTER the release is the other half, and it changed in the
    review of #135 (finding 2): the highlight does not outlive its text. Once
    the agent prints something else under it, it is gone, and ctrl+c is the
    interrupt again — ``test_output_printed_under_the_highlight_drops_it`` pins
    that with its negative halves."""

    async def drive() -> tuple[str, str]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            await press(pilot, pane, (0, 1))
            await move(pilot, pane, (3, 1), button=1)
            fake.panes["%1"].screen = ["red plain", "MOVED UNDER", "third row"]
            await wait_until(pilot, lambda: "MOVED" in pane._lines[1])
            await move(pilot, pane, (5, 1), button=1)
            await release(pilot, pane, (5, 1))
            return host.clipboard, rows(pane)[1].text[:6]

    on_release, painted = run(drive())
    assert on_release == painted == "MOVED ", "the copy is the row as painted at release"


def test_output_printed_under_the_highlight_drops_it(fake: FakeTmux, tmp_path: Path) -> None:
    """Finding 2 of the #135 review: nothing dropped a standing selection, so the
    first ctrl+c pressed later to stop the agent was swallowed by the copy
    intercept, which copied whatever text now sat under the old highlight (never
    selected) and sent no ``C-c``. A highlight means "this text"; when the text
    changes the highlight goes, and ctrl+c is the interrupt again.

    Two negative halves, because the rule is "the text UNDER it", not "any
    output": Claude Code redraws its status line several times a second, so a
    change on another row, or on the same row outside the span, must leave the
    highlight standing."""

    async def drive() -> tuple[
        Selection | None, Selection | None, Selection | None, str, list[tuple[str, ...]]
    ]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            await drag(pilot, pane, (0, 2), (4, 2))
            assert host.clipboard == "third", "the premise: the drag copied"
            fake.panes["%1"].screen = ["SPINNER ticks", "second row", "third row"]
            await wait_until(pilot, lambda: "SPINNER" in pane._lines[0])
            other_row = pane.text_selection
            fake.panes["%1"].screen = ["SPINNER ticks", "second row", "third XYZ"]
            await wait_until(pilot, lambda: "XYZ" in pane._lines[2])
            same_row_outside = pane.text_selection
            fake.panes["%1"].screen = ["SPINNER ticks", "second row", "DELETING files"]
            await wait_until(pilot, lambda: "DELETING" in pane._lines[2])
            under = pane.text_selection
            pane.focus()
            await pilot.press("ctrl+c")
            await pilot.pause()
            return other_row, same_row_outside, under, host.clipboard, list(fake.sent())

    other_row, same_row_outside, under, clipboard, sent = run(drive())
    assert other_row is not None, "a change on another row leaves the highlight"
    assert same_row_outside is not None, "so does a change on the same row outside the span"
    assert under is None, "the text under the highlight changed: the highlight is gone"
    assert sent == [("C-c",)], "so ctrl+c is the agent's interrupt"
    assert clipboard == "third", "and nothing that was never selected reached the clipboard"


def test_typing_into_the_agent_drops_the_highlight(fake: FakeTmux, tmp_path: Path) -> None:
    """Finding 2, the input half: a key or a paste forwarded to the agent means
    the highlight is stale — the user has moved on — and the ctrl+c that follows
    must be the interrupt. Before, ``run`` + Enter reached tmux with the
    selection still standing, and the ctrl+c meant to stop the agent copied."""

    async def drive() -> tuple[Selection | None, Selection | None, list[tuple[str, ...]]]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            await drag(pilot, pane, (0, 2), (4, 2))
            pane.focus()
            await pilot.press("r")
            await pilot.pause()
            after_key = pane.text_selection
            await drag(pilot, pane, (0, 2), (4, 2))
            assert pane.text_selection is not None, "the premise: a second highlight stands"
            pane.post_message(events.Paste("ls\n"))
            await pilot.pause()
            after_paste = pane.text_selection
            fake.input.clear()
            await pilot.press("ctrl+c")
            await pilot.pause()
            return after_key, after_paste, list(fake.sent())

    after_key, after_paste, sent = run(drive())
    assert after_key is None, "a key forwarded to the agent drops the highlight"
    assert after_paste is None, "so does a paste"
    assert sent == [("C-c",)], "and the next ctrl+c is the interrupt, not a copy"


def test_attach_to_another_pane_drops_the_selection(fake: FakeTmux, tmp_path: Path) -> None:
    fake.panes["%2"] = FakePane(screen=["other agent"], cursor=(0, 0))

    async def drive() -> Selection | None:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            await drag(pilot, pane, (0, 1), (5, 1))
            assert pane.text_selection is not None
            pane.attach("%2")
            await pilot.pause()
            return pane.text_selection

    assert run(drive()) is None


def test_a_theme_change_reaches_quiet_rows_and_a_standing_highlight(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """Anything holding a resolved colour has to be dropped when the theme
    changes. A cache of finished rows had to be (and was measured not to be
    worth keeping); ``_selection_bg`` memoises the highlight's background and
    was reset only when the selection cleared, so a theme picked mid-drag
    repainted every row around a highlight still tinted from the old palette —
    possibly invisible against it (review)."""

    async def drive() -> tuple[Style, Style, Style, Style]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            await drag(pilot, pane, (0, 1), (5, 1))
            quiet_before = style_at(rows(pane)[2], 0)
            tint_before = style_at(rows(pane)[1], 2)
            host.theme = "textual-light"
            await pilot.pause()
            return (
                quiet_before,
                style_at(rows(pane)[2], 0),
                tint_before,
                style_at(rows(pane)[1], 2),
            )

    quiet_before, quiet_after, tint_before, tint_after = run(drive())
    assert quiet_before.bgcolor != quiet_after.bgcolor, "a quiet row kept the old theme"
    assert tint_before.bgcolor != tint_after.bgcolor, "the highlight kept the old palette"


def test_cmd_c_copies_a_selection_and_types_nothing_without_one(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """macOS Cmd+C. Dropping it from the copy branch sent it to the key table
    instead, which reads the reported printable ``c`` — so Cmd+C copied nothing
    and typed a stray ``c`` into Claude Code's prompt (review). The event is
    posted as the parser builds it, character set; ``pilot.press`` carries none."""

    async def drive() -> tuple[str, list[tuple[str, ...]], list[tuple[str, ...]]]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            await drag(pilot, pane, (0, 2), (4, 2))
            pane.focus()
            pane.post_message(events.Key("super+c", "c"))
            await pilot.pause()
            copied, with_selection = host.clipboard, list(fake.sent())
            assert pane.text_selection is None, "cmd+c copied and cleared the selection"
            pane.post_message(events.Key("super+c", "c"))
            await pilot.pause()
            return copied, with_selection, list(fake.sent())

    copied, with_selection, after = run(drive())
    assert copied == "third"
    assert with_selection == [], "cmd+c is the copy, never a keystroke for the agent"
    assert after == [], "and with nothing selected it types nothing — not a bare `c`"


def test_only_the_drag_that_made_a_selection_copies_it(fake: FakeTmux, tmp_path: Path) -> None:
    """Copying on any release while a selection stood meant a right-button drag
    over the highlight replaced the clipboard with whatever it happened to cross
    — reproduced in review: ``third `` became ``hir``."""

    async def drive() -> tuple[str, int, str, int]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            await drag(pilot, pane, (0, 2), (5, 2))
            dragged, toasts = host.clipboard, len(host.notices)
            # The right button comes down on the highlight and is released
            # elsewhere, so Textual does not clear the selection first.
            await press(pilot, pane, (1, 2), button=3)
            await move(pilot, pane, (3, 2), button=3)
            await release(pilot, pane, (3, 2), button=3)
            await pilot.pause()
            return dragged, toasts, host.clipboard, len(host.notices)

    dragged, toasts, after, toasts_after = run(drive())
    assert dragged == "third " and toasts == 1
    assert after == dragged, "a release that is not the left drag's leaves the clipboard alone"
    assert toasts_after == toasts, "and says nothing about a copy it did not make"


def test_a_drag_on_rows_the_last_frame_did_not_fill_copies_nothing(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """``render_line`` stamps offsets for every row the widget SHOWS, but
    ``_lines`` is re-padded to that height only by the next successful frame. In
    the gap — a grow-resize, or a capture that failed — the extraction clamped a
    drag on the new bottom rows onto the LAST row's text and copied that."""

    async def drive() -> tuple[str | None, str | None]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            pane._lines = pane._lines[:3]  # the frame a grow-resize has outgrown
            bottom = pane.get_selection(Selection(Offset(0, 5), Offset(6, 5)))
            filled = pane.get_selection(Selection(Offset(0, 2), Offset(5, 2)))
            return (bottom[0] if bottom else None), (filled[0] if filled else None)

    bottom, filled = run(drive())
    assert bottom == "", "an unfilled row holds no text — least of all the last row's"
    assert filled == "third", "the rows that are filled copy as before"


def test_the_cursor_is_one_cell_even_past_the_end_of_the_row(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """Snapping the cursor span to glyph boundaries widened it to everything
    between the trimmed text and the cursor: `capture-pane -e` trims trailing
    spaces, so every quiet shell pane whose cursor is not flush against the text
    showed a black bar instead of a cell (review)."""

    def reverse_cells(screen: list[str], cursor: tuple[int, int]) -> list[int]:
        # Through the fake pane, not by poking `_cursor`/`_lines`: the render
        # loop rewrites both every 50 ms, and setting them by hand raced it.
        fake.panes["%1"] = FakePane(screen=screen, cursor=cursor)

        async def drive() -> list[int]:
            host = Host(fake.server(tmp_path), "%1")
            async with host.run_test(size=(40, 6)) as pilot:
                pane = host.pane
                await wait_until(pilot, lambda: synced(pane))
                pane.focus()
                await pilot.pause()
                return [x for x in range(40) if style_at(rows(pane)[0], x).reverse]

        return run(drive())

    past = reverse_cells(["hello", "", ""], (20, 0))
    wide = reverse_cells(["日本語ab", "", ""], (2, 0))
    assert past == [20], "one cell, at the cursor — not a bar back to the text"
    assert wide == [2, 3], "and still both cells of a wide glyph it sits on"


def test_a_click_is_handled_once_and_focuses_the_pane(
    fake: FakeTmux, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Textual resolves ``_on_click`` OR ``on_click`` per class in the MRO, never
    both — so a separate ``on_click`` holding ``self.focus()`` was never called,
    and calling ``super()._on_click`` from the other handler brokered every plain
    click twice, since the dispatch loop runs the base class itself (review)."""
    assert "on_click" not in vars(TerminalPane), "one handler, not a dead one beside it"
    brokered: list[str] = []
    original = Widget.broker_event

    async def counted(self: Widget, event_name: str, event: events.Event) -> bool:
        if event_name == "click" and isinstance(self, TerminalPane):
            brokered.append(event_name)
        return await original(self, event_name, event)

    monkeypatch.setattr(Widget, "broker_event", counted)

    async def drive() -> bool:
        host = Host(fake.server(tmp_path), "%1", with_input=True)
        async with host.run_test(size=(40, 8)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            host.query_one("#other", Input).focus()
            await pilot.pause()
            brokered.clear()
            await click(pilot, pane, (1, 1))
            await pilot.pause()
            return pane.has_focus

    focused = run(drive())
    assert focused, "the click focuses the pane"
    assert len(brokered) == 1, brokered


def test_the_notice_row_is_highlighted_by_the_same_drag_that_copies_it(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """The displayed row reports the notice as its text, so a drag copies it —
    while the early return that built the notice strip skipped every overlay, so
    it was the one row a selection never tinted (review)."""

    dead = fake.panes["%1"]
    dead.dead, dead.dead_status = True, 0

    async def drive() -> tuple[str | None, Style, Style]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane) and pane.notice == "(exited 0)")
            await drag(pilot, pane, (0, 5), (4, 5))
            row = rows(pane)[5]
            return pane.selected_text(), style_at(row, 2), style_at(row, 8)

    copied, inside, outside = run(drive())
    assert copied == "(exit", "the cell under the pointer at release is included"
    assert inside.bgcolor != outside.bgcolor, "what is copied is what is painted"


@pytest.mark.parametrize("crossing", ["in", "out"])
def test_a_drag_across_the_panes_edge_copies_in_either_direction(
    fake: FakeTmux, tmp_path: Path, crossing: str
) -> None:
    """A gesture that begins or ends outside the pane still copies what it left
    highlighted there.

    It used to depend on the neighbour. The pane copied from its own
    ``on_mouse_up``, and whether that arrived was decided by whoever sat next to
    it: an ``Input`` calls ``capture_mouse()`` so it never came, while the
    ``Static`` header ``AgentView`` actually uses does not, so it did — and a
    leftover press offset then decided whether it copied, which made the SAME
    gesture nondeterministic. The end of a gesture is a screen fact, and the app
    routes it here (review of the sixth version)."""

    async def drive() -> tuple[str, int]:
        host = Host(fake.server(tmp_path), "%1", with_header=True)
        async with host.run_test(size=(40, 8)) as pilot:
            pane = host.pane
            other = host.query_one("#other", Static)
            await wait_until(pilot, lambda: synced(pane))
            if crossing == "in":
                await press(pilot, other, (1, 0))
                await move(pilot, pane, (5, 1), button=1)
                await release(pilot, pane, (5, 1))
            else:
                await press(pilot, pane, (0, 1))
                await move(pilot, pane, (5, 1), button=1)
                await release(pilot, other, (1, 0))
            await pilot.pause()
            return host.clipboard, len(host.notices)

    copied, toasts = run(drive())
    # Exact, not just non-empty: an off-by-one, a wrong row or a leftover
    # clipboard all satisfy "not empty" (review of the eighth version). The two
    # directions differ because Textual orders the endpoints — a drag released
    # ABOVE its press ends at the press offset, so `out` stops at column 0 of
    # the row it started on.
    # `out` stops at the press offset — Textual orders the endpoints, and this
    # drag is released ABOVE its press — and carries no trailing newline for the
    # row its zero-width span leaves untinted (review of the tenth version).
    assert copied == ("red plain\nsecon" if crossing == "in" else "red plain"), copied
    assert toasts == 1, "and said so exactly once"


def test_the_same_cross_widget_gesture_copies_the_same_way_every_time(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """Nothing is carried between gestures, so the result cannot depend on what
    the user did before. Measured in review: the identical drag gave ``''`` then
    ``'red plain\nsecond'`` because a press offset from an earlier gesture was
    still set."""

    async def drive() -> list[str]:
        host = Host(fake.server(tmp_path), "%1", with_header=True)
        async with host.run_test(size=(40, 8)) as pilot:
            pane = host.pane
            other = host.query_one("#other", Static)
            await wait_until(pilot, lambda: synced(pane))
            # A drag pressed inside and released outside comes first: that is
            # the one that used to leave state behind.
            await press(pilot, pane, (0, 2))
            await move(pilot, pane, (5, 2), button=1)
            await release(pilot, other, (1, 0))
            await pilot.pause()
            host.screen.clear_selection()
            await pilot.pause()
            results = []
            for _ in range(2):
                await press(pilot, other, (1, 0))
                await move(pilot, pane, (5, 1), button=1)
                await release(pilot, pane, (5, 1))
                await pilot.pause()
                results.append(host.clipboard)
                host.screen.clear_selection()
                await pilot.pause()
            return results

    first, second = run(drive())
    assert first == second == "red plain\nsecon", (first, second)


def test_a_right_button_drag_across_a_highlight_still_leaves_the_clipboard_alone(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """Round 3's finding, re-pinned against the new end-of-gesture hook: only the
    left button asks for a copy, and the pane knows which button began a gesture
    it saw the press of."""

    async def drive() -> tuple[str, str, int]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            await drag(pilot, pane, (0, 2), (5, 2))
            dragged, toasts = host.clipboard, len(host.notices)
            await press(pilot, pane, (1, 2), button=3)
            await move(pilot, pane, (3, 2), button=3)
            await release(pilot, pane, (3, 2), button=3)
            await pilot.pause()
            return dragged, host.clipboard, len(host.notices) - toasts

    dragged, after, new_toasts = run(drive())
    assert dragged == "third "
    assert after == dragged, "a right-button drag is not a copy request"
    assert new_toasts == 0


def test_a_backwards_selection_copies_nothing_because_nothing_is_painted() -> None:
    """``Selection.get_span`` reorders nothing: for ``start.y > end.y`` it
    returns ``None`` on every row, so the paint covers nothing and the copy must
    be empty too. Textual normalises through ``Selection.from_offsets``, so the
    shape is unreachable through the UI — pinned so the next reader inherits the
    fact rather than re-deriving it, and so ``_extract`` cannot quietly start
    answering for a highlight that does not exist (review of the ninth)."""
    rows = [DisplayedRow(text) for text in ("aaa", "bbb", "ccc")]
    backwards = Selection(Offset(1, 2), Offset(1, 0))
    assert all(backwards.get_span(y) is None for y in range(len(rows))), "the premise"
    assert _extract(backwards, rows, 3) == ""
    # The negative half: ordered, it is the span the paint does cover.
    forwards = Selection(Offset(1, 0), Offset(1, 2))
    assert [forwards.get_span(y) for y in range(3)] == [(1, -1), (0, -1), (0, 1)]
    assert _extract(forwards, rows, 3) == "aa\nbbb\nc"


def test_what_is_copied_is_exactly_what_is_painted(fake: FakeTmux, tmp_path: Path) -> None:
    """copy == paint, read off the RENDERED strips rather than off ``get_span``.

    An earlier version of this asserted ``_extract`` against a re-implementation
    of its own slicing line, so anything the two misunderstood together survived
    — and something did: a zero-width span is not ``None``, contributed an empty
    piece to both sides, and agreed, while the row it names is left untinted
    (review of the tenth version). Diffing each row's cells against an unselected
    baseline sees the paint itself, so the character-to-cell conversion, the
    glyph snapping and the corner marker are all in scope — which is where
    rounds 3 to 5 found real splits.
    """
    fake.panes["%1"].screen = ["日本語abcdef", "second row", "", "tail 🎉 end"]

    async def drive() -> list[tuple[str, str]]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(20, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            width = pane.content_size.width
            base = [[style_at(strip, x).bgcolor for x in range(width)] for strip in rows(pane)]
            offsets = [Offset(x, y) for x in (0, 1, 6, 11, 25) for y in (-1, 0, 2, 3, 8)]
            pairs: list[tuple[str, str]] = []
            for first in offsets:
                for second in offsets:
                    pane.screen.selections = {pane: Selection.from_offsets(first, second)}
                    await pilot.pause()
                    pieces = []
                    for y, strip in enumerate(rows(pane)):
                        tinted = [
                            x for x in range(width) if style_at(strip, x).bgcolor != base[y][x]
                        ]
                        if not tinted:
                            continue
                        # The tinted cells, read back through the same row model
                        # the copy uses — ``slice`` is cell-addressed.
                        pieces.append(pane._displayed_row(y).slice(min(tinted), max(tinted) + 1))
                    pairs.append(("\n".join(pieces).rstrip("\n"), pane.selected_text() or ""))
            return pairs

    for painted, copied in run(drive()):
        assert painted == copied, (painted, copied)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (Offset(8, 8), Offset(4, 9)),  # clamped columns descending
        (Offset(4, 8), Offset(8, 9)),  # …and ascending, which a normal drag leaves
        (Offset(2, 8), Offset(7, 10)),  # round 8's own reproduction
    ],
)
def test_a_selection_left_over_from_a_taller_pane_copies_nothing(
    fake: FakeTmux, tmp_path: Path, start: Offset, end: Offset
) -> None:
    """Clamping the rows BEFORE ordering the endpoints mapped both onto the last
    row with the columns reversed, and the swap then read a span nobody had
    selected — `rows[last][4:8]`, with nothing highlighted on screen. Worse, a
    non-empty answer made ``_copy_selection`` report success, so ctrl+c reported
    a copy and swallowed the agent's interrupt: a runaway agent could not be
    stopped (review of the seventh version)."""

    # Every row carries text, so the bottom row the stale rows clamp onto has
    # something to copy — which is the whole point: a blank one hides the bug.
    fake.panes["%1"].screen = [f"row{n} xxxxxxxxxx" for n in range(6)]

    async def drive() -> tuple[str, list[tuple[str, ...]]]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            assert pane._lines[5].startswith("row5"), "the last row has text to take"
            pane.focus()
            # A selection left over from a taller pane: both rows are off the
            # end of this one, and clamp onto the last row with the columns the
            # wrong way round.
            pane.screen.selections = {pane: Selection(start, end)}
            await pilot.pause()
            assert all(
                pane.text_selection is not None and pane.text_selection.get_span(y) is None
                for y in range(pane.content_size.height)
            ), "the premise: nothing is highlighted on any row the pane has"
            await pilot.press("ctrl+c")
            await pilot.pause()
            return host.clipboard, list(fake.sent())

    clipboard, sent = run(drive())
    assert clipboard == "", "nothing was highlighted, so nothing is copied"
    assert sent == [("C-c",)], "and ctrl+c stays the agent's interrupt"


def test_a_gesture_that_touches_no_pane_row_leaves_the_clipboard_alone(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """The app hears every release, including ones with nothing to do with a
    pane. A drag on the footer — which does not allow selection, so Textual does
    not clear the standing one either — re-copied it and toasted again for a
    gesture that selected nothing (review of the seventh version)."""

    async def drive() -> tuple[str, int, str, int]:
        host = Host(fake.server(tmp_path), "%1", with_footer=True)
        async with host.run_test(size=(40, 10)) as pilot:
            pane = host.pane
            footer = host.query_one(Footer)
            await wait_until(pilot, lambda: synced(pane))
            await drag(pilot, pane, (0, 2), (5, 2))
            dragged, toasts = host.clipboard, len(host.notices)
            fake.panes["%1"].screen = ["AAA", "BBB", "CCC"]
            await wait_until(pilot, lambda: "CCC" in pane._lines)
            await press(pilot, footer, (1, 0))
            await move(pilot, footer, (6, 0), button=1)
            await release(pilot, footer, (6, 0))
            await pilot.pause()
            return dragged, toasts, host.clipboard, len(host.notices)

    dragged, toasts, after, toasts_after = run(drive())
    assert dragged == "third " and toasts == 1
    assert after == dragged, "the standing selection is not re-copied"
    assert toasts_after == toasts, "and nothing claims a copy that did not happen"


def test_a_double_click_does_not_leak_into_the_next_gesture(fake: FakeTmux, tmp_path: Path) -> None:
    """A double click selects its word AFTER the gesture that caused it ended,
    so the selection change it makes must not count as taking part in the next
    one — measured re-copying the word on the following release."""

    async def drive() -> tuple[str, int, str, int]:
        host = Host(fake.server(tmp_path), "%1", with_footer=True)
        async with host.run_test(size=(40, 10)) as pilot:
            pane = host.pane
            footer = host.query_one(Footer)
            await wait_until(pilot, lambda: synced(pane))
            await click(pilot, pane, (8, 1), times=2)
            await pilot.pause()
            word, toasts = host.clipboard, len(host.notices)
            await press(pilot, footer, (1, 0))
            await move(pilot, footer, (6, 0), button=1)
            await release(pilot, footer, (6, 0))
            await pilot.pause()
            return word, toasts, host.clipboard, len(host.notices)

    word, toasts, after, toasts_after = run(drive())
    assert word == "row" and toasts == 1
    assert after == word and toasts_after == toasts


def test_a_right_button_drag_from_outside_the_pane_does_not_copy(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """The pane only sees a press that lands ON it, so a right-button drag begun
    on the header read as a left one and copied — the same gesture being a copy
    or not purely on where it began (review of the seventh version). The app
    sees every press and passes the button down."""

    async def drive() -> tuple[str, int, str, int]:
        host = Host(fake.server(tmp_path), "%1", with_header=True)
        async with host.run_test(size=(40, 8)) as pilot:
            pane = host.pane
            header = host.query_one("#other", Static)
            await wait_until(pilot, lambda: synced(pane))
            await drag(pilot, pane, (0, 2), (5, 2))
            dragged, toasts = host.clipboard, len(host.notices)
            host.screen.clear_selection()
            await pilot.pause()
            await press(pilot, header, (1, 0), button=3)
            await move(pilot, pane, (5, 1), button=3)
            await release(pilot, pane, (5, 1), button=3)
            await pilot.pause()
            return dragged, toasts, host.clipboard, len(host.notices)

    dragged, toasts, after, toasts_after = run(drive())
    assert dragged == "third " and toasts == 1
    assert after == dragged, "a right-button drag is not a copy request, wherever it began"
    assert toasts_after == toasts


def test_only_the_rows_the_widget_shows_are_copied(fake: FakeTmux, tmp_path: Path) -> None:
    """``_lines`` longer than the widget means it SHRANK and no frame has landed
    since — a pane whose captures are failing keeps its taller frame for good.
    Copying the longer of the two put rows on the clipboard that were never
    rendered and never painted (review of the eighth version)."""

    async def drive() -> tuple[str | None, int]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 5)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            # The frame a shrink left behind, with no successful capture since.
            pane._lines = [f"row{n:02d}" for n in range(12)]
            # A span over every row of the old frame — not ``Selection(None,
            # None)``, which the pane refuses outright (class docstring, rule 4).
            everything = pane.get_selection(Selection(Offset(0, 0), Offset(99, 11)))
            return (everything[0] if everything else None), pane.content_size.height

    copied, height = run(drive())
    assert height == 5
    assert copied is not None
    assert copied.split("\n") == [f"row{n:02d}" for n in range(5)], copied


def test_a_selection_running_off_the_bottom_copies_the_rows_it_paints(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """A span that starts on a real row and runs past the last one. Clamping the
    end onto the last row copied it part-way while the paint covered it whole —
    a copy/paint split on a row that is on screen (review of the eighth)."""
    fake.panes["%1"].screen = [f"row{n} xxxxxxxxxx" for n in range(5)]

    async def drive() -> tuple[str | None, tuple[int, int] | None]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 5)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            selection = Selection(Offset(2, 3), Offset(7, 10))
            pane.screen.selections = {pane: selection}
            await pilot.pause()
            return pane.selected_text(), selection.get_span(4)

    copied, last_span = run(drive())
    assert last_span == (0, -1), "the paint covers the last row whole"
    assert copied == "w3 xxxxxxxxxx\nrow4 xxxxxxxxxx", copied


def test_a_double_click_with_another_button_selects_and_copies_nothing(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """Every button check went into the drag path and none into the click path,
    so a right- or middle-button double click selected a word and wrote the
    clipboard — on a terminal where the right button is paste or a context menu
    (review of the eighth version)."""

    async def drive() -> tuple[str, int, str | None]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            await click(pilot, pane, (8, 1), times=2, button=3)
            await pilot.pause()
            await click(pilot, pane, (8, 2), times=2, button=2)
            await pilot.pause()
            return host.clipboard, len(host.notices), pane.selected_text()

    clipboard, toasts, selected = run(drive())
    assert clipboard == "" and toasts == 0, "neither button copies"
    assert selected is None, "and neither selects a word"


def test_a_ctrl_c_copy_does_not_promise_a_selection_it_just_cleared(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """The drag's toast offers ctrl+c as a second copy, which is true while the
    highlight stands. The ctrl+c path clears it, so the same sentence there was
    false as the user read it (review of the eighth version)."""

    async def drive() -> tuple[list[str], Selection | None]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            await drag(pilot, pane, (0, 2), (5, 2))
            pane.focus()
            await pilot.press("ctrl+c")
            await pilot.pause()
            return list(host.notices), pane.text_selection

    notices, selection = run(drive())
    assert notices[0].endswith("while the selection stands"), "the drag's copy leaves it standing"
    assert notices[1] == "copied 6 characters", notices[1]
    assert selection is None, "and ctrl+c cleared it, which is why it says no such thing"


def test_attaching_another_agent_leaves_other_panes_selections_alone(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """`Screen.clear_selection()` is `selections = {}` — every widget on the
    screen. The app keeps a view per opened agent mounted, so a background poll
    re-attaching a HIDDEN pane wiped the highlight the user had in the visible
    one (review of the eighth version)."""
    fake.panes["%2"] = FakePane(screen=["other agent"], cursor=(0, 0))

    class TwoPanes(App[None]):
        def compose(self) -> ComposeResult:
            server = fake.server(tmp_path)
            yield TerminalPane("%1", server=server, escape_key="f12", id="pane")
            yield TerminalPane("%1", server=server, escape_key="f12", id="hidden")

    async def drive() -> tuple[Selection | None, Selection | None]:
        host = TwoPanes()
        async with host.run_test(size=(40, 8)) as pilot:
            visible = host.query_one("#pane", TerminalPane)
            hidden = host.query_one("#hidden", TerminalPane)
            await wait_until(pilot, lambda: synced(visible) and synced(hidden))
            host.screen.selections = {
                visible: Selection(Offset(0, 1), Offset(5, 1)),
                hidden: Selection(Offset(0, 0), Offset(3, 0)),
            }
            await pilot.pause()
            hidden.attach("%2")  # the background poll
            await pilot.pause()
            return visible.text_selection, hidden.text_selection

    still_there, dropped = run(drive())
    assert still_there == Selection(Offset(0, 1), Offset(5, 1)), "the visible pane keeps its own"
    assert dropped is None, "and the re-attached one drops only its own"


def test_a_double_click_leaves_other_panes_selections_alone(fake: FakeTmux, tmp_path: Path) -> None:
    """``_select_word`` is the other place this file writes ``screen.selections``,
    and it replaced the whole dict — the same harm ``_clear_own_selection`` was
    added to stop, by another route (review of the ninth version).

    Driven at the method, not through a click: Textual clears the WHOLE screen's
    selection on a release that moved nothing, which a double click is, so an
    end-to-end gesture wipes the neighbour before this line runs. That upstream
    clear is not ours to fix; writing only our own entry is.
    """

    class TwoPanes(App[None]):
        def compose(self) -> ComposeResult:
            server = fake.server(tmp_path)
            yield TerminalPane("%1", server=server, escape_key="f12", id="pane")
            yield TerminalPane("%1", server=server, escape_key="f12", id="other")

    async def drive() -> tuple[Selection | None, str | None, str, int]:
        host = TwoPanes()
        async with host.run_test(size=(40, 8)) as pilot:
            clicked = host.query_one("#pane", TerminalPane)
            neighbour = host.query_one("#other", TerminalPane)
            await wait_until(pilot, lambda: synced(clicked) and synced(neighbour))
            host.screen.selections = {neighbour: Selection(Offset(0, 0), Offset(5, 0))}
            await pilot.pause()
            # The press of the double click: every pane notes what it has.
            route_gesture_start(host)
            clicked._select_word(8, 1)
            await pilot.pause()
            clipboard, toasts = host.clipboard, len(host._notifications)
            # Its release: the neighbour's unchanged highlight must not copy,
            # and the word — copied by the click itself — must not copy again.
            route_selection_gesture(host, 1)
            await pilot.pause()
            assert host.clipboard == clipboard and len(host._notifications) == toasts
            return neighbour.text_selection, clicked.selected_text(), clipboard, toasts

    neighbours, word, clipboard, toasts = run(drive())
    assert word == "row", "the double click selected its own word"
    assert clipboard == "row" and toasts == 1, "and copied it exactly once"
    assert neighbours == Selection(Offset(0, 0), Offset(5, 0)), "and left the other pane alone"


def test_a_stale_selection_never_refreshes_a_row_the_pane_does_not_have(
    fake: FakeTmux, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rows a selection names are not the rows this widget has. A stale
    highlight over rows 8-10 of a now-5-row pane handed ``refresh`` Regions
    outside the widget — the shape ``refresh_frame`` documents as measured harm
    for the cursor, and which its own test pins there (review of the ninth)."""
    regions: list[Region] = []

    async def drive() -> tuple[list[Region], list[Region]]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 5)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            original = TerminalPane.refresh

            def record(self: TerminalPane, *args: object, **kwargs: object) -> TerminalPane:
                regions.extend(a for a in args if isinstance(a, Region))
                return original(self, *args, **kwargs)  # type: ignore[arg-type]

            monkeypatch.setattr(TerminalPane, "refresh", record)
            pane.screen.selections = {pane: Selection(Offset(0, 8), Offset(4, 10))}
            await pilot.pause()
            # …and above the top, which is the other half of the clamp and the
            # one that yields `Region(0, -2, w, 1)` (review of the tenth).
            pane.screen.selections = {pane: Selection(Offset(0, -2), Offset(4, 1))}
            await pilot.pause()
            stale = list(regions)
            regions.clear()
            # The negative half: a selection that IS on screen repaints its own
            # rows, so the clamp has not simply turned the repaint off.
            pane.screen.selections = {pane: Selection(Offset(0, 1), Offset(4, 2))}
            await pilot.pause()
            return stale, list(regions)

    stale, live = run(drive())
    height = 5
    assert stale, "the premise: rows 0-1 of the second selection are on screen"
    assert all(0 <= region.y < height for region in stale), stale
    assert live, "a selection on screen still repaints"
    assert all(0 <= region.y < height for region in live), live
    assert {region.y for region in live} >= {1, 2}, live


def test_a_gesture_whose_press_nobody_saw_is_not_a_copy(fake: FakeTmux, tmp_path: Path) -> None:
    """The app sees every press now (class docstring, rule 1), so an unknown
    button only reaches the pane from a caller that has none — but reading
    "unknown" as "left" is the assumption that let a right-button drag copy
    (review of the eighth), and the answer stays pinned."""

    async def drive() -> tuple[str, int, str, int]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            await drag(pilot, pane, (0, 2), (5, 2))
            dragged, toasts = host.clipboard, len(host.notices)
            # A new selection this pane would copy for the left button.
            route_gesture_start(host)
            pane.screen.selections = {pane: Selection(Offset(0, 1), Offset(6, 1))}
            await pilot.pause()
            assert pane.selected_text() == "second", "the premise"
            pane.selection_gesture_ended(None)
            await pilot.pause()
            return dragged, toasts, host.clipboard, len(host.notices)

    dragged, toasts, after, toasts_after = run(drive())
    assert dragged == "third " and toasts == 1
    assert after == dragged, "an unknown button copies nothing"
    assert toasts_after == toasts


def test_a_detached_pane_has_nothing_to_copy_and_does_not_raise(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """``ALLOW_SELECT`` is on whether or not a pane is attached, so a drag over
    the ``(no pane)`` placeholder leaves a selection standing while
    ``get_selection`` answers ``None``. Without the guard that is a ``TypeError``
    raised from inside the gesture routing — which would now be logged and
    swallowed, leaving copy silently dead (review of the tenth version)."""

    async def drive() -> tuple[str, int, bool, str | None]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            await drag(pilot, pane, (0, 2), (5, 2))
            copied, toasts = host.clipboard, len(host.notices)
            host.screen.clear_selection()
            await pilot.pause()
            pane.attach(None)  # the project and accounts views both do this
            await pilot.pause()
            pane.screen.selections = {pane: Selection(Offset(0, 1), Offset(5, 1))}
            await pilot.pause()
            standing = pane.text_selection
            assert standing is not None and pane.get_selection(standing) is None, "the premise"
            # The routing the app uses, on a pane with nothing behind it.
            route_selection_gesture(host, 1)
            await pilot.pause()
            return copied, toasts, host.clipboard == copied, pane.selected_text()

    copied, toasts, unchanged, selected = run(drive())
    assert copied == "third " and toasts == 1, "the real copy, before detaching"
    assert selected is None, "a detached pane has no text under its highlight"
    assert unchanged, "so the gesture copies nothing rather than raising"


def test_a_second_drag_copies_the_screen_it_was_made_on(fake: FakeTmux, tmp_path: Path) -> None:
    """A second gesture made while the first selection still stands used to reuse
    the first one's frozen rows — under a printing agent, a screen that no longer
    exists, so the clipboard got whatever had been at those coordinates.

    The second half is the shape that survived the first attempt at this: a drag
    PRESSED OUTSIDE the pane. Textual gives such a drag ``Selection(None, end)``
    every time, so an anchor test could not see a new gesture, and the widget
    never saw the press that would otherwise have reset it (review of the fifth
    version). Both halves now read the live rows, so neither can go stale."""

    async def drive() -> tuple[str, str, str]:
        host = Host(fake.server(tmp_path), "%1", with_header=True)
        async with host.run_test(size=(40, 8)) as pilot:
            pane = host.pane
            other = host.query_one("#other", Static)
            await wait_until(pilot, lambda: synced(pane))
            await drag(pilot, pane, (0, 1), (5, 1))
            first = host.clipboard
            fake.panes["%1"].screen = ["AAA newest", "BBB middle", "CCC bottom"]
            await wait_until(pilot, lambda: "CCC bottom" in pane._lines)
            # No intervening click: the first selection is still standing.
            await drag(pilot, pane, (0, 2), (5, 2))
            second = host.clipboard
            # And again from outside the pane, which no press of ours precedes.
            fake.panes["%1"].screen = ["XXX one", "YYY two", "ZZZ three"]
            await wait_until(pilot, lambda: "ZZZ three" in pane._lines)
            await press(pilot, other, (1, 0))
            await move(pilot, pane, (4, 2), button=1)
            await release(pilot, pane, (4, 2))
            await pilot.pause()
            pane.focus()
            await pilot.press("ctrl+c")
            await pilot.pause()
            return first, second, host.clipboard

    first, second, crossed = run(drive())
    assert first == "second"
    assert second == "CCC bo", "the live screen, not the one the first drag froze"
    assert crossed.startswith("XXX one"), crossed


def test_a_row_wider_than_the_pane_copies_only_what_is_shown(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """`adjust_cell_length` truncates a row longer than the widget, so it renders
    cut — while `_extract` read the whole thing and put 200 columns on the
    clipboard. A failed `resize-window` leaves the tmux window at its spawn
    geometry while captures keep succeeding, which is the documented state where
    rows are wider than the pane (review)."""
    fake.panes["%1"].screen = ["x" * 200, "y" * 200, "z" * 200]

    async def drive() -> tuple[str | None, int]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            await drag(pilot, pane, (0, 0), (39, 2))
            return pane.selected_text(), rows(pane)[1].cell_length

    copied, painted_width = run(drive())
    assert copied is not None
    lines = copied.split("\n")
    assert [len(line) for line in lines] == [40, 40, 40], lines
    assert painted_width == 40, "the row renders 40 cells wide; the copy matches it"


def test_the_scroll_marker_is_part_of_the_row_it_sits_on(fake: FakeTmux, tmp_path: Path) -> None:
    """The marker was layered AFTER the selection and rebuilt the tail of row 0,
    throwing the tint away while `_extract` copied that text anyway — and the
    replacement changed the row's character count, so the offsets stamped on the
    tail no longer indexed it (review)."""
    pane = fake.panes["%1"]
    pane.history = [f"old {n}" for n in range(5)]

    async def drive() -> tuple[str, str | None, Style, Style]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            widget = host.pane
            widget.focus()
            await wait_until(pilot, lambda: synced(widget))
            widget.post_message(scroll_event(widget, up=True))
            await wait_until(pilot, lambda: widget.scrollback > 0)
            await drag(pilot, widget, (0, 0), (39, 0))
            row = rows(widget)[0]
            return row.text, widget.selected_text(), style_at(row, 2), style_at(row, 35)

    shown, copied, left, over_marker = run(drive())
    assert "[↑" in shown, shown
    assert copied == shown.rstrip("\n")[:40], "what is copied is the row as displayed"
    assert copied is not None and "[↑" in copied, "the marker is on the row, so it copies"
    assert left.bgcolor == over_marker.bgcolor, "the whole dragged row is tinted, marker included"


def test_a_triple_click_on_the_header_selects_nothing_in_the_pane(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """Finding 6 of the #135 review. Textual's ``Widget._on_click`` answers a
    triple click with the CONTAINER's select-all, which writes
    ``Selection(None, None)`` on every widget in it — the pane included, though
    nobody clicked it. The pane's own guard only covered clicks on itself, so a
    triple click on the agent header (to grab the cwd) left the still-focused
    pane selected whole: the next ctrl+c copied the entire pane instead of
    interrupting, and a leftover participation flag made the next unrelated
    release overwrite the clipboard. Refused now, in every reader (class
    docstring, rule 4)."""

    async def drive() -> tuple[Selection | None, list[tuple[str, ...]], str, int]:
        host = Host(fake.server(tmp_path), "%1", with_header=True, with_footer=True)
        async with host.run_test(size=(40, 10)) as pilot:
            pane = host.pane
            header = host.query_one("#other", Static)
            await wait_until(pilot, lambda: synced(pane))
            pane.focus()
            await click(pilot, header, (1, 0), times=3)
            await pilot.pause()
            standing = pane.text_selection
            await pilot.press("ctrl+c")
            await pilot.pause()
            sent = list(fake.sent())
            # (b) of the finding: the next unrelated release must not copy either.
            footer = host.query_one(Footer)
            await drag(pilot, footer, (1, 0), (6, 0))
            return standing, sent, host.clipboard, len(host.notices)

    standing, sent, clipboard, toasts = run(drive())
    assert standing is None, "the pane is never selected whole"
    assert sent == [("C-c",)], "so ctrl+c is still the interrupt"
    assert clipboard == "" and toasts == 0, "and nothing copied, then or on the next release"


def test_a_burst_of_gestures_routes_each_release_with_its_own_button(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """Finding 7 of the #135 review. The button used to be recorded when the
    forwarded MouseDown finished bubbling pane → view → screen → app, while
    ``TextSelected`` reached the app one hop from the screen — so a burst of
    input handled back-to-back (the two-second ``refresh_data`` blocking the
    loop) routed a release with the previous gesture's button: 7 of 18 bursts
    misrouted. Both halves are read in ``App.on_event`` now, in order.

    Six events posted with no turn of the loop between them, as a blocked loop
    delivers them: a right-button drag from the header, then a left drag in the
    pane. Exactly the left one copies."""

    async def drive() -> tuple[str, list[str]]:
        host = Host(fake.server(tmp_path), "%1", with_header=True)
        async with host.run_test(size=(40, 8)) as pilot:
            pane = host.pane
            header = host.query_one("#other", Static)
            await wait_until(pilot, lambda: synced(pane))
            for event in (
                mouse_event(events.MouseDown, header, (1, 0), 3),
                mouse_event(events.MouseMove, pane, (5, 1), 3),
                mouse_event(events.MouseUp, pane, (5, 1), 3),
                mouse_event(events.MouseDown, pane, (0, 2), 1),
                mouse_event(events.MouseMove, pane, (5, 2), 1),
                mouse_event(events.MouseUp, pane, (5, 2), 1),
            ):
                host.post_message(event)
            await pilot.pause()
            await pilot.pause()
            return host.clipboard, list(host.notices)

    clipboard, notices = run(drive())
    assert clipboard == "third ", "the left drag copied its own text"
    assert len(notices) == 1, f"and the right drag copied nothing: {notices}"


def test_a_second_button_pressed_mid_drag_does_not_steal_the_drags_release(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """Review of #203, round 4. ``SelectionHost._pressed`` held one button and
    every press overwrote it: a right button pressed while the left drag was
    still down re-armed the host with ``3`` AND re-baselined every pane to the
    selection the drag had built, so the left release routed as a right-button
    gesture and the copy was silently dropped — a painted highlight, no toast,
    nothing on the clipboard, and the next ctrl+c the agent's interrupt. A
    second button while one is down is no gesture at all now; the release that
    ends the gesture is the one naming the button that began it."""

    async def drive() -> tuple[str, list[str]]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 8)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            for event in (
                mouse_event(events.MouseDown, pane, (0, 2), 1),
                mouse_event(events.MouseMove, pane, (5, 2), 1),
                mouse_event(events.MouseDown, pane, (5, 2), 3),
                mouse_event(events.MouseUp, pane, (5, 2), 1),
                mouse_event(events.MouseUp, pane, (5, 2), 3),
            ):
                host.post_message(event)
            await pilot.pause()
            await pilot.pause()
            return host.clipboard, list(host.notices)

    clipboard, notices = run(drive())
    assert clipboard == "third ", "the left drag's release was routed as the left button"
    assert len(notices) == 1, f"one copy, and the stray right release copied nothing: {notices}"


def test_a_stray_button_whose_release_never_arrives_does_not_lock_the_mouse_out(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """Review of the fold. The stray's bookkeeping was cleared only by its own
    release. Lost — the pointer left the terminal with the right button down —
    it outlived the drag; the next right click's press was accepted and its
    release dropped as the stray's, leaving ``_pressed`` armed for good and
    every later left press classified as stray: nothing in the app clickable.
    A stray goes with the gesture it interrupted, and a new gesture starts clean."""

    async def drive() -> tuple[str, list[str]]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 8)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            for event in (
                # a left drag with a stray right press whose release is lost
                mouse_event(events.MouseDown, pane, (0, 2), 1),
                mouse_event(events.MouseMove, pane, (5, 2), 1),
                mouse_event(events.MouseDown, pane, (5, 2), 3),
                mouse_event(events.MouseUp, pane, (5, 2), 1),
                # later: an ordinary right click, then an ordinary left drag
                mouse_event(events.MouseDown, pane, (1, 1), 3),
                mouse_event(events.MouseUp, pane, (1, 1), 3),
                mouse_event(events.MouseDown, pane, (0, 2), 1),
                mouse_event(events.MouseMove, pane, (5, 2), 1),
                mouse_event(events.MouseUp, pane, (5, 2), 1),
            ):
                host.post_message(event)
            await pilot.pause()
            await pilot.pause()
            return host.clipboard, list(host.notices)

    clipboard, notices = run(drive())
    assert clipboard == "third ", "the second left drag copied: its press was not read as stray"
    assert len(notices) == 2, f"both left drags copied, the right click nothing: {notices}"


def test_a_copy_over_a_wrapped_row_is_the_panes_width_while_the_pane_is_narrower(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """Pinned for the review of the fold, which read the wrapped-row crop as
    reaching only the widget's width while the pane was narrower (the resize
    debounce, a refused resize-window) — a 30-column command copied padded to 40
    cells and glued onto its continuation. Measured: the crop to the pane's width
    already covered it, so this pins the other half of the invariant the wider-pane
    test above pins — a wrapped row is copied at the NARROWER of the two widths."""
    fake.apply_resize = False
    pane_fake = fake.panes["%1"]
    pane_fake.width, pane_fake.height = 30, 3
    pane_fake.screen = ["a" * 30, "tail", ""]
    pane_fake.wrapped = {0}

    async def drive() -> tuple[str | None, int]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 3)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: pane.facts is not None and "tail" in rows(pane)[1].text)
            assert pane.facts is not None and pane.facts.width == 30, "the premise: a narrower pane"
            await drag(pilot, pane, (0, 0), (3, 1))
            return pane.selected_text(), pane.content_size.width

    copied, width = run(drive())
    assert width == 40
    assert copied == "a" * 30 + "tail", copied


_ORDERINGS = {
    "primary-up-first": ("D1", "D3", "U1", "U3"),
    "stray-up-first": ("D1", "D3", "U3", "U1"),
    "stray-lost": ("D1", "D3", "U1"),
    "stray-first-then-primary": ("D3", "D1", "U3", "U1"),
    "stray-first-primary-up-first": ("D3", "D1", "U1", "U3"),
    "stray-up-twice": ("D1", "D3", "U3", "U3", "U1"),
    "sequential-not-stray": ("D3", "U3", "D1", "U1"),
}


@pytest.mark.parametrize("ordering", sorted(_ORDERINGS), ids=sorted(_ORDERINGS))
def test_a_second_button_never_reaches_the_screen_whatever_the_release_order(
    fake: FakeTmux, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ordering: str
) -> None:
    """The INVARIANT, not one ordering of it (round 5 of the #203 review, on the
    churn of fixing one symptom at a time): while one button is down, a second
    button's press and release never reach the screen, whichever of them is
    lifted first and even when the second's release never comes; the gesture's
    own release always routes with the button that began it; and afterwards the
    host is clean — an ordinary left drag copies. Measured at the one seam that
    IS "reaching the screen": ``App.on_event``, the parent this class defers to.

    Sequential presses (``D3 U3 D1 U1``) are the control: two gestures, both
    forwarded, the left one copies."""
    forwarded: list[tuple[str, int]] = []
    real_on_event = App.on_event

    async def spy(self: App[Any], event: events.Event) -> None:
        if isinstance(event, (events.MouseDown, events.MouseUp)) and not event.is_forwarded:
            forwarded.append((type(event).__name__, event.button))
        await real_on_event(self, event)

    monkeypatch.setattr(App, "on_event", spy)
    steps = _ORDERINGS[ordering]
    # The stray is a SECOND button pressed while the first is still down.
    stray = int(steps[1][1]) if steps[1].startswith("D") and steps[1][1] != steps[0][1] else None
    # The left button's release routes a copy only when the left button was a
    # gesture of its own (not the stray) — the right button's never does.
    copies = 1 if "U1" in steps and stray != 1 else 0

    async def drive() -> tuple[list[tuple[str, int]], list[str], bool, bool, bool]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 8)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            for step in steps:
                kind = events.MouseDown if step[0] == "D" else events.MouseUp
                button = int(step[1])
                if kind is events.MouseDown and button == 1:
                    host.post_message(mouse_event(events.MouseDown, pane, (0, 2), 1))
                    host.post_message(mouse_event(events.MouseMove, pane, (5, 2), 1))
                else:
                    at = (5, 2) if button == 1 else (1, 1)
                    host.post_message(mouse_event(kind, pane, at, button))
            await pilot.pause()
            await pilot.pause()
            seen = list(forwarded)
            after_sequence = list(host.notices)
            released = host._pressed is None  # every gesture that began has ended
            # Afterwards: an ordinary left drag must copy — the host is clean.
            # Over ANOTHER row than the sequence's drag, and counted by the copy
            # toast: a drag that leaves a standing highlight as it was is not a
            # copy (class docstring, rule 2), and the clipboard may already hold
            # the sequence's own text. A stray whose release never came is
            # cleared HERE, by the next gesture's start.
            toasts_before = len(host.notices)
            for event in (
                mouse_event(events.MouseDown, pane, (0, 0), 1),
                mouse_event(events.MouseMove, pane, (4, 0), 1),
                mouse_event(events.MouseUp, pane, (4, 0), 1),
            ):
                host.post_message(event)
            await pilot.pause()
            await pilot.pause()
            copied_after = bool(host.clipboard) and len(host.notices) == toasts_before + 1
            clean_after = host._pressed is None and host._stray is None
            return seen, after_sequence, released, copied_after, clean_after

    seen, notices, released, copied_after, clean_after = run(drive())
    assert released, f"[{ordering}] a gesture was still armed after its release"
    if stray is not None:
        assert all(button != stray for _, button in seen), (
            f"[{ordering}] the stray button {stray} reached the screen: {seen}"
        )
    else:
        assert ("MouseDown", 3) in seen and ("MouseUp", 3) in seen, (
            f"[{ordering}] sequential presses are separate gestures and both reach the screen"
        )
    assert len(notices) == copies, f"[{ordering}] copies during the sequence: {notices}"
    assert copied_after, f"[{ordering}] the host was not clean for the next drag"
    assert clean_after, f"[{ordering}] _pressed/_stray were not reset by the next gesture"


def _mouse(kind: type[events.MouseEvent], button: int) -> events.MouseEvent:
    """One bare mouse event, as the driver posts it, at a fixed cell."""
    return kind(None, 3, 3, 0, 0, button, False, False, False, screen_x=3, screen_y=3)


def test_every_button_sequence_up_to_five_events_keeps_the_gesture_invariant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EXHAUSTIVE, not one ordering per round (rounds 4 to 7 of #203 each fixed
    the complement of the last): every sequence over {D1, D2, D3, U1, U2, U3, M0}
    up to five events — 19 607 of them, duplicates and lost releases included —
    is driven through ``SelectionHost.on_event`` against a reference model of
    the one rule, and both are compared event by event:

    * a press when nothing is down begins a gesture with that button: forwarded,
      every pane baselined; while one is down, ANY press is dropped (a second
      button becomes the stray; a repeat of the gesture's own button is one
      press reported twice);
    * the only release that ends the gesture names the button that began it:
      forwarded, routed once with that button; every other release while one is
      down is dropped;
    * with nothing down, the stray's own release is dropped once and any other
      release is forwarded and routed with no button;
    * a move with no button held (M0) is always forwarded; while a gesture is
      down it is that gesture's lost release — routed once with its button —
      and a stray is kept for its own release (review of the fold with #167).

    Measured at the two seams that matter — ``App.on_event`` (the screen) and
    the two route functions — on a bare host, with the clock frozen so a repeat
    press is always inside :data:`DUPLICATE_PRESS_WINDOW`."""
    forwarded: list[tuple[str, int]] = []
    routed: list[tuple[str, int | None]] = []

    async def screen(self: App[Any], event: events.Event) -> None:
        if isinstance(event, (events.MouseDown, events.MouseUp, events.MouseMove)):
            forwarded.append((type(event).__name__, event.button))

    monkeypatch.setattr(App, "on_event", screen)
    monkeypatch.setattr(
        terminal_module, "route_gesture_start", lambda app: routed.append(("start", None))
    )
    monkeypatch.setattr(
        terminal_module,
        "route_selection_gesture",
        lambda app, button: routed.append(("end", button)),
    )
    monkeypatch.setattr(terminal_module, "_monotonic", lambda: 100.0)

    alphabet = [("D", 1), ("D", 2), ("D", 3), ("U", 1), ("U", 2), ("U", 3), ("M", 0)]

    def model(
        steps: list[tuple[str, int]],
    ) -> tuple[list[tuple[str, int]], list[tuple[str, int | None]]]:
        pressed: int | None = None
        stray: int | None = None
        fwd: list[tuple[str, int]] = []
        rt: list[tuple[str, int | None]] = []
        for kind, b in steps:
            if kind == "M":
                if pressed is not None:
                    rt.append(("end", pressed))
                    pressed = None
                fwd.append(("MouseMove", b))
            elif kind == "D":
                if pressed is not None:
                    if b != pressed:
                        stray = b
                    continue
                stray, pressed = None, b
                rt.append(("start", None))
                fwd.append(("MouseDown", b))
            else:
                if pressed is not None and b != pressed:
                    continue
                if stray is not None and b == stray:
                    stray = None
                    continue
                fwd.append(("MouseUp", b))
                rt.append(("end", pressed))
                pressed = None
        return fwd, rt

    async def drive(steps: list[tuple[str, int]]) -> None:
        host = SelectionHost()
        kinds = {"D": events.MouseDown, "U": events.MouseUp, "M": events.MouseMove}
        for kind, b in steps:
            await host.on_event(_mouse(kinds[kind], b))
        assert host._pressed is None or any(k == "D" for k, _ in steps)

    checked = 0
    for length in range(1, 6):
        for steps in itertools.product(alphabet, repeat=length):
            forwarded.clear()
            routed.clear()
            asyncio.run(drive(list(steps)))
            expected_fwd, expected_rt = model(list(steps))
            assert forwarded == expected_fwd, f"{steps}: forwarded {forwarded} != {expected_fwd}"
            assert routed == expected_rt, f"{steps}: routed {routed} != {expected_rt}"
            checked += 1
    assert checked == 7 + 49 + 343 + 2401 + 16807


def test_a_duplicated_primary_press_does_not_restart_the_drag(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """Round 7 of #203, the press-side complement of the duplicated release: a
    repeat of the gesture's own button used to be forwarded, so the screen
    restarted its selection at the duplicate's cell and every pane was
    re-baselined mid-drag — the drag's highlight gone before its own release,
    the copy dropped. One gesture at a time includes the gesture's own button."""

    async def drive() -> tuple[str, list[str]]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 8)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            for event in (
                mouse_event(events.MouseDown, pane, (0, 2), 1),
                mouse_event(events.MouseMove, pane, (5, 2), 1),
                mouse_event(events.MouseDown, pane, (5, 2), 1),  # reported twice
                mouse_event(events.MouseUp, pane, (5, 2), 1),
            ):
                host.post_message(event)
            await pilot.pause()
            await pilot.pause()
            return host.clipboard, list(host.notices)

    clipboard, notices = run(drive())
    assert clipboard == "third ", "the drag copied what it selected"
    assert len(notices) == 1, notices


def test_a_lost_release_is_recovered_by_the_next_press_after_the_window(
    fake: FakeTmux, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the duplicate-press rule (round 4's lock-out, kept): a
    press of the gesture's own button LONG after it went down is not a report
    of the same press, it is a new gesture whose predecessor's release was lost
    with the pointer outside the terminal. Refusing it would leave every later
    gesture unrouted."""
    now = {"t": 100.0}
    monkeypatch.setattr(terminal_module, "_monotonic", lambda: now["t"])

    async def drive() -> tuple[str, list[str]]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 8)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            host.post_message(mouse_event(events.MouseDown, pane, (0, 1), 1))
            host.post_message(mouse_event(events.MouseMove, pane, (3, 1), 1))
            await pilot.pause()  # …and the release is lost with the pointer outside
            now["t"] += terminal_module.DUPLICATE_PRESS_WINDOW + 1.0
            for event in (
                mouse_event(events.MouseDown, pane, (0, 2), 1),
                mouse_event(events.MouseMove, pane, (5, 2), 1),
                mouse_event(events.MouseUp, pane, (5, 2), 1),
            ):
                host.post_message(event)
            await pilot.pause()
            await pilot.pause()
            return host.clipboard, list(host.notices)

    clipboard, notices = run(drive())
    assert clipboard == "third ", "the new gesture was accepted and copied"
    assert len(notices) == 1, notices


def test_a_lost_release_ends_the_drag_at_the_first_move_with_no_button_held(
    fake: FakeTmux, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of the fold with #167. The window above is the only recovery a
    lost release had, so a drag let go outside the terminal and followed by
    another drag within half a second lost the second one: its press was
    dropped as a duplicate of the first, its moves extended the lost drag's
    selection, and its release copied both rows. The pointer coming back in
    with no button held is the first report that the release was lost — the
    rule #167's sidebar divider ends its own drag by: the gesture ends there,
    and copies where the drag got to, not where the bare pointer came back in.
    The clock is frozen, so every repeat press is inside the window."""
    monkeypatch.setattr(terminal_module, "_monotonic", lambda: 100.0)

    async def drive() -> tuple[str, list[str], int | None]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 8)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            for event in (
                mouse_event(events.MouseDown, pane, (0, 1), 1),
                mouse_event(events.MouseMove, pane, (3, 1), 1),
                # …the release is lost outside, and the pointer comes back in
                mouse_event(events.MouseMove, pane, (7, 2), 0),
            ):
                host.post_message(event)
            await pilot.pause()
            await pilot.pause()
            pressed_after_bare_move = host._pressed
            for event in (
                mouse_event(events.MouseDown, pane, (0, 2), 1),
                mouse_event(events.MouseMove, pane, (5, 2), 1),
                mouse_event(events.MouseUp, pane, (5, 2), 1),
            ):
                host.post_message(event)
            await pilot.pause()
            await pilot.pause()
            return host.clipboard, list(host.notices), pressed_after_bare_move

    clipboard, notices, pressed_after_bare_move = run(drive())
    assert pressed_after_bare_move is None, "the bare move ended the lost gesture"
    assert len(notices) == 2, f"both drags copied: {notices}"
    assert notices[0].startswith("copied 4 characters"), (
        f"the lost drag copied where it got to ('seco'), not to the bare pointer: {notices}"
    )
    assert clipboard == "third ", "the second drag was a gesture of its own"


def test_a_lost_release_leaves_the_highlight_that_was_copied(
    fake: FakeTmux, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #203, round 1 of the fold delta. The bare move ended the
    GESTURE, but Textual's screen was still drag-selecting — only a ``MouseUp``
    ends that — so every bare move after it carried the highlight's end along
    with the pointer: the toast had just said "copied 4 characters — ctrl+c
    copies again while the selection stands", and ctrl+c then copied
    ``second row\\nthird row``, the rows the pointer had wandered over since.
    The screen's drag ends with the gesture: the highlight stays what was
    copied, and the copy key copies exactly that again. (That the next press
    still drag-selects is the test above's second drag.)"""
    monkeypatch.setattr(terminal_module, "_monotonic", lambda: 100.0)

    async def drive() -> tuple[str, str | None, str, list[str]]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 8)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            for event in (
                mouse_event(events.MouseDown, pane, (0, 1), 1),
                mouse_event(events.MouseMove, pane, (3, 1), 1),
                # …the release is lost outside, and the pointer comes back in
                # and goes on moving with no button held
                mouse_event(events.MouseMove, pane, (7, 2), 0),
                mouse_event(events.MouseMove, pane, (9, 2), 0),
            ):
                host.post_message(event)
            await pilot.pause()
            await pilot.pause()
            copied, highlighted = host.clipboard, pane.selected_text()
            host.set_focus(None)
            await pilot.press("ctrl+c")
            await pilot.pause()
            return copied, highlighted, host.clipboard, list(host.notices)

    copied, highlighted, copied_again, notices = run(drive())
    assert copied == "seco", "the premise: the lost drag copied where it got to"
    assert highlighted == "seco", f"the highlight followed the bare pointer: {highlighted!r}"
    assert copied_again == "seco", f"ctrl+c copied something else: {copied_again!r}"
    assert len(notices) == 2 and notices[1] == "copied 4 characters", notices


def test_a_pane_detached_but_still_registered_is_skipped_not_logged_as_failing(
    fake: FakeTmux, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #120, round 11. The filter that skips a pane off the active
    screen ran inside the catch meant for the pane's own handler, and a pane
    detached from the DOM while still in the register — ``pane.screen`` raises
    ``NoScreen`` — was logged as "selection gesture failed for a pane" at every
    press and release in the app. Textual 8 delivers ``Unmount``, which
    unregisters, before it detaches, so the state is made here by hand: removed,
    then registered again. It is skipped quietly, and the pane on the screen
    still copies."""
    logged: list[str] = []
    original_call = Logger.__call__

    def record(self: Logger, *args: object, **kwargs: object) -> None:
        logged.append(" ".join(str(a) for a in args))
        original_call(self, *args, **kwargs)

    monkeypatch.setattr(Logger, "__call__", record)

    async def drive() -> tuple[list[str], str]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 8)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            ghost = TerminalPane(None, id="ghost")
            await host.mount(ghost)
            await ghost.remove()
            assert ghost not in _MOUNTED_PANES, "the premise: Unmount unregistered it"
            _MOUNTED_PANES.add(ghost)
            try:
                with pytest.raises(NoScreen):
                    _ = ghost.screen
                logged.clear()
                await drag(pilot, pane, (0, 2), (5, 2))
                await pilot.pause()
                return list(logged), host.clipboard
            finally:
                _MOUNTED_PANES.discard(ghost)

    recorded, clipboard = run(drive())
    assert clipboard == "third ", "the pane on the screen still heard the gesture"
    assert not [line for line in recorded if "failed for a pane" in line], recorded


def test_the_servers_version_is_asked_once_across_attaches(tmp_path: Path) -> None:
    """Round 8 of #203. ``_wrap_flags`` needs the version for the FIRST frame, and
    it was read once per ATTACH — a blocking ``tmux -V`` subprocess on the UI
    thread at every project switch, tab activation and re-mounted view. Cached
    by socket, which is what the answer is about."""
    record: list[tuple[str, ...]] = []
    fake = FakeTmux(record=record)
    fake.panes["%1"] = FakePane(screen=["first"])
    fake.panes["%2"] = FakePane(screen=["other"])

    async def drive() -> int:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 4)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: screen_text(pane)[0] == "first")
            pane.attach("%2")
            await wait_until(pilot, lambda: screen_text(pane)[0] == "other")
            pane.attach("%1")
            await wait_until(pilot, lambda: screen_text(pane)[0] == "first")
            return sum(1 for argv in record if list(argv)[1:] == ["-V"])

    assert run(drive()) == 1, "three attaches, one tmux -V"


def test_the_copy_key_outside_the_pane_copies_the_panes_highlight_and_nothing_empty(
    fake: FakeTmux, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 10 of the #135 review. Two ctrl+c paths disagreed: the pane's
    ``on_key`` copied its own highlight while focused, and Textual's screen
    binding copied every widget's selection joined — with no toast, and as the
    empty string (an OSC 52 that CLEARS the clipboard) when the selections
    extracted as nothing. After a drag from the header the focus is not in the
    pane, so the toast's "ctrl+c copies again" copied the header line as well.

    Now the key reaches ``PaneScreen``, which copies a standing pane highlight
    through the pane's own path — the same text its release copied, the same
    toast, the highlight cleared — and an empty copy never reaches the driver."""

    async def drive() -> tuple[str, str, list[str], list[str], Selection | None]:
        host = Host(fake.server(tmp_path), "%1", with_header=True)
        writes: list[str] = []
        async with host.run_test(size=(40, 8)) as pilot:
            pane = host.pane
            header = host.query_one("#other", Static)
            await wait_until(pilot, lambda: synced(pane))
            driver = host._driver
            assert driver is not None
            monkeypatch.setattr(driver, "write", writes.append)
            await drag(pilot, header, (1, 0), (5, 1), to=pane)
            dragged = host.clipboard
            host.set_focus(None)  # the shell leaves focus in the sidebar after such a drag
            await pilot.pause()
            await pilot.press("ctrl+c")
            await pilot.pause()
            again, notices, cleared = host.clipboard, list(host.notices), pane.text_selection
            # Nothing highlighted in any pane, a zero-width header selection:
            # Textual's copy would write "" — the guard must not let it.
            host.screen.selections = {header: Selection(Offset(1, 0), Offset(1, 0))}
            await pilot.pause()
            await pilot.press("ctrl+c")
            await pilot.pause()
            return dragged, again, notices, [w for w in writes if "\x1b]52;" in w], cleared

    dragged, again, notices, osc52, cleared = run(drive())
    assert dragged == "red plain\nsecon"
    assert again == dragged, "ctrl+c from outside the pane copies what the release copied"
    assert len(notices) == 2 and notices[1] == "copied 15 characters", notices
    assert cleared is None, "and clears the highlight, as the pane's own ctrl+c does"
    assert len(osc52) == 2, f"two real copies reached the terminal, and no empty one: {osc52}"


def test_hiding_the_pane_drops_its_highlight(fake: FakeTmux, tmp_path: Path) -> None:
    """Finding 10 (a) of the #135 review: a pane behind another tab is not
    captured, and its entry in ``screen.selections`` outlived it — ctrl+c from
    the sidebar then extracted nothing from a 0x0 widget and wiped the
    clipboard. Hidden means dropped."""
    fake.panes["%2"] = FakePane(screen=["other agent"], cursor=(0, 0))

    async def drive() -> tuple[Selection | None, Selection | None, str]:
        host = SwitcherHost(fake.server(tmp_path))
        async with host.run_test(size=(40, 6)) as pilot:
            first = host.query_one("#first", TerminalPane)
            await wait_until(pilot, lambda: synced(first))
            await drag(pilot, first, (0, 1), (5, 1))
            standing = first.text_selection
            host.tabs.current = "second"
            await pilot.pause()
            hidden = first.text_selection
            host.set_focus(None)
            await pilot.press("ctrl+c")
            await pilot.pause()
            return standing, hidden, host.clipboard

    standing, hidden, clipboard = run(drive())
    assert standing is not None, "the premise: the drag selected"
    assert hidden is None, "hiding the pane dropped it"
    assert clipboard == "second", "so the copy key found nothing to wipe the clipboard with"


def test_a_click_right_after_a_drag_is_not_a_double_click(fake: FakeTmux, tmp_path: Path) -> None:
    """Finding 13 of the #135 review. Textual chains clicks by release position
    within half a second and synthesises a Click for a drag whose press and
    release land on the same widget, so a drag followed by a click on its end
    cell arrived as ``chain == 2``: the dragged selection was replaced by a
    word and copied twice. A click is a press and a release in one cell, and
    only clicks chain (class docstring, rule 6).

    Driven through the app, not ``Pilot.click``, because the chaining under test
    is the app's own (``tests/pane_harness.py``)."""

    async def drive() -> tuple[str, str, int, Selection | None, str, int]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            await drag(pilot, pane, (0, 1), (8, 1))
            dragged = host.clipboard
            await click(pilot, pane, (8, 1))  # well within Textual's 0.5 s
            after_click, toasts, standing = host.clipboard, len(host.notices), pane.text_selection
            # The positive half: two real clicks are still a double click, and
            # over a standing highlight they copy the word exactly once.
            await drag(pilot, pane, (0, 2), (5, 2))
            await click(pilot, pane, (8, 1), times=2)
            return dragged, after_click, toasts, standing, host.clipboard, len(host.notices)

    dragged, after_click, toasts, standing, word, toasts_after = run(drive())
    assert dragged == "second ro"
    assert after_click == dragged and toasts == 1, "the click copied nothing more"
    assert standing is None, "a click in place dismisses the highlight, as in any terminal"
    assert word == "row" and toasts_after == 3, "a real double click copies its word once"


def test_a_modal_pushed_mid_drag_leaves_no_gesture_behind(fake: FakeTmux, tmp_path: Path) -> None:
    """Cut finding of the #135 review: a modal pushed while a button is down
    takes the release, so the pane's gesture never ends on its own screen. The
    baseline it noted at the press is then stale — and stale is harmless, because
    the next press on the pane's screen rewrites it before anything reads it."""

    async def drive() -> tuple[str, int]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            await press(pilot, pane, (0, 1))
            await move(pilot, pane, (3, 1), button=1)
            modal = ModalScreen[None]()
            host.push_screen(modal)
            await pilot.pause()
            host.post_message(mouse_event(events.MouseUp, modal, (3, 1), 1))
            await pilot.pause()
            host.pop_screen()
            await pilot.pause()
            assert host.clipboard == "" and not host.notices, "the interrupted drag copied nothing"
            await drag(pilot, pane, (0, 2), (5, 2))
            return host.clipboard, len(host.notices)

    clipboard, toasts = run(drive())
    assert clipboard == "third " and toasts == 1, "the next drag copies as if nothing had happened"


def test_an_empty_copy_never_reaches_the_terminal(
    fake: FakeTmux, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OSC 52 with an empty payload clears the terminal's clipboard, and no
    gesture in this app means that (review of #135, finding 10). Every copy in
    the app goes through ``copy_to_clipboard``; the guard lives there."""

    async def drive() -> tuple[str, list[str]]:
        host = Host(fake.server(tmp_path), "%1")
        writes: list[str] = []
        async with host.run_test(size=(40, 6)) as pilot:
            await wait_until(pilot, lambda: synced(host.pane))
            driver = host._driver
            assert driver is not None
            monkeypatch.setattr(driver, "write", writes.append)
            host.copy_to_clipboard("abc")
            host.copy_to_clipboard("")
            return host.clipboard, [w for w in writes if "\x1b]52;" in w]

    clipboard, osc52 = run(drive())
    assert clipboard == "abc", "the empty copy changed nothing"
    assert len(osc52) == 1, f"one OSC 52, for the real copy: {osc52}"


def test_hovering_over_the_pane_repaints_nothing(fake: FakeTmux, tmp_path: Path) -> None:
    """Finding 3 of the #135 review. ``render_line`` stamped offset metadata on
    every painted row, which gave each segment a unique Rich link id and
    defeated Textual's per-style caches: plain mouse hover repainted the whole
    pane at every segment crossing — measured at the PR head, 120 motion
    reports on a 200x60 pane cost 7200 ``render_line`` calls — streaming frames
    cost twice the CPU, and a full strip cache held tens of MB of stamped
    copies. Painted rows carry no offsets now; the compositor's own direct
    ``render_line`` lookup, the only reader, still gets them.

    Two halves: hover must ask for no row at all, and the offsets must still
    be there for a drag — ``test_drag_select_highlights_the_rows_and_copies_on_release``
    proves the drag, this proves where the stamps are and are not."""

    async def drive() -> tuple[int, bool, bool]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            await pilot.pause(0.6)
            rendered = pane.lines_rendered
            for y in range(6):
                for x in (0, 4, 9, 20):
                    await move(pilot, pane, (x, y))
            repaints = pane.lines_rendered - rendered
            painted = [segment for strip in rows(pane) for segment in strip]
            stamped = list(pane.render_line(0))  # the compositor's direct call
            return (
                repaints,
                any("offset" in (s.style.meta if s.style else {}) for s in painted),
                all(s.style is not None and "offset" in s.style.meta for s in stamped),
            )

    repaints, painted_stamped, lookup_stamped = run(drive())
    assert repaints == 0, f"24 hovers with no button repainted {repaints} rows"
    assert not painted_stamped, "the painted rows carry no offset metadata"
    assert lookup_stamped, "the compositor's direct lookup still gets every segment stamped"


def test_a_selection_over_emoji_paints_and_copies_whole_glyphs(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """Finding 8 of the #135 review. ``⚠️`` and ``✔️`` are one 2-cell grapheme
    each to rich 15 and to tmux, but per code point ``[1, 0]``, so a crop
    snapped per code point cut through them: a drag from the first cell of ✔️
    repainted the row as 41 cells with a doubled ``d`` and copied a stray
    U+FE0F with no ✔. A ZWJ sequence with the cursor after it lost a
    character on every frame. One grapheme model for the paint and the copy."""
    fake.panes["%1"].screen = ["⚠️ disk ✔️ ok done", "👩‍🚀 xyz"]
    fake.panes["%1"].cursor = (3, 1)

    async def drive() -> tuple[str, int, str | None, str, list[int]]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane) and "done" in rows(pane)[0].text)
            pane.focus()
            await drag(pilot, pane, (8, 0), (12, 0))  # the first cell of ✔️ to the k of ok
            row = rows(pane)[0]
            cursor_row = rows(pane)[1]
            reversed_cells = [x for x in range(40) if style_at(cursor_row, x).reverse]
            return (
                row.text.rstrip(),
                row.cell_length,
                pane.selected_text(),
                cursor_row.text.rstrip(),
                reversed_cells,
            )

    painted, cells, copied, cursor_row, reversed_cells = run(drive())
    assert painted == "⚠️ disk ✔️ ok done", "the row is drawn intact under the highlight"
    assert cells == 40, "and is still exactly the pane's width"
    assert copied == "✔️ ok", "the glyph is copied whole, with no stray selector"
    assert cursor_row == "👩‍🚀 xyz", "a ZWJ sequence survives a cursor overlay on its row"
    assert reversed_cells == [3], "and the cursor sits on the cell after it, alone"


def test_a_soft_wrapped_line_is_copied_as_one_line(fake: FakeTmux, tmp_path: Path) -> None:
    """Finding 9 of the #135 review. Rows are captured one screen row each and
    joined with newlines, so a command tmux had wrapped copied as three lines
    split mid-token, with a space lost where the wrap fell on one — pasted into
    a shell, three broken commands. tmux 3.7c's ``capture-pane -F`` marks a
    wrapped row ``W``; every frame carries the flags and a copy joins those
    rows, keeping the wrap-point space. The negative half: a row that merely
    fills the width, unwrapped, still ends its line."""
    pane_fake = fake.panes["%1"]
    pane_fake.width, pane_fake.height = 40, 6
    pane_fake.screen = [
        "aisquare task review tsk_01K9ABCDEF --no",
        "te 'rm -rf build && make test' --as sess",
        "1234",
        "word word word word word word word word ",
        "tail",
        "0123456789012345678901234567890123456789",
    ]
    pane_fake.wrapped = {0, 1, 3}

    async def drive() -> tuple[str | None, str | None, int]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane) and "tail" in rows(pane)[4].text)
            await drag(pilot, pane, (0, 0), (3, 2))
            command = pane.selected_text()
            asked = fake.flag_captures
            await drag(pilot, pane, (0, 3), (39, 5))
            return command, pane.selected_text(), asked

    command, words, asked = run(drive())
    assert command == (
        "aisquare task review tsk_01K9ABCDEF --note 'rm -rf build && make test' --as sess1234"
    ), command
    assert (
        words
        == "word word word word word word word word tail\n0123456789012345678901234567890123456789"
    )
    assert asked >= 1, "the frames carried the flags"


def test_a_copy_runs_no_tmux_process_of_its_own_and_joins_by_the_frames_own_flags(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """Finding 9 of the second #135 review. The copy used to run a SECOND
    ``capture-pane -F`` and compare every row of its answer with the frame on
    screen, dropping the whole answer on any difference — a process on the
    event loop per copy, and newline joins under a busy agent, exactly when a
    wrapped command is most likely on screen. The frame capture the render loop
    already runs carries the flags now (``-F`` in the same process), so the
    copy reads them off the frame it shows: no process, no comparison, and a
    screen that moves after the frame cannot change what the highlight means."""
    pane_fake = fake.panes["%1"]
    pane_fake.screen = ["first line that wraps into the second on", "e", "third"]
    pane_fake.wrapped = {0}

    async def drive() -> tuple[str | None, int, str | None, str | None, int]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane) and "third" in rows(pane)[2].text)
            await drag(pilot, pane, (0, 0), (3, 1))
            joined = pane.selected_text()
            before = len(fake.captures)
            again = pane.get_selection(Selection(Offset(0, 0), Offset(4, 1)))
            ran = len(fake.captures) - before
            # tmux's screen moves, and its flags with it, but no frame has been
            # taken yet: the frame on screen — its text AND its flags — decides.
            pane_fake.screen = ["first line that wraps into the second on", "e MORE", "third"]
            pane_fake.wrapped = set()
            stale = pane.get_selection(Selection(Offset(0, 0), Offset(4, 1)))
            return (
                joined,
                ran,
                again[0] if again else None,
                stale[0] if stale else None,
                fake.flag_captures,
            )

    joined, ran, again, stale, flagged = run(drive())
    assert joined == "first line that wraps into the second one"
    assert ran == 0, "the copy ran no tmux process of its own"
    assert again == joined
    assert stale == joined, "the frame's own flags and text decide, not tmux's screen now"
    assert flagged >= 1, "the frames carried the flags"


def test_an_older_tmux_gets_plain_frames_and_one_line_per_row(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """``capture-pane -F`` is tmux 3.7's; an older server fails the whole frame
    on an unknown flag, and every frame would read ``(pane gone)``. Below the
    gate the pane asks for plain frames and a wrapped line copies as one line
    per row — the wrap join is all that refusing costs."""
    fake.version = "tmux 3.6"
    pane_fake = fake.panes["%1"]
    pane_fake.screen = ["first line that wraps into the second on", "e", "third"]
    pane_fake.wrapped = {0}

    async def drive() -> tuple[str | None, int, str]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane) and "third" in rows(pane)[2].text)
            await drag(pilot, pane, (0, 0), (3, 1))
            return pane.selected_text(), fake.flag_captures, screen_text(pane)[0]

    copied, flagged, first_row = run(drive())
    assert flagged == 0, "no frame asked a 3.6 server for flags it does not know"
    assert first_row == "first line that wraps into the second on", "and the frames still render"
    assert copied == "first line that wraps into the second on\ne"


def test_a_row_with_tabs_copies_and_highlights_what_the_pointer_covers(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """Finding 14 of the #135 review. tmux prints a tab cell as a literal TAB
    and pads the row as if expanded; passed through as a 0-cell character the
    terminal expanded it, while the offsets, the highlight and the copy placed
    everything after it eight cells to the left of what the eye saw — a drag
    over the visible ``c end`` copied nothing, and a drag over ``a..b`` copied
    the whole row. The strip expands tabs to the default stops."""
    fake.panes["%1"].screen = ["a\tb\tc end"]

    async def drive() -> tuple[str, str | None, str | None]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane) and "c end" in rows(pane)[0].text)
            shown = rows(pane)[0].text
            await drag(pilot, pane, (16, 0), (20, 0))
            tail = pane.selected_text()
            await drag(pilot, pane, (0, 0), (8, 0))
            return shown, tail, pane.selected_text()

    shown, tail, head = run(drive())
    assert "\t" not in shown and shown.startswith("a       b       c end"), repr(shown)
    assert tail == "c end", "the text under the pointer, at the cells the eye sees it in"
    assert head == "a       b", "and a drag over the first tab stop copies what it covers"


def test_the_highlight_is_visible_on_reverse_video_cells(fake: FakeTmux, tmp_path: Path) -> None:
    """Cut finding of the #135 review. A background tint on a reverse-video cell
    recoloured the glyph and left the block behind it unchanged — invisible on
    a blank reversed cell, which is what a chosen menu row or a status bar is
    made of. A reversed cell in the highlight is un-reversed with its colours
    swapped, so the tint sits behind the glyph like everywhere else."""
    fake.panes["%1"].screen = ["\x1b[7mrev\x1b[0m x"]

    async def drive() -> tuple[Style, Style, Style]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane) and "rev x" in rows(pane)[0].text)
            before = style_at(rows(pane)[0], 1)
            await drag(pilot, pane, (0, 0), (1, 0))  # "re" of the reversed word
            row = rows(pane)[0]
            return before, style_at(row, 1), style_at(row, 2)

    before, tinted, untouched = run(drive())
    tint = Style(bgcolor=tinted.bgcolor)
    assert before.reverse, "the premise: the cell is drawn in reverse video"
    assert not tinted.reverse and tinted.bgcolor is not None, "un-reversed under the highlight"
    assert tinted.bgcolor != before.bgcolor and tinted.bgcolor != untouched.bgcolor, tint
    assert untouched.reverse, "and the reversed cell outside the highlight is as it was"


def test_the_cursor_stays_visible_inside_a_highlight(fake: FakeTmux, tmp_path: Path) -> None:
    """The cursor was painted before the selection, so a highlight over the
    cursor's cell painted the tint over its reverse video and it vanished. It
    is painted last: reverse video over the tint, distinct from both."""

    async def drive() -> tuple[Style, Style]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            pane.focus()
            await drag(pilot, pane, (0, 0), (5, 0))  # the cursor sits at (2, 0)
            row = rows(pane)[0]
            return style_at(row, 2), style_at(row, 3)

    cursor, beside = run(drive())
    assert beside.bgcolor is not None and not beside.reverse, "the premise: a tinted neighbour"
    assert cursor.reverse, "the cursor cell is still drawn in reverse video"
    assert cursor.bgcolor == beside.bgcolor, "over the tint, not instead of it"


def test_extending_a_drag_repaints_only_the_rows_whose_span_changed(
    fake: FakeTmux, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cleanup of the #135 review: every MouseMove of a drag repainted every row
    of the old and new selection, so a forty-row highlight was redrawn whole
    for a pointer that moved one row. A row whose span did not change is not
    repainted; the row the pointer left and the row it reached are."""
    fake.panes["%1"].screen = [f"row {n}" for n in range(6)]
    regions: list[Region] = []

    async def drive() -> list[int]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane) and "row 5" in rows(pane)[5].text)
            await press(pilot, pane, (0, 0))
            await move(pilot, pane, (3, 3), button=1)
            await pilot.pause()
            original = TerminalPane.refresh

            def record(self: TerminalPane, *args: object, **kwargs: object) -> TerminalPane:
                regions.extend(a for a in args if isinstance(a, Region))
                return original(self, *args, **kwargs)  # type: ignore[arg-type]

            monkeypatch.setattr(TerminalPane, "refresh", record)
            await move(pilot, pane, (3, 4), button=1)
            await pilot.pause()
            return sorted({region.y for region in regions})

    repainted = run(drive())
    assert repainted == [3, 4], f"the row the pointer left and the one it reached: {repainted}"


def test_a_pane_without_history_does_not_scroll(fake: FakeTmux, tmp_path: Path) -> None:
    async def drive() -> int:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            pane.post_message(scroll_event(pane, up=True))
            await pilot.pause()
            return pane.scrollback

    assert run(drive()) == 0
    assert fake.captures and all(scrollback == 0 for _, scrollback in fake.captures)


# --- size -----------------------------------------------------------------------------------


def test_resize_is_debounced_and_forwarded_as_the_content_size(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """Two resizes inside the window are one ``resize-window``; two outside are two.

    The debounce is widened to a second for the coalescing half, so "inside the
    window" is true by construction rather than by luck: at the shipped 100 ms a
    scheduler stall between the two ``resize_terminal`` awaits — routine on a
    loaded runner — fires the first timer and reddens this test for load instead
    of for a regression. The separated pair is the control: the recorder can
    count two, so "exactly one" is a measurement and not a tautology.
    """

    def resizes() -> list[tuple[str, ...]]:
        return [call for call in fake.input if call[0] == "resize-window"]

    async def drive() -> tuple[list[tuple[str, ...]], list[tuple[str, ...]], list[tuple[str, ...]]]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(60, 20)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: bool(resizes()))
            initial = resizes()
            pane.RESIZE_DEBOUNCE = 1.0  # this instance only; the class default stands
            await pilot.resize_terminal(100, 30)
            await pilot.resize_terminal(90, 28)  # inside the (now one-second) window
            await wait_until(pilot, lambda: len(resizes()) > len(initial))
            coalesced = resizes()
            # The control: a resize AFTER the window closes is its own call.
            pane.RESIZE_DEBOUNCE = 0.05
            await pilot.resize_terminal(80, 26)
            await wait_until(pilot, lambda: len(resizes()) > len(coalesced))
            return initial, coalesced, resizes()

    initial, coalesced, final = run(drive())
    assert initial == [("resize-window", "%1", "60", "20")]
    assert coalesced == [*initial, ("resize-window", "%1", "90", "28")]  # ONE, the last size
    assert final == [*coalesced, ("resize-window", "%1", "80", "26")]


def _resizes(tmux: FakeTmux) -> list[tuple[str, ...]]:
    return [call for call in tmux.input if call[0] == "resize-window"]


def test_a_failed_resize_is_retried_until_the_window_matches_the_widget(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """One refused ``resize-window`` must not mis-size the pane for the view's life.

    ``_sync_size``'s only other caller is a ``Resize`` event, so a failure that
    is merely swallowed leaves the tmux window at its spawn geometry (200x50 by
    default) while captures keep succeeding — the widget then shows the bottom
    ``height`` rows of that screen, each truncated to its width.
    """
    fake.fail_resizes = 1

    async def drive() -> tuple[int, bool, bool, str | None]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: bool(_resizes(fake)))
            # The positive half: the refusal really left the 24-row pane in place.
            mis_sized = not synced(pane)
            await wait_until(pilot, lambda: synced(pane))
            return len(_resizes(fake)), mis_sized, synced(pane), pane.notice

    attempts, mis_sized, ended_synced, notice = run(drive())
    assert mis_sized, "the refusal must really mis-size the pane, or the retry proves nothing"
    assert attempts >= 2 and ended_synced  # it asked again, and the window came in line
    assert notice is None  # the transient (pane gone) cleared with the next good frame

    # The control: with tmux answering, ONE call is enough — the retry is a
    # response to the failure and not a resize loop.
    healthy = FakeTmux()
    healthy.panes["%1"] = FakePane(screen=["only row"])

    async def clean() -> tuple[int, bool]:
        host = Host(healthy.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            await pilot.pause(0.5)  # several ticks past the (single) resize
            return len(_resizes(healthy)), synced(pane)

    assert run(clean()) == (1, True)


def test_a_cursor_above_the_shown_window_is_neither_drawn_nor_dirtied(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """A pane taller than the widget can put the cursor off screen (a refused resize).

    Its row comes out negative: nothing renders it, yet it used to enter
    ``dirty`` — so every move of that invisible cursor handed ``refresh`` a
    Region outside the widget (measured: ``Region(0, -16, 40, 1)``) and re-armed
    the 50 ms cadence for rows nobody can see.
    """
    fake.apply_resize = False  # a tmux that will not shrink the window
    fake.panes["%1"].screen = [f"row{y}" for y in range(24)]
    fake.panes["%1"].cursor = (0, 3)  # row 3 of 24: above the six rows we show

    async def drive() -> tuple[bool, bool, int, bool, bool]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            pane.focus()
            await wait_until(pilot, lambda: ("resize-window", "%1", "40", "6") in fake.input)
            pane.refresh_frame()
            await pilot.pause()
            drawn = any(reverse_anywhere(strip, 10) for strip in rows(pane))
            painted = pane.rows_repainted
            fake.panes["%1"].cursor = (0, 4)  # the invisible cursor moves; no row changes
            changed = pane.refresh_frame()
            await pilot.pause()
            invisible = pane.rows_repainted - painted
            # The control: a cursor move INSIDE the window is a real change.
            fake.panes["%1"].cursor = (0, 20)
            visible = pane.refresh_frame()
            await pilot.pause()
            return (
                drawn,
                changed,
                invisible,
                visible,
                any(reverse_anywhere(strip, 10) for strip in rows(pane)),
            )

    drawn, changed, invisible, visible, drawn_after = run(drive())
    assert not drawn  # nothing on screen is the cursor…
    assert changed is False and invisible == 0, "an off-screen cursor is not a repaint"
    assert visible is True and drawn_after  # …until its row is one this widget has


# --- the tmux-version key gate -----------------------------------------------------------------


def _press_shift_enter(tmux: FakeTmux, tmp_path: Path) -> tuple[list[tuple[str, ...]], list[str]]:
    """Press an extended-only chord (then a plain key) into a pane on ``tmux``."""

    async def drive() -> tuple[list[tuple[str, ...]], list[str]]:
        host = Host(tmux.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            host.pane.focus()
            await pilot.pause()
            await pilot.press("shift+enter", "enter")
            await pilot.pause()
            return tmux.sent(), list(host.notices)

    return run(drive())


def test_the_servers_tmux_version_gates_the_chords_it_would_type_out(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """``_extended_keys`` is this widget's half of core.keys' anti-corruption rule.

    Below 3.5 tmux TYPES ``S-Enter`` into the running agent instead of sending
    the key (measured on 3.3a/3.4), so the pane drops it there — and sends it on
    every modern server, which is the only reason shift+enter works at all.
    Nothing outside test_keys.py's pure units reached the gate: the fake always
    answered 3.7c and no test pressed an extended-only chord, so the widget's
    gate could have been stuck at either value undetected.
    """
    fake.version = "tmux 3.4"
    old_sent, old_notices = _press_shift_enter(fake, tmp_path)
    modern = FakeTmux()
    modern.panes["%1"] = FakePane(screen=["one row"])
    modern_sent, modern_notices = _press_shift_enter(modern, tmp_path)

    assert old_sent == [("Enter",)], "a chord tmux 3.4 would type out must not be sent"
    assert len(old_notices) == 1 and old_notices[0].startswith("shift+enter")
    assert modern.version == "tmux 3.7c"  # the control's premise, spelled out
    assert modern_sent == [("S-Enter",), ("Enter",)]  # …and there the chord goes through
    assert modern_notices == []


def test_attach_re_reads_the_version_for_a_new_server(fake: FakeTmux, tmp_path: Path) -> None:
    """The version is read once PER SERVER, and a pane outlives its server.

    ``ManagerTab`` assigns ``pane.server`` and then calls ``attach``; a cached
    "extended chords are fine" from a 3.7 server would otherwise type
    ``S-Enter`` into an agent running on a 3.4 one. Since round 8 of #203 the
    answer is cached by SOCKET, so the other server is on another socket —
    as two servers always are; a server is its socket.
    """
    old = FakeTmux()
    old.version = "tmux 3.4"
    old.panes["%1"] = FakePane(screen=["older server"])

    async def drive() -> tuple[list[tuple[str, ...]], list[tuple[str, ...]]]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            pane.focus()
            await pilot.pause()
            await pilot.press("shift+enter")
            await pilot.pause()
            modern = fake.sent()
            pane.server = old.server(tmp_path, socket="older")
            pane.attach("%1")
            await pilot.pause()
            await pilot.press("shift+enter")
            await pilot.pause()
            return modern, old.sent()

    modern_sent, old_sent = run(drive())
    assert modern_sent == [("S-Enter",)]  # the 3.7c server got the chord…
    assert old_sent == [], "…and the version was re-read for the server attached after it"


# --- placeholder and failure states -----------------------------------------------------------


def test_no_pane_shows_a_placeholder_and_captures_nothing(fake: FakeTmux, tmp_path: Path) -> None:
    async def drive() -> tuple[list[str], int]:
        host = Host(fake.server(tmp_path), None)
        async with host.run_test(size=(40, 4)) as pilot:
            pane = host.pane
            pane.focus()
            await pilot.pause(0.2)
            await pilot.press("a")
            await pilot.pause()
            return screen_text(pane), pane.frames

    text, frames = run(drive())
    assert text[0] == NO_PANE and text[1:] == ["", "", ""]
    assert frames == 0 and fake.captures == []
    assert fake.sent() == []  # a key into nothing is not sent anywhere


def test_a_dead_pane_keeps_its_last_screen_under_an_exit_notice(
    fake: FakeTmux, tmp_path: Path
) -> None:
    async def drive() -> tuple[list[str], list[str], bool]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 4)) as pilot:
            pane = host.pane
            pane.focus()
            await wait_until(pilot, lambda: synced(pane))
            await pilot.pause()
            alive = screen_text(pane)
            fake.panes["%1"].dead = True
            fake.panes["%1"].dead_status = 3
            pane.refresh_frame()
            await pilot.pause()
            return alive, screen_text(pane), reverse_anywhere(rows(pane)[0], 9)

    alive, dead, cursor_drawn = run(drive())
    assert alive[3] == ""  # the negative: no notice while the process lives
    assert dead[0] == "red plain" and dead[3] == "(exited 3)"
    assert not cursor_drawn


def test_a_gone_pane_shows_a_notice_and_a_new_attach_recovers(
    fake: FakeTmux, tmp_path: Path
) -> None:
    fake.panes["%2"] = FakePane(screen=["fresh pane"])

    async def drive() -> tuple[list[str], list[str], list[str], int]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 4)) as pilot:
            pane = host.pane
            pane.focus()
            await wait_until(pilot, lambda: synced(pane))
            fake.panes["%1"].gone = True
            pane.refresh_frame()
            pane.refresh_frame()  # a second failure: still one notice
            await pilot.press("a")  # a key into a gone pane is dropped, never raised
            await pilot.pause()
            gone = screen_text(pane)
            notices = list(host.notices)
            pane.attach("%2")
            await wait_until(pilot, lambda: screen_text(pane)[0] == "fresh pane")
            return gone, notices, screen_text(pane), pane.scrollback

    gone, notices, recovered, scrollback = run(drive())
    assert gone[0] == "red plain" and gone[3] == PANE_GONE
    assert len(notices) == 1 and PANE_GONE in notices[0]
    assert recovered[0] == "fresh pane" and recovered[3] == ""
    assert scrollback == 0


def test_no_usable_tmux_is_a_notice_not_a_traceback(fake: FakeTmux, tmp_path: Path) -> None:
    server = TmuxServer(
        "fake", binary="definitely-not-a-tmux-binary", conf=tmp_path / "c", runner=fake
    )

    async def drive() -> tuple[list[str], list[str]]:
        host = Host(server, "%1")
        async with host.run_test(size=(40, 4)) as pilot:
            pane = host.pane
            await pilot.pause(0.2)
            return screen_text(pane), list(host.notices)

    text, notices = run(drive())
    assert text[3] == TMUX_UNAVAILABLE
    assert len(notices) == 1 and TMUX_UNAVAILABLE in notices[0]
    assert fake.captures == []  # the negative: nothing was even attempted


# --- AgentView --------------------------------------------------------------------------------


def _status(
    *,
    label: str = "coder-1",
    pane_id: str = "%1",
    task_id: str | None = "tsk_0123456789abcdef",
    exit_status: int | None = None,
    state: str = "working",
) -> FleetAgentStatus:
    agent = FleetAgent(
        id="fa_1",
        project_id="prj_1",
        label=label,
        role="coder",
        pane_id=pane_id,
        cwd=Path("/home/me/[archive]/repo"),
        task_id=task_id,
        created_at=datetime.now(UTC),
        exit_status=exit_status,
    )
    return FleetAgentStatus.model_validate({"agent": agent, "state": state})


def test_header_text_carries_every_field_as_data() -> None:
    text = str(header_text(_status(label="[coder-1]", exit_status=1, state="exited")))
    assert "[coder-1]" in text  # brackets survive: appended as text, never markup
    assert "coder" in text and "exited" in text
    assert "task 89abcdef" in text
    assert "/home/me/[archive]/repo" in text
    assert "exited 1" in text
    # The negative: absent facts leave no trace.
    bare = str(header_text(_status(task_id=None, exit_status=None)))
    assert "task " not in bare and "exited" not in bare


def test_agent_view_refreshes_its_header_and_reattaches_on_a_new_pane(
    fake: FakeTmux, tmp_path: Path
) -> None:
    fake.panes["%2"] = FakePane(screen=["restarted"])
    server = fake.server(tmp_path)

    class ViewHost(App[None]):
        def compose(self) -> ComposeResult:
            yield AgentView(_status(), server=server, escape_key="f12", id="view")

    async def drive() -> tuple[str, str, int, str, str, int]:
        host = ViewHost()
        async with host.run_test(size=(60, 8)) as pilot:
            view = host.query_one("#view", AgentView)
            header = host.query_one("#agent-header", Static)
            await wait_until(pilot, lambda: view.pane.frames >= 1)
            first = view.pane.pane_id or ""
            # Scroll the way a user does — through history the pane KNOWS about.
            # A bare `scrollback = 2` raced the render loop: the next frame
            # clamps to history_size (0 here), which is exactly what happened on
            # the slower CI runners while passing locally.
            fake.panes["%1"].history = ["old"] * 5
            view.pane.refresh_frame()
            view.pane.scroll_history(2)
            view.refresh_status(_status(state="waiting"))  # same pane: no re-attach
            await pilot.pause()
            kept, waiting = view.pane.scrollback, str(header.content)
            view.refresh_status(_status(pane_id="%2", state="working"))
            await wait_until(pilot, lambda: screen_text(view.pane)[0] == "restarted")
            return (
                first,
                view.pane.pane_id or "",
                kept,
                waiting,
                str(header.content),
                view.pane.scrollback,
            )

    first, second, kept, waiting, working, reset = run(drive())
    assert (first, second) == ("%1", "%2")
    assert kept == 2  # the same pane keeps its view
    assert reset == 0  # a new pane starts live
    assert "coder-1" in waiting and "waiting" in waiting
    assert "working" in working and "waiting" not in working


# --- against a real tmux ------------------------------------------------------------------------

_needs_tmux = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")


@pytest.fixture
def real_server(tmp_path: Path) -> Iterator[TmuxServer]:
    conf = tmp_path / "tmux.conf"
    conf.write_text(BUNDLED_CONF, encoding="utf-8")
    server = TmuxServer(f"asq-test-{os.getpid()}-pane", conf=conf)
    try:
        yield server
    finally:
        with contextlib.suppress(TmuxError):
            server.run("kill-server")


@_needs_tmux
def test_real_tmux_pane_renders_output_and_echoes_forwarded_keys(
    real_server: TmuxServer, tmp_path: Path
) -> None:
    window = real_server.spawn_window(
        "term",
        name="probe",
        cwd=tmp_path,
        command=["sh", "-c", "printf hello; cat"],
        width=80,
        height=24,
    )

    async def drive() -> tuple[list[str], list[str], float]:
        host = Host(real_server, window.pane_id)
        async with host.run_test(size=(80, 24)) as pilot:
            pane = host.pane
            pane.focus()
            await wait_until(pilot, lambda: screen_text(pane)[:1] == ["hello"])
            before = screen_text(pane)
            started = time.monotonic()
            await pilot.press("x", "enter")
            # The tty echoes the x after hello; cat echoes the line on the next row.
            await wait_until(
                pilot,
                lambda: screen_text(pane)[:2] == ["hellox", "x"],
                timeout=1.0,
            )
            return before, screen_text(pane), time.monotonic() - started

    before, after, elapsed = run(drive())
    assert before[:2] == ["hello", ""]
    assert after[:2] == ["hellox", "x"]
    assert elapsed < 1.0


# --- the second review round of #135 --------------------------------------------------


def test_the_highlight_is_visible_on_reverse_video_cells_under_a_theme_with_no_selection_bg(
    fake: FakeTmux, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 1 of the second #135 review. ``_selection_tint`` falls back to plain
    reverse video for a theme whose selection style names no background, and
    ``_tinted`` then swapped a reversed cell's colours "by hand" onto that
    ``None`` background — which kept the cell's own and drew the glyph in it:
    blue on blue, the invisibility the method exists to prevent (reproduced by
    the reviewer with this repo's rich). Inverting an inverted cell is
    un-reversing it; the plain cells beside it are reversed, as the fallback
    always did."""
    fake.panes["%1"].screen = ["\x1b[7mrev\x1b[0m x"]
    monkeypatch.setattr(
        TerminalPane, "selection_style", property(lambda self: Style(color="white"))
    )

    async def drive() -> tuple[Style, Style, Style]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane) and "rev x" in rows(pane)[0].text)
            before = style_at(rows(pane)[0], 1)
            await drag(pilot, pane, (0, 0), (4, 0))  # "rev x", reversed word and plain letter
            row = rows(pane)[0]
            return before, style_at(row, 1), style_at(row, 4)

    before, reversed_cell, plain_cell = run(drive())
    assert before.reverse, "the premise: the cell is drawn in reverse video"
    assert not reversed_cell.reverse, "inverting an inverted cell un-reverses it"
    assert reversed_cell.color != reversed_cell.bgcolor, (
        f"and the glyph is visible: {reversed_cell}"
    )
    assert plain_cell.reverse, "while the plain cell beside it takes the fallback's reverse video"


def test_a_right_click_does_not_seed_the_click_chain(fake: FakeTmux, tmp_path: Path) -> None:
    """Finding 2 of the second #135 review. The pane's own click chain was counted
    before the left-button gate, for every button — so a right click (paste, or
    a context menu, on most terminals) and a left click in the same cell within
    half a second read as a double click: a word selected and the clipboard
    written by one left click. Only left clicks count, and any other button
    breaks the run; two real left clicks are still a double click."""

    async def drive() -> tuple[str | None, str, int, str | None, str]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            await click(pilot, pane, (8, 1), button=3)
            await click(pilot, pane, (8, 1))  # well within Textual's 0.5 s
            after = (pane.selected_text(), host.clipboard, len(host.notices))
            await click(pilot, pane, (8, 1))  # the second LEFT click in a row
            return *after, pane.selected_text(), host.clipboard

    word, clipboard, toasts, real_word, copied = run(drive())
    assert word is None and clipboard == "" and toasts == 0, "right then left is one click"
    assert real_word == "row" and copied == "row", "left then left is still a double click"


def test_the_copy_key_takes_the_highlight_made_most_recently(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """Finding 3 of the second #135 review. ``copy_pane_selection`` promised "the
    first standing pane highlight" and iterated a ``WeakSet`` — hash order, which
    varies with allocation — so with two panes highlighted the copy key picked
    one at random. Most recent is the rule, and it is driven in BOTH orders on
    one host: whatever order the set holds the two panes in, one half would fail
    without the clock. The older highlight is next in line."""
    fake.panes["%2"] = FakePane(screen=["other agent"], cursor=(0, 0))

    async def drive() -> list[str]:
        host = PairHost(fake.server(tmp_path))
        async with host.run_test(size=(40, 8)) as pilot:
            first = host.query_one("#first", TerminalPane)
            second = host.query_one("#second", TerminalPane)
            await wait_until(pilot, lambda: synced(first) and synced(second))
            copies: list[str] = []
            for newest, older in ((second, first), (first, second)):
                host.screen.selections = {older: Selection(Offset(0, 0), Offset(3, 0))}
                await pilot.pause()
                host.screen.selections = {
                    **host.screen.selections,
                    newest: Selection(Offset(0, 0), Offset(3, 0)),
                }
                await pilot.pause()
                host.set_focus(None)
                for _ in range(2):
                    await pilot.press("ctrl+c")
                    await pilot.pause()
                    copies.append(host.clipboard)
            return copies

    assert run(drive()) == ["oth", "red", "red", "oth"]


def test_a_drag_across_two_panes_leaves_the_clipboard_with_the_highlight_the_copy_key_takes(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """Review of #120, round 11. One drag across two visible panes changes both
    highlights, and at the release both copy — so the last pane told wins the
    clipboard, and the release told them in the register's order: a ``WeakSet``,
    hash order, measured telling the top pane last on one run and the bottom on
    another. They are told in the copy key's order now, the most recently
    selected last. Driven in BOTH directions on one host: the drag's end is the
    pane that changed last, and whatever order the set holds the two in, one
    direction would fail without the order. The copy key, pressed after, copies
    what the release left on the clipboard."""
    fake.panes["%2"] = FakePane(screen=["BBB one", "BBB two", "BBB three"], cursor=(0, 0))

    async def drive() -> list[tuple[str, str]]:
        host = PairHost(fake.server(tmp_path))
        async with host.run_test(size=(40, 8)) as pilot:
            top = host.query_one("#first", TerminalPane)
            bottom = host.query_one("#second", TerminalPane)
            await wait_until(pilot, lambda: synced(top) and synced(bottom))
            results: list[tuple[str, str]] = []
            # Down from the top pane, ending in the bottom one; then up from the
            # bottom, ending in the top. Two moves in the pane the drag ends in:
            # the first one Textual writes both panes' selections in, in one
            # watcher call; the second changes the end pane's alone.
            for pressed_in, start, released_in, end in (
                (top, (1, 1), bottom, (4, 1)),
                (bottom, (3, 1), top, (5, 1)),
            ):
                await drag(pilot, pressed_in, start, end, to=released_in)
                on_release = host.clipboard
                host.set_focus(None)
                await pilot.press("ctrl+c")
                await pilot.pause()
                results.append((on_release, host.clipboard))
                host.screen.clear_selection()
                await pilot.pause()
            return results

    down, up = run(drive())
    assert down == ("BBB one\nBBB ", "BBB one\nBBB "), down
    assert up == ("d row\nthird row", "d row\nthird row"), up


def test_a_failed_frame_drops_the_highlight_on_the_row_its_notice_replaces(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """Finding 4 of the second #135 review. ``_fail`` put ``(pane gone)`` in the
    bottom row without the staleness check every frame runs, so a highlight
    over that row now covered the notice, and the next ctrl+c copied
    ``(pane`` and returned True instead of falling through to the interrupt.
    The negative half: a highlight on another row survives the notice."""

    async def on_bottom_row() -> tuple[str, Selection | None, str]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 3)) as pilot:
            pane = host.pane
            pane.focus()
            await wait_until(pilot, lambda: synced(pane))
            await drag(pilot, pane, (0, 2), (5, 2))
            assert host.clipboard == "third ", "the premise: the bottom row is highlighted"
            fake.panes["%1"].gone = True
            pane.refresh_frame()
            dropped = pane.text_selection
            await pilot.press("ctrl+c")
            await pilot.pause()
            return screen_text(pane)[2], dropped, host.clipboard

    bottom, dropped, clipboard = run(on_bottom_row())
    assert bottom == PANE_GONE
    assert dropped is None, "the highlight covered text the notice replaced"
    assert clipboard == "third ", "so the notice was never copied"

    fake.panes["%1"].gone = False

    async def elsewhere() -> Selection | None:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 3)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            await drag(pilot, pane, (0, 0), (3, 0))
            fake.panes["%1"].gone = True
            pane.refresh_frame()
            return pane.text_selection

    assert run(elsewhere()) is not None, "a highlight the notice does not touch stands"


def test_a_failure_before_the_widget_has_rows_asks_no_row_about_its_highlight(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """Review of #203. ``_fail`` can run before Textual has sized the widget —
    ``attach``'s first capture raising, ``on_mount``'s ``refresh_frame`` before
    the first layout — and ``content_size.height - 1`` is then ``-1``, which
    ``_displayed_row`` reads as the frame's LAST row and compares under a span
    meant for the notice row: a staleness verdict about a row nobody
    highlighted. No rows, no question asked."""

    async def drive() -> tuple[list[int], str | None, bool]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 3)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            await drag(pilot, pane, (0, 0), (3, 0))
            asked: list[int] = []
            real = pane._highlight_is_stale

            def spy(after: Shown, rows: set[int]) -> bool:
                asked.extend(sorted(rows))
                return real(after, rows)

            pane._highlight_is_stale = spy  # type: ignore[method-assign]
            with unsized(pane):
                changed = pane._fail("(pane gone)")
            return asked, pane.notice, changed

    asked, notice, changed = run(drive())
    assert asked == [], f"an unsized widget has no notice row to ask about; asked {asked}"
    assert notice == "(pane gone)" and changed, "the notice itself still lands"


@contextlib.contextmanager
def unsized(pane: TerminalPane) -> Iterator[None]:
    """``content_size`` as it reads before the first layout pass: zero rows."""
    original = TerminalPane.content_size
    TerminalPane.content_size = property(lambda self: Size(40, 0))  # type: ignore[assignment, method-assign]
    try:
        yield
    finally:
        TerminalPane.content_size = original  # type: ignore[method-assign]


def test_a_copy_over_a_wrapped_row_never_reaches_past_the_widget(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """Pinned for the review of #203, which read ``_displayed_row``'s crop of a
    wrapped row to the PANE's width as a copy that could reach past the widget
    while the pane is wider — between a ``Resize`` and its debounced
    ``resize-window``. It cannot: the row is read off ``_composed_strip``,
    which is already the widget's width, so the pane-width crop only ever
    narrows. Measured here with the resize held off, a 60-column pane under a
    40-column widget: the copy of a wrapped row is the 40 cells that were
    painted, joined to the row it wraps into."""
    fake.apply_resize = False
    pane_fake = fake.panes["%1"]
    pane_fake.width, pane_fake.height = 60, 3
    pane_fake.screen = ["a" * 60, "tail", ""]
    pane_fake.wrapped = {0}

    async def drive() -> tuple[str | None, int]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 3)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: pane.facts is not None and "tail" in rows(pane)[1].text)
            assert pane.facts is not None and pane.facts.width == 60, "the premise: a wider pane"
            await drag(pilot, pane, (0, 0), (3, 1))
            return pane.selected_text(), pane.content_size.width

    copied, width = run(drive())
    assert width == 40
    assert copied == "a" * 40 + "tail", copied


def test_unmounting_a_pane_takes_its_selection_entry_with_it(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """Finding 6 of the second #135 review. ``on_hide`` cleared the pane's entry in
    ``screen.selections``; ``on_unmount`` only left the register, so the screen
    kept a strong reference to the dead widget — its strip cache included — and
    a stale span nothing short of ``clear_selection`` could drop. The sibling's
    entry is left alone."""
    fake.panes["%2"] = FakePane(screen=["other agent"], cursor=(0, 0))

    async def drive() -> tuple[list[str | None], bool]:
        host = PairHost(fake.server(tmp_path))
        async with host.run_test(size=(40, 8)) as pilot:
            first = host.query_one("#first", TerminalPane)
            second = host.query_one("#second", TerminalPane)
            await wait_until(pilot, lambda: synced(first) and synced(second))
            host.screen.selections = {
                first: Selection(Offset(0, 0), Offset(3, 0)),
                second: Selection(Offset(0, 0), Offset(5, 0)),
            }
            await pilot.pause()
            await first.remove()
            await pilot.pause()
            return [widget.id for widget in host.screen.selections], first in _MOUNTED_PANES

    left, registered = run(drive())
    assert left == ["second"], f"the unmounted pane's entry is gone, its sibling's stays: {left}"
    assert not registered


def test_a_release_is_routed_even_when_the_apps_own_handling_of_it_raises(
    fake: FakeTmux, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 8 of the second #135 review. ``SelectionHost.on_event`` routed the
    release after ``super().on_event`` returned — and ``App.on_event`` renders
    the widget under the pointer to read its style, arbitrary widget code that
    can raise. Raised, the release was never routed and ``_pressed`` kept the
    gesture's button for the next one. A ``finally`` makes the pairing exact on
    every path; the app here survives the raise the way a subclass could."""

    class Boom(Exception):
        pass

    original = App.on_event

    async def raising(self: App[Any], event: events.Event) -> None:
        if (
            isinstance(event, events.MouseUp)
            and not event.is_forwarded
            and getattr(self, "boom_next_release", False)
        ):
            self.boom_next_release = False  # type: ignore[attr-defined]
            raise Boom("the widget under the pointer failed to render")
        await original(self, event)

    monkeypatch.setattr(App, "on_event", raising)

    class Surviving(Host):
        boom_next_release = False
        booms = 0

        async def on_event(self, event: events.Event) -> None:
            try:
                await super().on_event(event)
            except Boom:
                self.booms += 1

    async def drive() -> tuple[int, int | None, str]:
        host = Surviving(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            host.boom_next_release = True
            await drag(pilot, pane, (0, 1), (5, 1))
            return host.booms, host._pressed, host.clipboard

    booms, pressed, copied = run(drive())
    assert booms == 1, "the premise: the app's own handling of the release raised"
    assert copied == "second", "the release was routed all the same, and the drag copied"
    assert pressed is None, "and the press it paired with is disarmed"


def test_the_compositor_reads_offsets_through_render_line_and_paints_through_render_lines(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """Finding 10 of the second #135 review — a PIN, not a fix. Which method the
    compositor calls for which purpose is Textual's: it resolves a press or a
    drag through ``render_line`` (stamped here) and paints and reads the hover
    style through ``render_lines`` (unstamped, so the caches hold). There is no
    offset source that bypasses it — the Screen builds a drag's ``Selection``
    from what ``get_widget_and_offset_at`` reads off the stamps, and selects the
    whole widget when it finds none — so the assumption is checked against the
    installed Textual: this fails the moment either entry point moves, with the
    offsets going missing or the paint being stamped."""

    async def drive() -> tuple[bool, Offset | None, bool]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            compositor = host.screen._compositor
            x, y = pane.content_region.offset
            widget, offset = compositor.get_widget_and_offset_at(x + 4, y + 1)
            style = compositor.get_style_at(x + 4, y + 1)
            return widget is pane, offset, "offset" in style.meta

    is_pane, offset, stamped = run(drive())
    assert is_pane and offset == Offset(4, 1), (
        f"the drag path resolves the cell under the pointer from render_line's stamps: {offset}"
    )
    assert not stamped, "and the paint/hover path (render_lines) carries no stamps"


def test_the_cursor_row_reuses_its_text_model_from_frame_to_frame(
    fake: FakeTmux, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 11 of the second #135 review. The cursor is an overlay and is on
    screen essentially always, and ``_render_row`` built a fresh ``DisplayedRow``
    — an uncached ``Strip.text`` join and a grapheme scan — for its row on every
    frame the cursor moved. The model of a frame line is cached beside its
    Strip now, so the cursor walking along an unchanged row scans nothing."""
    scans = 0

    def counting(text: str) -> Any:
        nonlocal scans
        scans += 1
        return split_graphemes(text)

    monkeypatch.setattr(terminal_module, "split_graphemes", counting)

    async def drive() -> tuple[int, int, int]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            pane.focus()
            await wait_until(pilot, lambda: synced(pane) and pane._cursor == (2, 0))
            await pilot.pause(0.1)
            first_frames, before = scans, pane.lines_rendered

            def cursor_at(x: int) -> Callable[[], bool]:
                return lambda: pane._cursor == (x, 0)

            for x in range(3, 9):
                fake.panes["%1"].cursor = (x, 0)
                await wait_until(pilot, cursor_at(x))
                await pilot.pause()
            return first_frames, pane.lines_rendered - before, scans - first_frames

    first_frames, rendered, scanned = run(drive())
    assert first_frames >= 1, "the premise: the first frame built the cursor row's model"
    assert rendered >= 6, "the premise: the cursor row was re-rendered for every move"
    assert scanned == 0, f"and its model came from the cache each time, not {scanned} rebuilds"


def test_a_change_hidden_under_the_corner_marker_leaves_the_highlight_standing(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """Finding 12 of the second #135 review. The staleness check measured the RAW
    frame line while the paint and the copy measured the row as composed, so a
    frame that changed the cells under the ``[↑k/history]`` marker — cells the
    marker covers — dropped a highlight for a change nobody could see. One row
    model for all three: what is displayed under the span decides, and a change
    the user can see still drops it."""
    pane_fake = fake.panes["%1"]
    pane_fake.history = [f"old {n}" for n in range(5)]

    async def drive() -> tuple[str | None, bool, bool, Selection | None]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            widget = host.pane
            widget.focus()
            await wait_until(pilot, lambda: synced(widget))
            widget.post_message(scroll_event(widget, up=True))
            await wait_until(pilot, lambda: widget.scrollback == 3)
            await drag(pilot, widget, (0, 0), (39, 0))  # the whole row, marker included
            copied = widget.selected_text()
            standing = widget.text_selection is not None
            # Cells 34-39 of row 0 — exactly the six the marker covers.
            pane_fake.history[2] = "old 2" + " " * 29 + "HIDDEN"
            await wait_until(pilot, lambda: "HIDDEN" in widget._lines[0])
            after_hidden = widget.text_selection is not None
            pane_fake.history[2] = "NEW 2" + " " * 29 + "HIDDEN"
            await wait_until(pilot, lambda: "NEW" in widget._lines[0])
            return copied, standing, after_hidden, widget.text_selection

    copied, standing, after_hidden, after_visible = run(drive())
    assert copied is not None and copied.startswith("old 2") and copied.endswith("[↑3/5]")
    assert standing, "the premise: the whole row is highlighted"
    assert after_hidden, "the text the user sees did not change: the marker covers the cells"
    assert after_visible is None, "a change under the highlight the user CAN see drops it"
