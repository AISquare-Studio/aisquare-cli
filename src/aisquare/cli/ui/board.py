"""The Board: the widgets of ``aisquare board -w``, as one panel any view can host.

``BoardPanel`` is the sessions block, the task table, the live feed and the
detail pane of the board TUI, with the behaviours they had as ``BoardApp``:
click or Enter on a task or a feed line shows its full content below; ``d``
flips the table to the done archive; ``o`` opens the author session's
transcript at the selected moment; ``v`` freezes the feed into selectable text;
``a`` toggles autoscroll; ``b`` hides the board column; ``r`` refreshes now.
Every line between two parts is a ``Divider``: drag it to resize, double-click
it to put the stylesheet's size back; each size is remembered in ``state.json``
under one key for every project's board.

Lifted out of ``cli.watch`` (docs/plans/fleet-tui.md §4.2, §9 Phase 6) so the
fleet UI's Project view can show a project's board as one tab while
``aisquare board -w`` keeps composing the same widget under its own footer and
theme picker. The pure renderers — ``feed_line``, ``_session_lines``, the
detail texts, the transcript helpers — stay in ``cli.watch``: the Rich fallback
there needs them without Textual, and other modules import them from there.

All data comes from ``services.team``; nothing here writes. The panel resolves
its project ONCE (or takes it from the caller) and passes it to every board
read, so a tick never costs a ``git rev-parse`` and never touches ``os.environ``.

Two bounds, both because the fleet UI keeps every view it builds (one
``ProjectView`` per project ever selected, cli/ui/app.py): a tick behind a
hidden tab is SKIPPED and made up in ``on_show``, the way ``TerminalPane``
skips frames for a pane that is not on screen; and the feed keeps its last
:attr:`BoardPanel.FEED_LIMIT` lines, evicting the oldest from the OptionList
and the event cache together. Without them, N visited projects cost N store
reads every ``interval`` seconds forever, and each panel's feed grows for as
long as the session lives.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from rich.cells import cell_len
from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.geometry import Size
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import DataTable, OptionList, Static
from textual.widgets.option_list import Option, OptionDoesNotExist
from textual.worker import Worker, WorkerState

from aisquare.cli import watch
from aisquare.cli.common import local_time
from aisquare.cli.ui.divider import Divider, floor_of
from aisquare.cli.ui.ticks import while_running
from aisquare.core.store import unmet_needs
from aisquare.models import CLOSED_STATUSES, ProjectInfo, TeamEvent, TeamSession, TeamTask
from aisquare.services import claude_accounts as accounts_service
from aisquare.services import team as team_service

OPEN_STATUSES = ("todo", "doing", "review", "blocked")
LABELS_TTL = 30.0
"""How long the sessions block keeps the slot labels before asking the registry again."""
LABELS_WORKER = "board-account-labels"
COLLAPSE_BELOW = 80
"""Panel width (columns) under which the board column hides and the feed takes all:
the board column is 48 wide and a feed narrower than ~32 shows nothing readable.
Measured against the PANEL, not the terminal — inside the fleet UI a 30-column
sidebar sits beside it, and at 92 a 120-column terminal never showed the tasks."""
TRANSCRIPT_HINT = "\npress o to open the transcript at this moment"
COLUMN_WIDTH_KEY = "board_column_width"
"""The ``state.json`` key of the board column's width (the line beside the live feed)."""
TEAM_HEIGHT_KEY = "board_team_height"
"""The ``state.json`` key of the team block's height (the line above the tasks)."""
DETAIL_HEIGHT_KEY = "board_detail_height"
"""The ``state.json`` key of the detail pane's height (the line above it)."""

