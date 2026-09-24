"""Shared plumbing for the pane tests: a fake tmux, and mouse gestures that take the app's own road.

Not a test module. ``tests/test_terminal_pane.py`` drives ``TerminalPane`` under a
small host app and ``tests/test_ui_shell.py`` drives the real ``FleetApp``; both
need a tmux that answers ``capture-pane`` with scripted rows, and both need to
press, drag and release the mouse the way the terminal driver does. Each file
used to carry its own copy of each — ``PaneScript`` in the shell tests was a
second fake tmux, and the shell test's gestures were hand-rolled — and copies
drift: a harness that ends a gesture differently from production is a test that
proves nothing (reviews of #120, rounds 6 and 9).

**Why the gestures are posted to the app rather than sent through ``Pilot``.**
``Pilot.mouse_down`` and friends call ``Screen._forward_event`` directly and
document that they "bypass event processing in ``App.on_event``". The real
driver posts every mouse event to the app, and ``App.on_event`` is where the
app records which button began a gesture and where Textual chains clicks into
double and triple clicks. A pilot-driven test therefore never exercises either;
these helpers post the same events the driver would, so the code under test is
the code that runs.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from textual import events
from textual.geometry import Offset
from textual.pilot import Pilot
from textual.widget import Widget

from aisquare.core.tmux import Completed, TmuxServer

# --- the fake tmux --------------------------------------------------------------------

_FORMAT_FIELD = re.compile(r"#\{(\w+)\}")
_SGR = re.compile(r"\x1b\[[0-9;]*m")


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
    wrapped: set[int] = field(default_factory=set)
    """Screen rows (0 = the top of the visible screen) that tmux soft-wrapped into
    the row below — the ``W`` flag ``capture-pane -F`` prints for them."""

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

    def __init__(self, *, record: list[tuple[str, ...]] | None = None) -> None:
        self.panes: dict[str, FakePane] = {}
        self.captures: list[tuple[str, int]] = []
        """``(pane_id, scrollback)`` per ``capture-pane``."""
        self.capture_rows: list[int] = []
        """Rows each ``capture-pane`` piped back — what the subprocess actually
        transferred and the widget actually split, one entry per capture."""
        self.flag_captures = 0
        """``capture-pane -F`` calls — the frames that carried tmux's wrap flags."""
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
        self.record = record
        """Every argv this fake was asked to run, when a caller wants them — the
        shell tests' socket guard reads them after the test."""

    def server(self, tmp_path: Path, socket: str = "fake") -> TmuxServer:
        # ``binary`` must resolve through ``shutil.which`` on a machine WITHOUT
        # tmux: an absolute executable path does, and is never run. ``socket``
        # names the server: two fakes standing for two SERVERS take two sockets,
        # as two servers do (the widget keys its too-old notice by socket).
        return TmuxServer(socket, binary=sys.executable, conf=tmp_path / "fake.conf", runner=self)

    def sent(self) -> list[tuple[str, ...]]:
        """Every ``send-keys`` after ``-t <pane>``."""
        return [call[2:] for call in self.input if call[0] == "send-keys"]

    def __call__(self, argv: Sequence[str], stdin: bytes | None) -> Completed:
        args = list(argv)
        if self.record is not None:
            self.record.append(tuple(args))
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
            if "-e" not in group:
                rows = [_SGR.sub("", row) for row in rows]  # tmux prints plain text without -e
            if "-F" in group:
                # tmux 3.7c prints the line's flags, then a space, then the
                # line: ``W`` for a row wrapped into the next, ``-`` for none
                # (measured; ``X`` for extended cells rides along the same way).
                self.flag_captures += 1
                rows = [
                    ("W " if index - scrollback in pane.wrapped else "- ") + row
                    for index, row in enumerate(rows)
                ]
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


# --- the socket guard the UI test modules share --------------------------------------------


def socket_of(argv: Sequence[str]) -> str | None:
    """The ``-L <socket>`` a tmux argv addresses, or ``None`` when it names none."""
    args = list(argv)
    return args[args.index("-L") + 1] if "-L" in args else None


