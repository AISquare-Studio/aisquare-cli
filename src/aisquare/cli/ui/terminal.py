"""``TerminalPane`` — a tmux pane rendered inside the fleet UI, keys forwarded.

The risky core of docs/plans/fleet-tui.md (§6, §4.3, §3.1): tmux is the
terminal emulator, this widget is a viewport onto one of its panes.

Rendering. A frame is one ``server.capture`` (one tmux process:
``capture-pane -e`` + ``display-message``). Each captured row is a string with
SGR escapes; rows are diffed against the previous frame as STRINGS, and only
the rows that changed — plus the old and new cursor rows — are marked dirty,
so Textual's Line API (:meth:`render_line`) is asked for exactly those. A row
string becomes a :class:`Strip` through ``rich.text.Text.from_ansi``, cached by
the string, so a row that scrolled by one line is a dict lookup. The cursor is
a reverse-video cell (underline while the pane is unfocused) when tmux says it
is visible and the view is live (scrollback 0).

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
:class:`EscapeToSidebar` and is never forwarded. A key tmux has no safe name for
is dropped, with ONE warning per key name. ``Paste`` goes through the paste
buffer so the agent sees one bracketed paste. The wheel scrolls our own offset
over the pane's history (clamped to ``history_size``); any key returns to live.
``Resize`` is forwarded as ``resize-window`` after a 100 ms debounce. Forwarded
input re-arms the fast cadence, so an echo never waits for the idle tick.

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
from typing import ClassVar

from rich.cells import cell_len
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import events
from textual.geometry import Offset, Region
from textual.message import Message
from textual.selection import Selection
from textual.strip import Strip
from textual.timer import Timer
from textual.widget import Widget

from aisquare.core.keys import (
    ARGV_SEPARATOR,
    EXTENDED_MINIMUM,
    Translation,
    translate,
)
from aisquare.core.tmux import PaneFacts, TmuxError, TmuxServer, TmuxUnavailable
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


def _extract(selection: Selection, rows: list[str]) -> str:
    """The text ``selection`` covers in ``rows`` — one entry per rendered row,
    indices clamped, character offsets as the compositor reports them."""
    if not rows:
        return ""
    last = len(rows) - 1
    if selection.start is None:
        start_row, start_col = 0, 0
    else:
        start_row, start_col = min(selection.start.y, last), selection.start.x
    if selection.end is None:
        end_row, end_col = last, len(rows[last])
    else:
        end_row, end_col = min(selection.end.y, last), selection.end.x
    if start_row == end_row:
        return rows[start_row][start_col:end_col]
    first, *middle, final = rows[start_row : end_row + 1]
    return "\n".join([first[start_col:], *middle, final[:end_col]])


class EscapeToSidebar(Message):
    """The user pressed the escape hatch: focus goes back to the sidebar."""


class TerminalPane(Widget, can_focus=True):
    """One tmux pane, live. ``attach(pane_id)`` switches what it shows."""

    DEFAULT_CSS = """
    TerminalPane { height: 1fr; width: 1fr; }
    """

    #: Drag-select over the rendered rows (§4.3). Textual's default
    #: ``get_selection`` reads ``render()`` output, which a Line API widget does
    #: not have, so this widget supplies its own from the rows it showed when
    #: the drag began and paints the span itself in :meth:`render_line`. Copy is
    #: on release and on ctrl+c while a selection exists; without one ctrl+c
    #: reaches the agent. Double-click selects a word; a triple click nothing —
    #: Textual's defaults would select the whole pane, and ctrl+c would then
    #: copy 3000 characters instead of interrupting the agent.
    ALLOW_SELECT: ClassVar[bool] = True
    OFFSET_CACHE_LIMIT: int = 2048
    """Offset-stamped rows kept per ``(line, y)`` before the cache is emptied."""

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
    """Seconds notches are gathered before one tmux call carries them all."""
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
        self._extended: bool | None = None
        """Whether the server delivers extended chords; read once per server."""
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
        self._cursor: tuple[int, int] | None = None
        self._strip_cache: dict[str, Strip] = {}
        self._timer: Timer | None = None
        self._resize_timer: Timer | None = None
        self._resize_retry: float = self.RESIZE_RETRY
        self._synced: tuple[str, int, int] | None = None
        self._warned: set[str] = set()
        self._reported_gone = False
        self._wheel_queue: list[tuple[bool, int, int]] = []
        """Notches (up?, pane column, pane row) awaiting one forwarding call."""
        self._wheel_timer: Timer | None = None
        self._marker: tuple[int, int] | None = None
        """``(scrollback, history)`` the corner marker last showed, or ``None``."""
        self._offset_cache: dict[tuple[str, int], Strip] = {}
        """Offset-stamped rows, already painted in ``rich_style`` — so emptied
        whenever the resolved style changes (see :meth:`notify_style_update`)."""
        self._selection_rows: list[str] | None = None
        """The rows' plain text frozen when a drag began, so what is copied is what
        was highlighted — the live buffer moves every 50 ms under a printing agent."""
        self._selection_bg: Style | None = None
        """The selection tint, resolved once per selection rather than per row."""
        self._drag_from: Offset | None = None
        """Where the left button went down, while it is still down."""

    # --- what is shown -----------------------------------------------------------------

    @property
    def attached(self) -> bool:
        return self.pane_id is not None

    @property
    def history_size(self) -> int:
        """tmux's history behind the live screen — 0 until the first frame answers."""
        return self.facts.history_size if self.facts is not None else 0

    def _extended_keys(self) -> bool:
        """Whether this server delivers extended chords (tmux ≥ 3.5), read once.

        Below :data:`~aisquare.core.keys.EXTENDED_MINIMUM` tmux TYPES those
        chords' names into the agent (measured on 3.3a/3.4), so ``translate``
        drops them there. Fail-open to True when the version cannot be read:
        ``tmux -V`` answers on anything alive, and refusing shift+enter on
        every modern server to guard a hypothetical mute one inverts the trade.
        """
        if self._extended is None:
            version: tuple[int, int] | None = None
            if self.server is not None:
                with contextlib.suppress(TmuxError):
                    version = self.server.version()
            self._extended = version is None or version >= EXTENDED_MINIMUM
        return self._extended

    def notify_style_update(self) -> None:
        """Textual's "your resolved styles changed" hook — empty the painted cache.

        ``_offset_cache`` holds rows with ``rich_style`` already applied, so a
        live theme change (the ``t`` picker, the command palette) or any CSS
        refresh left every quiet row painted in the OLD colours, and a fully
        idle pane never recovered (review). Unlike ``_strip_cache``, which
        stores rows BEFORE the base style is applied and is theme-safe, this one
        has the theme baked in.
        """
        super().notify_style_update()
        self._offset_cache.clear()

    def attach(self, pane_id: str | None) -> None:
        """Show ``pane_id`` (``None`` clears the pane) and restart the render loop."""
        self.pane_id = pane_id
        self.scrollback = 0
        self.facts = None
        self.notice = None
        self._lines = []
        self._cursor = None
        self._synced = None
        self._resize_retry = self.RESIZE_RETRY
        self._reported_gone = False
        self._marker = None
        self._selection_rows = None
        self._offset_cache.clear()
        if self.is_mounted and self.text_selection is not None:
            self.screen.clear_selection()  # agent A's highlight must not sit on agent B
        self._wheel_queue = []
        if self._wheel_timer is not None:
            self._wheel_timer.stop()
            self._wheel_timer = None
        # A new attach may be a new server — ``ManagerTab`` assigns ``server``
        # then calls this — and a cached "extended chords are fine" from a 3.7
        # server would TYPE ``S-Enter`` into an agent on a 3.4 one. Re-read
        # lazily, on the next key: one ``tmux -V`` per attach at most.
        self._extended = None
        if pane_id is not None and self.server is None:
            # The fleet's server from config — a default like any other (§3.10).
            self.server = fleet_service.server()
        if self.is_mounted:
            self._sync_size()
            self.refresh_frame()
            self._schedule(self.FAST_INTERVAL)
        self.refresh()

    def on_mount(self) -> None:
        self.refresh_frame()
        self._schedule(self.FAST_INTERVAL)

    def on_unmount(self) -> None:
        if self._timer is not None:
            self._timer.stop()
        if self._wheel_timer is not None:
            self._wheel_timer.stop()
        if self._resize_timer is not None:
            self._resize_timer.stop()

    def on_show(self) -> None:
        """A hidden tab came back: pick the pane up at the fast cadence."""
        self.refresh_frame()
        self._schedule(self.FAST_INTERVAL)

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
        dirty = {y for y, line in enumerate(lines) if y >= len(previous) or previous[y] != line}
        if cursor != self._cursor:
            for point in (cursor, self._cursor):
                if point is not None:
                    dirty.add(point[1])
        if notice != self.notice:
            dirty.add(height - 1)
        # The corner marker: row 0 repaints whenever k or the history it is
        # measured against moved — including the clamp above, and history that
        # keeps growing under a frozen scrolled view. Decided HERE, once, rather
        # than at every site that touches ``scrollback``.
        marker = (self.scrollback, facts.history_size) if self.scrollback else None
        if marker != self._marker:
            dirty.add(0)
        self._marker = marker
        self._lines = lines
        self._cursor = cursor
        self.facts = facts
        self.notice = notice
        self._repaint_rows(dirty)
        return bool(dirty)

    def _fail(self, notice: str) -> bool:
        """Keep the last frame, show ``notice`` in the bottom row; True when that is new."""
        changed = notice != self.notice or self._cursor is not None
        self.notice = notice
        self._cursor = None
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

    def render_line(self, y: int) -> Strip:
        # Every return is offset-stamped: the compositor reads a drag's content
        # offset from segment metadata that Textual's ``render()`` path stamps
        # and a Line API widget must stamp itself — on EVERY row, or a drag that
        # touches an unstamped one (the notice row, a blank row) resolves to
        # "select all".
        plain = (
            self.pane_id is not None
            and self.text_selection is None
            and not (self._cursor is not None and self._cursor[1] == y)
            and not (y == 0 and self.scrollback)
            and not (self.notice is not None and y == self.content_size.height - 1)
        )
        if not plain:
            return self._render_row(y).apply_offsets(0, y)
        # The common row — no cursor, selection, marker or notice on it — is a
        # pure function of its text and row, so the stamped strip is kept:
        # ``apply_offsets`` rebuilds every segment's style otherwise, and the
        # render loop asks for the same rows twenty times a second.
        key = (self._lines[y] if y < len(self._lines) else "", y)
        cached = self._offset_cache.get(key)
        if cached is not None and cached.cell_length == self.content_size.width:
            return cached
        if len(self._offset_cache) >= self.OFFSET_CACHE_LIMIT:
            self._offset_cache.clear()
        stamped = self._render_row(y).apply_offsets(0, y)
        self._offset_cache[key] = stamped
        return stamped

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
        if self.notice is not None and y == height - 1:
            return Strip([Segment(self.notice, base + NOTICE)]).adjust_cell_length(width, base)
        line = self._lines[y] if y < len(self._lines) else ""
        strip = self._strip_for(line).apply_style(base).adjust_cell_length(width, base)
        # Every restyle below crops the strip, and ``Strip.crop`` through a wide
        # glyph renders it as two single-cell spaces — one character more than
        # the row had, which shifts every offset stamped after it (measured: a
        # cursor on ``日`` at cell 0 moved a drag's whole selection by one).
        # So every crop is snapped to a glyph boundary, and this is the text
        # those boundaries come from.
        text = self._row_text(y)
        if self._cursor is not None and self._cursor[1] == y and self._cursor[0] < width:
            strip = self._with_cursor(strip, self._cursor[0], text)
        selection = self.text_selection
        if selection is not None:
            span = selection.get_span(y)
            if span is not None:
                strip = self._with_selection(strip, span, text, width)
        if y == 0 and self.scrollback:
            strip = self._with_scroll_marker(strip, width, text)
        return strip

    @staticmethod
    def _snap(text: str, start: int, end: int) -> tuple[int, int]:
        """``[start, end)`` in cells, widened to the glyph boundaries of ``text``."""
        bounds = [0]
        for char in text:
            bounds.append(bounds[-1] + cell_len(char))
        snapped_start = max((b for b in bounds if b <= start), default=start)
        snapped_end = min((b for b in bounds if b >= end), default=end)
        return snapped_start, snapped_end

    def _restyled(self, strip: Strip, start: int, end: int, style: Style, text: str) -> Strip:
        """``strip`` with cells ``[start, end)`` restyled — ``style`` layered LAST.

        ``Strip.apply_style`` puts the segment's own style on top, and every
        segment already carries the widget background from ``apply_style(base)``
        — so a tint applied that way lost to it. Rich's ``post_style`` is the
        layering that wins, and control segments keep their ``None`` style.
        The span is snapped to glyph boundaries of ``text`` (see ``_render_row``).
        """
        start, end = self._snap(text, start, end)
        if start >= end:
            return strip
        span = strip.crop(start, end)
        painted = Strip(list(Segment.apply_style(span, post_style=style)), span.cell_length)
        return Strip.join([strip.crop(0, start), painted, strip.crop(end)])

    def _with_selection(self, strip: Strip, span: tuple[int, int], text: str, width: int) -> Strip:
        """Paint ``span`` — CHARACTER offsets from the compositor — as cells.

        ``apply_offsets`` counts characters; ``Strip.crop`` counts cells. On a row
        with wide glyphs (emoji status markers, CJK) the two diverge by one
        column per wide character, so what was painted was not what was copied.
        The row's own text converts one to the other.
        """
        start, end = span
        cell_start = cell_len(text[:start])
        cell_end = width if end == -1 else min(cell_len(text[:end]), width)
        if self._selection_bg is None:
            # The BACKGROUND only. The theme's ``screen--selection`` component
            # style resolves with foreground equal to background (``#094472 on
            # #094472`` — measured), so applying it whole painted the text
            # invisible: a solid block where the word was.
            bg = self.selection_style.bgcolor
            self._selection_bg = Style(bgcolor=bg) if bg is not None else Style(reverse=True)
        return self._restyled(strip, min(cell_start, width), cell_end, self._selection_bg, text)

    def _row_text(self, y: int) -> str:
        """Row ``y`` as plain text — from the frozen snapshot while a drag stands."""
        rows = self._selection_rows
        if rows is not None and y < len(rows):
            return rows[y]
        if self.notice is not None and y == self.content_size.height - 1:
            return self.notice  # what is displayed, not the row hidden under it
        line = self._lines[y] if y < len(self._lines) else ""
        return self._strip_for(line).text.rstrip()

    def _row_texts(self) -> list[str]:
        # As many rows as the widget RENDERS, not as many as the last frame
        # filled: ``render_line`` stamps offsets up to ``content_size.height``,
        # while ``_lines`` is only re-padded to that height by the next
        # successful frame. Between a grow-resize (or a ``_fail`` that returns
        # early) and that frame, ``_extract``'s clamp folded a drag on the new
        # bottom rows onto the LAST row's text and copied that (review).
        return [self._row_text(y) for y in range(max(len(self._lines), self.content_size.height))]

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        """The plain text under ``selection``, from the rows this widget shows.

        Own extraction rather than ``Selection.extract``: that re-splits with
        ``str.splitlines()``, which drops a trailing empty row and breaks on form
        feeds — so a drag in the blank area under an agent's output indexed past
        the end and took the app down (reproduced in review), and one ``\x0c``
        from a pager shifted every row after it.
        """
        if self.pane_id is None:
            return None
        return _extract(selection, self._row_texts()), "\n"

    def selected_text(self) -> str | None:
        """What a drag has selected in this pane, or ``None`` when nothing is."""
        selection = self.text_selection
        if selection is None:
            return None
        extracted = self.get_selection(selection)
        return extracted[0] if extracted and extracted[0] else None

    def selection_updated(self, selection: Selection | None) -> None:
        if selection is None:
            self._selection_rows = None
            self._selection_bg = None
        elif self._selection_rows is None:
            self._selection_rows = self._row_texts()  # freeze what is being selected
        self.refresh()

    def _with_scroll_marker(self, strip: Strip, width: int, text: str) -> Strip:
        """``[↑k/history]`` in the top-right corner while the view is in history.

        tmux's own copy-mode indicator, in the same place: without it a scrolled
        pane is indistinguishable from a live one that happens to be quiet. The
        cut is snapped to a glyph boundary; a widened gap is blank.
        """
        marker = f"[↑{self.scrollback}/{self.history_size}]"
        if len(marker) >= width:
            return strip
        cut, _ = self._snap(text, width - len(marker), width - len(marker))
        gap = " " * (width - len(marker) - cut)
        return Strip.join(
            [
                strip.crop(0, cut),
                Strip(
                    [
                        Segment(gap, self.rich_style),
                        Segment(marker, self.rich_style + SCROLL_MARKER),
                    ]
                ),
            ]
        )

    def _strip_for(self, line: str) -> Strip:
        strip = self._strip_cache.get(line)
        if strip is None:
            if len(self._strip_cache) >= self.CACHE_LIMIT:
                self._strip_cache.clear()
            text = Text.from_ansi(line, end="")
            strip = Strip(text.render(self.app.console)).simplify()
            self._strip_cache[line] = strip
        return strip

    def _with_cursor(self, strip: Strip, x: int, text: str) -> Strip:
        style = CURSOR if self.has_focus else UNFOCUSED_CURSOR
        widened = strip.extend_cell_length(x + 1, self.rich_style)
        return self._restyled(widened, x, x + 1, style, text)  # a wide glyph: both cells

    # --- input ---------------------------------------------------------------------------

    def _is_escape(self, event: events.Key) -> bool:
        hatch = self.escape_key.lower()
        return event.key.lower() == hatch or hatch in (alias.lower() for alias in event.aliases)

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
        if event.key == "ctrl+c" and self._copy_selection():
            # Every terminal emulator does this: ctrl+c copies while text is
            # selected. Without a selection it is the agent's interrupt.
            self.screen.clear_selection()
            return
        if event.key == "super+c":
            # macOS Cmd+C is copy and nothing else. It must not fall through to
            # the key table either: ``super`` is not a modifier tmux can spell,
            # the event carries a printable ``c``, and the pane typed a bare
            # ``c`` into the agent for a copy gesture (review).
            if self._copy_selection():
                self.screen.clear_selection()
            return
        translation = translate(
            event.key,
            event.character,
            printable=event.is_printable,
            extended_keys=self._extended_keys(),
        )
        if translation is None:
            self._warn_once(event.key, f"{event.key}: tmux has no name for this key — dropped")
            return
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

    def _warn_once(self, key: str, message: str) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        self.notify(message, severity="warning", markup=False)

    def on_paste(self, event: events.Paste) -> None:
        if self.pane_id is None or self.server is None:
            return
        event.stop()
        self.scrollback = 0
        try:
            self.server.paste(self.pane_id, event.text)
        except TmuxUnavailable:
            self._fail(TMUX_UNAVAILABLE)
        except TmuxError:
            self._fail(PANE_GONE)
        self._schedule(self.FAST_INTERVAL)

    def on_click(self, event: events.Click) -> None:
        self.focus()

    async def _on_click(self, event: events.Click) -> None:
        """Double-click selects the word under the pointer; a triple click nothing.

        Textual's defaults select the whole widget on a double click and the
        whole container on a triple — and the next ctrl+c, meant as the agent's
        interrupt, would copy the entire screen instead (reproduced in review).
        """
        if event.widget is self and event.chain >= 2:
            # Textual runs the handler of EVERY class in the MRO; without this
            # the base class still selects the whole widget after ours ran.
            event.prevent_default()
            if event.chain == 2:
                self._select_word(event.x, event.y)
            await self.broker_event("click", event)
            return
        await super()._on_click(event)

    def _select_word(self, x: int, y: int) -> None:
        text = self._row_text(y)
        # Cell → character index, as the compositor does for a drag.
        index, cells = 0, 0
        while index < len(text) and cells + cell_len(text[index]) <= x:
            cells += cell_len(text[index])
            index += 1
        if index >= len(text) or text[index].isspace():
            return
        start = index
        while start > 0 and not text[start - 1].isspace():
            start -= 1
        end = index
        while end < len(text) and not text[end].isspace():
            end += 1
        self.screen.selections = {self: Selection(Offset(start, y), Offset(end, y))}
        self._copy_selection()

    # --- selection and copy ------------------------------------------------------------

    def on_mouse_down(self, event: events.MouseDown) -> None:
        """Remember where a left-button drag began, so its release can copy."""
        self._drag_from = event.offset if event.button == 1 else None

    def on_mouse_up(self, event: events.MouseUp) -> None:
        """Copy on release — the drag itself is the request to copy.

        Only the release of the drag that MADE the selection copies. Copying on
        any release while a selection stood meant a triple click, or a right
        click over a highlight left by a double click, copied it a second time
        and raised a "copied N characters" toast for a gesture that selected
        nothing new (review). A click that moves nothing has its selection
        cleared by Textual before this runs.
        """
        start, self._drag_from = self._drag_from, None
        if start is None or event.offset == start:
            return
        if self.text_selection is not None:
            self._copy_selection()

    def _copy_selection(self) -> bool:
        """Copy the selected text to the clipboard (OSC 52); False when nothing is selected."""
        text = self.selected_text()
        if text is None:
            return False
        self.app.copy_to_clipboard(text)
        count = len(text)
        self.notify(
            f"copied {count} character{'s' if count != 1 else ''} — ctrl+c copies again "
            "while the selection stands",
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
        """Queue ``count`` wheel notches for the program at pane cell ``(x, y)``."""
        facts = self.facts
        if facts is None:
            return
        # The pane may be taller than the widget between a Resize and its
        # debounced resize-window: the widget shows the pane's LAST rows, so a
        # widget row maps to a pane row that many lines further down.
        offset = max(0, facts.height - self.content_size.height)
        self._wheel_queue.extend([(up, x, y + offset)] * count)
        if self._wheel_timer is None:
            # One tmux client per FLUSH, not per notch: a trackpad flick is 20-50
            # notches a second, each of which was its own fork+exec.
            self._wheel_timer = self.set_timer(self.WHEEL_COALESCE, self._flush_wheel, name="wheel")

    def _flush_wheel(self) -> None:
        """Send every notch queued since the last flush as one tmux call."""
        self._wheel_timer = None
        queue, self._wheel_queue = self._wheel_queue, []
        facts = self.facts
        if not queue or self.pane_id is None or self.server is None or facts is None:
            return
        try:
            if facts.mouse_sgr:
                self.server.send_literal(
                    self.pane_id,
                    "".join(f"\x1b[<{64 if up else 65};{x};{y}M" for up, x, y in queue),
                )
            else:
                # X10: three bytes after ESC [ M, each 32 + value, one byte each —
                # so a cell past 223 cannot be expressed and is clamped.
                payload = b"".join(
                    b"\x1b[M" + bytes([32 + (64 if up else 65), 32 + min(x, 223), 32 + min(y, 223)])
                    for up, x, y in queue
                )
                self.server.send_bytes(self.pane_id, payload)
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