WAITING = "waiting"
"""Not a status: a ``todo`` whose ``needs`` are not all done yet, which gets its own icon."""
STATUS_ICONS: dict[str, tuple[str, str, str]] = {
    "todo": ("○", "bold", "to do — nobody has it yet"),
    WAITING: ("⧗", "yellow", "to do, waiting on a task it needs"),
    "doing": ("▶", "green", "doing — claimed and being worked on"),
    "review": ("👀", "cyan", "in review — waiting to be checked"),
    "blocked": ("⛔", "red", "blocked — the detail says on what"),
    "done": ("✓", "green", "done"),
    "dropped": ("✗", "dim", "dropped — no longer worth doing"),
}
"""status → (glyph, style, what it means): the tasks table's one icon per state, and the
legend the help screens print from it. Each glyph is one or two cells wide."""
ICON_CELLS = 2
"""The status column's width: the widest glyph, so every row's title starts at one column."""
TASK_COLUMNS = ("#", "st", "title")
"""The tasks table: the number on the board, the status icon, the title."""
BOARD_KEYS: tuple[tuple[str, str], ...] = (
    ("click Enter", "a task or feed line: its full content below"),
    ("d", "the done archive · back to the open tasks"),
    ("o", "the author's transcript at that moment"),
    ("v  c", "freeze the feed to select text · copy it"),
    ("a", "feed autoscroll on or off"),
    ("b", "hide or show the tasks column"),
    ("r", "refresh now"),
    ("lines", "drag a line to resize; double-click resets it"),
)
"""The board's own keys, as the help screens list them."""


def status_icon(status: str, *, waiting: bool = False) -> Text:
    """The status as its icon, coloured, padded to :data:`ICON_CELLS`."""
    glyph, style, _meaning = STATUS_ICONS.get(WAITING if waiting else status, ("?", "dim", ""))
    return Text(glyph + " " * max(0, ICON_CELLS - cell_len(glyph)), style=style)


def board_help() -> Text:
    """The board's keys and the icon legend, for a help screen."""
    text = Text()
    for key, what in BOARD_KEYS:
        text.append(f"  {key:<11}", style="bold cyan")
        text.append(f" {what}\n")
    text.append("\n  task icons\n", style="bold")
    for state, (glyph, style, meaning) in STATUS_ICONS.items():
        text.append("  ")
        text.append(glyph + " " * max(0, ICON_CELLS - cell_len(glyph)), style=style)
        text.append(f"  {meaning}", style="dim" if state == "dropped" else "")
        text.append("\n")
    return text


class PickableTable(DataTable[Text]):
    """A DataTable that reports real mouse picks on data rows.

    ``DataTable._on_click`` stops the event inside the widget for data cells
    (textual 8.2.8, ``_data_table.py`` ``_on_click``), so a handler on an
    ancestor never sees a row click — while the header/blank clicks that DO
    bubble aren't row picks at all. The only place that sees exactly the right
    events is the widget's own click path: post ``RowPicked`` keyed off the
    click's own ``style.meta`` (``row >= 0`` — headers are ``row == -1`` and
    blank space carries no meta; ``out_of_bounds`` clicks past the last column
    stay accepted for a row cursor, exactly as the widget itself accepts them).

    Deliberately NO ``super()._on_click`` call: textual dispatches ``_on_click``
    once per class in the MRO (``message_pump._get_dispatch_methods`` walks
    ``__mro__``), so ``DataTable._on_click`` runs after this handler anyway.
    Calling it explicitly would run it twice per click — a double cursor move
    plus a spurious ``RowSelected`` from the second pass, which finds the cursor
    already on the clicked cell. Reading the row from the event meta (not the
    cursor) keeps this correct in either dispatch order.

    Rebuilds never pass through here, so a rebuild can never touch the detail
    pane; ``RowSelected`` stays for Enter.
    """

    class RowPicked(Message):
        def __init__(self, row_key: Any) -> None:
            self.row_key = row_key
            super().__init__()

    class Resized(Message):
        """The table's width changed (a terminal, a dragged line): titles refit to it."""

    def on_resize(self, event: events.Resize) -> None:
        self.post_message(self.Resized())

    async def _on_click(self, event: events.Click) -> None:
        meta = event.style.meta
        row = meta.get("row", -1)
        if row < 0:  # header click, or blank space with no row meta
            return
        if self.cursor_type != "row" and meta.get("out_of_bounds", False):
            return  # mirror DataTable's own out-of-bounds guard
        try:
            key = self.coordinate_to_cell_key(Coordinate(row, 0)).row_key
        except Exception:  # click raced a rebuild; the row is gone
            return
        self.post_message(self.RowPicked(key))


