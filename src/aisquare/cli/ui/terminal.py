"""``TerminalPane`` — a tmux pane rendered inside the fleet UI, keys forwarded.

The risky core of docs/plans/fleet-tui.md (§6, §4.3, §3.1): tmux is the
terminal emulator, this widget is a viewport onto one of its panes.

Rendering. A frame is one ``server.capture`` (one tmux process:
``capture-pane -e -F`` + ``display-message``; ``-F`` only on a server that
knows it). Each captured row is a string with SGR escapes; rows are diffed
against the previous frame as STRINGS, and only the rows that changed — plus
the old and new cursor rows — are marked dirty, so Textual's Line API
(:meth:`render_line`) is asked for exactly those. A row string becomes a
:class:`Strip` through ``rich.text.Text.from_ansi`` — tabs expanded, because
tmux prints a tab cell as a literal TAB and pads the row as if expanded —
cached by the string, so a row that scrolled by one line is a dict lookup; the
row's text model (:class:`DisplayedRow`) is cached beside it. The ``W`` flags
say which rows tmux soft-wrapped and travel WITH the frame, so a copy joins
the rows it shows and never asks tmux a second time (review of #135, second
round). The cursor is a reverse-video cell (underline while the pane is
unfocused) when tmux says it is visible and the view is live (scrollback 0).

Rows are painted UNSTAMPED. The compositor reads a drag's content offset from
segment metadata, and stamping every painted row with it gave each segment a
unique Rich link id, which defeated Textual's per-style caches: plain mouse
hover repainted the whole pane at every segment crossing (120 motion reports
on a 200x60 pane: 7200 ``render_line`` calls), streaming frames cost twice the
CPU, and a full strip cache held tens of MB of stamped copies (review of #135,
finding 3). The compositor asks :meth:`render_line` directly, and only there,
when it resolves a press or a drag; painting goes through :meth:`render_lines`,
so that one call is the only one stamped — in CELLS, one segment per grapheme
whose character count and cell count differ (see :class:`DisplayedRow`).

Cadence. A one-shot timer re-arms itself after every frame: :attr:`FAST_INTERVAL`
(50 ms) while the last frame changed something, :attr:`IDLE_INTERVAL` (500 ms)
once nothing moves. The frame diff IS the activity signal: tmux's own
``window_activity_flag`` only clears when an attached client looks at the
window, and no client ever attaches to a fleet window, so it would read "active"
forever. A pane that is not on screen (a hidden tab) is not captured at all.
Nothing here runs off the event loop. Measured against tmux 3.7c on
2026-08-28 while a process streamed coloured text (median of 150 frames):
80x24 — capture 1.75 ms + Strips 0.17 ms = 1.94 ms; 200x60 — 2.31 + 0.48 =
2.85 ms (p95 3.6 ms); an idle frame with the Strip cache warm costs the tmux
fork alone, ~1.7-2.1 ms. At the 20 fps ceiling that is 3.9 % and 5.7 % of one
core against the plan's 15 % go/no-go, so a fork per tick stays the design
until the control-mode client (§3.1) replaces polling.

Scrollback (§6). The wheel moves our own offset ``k`` over the pane's history,
and the last-seen pane height goes with every capture: tmux is asked for
``-S -k -E (H-1-k)``, one screen, instead of history-to-bottom. Without the
bound a frame at ``k`` pipes ``k + H`` rows out of the subprocess and splits
them all to keep ``H`` — reading the top of a 50 000-line Claude session, that
is 50 000 rows per tick on the event loop (measured with the fake tmux: 3006
rows unbounded against 6 bounded, at ``k=3000`` in a 6-row widget). A stale
hint yields a short frame, which ``core.tmux.capture`` detects and refetches
unbounded — one extra process, only then.

Input (§4.3). With the pane focused every key goes to tmux through
``core.keys.translate`` — literal text via ``send-keys -l``, everything else by
tmux's key name — except the escape hatch (``F12`` by default), which posts
:class:`EscapeToSidebar` and is never forwarded. A key tmux has no safe name
for is never sent under a guessed one: the text the terminal reported with it
is typed instead, a Cmd chord aside, and with none nothing is sent. For what
was not sent ``translate``'s reason decides what is said: a chord the reader
meant gets ONE quiet notice per key name in the pane, a chord this tmux is too
old to carry a warning that names the version, once per key name on each
server, and a key with nothing to type — a modifier, a lock, a Cmd chord, a
whole key a kitty-protocol terminal reports only because Textual asked for
every key — nothing at all (#151). ``Paste`` goes through the paste buffer so
the agent sees one bracketed paste. The wheel scrolls our own offset over the
pane's history (clamped to ``history_size``); any key returns to live.
``Resize`` is forwarded as ``resize-window`` after a 100 ms debounce. Forwarded
input re-arms the fast cadence, so an echo never waits for the idle tick.

Selection (§4.3). The pane owns its highlight; :class:`TerminalPane`'s docstring
states the rules — who sees a gesture, when a highlight is dropped, and which
one key path copies — and :class:`SelectionHost` is the app half of them.

Mouse (#148). A program that tracks the mouse (Claude Code's fullscreen renderer
turns on ``?1000`` + ``?1006``) gets the pointer as the events it asked for —
wheel notches, and button presses, drags and releases, SGR-encoded with the
modifier bits (shift 4, alt 8, ctrl 16; X10 when that is what it asked for) — so
a click places Claude Code's cursor, the ``✕`` on its diff panel and its menu
rows respond, and a drag selects inside Claude Code, which copies on release by
itself. While such a program owns the mouse this widget's own drag-select stands
down (:meth:`TerminalPane.allow_select`); shift+drag is the one gesture that
always selects locally, through this widget, and copies on release. After a
forwarded left-button release the widget reads tmux's newest paste buffer — what
Claude Code writes inside tmux when its own clipboard write cannot reach a
display — and mirrors a changed one to the outer clipboard. Measured before the
change (issue #148, 2026-09-12): tmux passes the sequences byte for byte with
``mouse off``; the pane simply never sent them.

Failing open. A pane that vanishes, a dead pane, an unavailable tmux, or an
unexpected error in the render loop never take the app down: the last frame
stays on screen with a notice in its bottom row — ``(pane gone)``,
``(exited N)``, ``(tmux unavailable)`` — and polling backs off to idle. What
that costs: one row of the agent's last screen is covered by the notice, and
keys pressed into a gone pane are dropped (the notice is the only feedback).
A failed ``resize-window`` fails open the same way but is RETRIED, on a
widening backoff (:attr:`TerminalPane.RESIZE_RETRY` doubling to
:attr:`TerminalPane.RESIZE_RETRY_MAX`): its only other caller is a ``Resize``
event, so one transient failure used to leave the tmux window at its spawn
geometry (``spawn_window`` defaults to 200x50) for the life of the view while
captures kept succeeding — the widget then shows the bottom ``height`` rows of
that screen with every row truncated to its width, so wrapped output is cut
mid-line and the cursor sits off screen. What the retry costs is one tmux
process per attempt while the pane stays unreachable (~2 in the first second,
then ever fewer).
"""

from __future__ import annotations

import contextlib
import itertools
import time
import weakref
from bisect import bisect_left
from collections.abc import Callable
from functools import partial
from typing import Any, ClassVar, Literal, NamedTuple, assert_never

from rich.cells import cell_len, set_cell_size, split_graphemes
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import events
from textual.actions import SkipAction
from textual.app import App
from textual.dom import NoScreen
from textual.geometry import Offset, Region
from textual.message import Message
from textual.notifications import SeverityLevel
from textual.screen import Screen
from textual.selection import SELECT_ALL, Selection
from textual.strip import Strip
from textual.timer import Timer
from textual.widget import Widget

from aisquare.core.keys import (
    ARGV_SEPARATOR,
    EXTENDED_MINIMUM,
    Drop,
    Translation,
    translate,
)
from aisquare.core.tmux import (
    WRAP_FLAGS_MINIMUM,
    PaneFacts,
    TmuxError,
    TmuxServer,
    TmuxUnavailable,
)
from aisquare.services import fleet as fleet_service

CURSOR = Style(reverse=True)
UNFOCUSED_CURSOR = Style(underline=True)
PLACEHOLDER = Style(dim=True)
SCROLL_MARKER = Style(reverse=True, bold=True)
"""The ``[↑k/history]`` corner marker while the view is in history."""
NOTICE = Style(dim=True, italic=True)

NO_PANE = "(no agent selected)"
PANE_GONE = "(pane gone)"
TMUX_UNAVAILABLE = "(tmux unavailable)"
NOTHING_TO_COPY = "nothing to copy — no text under the highlight"
"""What a left drag is told when it left a highlight with no text under it: once for
the gesture, by :func:`route_selection_gesture`, or by the pane that ran a shift+drag
itself (:meth:`TerminalPane._end_shift_drag`)."""


class DisplayedRow:
    """One row as the pane displays it: its text, its graphemes' cell boundaries,
    and whether tmux wrapped it into the row below.

    ONE model of a row for the paint and the copy, in CELLS. Selection offsets
    are cell positions (what :meth:`TerminalPane.render_line` stamps), and every
    crop, tint, cursor cell, word boundary and copied slice is snapped to the
    grapheme boundaries of the text ``rich`` actually draws. Measuring per code
    point with ``cell_len`` — what every earlier version did, and what Textual's
    compositor does — disagrees with rich 15 and tmux 3.7c on VS16 emoji (``⚠️``
    is one 2-cell grapheme, per code point ``[1, 0]``) and ZWJ sequences
    (``👩‍🚀``: ``[2, 0, 2]`` for 2 cells), so an overlay crop cut through them and
    corrupted the row, and the copied text differed from the drawn text (review
    of #135, finding 8). ``rich.cells.split_graphemes`` is the same table the
    drawing uses, so the two cannot disagree.
    """

    __slots__ = ("bounds", "starts", "text", "wrapped")

    def __init__(self, text: str, *, wrapped: bool = False) -> None:
        self.text = text
        self.wrapped = wrapped
        """tmux soft-wrapped this row into the next: a copy joins them without a newline."""
        bounds = [0]
        starts = [0]
        for _start, end, width in split_graphemes(text)[0]:
            bounds.append(bounds[-1] + width)
            starts.append(end)
        self.bounds: tuple[int, ...] = tuple(bounds)
        """Cell position at which grapheme ``i`` starts; the last entry is the row's cell width."""
        self.starts: tuple[int, ...] = tuple(starts)
        """Character index at which grapheme ``i`` starts; the last entry is ``len(text)``."""

    @property
    def cells(self) -> int:
        return self.bounds[-1]

    def _floor(self, cell: int) -> int:
        """The grapheme whose start is the last boundary at or before ``cell``."""
        index = bisect_left(self.bounds, cell)
        if index == len(self.bounds) or self.bounds[index] != cell:
            index -= 1
        return index

    def _ceil(self, cell: int) -> int:
        """The grapheme whose start is the first boundary at or after ``cell``."""
        return bisect_left(self.bounds, cell)

    def snap(self, start: int, end: int) -> tuple[int, int]:
        """``[start, end)`` in cells, widened to grapheme boundaries of the text.

        Only WITHIN the text. Past its last grapheme every cell is one cell wide
        and blank, so there is no boundary to widen to — and widening anyway
        stretched a span that starts out there back to the end of the text: the
        cursor on any quiet shell row became a black bar from the text to the
        cursor (review of #120, round 3).
        """
        cells = self.cells
        snapped_start = self.bounds[self._floor(start)] if 0 <= start < cells else start
        snapped_end = self.bounds[self._ceil(end)] if 0 <= end < cells else end
        return snapped_start, snapped_end

    def slice(self, start: int, end: int) -> str:
        """The text under cells ``[start, end)``, whole graphemes, nothing past the text."""
        start, end = self.snap(max(start, 0), end)
        cells = self.cells
        if start >= cells or end <= start:
            return ""
        return self.text[self.starts[self._floor(start)] : self.starts[self._ceil(min(end, cells))]]

    def word_at(self, cell: int) -> tuple[int, int] | None:
        """The cell span of the run of non-blank graphemes under ``cell``, if any."""
        if not 0 <= cell < self.cells:
            return None
        index = self._floor(cell)

        def blank(i: int) -> bool:
            return self.text[self.starts[i] : self.starts[i + 1]].isspace()

        if blank(index):
            return None
        first, last = index, index
        while first > 0 and not blank(first - 1):
            first -= 1
        while last + 1 < len(self.starts) - 1 and not blank(last + 1):
            last += 1
        return self.bounds[first], self.bounds[last + 1]


class Shown(NamedTuple):
    """What the pane displays at one moment, before any overlay: the frame's rows,
    the bottom-row notice standing in for the last row, and the corner marker's
    numbers on row 0 (``None`` while live).

    One value for the paint, the copy and the staleness check, so "did the text
    under the highlight change" is asked of the rows the user SEES. The check
    used to read the raw frame lines, so a change hidden under the marker, or
    under the notice, dropped a highlight for text nobody could see change —
    and a notice arriving under a highlight (``_fail``) was never checked at
    all (review of #135, second round, findings 4 and 12).
    """

    lines: list[str]
    notice: str | None
    marker: tuple[int, int] | None


def _extract(selection: Selection, rows: list[DisplayedRow], width: int) -> str:
    """The text ``selection`` covers in ``rows`` — read off the SAME spans
    :meth:`Selection.get_span` hands :meth:`TerminalPane._render_row` to paint,
    on a widget ``width`` cells wide.

    Derived, not re-clamped. Every earlier version computed its own start and
    end from the endpoints, and each review found one more geometry the last
    clamp had not anticipated: a stale span over a shrunken pane, the same span
    with its columns reversed, one running off the bottom, one whose rows are
    reversed (``get_span`` reorders nothing, so it paints no row at all). A row
    ``get_span`` returns ``None`` for contributes nothing, by construction — so
    "nothing is highlighted" and "nothing is copied" cannot come apart, and a
    non-empty answer can no longer make ``_copy_selection`` report success and
    swallow the agent's ctrl+c (reviews of the seventh to ninth versions).

    Measured before replacing the hand-clamped body: over 149 000 combinations
    of row-sets and selections the two agree everywhere except where the old
    body disagreed with the PAINT — reversed rows, and a reversed column pair on
    one row — which it answered with text nobody had highlighted.
    """
    pieces: list[str] = []
    for y, row in enumerate(rows):
        span = selection.get_span(y)
        if span is None:
            continue
        start, end = span
        if start >= (width if end == -1 else min(end, width)):
            # No cell of this row is highlighted — a zero-width span, or one
            # that starts past the widget's width (a selection left over from a
            # wider pane) — so it contributes nothing, not even a newline:
            # ``_with_selection`` tints such a row nowhere, and the paint and
            # the copy must count the same rows (review of #120, round 10).
            continue
        pieces.append(row.slice(start, row.cells if end == -1 else end))
        # A row tmux soft-wrapped continues on the next one: joined, as tmux's
        # own copy mode joins it. Copying a wrapped command as three lines split
        # mid-token, with a space lost at each wrap, pasted into a shell as three
        # broken commands (review of #135, finding 9). Only when the span runs
        # to the row's end — a selection that stops short of it has no
        # continuation to join.
        pieces.append("" if row.wrapped and end == -1 else "\n")
    # Trailing newlines dropped, as Textual's own ``get_selected_text`` does. A
    # zero-width span is not ``None`` — the paint leaves that row untinted while
    # the join still gave it a newline — and a drag ending in the blank area
    # below the output, which is how this PR's reporter grabs a command, put a
    # run of Enters on the clipboard for a shell outside bracketed paste to
    # execute (review of the tenth version).
    return "".join(pieces).rstrip("\n")


