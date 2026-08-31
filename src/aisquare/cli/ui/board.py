"""The Board: the widgets of ``aisquare board -w``, as one panel any view can host.

``BoardPanel`` is the sessions block, the task table, the live feed and the
detail pane of the board TUI, with the behaviours they had as ``BoardApp``:
click or Enter on a task or a feed line shows its full content below; ``d``
flips the table to the done archive; ``o`` opens the author session's
transcript at the selected moment; ``v`` freezes the feed into selectable text;
``a`` toggles autoscroll; ``b`` hides the board column; ``r`` refreshes now.

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
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.message import Message
from textual.widgets import DataTable, OptionList, Static
from textual.widgets.option_list import Option, OptionDoesNotExist

from aisquare.cli import watch
from aisquare.cli.common import local_time
from aisquare.core.store import unmet_needs
from aisquare.models import ProjectInfo, TeamEvent, TeamSession, TeamTask
from aisquare.services import team as team_service

OPEN_STATUSES = ("todo", "doing", "review", "blocked")
CLOSED_STATUSES = ("done", "dropped")
COLLAPSE_BELOW = 80
"""Panel width (columns) under which the board column hides and the feed takes all:
the board column is 48 wide and a feed narrower than ~32 shows nothing readable.
Measured against the PANEL, not the terminal — inside the fleet UI a 30-column
sidebar sits beside it, and at 92 a 120-column terminal never showed the tasks."""
TRANSCRIPT_HINT = "\npress o to open the transcript at this moment"


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


class BoardPanel(Vertical):
    """Sessions, tasks, the live feed and a detail pane for one project's board.

    ``project`` may be ``None``: the board then resolves from the process cwd
    on every read, which is what ``board -w`` does when its own resolution
    failed at start. ``interval`` is the refresh period in seconds.
    """

    DEFAULT_CSS = """
    BoardPanel { height: 1fr; }
    BoardPanel #board-main { height: 1fr; }
    BoardPanel #board-side { width: 48; min-width: 32; }
    BoardPanel #board-side.collapsed { display: none; }
    BoardPanel #sessions { height: auto; max-height: 10; border: round $primary;
                           padding: 0 1; }
    BoardPanel #tasks { height: 1fr; border: round $primary; }
    BoardPanel #feedpane { width: 1fr; }
    BoardPanel #feed { height: 1fr; border: round $secondary; }
    BoardPanel #feed.hidden { display: none; }
    BoardPanel #feedtext { height: 1fr; border: round $success; display: none;
                           padding: 0 1; }
    BoardPanel #feedtext.active { display: block; }
    BoardPanel #detailwrap { height: 8; border: heavy $warning; padding: 0 1; }
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
    fetch (``board_data(events=400)``) — deep enough that scrolling back has
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
        self._all_tasks: list[TeamTask] = []
        self.autoscroll = True
        self.detail_text = ""
        """The plain text of the detail pane (what a test reads)."""
        self.detail_moment: tuple[str | None, datetime] | None = None
        """(author session id, timestamp) of the selected item, for the transcript jump."""

    def compose(self) -> ComposeResult:
        with Horizontal(id="board-main"):
            with Vertical(id="board-side"):
                yield Static(id="sessions")
                yield PickableTable(id="tasks", cursor_type="row")
            with Vertical(id="feedpane"):
                yield OptionList(id="feed")
                with VerticalScroll(id="feedtext"):
                    yield Static(id="feedstatic")
        with VerticalScroll(id="detailwrap"):
            yield Static(id="detail")

    def on_mount(self) -> None:
        table = self.query_one("#tasks", DataTable)
        table.add_columns("id", "st", "who", "title")
        table.border_title = "tasks"
        self.query_one("#sessions", Static).border_title = "team"
        self.query_one("#feed", OptionList).border_title = "live feed"
        self.query_one(
            "#detailwrap", VerticalScroll
        ).border_title = "detail — click a task or feed line"
        self.refresh_data()
        self.set_interval(self.interval, self._tick)

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
            project, sessions, tasks, events = team_service.board_data(
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
        self.query_one("#sessions", Static).update(watch._session_lines(sessions))
        self._ring_on_attention(sessions)
        # Events first: a task_done/dropped this tick flushes the attribution
        # cache BEFORE the archive rebuilds, so the closer is never one frame stale.
        self._append_events(events)
        self._refresh_tasks(tasks)
        self.post_message(self.Refreshed(project))

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
        table = self.query_one("#tasks", DataTable)
        selected = self._cursor_task_id(table)
        table.clear()
        self._tasks_by_id = {}
        if self._show_done:
            closed = [t for t in tasks if t.status in CLOSED_STATUSES]
            closed.sort(key=lambda t: t.updated_at, reverse=True)
            table.border_title = f"done archive ({len(closed)}) — d for open tasks"
            for task in closed:
                self._tasks_by_id[task.id] = task
                event = self._done_event_for(task.id)
                who = team_service.short_id(event.session_id) if event and event.session_id else ""
                when = f"{local_time(task.updated_at):%H:%M}"
                table.add_row(
                    Text(task.id[-8:], style="dim"),
                    Text(f"{'✅' if task.status == 'done' else '🗑'} {when}"),
                    Text(who),
                    Text(task.title, no_wrap=True, overflow="ellipsis"),
                    key=task.id,
                )
        else:
            table.border_title = "tasks — d for done archive"
            for task in (t for t in tasks if t.status in OPEN_STATUSES):
                self._tasks_by_id[task.id] = task
                marker = "⧗" if unmet_needs(task, self._statuses) else ""
                who = team_service.short_id(task.claimed_by) if task.claimed_by else ""
                table.add_row(
                    Text(task.id[-8:], style="dim"),
                    Text(f"{task.status}{marker}"),
                    Text(who),
                    Text(task.title, no_wrap=True, overflow="ellipsis"),
                    key=task.id,
                )
        if selected in self._tasks_by_id:
            table.move_cursor(row=table.get_row_index(selected))

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
            self.notify("nothing selected — v enters select mode, then drag", timeout=4)
            return
        self.app.copy_to_clipboard(selected)
        self.notify(f"copied {len(selected)} chars", timeout=3)

    def action_toggle_board(self) -> None:
        self.query_one("#board-side").toggle_class("collapsed")

    def action_refresh_now(self) -> None:
        self.refresh_data()

    def on_resize(self, event: events.Resize) -> None:
        side = self.query_one("#board-side")
        if event.size.width < COLLAPSE_BELOW:
            side.add_class("collapsed")
        else:
            side.remove_class("collapsed")