class TeamBlock(Static):
    """The team block: as tall as its lines, up to :attr:`AUTO_ROWS` — until a drag says otherwise.

    The cap was the stylesheet's ``max-height: 10``; the column now writes the
    block's ``max-height`` as the room the tasks leave it (:class:`BoardColumn`),
    so a dragged height may go past ten rows. Capping the AUTO height here keeps
    the undragged block what it was — Textual asks for the content height only
    when the height is ``auto`` — and a height the divider set is the user's.
    """

    AUTO_ROWS: ClassVar[int] = 8
    """Content rows an undragged block shows: ten rows less its border."""

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        return min(super().get_content_height(container, viewport, width), self.AUTO_ROWS)


def _number_cell(task: TeamTask) -> Text:
    """The task's number on its board; its id's tail, dimmed, for a task that has none."""
    if task.number is not None:
        return Text(str(task.number))
    return Text(task.id[-8:], style="dim")


class BoardHelp(ModalScreen[None]):
    """``aisquare board -w``'s keys and the task icons; Esc, q or ? closes.

    The fleet UI's own help screen prints the same :func:`board_help` under its
    "Board" section; ``extra`` is the hosting app's keys (a theme picker, quit).
    """

    CSS = """
    BoardHelp { align: center middle; }
    #boardhelpbox { width: 72; height: auto; max-height: 90%; border: heavy $accent;
                    background: $surface; padding: 1 2; }
    #boardhelpbox Static { height: auto; }
    """
    BINDINGS: ClassVar = [
        ("escape", "close_help", "close"),
        ("q", "close_help", "close"),
        ("question_mark", "close_help", "close"),
    ]

    def __init__(self, extra: tuple[tuple[str, str], ...] = ()) -> None:
        super().__init__()
        self._extra = extra

    def compose(self) -> ComposeResult:
        text = Text("aisquare board — keys\n\n", style="bold")
        text.append_text(board_help())
        if self._extra:
            text.append("\n")
            for key, what in self._extra:
                text.append(f"  {key:<11}", style="bold cyan")
                text.append(f" {what}\n")
        text.append("\nEsc closes this", style="dim")
        with VerticalScroll(id="boardhelpbox"):
            yield Static(text)

    def action_close_help(self) -> None:
        self.dismiss(None)


class BoardColumn(Vertical):
    """The board column: the team block over the tasks, and the line between them.

    The column keeps the team block's ceiling — its height less the line and
    the rows the tasks must keep — as the block's ``max-height`` whenever its
    own height changes: the panel resizing, or the detail pane's line moving.
    Textual then clamps the block on every layout pass, as ``app.Panes`` does
    for the navigator.
    """

    def on_resize(self, event: events.Resize) -> None:
        if event.size.height <= 0:
            return  # behind a hidden tab: nothing to fit, and the next show resizes
        sessions = self.query_children("#sessions").first()
        tasks = self.query_children("#tasks").first()
        floor = floor_of(sessions, "height")
        sessions.styles.max_height = max(floor, event.size.height - 1 - floor_of(tasks, "height"))


