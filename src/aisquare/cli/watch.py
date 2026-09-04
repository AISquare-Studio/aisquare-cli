"""The live board (``aisquare board --watch``).

Two implementations behind one entry point:

- **Interactive TUI** (needs the ``tui`` extra: ``pip install
  aisquare-cli[tui]``): board on the left (sessions + task table), a
  bot-style agent feed on the right, and a detail bar at the bottom —
  click/select any task or feed line to see its full, unclipped content.
  The feed scrolls; the board collapses on narrow terminals (or with ``b``).
  The widgets are ``cli.ui.board.BoardPanel`` — the same panel the fleet UI
  shows as a project's Board tab (docs/plans/fleet-tui.md §4.2); this module
  hosts it under a footer, the theme picker and its autosave.
- **Rich fallback** (no extra deps): a full-screen frame that refreshes in
  place and sizes the feed to the terminal height.

Only presentation lives here; all data comes from ``services.team``. The pure
renderers (``feed_line``, ``_session_lines``, the detail texts, the transcript
helpers) live here rather than beside the widgets because the fallback needs
them without Textual, and ``_load_saved_theme`` / ``_save_theme`` are imported
by the fleet UI, which reuses the theme persistence verbatim.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from rich.text import Text

from aisquare.cli.common import local_time
from aisquare.core import harness, paths
from aisquare.core.console import stderr_console, stdout_console
from aisquare.core.store import unmet_needs
from aisquare.models import ProjectInfo, TeamEvent, TeamSession, TeamTask
from aisquare.services import team as team_service

if TYPE_CHECKING:
    from textual.app import App

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
    "activate": ("⚡", "activated the orchestrator"),
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
    accounts = len({s.account for s in live if s.account})
    for session in live:
        emoji = _ROLE_EMOJI.get(session.role, "🤖")
        style = _ROLE_STYLE.get(session.role, "white")
        minutes = max(0, int((now - session.last_seen_at).total_seconds() // 60))
        text.append(f"{emoji} {session.role}·{team_service.short_id(session.id)}", style=style)
        chip, chip_style = _STATE_CHIP.get(session.state, (session.state, "dim"))
        text.append(f"  {chip}", style=chip_style)
        label = team_service.account_label(session.account)
        # Only meaningful once the board spans several accounts.
        if label and accounts > 1:
            text.append(f"  {label}", style="cyan dim")
        if session.model:
            text.append(f"  {session.model}", style="dim cyan")
            if harness.model_mismatch(session.role, session.model):
                text.append("  ⚠ off-ladder", style="bold yellow")
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


def action_open_transcript(app: App[Any], command: list[str]) -> str | None:
    """Run ``command`` — a viewer on a transcript file — with the TUI suspended.

    Returns the error text when the viewer could not be started, ``None`` when
    it ran; the caller decides how to show that. Never raises: a board must not
    die over a pager.

    The name is a spawn-seam key. ``core.spawn.SEAMS`` rules on
    ``cli/watch.py::action_open_transcript`` — the ``BoardApp`` method this was
    before the widgets moved to ``cli.ui.board`` — and the registry keys on
    ``<module>::<enclosing function>``, so the one line that starts a process
    kept its module and its name while the widget that calls it moved.
    """
    import subprocess

    try:
        with app.suspend():
            subprocess.call(command)
    except Exception as exc:  # never crash the board over a viewer
        return str(exc)
    return None


def _build_app_class(interval: float) -> Any:
    """Build the Textual app class (a factory so tests can drive it headless).

    The board itself is ``cli.ui.board.BoardPanel``; this app adds what a
    standalone board needs around it — a footer, the theme picker with its
    autosave, screenshots — and resolves the project once so a tick never
    costs a ``git rev-parse`` (mutating ``os.environ`` to pin it would leak
    process-wide).
    """
    from textual.app import App as TextualApp
    from textual.app import ComposeResult
    from textual.containers import Vertical
    from textual.screen import ModalScreen
    from textual.widgets import Footer, OptionList, Static
    from textual.widgets.option_list import Option

    from aisquare.cli.ui.board import BoardPanel

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

    class BoardApp(TextualApp[None]):
        TITLE = "aisquare board"
        BINDINGS: ClassVar = [
            ("q", "quit", "quit"),
            ("t", "pick_theme", "themes"),
            ("s", "screenshot", "screenshot"),
        ]

        @property
        def board(self) -> BoardPanel:
            """The one board this app hosts (what tests and actions reach for)."""
            return self.query_one(BoardPanel)

        def compose(self) -> ComposeResult:
            try:
                project: ProjectInfo | None = team_service.resolve_project(None)
            except Exception:
                project = None
            yield BoardPanel(project, interval=interval, id="board-panel")
            yield Footer()

        def on_mount(self) -> None:
            saved = _load_saved_theme()
            if saved and saved in self.available_themes:
                self.theme = saved
            self._theme_restored = True

        def on_board_panel_refreshed(self, event: BoardPanel.Refreshed) -> None:
            self.title = f"aisquare board — {event.project.root.name or event.project.id}"

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