_SERVER_VERSIONS: dict[str, tuple[int, int]] = {}
"""``tmux -V`` answers by socket — see :meth:`TerminalPane._server_version`."""


def forget_server_versions() -> None:
    """Drop every cached ``tmux -V`` answer — a new server is a new machine (tests)."""
    _SERVER_VERSIONS.clear()


DUPLICATE_PRESS_WINDOW = 0.5
"""Seconds within which a repeat of the pressed button is one press reported twice.

A terminal that double-reports does so within milliseconds; a human whose
release was lost — the pointer left the window with the button down — presses
again after a drag's worth of time. The window tells the two apart, and a
double-click is not in question: its second press finds nothing down. A lost
release is normally caught sooner, by the first move reported with no button
held (:meth:`SelectionHost.on_event`); the window is what is left for a
terminal that reports no motion without a button."""
_monotonic: Callable[[], float] = time.monotonic


def _sgr(kind: str, code: int, x: int, y: int) -> str:
    """One SGR (``?1006``) mouse report: ``ESC [ < b ; x ; y M`` — ``m`` for a release.

    ``b`` is the button with its modifier bits (wheel notches are 64 and 65);
    a drag adds 32, the motion flag. Coordinates are 1-based pane cells.
    """
    button = code + 32 if kind == "drag" else code
    return f"\x1b[<{button};{x};{y}{'m' if kind == 'release' else 'M'}"


def _x10(kind: str, code: int, x: int, y: int) -> bytes:
    """One X10 (``?1000``) report: three bytes after ``ESC [ M``, each 32 + value.

    A release is button 3 with the modifier bits kept, a drag adds 32, and a
    cell past 223 cannot be expressed, so it is clamped.
    """
    if kind == "release":
        button = 3 | (code & ~3)
    elif kind == "drag":
        button = code + 32
    else:
        button = code
    return b"\x1b[M" + bytes([32 + button, 32 + min(x, 223), 32 + min(y, 223)])


_MOUNTED_PANES: weakref.WeakSet[TerminalPane] = weakref.WeakSet()
"""Every mounted pane, so the start and end of a gesture reach them without a DOM walk.
Unordered: :func:`_tell_panes` gives the panes their order."""

_SELECTION_CLOCK = itertools.count(1)
"""Ticks once per pane selection that changed: the order the copy key reads
when more than one pane holds a highlight (:func:`copy_pane_selection`), and
the order a release tells the panes in (:func:`_tell_panes`)."""


def _tell_panes(app: App[Any], what: str, tell: Callable[[TerminalPane], object]) -> None:
    """Run ``tell`` on every pane of ``app``'s active screen, logging what fails.

    Read off the panes' own register rather than by querying the DOM: this runs
    on EVERY mouse press and release in the app — a click, a drag, a scrollbar
    grab, a button press — and ``query`` walks and filters the whole active
    screen to reach at most a handful of panes (reviews of #120, rounds 8 and
    10). Logged, never swallowed silently: resolving the screen, and anything a
    pane does with the news, are both places this widget's history says an
    unguarded exception in a mouse handler takes the app down.

    Told in one order, the pane whose selection changed least recently first
    (:attr:`TerminalPane.selected_at`) — never the register's, which is a
    ``WeakSet`` iterated in hash order. At a release every pane whose highlight
    the gesture changed copies, so the last one told is the one the clipboard
    keeps: in hash order that was whichever pane happened to hash last (review
    of #120, round 11); in this order it is the highlight made most recently,
    the one the copy key takes first (:func:`copy_pane_selection`). The order
    is stamped inside the write that changed a highlight
    (:meth:`PaneScreen.watch_selections`), so a release routed straight after a
    drag's last move reads that move (review of the #167 fold, F2).
    """
    try:
        screen = app.screen
    except Exception as error:  # no screen on the stack
        app.log.error(f"{what}: no screen to tell", error)
        return
    for pane in sorted(_MOUNTED_PANES, key=lambda pane: pane.selected_at):
        # The filter answers apart from the pane's own handler: a pane detached
        # from the DOM while still registered here raises ``NoScreen`` from
        # ``pane.screen`` — it is not on the active screen, which is the
        # filter's answer, and was logged as "failed for a pane" at every
        # press and release in the app (review of #120, round 11). The line
        # below means what it says: the pane's handler raised.
        try:
            on_screen = pane.is_mounted and pane.screen is screen
        except NoScreen:
            on_screen = False
        if not on_screen:
            continue
        try:
            tell(pane)
        except Exception as error:
            app.log.error(f"{what} failed for a pane", error)


GestureEnd = Literal["copied", "blank"]
"""What a release did in one pane (:meth:`TerminalPane.selection_gesture_ended`):
copied its highlight, or dropped one with no text under it."""


def route_gesture_start(app: App[Any]) -> None:
    """A mouse button went down somewhere on ``app``: every pane on the active
    screen notes the selection it has now, which is what a release must differ
    from to be that pane's copy (see :meth:`TerminalPane.selection_gesture_started`)."""
    _tell_panes(app, "selection gesture start", TerminalPane.selection_gesture_started)


def route_selection_gesture(app: App[Any], button: int | None, *, moved: bool = True) -> None:
    """The button came up: every pane on the active screen may copy what the
    gesture left highlighted in it (see :meth:`TerminalPane.selection_gesture_ended`).

    ONE routing, called by :class:`SelectionHost` — which the shell and every
    test host are — so the two cannot drift: a harness that ends a gesture
    differently from production is a test that proves nothing, which is how a
    broken cross-widget copy passed review twice (reviews of #120, rounds 6
    and 9). Every pane is told, always: a pane's per-gesture state is reset at
    the next press, not here, so there is nothing an early return could leave
    armed (review of #120, round 9; review of #135, finding 6). The most
    recently selected pane is told last, so when one drag changed two panes
    the clipboard ends with what the copy key would copy (``_tell_panes``).

    "Nothing to copy" is said here, once for the gesture, and never beside a
    copy. Each pane used to say its own: a drag from one pane's text into the
    blank rows of another said "copied 11 characters" and then — the blank
    pane was where the drag ended, so it was told last — "nothing to copy",
    with the copy on the clipboard (review of the #167 fold, F1).

    Nor is it said when the pointer came up within a cell of where it went
    down (``moved`` false). Textual's screen clears the selections at a
    release on the press's own cell, its click; one cell off, the move in
    between leaves a highlight, and over blank rows a focus click whose hand
    drifted was told "nothing to copy" (review of the #167 fold, F3). Such a
    gesture asked for no copy: its blank highlight still goes, without a word.
    """
    ended: list[GestureEnd | None] = []
    _tell_panes(
        app, "selection gesture", lambda pane: ended.append(pane.selection_gesture_ended(button))
    )
    if button == 1 and moved and "blank" in ended and "copied" not in ended:
        app.notify(NOTHING_TO_COPY, markup=False)


def route_lost_release(app: App[Any]) -> None:
    """The gesture's release was lost: every pane on the active screen ends the
    part of it the pane runs itself (see :meth:`TerminalPane.gesture_release_lost`).

    Called by :class:`SelectionHost` at each door it reads a lost release by,
    alongside ``_end_screen_drag``: the screen's drag-select and a pane's own
    gestures — a press forwarded to the program, a shift+drag (#148) — are the
    three things a release would have ended, and the app is the only place
    that learns it never came.
    """
    _tell_panes(app, "lost release", TerminalPane.gesture_release_lost)


def copy_pane_selection(app: App[Any]) -> bool:
    """Copy the pane highlight made MOST RECENTLY on ``app``'s active screen; whether one was.

    The copy key outside a pane: :class:`PaneScreen` asks this before Textual's
    own copy, so ctrl+c from the sidebar copies exactly what the pane's release
    copied — its own rows, through its own path — and clears the highlight the
    same way the pane's focused ctrl+c does (review of #135, finding 10).

    Most recent, by :attr:`TerminalPane.selected_at`: the panes are kept in a
    ``WeakSet``, and "the first one" in a set is whichever hash order yields,
    which changed from run to run (review of #135, second round, finding 3).
    A pane whose highlight extracts as nothing is passed over for the next.
    """
    standing: list[TerminalPane] = []

    def tell(pane: TerminalPane) -> None:
        if pane.has_standing_selection():
            standing.append(pane)

    _tell_panes(app, "copy key", tell)
    for pane in sorted(standing, key=lambda pane: pane.selected_at, reverse=True):
        try:
            if pane.copy_standing_selection():
                return True
        except Exception as error:
            app.log.error("copy key failed for a pane", error)
    return False


class PaneScreen(Screen[None]):
    """The default screen of a :class:`SelectionHost`: one copy key for the whole app.

    Textual binds ``ctrl+c,super+c`` to ``screen.copy_text`` on every screen and
    that action copies every widget's selection joined, with no toast, and copies
    the empty string when the selections extract as nothing. With a focused pane
    the pane's own ``on_key`` handles the key first; everywhere else the key
    landed here and disagreed with the pane: a hidden pane's stale entry wiped
    the clipboard with an empty OSC 52, and after a drag from the agent header
    the promised "ctrl+c copies again" copied the header line as well (review
    of #135, finding 10). Now a standing pane highlight is copied by the pane,
    from anywhere; only when no pane has one does Textual's copy run, and it
    never copies nothing.

    It also keeps the clock the panes are ordered by (:meth:`watch_selections`).
    """

    def watch_selections(self, old: dict[Widget, Selection], new: dict[Widget, Selection]) -> None:
        """Stamp every pane whose highlight this write changed, as the write happens.

        :attr:`TerminalPane.selected_at` orders the panes for a release
        (:func:`_tell_panes`) and for the copy key. The pane used to stamp it
        in ``selection_updated``, which Textual calls from
        ``Screen._watch_selections`` — a coroutine, queued on this screen's
        loop. A drag writes the selections inside ``App.on_event``, and a burst
        routed its release before the watcher had run for the last move: the
        release read the ticks of the gesture before, and copied the pane the
        drag began in (review of the #167 fold, F2). A plain watcher runs
        inside the assignment, so every stamp is in place before the next
        event is handled.

        One write can change two panes — the move that carries a drag across
        into the next one — and Textual's own order for them is its set's. The
        pane under the pointer is stamped last: the drag's end is there, and
        the highlight it is still making is the newest.
        """
        changed = [
            widget
            for widget in old.keys() | new.keys()
            if isinstance(widget, TerminalPane)
            and new.get(widget) not in (None, SELECT_ALL)
            and new.get(widget) != old.get(widget)
        ]
        pointer = self.app.mouse_position
        for pane in sorted(changed, key=lambda pane: pane.region.contains_point(pointer)):
            pane.stamp_selection()

    def action_copy_text(self) -> None:
        if copy_pane_selection(self.app):
            return
        text = self.get_selected_text()
        if not text:
            raise SkipAction()
        self.app.copy_to_clipboard(text)