class BoardPanel(Vertical):
    """Sessions, tasks, the live feed and a detail pane for one project's board.

    ``project`` may be ``None``: the board then resolves from the process cwd
    on every read, which is what ``board -w`` does when its own resolution
    failed at start. ``interval`` is the refresh period in seconds.

    Three lines resize it, each a :class:`Divider` that finds its target among
    its own siblings — the fleet UI mounts a board per project at once, and the
    ids repeat: the column beside the feed, the detail pane below everything,
    and the team block over the tasks (:class:`BoardColumn`). The panel keeps
    the first two ceilings current from its own size, so the feed and the
    board above the detail pane keep their stylesheet minimums whatever the
    drag or the terminal does.
    """

    DEFAULT_CSS = """
    BoardPanel { height: 1fr; }
    BoardPanel #board-main { height: 1fr; min-height: 8; }
    BoardPanel #board-side { width: 48; min-width: 32; }
    BoardPanel #board-side.collapsed { display: none; }
    BoardPanel #side-divider.collapsed { display: none; }
    BoardPanel #sessions { height: auto; min-height: 3; border: round $primary;
                           padding: 0 1; }
    BoardPanel #tasks { height: 1fr; min-height: 4; border: round $primary; }
    BoardPanel #feedpane { width: 1fr; min-width: 32; }
    BoardPanel #feed { height: 1fr; border: round $secondary; }
    BoardPanel #feed.hidden { display: none; }
    BoardPanel #feedtext { height: 1fr; border: round $success; display: none;
                           padding: 0 1; }
    BoardPanel #feedtext.active { display: block; }
    BoardPanel #detailwrap { height: 8; min-height: 3; border: heavy $warning;
                             padding: 0 1; }
    """
    BINDINGS: ClassVar = [
        ("b", "toggle_board", "board on/off"),
        ("r", "refresh_now", "refresh"),
        ("a", "toggle_autoscroll", "autoscroll"),
        ("v", "toggle_select", "select text"),
        ("c", "copy_selection", "copy"),
        ("d", "toggle_done", "done archive"),
        ("o", "open_transcript", "transcript"),
    ]

    FEED_LIMIT: ClassVar[int] = 2000
    """Feed lines kept before the oldest are dropped.

    The panel outlives every tab switch, so the feed is the one structure here
    that only grows: ``_events_by_id``, ``_feed_order`` and the OptionList all
    gain a row per event for the life of the app. 2000 lines is ~5x the first
    fetch (``board_read(events=400)``) — deep enough that scrolling back has
    somewhere to go, bounded enough that a week-long session cannot swell.
    """

    class Refreshed(Message):
        """A refresh landed; ``project`` is the board it read."""

        def __init__(self, project: ProjectInfo) -> None:
            self.project = project
            super().__init__()

    def __init__(
        self, project: ProjectInfo | None, *, interval: float = 2.0, id: str | None = None
    ) -> None:
        super().__init__(id=id)
        self.project = project
        self.interval = interval
        self._events_by_id: dict[str, TeamEvent] = {}
        self._feed_order: list[str] = []
        self._tasks_by_id: dict[str, TeamTask] = {}
        self._roles: dict[str, str] = {}
        self._statuses: dict[str, str] = {}
        self._last_seq = 0
        self._terminal_by_task: dict[str, TeamEvent] = {}
        self._terminal_fetched = False
        self._prev_states: dict[str, str] = {}
        self._select_mode = False
        self._show_done = False
        self._sessions: dict[str, TeamSession] = {}
        self._switches: dict[str, str] = {}
        """The ``fleet switch`` of each limited session the last refresh read, by its id."""
        self._labels: Mapping[int, str] = {}
        self._labels_asked_at: float | None = None
        self._all_tasks: list[TeamTask] = []
        self.autoscroll = True
        self.detail_text = ""
        """The plain text of the detail pane (what a test reads)."""
        self.detail_moment: tuple[str | None, datetime] | None = None
        """(author session id, timestamp) of the selected item, for the transcript jump."""

    def compose(self) -> ComposeResult:
        with Horizontal(id="board-main"):
            with BoardColumn(id="board-side"):
                yield TeamBlock(id="sessions")
                yield Divider(
                    "#sessions",
                    orientation="horizontal",
                    state_key=TEAM_HEIGHT_KEY,
                    what="the board's team height",
                    id="team-divider",
                )
                yield PickableTable(id="tasks", cursor_type="row")
            yield Divider(
                "#board-side",
                state_key=COLUMN_WIDTH_KEY,
                what="the board column's width",
                id="side-divider",
            )
            with Vertical(id="feedpane"):
                yield OptionList(id="feed")
                with VerticalScroll(id="feedtext"):
                    yield Static(id="feedstatic")
        yield Divider(
            "#detailwrap",
            orientation="horizontal",
            side="after",
            state_key=DETAIL_HEIGHT_KEY,
            what="the board's detail height",
            id="detail-divider",
        )
        with VerticalScroll(id="detailwrap"):
            yield Static(id="detail")

    def on_mount(self) -> None:
        table = self.query_one("#tasks", DataTable)
        table.add_columns(*TASK_COLUMNS)
        table.border_title = "tasks"
        self.query_one("#sessions", Static).border_title = "team"
        self.query_one("#feed", OptionList).border_title = "live feed"
        self.query_one(
            "#detailwrap", VerticalScroll
        ).border_title = "detail — click a task or feed line"
        self.refresh_data()
        self.set_interval(self.interval, self._tick)

    @while_running
    def _tick(self) -> None:
        """The polled read — skipped while the panel is off screen.

        The fleet UI keeps a ``ProjectView`` per project ever selected, so an
        ungated timer means N store reads every ``interval`` seconds for the
        life of the app, N-1 of them behind ``display: none``. What skipping
        costs: a hidden panel's data is stale, so :meth:`on_show` re-reads the
        moment the tab comes back (as ``TerminalPane`` does for its frames).
        """
        if self.is_on_screen:
            self.refresh_data()

    def on_show(self) -> None:
        """A hidden tab came back: catch up on what the skipped ticks missed."""
        self.refresh_data()

    # --- data ---------------------------------------------------------------------

    def refresh_data(self) -> None:
        """Re-read the board; keep the last frame when the store is briefly busy."""
        try:
            project, sessions, tasks, events, switches = team_service.board_read(
                events=400 if self._last_seq == 0 else 200,
                since_seq=self._last_seq or None,
                project=self.project,
            )
        except team_service.TeamDisabledError:
            self.query_one("#sessions", Static).update(
                Text("orchestrator disabled (AISQUARE_TEAM=0)", style="bold red")
            )
            return
        except Exception:  # store briefly unavailable — keep the last frame
            return
        self._roles = {s.id: s.role for s in sessions}
        self._sessions = {s.id: s for s in sessions}
        self._statuses = {t.id: t.status for t in tasks}
        self._all_tasks = tasks
        self._refresh_labels(sessions)
        self._switches = switches
        self.query_one("#sessions", Static).update(
            watch._session_lines(sessions, self._labels, switches=self._switches)
        )
        self._ring_on_attention(sessions)
        # Events first: a task_done/dropped this tick flushes the attribution
        # cache BEFORE the archive rebuilds, so the closer is never one frame stale.
        self._append_events(events)
        self._refresh_tasks(tasks)
        self.post_message(self.Refreshed(project))

    def _refresh_labels(self, sessions: list[TeamSession]) -> None:
        """Ask the registry for the slot labels OFF the UI thread, at most every LABELS_TTL s.

        ``watch._session_lines`` used to read them itself — a ``context.db``
        open with a busy timeout of seconds, a scan of the account directories
        and, on a registry not yet reconciled, its writes — on the event loop,
        at every tick of every open board (review of #205, fourth round). The
        block paints the last good map (the built-in names before the first
        answer) and repaints when the worker answers, as the agent header does
        (``views.agent``). Asked only once several accounts are in play, the
        only time the block shows one.
        """
        if len({s.account for s in sessions if s.ended_at is None and s.account}) < 2:
            return
        now = time.monotonic()
        if self._labels_asked_at is not None and now - self._labels_asked_at < LABELS_TTL:
            return
        self._labels_asked_at = now
        self.run_worker(
            accounts_service.slot_labels,
            name=LABELS_WORKER,
            group=LABELS_WORKER,
            exclusive=True,
            thread=True,
            exit_on_error=False,
        )

    @on(Worker.StateChanged)
    def _labels_answered(self, event: Worker.StateChanged) -> None:
        if event.worker.group != LABELS_WORKER:
            return
        if event.state is WorkerState.SUCCESS and isinstance(event.worker.result, dict):
            self._labels = event.worker.result
            if self.is_mounted:
                self.query_one("#sessions", Static).update(
                    watch._session_lines(
                        list(self._sessions.values()), self._labels, switches=self._switches
                    )
                )

    def _ring_on_attention(self, sessions: list[TeamSession]) -> None:
        """Terminal bell when a session newly flips to needing the user."""
        states = {s.id: s.state for s in sessions if s.ended_at is None}
        for session_id, state in states.items():
            previous = self._prev_states.get(session_id)
            if state == "attention" and previous not in (None, "attention"):
                self.app.bell()
                break
        self._prev_states = states

    def _done_event_for(self, task_id: str) -> TeamEvent | None:
        """The latest done/dropped event for a task (who closed it, when).

        Resolved from the store, not the bounded feed cache — a task closed
        thousands of events ago still knows who closed it. The cache is
        flushed in ``_append_events`` whenever a new terminal event arrives, so
        a reopen-and-reclose re-attributes correctly. Fetched once per flush,
        not per row: the populated dict is the cache, so a task with NO
        terminal event returns ``None`` from ``.get()`` without re-running the
        store query every tick (negative caching).
        """
        if not self._terminal_fetched:
            try:
                self._terminal_by_task = team_service.terminal_attribution(project=self.project)
            except Exception:
                return None
            self._terminal_fetched = True
        return self._terminal_by_task.get(task_id)

    def _refresh_tasks(self, tasks: list[TeamTask]) -> None:
        """Rebuild the table: the task's number, its status as an icon, and its title.

        Who has it, its full id and the rest are one click away in the detail pane.
        The title is cut, with an ellipsis, to the room the table has, so it takes
        the rest of the width and the table never scrolls sideways; a resize
        rebuilds (``PickableTable.Resized``).
        """
        table = self.query_one("#tasks", DataTable)
        selected = self._cursor_task_id(table)
        # The columns too: a column keeps the widest cell it ever held, so a title cut
        # to a narrower table still scrolled sideways to the width of the uncut one.
        table.clear(columns=True)
        table.add_columns(*TASK_COLUMNS)
        self._tasks_by_id = {}
        rows: list[tuple[TeamTask, Text, Text]] = []
        if self._show_done:
            closed = [t for t in tasks if t.status in CLOSED_STATUSES]
            closed.sort(key=lambda t: t.updated_at, reverse=True)
            table.border_title = f"done archive ({len(closed)}) — d for open tasks"
            for task in closed:
                when = f"{local_time(task.updated_at):%H:%M}"
                rows.append((task, _number_cell(task), status_icon(task.status) + f" {when}"))
        else:
            table.border_title = "tasks — d for done archive"
            for task in (t for t in tasks if t.status in OPEN_STATUSES):
                waiting = task.status == "todo" and bool(unmet_needs(task, self._statuses))
                rows.append((task, _number_cell(task), status_icon(task.status, waiting=waiting)))
        room = self._title_room(table, rows)
        for task, number, icon in rows:
            self._tasks_by_id[task.id] = task
            title = Text(task.title, no_wrap=True, overflow="ellipsis")
            if room is not None:
                title.truncate(room, overflow="ellipsis")
            table.add_row(number, icon, title, key=task.id)
        if selected in self._tasks_by_id:
            table.move_cursor(row=table.get_row_index(selected))

    @staticmethod
    def _title_room(table: DataTable[Any], rows: list[tuple[TeamTask, Text, Text]]) -> int | None:
        """The cells the title column may take: what the table has less the other two
        columns and every column's padding — ``None`` before the table has a size."""
        width = table.scrollable_content_region.width
        if width <= 0:
            return None
        pad = 2 * table.cell_padding
        number = max([1, *(row[1].cell_len for row in rows)]) + pad
        status = max([ICON_CELLS, *(row[2].cell_len for row in rows)]) + pad
        return max(8, width - number - status - pad)

    def on_pickable_table_resized(self, event: PickableTable.Resized) -> None:
        self._refresh_tasks(self._all_tasks)

    def _cursor_task_id(self, table: DataTable[Any]) -> str | None:
        try:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
            return str(key) if key is not None else None
        except Exception:
            return None

    def _append_events(self, events: list[TeamEvent]) -> None:
        if self._select_mode:
            return  # frozen while selecting; the backlog lands on exit
        feed = self.query_one("#feed", OptionList)
        fresh = [e for e in events if e.seq > self._last_seq]
        if not fresh:
            return
        for event in fresh:
            self._events_by_id[event.id] = event
            self._feed_order.append(event.id)
            feed.add_option(Option(watch.feed_line(event, self._roles), id=event.id))
            self._last_seq = max(self._last_seq, event.seq)
            if event.kind in ("task_done", "task_dropped"):
                # A close happened — attribution (positive AND negative entries)
                # is stale; a reopen-and-reclose must re-attribute.
                self._terminal_by_task = {}
                self._terminal_fetched = False
        self._evict_oldest(feed, len(self._feed_order) - self.FEED_LIMIT)
        if self.autoscroll:
            feed.scroll_end(animate=False)

    def _evict_oldest(self, feed: OptionList, count: int) -> None:
        """Drop the oldest ``count`` feed lines from the list AND the caches.

        All three together: ``_feed_order`` is what select mode replays through
        ``_events_by_id``, so an id may never outlive its event, and the option
        holds the row on screen. A close attribution does NOT come from here
        (``_done_event_for`` asks the store), so an evicted line costs only
        history the user can no longer scroll to.
        """
        if count <= 0:
            return
        for event_id in self._feed_order[:count]:
            self._events_by_id.pop(event_id, None)
            with contextlib.suppress(OptionDoesNotExist):  # already gone
                feed.remove_option(event_id)
        del self._feed_order[:count]

    # --- the detail pane -----------------------------------------------------------

    def _show_detail(self, content: Text) -> None:
        self.detail_text = content.plain
        self.query_one("#detail", Static).update(content)
        self.query_one("#detailwrap", VerticalScroll).scroll_home(animate=False)

    def _moment_transcript(self) -> tuple[Path, datetime] | None:
        """(transcript path, timestamp) for the selected item, if known."""
        if self.detail_moment is None:
            return None
        session_id, when = self.detail_moment
        session = self._sessions.get(session_id or "")
        if session is None or not session.transcript_path:
            return None
        transcript = Path(session.transcript_path)
        if not transcript.exists():
            return None
        return transcript, when

    def _show_task_detail(self, key: str | None) -> None:
        if key is None:
            return
        task = self._tasks_by_id.get(key)
        if task is None:
            return
        detail = watch._task_detail(task, self._statuses)
        done_event = self._done_event_for(task.id) if task.status in CLOSED_STATUSES else None
        if done_event is not None:
            who = team_service.short_id(done_event.session_id or "cli")
            verb = "done" if done_event.kind == "task_done" else "dropped"
            detail.append(
                f"\n{verb} by {who} at {local_time(done_event.created_at):%H:%M:%S}",
                style="green" if verb == "done" else "yellow",
            )
            self.detail_moment = (done_event.session_id, done_event.created_at)
        else:
            self.detail_moment = (task.claimed_by, task.updated_at)
        if self._moment_transcript() is not None:
            detail.append(TRANSCRIPT_HINT, style="dim")
        self._show_detail(detail)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # Enter (and click-when-cursor-already-there) post RowSelected. A
        # rebuild's clear/add_row/move_cursor posts RowHighlighted but never
        # RowSelected, so no artifact can touch the detail pane.
        key = event.row_key.value if event.row_key else None
        self._show_task_detail(str(key) if key is not None else None)

    def on_pickable_table_row_picked(self, event: PickableTable.RowPicked) -> None:
        # A real mouse click on a data row (posted from inside the widget's click
        # path, where DataTable stops the event before it can bubble).
        # Header/blank clicks never post this, so they can't clobber a selected
        # feed event — and rebuilds can't reach it at all.
        key = event.row_key.value if event.row_key else None
        self._show_task_detail(str(key) if key is not None else None)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        # OptionSelected (click / Enter) only, mirroring the task table: an
        # append/scroll never posts it, so a feed selection is stable.
        if event.option is None or event.option.id is None:
            return
        stored = self._events_by_id.get(event.option.id)
        if stored is None:
            return
        self.detail_moment = (stored.session_id, stored.created_at)
        detail = watch._event_detail(stored, self._roles)
        if self._moment_transcript() is not None:
            detail.append(TRANSCRIPT_HINT, style="dim")
        self._show_detail(detail)

    # --- actions -------------------------------------------------------------------

    def action_toggle_done(self) -> None:
        self._show_done = not self._show_done
        self._refresh_tasks(self._all_tasks)

    def action_open_transcript(self) -> None:
        """Jump into the author session's transcript around the selected moment."""
        target = self._moment_transcript()
        if target is None:
            self.notify(
                "no transcript for this item (session predates transcript capture, "
                "or it was a CLI/remote action)",
                timeout=5,
                markup=False,
            )
            return
        transcript, when = target
        line = watch.transcript_line_near(transcript, when)
        error = watch.action_open_transcript(self.app, watch._transcript_command(transcript, line))
        if error is not None:
            self.notify(
                f"could not open transcript: {error}", severity="error", timeout=6, markup=False
            )

    def _feed_title(self) -> str:
        title = "live feed"
        if self._select_mode:
            return f"{title} — SELECT MODE (drag to select, c copies, v resumes)"
        if not self.autoscroll:
            title += " — autoscroll off"
        return title

    def action_toggle_autoscroll(self) -> None:
        self.autoscroll = not self.autoscroll
        feed = self.query_one("#feed", OptionList)
        feed.border_title = self._feed_title()
        if self.autoscroll:
            feed.scroll_end(animate=False)

    def action_toggle_select(self) -> None:
        """Swap the live feed for a frozen, mouse-selectable text view."""
        self._select_mode = not self._select_mode
        feed = self.query_one("#feed", OptionList)
        wrap = self.query_one("#feedtext", VerticalScroll)
        if self._select_mode:
            snapshot = Text()
            for event_id in self._feed_order:
                snapshot.append(watch.feed_line(self._events_by_id[event_id], self._roles))
                snapshot.append("\n")
            self.query_one("#feedstatic", Static).update(snapshot)
            feed.add_class("hidden")
            wrap.add_class("active")
            wrap.border_title = self._feed_title()
            wrap.scroll_end(animate=False)
        else:
            feed.remove_class("hidden")
            wrap.remove_class("active")
            feed.border_title = self._feed_title()
            self.refresh_data()  # apply the backlog accumulated while frozen
            if self.autoscroll:
                feed.scroll_end(animate=False)

    def action_copy_selection(self) -> None:
        """Copy the mouse-selected text (select mode) to the clipboard."""
        getter = getattr(self.screen, "get_selected_text", None)
        selected = getter() if callable(getter) else None
        if not selected:
            self.notify(
                "nothing selected — v enters select mode, then drag", timeout=4, markup=False
            )
            return
        self.app.copy_to_clipboard(selected)
        self.notify(f"copied {len(selected)} chars", timeout=3, markup=False)

    def action_toggle_board(self) -> None:
        """``b``: the board column off or on, and focus with it when it comes back."""
        shown = self.query_one("#board-side").has_class("collapsed")
        self._collapse(not shown)
        if shown:
            self.query_one("#tasks").focus()

    def action_refresh_now(self) -> None:
        self.refresh_data()

    def _collapse(self, collapsed: bool) -> None:
        """Hide (or show) the board column and the line that resizes it, together.

        Focus inside a column going out of sight moves to the feed beside it, as it is
        shown (the live list, or its frozen text in select mode). Left on the hidden
        tasks table it went to nothing, and with nothing focused no key reaches this
        panel: the next ``b`` could not bring the column back.
        """
        side = self.query_one("#board-side")
        side.set_class(collapsed, "collapsed")
        self.query_one("#side-divider").set_class(collapsed, "collapsed")
        focused = self.app.focused
        if collapsed and focused is not None and side in focused.ancestors_with_self:
            feed = self.query_one("#feed")
            (self.query_one("#feedtext") if feed.has_class("hidden") else feed).focus()

    def on_resize(self, event: events.Resize) -> None:
        self._collapse(event.size.width < COLLAPSE_BELOW)
        if event.size.width > 0 and event.size.height > 0:
            self._fit(event.size.width, event.size.height)

    def _fit(self, width: int, height: int) -> None:
        """Write the column's and the detail pane's ceilings from the panel's size.

        Each is the panel less its line and what the other side must keep —
        the feed's ``min-width``, the board's ``min-height`` above the detail
        pane — so Textual clamps the dragged part on every layout pass and a
        shrinking terminal re-clamps it with no handler. Too small for both
        minimums, the dragged part's floor wins.
        """
        side = self.query_one("#board-side")
        side.styles.max_width = max(
            floor_of(side), width - 1 - floor_of(self.query_one("#feedpane"))
        )
        detail = self.query_one("#detailwrap")
        detail.styles.max_height = max(
            floor_of(detail, "height"),
            height - 1 - floor_of(self.query_one("#board-main"), "height"),
        )
