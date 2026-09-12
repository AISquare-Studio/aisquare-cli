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
from textual.geometry import Offset, Region
from textual.notifications import SeverityLevel
from textual.pilot import Pilot
from textual.selection import Selection
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import ContentSwitcher, Footer, Input, Static

from aisquare.cli.ui.terminal import (
    NO_PANE,
    PANE_GONE,
    TMUX_UNAVAILABLE,
    EscapeToSidebar,
    TerminalPane,
    _extract,
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
    alternate_on: bool = False
    """The program switched to the alternate screen (a fullscreen TUI)."""
    mouse_on: bool = False
    """The program turned mouse reporting on — it wants the wheel itself."""
    in_mode: bool = False
    """The pane is in a tmux mode (copy mode): tmux owns it for the moment."""
    mouse_sgr: bool = False
    """…in SGR encoding (``?1006``); False is the X10 encoding older programs use."""

    def facts(self, pane_id: str, fmt: str) -> str:
        """``display-message`` output for ``fmt`` — any field order the caller asks for."""
        values = {
            "pane_id": pane_id,
            "pane_width": str(self.width),
            "pane_height": str(self.height),
            "cursor_x": str(self.cursor[0]),
            "cursor_y": str(self.cursor[1]),
            "cursor_flag": "1" if self.cursor_visible else "0",
            "alternate_on": "1" if self.alternate_on else "0",
            "mouse_any_flag": "1" if self.mouse_on else "0",
            "mouse_sgr_flag": "1" if self.mouse_sgr else "0",
            "history_size": str(len(self.history)),
            "pane_dead": "1" if self.dead else "0",
            "pane_dead_status": "" if self.dead_status is None else str(self.dead_status),
            "pane_in_mode": "1" if self.in_mode else "0",
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
        self.version = "tmux 3.7c"
        """What ``tmux -V`` answers. Below 3.5 tmux TYPES the extended chords'
        names into the pane, so the widget must drop them there (core.keys)."""
        self.fail_resizes = 0
        """How many ``resize-window`` calls fail like a killed window first. The
        attempt is still recorded: a test counts the retries."""

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
            return Completed(0, f"{self.version}\n", "")
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
            if self.fail_resizes > 0:
                self.fail_resizes -= 1
                return Completed(1, "", f"can't find pane: {pane_id}\n")
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
    """The pane, optionally under a neighbour, with notices recorded.

    ``with_header`` is what ``AgentView`` actually builds: a plain ``Static``
    above the pane. ``with_input`` is an ``Input``, which calls
    ``capture_mouse()`` — so a drag begun there never delivers its release to
    the pane at all. Any test about a gesture crossing the pane's edge has to
    use the header, or it measures a path the app does not have (review of
    #120, round 6, which is exactly how a broken cross-widget copy passed).

    ``on_mouse_down`` / ``on_text_selected`` mirror ``FleetApp``, and must keep
    mirroring it: the Screen posts ``TextSelected`` on every MouseUp and it
    bubbles to the app, never to a widget, so the app is where a pane learns
    that a gesture finished AND which button began it. Anything this harness
    does differently from ``FleetApp`` is a test that proves nothing —
    ``tests/test_ui_shell.py`` drives the real wiring for that reason.
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
        self._gesture_button: int | None = None

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

    def on_mouse_down(self, event: events.MouseDown) -> None:
        self._gesture_button = event.button

    def on_text_selected(self, event: events.TextSelected) -> None:
        button, self._gesture_button = self._gesture_button, None
        for pane in self.screen.query(TerminalPane):
            pane.selection_gesture_ended(button)

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


class SwitcherHost(App[None]):
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


async def _drag(
    pilot: Pilot[None], pane: TerminalPane, start: tuple[int, int], end: tuple[int, int]
) -> None:
    await pilot.mouse_down(pane, offset=start)
    await pilot.hover(pane, offset=((start[0] + end[0]) // 2, end[1]))
    await pilot.hover(pane, offset=end)
    await pilot.mouse_up(pane, offset=end)
    await pilot.pause()


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
            await _drag(pilot, pane, (0, 1), (5, 1))
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
            await _drag(pilot, pane, (0, 2), (4, 2))
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


def test_a_drag_below_the_output_neither_crashes_nor_selects_everything(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """Reproduced in review: ``Selection.extract`` re-splits with ``splitlines``,
    drops the trailing empty row, and a drag in the blank area under three rows
    of output raised IndexError out of the mouse handler — the whole UI down."""

    async def drive() -> tuple[str | None, str, Selection | None]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            await _drag(pilot, pane, (0, 5), (4, 5))
            return pane.selected_text(), host.clipboard, pane.text_selection

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
            await pilot.click(pane, offset=(8, 1), times=2)
            await pilot.pause()
            word, clip = pane.selected_text(), host.clipboard
            await pilot.click(pane, offset=(1, 0), times=3)
            await pilot.pause()
            return word, clip, pane.selected_text()

    word, clip, after_triple = run(drive())
    assert word == "row" and clip == "row"
    assert after_triple is None


def test_a_drag_into_the_notice_row_stays_a_drag(fake: FakeTmux, tmp_path: Path) -> None:
    """Every ``render_line`` return is offset-stamped: a drag that ends on the
    ``(exited)`` row used to resolve to select-all because that row was not."""
    pane = fake.panes["%1"]
    pane.dead, pane.dead_status = True, 0

    async def drive() -> tuple[Selection | None, str | None]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            widget = host.pane
            await wait_until(pilot, lambda: widget.notice == "(exited 0)")
            await _drag(pilot, widget, (0, 1), (3, 5))
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
            await _drag(pilot, pane, (6, 0), (11, 0))
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
    copy DISAGREE with the paint rather than agree with it — and ctrl+c seconds
    later copied a screen that was no longer under the highlight. Re-freezing per
    gesture is not available either; the widget never sees a press or a release
    that lands elsewhere, so nothing marks a gesture's end (rounds 4 and 5 found
    both halves of that). Both sides read the live rows now, so what is copied is
    what is shown, whenever it is asked for — which is what a terminal does."""

    async def drive() -> tuple[str, str, str]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            await pilot.mouse_down(pane, offset=(0, 1))
            await pilot.hover(pane, offset=(3, 1))
            fake.panes["%1"].screen = ["red plain", "MOVED UNDER", "third row"]
            await wait_until(pilot, lambda: "MOVED" in pane._lines[1])
            await pilot.hover(pane, offset=(5, 1))
            await pilot.mouse_up(pane, offset=(5, 1))
            await pilot.pause()
            on_release, painted = host.clipboard, rows(pane)[1].text[:6]
            # Later, with the highlight still standing and the agent still printing.
            fake.panes["%1"].screen = ["red plain", "LATER AGAIN", "third row"]
            await wait_until(pilot, lambda: "LATER" in pane._lines[1])
            pane.focus()
            await pilot.press("ctrl+c")
            await pilot.pause()
            return on_release, painted, host.clipboard

    on_release, painted, later = run(drive())
    assert on_release == painted == "MOVED ", "the copy is the row as painted at release"
    assert later == "LATER ", "and ctrl+c takes what is under the highlight when it is pressed"


def test_attach_to_another_pane_drops_the_selection(fake: FakeTmux, tmp_path: Path) -> None:
    fake.panes["%2"] = FakePane(screen=["other agent"], cursor=(0, 0))

    async def drive() -> Selection | None:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: synced(pane))
            await _drag(pilot, pane, (0, 1), (5, 1))
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
            await _drag(pilot, pane, (0, 1), (5, 1))
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
            await _drag(pilot, pane, (0, 2), (4, 2))
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
            await _drag(pilot, pane, (0, 2), (5, 2))
            dragged, toasts = host.clipboard, len(host.notices)
            # The right button comes down on the highlight and is released
            # elsewhere, so Textual does not clear the selection first.
            await pilot.mouse_down(pane, offset=(1, 2), button=3)
            await pilot.hover(pane, offset=(3, 2))
            await pilot.mouse_up(pane, offset=(3, 2))
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
            await pilot.click(pane, offset=(1, 1))
            await pilot.pause()
            return pane.has_focus

    focused = run(drive())
    assert focused, "the click focuses the pane"
    assert len(brokered) == 1, brokered


def test_the_notice_row_is_highlighted_by_the_same_drag_that_copies_it(
    fake: FakeTmux, tmp_path: Path
) -> None:
    """`_row_text` reports the notice as the row's text, so a drag copies it —
    while the early return that built the notice strip skipped every overlay, so
    it was the one row a selection never tinted (review)."""

    dead = fake.panes["%1"]
    dead.dead, dead.dead_status = True, 0

    async def drive() -> tuple[str | None, Style, Style]:
        host = Host(fake.server(tmp_path), "%1")
        async with host.run_test(size=(40, 6)) as pilot:
            pane = host.pane
            await wait_until(pilot, lambda: pane.notice == "(exited 0)")
            await _drag(pilot, pane, (0, 5), (4, 5))
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
                await pilot.mouse_down(other, offset=(1, 0))
                await pilot.hover(pane, offset=(5, 1))
                await pilot.mouse_up(pane, offset=(5, 1))
            else:
                await pilot.mouse_down(pane, offset=(0, 1))
                await pilot.hover(pane, offset=(5, 1))
                await pilot.mouse_up(other, offset=(1, 0))
            await pilot.pause()
            return host.clipboard, len(host.notices)

    copied, toasts = run(drive())
    assert copied != "", "the gesture highlighted text in the pane; it is copied"
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
            await pilot.mouse_down(pane, offset=(0, 2))
            await pilot.hover(pane, offset=(5, 2))
            await pilot.mouse_up(other, offset=(1, 0))
            await pilot.pause()
            host.screen.clear_selection()
            await pilot.pause()
            results = []
            for _ in range(2):
                await pilot.mouse_down(other, offset=(1, 0))
                await pilot.hover(pane, offset=(5, 1))
                await pilot.mouse_up(pane, offset=(5, 1))
                await pilot.pause()
                results.append(host.clipboard)
                host.screen.clear_selection()
                await pilot.pause()
            return results

    first, second = run(drive())
    assert first == second, (first, second)


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
            await _drag(pilot, pane, (0, 2), (5, 2))
            dragged, toasts = host.clipboard, len(host.notices)
            await pilot.mouse_down(pane, offset=(1, 2), button=3)
            await pilot.hover(pane, offset=(3, 2))
            await pilot.mouse_up(pane, offset=(3, 2))
            await pilot.pause()
            return dragged, host.clipboard, len(host.notices) - toasts

    dragged, after, new_toasts = run(drive())
    assert dragged == "third "
    assert after == dragged, "a right-button drag is not a copy request"
    assert new_toasts == 0


def test_a_backwards_selection_is_ordered_rather_than_raising() -> None:
    """Every other index in ``_extract`` is clamped; this one unpacked a slice
    that could be empty. Textual hands the widget a normalised selection today,
    so it is unreachable through the UI — but it is an unguarded ValueError in a
    mouse handler otherwise, the same class as the IndexError that took the app
    down in round 1 (review)."""
    rows = ["aaa", "bbb", "ccc"]
    assert _extract(Selection(Offset(1, 2), Offset(1, 0)), rows) == "aa\nbbb\nc"
    assert _extract(Selection(Offset(1, 0), Offset(1, 2)), rows) == "aa\nbbb\nc"
    assert _extract(Selection(Offset(2, 1), Offset(1, 1)), rows) == "b"


def test_a_selection_left_over_from_a_taller_pane_copies_nothing(
    fake: FakeTmux, tmp_path: Path
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
            pane.screen.selections = {pane: Selection(Offset(8, 8), Offset(4, 9))}
            await pilot.pause()
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
            await _drag(pilot, pane, (0, 2), (5, 2))
            dragged, toasts = host.clipboard, len(host.notices)
            fake.panes["%1"].screen = ["AAA", "BBB", "CCC"]
            await wait_until(pilot, lambda: "CCC" in pane._lines)
            await pilot.mouse_down(footer, offset=(1, 0))
            await pilot.hover(footer, offset=(6, 0))
            await pilot.mouse_up(footer, offset=(6, 0))
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
            await pilot.click(pane, offset=(8, 1), times=2)
            await pilot.pause()
            word, toasts = host.clipboard, len(host.notices)
            await pilot.mouse_down(footer, offset=(1, 0))
            await pilot.hover(footer, offset=(6, 0))
            await pilot.mouse_up(footer, offset=(6, 0))
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
            await _drag(pilot, pane, (0, 2), (5, 2))
            dragged, toasts = host.clipboard, len(host.notices)
            host.screen.clear_selection()
            await pilot.pause()
            await pilot.mouse_down(header, offset=(1, 0), button=3)
            await pilot.hover(pane, offset=(5, 1))
            await pilot.mouse_up(pane, offset=(5, 1))
            await pilot.pause()
            return dragged, toasts, host.clipboard, len(host.notices)

    dragged, toasts, after, toasts_after = run(drive())
    assert dragged == "third " and toasts == 1
    assert after == dragged, "a right-button drag is not a copy request, wherever it began"
    assert toasts_after == toasts


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
            await _drag(pilot, pane, (0, 1), (5, 1))
            first = host.clipboard
            fake.panes["%1"].screen = ["AAA newest", "BBB middle", "CCC bottom"]
            await wait_until(pilot, lambda: "CCC bottom" in pane._lines)
            # No intervening click: the first selection is still standing.
            await _drag(pilot, pane, (0, 2), (5, 2))
            second = host.clipboard
            # And again from outside the pane, which no press of ours precedes.
            fake.panes["%1"].screen = ["XXX one", "YYY two", "ZZZ three"]
            await wait_until(pilot, lambda: "ZZZ three" in pane._lines)
            await pilot.mouse_down(other, offset=(1, 0))
            await pilot.hover(pane, offset=(4, 2))
            await pilot.mouse_up(pane, offset=(4, 2))
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
            await _drag(pilot, pane, (0, 0), (39, 2))
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
            await _drag(pilot, widget, (0, 0), (39, 0))
            row = rows(widget)[0]
            return row.text, widget.selected_text(), style_at(row, 2), style_at(row, 35)

    shown, copied, left, over_marker = run(drive())
    assert "[↑" in shown, shown
    assert copied == shown.rstrip("\n")[:40], "what is copied is the row as displayed"
    assert copied is not None and "[↑" in copied, "the marker is on the row, so it copies"
    assert left.bgcolor == over_marker.bgcolor, "the whole dragged row is tinted, marker included"


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
    ``S-Enter`` into an agent running on a 3.4 one.
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
            pane.server = old.server(tmp_path)
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
