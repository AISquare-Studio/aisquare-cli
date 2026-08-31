"""``TerminalPane`` and ``AgentView``, driven headless against a fake tmux.

The fake is a :data:`aisquare.core.tmux.Runner` — it answers the argv the real
``TmuxServer`` builds the way tmux 3.7c would (``capture-pane`` rows, then the
``display-message`` line, or ``can't find pane`` on exit 1) and records every
``send-keys`` / ``load-buffer`` / ``paste-buffer`` / ``resize-window``. So the
widget is tested through the real ``TmuxServer`` plumbing, with tmux itself the
only thing replaced; the one test at the end puts a real tmux behind the same
widget.

Every claim has a negative half (CONTRIBUTING, "Writing a guard that still
guards"): a row that did not change is NOT repainted, a key that is not the
escape hatch IS forwarded, a pane with no history does NOT scroll, and so on.
Assertions read what reaches the screen — the Strips Textual composites — not
the strings the widget was handed.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import sys
import time
from collections.abc import Callable, Coroutine, Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

import pytest
from rich.style import Style
from textual import events
from textual.app import App, ComposeResult
from textual.geometry import Region
from textual.notifications import SeverityLevel
from textual.pilot import Pilot
from textual.strip import Strip
from textual.widgets import Input, Static

from aisquare.cli.ui.terminal import (
    NO_PANE,
    PANE_GONE,
    TMUX_UNAVAILABLE,
    EscapeToSidebar,
    TerminalPane,
)
from aisquare.cli.ui.views.agent import AgentView, header_text
from aisquare.core.tmux import BUNDLED_CONF, Completed, TmuxError, TmuxServer
from aisquare.models import FleetAgent, FleetAgentStatus

T = TypeVar("T")

# --- the fake tmux --------------------------------------------------------------------

_FORMAT_FIELD = re.compile(r"#\{(\w+)\}")


@dataclass
class FakePane:
    """One pane's state, as tmux would report it."""

    screen: list[str]
    history: list[str] = field(default_factory=list)
    width: int = 80
    height: int = 24
    cursor: tuple[int, int] = (0, 0)
    cursor_visible: bool = True
    dead: bool = False
    dead_status: int | None = None
    gone: bool = False
    """``True`` makes every command targeting the pane fail like a killed window."""

    def facts(self, pane_id: str, fmt: str) -> str:
        """``display-message`` output for ``fmt`` — any field order the caller asks for."""
        values = {
            "pane_id": pane_id,
            "pane_width": str(self.width),
            "pane_height": str(self.height),
            "cursor_x": str(self.cursor[0]),
            "cursor_y": str(self.cursor[1]),
            "cursor_flag": "1" if self.cursor_visible else "0",
            "alternate_on": "0",
            "history_size": str(len(self.history)),
            "pane_dead": "1" if self.dead else "0",
            "pane_dead_status": "" if self.dead_status is None else str(self.dead_status),
            "pane_in_mode": "0",
            "pane_current_command": "sh",
            "pane_title": "",
            "window_activity_flag": "0",
        }
        return _FORMAT_FIELD.sub(lambda m: values.get(m.group(1), ""), fmt)