def asks_a_server(argv: Sequence[str]) -> bool:
    """Whether a tmux argv reaches a SERVER at all.

    ``tmux -V`` asks the binary its version and touches no socket — the pane
    reads it once per attach to decide which flags the server knows (extended
    chords, ``capture-pane -F``) — so it can address no fleet, ours or anyone's.
    The guard that every other argv must name the test's private socket leaves
    it alone; a copy of that guard per test module drifted on exactly this
    (the shell tests and the accounts tests each carried one).
    """
    return list(argv)[1:] != ["-V"]


# --- mouse gestures, the driver's way ---------------------------------------------------


def _at(widget: Widget, offset: tuple[int, int]) -> Offset:
    """Screen coordinates of ``offset`` within ``widget`` — what the driver reports."""
    return widget.region.offset + Offset(*offset)


def mouse_event(
    kind: type[events.MouseEvent], widget: Widget, offset: tuple[int, int], button: int
) -> events.MouseEvent:
    """One mouse event as the driver would post it, to hand to ``app.post_message``
    yourself — for a burst posted with no turn of the loop between events."""
    x, y = _at(widget, offset)
    # The shape ``_xterm_parser.parse_mouse_code`` builds: no widget, screen
    # coordinates, and the button that is down (0 on a plain move).
    return kind(None, x, y, 0, 0, button, False, False, False, screen_x=x, screen_y=y)


async def press(
    pilot: Pilot[Any], widget: Widget, offset: tuple[int, int], *, button: int = 1
) -> None:
    """The pointer arrives at ``offset`` within ``widget`` and a button goes down there.

    A press is preceded by the motion that brought the pointer there, as the
    driver reports it (and as ``Pilot.mouse_down`` posts it).
    """
    pilot.app.post_message(mouse_event(events.MouseMove, widget, offset, 0))
    pilot.app.post_message(mouse_event(events.MouseDown, widget, offset, button))
    await pilot.pause()


async def move(
    pilot: Pilot[Any], widget: Widget, offset: tuple[int, int], *, button: int = 0
) -> None:
    """The pointer moves to ``offset`` within ``widget``, with ``button`` held (0: none)."""
    pilot.app.post_message(mouse_event(events.MouseMove, widget, offset, button))
    await pilot.pause()


async def release(
    pilot: Pilot[Any], widget: Widget, offset: tuple[int, int], *, button: int = 1
) -> None:
    """The pointer arrives at ``offset`` within ``widget`` with ``button`` held, and it comes up.

    The motion matters: Textual moves a selection's end on MouseMove, never on
    MouseUp, so a release the pointer had not moved to would leave the
    selection where the last motion put it — a gesture no terminal delivers.
    """
    pilot.app.post_message(mouse_event(events.MouseMove, widget, offset, button))
    pilot.app.post_message(mouse_event(events.MouseUp, widget, offset, button))
    await pilot.pause()


async def click(
    pilot: Pilot[Any],
    widget: Widget,
    offset: tuple[int, int],
    *,
    times: int = 1,
    button: int = 1,
) -> None:
    """``times`` presses and releases in one cell, in quick succession — so the
    app's own click chaining turns two into a double click and three into a
    triple. The loop turns between clicks, as it does between a person's: each
    click's ``Click`` event is handled before the next press arrives."""
    pilot.app.post_message(mouse_event(events.MouseMove, widget, offset, 0))
    for _ in range(times):
        pilot.app.post_message(mouse_event(events.MouseDown, widget, offset, button))
        pilot.app.post_message(mouse_event(events.MouseUp, widget, offset, button))
        await pilot.pause()


async def drag(
    pilot: Pilot[Any],
    widget: Widget,
    start: tuple[int, int],
    end: tuple[int, int],
    *,
    button: int = 1,
    to: Widget | None = None,
) -> None:
    """Press at ``start`` in ``widget``, move, and release at ``end`` — in ``to``
    when the release lands on another widget."""
    target = widget if to is None else to
    await press(pilot, widget, start, button=button)
    await move(pilot, target, ((start[0] + end[0]) // 2, end[1]), button=button)
    await move(pilot, target, end, button=button)
    await release(pilot, target, end, button=button)