class SelectionHost(App[None]):
    """An app that hosts panes: the app half of pane selection, in one class.

    ``FleetApp`` and the test host both derive from this, so the shell and its
    tests cannot drift — the test host once mirrored the shell's handlers by
    hand and fell behind, leaving twenty gesture tests exercising an end of
    gesture production did not have (review of #120, round 9).

    Presses and releases are read in :meth:`on_event`, the driver's entry into
    the app, BEFORE the event is forwarded and starts bubbling. That is what
    makes the button pairing exact: the previous design recorded the button
    from the bubbled ``MouseDown`` (pane → view → switcher → screen → app, one
    queue hop each) and read it from ``TextSelected``, which the screen posts
    one hop from the app, so a burst of input handled back-to-back routed a
    release with the previous gesture's button and left this gesture's behind
    for the next one — measured: 7 of 18 bursts misrouted (review of #135,
    finding 7). Here both halves of a gesture pass through one method, in
    order, and the selection the screen wrote during the gesture is already
    final when the release is routed (``Screen._forward_event`` handles the
    release synchronously inside ``super().on_event``).
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._pressed: int | None = None
        """The button of the press now down, until its release is routed."""
        self._stray: int | None = None
        """A second button pressed while ``_pressed`` was down: dropped, with its release."""
        self._pressed_at = 0.0
        """When ``_pressed`` went down — a repeat of it inside :data:`DUPLICATE_PRESS_WINDOW`
        is the terminal reporting one press twice; later, its release was lost."""
        self._pressed_offset: Offset | None = None
        """Where on the screen ``_pressed`` went down: a release within a cell of it is a
        click the hand drifted on, not a drag (:func:`route_selection_gesture`)."""

    def get_default_screen(self) -> Screen[None]:
        return PaneScreen(id="_default")

    def copy_to_clipboard(self, text: str) -> None:
        """Textual's OSC 52 write — except that nothing is ever written for ``""``.

        An OSC 52 with an empty payload CLEARS the terminal's clipboard, and no
        gesture in this app means that: Textual's ``screen.copy_text`` produced
        one whenever the screen's selections extracted as nothing (review of
        #135, finding 10). Every copy in the app goes through here, so the rule
        holds for the pane's own paths and Textual's alike.
        """
        if not text:
            return
        super().copy_to_clipboard(text)

    def _end_screen_drag(self) -> None:
        """End the active screen's drag-select the way its ``MouseUp`` would have.

        Textual's ``Screen`` drag-selects from a press until a ``MouseUp``
        clears ``_selecting``, and every move in between carries the
        highlight's end to the pointer. A lost release never arrives, so ending
        only the gesture left the screen selecting: each bare move after the
        copy dragged the highlight along, and the "ctrl+c copies again" the
        toast had just promised copied the rows the pointer wandered over since
        (review of #203, round 1 of the fold delta). ``_selecting`` and
        ``_mouse_down_offset`` are Textual's PRIVATE state, set here exactly as
        ``Screen._forward_event`` sets them on a release (textual 8.2); a rename
        of ``_selecting`` fails ``test_a_lost_release_leaves_the_highlight_that_was_copied``.
        """
        try:
            screen = self.screen
        except Exception as error:  # no screen on the stack
            self.log.error("lost release: no screen to end the drag on", error)
            return
        screen._selecting = False
        screen._mouse_down_offset = None

    async def on_event(self, event: events.Event) -> None:
        pressed = isinstance(event, events.MouseDown) and not event.is_forwarded
        released = isinstance(event, events.MouseUp) and not event.is_forwarded
        if (
            isinstance(event, events.MouseMove)
            and not event.is_forwarded
            and event.button == 0
            and self._pressed is not None
        ):
            # A move with NO button held while a gesture is down: its release
            # was lost — let go outside the terminal, or the driver dropped it —
            # and this is the first report that says so. The gesture ends here,
            # routed with its button BEFORE the move is forwarded, so a drag
            # copies where it got to, not where the bare pointer came back in.
            # Left armed, a press of the same button inside
            # DUPLICATE_PRESS_WINDOW was dropped as a duplicate and the next
            # drag extended the lost one (review of the fold with #167). A stray
            # is left to its own release or the next press: its release reaching
            # the screen is the damage the stray rule exists to prevent. The
            # screen's drag ends with it, or this move and every one after it
            # would go on moving the highlight that was just copied — and so do
            # the gestures a pane runs itself (#148), which hold the pointer.
            lost, self._pressed = self._pressed, None
            self._end_screen_drag()
            route_lost_release(self)
            route_selection_gesture(self, lost)
        # ONE gesture at a time. A second button pressed while one is down is
        # not a new gesture — and it is not the screen's to see either. Recording
        # it overwrote ``_pressed`` and re-baselined every pane to the selection
        # the drag had built so far, so the left button's release routed as the
        # other button; and forwarded, Textual's screen restarts its selection
        # at every ``MouseDown`` and reads the ``MouseUp`` that lands on the same
        # cell as a click that CLEARS it — so the drag's highlight was gone
        # before its own release arrived, and the copy was silently dropped
        # (review of #203, round 4). The stray button's press and release are
        # dropped here, before either reaches the screen. The same button
        # pressed again re-arms: its release was lost (the pointer left the
        # terminal), and refusing it would leave every later gesture unrouted.
        if pressed:
            assert isinstance(event, events.MouseDown)
            if self._pressed is not None:
                # One gesture at a time, and that includes a REPEAT of the
                # gesture's own button: a terminal that reports a press twice
                # would otherwise re-baseline every pane mid-drag and let the
                # screen restart its selection at the duplicate's cell — the
                # dropped copy of round 5, arriving by the press (round 7). A
                # stray's button is remembered so its own release is dropped too.
                if event.button != self._pressed:
                    self._stray = event.button
                    return
                if _monotonic() - self._pressed_at < DUPLICATE_PRESS_WINDOW:
                    return
                # The same button, pressed again long after: its release was
                # lost (the pointer left the terminal with it down), and refusing
                # it would leave every later gesture unrouted (round 4). A new
                # gesture — and it takes the screen's selection with it, as any
                # press does. A pane that ran the lost one lets go of the
                # pointer first, so this press lands where it was made (#148).
                route_lost_release(self)
            # A new gesture starts clean: a stray whose release never arrived
            # (the pointer left the terminal) used to outlive its gesture, and
            # the next press of THAT button was accepted while its release was
            # dropped as the stray's — leaving `_pressed` armed forever, every
            # later left press classified as stray and dropped, and nothing in
            # the app clickable (review of the fold). The stray is cleared by its
            # own release, or here.
            self._stray = None
            self._pressed = event.button
            self._pressed_at = _monotonic()
            self._pressed_offset = event.screen_offset
            route_gesture_start(self)
        moved = True
        if released:
            assert isinstance(event, events.MouseUp)
            # While a gesture is down, the only release that is its own names
            # the button that began it: anything else — the stray's release, a
            # DUPLICATE of it from a terminal that reports releases twice — is
            # dropped before it reaches the screen, where it would synthesise a
            # Click at the primary press's offset, clear the drag's highlight,
            # focus the pane and zero its double-click chain (round 5; a
            # one-shot stray token let the duplicate through, round 6). After
            # the gesture ended, the stray's own release is still the stray's
            # (`Down(1) Down(3) Up(1) Up(3)`, the commonest order).
            if self._pressed is not None and event.button != self._pressed:
                return
            if self._stray is not None and event.button == self._stray:
                self._stray = None
                return
            if self._pressed_offset is not None:
                drift = event.screen_offset - self._pressed_offset
                moved = max(abs(drift.x), abs(drift.y)) > 1
        try:
            await super().on_event(event)
        finally:
            # Routed even when the app's own handling of the release raised —
            # ``App.on_event`` renders the widget under the pointer to read its
            # style, which is arbitrary widget code. Skipped, the release left
            # ``_pressed`` armed with this gesture's button for the next one,
            # and the pairing this class exists for was exact only on the happy
            # path (review of #135, second round, finding 8).
            if released:
                button, self._pressed = self._pressed, None
                route_selection_gesture(self, button, moved=moved)


class EscapeToSidebar(Message):
    """The user pressed the escape hatch: focus goes back to the sidebar."""


def _printable_name(key: str) -> str:
    """``key`` as a notice may carry it: every unprintable character spelt ``U+XXXX``.

    Textual names a key it has no name for after the character itself, so a
    raw C0/C1 byte a kitty-protocol terminal reports (``CSI 155 u`` is U+009B,
    a CSI introducer; U+0007 rings the bell) arrived here as the key name and
    went into the toast verbatim — ``markup=False`` only disables Rich markup,
    and the emulator honoured the byte (review of #161, round 5). The table's
    refusal keeps such a byte out of ``send-keys``; this keeps it off the
    reader's screen.
    """
    return "".join(char if char.isprintable() else f"U+{ord(char):04X}" for char in key)


class TerminalPane(Widget, can_focus=True):
    """One tmux pane, live. ``attach(pane_id)`` switches what it shows.

    **Selection — the rules, decided once** (§4.3). Every earlier version fixed
    one symptom at a time and the next review found the next one (reviews of
    #120, rounds 3 to 10; review of #135, findings 2, 6, 7, 10 and 13), so:

    1. *A gesture is what the app sees.* :class:`SelectionHost` reads every
       press and release in ``App.on_event`` and tells every pane on the active
       screen when a gesture starts (:meth:`selection_gesture_started`) and when
       it ends (:meth:`selection_gesture_ended`, with the button that began it)
       — at its release, or, when the release was lost, at the first move
       reported with no button held. A pane's own ``on_mouse_down`` only adds
       what the app cannot know: where in the pane the press landed. The one
       gesture a pane ends itself is the shift+drag it runs under a program
       that owns the mouse (#148): it is run from the pane's own queue, which
       the app's routing does not wait for, so it copies at the pane's own
       release (:meth:`_end_shift_drag`).
    2. *A release copies by value.* At the start of a gesture a pane notes the
       selection it has (its baseline); at the end it copies exactly when its
       selection differs from that baseline, and only for the left button. An
       unrelated release — a drag on the footer, a scrollbar, a right-button
       drag — leaves a standing highlight uncopied because nothing changed. No
       flag is set from the asynchronous selection watcher: under load that
       watcher ran after the release was routed and left the flag armed for the
       next gesture (review of #135, findings 6 and 13).
    3. *The highlight stands only while it means what was selected.* Any key or
       paste forwarded to the agent drops it, so ctrl+c after typing is the
       agent's interrupt and never a copy of whatever now sits under an old
       highlight; a frame that changes the text under it drops it — the text as
       DISPLAYED, notice and corner marker included, whether a frame or a
       failed capture put it there; so do hiding the pane, unmounting it and
       attaching another pane (finding 2; second round, findings 4, 6 and 12).
       A gesture that leaves a highlight with no text under it drops it at the
       release, so a highlight that stands always has something for ctrl+c to
       copy (review of #120, round 11).
    4. *The pane is never selected whole.* ``Selection(None, None)`` is what
       Textual writes for a multi-click on a neighbour (the container's
       select-all) and for a drag that starts and ends beyond both of the pane's
       edges; the pane refuses it in every reader and clears its entry, so a
       later ctrl+c never copies 3000 characters instead of interrupting
       (finding 6). A triple click in the pane itself selects nothing.
    5. *One key path copies pane text.* With the pane focused, ctrl+c and cmd+c
       copy a standing highlight and clear it; without one ctrl+c is the
       interrupt and cmd+c types nothing. Everywhere else the same keys reach
       :class:`PaneScreen`, which asks the panes first through the same method
       — the most recently made highlight first, should more than one pane
       hold one — and an empty copy never reaches the terminal (finding 10;
       second round, finding 3).
    6. *A click is a press and a release in one cell, with the left button.*
       Textual chains clicks by release position alone, so a drag followed by a
       click on its end cell arrived as a double click; the pane keeps its own
       chain over real LEFT clicks and selects a word on the second of two. A
       click with any other button breaks the chain and counts for nothing, so
       a right click followed by a left click is one click, not two (finding
       13; review of #120, round 8; second round, finding 2).
    7. *A modal pushed mid-drag takes the release.* The pane's baseline is then
       stale, and stale is harmless: it is rewritten at the next press on the
       pane's own screen, the only place it is read (cut finding of #135).

    What the rules cost: a drag that starts on the header and ends below the
    pane selects nothing in it (rule 4), and a highlight over a row the agent
    keeps rewriting is dropped as soon as the text under it changes (rule 3) —
    the drag that made it has already copied.
    """

    DEFAULT_CSS = """
    TerminalPane { height: 1fr; width: 1fr; }
    """

    #: Drag-select over the rendered rows (§4.3). Textual's default
    #: ``get_selection`` reads ``render()`` output, which a Line API widget does
    #: not have, so this widget supplies its own from the rows it is showing —
    #: the same rows it paints the span on, so the two cannot disagree. The
    #: rules are in the class docstring.
    ALLOW_SELECT: ClassVar[bool] = True
    FAST_INTERVAL: float = 0.05
    """Seconds between frames while the screen is changing (~20 fps)."""
    IDLE_INTERVAL: float = 0.5
    """Seconds between frames once nothing changed (~2 fps)."""
    RESIZE_DEBOUNCE: float = 0.1
    """Seconds to wait after the last ``Resize`` before telling tmux."""
    RESIZE_RETRY: float = 0.25
    """Seconds before a FAILED ``resize-window`` is tried again (then doubling)."""
    RESIZE_RETRY_MAX: float = 8.0
    """Where the resize backoff stops, so an unreachable pane costs ~1 process / 8 s."""
    WHEEL_LINES: int = 3
    """History lines one wheel notch moves."""
    WHEEL_COALESCE: float = 0.02
    """Seconds mouse events are gathered before one tmux call carries them all."""
    BUFFER_MIRROR_DELAY: float = 0.15
    """Seconds after a forwarded left-button release before tmux's paste buffer is
    read: the program's copy-on-select has to run first (Claude Code writes it
    from its release handler; measured well under 100 ms on this machine)."""
    SCROLL_KEYS: ClassVar[dict[str, str]] = {
        "shift+pageup": "page_up",
        "shift+pagedown": "page_down",
        "alt+pageup": "page_up",
        "alt+pagedown": "page_down",
        "shift+home": "top",
        "shift+end": "live",
    }
    """Keys that move the view instead of reaching the agent. Claude Code binds
    none of them. Two spellings per page on purpose: shift+PgUp is the key most
    terminals use for THEIR scrollback and some never forward it (VTE; Windows
    Terminal depending on its bindings), and alt+PgUp is rarely claimed by
    anyone. The wheel is not a given either — reported 2026-09-08 from WSL2 as
    "scroll not working", with no other way into the history."""
    CACHE_LIMIT: int = 4096
    """Distinct row strings kept as Strips before the cache is emptied."""

    def __init__(
        self,
        pane_id: str | None = None,
        *,
        server: TmuxServer | None = None,
        escape_key: str = "f12",
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.pane_id = pane_id
        self.server = server
        self.escape_key = escape_key
        self._version: tuple[int, int] | None = None
        """The server's version, read once per attach (``_server_version``)."""
        self._version_read = False
        self.scrollback = 0
        """How many history lines above the live screen the view starts at (``k``)."""
        self.facts: PaneFacts | None = None
        """What the last frame's ``display-message`` said; ``None`` before the first."""
        self.notice: str | None = None
        """The bottom-row notice — ``(pane gone)``, ``(exited N)`` — or ``None``."""
        self.interval: float = self.FAST_INTERVAL
        """The delay chosen for the next frame — what the render loop decided last."""
        self.frames = 0
        """Frames captured (instrumentation)."""
        self.rows_repainted = 0
        """Rows this widget asked Textual to repaint (instrumentation)."""
        self.lines_rendered = 0
        """Rows Textual actually asked :meth:`render_line` for (instrumentation)."""
        self._lines: list[str] = []
        self._wrapped: list[bool] = []
        """Per row of ``_lines``, whether tmux soft-wrapped it into the next (``-F``)."""
        self._cursor: tuple[int, int] | None = None
        self._strip_cache: dict[str, Strip] = {}
        self._row_cache: dict[tuple[str, int], DisplayedRow] = {}
        """The text model of a frame line shown in a row of N cells, beside its Strip."""
        self._timer: Timer | None = None
        self._resize_timer: Timer | None = None
        self._resize_retry: float = self.RESIZE_RETRY
        self._synced: tuple[str, int, int] | None = None
        self._warned: set[tuple[str | None, str]] = set()
        """``(server socket or None, key)`` per notice said — see :meth:`_warn_once`."""
        self._reported_gone = False
        self._mouse_queue: list[tuple[str, int, int, int]] = []
        """Mouse events (kind, button code, pane column, pane row) awaiting one
        forwarding call; ``kind`` is ``wheel``, ``press``, ``drag`` or ``release``."""
        self._mouse_timer: Timer | None = None
        self._forwarding: int | None = None
        """The Textual button whose press went to the program — until its release."""
        self._shift_drag: Offset | None = None
        """Where a shift+drag began, while this widget runs that selection itself."""
        self._shift_button = 0
        """The button that began that shift+drag: its end copies for the left one only."""
        self._buffer_before: str | None = None
        """tmux's paste buffer as it stood at a forwarded left press (#148), until
        that press's release takes it to the mirror."""
        self._mirror: tuple[object, TmuxServer | None, str | None] | None = None
        """The paste-buffer mirror a left release armed and that has not run yet:
        its token, the server the press went to, and the buffer it compares with
        (:meth:`_arm_mirror`)."""
        self._last_drag: tuple[int, int, int] | None = None
        """The (code, column, row) of the last drag report sent, so a pointer that
        has not left its cell is not reported again — a terminal reports motion
        per cell, not per pixel."""
        self._marker: tuple[int, int] | None = None
        """``(scrollback, history)`` the corner marker last showed, or ``None``."""
        self._selection_bg: Style | None = None
        """The selection tint, resolved once per selection rather than per row."""
        self._baseline: Selection | None = None
        """The selection this pane had when the gesture now running began, or one
        it has already copied itself: what a release must differ from to copy."""
        self._press: Offset | None = None
        """Where in this pane the last press landed, so a release elsewhere is a drag."""
        self._clicks = 0
        """This pane's own click chain: presses and releases in one cell, in quick succession."""
        self._last_click: tuple[Offset, float] | None = None
        """Where and when the last real click on this pane was released."""
        self._painted_span: Selection | None = None
        """The selection the rows on screen were last painted for."""
        self._painting = False
        """Inside :meth:`render_lines`: rows are being painted, not read for offsets."""
        self._selected_at = 0
        """When this pane's selection last changed, on :data:`_SELECTION_CLOCK` — stamped
        inside the write by :meth:`PaneScreen.watch_selections`."""
        # A tmux pane's links are the agent's, not Textual's: no hover highlight,
        # and no repaint of the whole pane when the pointer crosses one.
        self.auto_links = False

    # --- what is shown -----------------------------------------------------------------

    @property
    def attached(self) -> bool:
        return self.pane_id is not None

    @property
    def history_size(self) -> int:
        """tmux's history behind the live screen — 0 until the first frame answers."""
        return self.facts.history_size if self.facts is not None else 0

    def _server_version(self) -> tuple[int, int] | None:
        """The server's version, asked once per SERVER; ``None`` when it will not say.

        Cached by SOCKET across attaches: ``_wrap_flags`` needs the answer for
        the FIRST frame, and asking per attach put a blocking ``tmux -V``
        subprocess on the UI thread at every project switch, tab activation and
        re-mounted view (round 8 of #203). The server behind a socket is what
        the answer is about, and it does not change on an attach. Only an answer
        is cached — a server that will not say is asked again next time.
        """
        if not self._version_read:
            self._version = None
            if self.server is not None:
                key = self.server.socket
                if key in _SERVER_VERSIONS:
                    self._version = _SERVER_VERSIONS[key]
                else:
                    with contextlib.suppress(TmuxError):
                        self._version = self.server.version()
                    if self._version is not None:
                        _SERVER_VERSIONS[key] = self._version
            self._version_read = True
        return self._version

    def _extended_keys(self) -> bool:
        """Whether this server delivers extended chords (tmux ≥ 3.5).

        Below :data:`~aisquare.core.keys.EXTENDED_MINIMUM` tmux TYPES those
        chords' names into the agent (measured on 3.3a/3.4), so ``translate``
        refuses them there, sending shift+enter as ``C-j`` instead
        (:data:`~aisquare.core.keys.LEGACY_FALLBACK`). Fail-open to True when
        the version cannot be read: ``tmux -V`` answers on anything alive, and
        refusing the extended chords on every modern server to guard a
        hypothetical mute one inverts the trade.
        """
        version = self._server_version()
        return version is None or version >= EXTENDED_MINIMUM

    def _wrap_flags(self) -> bool:
        """Whether a frame may ask for ``capture-pane -F`` (tmux ≥ 3.7).

        The OTHER way round from :meth:`_extended_keys`: fail-closed. A server
        that does not know the flag fails the whole capture, and every frame
        would then read ``(pane gone)``; what refusing costs on a server that
        would have answered is the wrapped-line join in a copy, nothing else.
        """
        version = self._server_version()
        return version is not None and version >= WRAP_FLAGS_MINIMUM

    def notify_style_update(self) -> None:
        """Textual's "your resolved styles changed" hook — drop what bakes in a theme.

        ``_selection_bg`` memoises ``selection_style.bgcolor`` and was reset only
        when the selection cleared, so picking a theme mid-drag repainted every
        row around a highlight still tinted from the old palette — possibly
        invisible against it (review of the third version). It is the only field
        here with a theme baked in: ``_strip_cache`` stores rows BEFORE the base
        style is applied, and ``render_line`` applies the live one every time.
        """
        super().notify_style_update()
        self._selection_bg = None

    def attach(self, pane_id: str | None) -> None:
        """Show ``pane_id`` (``None`` clears the pane) and restart the render loop."""
        self.pane_id = pane_id
        self.scrollback = 0
        self.facts = None
        self.notice = None
        self._lines = []
        self._wrapped = []
        self._cursor = None
        self._synced = None
        self._resize_retry = self.RESIZE_RETRY
        self._reported_gone = False
        self._marker = None
        self._baseline = None
        self._press = None
        self._clicks = 0
        self._last_click = None
        self._painted_span = None
        if self.is_mounted and self.text_selection is not None:
            self._clear_own_selection()  # agent A's highlight must not sit on agent B
        self._mouse_queue = []
        if self._mouse_timer is not None:
            self._mouse_timer.stop()
            self._mouse_timer = None
        self._forwarding = None
        self._shift_drag = None
        if self.is_mounted:
            # Both gestures held the pointer, and forgetting them kept it: the
            # release that followed found nothing to end, and every click in
            # the app went on landing here (review of #203, round 1 of the
            # terminal-ux fold). The release the old program is owed is not
            # sent — the queue that would carry it is dropped above, and an
            # attach is most often that program's pane gone or restarted.
            self.release_mouse()
        self._buffer_before = None
        self._last_drag = None
        # A new attach may be a new server — ``ManagerTab`` assigns ``server``
        # then calls this — and a cached "extended chords are fine" from a 3.7
        # server would TYPE ``S-Enter`` into an agent on a 3.4 one, as a cached
        # "-F is known" would fail every frame. Re-read lazily: one ``tmux -V``
        # per attach at most. The notices are NOT cleared with it: an attach is
        # as often a restarted agent or a sign-in on the SAME server, and the
        # one line that names a server is deduped per server by ``_warn_once``
        # (reviews of #161, rounds 6-7).
        self._version_read = False
        if pane_id is not None and self.server is None:
            # The fleet's server from config — a default like any other (§3.10).
            self.server = fleet_service.server()
        if self.is_mounted:
            self._sync_size()
            self.refresh_frame()
            self._schedule(self.FAST_INTERVAL)
        self.refresh()

    def on_mount(self) -> None:
        _MOUNTED_PANES.add(self)
        self.refresh_frame()
        self._schedule(self.FAST_INTERVAL)

    def on_unmount(self) -> None:
        """The pane goes: so does its entry in ``screen.selections``.

        ``on_hide`` clears it because a hidden pane's highlight can go stale;
        an unmounted pane's entry was left behind, a strong reference from the
        screen's dict to a dead widget — and its strip cache, up to
        ``CACHE_LIMIT`` rows — that nothing short of ``clear_selection`` could
        drop (review of #135, second round, finding 6). Textual dispatches
        ``Unmount`` while the widget still hangs in the DOM, so the screen is
        reachable here.
        """
        _MOUNTED_PANES.discard(self)
        if self.text_selection is not None:
            self._clear_own_selection()
        if self._timer is not None:
            self._timer.stop()
        if self._mouse_timer is not None:
            self._mouse_timer.stop()
        if self._resize_timer is not None:
            self._resize_timer.stop()

    def on_show(self) -> None:
        """A hidden tab came back: pick the pane up at the fast cadence."""
        self.refresh_frame()
        self._schedule(self.FAST_INTERVAL)

    def on_hide(self) -> None:
        """A tab went behind another: its highlight goes with it.

        A hidden pane is not captured, so whatever it highlights can change
        unseen — and its entry stayed in ``screen.selections``, where the copy
        key outside a pane found it, extracted nothing from a 0x0 widget, and
        wiped the clipboard with an empty OSC 52 (review of #135, finding 10).
        """
        if self.text_selection is not None:
            self._clear_own_selection()

    def on_focus(self) -> None:
        self._refresh_cursor_row()

    def on_blur(self) -> None:
        self._refresh_cursor_row()

    # --- the render loop ---------------------------------------------------------------

    def _schedule(self, delay: float) -> None:
        if self._timer is not None:
            self._timer.stop()
        self.interval = delay
        self._timer = self.set_timer(delay, self._tick, name="terminal-frame")

    def _tick(self) -> None:
        self._timer = None
        if self.pane_id is None or self.server is None:
            return  # attach() restarts the loop
        changed = self.refresh_frame() if self.is_on_screen else False
        self._schedule(self.FAST_INTERVAL if changed else self.IDLE_INTERVAL)

    def refresh_frame(self) -> bool:
        """Capture one frame now. Returns whether anything on screen changed."""
        if self.pane_id is None or self.server is None:
            return False
        width, height = self.content_size
        if width <= 0 or height <= 0:
            return False
        try:
            capture = self.server.capture(
                self.pane_id,
                scrollback=self.scrollback,
                # The pane height the last frame reported bounds a scrolled
                # capture to one screen (§6); without it tmux pipes
                # ``scrollback + height`` rows every tick. ``None`` before the
                # first frame, when scrollback is 0 and there is nothing to bound.
                height=self.facts.height if self.facts is not None else None,
                flags=self._wrap_flags(),
            )
        except TmuxUnavailable:
            return self._fail(TMUX_UNAVAILABLE)
        except TmuxError:
            return self._fail(PANE_GONE)
        except Exception as error:  # the loop must outlive a surprise
            self.log.error("terminal frame failed", error)
            return self._fail(f"(capture failed: {type(error).__name__})")
        self.frames += 1
        facts = capture.facts
        if self.scrollback > facts.history_size:
            self.scrollback = facts.history_size
        # A pane taller than the widget shows its LAST rows — the prompt lives
        # at the bottom — until the debounced resize brings the two in line.
        offset = max(0, len(capture.lines) - height)
        lines = capture.lines[offset:]
        lines += [""] * (height - len(lines))
        wrapped = (capture.wrapped or [])[offset:]
        wrapped += [False] * (height - len(wrapped))
        cursor: tuple[int, int] | None = None
        if facts.cursor_visible and self.scrollback == 0 and not facts.dead:
            row = facts.cursor_y - offset
            # Only a row this widget HAS. A pane taller than the widget whose
            # cursor sits above the shown window gives a negative row: nothing
            # can render it, yet it entered ``dirty`` and every move of that
            # invisible cursor held the 50 ms cadence and handed ``refresh`` a
            # Region outside the widget (measured: ``Region(0, -16, 40, 1)``).
            if 0 <= row < height:
                cursor = (facts.cursor_x, row)
        notice: str | None = None
        if facts.dead:
            notice = "(exited)" if facts.dead_status is None else f"(exited {facts.dead_status})"

        previous = self._lines
        changed = {y for y, line in enumerate(lines) if y >= len(previous) or previous[y] != line}
        dirty = set(changed)
        if cursor != self._cursor:
            for point in (cursor, self._cursor):
                if point is not None:
                    dirty.add(point[1])
        # Rows whose DISPLAYED text is replaced wholesale rather than re-read
        # from the frame: the notice, and the corner marker on row 0.
        replaced: set[int] = set()
        if notice != self.notice:
            dirty.add(height - 1)
            replaced.add(height - 1)
        # The corner marker: row 0 repaints whenever k or the history it is
        # measured against moved — including the clamp above, and history that
        # keeps growing under a frozen scrolled view. Decided HERE, once, rather
        # than at every site that touches ``scrollback``.
        marker = (self.scrollback, facts.history_size) if self.scrollback else None
        if marker != self._marker:
            dirty.add(0)
            replaced.add(0)
        stale = self._highlight_is_stale(Shown(lines, notice, marker), changed | replaced)
        self._marker = marker
        self._lines = lines
        self._wrapped = wrapped
        self._cursor = cursor
        self.facts = facts
        self.notice = notice
        if stale:
            self._clear_own_selection()
        self._repaint_rows(dirty)
        return bool(dirty)

    def _shown(self) -> Shown:
        """What the pane displays now — the state every reader of a row starts from."""
        return Shown(self._lines, self.notice, self._marker)

    def _highlight_is_stale(self, after: Shown, rows: set[int]) -> bool:
        """Whether showing ``after`` in place of what is shown now changes the text
        under the standing highlight, on ``rows`` (the rows about to repaint).

        The highlight means "this text": once the agent has printed something
        else there, a ctrl+c meant as the interrupt would copy text nobody
        selected and send no ``C-c`` (review of #135, finding 2). Compared cell
        for cell under the span, not row for row — Claude Code's status line
        redraws several times a second, and a highlight elsewhere on that row
        would otherwise never survive it. Compared as DISPLAYED (:class:`Shown`),
        through the same :meth:`_displayed_row` the copy reads: a frame that
        changes the text hidden under the corner marker changes nothing the user
        sees and leaves the highlight; a notice arriving in the bottom row
        changes what a highlight there means and drops it (second round,
        findings 4 and 12).
        """
        selection = self._own_selection()
        if selection is None:
            return False
        before = self._shown()
        for y in rows:
            span = selection.get_span(y)
            if span is None:
                continue
            start, end = span
            old = self._displayed_row(y, shown=before)
            new = self._displayed_row(y, shown=after)
            if old.slice(start, old.cells if end == -1 else end) != new.slice(
                start, new.cells if end == -1 else end
            ):
                return True
        return False

    def _fail(self, notice: str) -> bool:
        """Keep the last frame, show ``notice`` in the bottom row; True when that is new.

        The notice replaces the bottom row's text, and a highlight over that
        row now covers ``(pane gone)`` rather than what was selected — checked
        like any frame (class docstring, rule 3; review of #135, second round,
        finding 4). It used to be the one path that rewrote a displayed row
        without asking, so the next ctrl+c copied the notice and swallowed the
        interrupt.
        """
        changed = notice != self.notice or self._cursor is not None
        height = self.content_size.height
        # A failure can arrive before Textual has sized the widget — the first
        # capture of ``attach`` or ``on_mount`` raising — and ``height - 1`` is
        # then ``-1``, which ``_displayed_row`` reads as the frame's LAST row
        # (``lines[-1]``) and compares under a span meant for the notice row:
        # a verdict about a row nobody highlighted (review of #203). No rows,
        # no row the notice replaces.
        stale = (
            height > 0
            and notice != self.notice
            and self._highlight_is_stale(Shown(self._lines, notice, self._marker), {height - 1})
        )
        self.notice = notice
        self._cursor = None
        if stale:
            self._clear_own_selection()
        if changed:
            self.refresh()
        if not self._reported_gone and self.pane_id is not None:
            self._reported_gone = True
            self.notify(f"{self.pane_id}: {notice}", severity="warning", markup=False)
        return changed

    def _repaint_rows(self, rows: set[int]) -> None:
        """Mark exactly ``rows`` dirty; Textual asks :meth:`render_line` for those alone.

        Regions handed to ``refresh`` are content-relative — ``Widget._set_dirty``
        adds the gutter itself — so row ``y`` is ``Region(0, y, width, 1)``.
        """
        if not rows:
            return
        width = self.content_size.width
        self.rows_repainted += len(rows)
        self.refresh(*(Region(0, y, width, 1) for y in sorted(rows)))

    def _refresh_cursor_row(self) -> None:
        if self._cursor is not None:
            self._repaint_rows({self._cursor[1]})

    # --- the Line API ------------------------------------------------------------------

    def render_lines(self, crop: Region) -> list[Strip]:
        """Textual's paint path: the rows it asks :meth:`render_line` for are unstamped.

        The compositor takes a widget's picture through here (and the style
        under the pointer, for hover), and resolves a press or a drag's content
        offset by calling :meth:`render_line` directly — the one and only direct
        caller on Textual 8.2.8. Stamping the painted rows too gave every
        segment a unique link id, and the module docstring has what that cost
        (review of #135, finding 3).

        Which method the compositor calls for which purpose is Textual's, not
        ours, and there is no offset source that does not go through it: the
        Screen builds a drag's ``Selection`` from what ``get_widget_and_offset_at``
        reads off the stamped metadata, and hands back a whole-widget
        selection when it finds none (review of #135, second round, finding
        10). So the assumption is PINNED rather than trusted:
        ``test_the_compositor_reads_offsets_through_render_line_and_paints_through_render_lines``
        drives both entry points on the installed Textual and fails the moment
        either moves — the offsets going missing, or the paint being stamped.
        """
        self._painting = True
        try:
            return super().render_lines(crop)
        finally:
            self._painting = False

    def render_line(self, y: int) -> Strip:
        strip = self._render_row(y)
        return strip if self._painting else self._stamped(strip, y)

    @staticmethod
    def _stamped(strip: Strip, y: int) -> Strip:
        """``strip`` with the compositor's ``offset`` metadata, in CELLS.

        The compositor walks a segment's characters with per-code-point widths to
        find the offset under the pointer, and adds ``len(segment.text)`` to reach
        the end. Both count characters. Handing it one segment per grapheme whose
        character count and cell count differ keeps that arithmetic inside a
        single glyph, where the answer is at most one past the glyph's first cell
        and :meth:`DisplayedRow.snap` lands it on the glyph; every other run of
        text is one segment, characters and cells being the same thing there.
        Built fresh, without ``Strip.apply_offsets``: that caches its stamped
        copy on the strip, keyed by row, and rows scrolling through every row of
        a pane held a copy per row in the strip cache (review of #135, finding 3).
        """
        segments: list[Segment] = []
        x = 0
        for text, style, control in strip:
            if control:
                segments.append(Segment(text, style, control))
                continue
            for run, cells in TerminalPane._runs(text):
                meta = Style.from_meta({"offset": (x, y)})
                segments.append(Segment(run, style + meta if style is not None else meta))
                x += cells
        return Strip(segments, strip.cell_length)

    @staticmethod
    def _runs(text: str) -> list[tuple[str, int]]:
        """``text`` as ``(run, cells)`` pieces: plain runs, and every complex grapheme alone."""
        if text.isascii() and text.isprintable():
            return [(text, len(text))]
        runs: list[tuple[str, int]] = []
        plain_start: int | None = None
        for start, end, width in split_graphemes(text)[0]:
            if end - start == 1 and width == 1:
                if plain_start is None:
                    plain_start = start
                continue
            if plain_start is not None:
                runs.append((text[plain_start:start], start - plain_start))
                plain_start = None
            runs.append((text[start:end], width))
        if plain_start is not None:
            runs.append((text[plain_start:], len(text) - plain_start))
        return runs

    def _render_row(self, y: int) -> Strip:
        width, height = self.content_size
        if width <= 0:
            return Strip.blank(0)
        base = self.rich_style
        self.lines_rendered += 1
        if self.pane_id is None:
            if y == 0:
                return Strip([Segment(NO_PANE, base + PLACEHOLDER)]).adjust_cell_length(width, base)
            return Strip.blank(width, base)
        strip = self._composed_strip(y)
        cursor = self._cursor
        notice = self.notice is not None and y == height - 1
        cursor_x = (
            cursor[0]
            if not notice and cursor is not None and cursor[1] == y and cursor[0] < width
            else None
        )
        selection = self._own_selection()
        span = None if selection is None else selection.get_span(y)
        if cursor_x is None and span is None:
            return strip
        # The row the overlays are measured against is the row AS COMPOSED —
        # the notice and the corner marker included — so the paint, the copy
        # and the word under a double click cannot come apart (review of #120,
        # rounds 3 to 5). For a plain frame row that is the cached model of its
        # line; only the two composed rows are measured from the strip.
        row = self._overlay_row(y, strip)
        if span is not None:
            strip = self._with_selection(strip, span, row, width)
        if cursor_x is not None:
            # LAST: reverse video over the tint keeps the cursor visible inside
            # a highlight; painted first it disappeared under it.
            strip = self._with_cursor(strip, cursor_x, row)
        return strip

    def _composed_strip(self, y: int, shown: Shown | None = None) -> Strip:
        """Row ``y`` as it is shown, before any overlay: the frame's row, or the
        notice in its place, with the corner marker composed into row 0 while
        the view is scrolled. The paint and the copy both start from here —
        from what is shown now, or from ``shown`` (a frame about to be)."""
        if shown is None:
            shown = self._shown()
        width, height = self.content_size
        base = self.rich_style
        line = shown.lines[y] if y < len(shown.lines) else ""
        if shown.notice is not None and y == height - 1:
            # Built, not returned: the overlays still apply. Returning early
            # left the one row a drag COPIES as the only row a selection never
            # tinted (review of #120, round 3).
            strip = Strip([Segment(shown.notice, base + NOTICE)]).adjust_cell_length(width, base)
        else:
            strip = self._strip_for(line).apply_style(base).adjust_cell_length(width, base)
        if y == 0 and shown.marker is not None:
            # The row's text model is read by the marker alone, so it is built
            # here and nowhere else: built for the notice row on every render,
            # it paid an uncached ``Strip.text`` join and a grapheme scan for a
            # value the notice row — which is never row 0 on a widget taller
            # than one row — then threw away (review of #203, round 4). The
            # notice row's model comes from its strip, a frame row's from the
            # cache, as the overlays read them (``_overlay_row``).
            row = (
                DisplayedRow(strip.text.rstrip())
                if shown.notice is not None and y == height - 1
                else self._frame_row(line, width)
            )
            strip = self._with_scroll_marker(strip, width, row, shown.marker)
        return strip

    def _composed(self, y: int, shown: Shown) -> bool:
        """Whether row ``y`` displays something other than its frame line."""
        height = self.content_size.height
        notice_row = shown.notice is not None and y == height - 1
        return notice_row or (y == 0 and shown.marker is not None)

    def _overlay_row(self, y: int, strip: Strip) -> DisplayedRow:
        """The text model the overlays on row ``y`` are measured against.

        The cached model of the frame line for a plain row — the cursor row is
        re-rendered on every frame the cursor moves on, and it paid an uncached
        ``Strip.text`` join and a grapheme scan each time, twice for row 0
        while scrolled (review of #135, second round, finding 11) — and the
        composed ``strip``'s own text for the two rows that display something
        else, built once from the strip already in hand.
        """
        shown = self._shown()
        if self._composed(y, shown):
            return DisplayedRow(strip.text.rstrip())
        line = shown.lines[y] if y < len(shown.lines) else ""
        return self._frame_row(line, self.content_size.width)

    def _frame_row(self, line: str, width: int) -> DisplayedRow:
        """A frame line as a row of ``width`` cells — cropped to it, trailing
        padding dropped — cached by the line beside its Strip.

        ONE model for the paint's overlays, the copy and the staleness check;
        ``_line_row`` re-implemented it against the raw line and the two had to
        be kept in step by hand (review of #135, second round, finding 12).
        Trailing blanks are the pane's padding, not text: past the last glyph
        every cell is one blank cell, so snapping and slicing read the same
        with or without them, and the copy must not take them.
        """
        key = (line, width)
        row = self._row_cache.get(key)
        if row is None:
            if len(self._row_cache) >= self.CACHE_LIMIT:
                self._row_cache.clear()
            text = self._strip_for(line).text
            if cell_len(text) > width:
                text = set_cell_size(text, width)
            row = DisplayedRow(text.rstrip())
            self._row_cache[key] = row
        return row

    def _displayed_row(
        self, y: int, *, shown: Shown | None = None, wrapped: bool = False
    ) -> DisplayedRow:
        """Row ``y`` as the widget DISPLAYS it — the text a drag over it copies.

        Trailing blanks are the pane's padding, not text, and are dropped — except
        on a row tmux wrapped, where every cell up to the pane's width was
        written by the program and a space at the wrap point is real (review of
        #135, finding 9). ``shown`` reads the row from a state other than the
        current one (the frame about to replace it, in ``_highlight_is_stale``).
        """
        if shown is None:
            shown = self._shown()
        if wrapped:
            # The composed strip is padded to the WIDGET's width; a wrapped row's
            # real extent is the PANE's (where tmux wrapped it). While the two
            # disagree — the 100 ms resize debounce, a refused resize-window — the
            # narrower one is the row, stated as such: a narrower pane's row is
            # cropped to the pane (the strip's padding is not text), and a wider
            # pane can never show more than the widget paints (reviews of #203,
            # round 4, and of the fold — both measured; neither reproduced a copy
            # past either width, and the rule is now written where it is read).
            text = self._composed_strip(y, shown).text
            facts = self.facts
            pane_width = facts.width if facts is not None else self.content_size.width
            limit = min(pane_width, self.content_size.width)
            if cell_len(text) > limit:
                text = set_cell_size(text, limit)
            return DisplayedRow(text, wrapped=True)
        if self._composed(y, shown):
            return DisplayedRow(self._composed_strip(y, shown).text.rstrip())
        line = shown.lines[y] if y < len(shown.lines) else ""
        return self._frame_row(line, self.content_size.width)

    def _displayed_rows(self) -> list[DisplayedRow]:
        # Exactly the rows the widget RENDERS: the height alone. Taking the longer
        # of the height and ``_lines`` copied rows that are not on screen — a pane
        # whose captures are failing keeps the taller frame (review of #120,
        # round 8). The notice row and a marker row display something other than
        # the frame's row, so neither continues onto the next. The wrap flags
        # are the frame's own (``capture-pane -F``, carried with every frame):
        # they describe exactly the rows on screen, so a copy runs no process of
        # its own and never has to compare two screens (review of #135, finding
        # 9; second round, finding 9).
        shown = self._shown()
        rows: list[DisplayedRow] = []
        for y in range(self.content_size.height):
            joined = y < len(self._wrapped) and self._wrapped[y] and not self._composed(y, shown)
            rows.append(self._displayed_row(y, shown=shown, wrapped=joined))
        return rows

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        """The plain text under ``selection``, from the rows this widget shows.

        Own extraction rather than ``Selection.extract``: that re-splits with
        ``str.splitlines()``, which drops a trailing empty row and breaks on form
        feeds — so a drag in the blank area under an agent's output indexed past
        the end and took the app down (reproduced in review), and one ``\x0c``
        from a pager shifted every row after it.
        """
        if self.pane_id is None or selection == SELECT_ALL:
            return None  # a whole-pane selection is never this pane's (class docstring, rule 4)
        return _extract(selection, self._displayed_rows(), self.content_size.width), "\n"

    def selected_text(self) -> str | None:
        """What a drag has selected in this pane, or ``None`` when nothing is.

        A span over rows the agent has never printed on is not a copy request:
        those rows extract as nothing at all now that ``_extract`` drops the
        trailing newlines, so plain emptiness is the whole test. It was
        ``.strip()`` for one round, which also refused whitespace the user
        really had selected — a column gap in ``ls -l``, an indent, a diff
        gutter — leaving the highlight painted, no toast, and the follow-up
        ctrl+c killing the agent instead of copying (reviews of the ninth and
        tenth versions).
        """
        selection = self._own_selection()
        if selection is None:
            return None
        extracted = self.get_selection(selection)
        text = extracted[0] if extracted else ""
        return text or None

    @property
    def selected_at(self) -> int:
        """When this pane's selection last changed, on the module's selection clock
        — what orders the panes for the copy key (:func:`copy_pane_selection`)
        and for a release (:func:`_tell_panes`)."""
        return self._selected_at

    def stamp_selection(self) -> None:
        """This pane's highlight was just written: it is now the newest.

        Called by :meth:`PaneScreen.watch_selections` inside the write itself,
        never from this widget's own watcher, which runs later (see there).
        """
        self._selected_at = next(_SELECTION_CLOCK)

    def has_standing_selection(self) -> bool:
        """Whether a highlight the copy key could take stands in this pane."""
        return self._own_selection() is not None

    def _own_selection(self) -> Selection | None:
        """This pane's standing selection — never a whole-pane one (class docstring, rule 4).

        Read here by every path that acts on the selection, because the watcher
        that clears a refused whole-pane entry runs asynchronously and a copy
        key or a release can arrive first.
        """
        selection = self.text_selection
        return None if selection == SELECT_ALL else selection

    def selection_updated(self, selection: Selection | None) -> None:
        """Repaint for a selection change; nothing about the rows is remembered.

        There was a snapshot here — the rows frozen when a drag began, so that a
        copy could not pick up output printed during the gesture. It is gone,
        and this reverses a first-round decision deliberately. It could not hold
        the property it was for: the STRIP is always built from the live
        ``_lines``, so a frozen text made the copy disagree with the paint
        instead of agreeing with it. Reading the live rows for BOTH means they
        always agree, which is the property that was actually wanted; and a
        highlight whose text changes under it is dropped (class docstring, rule
        3), so a copy never reads a screen that is no longer under it.
        """
        if selection == SELECT_ALL:
            # A neighbour's triple click, or a drag past both of this pane's
            # edges: refused, and the entry cleared so nothing else reads it
            # (class docstring, rule 4). The clear re-enters this watcher with
            # ``None`` and repaints then.
            self._clear_own_selection()
            return
        if selection is None:
            self._selection_bg = None
        # Nothing about participation is recorded here: Textual tells the union
        # of the old and new selection owners, this watcher runs asynchronously
        # — after the release was routed, under load — and every flag set from
        # it was found armed for the wrong gesture (reviews of #120, rounds 7
        # and 10; review of #135, findings 6 and 13). A release compares values
        # instead (``selection_gesture_ended``). WHEN the highlight changed, for
        # the order of rule 5, went the same way: it is stamped inside the
        # write, by ``PaneScreen.watch_selections`` (review of the #167 fold, F2).
        self._repaint_selection(selection)

    def _repaint_selection(self, selection: Selection | None) -> None:
        """Repaint the rows whose SPAN this selection change altered.

        A bare ``refresh()`` here redrew every row of the widget on every
        MouseMove of a drag — and each row with a span pays for a text join and
        a grapheme scan (review of #120, round 4); repainting every row of the
        old and new selection still redrew a forty-row highlight for a pointer
        that moved one row (cleanup of #135). Only rows the widget has: a stale
        selection names rows a shrunken pane no longer renders, and handing
        those to ``refresh`` is the Region-outside-the-widget shape
        ``refresh_frame`` documents as measured harm (review of #120, round 8).
        """
        painted, self._painted_span = self._painted_span, selection
        rows = {
            y
            for y in range(self.content_size.height)
            if (None if painted is None else painted.get_span(y))
            != (None if selection is None else selection.get_span(y))
        }
        if rows:
            self._repaint_rows(rows)

    def _restyled(self, strip: Strip, start: int, end: int, style: Style) -> Strip:
        """``strip`` with cells ``[start, end)`` restyled — ``style`` layered LAST.

        ``Strip.apply_style`` puts the segment's own style on top, and every
        segment already carries the widget background from ``apply_style(base)``
        — so a tint applied that way lost to it. Rich's ``post_style`` is the
        layering that wins, and control segments keep their ``None`` style.
        Callers snap ``start`` and ``end`` to grapheme boundaries first: a crop
        through a wide glyph renders it as two single-cell spaces, one character
        more than the row had (measured: a cursor on ``日`` at cell 0 moved a
        drag's whole selection by one).
        """
        if start >= end:
            return strip
        span = strip.crop(start, end)
        painted = Strip(list(Segment.apply_style(span, post_style=style)), span.cell_length)
        return Strip.join([strip.crop(0, start), painted, strip.crop(end)])

    def _with_selection(
        self, strip: Strip, span: tuple[int, int], row: DisplayedRow, width: int
    ) -> Strip:
        """Paint ``span`` — CELL offsets from the compositor, ``-1`` to the end — as cells.

        The row is skipped by the same test :func:`_extract` skips it by, so the
        two count the same rows. ``start`` is not clamped (review of #120, round
        11): at or past ``width`` — a selection left from a wider pane — the
        test already skips the row; below 0 — which Textual never writes, its
        compositor clamps offsets at 0 — ``Strip.crop`` starts at cell 0, as
        :meth:`DisplayedRow.slice` copies from it. A clamp here changed nothing.
        """
        start, end = span
        cell_end = width if end == -1 else min(end, width)
        if start >= cell_end:
            return strip  # no cell of this row: nothing to widen, nothing to tint
        cell_start, cell_end = row.snap(start, cell_end)
        tint = self._selection_tint()
        painted = strip.crop(cell_start, cell_end)
        segments = [self._tinted(segment, tint) for segment in painted]
        return Strip.join(
            [strip.crop(0, cell_start), Strip(segments, painted.cell_length), strip.crop(cell_end)]
        )

    def _selection_tint(self) -> Style:
        if self._selection_bg is None:
            # The BACKGROUND only. The theme's ``screen--selection`` component
            # style resolves with foreground equal to background (``#094472 on
            # #094472`` — measured), so applying it whole painted the text
            # invisible: a solid block where the word was.
            bg = self.selection_style.bgcolor
            self._selection_bg = Style(bgcolor=bg) if bg is not None else Style(reverse=True)
        return self._selection_bg

    def _tinted(self, segment: Segment, tint: Style) -> Segment:
        """``segment`` with the selection tint behind its text.

        A reverse-video cell draws its glyph in its background colour on its
        foreground colour, so a background tint layered on it changed the glyph's
        colour and left the block behind it as it was — invisible on a blank
        reversed cell, which is what Claude Code's chosen menu row and a shell's
        status bar are made of (cut finding of #135). Such a cell is un-reversed
        and its colours swapped by hand, so the tint sits behind the glyph as
        it does everywhere else.

        When the tint has no background of its own — :meth:`_selection_tint`'s
        fallback for a theme whose selection style names none is plain reverse
        video — a reversed cell is simply un-reversed: inverting an inverted
        cell. Swapping its colours "by hand" onto a ``None`` background kept the
        cell's own, and drew the glyph in it — blue on blue, the invisibility
        this method exists to prevent (review of #135, second round, finding 1).
        """
        text, style, control = segment
        if control:
            return segment
        if style is not None and style.reverse:
            if tint.bgcolor is None:
                return Segment(text, style + Style(reverse=False))
            shown = style.bgcolor if style.bgcolor is not None else self.rich_style.bgcolor
            over = Style(reverse=False, color=shown, bgcolor=tint.bgcolor)
            return Segment(text, style + over)
        return Segment(text, style + tint if style is not None else tint)

    def _with_scroll_marker(
        self, strip: Strip, width: int, row: DisplayedRow, numbers: tuple[int, int]
    ) -> Strip:
        """``[↑k/history]`` in the top-right corner while the view is in history.

        tmux's own copy-mode indicator, in the same place: without it a scrolled
        pane is indistinguishable from a live one that happens to be quiet. The
        cut is snapped to a grapheme boundary of ``row`` — ``strip``'s text
        model, cached — and a widened gap is blank.
        """
        layout = self._marker_layout(row, width, numbers)
        if layout is None:
            return strip
        cut, gap, marker = layout
        return Strip.join(
            [
                strip.crop(0, cut),
                Strip(
                    [
                        Segment(" " * gap, self.rich_style),
                        Segment(marker, self.rich_style + SCROLL_MARKER),
                    ]
                ),
            ]
        )

    SCROLL_MARKER_TEMPLATE: ClassVar[str] = "[↑{scrollback}/{history}]"
    """The corner marker's text. A class attribute so the cell-vs-character rule
    below can be exercised with a marker whose two measures differ — ``↑`` is
    East-Asian Ambiguous and resolves to one cell, so the shipped marker cannot
    tell the two apart (review of the ninth version)."""

    def _marker_layout(
        self, row: DisplayedRow, width: int, shown: tuple[int, int]
    ) -> tuple[int, int, str] | None:
        """``(cut, gap, marker)`` in cells — one answer for the strip and the text.

        ``shown`` is the ``(scrollback, history)`` pair the marker names — the
        frame's own, so a row can be composed for the frame about to replace
        this one as well as for the one on screen.
        """
        scrollback, history = shown
        marker = self.SCROLL_MARKER_TEMPLATE.format(scrollback=scrollback, history=history)
        # CELLS, like `cut`, `gap` and every crop they feed. `↑` is East-Asian
        # Ambiguous and resolves to one cell today, so a character count agreed
        # by luck — and a row where they diverge puts the marker over its corner
        # and splits the paint from the copy (review of the ninth version).
        marker_cells = cell_len(marker)
        if marker_cells >= width:
            return None
        cut, _ = row.snap(width - marker_cells, width - marker_cells)
        return cut, width - marker_cells - cut, marker

    TAB_STOPS: ClassVar[int] = 8
    """Where a TAB cell advances to. tmux stores a tab as a cell and prints it as
    a literal TAB, with the row padded as if expanded (measured on 3.7c); the
    terminal would expand it, but this widget is the terminal here, and a
    0-cell TAB put every offset, highlight and copy after it eight cells to the
    left of what the eye saw (review of #135, finding 14). Default stops only:
    a program that moves them is rare, and costs one row's alignment."""

    def _strip_for(self, line: str) -> Strip:
        strip = self._strip_cache.get(line)
        if strip is None:
            if len(self._strip_cache) >= self.CACHE_LIMIT:
                self._strip_cache.clear()
                self._row_cache.clear()
            text = Text.from_ansi(line, end="")
            text.expand_tabs(self.TAB_STOPS)
            strip = Strip(text.render(self.app.console)).simplify()
            self._strip_cache[line] = strip
        return strip

    def _with_cursor(self, strip: Strip, x: int, row: DisplayedRow) -> Strip:
        style = CURSOR if self.has_focus else UNFOCUSED_CURSOR
        widened = strip.extend_cell_length(x + 1, self.rich_style)
        start, end = row.snap(x, x + 1)  # a wide glyph: both cells
        return self._restyled(widened, start, end, style)

    # --- input ---------------------------------------------------------------------------

    def _is_escape(self, event: events.Key) -> bool:
        hatch = self.escape_key.lower()
        return event.key.lower() == hatch or hatch in (alias.lower() for alias in event.aliases)

    @property
    def allow_select(self) -> bool:
        """Textual's own drag-select — except while a program owns the mouse (#148).

        Read by the screen at every mouse-down to decide whether the gesture may
        start a selection. A program that tracks the mouse gets the gesture
        instead (Claude Code selects, and copies, inside itself); shift+drag is
        the local selection then, and this widget runs it by hand
        (:meth:`on_mouse_down`).
        """
        return self.ALLOW_SELECT and not self._program_owns_mouse()

    def _program_owns_mouse(self) -> bool:
        """Whether the pointer belongs to the program in the pane right now.

        The wheel's own decision (:meth:`_scroll_owner`): a program that asked for
        mouse tracking, not in tmux copy mode, with the view live.
        """
        return self.attached and self.server is not None and self._scroll_owner() == "program"

    def on_key(self, event: events.Key) -> None:
        if self._is_escape(event):
            event.stop()
            event.prevent_default()
            self.post_message(EscapeToSidebar())
            return
        if self.pane_id is None or self.server is None:
            return  # nothing to type into; the app's own bindings stay live
        event.stop()
        event.prevent_default()
        action = self.SCROLL_KEYS.get(event.key)
        if action is not None:
            self._scroll_by_key(action)
            return
        if event.key == "ctrl+c":
            # Every terminal emulator does this: ctrl+c copies while text is
            # selected. Without a selection it falls through to the agent's
            # interrupt. Called as a statement, not as a boolean operand:
            # the copy writes the clipboard and raises a toast, which is not
            # what a predicate in an `and` chain reads as (review).
            copied = self.copy_standing_selection()
            if copied:
                return
        if event.key == "super+c":
            # macOS Cmd+C is copy and nothing else. It must not fall through to
            # the key table either: ``super`` is not a modifier tmux can spell,
            # the event carries a printable ``c``, and the pane typed a bare
            # ``c`` into the agent for a copy gesture (review).
            self.copy_standing_selection()
            return
        version = self._server_version()
        translation = translate(
            event.key,
            event.character,
            printable=event.is_printable,
            extended_keys=self._extended_keys(),
        )
        if isinstance(translation, Drop):
            # Nothing is typed — mistyping into a running agent is the worse
            # failure. What, if anything, to say comes from translate's REASON
            # and never from the key's shape: the shape misread a Cmd chord as
            # deliberate aim, numpad + as a key nobody pressed, and a tmux too
            # old for shift+enter as a chord with no spelling anywhere (review
            # of #161, round 2). The version the gate just read goes with it,
            # so the too-old notice can say what the reader HAS as well as
            # what they need (review of #161, round 5).
            self._explain(event.key, translation, version)
            return
        if self.text_selection is not None:
            # Typing means the highlight is stale: the next ctrl+c must be the
            # interrupt, not a copy of whatever now sits under it (class
            # docstring, rule 3; review of #135, finding 2).
            self._clear_own_selection()
        self._send(translation)
        if self.scrollback:
            self.scrollback = 0
            self.refresh_frame()
        # A key is activity: the echo must not wait for the idle tick.
        self._schedule(self.FAST_INTERVAL)

    def _send(self, translation: Translation) -> None:
        if self.pane_id is None or self.server is None:
            return
        try:
            if translation.kind == "key":
                self.server.send_keys(self.pane_id, translation.value)
            elif translation.value.endswith(ARGV_SEPARATOR):
                # tmux reads an argument ending in ';' as a command separator, so
                # ``send-keys -l -- ';'`` sends nothing: such text goes through the
                # paste buffer (stdin, never argv) until core.tmux escapes it.
                self.server.paste(self.pane_id, translation.value)
            else:
                self.server.send_literal(self.pane_id, translation.value)
        except TmuxUnavailable:
            self._fail(TMUX_UNAVAILABLE)
        except TmuxError:
            self._fail(PANE_GONE)

    def _explain(self, key: str, drop: Drop, version: tuple[int, int] | None) -> None:
        """Say what became of a key that was not sent — from ``translate``'s reason.

        Two of the four reasons are silence, and they are the #151 fix: a
        modifier tapped on its own, a whole key the terminal reports only
        because Textual asked for every key, a Cmd chord meant for the OS —
        none was a keystroke aimed at the agent, and a toast for any of them
        read as tmux failing. The other two are a keystroke LOST, and differ in
        whether the reader can do anything about it: a chord tmux has no safe
        spelling for is a fact about the key table, said once as information;
        a chord this tmux is too old to carry is a warning that names the
        version it needs AND the one it has, since a newer server delivers it
        (review of #161, round 2 — the first version gave the old-server loss
        the "no way to type" line, which is false: there is a way, on tmux
        3.5). ``version`` is what ``on_key`` read for the gate, handed down
        rather than read again. ``too_old`` is only ever produced under a
        version the gate read and found below the minimum — ``_extended_keys``
        fails open on ``None`` — but the type admits ``None``, so the wording
        covers it instead of asserting it away, and a direct call pins that
        wording rather than leaving the branch dead (reviews of #161, rounds
        3-5, one objection each to a dead string, an assert, and the number
        gone missing).

        TOTAL over ``DropReason``, and the type checker holds it so: a reason
        added to ``core.keys`` that this does not answer is a red ``mypy``, not
        a keystroke silently lost — the mute twin of the #151 bug (review of
        #161, round 3).
        """
        reason = drop.reason
        match reason:
            case "too_old":
                need = ".".join(str(part) for part in EXTENDED_MINIMUM)
                have = f"tmux {version[0]}.{version[1]}" if version is not None else "this tmux"
                self._warn_once(
                    key,
                    f"{have} cannot carry {_printable_name(key)} — {need} or newer can",
                    server=self.server.socket if self.server is not None else None,
                )
            case "no_name":
                self._warn_once(
                    key,
                    f"no way to type {_printable_name(key)} into a tmux pane",
                    severity="information",
                )
            case "command" | "nothing_to_type":
                pass  # nothing was pressed at the agent, and nothing is said
            case _ as unreachable:
                assert_never(unreachable)

    def _warn_once(
        self,
        key: str,
        message: str,
        *,
        severity: SeverityLevel = "warning",
        server: str | None = None,
    ) -> None:
        """Say ``message`` once per ``key`` — and per ``server``, when it names one.

        Severity is a property of the MESSAGE and not of the once-per-name
        mechanism, so each caller keeps its own answer (review): the wheel's
        "this program is fullscreen" notice and the too-old-tmux notice are
        warnings, an unmappable chord is information — :meth:`_explain` says
        which and why.

        Once per PANE, not per session: ``_warned`` is this widget's, and the
        app composes a ``TerminalPane`` per view — so that is the scope
        ``docs/fleet.md`` promises, and no wider (review). A line about a
        SERVER passes that server's socket as ``server`` — ``views/project.py``
        builds a fresh ``TmuxServer`` for the same socket at every attach, so
        the socket is what names a server here — and is said once per key name
        on EACH: the too-old line names the version, so a pane moved to another
        server hears it for that one (review of #161, round 6). A server
        restarted on the same socket under another tmux counts as the same
        one: the line is not said again, since its advice — 3.5 or newer —
        still holds, though the version it named is stale (round 8). Everything
        else is keyed by the key alone, and an attach re-arms nothing: round 6
        cleared the whole set there, and a restarted agent or a sign-in — a new
        pane on the same server — brought back the ``f13`` line, a fact about
        the key table, and the fullscreen one, the very re-toasting #151 is
        about (round 7).
        """
        if (server, key) in self._warned:
            return
        self._warned.add((server, key))
        self.notify(message, severity=severity, markup=False)

    def on_paste(self, event: events.Paste) -> None:
        if self.pane_id is None or self.server is None:
            return
        event.stop()
        self.scrollback = 0
        if self.text_selection is not None:
            self._clear_own_selection()  # input into the agent: the highlight is stale (rule 3)
        try:
            self.server.paste(self.pane_id, event.text)
        except TmuxUnavailable:
            self._fail(TMUX_UNAVAILABLE)
        except TmuxError:
            self._fail(PANE_GONE)
        self._schedule(self.FAST_INTERVAL)

    async def _on_click(self, event: events.Click) -> None:
        """Focus the pane; a double click selects the word under the pointer.

        Textual's defaults select the whole widget on a double click and the
        whole container on a triple — and the next ctrl+c, meant as the agent's
        interrupt, would copy the entire screen instead (reproduced in review).

        ONE handler, and no ``super()`` call. ``_get_dispatch_methods`` takes
        ``_on_click`` OR ``on_click`` per class in the MRO, never both, so the
        separate ``on_click`` that used to hold ``self.focus()`` was never
        called — click-to-focus survived only through the screen's own MouseDown
        focus. And the loop dispatches ``Widget._on_click`` itself, so calling it
        here as well brokered every plain click twice (review of the third
        version). ``prevent_default`` stops that loop before the base class, so
        the multi-click path owns the gesture and brokers it itself.

        The chain is the pane's own, not ``event.chain``. Textual chains clicks
        by release position and time alone, whatever the button, and
        synthesises a Click for a drag whose press and release land on the same
        widget — so a drag followed within half a second by a click on its end
        cell arrived here as a double click, replaced the dragged selection
        with a word and copied it twice (review of #135, finding 13). A click
        here is a press and a release in the same cell, with the LEFT button;
        only those count, and only in succession (class docstring, rule 6).
        """
        self.focus()
        if self._program_owns_mouse() and not event.shift:
            # The press and the release already went to the program (#148): a
            # double click is two presses there, and Claude Code does its own
            # word and line selection. Nothing to select here — and no default,
            # which would select the whole pane.
            event.prevent_default()
            return
        if event.widget is self and event.chain >= 2:
            # Textual's own multi-click would select the whole widget or its
            # container: never, whatever this pane makes of the click.
            event.prevent_default()
        if event.widget is self and self._own_click_chain(event) == 2:
            self._select_word(event.x, event.y)
        if event.widget is self and event.chain >= 2:
            await self.broker_event("click", event)

    def _own_click_chain(self, event: events.Click) -> int:
        """How many real LEFT clicks in a row this one makes; 0 when it is a
        drag's release, or a click with any other button.

        The button is read HERE, where the chain is counted, and not after it:
        counted first and gated after, a right click (paste, or a context menu,
        on most terminals) seeded the chain, and the left click that followed
        it in the same cell read as the second of two — a word selected and
        the clipboard written by a gesture that was one left click (review of
        the eighth version; review of #135, second round, finding 2). Any other
        button breaks the run, so the next left click starts one afresh.
        """
        if event.button != 1:
            self._clicks = 0
            self._last_click = None
            return 0
        moved = self._press is not None and event.offset != self._press
        threshold = self.app.CLICK_CHAIN_TIME_THRESHOLD
        last = self._last_click
        if moved:
            self._clicks = 0
        elif last is not None and last[0] == event.offset and event.time - last[1] <= threshold:
            self._clicks += 1
        else:
            self._clicks = 1
        self._last_click = None if moved else (event.offset, event.time)
        return self._clicks

    def _select_word(self, x: int, y: int) -> None:
        span = self._displayed_row(y).word_at(x)
        if span is None:
            return
        start, end = span
        word = Selection(Offset(start, y), Offset(end, y))
        self._set_own_selection(word)
        # Copied here, and folded into the baseline. The release this click IS
        # has already been routed (a Click is dispatched after its release is
        # forwarded), so this is defence in depth: whatever reads the baseline
        # next finds the word already accounted for (class docstring, rule 2).
        self._baseline = word
        self._copy_selection()

    # --- selection and copy ------------------------------------------------------------

    def on_mouse_down(self, event: events.MouseDown) -> None:
        """A press landed HERE: start the gesture, and run it if the program owns the mouse.

        The app has already told every pane the gesture began (rule 1); which of
        three gestures this is depends on what the program asked for.

        Under a program that does NOT track the mouse, nothing changed: Textual's
        selection machinery takes the drag (the screen decided that before this
        ran — see :meth:`allow_select`) and the release is heard through
        :meth:`selection_gesture_ended`. The press is remembered and this pane's
        baseline cleared, so an identical re-drag of a standing highlight copies
        again rather than reading as "nothing changed".

        Under one that DOES, shift+drag begins a LOCAL selection this widget runs
        itself — the same bookkeeping, and the mouse captured so the moves and the
        release arrive wherever they land; it ends, and copies, at this widget's
        own release (:meth:`_end_shift_drag`) — while any other press is forwarded as
        the SGR press the program asked for (#148), the mouse captured for the drag
        and the release to follow. A FORWARDED press makes no selection here, so it
        leaves the baseline the gesture started with: clearing it would make every
        click in an agent pane re-copy the standing highlight, because the app reads
        the release in :meth:`SelectionHost.on_event` whatever this handler stops.
        ``event.stop()`` on both: a press the program owns is not the app's.
        """
        if not self._program_owns_mouse():
            self._press = event.offset
            self._baseline = None
            return
        event.stop()
        self.focus()
        x, y = self._cell(event)
        if event.shift:
            self._press = event.offset
            self._baseline = None
            self._shift_drag = Offset(x, y)
            self._shift_button = event.button
            self._set_own_selection(Selection(Offset(x, y), Offset(x + 1, y)))
            self.capture_mouse()
            return
        self._forwarding = event.button
        code = self._button_code(event.button, event)
        # A move that stays in the pressed cell is no motion: a terminal reports
        # motion per cell, and Textual moves the pointer before every release.
        self._last_drag = (code, x, y)
        # The buffer as it stands, so a release can tell a NEW copy from an old one.
        self._buffer_before = self._read_buffer(self.server) if event.button == 1 else None
        self._queue_mouse("press", code, x, y)
        self.capture_mouse()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self._shift_drag is not None:
            event.stop()
            self._set_own_selection(self._shift_span(event))
            return
        if self._forwarding is not None:
            event.stop()
            facts = self.facts
            if facts is None or not facts.mouse_drag:
                return  # the program asked for presses and releases only (``?1000``)
            x, y = self._cell(event)
            report = (self._button_code(self._forwarding, event), x, y)
            if report == self._last_drag:
                return
            self._last_drag = report
            self._queue_mouse("drag", *report)

    def on_mouse_up(self, event: events.MouseUp) -> None:
        """End a gesture this widget began: the local shift+drag, or a forwarded button.

        The shift+drag ends with its span at the release's cell and is copied
        here, after the last move this widget was sent (:meth:`_end_shift_drag`
        says why not at the app's routing). A release where the press was is a
        click, which selects nothing. A forwarded left release arms the
        paste-buffer mirror. A release that never comes is
        :meth:`gesture_release_lost`.
        """
        if self._shift_drag is not None:
            event.stop()
            # The screen read this release as a click — the offset it pressed at
            # — and cleared every selection for it (``Screen._forward_event``).
            # Putting the one-cell span back left a highlight nothing copied,
            # and the next ctrl+c copied that cell instead of reaching the agent
            # as its interrupt (review of #203, round 1 of the terminal-ux fold).
            click = event.offset == self._press
            self._end_shift_drag(None if click else self._shift_span(event))
            return
        if self._forwarding is None:
            return
        event.stop()
        button, self._forwarding = self._forwarding, None
        self._last_drag = None
        self.release_mouse()
        x, y = self._cell(event)
        self._queue_mouse("release", self._button_code(button, event), x, y)
        before, self._buffer_before = self._buffer_before, None
        if button == 1:
            self._arm_mirror(before)

    def gesture_release_lost(self) -> None:
        """The app learned the gesture's release was lost: end what this widget runs itself.

        :meth:`on_mouse_up` is the only other end of a forwarded press or a
        shift+drag, and a release let go outside the terminal never reaches it.
        Both hold the pointer, so every bare move after it was this widget's:
        a forwarded press went on sending the program drags with no button down
        — Claude Code's selection followed the pointer — and a shift+drag went
        on extending the highlight the app had just copied; the next press
        anywhere in the app landed here too, and was forwarded as a second
        press with no release between (review of the fold of #148 into
        rc/fixes, whose :class:`SelectionHost` reads the lost release).

        The pointer is let go NOW, so the move or press that told the app is
        routed by where it is. The rest waits its turn in this widget's queue:
        the app hears of a gesture as the driver reports it, this widget when
        its own handlers run, and a burst can put the loss ahead of the press
        this widget has yet to forward. Then the program gets the release it
        was owed where the drag got to — the last cell it was sent, with that
        report's modifier bits — as it would have had the release come there,
        and a left one arms the paste-buffer mirror, since the program copies
        on that release. A shift+drag ends with its highlight where it got to,
        copied as its own release would have copied it (:meth:`_end_shift_drag`).
        """
        self.release_mouse()
        self.call_later(self._end_lost_gesture)

    def _end_lost_gesture(self) -> None:
        """:meth:`gesture_release_lost`'s second half, after this widget's queued events."""
        self.release_mouse()
        if self._shift_drag is not None:
            self._end_shift_drag(self._own_selection())
        if self._forwarding is None:
            return
        button, self._forwarding = self._forwarding, None
        report, self._last_drag = self._last_drag, None
        if report is not None:
            self._queue_mouse("release", *report)
        before, self._buffer_before = self._buffer_before, None
        if button == 1:
            self._arm_mirror(before)

    def _end_shift_drag(self, span: Selection | None) -> None:
        """End the local shift+drag with ``span`` highlighted, and copy it as a release does.

        Here, in this widget's own queue, and not where the app routes the
        release (:meth:`selection_gesture_ended`, which passes over a pane with a
        shift+drag running). The app hears a gesture as the driver reports it
        and this widget runs the drag from its own handlers, so at the routing
        the span was as far as this widget had got: in a burst, nowhere — the
        highlight then stood uncopied, and the next ctrl+c copied it instead of
        interrupting — and with less lag, short of the highlight left on screen
        (review of #203, round 1 of the terminal-ux fold). The routing's rules
        hold here: a span with no text under it is dropped, and a left drag is
        told why nothing was copied; only the left button copies; what this
        widget copied goes into its baseline, so no later release copies it again.
        """
        button, self._shift_drag = self._shift_button, None
        self.release_mouse()
        self._set_own_selection(span)
        if span is None:
            return
        if self.selected_text() is None:
            self._clear_own_selection()
            if button == 1:
                self.notify(NOTHING_TO_COPY, markup=False)
            return
        if button == 1 and self._copy_selection():
            self._baseline = span

    def _cell(self, event: events.MouseEvent) -> tuple[int, int]:
        """The widget cell under the pointer, clamped to the rows and columns shown.

        A captured pointer reports offsets outside the widget — negative, or past
        the edge — and the program has no cell there to receive them.
        """
        width, height = self.content_size
        return (
            min(max(int(event.x), 0), max(width - 1, 0)),
            min(max(int(event.y), 0), max(height - 1, 0)),
        )

    def _shift_span(self, event: events.MouseEvent) -> Selection:
        """The local selection from where the shift+drag began to the cell under the pointer.

        Ordered, whichever way the drag went: ``Selection.get_span`` reorders
        nothing, so a reversed pair paints — and copies — nothing (see
        :func:`_extract`). The cell under the pointer is included, as in every
        terminal.
        """
        assert self._shift_drag is not None
        x, y = self._cell(event)
        here, there = Offset(x, y), self._shift_drag
        if (here.y, here.x) < (there.y, there.x):
            here, there = there, here
        return Selection(there, Offset(here.x + 1, here.y))

    @staticmethod
    def _button_code(button: int, event: events.MouseEvent) -> int:
        """The SGR button number: 0, 1, 2 for left, middle, right, plus the modifier bits.

        Shift 4, alt (meta) 8, ctrl 16 — the xterm encoding every mouse-tracking
        program reads, which is how ctrl+click opens a link in Claude Code.
        """
        code = {1: 0, 2: 1, 3: 2}.get(button, 0)
        if event.shift:
            code += 4
        if event.meta:
            code += 8
        if event.ctrl:
            code += 16
        return code

    def _read_buffer(self, server: TmuxServer | None) -> str | None:
        """``server``'s newest paste buffer, or ``None`` when there is none or tmux cannot say."""
        if server is None:
            return None
        try:
            return server.show_buffer()
        except TmuxError:
            return None

    def _arm_mirror(self, before: str | None) -> None:
        """A forwarded left release: read the paste buffer after the program's copy has run.

        ``before`` is the buffer as it stood at that release's own press. Each
        release used to arm a timer that read ONE shared snapshot, which the
        first to run emptied: the second mirror of a double-click — or the one
        after a right press inside the delay, which set the snapshot to nothing
        — compared the buffer with nothing and copied whatever tmux held, an
        hour-old copy included, with a toast (review of #203, round 1 of the
        terminal-ux fold). Now the snapshot travels with the pending mirror, and
        so does the server the press went to: an attach inside the delay may
        move this widget to another server (``ManagerTab`` sets ``server``, then
        attaches), whose buffer says nothing about this snapshot.

        A release while a mirror is still pending folds into it rather than
        arming a second: one read, after the LAST release, against the buffer
        from before the FIRST press. Two reads each saw what the program wrote
        at the second release — the word of a double-click went out twice, with
        two toasts — and the earlier one ran before that write could land. The
        earlier timer still fires, and finds itself superseded.
        """
        server = self.server
        if self._mirror is not None:
            _, server, before = self._mirror
        token = object()
        self._mirror = (token, server, before)
        self.set_timer(
            self.BUFFER_MIRROR_DELAY, partial(self._mirror_buffer, token), name="buffer-mirror"
        )

    def _mirror_buffer(self, token: object) -> None:
        """After a forwarded left release: a paste buffer the program wrote is copied out.

        Claude Code's copy-on-select runs ``wl-copy``/``xclip`` in the PANE's
        environment — the tmux server's, which may have no display — and, inside
        tmux, writes the tmux paste buffer. That buffer is the one place the
        selection is sure to land, so one that changed between the press and now
        goes to the outer terminal's clipboard (OSC 52), the way this widget's
        own copies do. Unchanged, or none: nothing was selected there — a click
        that placed the cursor — and nothing is said. ``token`` names the arming
        this timer was set for; when it no longer matches, a later release took
        the mirror over and reads after its own release (:meth:`_arm_mirror`).
        """
        mirror = self._mirror
        if mirror is None or mirror[0] is not token:
            return
        self._mirror = None
        _, server, before = mirror
        after = self._read_buffer(server)
        if not after or after == before:
            return
        self.app.copy_to_clipboard(after)
        count = len(after)
        self.notify(
            f"copied {count} character{'s' if count != 1 else ''} — the agent's own selection",
            markup=False,
        )

    def _set_own_selection(self, selection: Selection | None) -> None:
        """Write THIS widget's entry on the screen, leaving every other widget's alone.

        ``Screen.clear_selection`` is ``selections = {}`` — every widget on the
        screen — and replacing the dict with one entry is the same harm by the
        other route. The app keeps a view per opened agent mounted, so either
        one wipes the highlight the user has in another pane (reviews of the
        eighth and ninth versions). Both writers go through here, so the rule
        lives in one place.
        """
        selections = {
            widget: span for widget, span in self.screen.selections.items() if widget is not self
        }
        if selection is not None:
            selections[self] = selection
        self.screen.selections = selections

    def _clear_own_selection(self) -> None:
        """Drop this widget's selection and nobody else's."""
        self._set_own_selection(None)

    def selection_gesture_started(self) -> None:
        """A button went down somewhere on screen: note what this pane has now.

        Whatever a release finds here that differs from this is what the gesture
        selected (class docstring, rule 2). Read at the app's press, before the
        screen has done anything with it, so the baseline is the selection as
        it stood — a standing highlight the gesture then leaves alone is not
        copied again.
        """
        self._baseline = self._own_selection()

    def selection_gesture_ended(self, button: int | None = None) -> GestureEnd | None:
        """The button came up somewhere on screen: copy what the gesture left here.

        Routed by the app, because the app is the only place the release is
        certain to arrive: a widget below the screen may never see it — an
        ``Input`` calls ``capture_mouse``, a scrollbar stops it — and copying
        from this pane's own ``on_mouse_up`` made the same gesture behave
        differently depending on the neighbour (review of #120, round 6).

        Copies exactly when this pane's selection differs from the one it had
        when the gesture began, and only for the LEFT button. The app hears
        every release, including ones with nothing to do with a pane — a drag on
        the Footer, a scrollbar, a button — and those leave a standing highlight
        as it was (review of #120, round 7); a right-button drag across it
        changes it and must still not copy (round 3); a word this pane selected
        on a double click is already in the baseline (rule 2). An UNKNOWN
        button is not a copy: reading "no press was seen" as "left" is the
        assumption that let a right-button drag copy (round 8).

        A changed selection with no text under it — rows nothing was printed
        on, the blank past a line's end — is dropped here, whatever the button
        (rule 3). Left standing, it was tinted full width, the release copied
        nothing and said nothing, and the ctrl+c the highlight invited went to
        the agent as its interrupt (review of #120, round 11). A left drag was
        a request to copy, so it is told why none happened — by the routing,
        once for the gesture, which is what the answer is for: ``"copied"``,
        ``"blank"`` for a highlight dropped with nothing under it, else ``None``
        (:func:`route_selection_gesture`).

        A shift+drag this pane runs itself (#148) is passed over: this widget
        has not handled its release yet, so the span here is only as far as the
        drag had got, and :meth:`_end_shift_drag` copies it at that release. A
        burst can route the release before this widget has handled the press;
        its selection is then the baseline still, and nothing is copied here.
        """
        if self._shift_drag is not None:
            return None
        selection = self._own_selection()
        if selection is None or selection == self._baseline:
            return None
        if self.selected_text() is None:
            self._clear_own_selection()
            return "blank"
        if button != 1 or not self._copy_selection():
            return None
        self._baseline = selection
        return "copied"

    def copy_standing_selection(self) -> bool:
        """The copy KEY: copy this pane's standing highlight and clear it; whether there was one.

        One method for ctrl+c and cmd+c with the pane focused and for the same
        keys anywhere else (:class:`PaneScreen`), so the two cannot disagree
        about what is copied or what happens to the highlight (class docstring,
        rule 5). Cleared afterwards: the toast for a drag promises "ctrl+c
        copies again while the selection stands", and a second ctrl+c after
        this one is the agent's interrupt, as the user expects of a key that
        just reported a copy (review of #120, round 8).
        """
        if not self._copy_selection(standing=False):
            return False
        self._clear_own_selection()
        return True

    def _copy_selection(self, *, standing: bool = True) -> bool:
        """Copy the selected text to the clipboard (OSC 52); False when nothing is selected.

        ``standing`` is whether the highlight survives this copy. The key paths
        clear it immediately afterwards, so telling the user there that "ctrl+c
        copies again while the selection stands" was false as they read it — the
        next ctrl+c is the agent's interrupt (review of the eighth version).
        """
        text = self.selected_text()
        if text is None:
            return False
        self.app.copy_to_clipboard(text)
        count = len(text)
        again = " — ctrl+c copies again while the selection stands" if standing else ""
        self.notify(
            f"copied {count} character{'s' if count != 1 else ''}{again}",
            markup=False,
        )
        return True

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        if self.attached:
            event.stop()
            event.prevent_default()
            self._wheel(event, up=True)

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        if self.attached:
            event.stop()
            event.prevent_default()
            self._wheel(event, up=False)

    def _scroll_owner(self) -> str:
        """Who a scroll gesture belongs to: ``history`` (tmux's, this widget's
        offset), ``program`` (a mouse-tracking program's own transcript) or
        ``none`` (a fullscreen program that takes neither).

        One decision for the wheel and the scroll keys alike — the review of the
        routing found the keys bypassing it and pulling stale pre-launch shell
        lines over a Claude Code pane.
        """
        facts = self.facts
        if self.scrollback or facts is None or facts.in_mode:
            return "history"
        if facts.mouse_on:
            return "program"
        if facts.alternate_on:
            return "none"
        return "history"

    def _wheel(self, event: events.MouseEvent, *, up: bool) -> None:
        """Route a wheel notch to whoever can act on it.

        Panes look alike from outside and want different things:

        * a view this widget has already scrolled into history is the widget's
          own — the wheel always brings it back, whatever the program wants;
        * a pane in tmux copy mode belongs to tmux for the moment — forwarding a
          mouse event there cancels the mode and delivers nothing (measured on
          3.7c), so the history offset is used instead;
        * a program that tracks the mouse (Claude Code's fullscreen TUI turns on
          ``?1000`` + ``?1006`` and scrolls its own transcript on the wheel) gets
          the notch as the mouse event it asked for — reported 2026-09-08 as
          "scroll not working": this widget was scrolling tmux's history, which
          the alternate screen does not have;
        * a program on the alternate screen that does NOT track the mouse is
          told, once, that its own keys are the way: arrow keys would land in a
          prompt (Claude Code's ``Up`` recalls a previous prompt) and there is no
          history behind it to scroll;
        * anything else scrolls tmux's history, as before.
        """
        owner = self._scroll_owner()
        if owner == "none":
            self._warn_fullscreen()
            return
        if owner == "history":
            self.scroll_history(self.WHEEL_LINES if up else -self.WHEEL_LINES)
            return
        self._queue_notches(up, 1, event.x + 1, event.y + 1)

    def _scroll_by_key(self, action: str) -> None:
        """The scroll keys, through the same owner decision as the wheel.

        On tmux history: a screen per page, the top, live. On a program that
        owns its own transcript: the same distance as wheel notches, at the
        pane's centre — the one scroll vocabulary such a program is known to
        speak. ``top``/``live`` there are ten pages: the program's depth is
        not knowable from outside.
        """
        owner = self._scroll_owner()
        page = max(1, self.content_size.height - 1)
        if owner == "none":
            self._warn_fullscreen()
            return
        if owner == "history":
            distance = {
                "page_up": page,
                "page_down": -page,
                "top": self.history_size,
                "live": -self.history_size,
            }[action]
            self.scroll_history(distance)
            return
        notches = max(1, page // self.WHEEL_LINES)
        if action in ("top", "live"):
            notches *= 10
        width, height = self.content_size
        self._queue_notches(action in ("page_up", "top"), notches, width // 2 + 1, height // 2 + 1)

    def _warn_fullscreen(self) -> None:
        self._warn_once(
            "wheel:alternate-screen",
            "this program is fullscreen and does not take the mouse — scroll it with its own keys",
        )

    def _queue_notches(self, up: bool, count: int, x: int, y: int) -> None:
        """Queue ``count`` wheel notches for the program at 1-based widget cell ``(x, y)``."""
        for _ in range(count):
            self._queue_mouse("wheel", 64 if up else 65, x - 1, y - 1)

    def _queue_mouse(self, kind: str, code: int, x: int, y: int) -> None:
        """Queue one mouse event for the program at widget cell ``(x, y)`` (0-based).

        Presses, drags, releases and notches share one queue so they reach the
        program in the order they happened.
        """
        facts = self.facts
        if facts is None:
            return
        # The pane may be taller than the widget between a Resize and its
        # debounced resize-window: the widget shows the pane's LAST rows, so a
        # widget row maps to a pane row that many lines further down.
        offset = max(0, facts.height - self.content_size.height)
        self._mouse_queue.append((kind, code, x + 1, y + 1 + offset))
        if self._mouse_timer is None:
            # One tmux client per FLUSH, not per event: a trackpad flick is 20-50
            # notches a second, a drag as many moves, each its own fork+exec before.
            self._mouse_timer = self.set_timer(self.WHEEL_COALESCE, self._flush_mouse, name="mouse")

    def _flush_mouse(self) -> None:
        """Send every mouse event queued since the last flush as one tmux call."""
        self._mouse_timer = None
        queue, self._mouse_queue = self._mouse_queue, []
        facts = self.facts
        if not queue or self.pane_id is None or self.server is None or facts is None:
            return
        try:
            if facts.mouse_sgr:
                self.server.send_literal(self.pane_id, "".join(_sgr(*event) for event in queue))
            else:
                self.server.send_bytes(self.pane_id, b"".join(_x10(*event) for event in queue))
        except TmuxUnavailable:
            self._fail(TMUX_UNAVAILABLE)
            return
        except TmuxError:
            self._fail(PANE_GONE)
            return
        self._schedule(self.FAST_INTERVAL)

    def scroll_history(self, delta: int) -> None:
        """Move the view ``delta`` lines up (positive) into history, clamped; 0 is live."""
        target = max(0, min(self.history_size, self.scrollback + delta))
        if target == self.scrollback:
            return
        self.scrollback = target
        self.refresh_frame()
        self._schedule(self.FAST_INTERVAL)

    # --- size ----------------------------------------------------------------------------

    def on_resize(self, event: events.Resize) -> None:
        if self._resize_timer is not None:
            self._resize_timer.stop()
        self._resize_retry = self.RESIZE_RETRY  # a new size, not the failed one's backoff
        self._resize_timer = self.set_timer(
            self.RESIZE_DEBOUNCE, self._sync_size, name="terminal-resize"
        )

    def _sync_size(self) -> None:
        """Size the tmux window to this widget's content area (once per distinct size)."""
        self._resize_timer = None
        if self.pane_id is None or self.server is None:
            return
        width, height = self.content_size
        if width <= 0 or height <= 0:
            return
        wanted = (self.pane_id, width, height)
        if wanted == self._synced:
            return
        try:
            self.server.resize(self.pane_id, width, height)
        except TmuxUnavailable:
            self._fail(TMUX_UNAVAILABLE)
            self._retry_size()
            return
        except TmuxError:
            self._fail(PANE_GONE)
            self._retry_size()
            return
        self._resize_retry = self.RESIZE_RETRY
        self._synced = wanted
        self.refresh_frame()
        self._schedule(self.FAST_INTERVAL)

    def _retry_size(self) -> None:
        """Ask again later. Nothing else would: the only other caller is ``Resize``.

        See the module's "Failing open" paragraph for what an un-retried failure
        costs — a window left at its spawn geometry for the life of the view.
        """
        if self._resize_timer is not None:
            self._resize_timer.stop()
        self._resize_timer = self.set_timer(
            self._resize_retry, self._sync_size, name="terminal-resize-retry"
        )
        self._resize_retry = min(self._resize_retry * 2, self.RESIZE_RETRY_MAX)