class FakeTmux:
    """A ``Runner`` that plays tmux: scripted screens out, recorded input in."""

    def __init__(self) -> None:
        self.panes: dict[str, FakePane] = {}
        self.captures: list[tuple[str, int]] = []
        """``(pane_id, scrollback)`` per ``capture-pane``."""
        self.capture_rows: list[int] = []
        """Rows each ``capture-pane`` piped back — what the subprocess actually
        transferred and the widget actually split, one entry per capture."""
        self.input: list[tuple[str, ...]] = []
        """``("send-keys", pane, *args)``, ``("load-buffer", text)``,
        ``("paste-buffer", pane)``, ``("resize-window", pane, w, h)`` in order."""
        self.before_capture: Callable[[FakePane], None] | None = None
        """A hook to script a screen that changes under the widget."""
        self.apply_resize = True
        """Whether ``resize-window`` changes the pane, as tmux does. ``False`` holds
        the pane at its size — the window between a ``Resize`` and its debounced
        ``resize-window``, or a tmux that refused the resize."""

    def server(self, tmp_path: Path) -> TmuxServer:
        # ``binary`` must resolve through ``shutil.which`` on a machine WITHOUT
        # tmux: an absolute executable path does, and is never run.
        return TmuxServer("fake", binary=sys.executable, conf=tmp_path / "fake.conf", runner=self)

    def sent(self) -> list[tuple[str, ...]]:
        """Every ``send-keys`` after ``-t <pane>``."""
        return [call[2:] for call in self.input if call[0] == "send-keys"]

    def __call__(self, argv: Sequence[str], stdin: bytes | None) -> Completed:
        args = list(argv)
        if args[1:] == ["-V"]:
            return Completed(0, "tmux 3.7c\n", "")
        # <binary> -L <socket> -f <conf> <command...>
        command = args[5:]
        groups: list[list[str]] = [[]]
        for arg in command:
            if arg == ";":
                groups.append([])
            else:
                groups[-1].append(arg)
        out: list[str] = []
        for group in groups:
            result = self._one(group, stdin)
            if result.returncode != 0:
                return result
            out.append(result.stdout)
        return Completed(0, "".join(out), "")

    @staticmethod
    def _flag(group: list[str], flag: str) -> str:
        return group[group.index(flag) + 1]

    def _one(self, group: list[str], stdin: bytes | None) -> Completed:
        name = group[0]
        if name == "load-buffer":
            self.input.append((name, (stdin or b"").decode("utf-8")))
            return Completed(0, "", "")
        pane_id = self._flag(group, "-t")
        pane = self.panes.get(pane_id)
        if pane is None or pane.gone:
            return Completed(1, "", f"can't find pane: {pane_id}\n")
        if name == "capture-pane":
            if self.before_capture is not None:
                self.before_capture(pane)
            scrollback = -int(self._flag(group, "-S"))
            self.captures.append((pane_id, scrollback))
            rows = pane.history[len(pane.history) - scrollback :] if scrollback else []
            rows = rows + pane.screen + [""] * (pane.height - len(pane.screen))
            # ``-E`` is tmux's LAST line, numbered from the top of the screen
            # (0), so history lines are negative: line ``e`` sits at index
            # ``e + scrollback`` of the span we just built. Without it tmux
            # answers history-to-bottom, which is the whole point of the flag.
            if "-E" in group:
                rows = rows[: max(0, int(self._flag(group, "-E")) + scrollback + 1)]
            self.capture_rows.append(len(rows))
            return Completed(0, "".join(row + "\n" for row in rows), "")
        if name == "display-message":
            return Completed(0, pane.facts(pane_id, group[-1]) + "\n", "")
        if name == "send-keys":
            assert group[1] == "-t", group
            self.input.append((name, pane_id, *group[3:]))
            return Completed(0, "", "")
        if name == "paste-buffer":
            self.input.append((name, pane_id))
            return Completed(0, "", "")
        if name == "resize-window":
            width, height = int(self._flag(group, "-x")), int(self._flag(group, "-y"))
            self.input.append((name, pane_id, str(width), str(height)))
            if self.apply_resize:
                pane.width, pane.height = width, height
                surplus = len(pane.screen) - height
                if surplus > 0:  # tmux scrolls the top rows into history
                    pane.history += pane.screen[:surplus]
                    pane.screen = pane.screen[surplus:]
                    pane.cursor = (pane.cursor[0], max(0, pane.cursor[1] - surplus))
            return Completed(0, "", "")
        return Completed(1, "", f"unknown command: {name}\n")


# --- the host app -----------------------------------------------------------------------


class Host(App[None]):
    """The pane alone (or under an ``Input`` when ``with_input``), notices recorded."""

    def __init__(
        self,
        server: TmuxServer,
        pane_id: str | None,
        *,
        escape_key: str = "f12",
        with_input: bool = False,
    ) -> None:
        super().__init__()
        self._server = server
        self._pane_id = pane_id
        self._escape_key = escape_key
        self._with_input = with_input
        self.escapes = 0
        self.notices: list[str] = []

    def compose(self) -> ComposeResult:
        if self._with_input:
            yield Input(id="other")
        yield TerminalPane(
            self._pane_id, server=self._server, escape_key=self._escape_key, id="pane"
        )

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


def run(coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def rows(pane: TerminalPane) -> list[Strip]:
    """What Textual composites for the pane: its rendered Strips, top to bottom."""
    width, height = pane.content_size
    return pane.render_lines(Region(0, 0, width, height))


def screen_text(pane: TerminalPane) -> list[str]:
    return [strip.text.rstrip() for strip in rows(pane)]


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
            await pilot.click("#pane")
            await pilot.pause()
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
