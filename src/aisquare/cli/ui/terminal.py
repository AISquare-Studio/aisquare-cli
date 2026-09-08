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

from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import events
from textual.geometry import Region
from textual.message import Message
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
NOTICE = Style(dim=True, italic=True)

NO_PANE = "(no agent selected)"
PANE_GONE = "(pane gone)"
TMUX_UNAVAILABLE = "(tmux unavailable)"


class EscapeToSidebar(Message):
    """The user pressed the escape hatch: focus goes back to the sidebar."""


class TerminalPane(Widget, can_focus=True):
    """One tmux pane, live. ``attach(pane_id)`` switches what it shows."""

    DEFAULT_CSS = """
    TerminalPane { height: 1fr; width: 1fr; }
    """

    #: Textual's built-in text selection works over ``render()`` output, which a
    #: Line API widget does not have; drag-select lands with its own
    #: ``get_selection`` (§4.3) rather than half-working now.
    ALLOW_SELECT: ClassVar[bool] = False

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

    # --- what is shown -----------------------------------------------------------------

    @property
    def attached(self) -> bool:
        return self.pane_id is not None

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
        if self._cursor is not None and self._cursor[1] == y and self._cursor[0] < width:
            strip = self._with_cursor(strip, self._cursor[0])
        return strip

    def _strip_for(self, line: str) -> Strip:
        strip = self._strip_cache.get(line)
        if strip is None:
            if len(self._strip_cache) >= self.CACHE_LIMIT:
                self._strip_cache.clear()
            text = Text.from_ansi(line, end="")
            strip = Strip(text.render(self.app.console)).simplify()
            self._strip_cache[line] = strip
        return strip

    def _with_cursor(self, strip: Strip, x: int) -> Strip:
        style = CURSOR if self.has_focus else UNFOCUSED_CURSOR
        strip = strip.extend_cell_length(x + 1, self.rich_style)
        return Strip.join(
            [strip.crop(0, x), strip.crop(x, x + 1).apply_style(style), strip.crop(x + 1)]
        )

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

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        if self.attached:
            event.stop()
            event.prevent_default()
            self.scroll_history(self.WHEEL_LINES)

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        if self.attached:
            event.stop()
            event.prevent_default()
            self.scroll_history(-self.WHEEL_LINES)

    def scroll_history(self, delta: int) -> None:
        """Move the view ``delta`` lines up (positive) into history, clamped; 0 is live."""
        history = self.facts.history_size if self.facts is not None else 0
        target = max(0, min(history, self.scrollback + delta))
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
