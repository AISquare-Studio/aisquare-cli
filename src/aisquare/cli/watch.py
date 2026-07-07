"""The live board (``aisquare board --watch``).

Two implementations behind one entry point:

- **Interactive TUI** (needs the ``tui`` extra: ``pip install
  aisquare-cli[tui]``): board on the left (sessions + task table), a
  bot-style agent feed on the right, and a detail bar at the bottom —
  click/select any task or feed line to see its full, unclipped content.
  The feed scrolls; the board collapses on narrow terminals (or with ``b``).
- **Rich fallback** (no extra deps): a full-screen frame that refreshes in
  place and sizes the feed to the terminal height.

Only presentation lives here; all data comes from ``services.team``.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from rich.text import Text

from aisquare.cli.common import local_time
from aisquare.core import paths
from aisquare.core.console import stderr_console, stdout_console
from aisquare.core.store import unmet_needs
from aisquare.models import TeamEvent, TeamSession, TeamTask
from aisquare.services import team as team_service

_ROLE_EMOJI = {
    "planner": "🧭",
    "coder": "🔨",
    "runner": "🧪",
    "tester": "🧪",
    "debugger": "🐞",
    "remote": "📡",
}
_ROLE_STYLE = {
    "planner": "cyan",
    "coder": "magenta",
    "runner": "green",
    "tester": "green",
    "debugger": "yellow",
    "remote": "yellow",
}
_KIND_VERB = {
    "note": ("💬", "noted"),
    "decision": ("💡", "decided"),
    "question": ("❓", "asked"),
    "result": ("📊", "reported"),
    "join": ("👋", "joined the team"),
    "end": ("🚪", "left the team"),
    "focus": ("🎯", "is focusing on"),
    "activate": ("⚡", "activated the bus"),
    "task_added": ("📌", "added"),
    "task_claimed": ("🤝", "claimed"),
    "task_review": ("👀", "sent to review"),
    "task_done": ("✅", "finished"),
    "task_blocked": ("🧱", "hit a wall on"),
    "task_reopened": ("↩️", "bounced back"),
    "task_released": ("🔓", "released"),
    "task_dropped": ("🗑️", "dropped"),
}


def _who(event: TeamEvent, roles: dict[str, str]) -> tuple[str, str, str]:
    """(emoji, name, style) for the author of an event."""
    if event.session_id is None:
        return "⌨️", "cli", "dim"
    role = roles.get(event.session_id, "unassigned")
    name = f"{role}·{team_service.short_id(event.session_id)}"
    return _ROLE_EMOJI.get(role, "🤖"), name, _ROLE_STYLE.get(role, "white")


def feed_line(event: TeamEvent, roles: dict[str, str]) -> Text:
    """One bot-style feed line: ``🔨 coder·7188d074 🤝 claimed: wire auth``."""
    emoji, name, style = _who(event, roles)
    verb_emoji, verb = _KIND_VERB.get(event.kind, ("•", event.kind))
    line = Text(no_wrap=True, overflow="ellipsis")
    line.append(f"{local_time(event.created_at):%H:%M} ", style="dim")
    line.append(f"{emoji} {name} ", style=style)
    line.append(f"{verb_emoji} {verb}: ", style="bold")
    line.append(event.text)
    if event.to_role:
        line.append(f" → {event.to_role}", style="italic yellow")
    return line


def _event_detail(event: TeamEvent, roles: dict[str, str]) -> Text:
    _, name, style = _who(event, roles)
    text = Text()
    text.append(f"{event.kind}", style="bold")
    text.append(f" by {name}", style=style)
    text.append(f" at {local_time(event.created_at):%H:%M:%S} (seq {event.seq})\n", style="dim")
    text.append(event.text)
    if event.task_id:
        text.append(f"\ntask: {event.task_id}", style="dim")
    if event.to_role:
        text.append(f"\naddressed to: {event.to_role}", style="yellow")
    return text


def _task_detail(task: TeamTask, statuses: dict[str, str]) -> Text:
    text = Text()
    text.append(f"{task.title}\n", style="bold")
    text.append(f"{task.id} · [{task.status}] · key={task.key}", style="dim")
    if task.role:
        text.append(f" · for {task.role}", style="dim")
    if task.claimed_by:
        text.append(f"\nclaimed by {team_service.short_id(task.claimed_by)}")
        if task.claim_expires_at:
            text.append(f" (lease until {local_time(task.claim_expires_at):%H:%M})", style="dim")
    if task.needs:
        waiting = set(unmet_needs(task, statuses))
        text.append("\nneeds: ")
        for index, need in enumerate(task.needs):
            if index:
                text.append(", ")
            done = need not in waiting
            text.append(f"{need[-8:]} {'✓' if done else '⧗'}", style="green" if done else "red")
    if task.detail:
        text.append(f"\n{task.detail}")
    return text


_STATE_CHIP = {
    "working": ("▶ working", "green"),
    "waiting": ("⏸ waiting for input", "yellow"),
    "attention": ("🔔 NEEDS YOU", "bold red"),
}


def _session_lines(sessions: list[TeamSession]) -> Text:
    text = Text(no_wrap=True, overflow="ellipsis")
    live = [s for s in sessions if s.ended_at is None]
    if not live:
        text.append("(nobody here yet)", style="dim")
        return text
    now = datetime.now(tz=live[0].last_seen_at.tzinfo)
    for session in live:
        emoji = _ROLE_EMOJI.get(session.role, "🤖")
        style = _ROLE_STYLE.get(session.role, "white")
        minutes = max(0, int((now - session.last_seen_at).total_seconds() // 60))
        text.append(f"{emoji} {session.role}·{team_service.short_id(session.id)}", style=style)
        chip, chip_style = _STATE_CHIP.get(session.state, (session.state, "dim"))
        text.append(f"  {chip}", style=chip_style)
        text.append(f"  {minutes}m", style="dim")
        if minutes > 30:
            text.append("  (stale)", style="red dim")
        if session.focus:
            text.append(f"\n   🎯 {session.focus}", style="italic")
        text.append("\n")
    return text


def transcript_line_near(path: Path, when: datetime) -> int:
    """The 1-based line of a Claude Code transcript nearest ``when``.

    Transcript JSONL lines carry ISO ``"timestamp"`` fields; returns the first
    line at/after the moment (so opening there shows the surrounding turn),
    or the last line when the moment is past the end. Never raises.
    """
    import re

    pattern = re.compile(r'"timestamp"\s*:\s*"([^"]+)"')
    best = 1
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for number, line in enumerate(handle, start=1):
                best = number
                match = pattern.search(line)
                if match is None:
                    continue
                try:
                    stamp = datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
                except ValueError:
                    continue
                if when.tzinfo is not None and stamp.tzinfo is not None and stamp >= when:
                    return number
    except OSError:
        return 1
    return best


def _transcript_command(path: Path, line: int) -> list[str]:
    """How to open a transcript at a line: $EDITOR when it takes +line, else less."""
    import os
    import shutil as _shutil

    editor = os.environ.get("EDITOR", "").strip()
    program = Path(editor.split()[0]).name if editor else ""
    if program in ("vi", "vim", "nvim", "nano", "micro", "hx", "emacs", "emacsclient"):
        return [*editor.split(), f"+{line}", str(path)]
    if program == "code":
        return [*editor.split(), "-g", f"{path}:{line}"]
    pager = _shutil.which("less") or "more"
    return [pager, f"+{line}", str(path)]


# --- the interactive TUI --------------------------------------------------------

_THEME_KEY = "board_theme"


def _load_saved_theme() -> str | None:
    """The autosaved board theme from ``state.json``, if any."""
    path = paths.state_path()
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get(_THEME_KEY)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, str) else None


def _save_theme(name: str) -> None:
    """Autosave the board theme (every change persists — no save step).

    Tolerates a corrupt state.json (same anticipation as the loader) and
    writes atomically (tmp + rename) so a mid-write crash can never leave
    the shared state file truncated.
    """
    try:
        paths.ensure_home()
        path = paths.state_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, ValueError):
            data = {}
        data[_THEME_KEY] = name
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        temp.replace(path)
    except OSError:
        return


def _build_app_class(interval: float) -> Any:
    """Build the Textual app class (a factory so tests can drive it headless)."""
    from textual.app import App, ComposeResult
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.screen import ModalScreen
    from textual.widgets import DataTable, Footer, OptionList, Static
    from textual.widgets.option_list import Option

    class ThemePicker(ModalScreen[None]):
        """A theme browser that STAYS OPEN: every highlight applies (and
        autosaves) instantly; ``Esc`` is the explicit close."""

        CSS = """
        ThemePicker { align: center middle; }
        #themebox { width: 44; height: 70%; border: heavy $accent;
                    background: $surface; padding: 1; }
        #themehint { height: 2; color: $text-muted; }
        #themelist { height: 1fr; }
        """
        BINDINGS: ClassVar = [("escape", "close_picker", "close")]

        def compose(self) -> ComposeResult:
            with Vertical(id="themebox"):
                yield Static(
                    "browse themes — ↑/↓ or click applies instantly (autosaved) · Esc closes",
                    id="themehint",
                )
                yield OptionList(id="themelist")

        def on_mount(self) -> None:
            picker = self.query_one("#themelist", OptionList)
            current = self.app.theme
            for index, name in enumerate(sorted(self.app.available_themes)):
                picker.add_option(Option(name, id=name))
                if name == current:
                    picker.highlighted = index
            picker.focus()

        def on_option_list_option_highlighted(self, event: Any) -> None:
            if event.option is not None and event.option.id is not None:
                self.app.theme = event.option.id  # applied live; watcher autosaves

        def on_option_list_option_selected(self, event: Any) -> None:
            # Enter/click applies too — and the picker deliberately stays open.
            self.on_option_list_option_highlighted(event)

        def action_close_picker(self) -> None:
            self.dismiss(None)

    class BoardApp(App[None]):
        TITLE = "aisquare board"
        CSS = """
        #main { height: 1fr; }
        #board { width: 48; min-width: 32; }
        #board.collapsed { display: none; }
        #sessions { height: auto; max-height: 10; border: round $primary;
                    padding: 0 1; }
        #tasks { height: 1fr; border: round $primary; }
        #feedpane { width: 1fr; }
        #feed { height: 1fr; border: round $secondary; }
        #feed.hidden { display: none; }
        #feedtext { height: 1fr; border: round $success; display: none;
                    padding: 0 1; }
        #feedtext.active { display: block; }
        #detailwrap { height: 8; border: heavy $warning; padding: 0 1; }
        """
        BINDINGS: ClassVar = [
            ("q", "quit", "quit"),
            ("b", "toggle_board", "board on/off"),
            ("r", "refresh_now", "refresh"),
            ("t", "pick_theme", "themes"),
            ("s", "screenshot", "screenshot"),
            ("a", "toggle_autoscroll", "autoscroll"),
            ("v", "toggle_select", "select text"),
            ("c", "copy_selection", "copy"),
            ("d", "toggle_done", "done archive"),
            ("o", "open_transcript", "transcript"),
        ]

        def __init__(self) -> None:
            super().__init__()
            self._events_by_id: dict[str, TeamEvent] = {}
            self._feed_order: list[str] = []
            self._tasks_by_id: dict[str, TeamTask] = {}
            self._roles: dict[str, str] = {}
            self._statuses: dict[str, str] = {}
            self._last_seq = 0
            self._terminal_by_task: dict[str, TeamEvent] = {}
            self._terminal_fetched = False
            self._prev_states: dict[str, str] = {}
            self._autoscroll = True
            self._select_mode = False
            self._show_done = False
            self._project: Any = None
            self._sessions: dict[str, TeamSession] = {}
            self._all_tasks: list[TeamTask] = []
            self._detail_moment: tuple[str | None, Any] | None = None
            self.detail_text = ""

        def compose(self) -> ComposeResult:
            with Horizontal(id="main"):
                with Vertical(id="board"):
                    yield Static(id="sessions")
                    yield DataTable[Text](id="tasks", cursor_type="row")
                with Vertical(id="feedpane"):
                    yield OptionList(id="feed")
                    with VerticalScroll(id="feedtext"):
                        yield Static(id="feedstatic")
            with VerticalScroll(id="detailwrap"):
                yield Static(id="detail")
            yield Footer()

        def action_pick_theme(self) -> None:
            self.push_screen(ThemePicker())

        def action_change_theme(self) -> None:
            # The command palette's "Change theme" lands here — route it to
            # our stays-open picker instead of textual's pick-and-close one.
            self.action_pick_theme()

        def action_screenshot(self, filename: str | None = None, path: str | None = None) -> None:
            """Save an SVG of the board to ~/.aisquare/screenshots (always local —
            textual's own palette screenshot 'delivers' and can fail in plain
            terminals)."""
            try:
                target = Path(path) if path else paths.aisquare_home() / "screenshots"
                target.mkdir(parents=True, exist_ok=True)
                name = filename or f"board-{datetime.now():%Y%m%d-%H%M%S}.svg"
                file = target / name
                file.write_text(self.export_screenshot(), encoding="utf-8")
                self.notify(str(file), title="screenshot saved", timeout=6)
            except Exception as exc:  # never crash the board over a screenshot
                self.notify(f"screenshot failed: {exc}", severity="error", timeout=8)

        def watch_theme(self, theme_name: str) -> None:
            # Fires on ANY theme change (our picker or the command palette):
            # every change is the save. Restored on the next launch.
            parent = getattr(super(), "watch_theme", None)
            if parent is not None:
                parent(theme_name)
            if getattr(self, "_theme_restored", False):
                _save_theme(theme_name)

        def on_mount(self) -> None:
            # Resolve the project once and hold it on the app, passing it to
            # every board query — a `git rev-parse` per tick otherwise, and
            # mutating os.environ to pin it leaks process-wide (round-3
            # finding 3).
            try:
                self._project = team_service.resolve_project(None)
            except Exception:
                self._project = None
            saved = _load_saved_theme()
            if saved and saved in self.available_themes:
                self.theme = saved
            self._theme_restored = True
            table = self.query_one("#tasks", DataTable)
            table.add_columns("id", "st", "who", "title")
            self.query_one("#sessions", Static).border_title = "team"
            self.query_one("#tasks", DataTable).border_title = "tasks"
            self.query_one("#feed", OptionList).border_title = "live feed"
            self.query_one(
                "#detailwrap", VerticalScroll
            ).border_title = "detail — click a task or feed line"
            self._refresh_data()
            self.set_interval(interval, self._refresh_data)

        def _refresh_data(self) -> None:
            try:
                project, sessions, tasks, events = team_service.board_data(
                    events=400 if self._last_seq == 0 else 200,
                    since_seq=self._last_seq or None,
                    project=self._project,
                )
            except team_service.TeamDisabledError:
                self.query_one("#sessions", Static).update(
                    Text("team bus disabled (AISQUARE_TEAM=0)", style="bold red")
                )
                return
            except Exception:  # bus briefly unavailable — keep the last frame
                return
            self.title = f"aisquare board — {project.root.name or project.id}"
            self._roles = {s.id: s.role for s in sessions}
            self._sessions = {s.id: s for s in sessions}
            self._statuses = {t.id: t.status for t in tasks}
            self._all_tasks = tasks
            self.query_one("#sessions", Static).update(_session_lines(sessions))
            self._ring_on_attention(sessions)
            # Events first: a task_done/dropped this tick flushes the
            # attribution cache BEFORE the archive rebuilds, so the closer is
            # never one frame stale (round-4 finding 6).
            self._append_events(events)
            self._refresh_tasks(tasks)

        def _ring_on_attention(self, sessions: list[TeamSession]) -> None:
            """Terminal bell when a session newly flips to needing the user."""
            states = {s.id: s.state for s in sessions if s.ended_at is None}
            for session_id, state in states.items():
                previous = self._prev_states.get(session_id)
                if state == "attention" and previous not in (None, "attention"):
                    self.bell()
                    break
            self._prev_states = states

        def _done_event_for(self, task_id: str) -> TeamEvent | None:
            """The latest done/dropped event for a task (who closed it, when).

            Resolved from the store, not the bounded feed cache — a task
            closed thousands of events ago still knows who closed it. The
            cache is flushed in ``_append_events`` whenever a new terminal
            event arrives, so a reopen-and-reclose re-attributes correctly.
            """
            # Fetch the whole attribution map once per flush, not per row: the
            # populated dict is the cache, so a task with NO terminal event
            # (a crash between set_task_status and its _emit, or a pre-event
            # row) returns None from .get() without re-running the store query
            # every tick (round-4 finding 6, negative caching).
            if not self._terminal_fetched:
                try:
                    self._terminal_by_task = team_service.terminal_attribution(
                        project=self._project
                    )
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
                closed = [t for t in tasks if t.status in ("done", "dropped")]
                closed.sort(key=lambda t: t.updated_at, reverse=True)
                table.border_title = f"done archive ({len(closed)}) — d for open tasks"
                for task in closed:
                    self._tasks_by_id[task.id] = task
                    event = self._done_event_for(task.id)
                    who = (
                        team_service.short_id(event.session_id)
                        if event and event.session_id
                        else ""
                    )
                    when = f"{local_time(task.updated_at):%H:%M}"
                    table.add_row(
                        Text(task.id[-8:], style="dim"),
                        Text(f"{'✅' if task.status == 'done' else '🗑'} {when}"),
                        Text(who),
                        Text(task.title, no_wrap=True, overflow="ellipsis"),
                        key=task.id,
                    )
            else:
                open_tasks = [
                    t for t in tasks if t.status in ("todo", "doing", "review", "blocked")
                ]
                table.border_title = "tasks — d for done archive"
                for task in open_tasks:
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

        def action_toggle_done(self) -> None:
            self._show_done = not self._show_done
            self._refresh_tasks(self._all_tasks)

        def _moment_transcript(self) -> tuple[Path, Any] | None:
            """(transcript path, timestamp) for the selected item, if known."""
            if self._detail_moment is None:
                return None
            session_id, when = self._detail_moment
            session = self._sessions.get(session_id or "")
            if session is None or not session.transcript_path:
                return None
            transcript = Path(session.transcript_path)
            if not transcript.exists():
                return None
            return transcript, when

        def action_open_transcript(self) -> None:
            """Jump into the author session's transcript around the selected moment."""
            import subprocess

            target = self._moment_transcript()
            if target is None:
                self.notify(
                    "no transcript for this item (session predates transcript capture, "
                    "or it was a CLI/remote action)",
                    timeout=5,
                )
                return
            transcript, when = target
            line = transcript_line_near(transcript, when)
            try:
                with self.suspend():
                    subprocess.call(_transcript_command(transcript, line))
            except Exception as exc:  # never crash the board over a viewer
                self.notify(f"could not open transcript: {exc}", severity="error", timeout=6)

        def _cursor_task_id(self, table: DataTable[Text]) -> str | None:
            try:
                coordinate = table.cursor_coordinate
                key = table.coordinate_to_cell_key(coordinate).row_key.value
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
                feed.add_option(Option(feed_line(event, self._roles), id=event.id))
                self._last_seq = max(self._last_seq, event.seq)
                if event.kind in ("task_done", "task_dropped"):
                    # A close happened — attribution (positive AND negative
                    # entries) is stale; a reopen-and-reclose must re-attribute.
                    self._terminal_by_task = {}
                    self._terminal_fetched = False
            if self._autoscroll:
                feed.scroll_end(animate=False)

        def _feed_title(self) -> str:
            title = "live feed"
            if self._select_mode:
                return f"{title} — SELECT MODE (drag to select, c copies, v resumes)"
            if not self._autoscroll:
                title += " — autoscroll off"
            return title

        def action_toggle_autoscroll(self) -> None:
            self._autoscroll = not self._autoscroll
            feed = self.query_one("#feed", OptionList)
            feed.border_title = self._feed_title()
            if self._autoscroll:
                feed.scroll_end(animate=False)

        def action_toggle_select(self) -> None:
            """Swap the live feed for a frozen, mouse-selectable text view."""
            self._select_mode = not self._select_mode
            feed = self.query_one("#feed", OptionList)
            wrap = self.query_one("#feedtext", VerticalScroll)
            if self._select_mode:
                snapshot = Text()
                for event_id in self._feed_order:
                    event = self._events_by_id[event_id]
                    snapshot.append(feed_line(event, self._roles))
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
                self._refresh_data()  # apply the backlog accumulated while frozen
                if self._autoscroll:
                    feed.scroll_end(animate=False)

        def action_copy_selection(self) -> None:
            """Copy the mouse-selected text (select mode) to the clipboard."""
            getter = getattr(self.screen, "get_selected_text", None)
            selected = getter() if callable(getter) else None
            if not selected:
                self.notify("nothing selected — v enters select mode, then drag", timeout=4)
                return
            self.copy_to_clipboard(selected)
            self.notify(f"copied {len(selected)} chars", timeout=3)

        def _show_detail(self, content: Text) -> None:
            self.detail_text = content.plain  # exposed for tests
            self.query_one("#detail", Static).update(content)
            self.query_one("#detailwrap", VerticalScroll).scroll_home(animate=False)

        def on_data_table_row_selected(self, event: Any) -> None:
            # Enter (and click-when-cursor-already-there) post RowSelected. A
            # rebuild's clear/add_row/move_cursor posts RowHighlighted but never
            # RowSelected, so no artifact can touch the detail pane — the
            # timing-independent fix that closed the clobber across rounds 2-5.
            key = event.row_key.value if event.row_key else None
            self._show_task_detail(str(key) if key is not None else None)

        def on_click(self, event: Any) -> None:
            # DataTable only posts RowSelected when the click lands on the cell
            # the cursor is ALREADY on, so a first click would move the cursor
            # but show no detail. A mouse Click is user-only (rebuilds never
            # post one), so handling it directly restores one-click detail
            # without reopening the rebuild-artifact door.
            try:
                table = self.query_one("#tasks", DataTable)
                widget, _ = self.get_widget_at(event.screen_x, event.screen_y)
            except Exception:
                return
            if widget is table:
                self.call_after_refresh(self._show_cursor_task_detail)

        def _show_cursor_task_detail(self) -> None:
            table = self.query_one("#tasks", DataTable)
            try:
                cell = table.coordinate_to_cell_key(table.cursor_coordinate)
                key = cell.row_key.value
            except Exception:
                return
            self._show_task_detail(str(key) if key is not None else None)

        def _show_task_detail(self, key: str | None) -> None:
            if key is None:
                return
            task = self._tasks_by_id.get(key)
            if task is None:
                return
            detail = _task_detail(task, self._statuses)
            done_event = (
                self._done_event_for(task.id) if task.status in ("done", "dropped") else None
            )
            if done_event is not None:
                who = team_service.short_id(done_event.session_id or "cli")
                verb = "done" if done_event.kind == "task_done" else "dropped"
                detail.append(
                    f"\n{verb} by {who} at {local_time(done_event.created_at):%H:%M:%S}",
                    style="green" if verb == "done" else "yellow",
                )
                self._detail_moment = (done_event.session_id, done_event.created_at)
            else:
                self._detail_moment = (task.claimed_by, task.updated_at)
            if self._moment_transcript() is not None:
                detail.append("\npress o to open the transcript at this moment", style="dim")
            self._show_detail(detail)

        def on_option_list_option_selected(self, event: Any) -> None:
            # OptionSelected (click / Enter) only, mirroring the task table:
            # an append/scroll never posts it, so a feed selection is stable.
            if event.option is not None and event.option.id is not None:
                stored = self._events_by_id.get(event.option.id)
                if stored is not None:
                    self._detail_moment = (stored.session_id, stored.created_at)
                    detail = _event_detail(stored, self._roles)
                    if self._moment_transcript() is not None:
                        detail.append(
                            "\npress o to open the transcript at this moment", style="dim"
                        )
                    self._show_detail(detail)

        def action_toggle_board(self) -> None:
            self.query_one("#board").toggle_class("collapsed")

        def action_refresh_now(self) -> None:
            self._refresh_data()

        def on_resize(self, event: Any) -> None:
            board = self.query_one("#board")
            if event.size.width < 92:
                board.add_class("collapsed")
            else:
                board.remove_class("collapsed")

    return BoardApp


def _run_tui(interval: float) -> None:
    _build_app_class(interval)().run()


# --- the Rich fallback ------------------------------------------------------------


def board_frame(height: int, width: int) -> Text:
    """One fallback frame: header, sessions, open tasks, then as many recent
    events as the remaining terminal rows can hold."""
    project, sessions, tasks, events = team_service.board_data(events=200)
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(
        f"aisquare board — {project.root.name or project.id} — {datetime.now():%H:%M:%S}\n",
        style="bold",
    )
    live_sessions = [s for s in sessions if s.ended_at is None]
    text.append("sessions\n", style="bold cyan")
    if live_sessions:
        text.append(_session_lines(sessions))
    else:
        text.append("  (none live)\n", style="dim")
    statuses = {t.id: t.status for t in tasks}
    open_tasks = [t for t in tasks if t.status in ("todo", "doing", "review", "blocked")]
    done = sum(1 for t in tasks if t.status == "done")
    text.append(f"tasks — {len(open_tasks)} open · {done} done\n", style="bold cyan")
    for task in open_tasks[:10]:
        claim = f" @{team_service.short_id(task.claimed_by)}" if task.claimed_by else ""
        marker = "⧗" if unmet_needs(task, statuses) else ""
        text.append(f"  {task.id[-8:]} [{task.status}{marker}{claim}] {task.title}\n")
    if len(open_tasks) > 10:
        text.append(f"  … {len(open_tasks) - 10} more\n", style="dim")
    used = 3 + max(len(live_sessions), 1) + min(len(open_tasks), 10) + 2
    room = max(3, height - used - 1)
    text.append("updates (newest last)\n", style="bold cyan")
    roles = {s.id: s.role for s in sessions}
    for event in events[-room:]:
        text.append(feed_line(event, roles))
        text.append("\n")
    return text


def _run_fallback(interval: float) -> None:
    from rich.live import Live

    console = stdout_console()
    try:
        with Live(console=console, screen=True, auto_refresh=False) as live:
            while True:
                frame = board_frame(console.size.height, console.size.width)
                live.update(frame, refresh=True)
                time.sleep(interval)
    except KeyboardInterrupt:
        return


def run_watch(interval: float) -> None:
    """The board watch entry point: interactive TUI, or Rich fallback."""
    try:
        import textual  # noqa: F401
    except ImportError:
        stderr_console().print(
            "tip: pip install 'aisquare-cli[tui]' for the interactive board "
            "(clickable tasks/feed, scrolling, detail bar)"
        )
        _run_fallback(interval)
        return
    _run_tui(interval)
