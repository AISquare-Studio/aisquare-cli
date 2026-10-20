"""``FleetApp`` — the two-pane shell, and ``run_ui`` which bare ``asq`` calls.

docs/plans/fleet-tui.md §4, §4.2, §4.3, §3.8. Left: the ``Sidebar``. Right: a
``ContentSwitcher`` over the views — Welcome and Doctor from the start, Onboard on
the first ``+``, Accounts on the first click of its section
(docs/plans/claude-accounts.md), one ``ProjectView`` / ``AgentView`` per
selection, created the first time it is asked for and kept (a view hosts a live
terminal pane; re-creating it on every click would restart that pane's render
loop).

**The TUI holds no state that matters** (§2). Projects come from the store,
agents from the fleet service, both re-read every two seconds exactly as
``board -w`` does; the last good frame is kept — and labelled stale — when the
store cannot be read, because the agents are unaffected by our trouble and a
blank sidebar would say otherwise.

**The app's keys belong to the sidebar** (§4.3). ``q`` ``t`` ``r`` ``?`` and the
palette are declared non-priority and refused by ``check_action`` unless focus
is in the sidebar (or nowhere yet) — not merely "unless a ``TerminalPane`` has
focus": the views mount ``Button``s, ``Select``s and ``Switch``es, none of which
consumes a letter key, so ``q`` in a half-filled form used to quit. Textual's
defaults that would steal keys from the agent are removed:
``inherit_bindings=False`` drops the priority ``ctrl+q`` and the ``ctrl+c`` "how
to quit" hint, and the command palette moves from ``ctrl+p`` to ``F1``. Claude
Code uses ctrl+c, ctrl+o, ctrl+r, ctrl+t, ctrl+b, ctrl+g, ctrl+v and shift+tab;
all of them must reach it.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import threading
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from rich.text import Text
from textual import events
from textual.app import ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.css.query import NoMatches
from textual.dom import DOMNode
from textual.geometry import Region
from textual.message import Message
from textual.notifications import SeverityLevel
from textual.screen import ModalScreen, Screen
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import ContentSwitcher, Footer, Static
from textual.worker import Worker, WorkerState, WorkType
from textual.worker_manager import WorkerManager

from aisquare.cli.ui.attach import NewAccountRequested
from aisquare.cli.ui.autosave import Autosave
from aisquare.cli.ui.board import BoardPanel, board_help
from aisquare.cli.ui.divider import Divider, floor_of
from aisquare.cli.ui.groups import (
    DropGroup,
    DropProject,
    GroupPicker,
    GroupProjects,
    MoveRow,
    ToggleCollapse,
    TogglePin,
    UndoLayout,
)
from aisquare.cli.ui.project_menu import PIN, REMOVE, RENAME, SETTINGS, ConfirmRemove, ProjectMenu
from aisquare.cli.ui.remote_control import SWITCHES, RemoteController, ngrok_ends_with
from aisquare.cli.ui.sidebar import (
    HEADROOM_DOT,
    RED,
    SETTINGS_KEY,
    TIMES,
    AccountsSelected,
    AddProject,
    AgentSelected,
    AppSettingsSelected,
    DoctorSelected,
    OpenProjectMenu,
    ProjectCard,
    ProjectRenamed,
    ProjectSelected,
    RenameProject,
    ResizeSidebar,
    SettingsRow,
    SettingsSelected,
    Sidebar,
    SpawnAgent,
    StopAgent,
    agent_name,
)
from aisquare.cli.ui.spawn import SpawnCompleted, SpawnDialog
from aisquare.cli.ui.stop import StopAgentScreen
from aisquare.cli.ui.terminal import EscapeToSidebar, SelectionHost, TerminalPane
from aisquare.cli.ui.theme import ThemePicker, restore_theme, theme_autosave
from aisquare.cli.ui.ticks import while_running
from aisquare.cli.ui.views.accounts import AccountsChanged, AccountsView, UsageRead, read_session
from aisquare.cli.ui.views.agent import AgentRestarted, AgentView
from aisquare.cli.ui.views.app_settings import AppSettingsView
from aisquare.cli.ui.views.doctor import DoctorRefreshed, DoctorView
from aisquare.cli.ui.views.onboard import OnboardFailed, OnboardView, ProjectOnboarded
from aisquare.cli.ui.views.project import ProjectView
from aisquare.cli.ui.views.remote import RemotePanel
from aisquare.cli.ui.views.settings_hub import SettingsHubView
from aisquare.cli.ui.views.welcome import WelcomeView
from aisquare.core import clipboard
from aisquare.core.console import stderr_console
from aisquare.core.store import ContextStore, store_session
from aisquare.core.terminal import mac_keyboard
from aisquare.models import (
    AccountsOverview,
    CheckStatus,
    ClaudeAccount,
    DoctorCheck,
    FleetAgent,
    FleetAgentStatus,
    HeadroomSummary,
    ProjectForgetReport,
    ProjectInfo,
)
from aisquare.services import claude_accounts as accounts_service
from aisquare.services import diagnostics, project_groups
from aisquare.services import fleet as fleet_service
from aisquare.services import project as project_service

DoctorRunner = Callable[[], list[DoctorCheck]]
"""What the Doctor page runs: ``diagnostics.doctor`` in production, a stub in tests."""

AccountsReader = Callable[[], AccountsOverview]
"""What the Accounts page reads: ``claude_accounts.overview`` in production, a stub in tests."""

HeadroomReader = Callable[[Sequence[ClaudeAccount] | None], HeadroomSummary]
"""What the status line's Claude dot reads: ``claude_accounts.read_fleet_headroom`` (through
the five-minute usage cache) in production, a stub in tests."""

_DoctorReport = tuple[Path | None, list[DoctorCheck]]
"""What the doctor worker hands back: the scope it ran for, and its checks."""

_DOCTOR_WORKER = "doctor"
ACCOUNTS_WORKER = "shell-accounts"
"""The thread worker that reads the accounts' frame (``FleetApp.refresh_accounts``)."""
HEADROOM_WORKER = "sidebar-headroom"
"""The thread worker that reads the status line's headroom (``FleetApp.refresh_headroom``)."""
HEADROOM_SECONDS = 60.0
"""How often the status line's headroom is re-read — never on the two-second tick. A read
goes to the network only for an account whose cached usage is over five minutes old."""
PROJECT_REMOVE_WORKER = "project-remove"
"""The thread worker a card's *Remove from fleet…* forgets its project on (``project forget``)."""
_QUIT_POLL = 0.1
"""Seconds between the looks a hand-over takes at the reads it waits out (``_exit_when_read``)."""
_HAND_OFF_PATIENCE = 10.0
"""Seconds a hand-over waits for the reads running before it names them and can be called off."""
_hand_off_clock: Callable[[], float] = time.monotonic
"""What a hand-over's wait is timed by: the injection point a test's own clock takes, so the
seconds its toast names are the test's, not a runner's (a slow one said 1 s for 0)."""

SIDEBAR_WIDTH_KEY = "sidebar_width"
"""The ``state.json`` key the navigator's width is remembered under (#137) — beside
``board_theme`` and ``active_project_id``; ``core.state_file`` is the file's one
reader and writer."""


def _doctor_report(result: object) -> _DoctorReport | None:
    """Unpack a doctor worker's tagged result; ``None`` when it carries no tag.

    The worker is ours (its identity is checked before this is called), so the
    shape holds — the check is here so a runner that ever forgets the scope tag
    paints nothing instead of raising inside a message handler.
    """
    if isinstance(result, tuple) and len(result) == 2:
        scope, checks = result
        if (scope is None or isinstance(scope, Path)) and isinstance(checks, list):
            return scope, list(checks)
    return None


UNDO_DEPTH = 50
"""How many layout gestures ``u`` can walk back in one session (#140)."""

SELECTED_KEY = "fleet.selected"
"""``ui_state`` key for what is open: ``project:<id>``, ``agent:<project>/<id>``, ``settings``
(the hub), ``app-settings``, ``accounts`` or ``doctor:<project or ''>`` (#144)."""
SHOW_CAPTURED_KEY = "fleet.show_captured"


def _ui_state(key: str) -> str | None:
    """A remembered UI fact — ``None`` when there is none or the store cannot say."""
    try:
        with store_session() as store:
            return store.ui_state(key)
    except Exception:
        return None


def _remember_ui_state(key: str, value: str | None) -> None:
    """Every change is the save; a store that will not take it costs the memory, never the UI."""
    try:
        with store_session() as store:
            store.set_ui_state(key, value)
    except Exception:
        return


@dataclass(frozen=True)
class FleetSnapshot:
    """One frame of what the sidebar shows — kept on the app so tests and views can read it."""

    projects: list[ProjectInfo]
    agents: dict[str, list[FleetAgentStatus]]
    notices: dict[str, str] = field(default_factory=dict)
    """Per project: why its agent rows are missing (a fleet call that failed open)."""
    taken_at: datetime = field(default_factory=datetime.now)
    stale_since: datetime | None = None
    """Set when a later refresh could not read the store and this frame was kept."""

    def project(self, project_id: str) -> ProjectInfo | None:
        return next((p for p in self.projects if p.id == project_id), None)

    def agent(self, project_id: str, agent_id: str) -> FleetAgentStatus | None:
        return next((s for s in self.agents.get(project_id, []) if s.agent.id == agent_id), None)


class RemoteNews(Message):
    """What the Remote controller says the human should hear (``RemoteController.on_news``),
    posted from whichever thread learned it: ``post_message`` is safe from any thread.
    ``done``: what a control's write did (``RemoteController.on_done``), said in any case."""

    def __init__(self, text: str, trouble: bool, *, done: bool = False) -> None:
        super().__init__()
        self.text = text
        self.trouble = trouble
        self.done = done


class HelpScreen(ModalScreen[None]):
    """The keys, in one place. Esc, q or ? closes; opened from the board, it shows the
    Board section first — the icon legend sat ~45 lines down, below the fold."""

    CSS = """
    HelpScreen { align: center middle; }
    #helpbox { width: 78; height: auto; max-height: 90%; border: heavy $accent;
               background: $surface; padding: 1 2; }
    #helpbox Static { height: auto; }
    """
    BINDINGS: ClassVar = [
        ("escape", "close_help", "close"),
        ("q", "close_help", "close"),
        ("question_mark", "close_help", "close"),
    ]
    BOARD_HEADING: ClassVar[str] = "Board (a project's Board tab, and aisquare board -w)"
    MAC_COPY_KEYS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("ctrl+c", "copy a pane's highlight again; ⌘C stays the terminal's"),
        ("⌘V", "paste into the focused pane or field"),
        ("⌥ drag", "iTerm2: select with the terminal itself, then ⌘C"),
        ("fn drag", "Terminal.app, on most Macs: the same"),
    )
    """On macOS, after ``drag``: the copy and paste keys a Mac has. Terminal.app and iTerm2
    keep ⌘C as their own Copy, so it never reaches asq; ⌘V does, as a bracketed paste."""

    def __init__(self, escape_key: str, *, at_board: bool = False, capturing: bool = False) -> None:
        super().__init__()
        self.escape_key = escape_key
        self.at_board = at_board
        self.capturing = capturing

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="helpbox"):
            yield Static(self.keys_text())

    def keys_text(self) -> Text:
        """What the help says: the fleet's keys, the board's, and the legends."""
        text = Text()
        text.append("aisquare fleet — keys\n\n", style="bold")
        for key, what in (
            ("click", "select a project or an agent; ⚙ Settings; + onboards a project"),
            ("↑ ↓ Enter", "move over the sidebar and open the row under the cursor"),
            # Arranging the sidebar (#140): every one of these has a CLI twin.
            ("shift+↑ ↓", "move the row under the cursor one place"),
            ("g p space", "group · pin · fold the row under the cursor"),
            ("u", "undo the last arrangement; a toast says what"),
            ("m", "mark the card under the cursor, as shift+click does"),
            ("shift+g", "group the marked cards; Esc clears the marks"),
            ("drag title", "move a card or a group header to a new place"),
            ("F2 dblclick", "rename the card in place: Enter or a click away saves, Esc cancels"),
            (". ⋯ r-click", "the card's menu: rename, pin, settings, remove from the fleet"),
            (self.escape_key.upper(), "hand focus from an agent's pane back to the sidebar"),
            ("wheel", "scroll an agent pane; shift/alt+PgUp/PgDn too, shift+Home/End"),
            ("drag", "select text in a pane (double-click: a word) — copied on release"),
            *(self.MAC_COPY_KEYS if mac_keyboard() else ()),
            ("divider", "drag the line beside the sidebar to resize it; double-click puts it back"),
            ("> < =", "from the sidebar: widen, narrow, reset the divider"),
            ("x", "from the sidebar: stop the selected agent (asks first)"),
            ("t", "themes (applied live, autosaved)"),
            ("R", "remote control — the fleet on your phone: link, QR, password, devices"),
            ("r", "refresh now"),
            # Only while `[projects] capture` is on: off, nothing is captured to show.
            *(
                [("a", "show or hide the captured directories (never added)")]
                if self.capturing
                else []
            ),
            ("+  w", "onboard a project · back to the Welcome page"),
            ("F1", "command palette"),
            ("q", "quit — from the sidebar; inside a pane every key goes to the agent"),
            ("?", "this help — from anywhere but an agent's pane"),
        ):
            text.append(f"  {key:<11}", style="bold cyan")
            text.append(f" {what}\n")
        text.append(f"\n{self.BOARD_HEADING}\n", style="bold")
        text.append_text(board_help())
        text.append("\nAccounts (⚙ Settings ▸ Accounts)\n", style="bold")
        text.append(f"  {'F2 dblclick':<11}", style="bold cyan")
        text.append(" rename an account in place; Enter on its row does too\n")
        text.append("\nBeside ⚙ Settings\n", style="bold")
        text.append(f"  {'✗ 1  ⚠ 2':<11}", style="bold cyan")
        text.append(" what the doctor's last run found; its page has the report\n")
        text.append("\nThe line under ⚙ Settings", style="bold")
        text.append(" (a click on it opens Accounts)\n", style="dim")
        text.append("  ● aisquare", style="bold cyan")
        text.append("  green signed in · red signed out · grey not known\n")
        text.append("  ● 3", style="bold cyan")
        text.append(f"{TIMES} claude", style="bold cyan")
        text.append("  usable Claude accounts; the dot is the room LEFT across them:\n")
        text.append("     each account 100 - max(session % used, week % used), averaged —\n")
        text.append("     ")
        # The line's own dots, so the legend paints exactly what the line does.
        for level, what in (
            ("good", "over 50 %"),
            ("fair", "25-50 %"),
            ("low", "5-25 %"),
            ("critical", "under 5 %"),
        ):
            dot, colour = HEADROOM_DOT[level]
            text.append(dot, style=colour)
            text.append(f" {what}  ")
        text.append("💀 none left  ")
        dot, colour = HEADROOM_DOT["unknown"]
        text.append(dot, style=colour)
        text.append(" usage could not be read (offline, a locked Keychain, nothing usable)\n")
        text.append("     ")
        text.append("●", style=RED)
        text.append(" and ⚠ — accounts that need a login (signed out, token expired or refused);\n")
        text.append("     the dot is red when every enabled account needs one\n")
        text.append("\nEsc closes this", style="dim")
        return text

    def on_mount(self) -> None:
        if self.at_board:
            # Once laid out: until then the box has nothing to scroll.
            self.call_after_refresh(self._show_the_board)

    def _show_the_board(self) -> None:
        """Scroll the Board heading to the top. It is found among the lines as laid out:
        keys above it wrap at the box's width, and its line in the text was rows short."""
        box = self.query_one("#helpbox", VerticalScroll)
        keys = box.query_one(Static)
        lines = keys.render_lines(Region(0, 0, keys.size.width, keys.size.height))
        row = next(
            (y for y, line in enumerate(lines) if line.text.startswith(self.BOARD_HEADING)), 0
        )
        box.scroll_to(y=row, animate=False)

    def action_close_help(self) -> None:
        self.dismiss(None)


class Panes(Horizontal):
    """The two panes and the partition between them — and the partition's wiring (#137).

    ``Sidebar`` and ``Divider`` are siblings, so a message that bubbles from one
    can never reach the other; it reaches this container, which is where the
    sidebar's keyboard request (``ResizeSidebar``) meets the handle. Two more
    things are the container's because they are about the layout, not about
    either child:

    - **The content's minimum.** A ``TerminalPane`` under :data:`MIN_CONTENT`
      columns wraps every prompt line and Claude Code's own layout gives up.
      Rather than re-derive that bound on every gesture — which left it
      unenforced when the TERMINAL shrank, collapsing the pane to one column
      with the handle off screen — the container writes it as the sidebar's
      ``max-width`` whenever its own width changes, and Textual clamps against
      ``max-width`` on every layout pass. Too narrow for both minimums, the
      navigator's wins: one you can read beats a pane you cannot, and the pane
      says so with its own placeholder.
    - **The focus signal.** Focus is in the sidebar or in a pane (§4.3), and
      the sidebar's ``border-right`` used to say which. The divider is that
      line now — one column, the one the hand grabs — and lights ``$accent``
      while focus is in the sidebar.

    The app keeps no handler for any of it, and nothing here assumes there is
    one ``Divider`` on the screen: the handle and the navigator are this
    container's direct children, and a later split inside a view is not its
    business.
    """

    MIN_CONTENT: ClassVar[int] = 40
    """The columns the content pane keeps, whatever the drag or the terminal's size."""

    @property
    def sidebar(self) -> Sidebar:
        return self.query_children(Sidebar).first()

    @property
    def divider(self) -> Divider:
        return self.query_children(Divider).first()

    @classmethod
    def sidebar_ceiling(cls, total: int, floor: int) -> int:
        """The widest the navigator may be in ``total`` columns; the divider takes one of them."""
        return max(floor, total - 1 - cls.MIN_CONTENT)

    def on_mount(self) -> None:
        # Before the first layout as well as on every resize, so a width the
        # divider restores from the file is bounded whenever it is applied.
        self._fit(self.app.size.width)

    def on_resize(self, event: events.Resize) -> None:
        self._fit(event.size.width)

    def _fit(self, total: int) -> None:
        sidebar = self.sidebar
        sidebar.styles.max_width = self.sidebar_ceiling(total, floor_of(sidebar))

    def on_resize_sidebar(self, event: ResizeSidebar) -> None:
        """The sidebar's keyboard fallback: step or reset the partition."""
        event.stop()
        if event.delta is None:
            self.divider.reset()
        else:
            self.divider.step(event.delta)

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        self._mark_focus()

    def on_descendant_blur(self, event: events.DescendantBlur) -> None:
        self._mark_focus()

    def _mark_focus(self) -> None:
        """Light the divider iff focus is in the sidebar — read off the screen, not the event.

        A pane's ``DescendantBlur`` bubbles up through the content switcher while
        the sidebar's ``DescendantFocus`` is posted straight here, so the two can
        arrive in either order; the screen's ``focused`` is already settled by
        the time either does.
        """
        focused = self.screen.focused
        sidebar = self.sidebar
        beside = focused is not None and (focused is sidebar or sidebar in focused.ancestors)
        self.divider.set_class(beside, "-neighbour-focused")


class _WorkerThreads(WorkerManager):
    """The app's worker manager, which also knows the thread workers a quit would cut off,
    and holds back the reads asq starts while it waits to hand its terminal over.

    Textual's ``Worker.cancel`` cancels the task that awaits a thread worker's thread, not
    the thread: the callable runs on while the worker reads finished and leaves
    ``app.workers``. A newer exclusive start cancels an older one (the doctor on a project
    switch, a usage or credits read), and so does the removal of the view that started it,
    so a hand-over that went by the workers quit under a thread still opening the store
    (round 23 of #257). Every worker start passes through ``_new_worker``, ``@work``'s too,
    so each thread worker's callable is counted here from its first line to its last. An
    ``async def`` or an awaitable run in a thread is counted too: it is handed to Textual
    as the callable that runs it in a loop of its own, as Textual's own runner would
    (:func:`_in_its_own_loop`), since a cancel leaves that thread running as well (round 24).

    The hand-over waits out every thread worker still running
    (``FleetApp._holding_the_quit``), so a read that began after its last look would be cut
    off by the quit. While one is pending (``app.hand_off``), a read (:func:`_waited_reads`)
    is made but not started: it is held, and the newest start of a node's read replaces one
    held before it, as a newer exclusive start cancels an older. A call-off starts each
    held read once (:meth:`release`), and the quit drops them (:meth:`drop_held`). Held
    here, a read is held whatever began it (a tick, a page's mount or show, a tab, a
    keystroke): each start site asked for itself until round 26, and rounds 19 to 25 each
    found one that did not. A writer, and any worker not filed as a read, starts as ever
    and is waited out, so nothing anyone asked to write is dropped.

    Textual's own thread workers are left out (:func:`_textuals_own`): the Onboard view's
    ``DirectoryTree`` lists each folder in one, ``_load_directory``, which writes nothing,
    and one listing a hung mount never ends (round 23).
    """

    def __init__(self, app: Any) -> None:
        super().__init__(app)
        self._lock = threading.Lock()
        self._threads: Counter[str] = Counter()
        self._waited: set[Worker[Any]] = set()
        self._held: dict[tuple[DOMNode, str], tuple[Worker[Any], bool]] = {}
        """The reads held for a pending hand-over, by node and name, each with whether it
        is exclusive (applied when it starts)."""

    def _new_worker(self, work: WorkType[Any], node: Any, *args: Any, **kwargs: Any) -> Worker[Any]:
        # Textual's private method, so what it is given is handed on as it came: a keyword
        # a later 8.x adds to `run_worker` and passes here must reach it, or every worker
        # start raises TypeError (round 25 of #257). Only those read here are named.
        waited = bool(kwargs.get("thread")) and not _textuals_own(work)
        if waited and (callable(work) or inspect.isawaitable(work)):
            # The name and description Textual would give it, read off the work before it
            # is wrapped.
            name = kwargs.get("name") or getattr(work, "__name__", "") or ""
            kwargs.update(name=name, description=kwargs.get("description") or repr(work))
            work = self._counted(_in_its_own_loop(work), name)
        held = (
            waited
            and kwargs.get("start", True)
            and getattr(self._app, "hand_off", None) is not None
            and kwargs.get("name") in _waited_reads()
        )
        if held:
            exclusive = bool(kwargs.pop("exclusive", False))
            worker = super()._new_worker(work, node, *args, **{**kwargs, "start": False})
            replaced = self._held.pop((node, worker.name), None)
            if replaced is not None:
                self._remove_worker(replaced[0])
            self._held[(node, worker.name)] = (worker, exclusive)
            return worker
        worker = super()._new_worker(work, node, *args, **kwargs)
        if waited:
            self._waited.add(worker)
        return worker

    def release(self) -> list[tuple[str, Exception]]:
        """Start each read held for a hand-over now called off, once, for a node still in
        the app and not cancelled since (a newer session's credits, a view removed).

        Each on its own: one that raises is answered, ``(name, error)``, and the rest
        still start. In the key's handler, the first to raise took asq down on the press
        that keeps it open, and the starts after it never ran (round 25 of #257).
        """
        held, self._held = self._held, {}
        failed: list[tuple[str, Exception]] = []
        for (node, name), (worker, exclusive) in held.items():
            if worker.is_cancelled or not node.is_attached:
                self._remove_worker(worker)
                continue
            try:
                if exclusive:
                    for other in list(self):
                        if (
                            other is not worker
                            and other.node is node
                            and other.group == worker.group
                        ):
                            other.cancel()
                self._waited.add(worker)
                worker._start(self._app, self._remove_worker)
            except Exception as exc:
                self._remove_worker(worker)
                failed.append((name, exc))
        return failed

    def drop_held(self) -> None:
        """Forget every held read: asq quits, and a read it never began loses nothing."""
        held, self._held = self._held, {}
        for worker, _exclusive in held.values():
            self._remove_worker(worker)

    def _remove_worker(self, worker: Worker[Any]) -> None:
        self._waited.discard(worker)
        super()._remove_worker(worker)

    def _counted(self, work: Callable[[], Any], name: str) -> Callable[[], Any]:
        def counted() -> Any:
            with self._lock:
                self._threads[name] += 1
            try:
                return work()
            finally:
                with self._lock:
                    self._threads[name] -= 1

        return counted

    def still_running(self) -> set[str]:
        """The names of the thread workers a quit would cut off: still to start, running, or
        cancelled with their callable still running."""
        with self._lock:
            names = set(+self._threads)
        return names | {worker.name for worker in self._waited if not worker.is_finished}


def _in_its_own_loop(work: WorkType[Any]) -> Callable[[], Any]:
    """``work`` as the plain callable its thread runs: an ``async def`` (or a ``partial`` of
    one) or an awaitable is run to its end in an event loop of its own, as Textual runs one
    in a thread (``Worker._run_threaded``); a plain callable is itself."""
    if inspect.iscoroutinefunction(work) or inspect.iscoroutinefunction(
        getattr(work, "func", None)
    ):
        return lambda: asyncio.run(_awaited(work()))  # type: ignore[operator]
    if inspect.isawaitable(work):
        return lambda: asyncio.run(_awaited(work))
    return work


async def _awaited(awaitable: Awaitable[Any]) -> Any:
    return await awaitable


def _textuals_own(work: object) -> bool:
    """Whether ``work`` is one of Textual's own methods; ``@work`` hands over a ``partial``."""
    return str(getattr(getattr(work, "func", work), "__module__", "")).startswith("textual.")


class FleetApp(SelectionHost, inherit_bindings=False):
    """One `asq` view over every project, agent and session.

    A :class:`~aisquare.cli.ui.terminal.SelectionHost`: the press, the release
    and the copy key of a pane selection are the base class's, shared with the
    pane tests' host so the two cannot drift (review of #120, round 9; review
    of #135, findings 7 and 10).
    """

    TITLE = "aisquare"
    COMMAND_PALETTE_BINDING = "f1"
    CSS = """
    #main { height: 1fr; }
    #content { width: 1fr; height: 1fr; }
    """
    BINDINGS: ClassVar = [
        Binding("q", "quit", "quit"),
        Binding("ctrl+q", "quit", "quit", show=False),
        Binding("t", "pick_theme", "theme"),
        # Off the footer, which must fit 80 columns; the ? screen and the palette list it.
        Binding("R", "remote_panel", "remote", show=False),
        Binding("r", "refresh_now", "refresh"),
        Binding("a", "toggle_captured", "captured", show=False),
        Binding("plus", "add_project", "add project", show=False, key_display="+"),
        Binding("w", "welcome", "welcome", show=False),
        Binding("question_mark", "help", "help", key_display="?"),
    ]
    SIDEBAR_ACTIONS: ClassVar[frozenset[str]] = frozenset(
        {
            "quit",
            "pick_theme",
            "remote_panel",
            "refresh_now",
            "command_palette",
            "change_theme",
            "toggle_captured",
            "add_project",
            "welcome",
        }
    )
    """Actions that are live only while focus is in the sidebar (§4.3)."""

    def __init__(
        self,
        *,
        refresh_seconds: float = 2.0,
        doctor: DoctorRunner | None = diagnostics.doctor,
        escape_key: str | None = None,
        accounts: AccountsReader | None = accounts_service.overview,
        headroom: HeadroomReader | None = accounts_service.read_fleet_headroom,
        headroom_seconds: float = HEADROOM_SECONDS,
        remote: RemoteController | None = None,
    ) -> None:
        super().__init__()
        self._workers = self._worker_threads = _WorkerThreads(self)
        """Textual's own manager, which ``self.workers`` reads, and the threads it ran."""
        self._hand_off_since = 0.0
        """When the pending hand-over was asked for (``_hand_off_clock``)."""
        self._hand_off_overdue = False
        """The pending hand-over has waited ``_HAND_OFF_PATIENCE``: a second press calls it off."""
        self._hand_off_poll: Timer | None = None
        self.remote = remote if remote is not None else RemoteController()
        """The Remote (``R``) model — one per app, so the tunnel outlives the dialog."""
        self._remote_savers = {
            key: Autosave(self, key, what=what) for key, what in SWITCHES.items()
        }
        """Remote's switches' saves, and its deadline's, as the theme's (``autosave.py``)."""
        self.remote.save_switch = self._save_remote_switch
        self.remote.on_news = self._post_remote_news
        self.remote.on_done = self._post_remote_done
        # A start saves its switch here once its deadline is written on the controller's
        # writer thread, in order with a turn-off's own save (post_message is thread-safe).
        self.remote.call_back = self.call_later
        self.refresh_seconds = refresh_seconds
        self._doctor = doctor
        self._accounts = accounts
        self.accounts_overview: AccountsOverview | None = None
        """The last Accounts frame that was read; ``None`` before the first or when disabled."""
        self._accounts_worker: Worker[AccountsOverview] | None = None
        """The accounts read in flight, or the last one; only one runs at a time."""
        self._accounts_owed = False
        """A frame was asked for while a read was in flight: read once more after it."""
        self._headroom = headroom
        self.headroom_seconds = headroom_seconds
        self._headroom_worker: Worker[tuple[HeadroomSummary, bool]] | None = None
        """The headroom read in flight, or the last one; only one runs at a time."""
        self._headroom_owed = False
        """A headroom read was asked for while one ran: read once more after it."""
        self._headroom_due = True
        """Read the headroom once the next accounts frame lands: the first frame at start,
        and the one after the Accounts page changed something (``AccountsChanged``)."""
        self.escape_key = escape_key or fleet_service.settings().escape_key
        self.snapshot: FleetSnapshot | None = None
        """The last frame that was read successfully; ``None`` before the first."""
        self.store_error: str | None = None
        """Why the newest refresh kept the previous frame, or ``None`` when it did not."""
        self.doctor_checks: list[DoctorCheck] = []
        """The newest report on the Doctor page; its ✗/⚠ counts are beside *⚙ Settings*."""
        self.doctor_scope: str | None = None
        """The project the doctor answers for when it NEXT runs — the one most recently
        selected; ``None`` = global checks. Selecting changes it and runs nothing: the
        report on screen keeps the root it ran in (:attr:`_doctor_root`), so its fixes
        never run in the project just selected."""
        self._doctor_root: Path | None = None
        """The root the report on screen — or the run under way — is about: the cwd its
        project fixes run in. Set only when a run starts, never by a selection."""
        self._doctor_worker: Worker[Any] | None = None
        """The newest doctor run; an older one's result is not ours to paint."""
        self._theme_restored = False
        self._theme_autosave = theme_autosave(self)
        self.unsaved: list[str] = []
        """What the quit-time flush could not land (a preference each), for ``run_ui`` to say
        once the screen is gone."""
        self.show_captured = False
        """Whether the sidebar also lists the directories sessions merely ran in (#139)."""
        self.capturing = False
        """``[projects] capture``, read at each refresh. Off, the default, the captured rows
        are never listed, whatever ``show_captured`` says, and ``a`` is no key."""
        self._undo: list[project_groups.UndoEntry] = []
        """The layout gestures of this session, newest last; ``u`` reverts the last (#140)."""

    # --- layout -------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Panes(id="main"):
            yield Sidebar(id="sidebar")
            # The partition is a widget, not a border: drag it, or step it with
            # < > = from the sidebar; the width is remembered (#137).
            yield Divider(
                "#sidebar", state_key=SIDEBAR_WIDTH_KEY, what="the navigator's width", id="divider"
            )
            with ContentSwitcher(id="content", initial="welcome"):
                yield WelcomeView(escape_key=self.escape_key, id="welcome")
                # The Onboard view is built on the first `+` (on_add_project): its
                # DirectoryTree scans the home directory and keeps a loader worker
                # alive for its whole life — not a cost to pay at every start-up.
                yield DoctorView(id="doctor", machine=True)
        yield Footer()

    def on_mount(self) -> None:
        restore_theme(self)
        self._theme_restored = True
        self.show_captured = _ui_state(SHOW_CAPTURED_KEY) == "1"
        self.capturing = project_service.capture_on()
        self.refresh_data()
        self.set_interval(self.refresh_seconds, self.refresh_data)
        # The status line's headroom is the usage cache's, on its own slow clock: never
        # on the two-second tick, which would ask the network as often as it went stale.
        # Its first read waits for the first accounts frame, whose signed-out slots it
        # counts (`_accounts_read`); with no accounts reader there is no frame to wait for.
        if self._accounts is None:
            self.refresh_headroom()
        self.set_interval(self.headroom_seconds, self.refresh_headroom)
        # The doctor runs ONCE at start, about the project the restored view shows;
        # a restored Doctor page runs it itself, as opening it does.
        if not self._restore_selection():
            self.run_doctor()
        # wait=False here and below: a Remote that stops runs down on a thread of its own,
        # since stopping uvicorn and ngrok froze the fleet UI for seconds (r3 review of #243).
        self.remote.restore(wait=False)
        self.set_interval(30.0, self._remote_auto_off)
        self.set_interval(30.0, self.remote.revive_tunnel_if_dead)

    # --- what was open (#144) ---------------------------------------------------------

    def _restore_selection(self) -> bool:
        """Reopen the view that was open when the UI last ran, if its row is still there.

        Read from the store's ``ui_state`` (v18), never from a file the UI
        keeps for itself: the theme stays in ``state.json`` because the board
        shares it. A remembered agent whose row has left the frame falls back
        to its project; a project that is gone falls back to the welcome view,
        and the memory is dropped rather than retried every launch.

        The doctor's scope is set here, not when the posted selection lands, so
        the one run at start is about the restored project. Whether the Doctor
        page is the view reopened is the answer: opening it runs the checks.
        """
        remembered = _ui_state(SELECTED_KEY)
        if not remembered or self.snapshot is None:
            return False
        kind, _, ident = remembered.partition(":")
        if kind == "agent":
            project_id, _, agent_id = ident.partition("/")
            if self.snapshot.agent(project_id, agent_id) is not None:
                self.doctor_scope = project_id
                self.post_message(AgentSelected(project_id, agent_id))
                return False
            if self.snapshot.project(project_id) is not None:
                self.doctor_scope = project_id
                self.post_message(ProjectSelected(project_id))
                return False
        elif kind == "project" and self.snapshot.project(ident) is not None:
            self.doctor_scope = ident
            self.post_message(ProjectSelected(ident))
            return False
        elif kind == "settings":
            self.post_message(SettingsSelected())
            return False
        elif kind == "app-settings":
            self.post_message(AppSettingsSelected())
            return False
        elif kind == "accounts":
            self.post_message(AccountsSelected())
            return False
        elif kind == "doctor":
            self.post_message(DoctorSelected(ident or None))
            return True
        _remember_ui_state(SELECTED_KEY, None)
        return False

    def _remember_selection(self, value: str | None) -> None:
        _remember_ui_state(SELECTED_KEY, value)

    @property
    def sidebar(self) -> Sidebar:
        return self.query_one(Sidebar)

    @property
    def content(self) -> ContentSwitcher:
        return self.query_one("#content", ContentSwitcher)

    # --- focus model (§4.3) ---------------------------------------------------------

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """App keys are live only while focus is in the sidebar (or nowhere yet).

        Gated on the sidebar rather than on ``isinstance(focused, TerminalPane)``:
        of everything the views mount only ``Input`` implements
        ``check_consume_key``, so with a ``Button``, ``Select``, ``Switch``,
        ``DataTable`` or ``DirectoryTree`` focused these letters stayed live —
        measured against the running app, ``q`` with the Settings tab's
        permission-mode ``Select`` focused exited the UI and took the unsaved
        form with it. Focus nowhere keeps them live, or the shell would open
        unquittable (at mount the sidebar has focus, so this is only the
        fallback).
        """
        if action == "help":
            # `?` from anywhere it is not typing: the board, a form's button, a list —
            # never an agent's pane, where every key is the agent's. An Input keeps it
            # itself (``check_consume_key``).
            focused = self.focused
            return focused is None or not any(
                isinstance(node, TerminalPane) for node in (focused, *focused.ancestors)
            )
        if action == "toggle_captured" and not self.capturing:
            return False  # nothing is captured, so there is nothing to show (#139)
        if action not in self.SIDEBAR_ACTIONS:
            return True
        focused = self.focused
        # query, not self.sidebar: bindings are inspected while the screen is
        # still composing, and a missing sidebar must not raise from a key press.
        sidebar = next(iter(self.query(Sidebar)), None)
        if focused is None or sidebar is None:
            return True
        return focused is sidebar or sidebar in focused.ancestors

    def on_escape_to_sidebar(self, event: EscapeToSidebar) -> None:
        self.sidebar.focus()

    # --- theme ----------------------------------------------------------------------

    def action_pick_theme(self) -> None:
        self.push_screen(ThemePicker())

    def action_change_theme(self) -> None:
        # The command palette's "Change theme" lands here — route it to the
        # stays-open picker instead of textual's pick-and-close one.
        self.action_pick_theme()

    # --- remote (R) ------------------------------------------------------------------

    def action_remote_panel(self) -> None:
        self.push_screen(RemotePanel(self.remote))

    def _save_remote_switch(self, key: str, value: object) -> None:
        """A Remote switch's save, as the theme's: on a thread of its own, a refusal toasted
        once and said again at quit. Saved where it changed, it waited for ``state.json``'s
        lock on Textual's thread, two seconds a key while another process held it (r3 review
        of #243: nothing that stops or saves Remote may hold that thread)."""
        self._remote_savers[key].remember(value)

    def _post_remote_news(self, text: str, trouble: bool) -> None:
        self.post_message(RemoteNews(text, trouble))

    def _post_remote_done(self, text: str, trouble: bool) -> None:
        self.post_message(RemoteNews(text, trouble, done=True))

    def on_remote_news(self, news: RemoteNews) -> None:
        """Toast what Remote says, unless the R panel is open, whose status line says it:
        a Remote that did not come back at start, a tunnel that did not come up, auto-off.
        Said only there, the human found out from the phone, away from the desk (sweep of
        #243). What a control's write did (a new passphrase, a revoke) is said in any case:
        no line of the panel says it."""
        if not news.done and isinstance(self.screen, RemotePanel):
            return
        severity: SeverityLevel = "warning" if news.trouble else "information"
        self.notify(news.text, title="Remote", severity=severity, timeout=10, markup=False)

    def _remote_auto_off(self) -> None:
        """The 30 s auto-off check; a Remote whose timer ran out stops without this thread."""
        self.remote.enforce_auto_off(wait=False)

    def get_system_commands(self, screen: Screen[Any]) -> Iterable[SystemCommand]:
        yield from super().get_system_commands(screen)
        yield SystemCommand(
            "Remote control",
            "Remote on/off, link + QR, password, devices (R)",
            self.action_remote_panel,
        )

    def watch_theme(self, theme_name: str) -> None:
        # Fires on ANY theme change (our picker or the palette): every change is
        # the save. Not while the saved theme is being restored at mount.
        parent = getattr(super(), "watch_theme", None)
        if parent is not None:
            parent(theme_name)
        if self._theme_restored:
            self._theme_autosave.remember(theme_name)

    def on_unmount(self) -> None:
        # The TUI is leaving: no ngrok may outlive it. The saved switches stay, so a
        # Remote that was on comes back on at the next start (restore()). It stops on a
        # thread of its own, started first so the saves below overlap it; run_ui waits
        # for it once the terminal is back.
        self.remote.shutdown_for_exit(wait=False)
        # Every saver — the theme's here, the divider's — started first and joined
        # against ONE deadline, so quit waits once, not once per preference.
        self.unsaved = Autosave.flush_all(self)

    # --- help / refresh ---------------------------------------------------------------

    def action_help(self) -> None:
        self.push_screen(
            HelpScreen(
                self.escape_key, at_board=self._focus_on_the_board(), capturing=self.capturing
            )
        )

    def _focus_on_the_board(self) -> bool:
        """Whether the focus is on a project's Board tab — its table, or the tab row on it."""
        for node in self.focused.ancestors_with_self if self.focused is not None else ():
            if isinstance(node, BoardPanel):
                return True
            if isinstance(node, ProjectView):
                return node.active == "tab-board"
        return False

    def action_refresh_now(self) -> None:
        """The fleet, now. Not the doctor: it runs at start, when its page opens, after a fix
        and from its Re-run button — seconds of git/tmux/claude probing per run."""
        self.refresh_data()

    def _listing_captured(self) -> bool:
        """Whether the sidebar lists the captured directories: ``a`` showed them, and capture
        is on. A ``show_captured`` saved while it was on lists nothing once it is off."""
        return self.show_captured and self.capturing

    def action_toggle_captured(self) -> None:
        """Show, or hide again, the captured directories the sidebar leaves out (#139).

        Only the project list changes, so only the store is re-read: the doctor's
        findings do not depend on which cards are shown, and ``r`` re-runs it.
        """
        self.show_captured = not self.show_captured
        _remember_ui_state(SHOW_CAPTURED_KEY, "1" if self.show_captured else None)
        self.refresh_data()
        self.notify(
            "showing captured directories too — `a` hides them again"
            if self.show_captured
            else "captured directories hidden — `a` shows them",
            timeout=4,
        )

    # --- data ---------------------------------------------------------------------------

    @while_running
    def refresh_data(self) -> None:
        """Re-read projects and agents; keep (and label) the last frame if the store is busy.

        While asq waits to hand its terminal over too: the reads this starts are held
        (:class:`_WorkerThreads`), and this one ends on the UI thread before any quit.
        """
        sidebar = self.sidebar
        self.capturing = project_service.capture_on()
        try:
            with store_session() as store:
                projects = store.list_projects(all=self._listing_captured())
                groups = store.project_groups()
        except Exception as exc:  # the store is briefly unavailable — keep what is shown
            self.store_error = f"{type(exc).__name__}: {exc}"
            if self.snapshot is None:
                sidebar.show_notice("store unreadable — nothing to show yet")
            else:
                if self.snapshot.stale_since is None:
                    self.snapshot = replace(self.snapshot, stale_since=datetime.now())
                sidebar.show_notice(
                    f"store unreadable — showing the frame from {self.snapshot.taken_at:%H:%M:%S}"
                )
            return
        agents: dict[str, list[FleetAgentStatus]] = {}
        notices: dict[str, str] = {}
        for project in projects:
            try:
                agents[project.id] = fleet_service.list_agents(project)
            except fleet_service.FleetError as exc:
                agents[project.id] = []
                notices[project.id] = f"agents unavailable — {exc}"
            except Exception as exc:  # a bug in the fleet path must not take the view down
                agents[project.id] = []
                notices[project.id] = f"agents unavailable — {type(exc).__name__}: {exc}"
        self.store_error = None
        self.snapshot = FleetSnapshot(projects, agents, notices)
        sidebar.show_notice(None)
        sidebar.show_projects(projects, agents, notices=notices, groups=groups)
        self._feed_open_views(self.snapshot)
        self.refresh_accounts()

    def refresh_accounts(self) -> None:
        """Re-read the Claude accounts OFF the UI thread; the status line and the page follow.

        ``accounts_service.overview`` reads the registry through ``context.db``
        and reconciles it as it reads: a slot ``+ Add`` just made gets its row,
        a vanished one is deleted and the order renumbered. Behind another
        process's write that waits out the store's busy timeout, and this rides
        the two-second tick whatever page is open, so the whole UI froze for as
        long as a writer held the lock. The board, the agent header and the
        Settings tab moved this same call into thread workers (review of #205,
        fourth round); the shell's tick was the caller they missed (final
        review of #203, accounts F2). :meth:`_accounts_read` paints the answer.

        One read at a time. It was an ``exclusive`` worker, so each tick
        cancelled the read still waiting and started another; a thread cannot be
        stopped, so the cancelled read kept its thread and its answer was
        dropped. While the store stayed busy every read outlived the tick,
        nothing was painted, and a thread piled up per tick (review of that fix,
        round 1). A call that finds a read waiting now leaves it be and marks one
        more read owed, which :meth:`_accounts_read` starts once it has painted:
        however many ticks a wait outlives, they cost one read after it, and
        what the page asked for meanwhile (``AccountsChanged`` after a write it
        does not show optimistically) is still read after that write. The usage
        numbers are the view's own, slower business
        (``AccountsView.refresh_usage``).
        """
        if self._accounts is None:
            return
        reading = self._accounts_worker
        if reading is not None and not reading.is_finished:
            # A read held for a pending hand-over has not begun, and reads what this would.
            self._accounts_owed = reading.state is not WorkerState.PENDING
            return
        self._accounts_owed = False
        self._accounts_worker = self.run_worker(
            self._accounts,
            name=ACCOUNTS_WORKER,
            group=ACCOUNTS_WORKER,
            thread=True,
            exit_on_error=False,
        )

    def _accounts_read(self, event: Worker.StateChanged) -> None:
        """Paint the frame the accounts read answered, and the AISquare session beside it.

        The session is a file read, as it always was here; only the registry
        went to the worker. Then the read that was owed while this one waited,
        if one was (:meth:`refresh_accounts`).
        """
        if event.state is WorkerState.SUCCESS:
            result = event.worker.result
            overview = result if isinstance(result, AccountsOverview) else None
        elif event.state is WorkerState.ERROR:
            overview = None  # a directory we cannot read costs the line, never the frame
        else:
            return  # still running, or cancelled with the app: nothing to paint or owe
        sidebar = self.sidebar
        self.accounts_overview = overview
        session_known = True
        try:
            session = read_session()
        except Exception:  # read_session never raises; belt to its braces
            session, session_known = None, False
        sidebar.show_aisquare((session is not None) if session_known else None)
        if overview is not None:
            for view in self.query(AccountsView):
                view.show(overview)
        # The read owed first: an AccountsChanged that landed while this read was in flight
        # asked for the frame AFTER the change, and the headroom it is due waits for that
        # frame — spent on this one, the ⚠ lagged a sign-out by a minute (review of #261, N1).
        if self._accounts_owed:
            self.refresh_accounts()  # the ticks this read outlived: one read, after it
        elif self._headroom_due:
            self._headroom_due = False
            self.refresh_headroom()

    @while_running
    def refresh_headroom(self) -> None:
        """Re-read the status line's headroom OFF the UI thread; :meth:`_headroom_read` paints it.

        Through the usage cache (``claude_accounts.read_fleet_headroom``): the network is
        asked only for an account whose reading is over five minutes old, and never from
        the two-second tick — this runs once the first accounts frame lands, every
        :attr:`headroom_seconds`, after the Accounts page changed something, and when its
        usage read lands (``UsageRead``: the same cache, no request). The accounts are the
        last frame's, so what the dot counts is what the page lists.

        The ⚠ counts every enabled slot that needs a login — the page's *Log in* rows: an
        expired or refused token is in the summary (its reading says so), and a signed-out
        slot, which the reader never asks, is added from the frame (review of #261, m2).

        One read at a time. One asked for while another runs is owed, and made once that
        one has painted, as :meth:`refresh_accounts` does: a login landing mid-read is
        counted then, not a minute later (review of #261, m3).
        """
        if self._headroom is None:
            return
        reading = self._headroom_worker
        if reading is not None and not reading.is_finished:
            self._headroom_owed = True
            return
        self._headroom_owed = False
        overview = self.accounts_overview
        accounts = None if overview is None else [status.account for status in overview.accounts]
        enabled = (
            [] if overview is None else [s for s in overview.accounts if not s.account.disabled]
        )
        signed_out = sum(1 for s in enabled if not s.signed_in)
        read_headroom = self._headroom

        def read() -> tuple[HeadroomSummary, bool]:
            summary = read_headroom(accounts)
            if signed_out:
                needs = summary.needs_login + signed_out
                summary = summary.model_copy(update={"needs_login": needs})
            # Every enabled slot needs a login — the one case the dot is red for want of
            # one; an unreadable slot beside a signed-out one is grey with the ⚠ (N2).
            return summary, bool(enabled) and summary.needs_login >= len(enabled)

        self._headroom_worker = self.run_worker(
            read,
            name=HEADROOM_WORKER,
            group=HEADROOM_WORKER,
            thread=True,
            exit_on_error=False,
        )

    def _headroom_read(self, event: Worker.StateChanged) -> None:
        """Paint the headroom a read answered (a read that failed leaves the dot as it was),
        then make the read that was owed while it ran, if one was."""
        result = event.worker.result
        if event.state is WorkerState.SUCCESS and isinstance(result, tuple):
            summary, every_needs_login = result
            self.sidebar.show_headroom(summary, every_needs_login=every_needs_login)
        if event.state in (WorkerState.SUCCESS, WorkerState.ERROR) and self._headroom_owed:
            self.refresh_headroom()

    def _feed_open_views(self, snapshot: FleetSnapshot) -> None:
        """Hand every open Project/Agent view its row from the new frame.

        The views are built once and kept (see the module docstring), so without
        this an agent's header would show the state it had when first clicked.
        The attribute is the scaffold's contract; ``show`` is called when a view
        offers one (the views are another work package's), and a view whose row
        has left the frame keeps what it has — the pane it renders is still real.

        **Only a frame that actually answered is pushed.** ``refresh_data``
        records ``agents[id] = []`` plus a notice when a fleet read failed open,
        and that empty list is indistinguishable from "this project has no
        manager": handed on, ``ManagerTab.show(None)`` detaches the LIVE
        manager's pane, hides it, paints "<name> has no manager yet" with a
        Start-manager button and takes the focus the user was typing into with
        it (measured: ``pane_id`` %7 → ``None``, focus ``TerminalPane`` →
        ``Button``, for one momentarily locked sqlite db). So a noticed project
        keeps what its view has; what failing open costs here is a manager
        header that ages until the next read succeeds, while the sidebar carries
        the reason on the project's own card.
        """
        for welcome in self.query(WelcomeView):
            welcome.note_frame()  # shown or not: what step 3's refusals were about still stands
        for agent_view in self.query(AgentView):
            fresh = snapshot.agent(agent_view.status.agent.project_id, agent_view.status.agent.id)
            if fresh is not None and fresh != agent_view.status:
                agent_view.refresh_status(fresh)
        for project_view in self.query(ProjectView):
            fresh_project = snapshot.project(project_view.project.id)
            if fresh_project is None:
                continue  # the row has left the frame: keep what the view has
            if fresh_project != project_view.project:
                # A new codename, root or nickname: the view re-reads what depends on it.
                project_view.refresh_project(fresh_project)
            if snapshot.notices.get(fresh_project.id) is not None:
                continue  # we could not ask — not "the fleet answered: none"
            # ProjectView.show() is the MANAGER status renderer; the snapshot
            # goes through refresh_status, which picks the manager out of it.
            project_view.refresh_status(snapshot.agents.get(fresh_project.id, []))

    # --- doctor -------------------------------------------------------------------------

    def _scoped_project(self) -> ProjectInfo | None:
        """The project the next doctor run is about, or ``None`` for the global checks.

        One place for it: :meth:`run_doctor` takes its root as the doctor's ``cwd``
        and keeps the same root as :attr:`_doctor_root`, the cwd the report's
        project-scoped fix buttons run in — the two must never disagree.
        """
        snapshot = self.snapshot
        if snapshot is None or not self.doctor_scope:
            return None
        return snapshot.project(self.doctor_scope)

    def run_doctor(self) -> None:
        """Run the checks for :attr:`doctor_scope` off the UI thread; the worker state paints them.

        Called at start, when the Doctor page opens, after a one-click fix and
        from the page's Re-run button — nowhere else: a run is seconds of
        git/tmux/claude probing, and selecting a project or pressing ``r`` must
        not pay for it.

        The result carries the SCOPE it was run for, the newest run is
        remembered, and :attr:`_doctor_root` — what the report's fixes run in —
        moves only here. Without all three, a report that finished after the
        scope had changed was painted with the new scope's root, and a one-click
        project fix would have run in the wrong project.
        """
        if self._doctor is None:
            return
        injected: DoctorRunner = self._doctor
        project = self._scoped_project()
        scope: Path | None = project.root if project is not None else None
        self._doctor_root = scope
        # An injected fake keeps its own signature and stays global; it is still
        # tagged with the scope its report is ABOUT, which is what the page and
        # the fix buttons are keyed on.
        runner: Callable[[], _DoctorReport] = lambda: (scope, injected())  # noqa: E731
        if project is not None and self._doctor is diagnostics.doctor:
            # Per-project checks: the real doctor takes the project's root as
            # cwd (services.diagnostics.doctor(cwd=...)).
            runner = lambda: (scope, diagnostics.doctor(cwd=scope))  # noqa: E731 — worker target
        self._doctor_worker = self.run_worker(
            runner,
            name=_DOCTOR_WORKER,
            group=_DOCTOR_WORKER,
            exclusive=True,
            thread=True,
            exit_on_error=False,
        )
        self.query_one("#doctor", DoctorView).show_running(self._doctor_subject(scope))

    def _doctor_subject(self, root: Path | None) -> str:
        """What a report about ``root`` is about, as the Doctor page's header names it: the
        project's name, or "this machine" for the global checks."""
        if root is None:
            return "this machine"
        snapshot = self.snapshot
        project = next((p for p in snapshot.projects if p.root == root), None) if snapshot else None
        return project_service.display_name(project) if project is not None else root.name

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        """Paint what the app's own workers answered: the accounts read and the doctor.

        Nothing once asq has stopped running. A thread worker that finishes as the app
        shuts down delivers its answer all the same, after the frame it would paint has
        gone, and the first query for it raised NoMatches out of this handler: the
        accounts read's on windows-latest (round 25 of #257), and a doctor run's in
        ``show_doctor`` and ``show_doctor_failure`` (round 26). One check here answers for
        every worker the app runs. A view's own answers need none: Textual handles its
        messages only until it is pruned, and prunes its children after that.
        """
        if not self.is_running:
            return
        if event.worker.name == PROJECT_REMOVE_WORKER:
            self._project_removal(event)
            return
        if event.worker.name == ACCOUNTS_WORKER:
            # Every read's answer, not only the newest read's: a worker is finished
            # before its StateChanged is handled, so a tick handled in between
            # starts the next read, and the answer that then arrived was dropped
            # (review of the #203 accounts fix, round 2). Reads never overlap
            # (`refresh_accounts`), so answers arrive in the order they were read.
            self._accounts_read(event)
            return
        if event.worker.name == HEADROOM_WORKER:
            self._headroom_read(event)
            return
        if event.worker.name != _DOCTOR_WORKER:
            return
        if event.worker is not self._doctor_worker:
            return  # a superseded run: the newest one owns the page
        if event.state is WorkerState.SUCCESS:
            report = _doctor_report(event.worker.result)
            if report is None or report[0] != self._doctor_root:
                return  # a report about a scope the newest run left is not ours to paint
            self.show_doctor(report[1])
        elif event.state is WorkerState.ERROR:
            self.show_doctor_failure(f"{type(event.worker.error).__name__}: {event.worker.error}")

    def show_doctor(self, checks: list[DoctorCheck]) -> None:
        """Paint a report about :attr:`_doctor_root` on the Doctor page, and its counts beside
        *⚙ Settings* and the hub's Doctor link."""
        self.doctor_checks = checks
        self._feed_doctor(
            self.query_one("#doctor", DoctorView),
            checks,
            self._doctor_root,
            self._doctor_subject(self._doctor_root),
        )
        self._show_doctor_findings()

    def _show_doctor_findings(self) -> None:
        """The newest report's ✗/⚠ counts on *⚙ Settings* and on the hub's Doctor link.

        The doctor runs at start with its page hidden, and these are the only places
        that say what it found before someone opens the page (review of #261, m9).
        """
        failing = sum(check.status is CheckStatus.fail for check in self.doctor_checks)
        warning = sum(check.status is CheckStatus.warn for check in self.doctor_checks)
        self.query_one(SettingsRow).show_findings(failing, warning)
        for hub in self.query(SettingsHubView):
            hub.show_findings(failing, warning)

    @staticmethod
    def _feed_doctor(
        view: DoctorView, checks: list[DoctorCheck], root: Path | None, about: str
    ) -> None:
        """Hand a Doctor view the report, what it is about AND the root its project fixes run in.

        Without the root every ``scope == "project"`` fix renders disabled with
        "(select a project first)" — while a project is selected — so the
        one-click fix of §0 item 4 never worked from the shell. ``show(cwd=None)``
        means "keep the cwd you have", so the global scope is set on the
        attribute: a project's root must not linger once the report is the
        machine-wide one, or a button would run in a project the user has left.
        """
        view.cwd = root
        view.show(checks, about=about)

    def show_doctor_failure(self, reason: str) -> None:
        """Doctor itself crashed: say so on the Doctor page, as its one finding."""
        self.doctor_checks = [
            DoctorCheck(
                name="doctor",
                status=CheckStatus.fail,
                detail=f"the checks crashed: {reason}",
                fix="Run it in a terminal for the full traceback: aisquare doctor",
            )
        ]
        self.query_one("#doctor", DoctorView).show(
            self.doctor_checks, about=self._doctor_subject(self._doctor_root)
        )
        self._show_doctor_findings()

    # --- selection → content ----------------------------------------------------------

    async def _show(self, view_id: str, factory: Callable[[], Widget] | None = None) -> None:
        """Switch the right pane to ``view_id``, building it with ``factory`` the first time."""
        switcher = self.content
        try:
            switcher.get_child_by_id(view_id)
        except NoMatches:
            if factory is None:
                return
            await switcher.add_content(factory(), set_current=True)
        else:
            switcher.current = view_id

    async def on_add_project(self, event: AddProject) -> None:
        await self._show("onboard", lambda: OnboardView(id="onboard"))
        self.sidebar.select(None)

    async def on_project_selected(self, event: ProjectSelected) -> None:
        project = self.snapshot.project(event.project_id) if self.snapshot else None
        if project is None:
            self.notify("that project is no longer listed", severity="warning", timeout=4)
            return
        view_id = f"project-{project.id}"
        await self._show(view_id, lambda: ProjectView(project, id=view_id))
        self.sidebar.select(f"project:{project.id}")
        self.doctor_scope = project.id
        self._remember_selection(f"project:{project.id}")

    async def on_agent_selected(self, event: AgentSelected) -> None:
        status = self.snapshot.agent(event.project_id, event.agent_id) if self.snapshot else None
        if status is None:
            self.notify("that agent is no longer listed", severity="warning", timeout=4)
            return
        view_id = f"agent-{status.agent.id}"
        await self._show(view_id, lambda: AgentView(status, id=view_id))
        self.sidebar.select(f"agent:{status.agent.id}")
        self.doctor_scope = event.project_id
        self._remember_selection(f"agent:{event.project_id}/{status.agent.id}")
        self._focus_pane(view_id)

    def _focus_pane(self, view_id: str) -> None:
        """Give the agent just selected the keyboard (#147).

        Selecting a row showed the pane and left focus in the sidebar, where the
        app's bindings are live: a sentence typed at what looked like Claude Code
        quit the UI on its first ``q``. The pane takes focus once the frame that
        shows it has been drawn — a view mounted this instant has no size to
        focus into yet — and ``F12`` stays the deliberate way back.
        """
        try:
            view = self.content.get_child_by_id(view_id)
        except NoMatches:
            return
        pane = getattr(view, "pane", None)
        if isinstance(pane, TerminalPane):
            self.call_after_refresh(pane.focus)

    async def on_agent_restarted(self, event: AgentRestarted) -> None:
        """A restart minted a new row (#138): say so, and show it where the old one was — but
        only from the old row's own view, still on screen.

        A restart takes seconds, and by its end the user may be on another agent: the new
        row was shown, selected and focused all the same, which took the screen back to
        it, with its Stop and Restart under a pointer aimed at the other pane (user
        feedback, 2026-10-19: "it takes me back to manager 0's pane … that had destroyed
        my manager session on more than one occasion"). Anywhere else, the view, the
        sidebar's highlight, the remembered selection and the focus stay where the user
        put them; the sidebar's next frame lists the new row, and the toast names the
        project, as one ``manager`` reads like another's.

        The view that posted this refreshed the frame first, so the row is
        normally in the snapshot already; one more read covers a store that
        was briefly busy. A row still missing is reported, not invented — the
        next tick lists it. The new pane takes the keyboard, as a selected
        agent's does (#147): the Restart button that had it went with the view
        it was on, and focus was left with nobody — where the app's keys are
        live, so a ``q`` typed at the restarted agent quit the UI.
        """
        receipt = event.receipt
        started = event.agent
        project = self.snapshot.project(started.project_id) if self.snapshot else None
        named = agent_name(started.label, project)
        self.notify(f"✓ restarted {named} — {receipt.how}", timeout=6, markup=False)
        for note in receipt.notes:
            self.notify(note, severity="warning", timeout=8, markup=False)
        if self.content.current != f"agent-{event.replaced.id}":
            return  # the user is elsewhere: nothing of theirs moves
        status = self.snapshot.agent(started.project_id, started.id) if self.snapshot else None
        if status is None:
            self.refresh_data()
            status = self.snapshot.agent(started.project_id, started.id) if self.snapshot else None
        if status is None:
            self.notify(
                f"{started.label} restarted — its row appears on the next refresh",
                timeout=5,
                markup=False,
            )
            return
        view_id = f"agent-{status.agent.id}"
        await self._show(view_id, lambda: AgentView(status, id=view_id))
        self.sidebar.select(f"agent:{status.agent.id}")
        self.doctor_scope = started.project_id
        self._remember_selection(f"agent:{started.project_id}/{status.agent.id}")
        self._focus_pane(view_id)

    # --- groups, pins and order (#140) -------------------------------------------------

    def _layout(self, what: Callable[[ContextStore], project_groups.UndoEntry], said: str) -> None:
        """Apply one gesture through the service, remember its way back, repaint, say so."""
        try:
            with store_session() as store:
                entry = what(store)
        except KeyError as exc:
            self.notify(f"nothing to do: {exc.args[0]!r} is gone", severity="warning", timeout=4)
            return
        except ValueError as exc:
            self.notify(str(exc), severity="error", timeout=6, markup=False)
            return
        except Exception as exc:  # the store said no: the frame stands, the gesture is lost
            self.notify(f"could not {said}: {exc}", severity="error", timeout=6, markup=False)
            return
        if entry.projects or entry.groups:
            # A gesture that touched no row ("nothing to move": a step at an end,
            # a pinned row) is no step back — `u` would undo nothing and say it did.
            self._undo.append(entry)
            del self._undo[:-UNDO_DEPTH]
        self.refresh_data()

    def on_move_row(self, event: MoveRow) -> None:
        if event.kind == "group":
            self._layout(lambda s: project_groups.step_group(s, event.ident, event.delta), "move")
        else:
            # One step is one row ON SCREEN: the captured rows count only while `a` shows them.
            shown = self._listing_captured()
            self._layout(
                lambda s: project_groups.step(s, event.ident, event.delta, all=shown), "move"
            )

    def on_toggle_pin(self, event: TogglePin) -> None:
        def flip(store: ContextStore) -> project_groups.UndoEntry:
            if event.kind == "group":
                group = store.get_project_group(event.ident)
                if group is None:
                    raise KeyError(event.ident)
                return project_groups.pin_group(store, event.ident, group.pinned_at is None)
            project = store.update_project_layout(event.ident)
            return project_groups.pin(store, event.ident, project.pinned_at is None)

        self._layout(flip, "pin")

    def on_toggle_collapse(self, event: ToggleCollapse) -> None:
        def fold(store: ContextStore) -> project_groups.UndoEntry:
            group = store.get_project_group(event.group_id)
            if group is None:
                raise KeyError(event.group_id)
            return project_groups.set_collapsed(store, event.group_id, not group.collapsed)

        self._layout(fold, "fold")

    def on_drop_project(self, event: DropProject) -> None:
        ids = list(event.project_ids)

        def drop(store: ContextStore) -> project_groups.UndoEntry:
            entry = project_groups.UndoEntry(f"move {len(ids)} project(s)")
            before = event.before
            # One gesture, one transaction, as the service's own changes are: a move
            # the store refuses part-way leaves the moves before it undone too, so no
            # part of the drop lands without its entry (review of #203).
            with store.layout_change():
                for project_id in ids:
                    part = project_groups.move_project(
                        store, project_id, to=event.scope or project_groups.TOP, before=before
                    )
                    for pid, layout in part.projects.items():
                        entry.projects.setdefault(pid, layout)
            return entry

        self._layout(drop, "move")
        self.sidebar.action_clear_marks()

    def on_drop_group(self, event: DropGroup) -> None:
        self._layout(
            lambda s: project_groups.move_group(s, event.group_id, before=event.before), "move"
        )

    def on_group_projects(self, event: GroupProjects) -> None:
        """``g`` / ``shift+g``: the picker, then the move it chose."""
        ids = list(event.project_ids)
        try:
            with store_session() as store:
                groups = [g for g in store.project_groups()]
        except Exception as exc:
            self.notify(f"could not read the groups: {exc}", severity="error", markup=False)
            return

        def chosen(choice: str | None) -> None:
            if choice is None:
                return
            if choice == "ungroup":
                self._layout(lambda s: project_groups.remove_from_group(s, ids), "ungroup")
            elif choice.startswith("new:"):
                name = choice[4:]
                self._layout(lambda s: project_groups.create_group(s, name, ids)[1], "group")
            elif choice.startswith("group:"):
                gid = choice[6:]
                self._layout(lambda s: project_groups.add_to_group(s, gid, ids), "group")
            self.sidebar.action_clear_marks()

        self.push_screen(GroupPicker(groups, len(ids)), chosen)

    def on_undo_layout(self, event: UndoLayout) -> None:
        if not self._undo:
            self.notify("nothing to undo", timeout=3)
            return
        entry = self._undo.pop()
        try:
            with store_session() as store:
                done = project_groups.undo(store, entry)
        except Exception as exc:
            # Back on the stack: an undo is one transaction (`project_groups.undo`), so
            # one the store refused changed nothing, and popped for good it was the way
            # back lost with nothing done — `u` again said "nothing to undo" (review of #203).
            self._undo.append(entry)
            self.notify(f"could not undo: {exc}", severity="error", markup=False)
            return
        self.refresh_data()
        self.notify(f"undid: {done}", timeout=4, markup=False)

    # --- a card's name and menu ------------------------------------------------------------

    def on_rename_project(self, event: RenameProject) -> None:
        """A card's name field was saved: write the nickname, as ``aisquare project rename``.

        On this thread, as every sidebar gesture's write is (:meth:`_layout`): one row,
        and the card already says the new name. A refusal says why and reopens the
        field with what was typed, to correct.
        """
        project = self.snapshot.project(event.project_id) if self.snapshot else None
        if project is None:
            self.notify("that project is no longer listed", severity="warning", timeout=4)
            return
        try:
            renamed = project_service.set_nickname(project, event.typed)
        except KeyError:
            self.notify("that project is no longer listed", severity="warning", timeout=4)
            self.refresh_data()
            return
        except Exception as exc:  # too long, a control character, or the store said no
            self.notify(f"not renamed: {exc}", severity="error", timeout=6, markup=False)
            self.refresh_data()  # the card says the stored name again
            self.sidebar.open_rename(project.id, event.typed)
            return
        self.post_message(ProjectRenamed(renamed))

    def on_project_renamed(self, event: ProjectRenamed) -> None:
        """A nickname was written — a card, its menu, a Settings tab: every name says it now."""
        self.refresh_data()

    def on_open_project_menu(self, event: OpenProjectMenu) -> None:
        """⋯, ``.`` or a right-click on a card: its menu, then what was chosen in it."""
        project = self.snapshot.project(event.project_id) if self.snapshot else None
        if project is None:
            self.notify("that project is no longer listed", severity="warning", timeout=4)
            return
        project_id = project.id

        def confirmed(yes: bool | None) -> None:
            if yes:
                self._remove_project(project_id)

        def chosen(choice: str | None) -> None:
            if choice == RENAME:
                # Once the menu has gone: closing it hands the focus back to the sidebar.
                self.call_after_refresh(self.sidebar.open_rename, project_id)
            elif choice == PIN:
                self.on_toggle_pin(TogglePin("project", project_id))
            elif choice == SETTINGS:
                self.call_later(self._open_settings, project_id)
            elif choice == REMOVE:
                self.push_screen(ConfirmRemove(project), confirmed)

        self.push_screen(ProjectMenu(project, event.anchor), chosen)

    async def _open_settings(self, project_id: str) -> None:
        """The menu's *Open settings*: the project's page, on its Settings tab."""
        await self.on_project_selected(ProjectSelected(project_id))
        try:
            view = self.content.get_child_by_id(f"project-{project_id}")
        except NoMatches:
            return
        if isinstance(view, ProjectView):
            view.active = "tab-settings"

    def _remove_project(self, project_id: str) -> None:
        """*Remove from fleet…*, confirmed: ``aisquare project forget``, off the UI thread.

        Refused (``ProjectBusyError``) while the project has live agents; the toast
        names them and the way out.
        """
        project = self.snapshot.project(project_id) if self.snapshot else None
        if project is None:
            self.notify("that project is no longer listed", severity="warning", timeout=4)
            return
        target = project
        self.run_worker(
            lambda: project_service.forget_project(target),
            name=PROJECT_REMOVE_WORKER,
            group=PROJECT_REMOVE_WORKER,
            thread=True,
            exit_on_error=False,
        )

    def _project_removal(self, event: Worker.StateChanged) -> None:
        result = event.worker.result
        if event.state is WorkerState.SUCCESS and isinstance(result, ProjectForgetReport):
            removed = result.project
            back = project_service.onboard_command(removed.root)
            self.notify(
                f"✓ {project_service.display_name(removed)} removed from the fleet — "
                f"its folder and history are kept; {back} brings it back",
                timeout=8,
                markup=False,
            )
            self.call_later(self._forget_views, removed.id)
        elif event.state is WorkerState.ERROR:
            error = event.worker.error
            said = (
                str(error)
                if isinstance(error, project_service.ProjectBusyError)
                else (f"could not remove it: {type(error).__name__}: {error}")
            )
            self.notify(said, title="not removed", severity="error", timeout=12, markup=False)

    async def _forget_views(self, project_id: str) -> None:
        """A removed project leaves the screen: its card with the next frame, its page and
        its agents' panes now. What was open on it moves to the next project in the list,
        else the one before it, else Welcome."""
        order = [card.project.id for card in self.sidebar.query(ProjectCard)]
        self.refresh_data()
        views = [
            view
            for view in self.content.children
            if view.id == f"project-{project_id}"
            or (isinstance(view, AgentView) and view.status.agent.project_id == project_id)
        ]
        if self.current_view() in views:
            at = order.index(project_id) if project_id in order else len(order)
            nearby = [*order[at + 1 :], *reversed(order[:at])]
            listed = [pid for pid in nearby if self.snapshot and self.snapshot.project(pid)]
            if listed:
                await self.on_project_selected(ProjectSelected(listed[0]))
            else:
                await self.action_welcome()
        for view in views:
            await view.remove()

    def on_spawn_agent(self, event: SpawnAgent) -> None:
        """The sidebar's spawn-agent row: the Spawn dialog for THAT row's project."""
        project = self.snapshot.project(event.project_id) if self.snapshot else None
        if project is None:
            self.notify("that project is no longer listed", severity="warning", timeout=4)
            return
        self.push_screen(
            SpawnDialog(project, accounts=self._accounts), callback=self.spawn_finished
        )

    def spawn_finished(self, receipt: fleet_service.SpawnReceipt | None) -> None:
        """The dialog closed: toast the receipt and its notes, then show the new agent.

        The Project view's Start-manager toasts, word for word. ``markup=False``
        on every one: a note can carry a path or a branch with brackets in it.
        """
        if receipt is None:
            return
        agent = receipt.agent
        self.notify(
            f"✓ spawned {agent.label} ({agent.id}) → {receipt.tmux_session} {agent.pane_id}",
            timeout=6,
            markup=False,
        )
        for note in receipt.notes:
            self.notify(note, severity="warning", timeout=8, markup=False)
        self.refresh_data()
        self.post_message(AgentSelected(agent.project_id, agent.id))

    def on_stop_agent(self, event: StopAgent) -> None:
        """The agent view's Stop button, or ``x`` on the selected row: one question first.

        A frame whose read of the agent's project failed lists none of its agents
        (``refresh_data`` fails open with the cost shown), and Stop was then refused as
        "no longer listed" for an agent that still ran (review of #240, "also confirmed":
        Stop after a failed refresh). Its open view still holds the status it last
        showed, and the dialog asks the service about that row, which decides.
        """
        project = self.snapshot.project(event.project_id) if self.snapshot else None
        status = self.snapshot.agent(event.project_id, event.agent_id) if self.snapshot else None
        if (
            status is None
            and self.snapshot is not None
            and event.project_id in self.snapshot.notices
        ):
            view = self.current_view()
            if isinstance(view, AgentView) and view.status.agent.id == event.agent_id:
                status = view.status
        if project is None or status is None:
            self.notify("that agent is no longer listed", severity="warning", timeout=4)
            return
        self.push_screen(StopAgentScreen(project, status), callback=self.stop_finished)

    def stop_finished(self, agent: FleetAgent | None) -> None:
        """The dialog closed: toast the ended row, re-read, and leave the view it stopped.

        ``None`` is Cancel, or a refusal the dialog is still showing — nothing
        happened, so nothing is said. Its own view would otherwise keep polling
        a pane that is gone, so the shell goes back to the project the way a
        click on the project's title does.
        """
        if agent is None:
            return
        self.notify(f"✓ stopped {agent.label} ({agent.id})", timeout=6, markup=False)
        self.refresh_data()
        view = self.current_view()
        if isinstance(view, AgentView) and view.status.agent.id == agent.id:
            self.post_message(ProjectSelected(agent.project_id))

    def on_spawn_completed(self, event: SpawnCompleted) -> None:
        """A Spawn dialog opened from a persona's *Attach to new*: the same receipt path."""
        self.spawn_finished(event.receipt)

    async def on_new_account_requested(self, event: NewAccountRequested) -> None:
        """The picker's *+ New account*: the Accounts page, its add-account flow started."""
        await self.on_accounts_selected(AccountsSelected())
        self.query_one("#accounts", AccountsView).begin_claude_sign_in(None)

    async def on_settings_selected(self, event: SettingsSelected) -> None:
        """*⚙ Settings*: the hub, with the keyboard on its first link when it came from the
        keyboard (the sidebar had it)."""
        from_keyboard = self.focused is self.sidebar
        await self._show("settings-hub", lambda: SettingsHubView(id="settings-hub"))
        self._show_doctor_findings()  # a hub built just now has not been told yet
        self.sidebar.select(SETTINGS_KEY)
        self._remember_selection("settings")
        if from_keyboard:
            self.query_one("#settings-hub", SettingsHubView).focus_first()

    async def on_app_settings_selected(self, event: AppSettingsSelected) -> None:
        """The hub's *App settings*: the machine-wide settings, under *⚙ Settings*."""
        await self._show("app-settings", lambda: AppSettingsView(id="app-settings"))
        self.sidebar.select(SETTINGS_KEY)
        self._remember_selection("app-settings")

    async def on_accounts_selected(self, event: AccountsSelected) -> None:
        """The Accounts page — from the hub or the status line; *⚙ Settings* stays the
        highlighted row either way, the page being one of Settings' own."""
        await self._show(
            "accounts", lambda: AccountsView(escape_key=self.escape_key, id="accounts")
        )
        self.sidebar.select(SETTINGS_KEY)
        self._remember_selection("accounts")
        if self.accounts_overview is not None:
            self.query_one("#accounts", AccountsView).show(self.accounts_overview)

    def on_accounts_changed(self, event: AccountsChanged) -> None:
        """The page added, removed or signed in something: the status line follows once the
        frame that shows it is read (a slot signed out or in counts in the ⚠ from it)."""
        if self._accounts is None:
            self.refresh_headroom()
            return
        self._headroom_due = True
        self.refresh_accounts()

    def on_usage_read(self, event: UsageRead) -> None:
        """The Accounts page read usage (a tick, or *↻ Refresh usage*): the dot reads the
        same cache now, at no request, rather than up to a minute later (review of #261, m3)."""
        self.refresh_headroom()

    async def on_doctor_selected(self, event: DoctorSelected) -> None:
        """Open the Doctor page and run the checks — opening it is what asks for them.

        About ``event.project_id``, or, when that is ``None``, the project most
        recently selected (the machine's checks when there is none).
        """
        scope = event.project_id if event.project_id is not None else self.doctor_scope
        await self._show("doctor")
        self.sidebar.select(SETTINGS_KEY)
        self.doctor_scope = scope
        self._remember_selection(f"doctor:{scope or ''}")
        self.run_doctor()

    def on_doctor_view_rerun(self, event: DoctorView.Rerun) -> None:
        """The Doctor page's Re-run: the checks again, for :attr:`doctor_scope` — the scope
        the page's report is about, since opening the page is what last set it."""
        event.stop()
        self.run_doctor()

    async def on_project_onboarded(self, event: ProjectOnboarded) -> None:
        # Asked BEFORE the switch below, which hides whatever the content pane had
        # focused: only a keyboard still on the Onboard view (or nowhere) is handed on.
        focused = self.focused
        onboarding = focused is None or any(isinstance(n, OnboardView) for n in focused.ancestors)
        self.refresh_data()
        await self.on_project_selected(ProjectSelected(event.project_id))
        if onboarding:
            self.call_after_refresh(self._hand_on_after_onboarding, f"project-{event.project_id}")

    def _hand_on_after_onboarding(self, view_id: str) -> None:
        """The Onboard view kept the keyboard on its path box, hidden now: give it the next step.

        Keys typed there went nowhere anyone could see, and the footer had nothing
        to offer. The project's *Start manager* takes it, else the sidebar. Only
        asked when the keyboard was on the Onboard view as onboarding finished; one
        the user has put on a visible widget since then is left where it is.
        """
        chain = self.screen.focus_chain
        if self.focused is not None and self.focused in chain:
            return
        try:
            view = self.content.get_child_by_id(view_id)
        except NoMatches:
            view = None
        start = (
            next((w for w in view.query("#start-manager") if w in chain), None) if view else None
        )
        (start or self.sidebar).focus()

    def on_onboard_failed(self, event: OnboardFailed) -> None:
        # markup=False: the message carries a path the user chose, and a toast
        # parses markup by default — ``/home/me/[archive]/repo`` would reach the
        # screen as ``/home/me//repo`` and name a directory that did not fail.
        self.notify(f"{event.path}: {event.reason}", severity="error", timeout=8, markup=False)

    def action_add_project(self) -> None:
        """``+`` from the sidebar: what a click on the ``+`` beside Fleet does."""
        self.post_message(AddProject())

    async def action_welcome(self) -> None:
        """``w`` from the sidebar: back to the Welcome page, with nothing selected."""
        await self._show("welcome")
        self.sidebar.select(None)
        self._remember_selection(None)
        # The page says which project step 1 is on from its ``on_show``, but Textual sends
        # ``Show`` by comparing one layout with the next: a page hidden and shown again
        # before the next layout pass is sent none, and the doctor kept the scope of the
        # page just left (~1 in 6 runs of the welcome tests). Asked here as well.
        self.query_one(WelcomeView).tell_chosen()

    def on_welcome_view_progress(self, event: WelcomeView.Progress) -> None:
        """Welcome added a project or started an agent: the sidebar shows it now."""
        self.refresh_data()

    def on_welcome_view_connected(self, event: WelcomeView.Connected) -> None:
        """Welcome ran the doctor's Connect: the checks run again, as after a Doctor-view fix."""
        self.run_doctor()

    def on_welcome_view_chosen(self, event: WelcomeView.Chosen) -> None:
        """While Welcome is shown, the Doctor answers for the project its step 2 answers for."""
        if self.content.current == "welcome":
            self.doctor_scope = event.project_id

    async def action_quit(self) -> None:
        """``q`` and ``ctrl+q``: quit, unless Settings ▸ Fleet's restart or stop of every agent
        still runs, which is refused with a toast as Update and Uninstall refuse it.

        A quit cannot stop its thread: the screen went, ``asyncio.run`` joined the thread
        with the terminal blank for as long as the rest of the agents took, minutes for a
        restart of all, and each agent's toast after the quit was lost (review of #266,
        round 3). An agent view's own Stop or Restart, a few seconds, is left to the quit.
        """
        from aisquare.cli.ui.views.fleet_bulk import BULK_WORKER

        running = self._work_running({BULK_WORKER: _writing_workers()[BULK_WORKER]})
        if running is not None:
            self.notify(f"{running} is still running — quit when it ends", severity="warning")
            return
        await super().action_quit()

    hand_off: tuple[str, ...] | None = None
    """The ``aisquare`` command this terminal goes to when asq quits (``run_ui``)."""

    def on_doctor_view_hand_off(self, event: DoctorView.HandOff) -> None:
        """Update or Uninstall in the Doctor view: quit, and let ``run_ui`` hand over.

        Never under work that is still writing, which quitting would cut off (asyncio.run
        then joins its thread with the terminal blank, and the hand-over can replace the
        install under it): any worker :func:`_writing_workers` names, in any view, shown
        or not (rounds 15 and 16 of #257), is refused with a toast.

        Every other worker still running is waited out instead (:meth:`_holding_the_quit`):
        the reads asq starts on its own schedule, the accounts read on every two-second
        tick. Refused for them, Update and Uninstall were refused for much of the time,
        and a retry had the same odds (round 20). Waited for by default, not by a list of
        the reads that write: rounds 19 to 22 each found a read, filed as one that writes
        nothing, that opened the store or wrote a cache. None starts once ``hand_off`` is
        set (:class:`_WorkerThreads` holds them), so none can start between the last look
        and the quit either.

        A read that never ends (a store on a hung mount) would hold the quit for good, with
        nothing on screen after the first toast: past ``_HAND_OFF_PATIENCE`` the wait names
        what still runs, and a second press calls the hand-over off (round 23).
        """
        if self.hand_off is not None:
            if self._hand_off_overdue:
                self._call_off_hand_off()
            return  # already handing over, once the reads running end
        running = self._work_running(_writing_workers())
        if running is not None:
            self.notify(f"{running} is still running — try again when it ends", severity="warning")
            return
        self.hand_off = event.args
        self._hand_off_since = _hand_off_clock()
        self._hand_off_overdue = False
        if self._holding_the_quit():
            self.notify("asq quits once the reads running end", timeout=4)
        self._exit_when_read()

    def _exit_when_read(self) -> None:
        """Quit once no worker holds the quit; until then, look again shortly.

        The writers are waited for here too: one started from a page while the reads
        were waited out is cut off by a quit as much as any.

        A hand-over called off ends the polling here, not only by the timer's stop: the timer
        queues each poll (``set_timer`` goes by ``call_next``), and a poll already queued when
        the second press was handled would quit asq with nothing to hand over (round 24).
        """
        if self.hand_off is None:
            return  # called off (_call_off_hand_off): a poll queued before the press ends here
        running = self._holding_the_quit()
        if not running:
            self._worker_threads.drop_held()
            if self.hand_off[:1] == ("uninstall",):
                # Off, not only stopped as a quit leaves it: every phone signed out (its
                # farewell drained before the exec, run_ui) and the switch saved off. Stopped,
                # remote.json kept the devices, the link and the passphrase past an Uninstall
                # without --purge, and the first asq after a reinstall brought Remote back,
                # its phones let in by their cookies (review of #259, remote focus, 12/12).
                # An Update's asq --reopen brings it back, read-only.
                self.remote.turn_off(wait=False)
            self.exit()
            return
        waited = _hand_off_clock() - self._hand_off_since
        if not self._hand_off_overdue and waited >= _HAND_OFF_PATIENCE:
            self._hand_off_overdue = True
            self.notify(
                f"Still waiting for {_in_words(running)} after {waited:.0f} s. Press Update "
                "or Uninstall again to stay in asq; quit, and it runs once they end.",
                severity="warning",
                timeout=10,
            )
        self._hand_off_poll = self.set_timer(_QUIT_POLL, self._exit_when_read)

    def _call_off_hand_off(self) -> None:
        """Stay in asq: stop waiting, and start the reads held meanwhile, once each.

        Until round 23 a hand-over always ended in a quit, so a read held back lost
        nothing. Now asq carries on, and a page left as the wait found it showed the old
        project's doctor report under the new one's name, a Settings tab with no slot to
        pick, Welcome's path box never judged and an empty Project Doctor tab (round 24 of
        #257). Each held read starts once, for a node still in the app
        (:meth:`_WorkerThreads.release`); the ticks go on as they did. One that cannot start
        is said, and the rest still start (round 25).
        """
        if self._hand_off_poll is not None:
            self._hand_off_poll.stop()
        self.hand_off = None
        self._hand_off_overdue = False
        self.notify("Called off: asq carries on", timeout=4)
        for name, exc in self._worker_threads.release():
            self.notify(
                f"could not start {_in_words([name])} again: {exc}",
                severity="warning",
                timeout=6,
                markup=False,
            )

    def _holding_the_quit(self) -> list[str]:
        """The workers a quit would cut off that still run, by name: any thread worker but
        Textual's own and those :func:`_cut_off_workers` names, whether it is still to start,
        running, or cancelled with its thread still running (:class:`_WorkerThreads`).

        A quit cannot stop a thread worker: Textual's cancel leaves its callable running,
        asyncio.run joins its thread with the terminal blank, and the hand-over can replace
        the install under it. A task worker is cancelled at its next await, and Textual's
        own include one that never ends (the Onboard view's ``DirectoryTree`` loader), so
        those are left to the quit, as is the thread that loader starts for each folder.
        Every worker the UI starts is a thread worker (tests/test_ui_writing_workers.py);
        the hand-over tests run both kinds.
        """
        return sorted(self._worker_threads.still_running() - _cut_off_workers())

    def _work_running(self, named: dict[str, str]) -> str | None:
        """What work of ``named`` still runs, in the toast's words, else ``None``."""
        return next(
            (named[w.name] for w in self.workers if w.name in named and not w.is_finished),
            None,
        )

    def on_doctor_refreshed(self, event: DoctorRefreshed) -> None:
        """A view re-ran the doctor after a one-click fix — follow it.

        Without this the sidebar keeps the pre-fix ✓/⚠/✗ counts and the old
        worst-findings lines until the user presses ``r``. A report about the
        scope we are showing is adopted as it is (no second run of the checks);
        one about another root — the Onboard view's, for the project it just
        registered — means our own report is stale, so we re-run ours.
        """
        if event.cwd == self._doctor_root:
            self.show_doctor(list(event.checks))
        else:
            self.run_doctor()

    # --- for tests and callers --------------------------------------------------------

    def current_view(self) -> Widget | None:
        """The widget the right pane shows right now."""
        return self.content.visible_content

    @staticmethod
    def projects() -> list[ProjectInfo]:
        with store_session() as store:
            return store.list_projects()


def _writing_workers() -> dict[str, str]:
    """Every worker that writes when someone asks it to, by name, in a toast's words.

    One rule on the app's worker manager, where views kept a flag each (a bool, a set, a
    property) and the next writer was missed: the Project tab's Start manager, an agent's
    Stop and Restart, and Explainability's writes all quit under an Update (round 16 of
    #257). The views' own name constants, imported here so the import block stays as it
    is. The AISquare sign-in is left out (:func:`_cut_off_workers`).

    Every other thread worker is waited out rather than refused
    (``FleetApp._holding_the_quit``), so a writer missing here is still never cut off;
    listed, it is refused with its name, before asq stops starting its reads.
    """
    from aisquare.cli.ui import persona_dialogs, spawn, stop
    from aisquare.cli.ui.views import (
        accounts,
        agent,
        doctor,
        explainability,
        fleet_bulk,
        onboard,
        personas_tab,
        project,
        settings,
        welcome,
    )

    return {
        doctor.FIX_WORKER: "a fix",
        onboard.ONBOARD_WORKER: "a folder's setup",
        welcome.worker_name("onboard"): "a folder's setup",
        welcome.worker_name("connect"): "Connect",
        welcome.worker_name("manager"): "a manager start",
        welcome.worker_name("coders"): "the coders' start",
        project.SPAWN_WORKER: "a manager start",
        settings.NICKNAME_WORKER: "a project's rename",
        stop.STOP_WORKER: "an agent's stop",
        spawn.SPAWN_WORKER: "an agent's spawn",
        persona_dialogs.IMPORT_WORKER: "a persona's import",
        personas_tab.ATTACH_WORKER: "a persona's attachment",
        agent.RESTART_WORKER: "an agent's restart",
        fleet_bulk.BULK_WORKER: "Settings ▸ Fleet's restart or stop of every agent",
        explainability.SAVE_WORKER: "Explainability's setup",
        explainability.REGISTER_WORKER: "Explainability's registration",
        explainability.SHIP_WORKER: "Explainability's ship",
        accounts.SIGN_OUT_WORKER: "an AISquare sign-out",
        accounts.COMPLETE_WORKER: "a Claude sign-in",
        accounts.ABANDON_WORKER: "a Claude sign-in's discard",
        accounts.REMOVE_WORKER: "an account's removal",
        accounts.ARRANGE_WORKER: "an account change",
        accounts.RENAME_WORKER: "an account's rename",
        PROJECT_REMOVE_WORKER: "a project's removal",
    }


def _waited_reads() -> dict[str, str]:
    """Every read a hand-over waits out, by name, in a toast's words.

    What the overdue toast names (``FleetApp._exit_when_read``), in the voice of the
    refusal's :func:`_writing_workers`: it listed the workers by name, ``doctor``,
    ``shell-accounts``, ``welcome-validate`` (round 24 of #257). tests/test_ui_writing_workers.py
    checks that every read the UI starts is here.
    """
    from aisquare.cli.ui import attach, board, persona_dialogs, spawn
    from aisquare.cli.ui.views import (
        accounts,
        agent,
        app_settings,
        doctor,
        explainability,
        fleet_bulk,
        welcome,
    )

    return {
        _DOCTOR_WORKER: "the doctor's checks",
        ACCOUNTS_WORKER: "the accounts' read",
        HEADROOM_WORKER: "the accounts' headroom",
        doctor.ROUTE_WORKER: "how aisquare was installed",
        accounts.USAGE_WORKER: "the accounts' usage",
        accounts.CREDITS_WORKER: "the workspaces' credits",
        accounts.LOGIN_OPEN_WORKER: "a Claude log-in's stored login",
        accounts.LOGIN_POLL_WORKER: "a Claude sign-in's check",
        agent.LABELS_WORKER: "an agent's account names",
        board.LABELS_WORKER: "a board's account names",
        explainability.STATUS_WORKER: "Explainability's status",
        fleet_bulk.PLAN_WORKER: "Settings ▸ Fleet's read of the running agents",
        app_settings.ACCOUNTS_WORKER: "App settings' accounts",
        welcome.worker_name("look"): "Welcome's checks",
        welcome.worker_name("candidates"): "Welcome's folder list",
        welcome.worker_name("validate"): "Welcome's folder check",
        spawn.ACCOUNTS_WORKER: "the Spawn dialog's accounts",
        attach.TARGETS_WORKER: "the persona target picker's list",
        persona_dialogs.SKILLS_WORKER: "the Import dialog's skill list",
    }


def _in_words(names: list[str]) -> str:
    """``names``, workers a hand-over waits for, as the toast says them: a read or a writer
    in its words (:func:`_waited_reads`, :func:`_writing_workers`), any other as "a read"."""
    words = {**_waited_reads(), **_writing_workers()}
    said = list(dict.fromkeys(words.get(name, "a read") for name in names))
    return said[0] if len(said) == 1 else f"{', '.join(said[:-1])} and {said[-1]}"


def _cut_off_workers() -> frozenset[str]:
    """The workers a hand-over leaves to the quit, which loses nothing of theirs.

    The AISquare sign-in alone: it waits for the browser until a token arrives, a quit
    cancels that wait (``AccountsView.on_unmount``) and nothing is stored past a cancel.
    Waited for, it held Update and Uninstall for the device code's lifetime, 10 to 15
    minutes (round 16 of #257). Every other thread worker is waited for, a new one too.
    """
    from aisquare.cli.ui.views import accounts

    return frozenset({accounts.SIGN_IN_WORKER})


REMOTE_QUIT_QUIET_SECONDS = 0.5
"""How long ``run_ui`` waits for a Remote still stopping before it says that it is."""

_HANDED_OVER = {"upgrade": "Update", "uninstall": "Uninstall"}
"""The Doctor's button for each command it hands over (``doctor.HAND_OFFS``)."""


def _hand_off_undone(hand_off: tuple[str, ...]) -> str:
    """What a Ctrl-C in the quit's waits says of the hand-over it ends: that the Update or
    Uninstall did not run, and the command that runs it, with this process's global flags.

    The Ctrl-C ends the process at once (``remote_server._remote_quit_now``), and said
    nothing of it: a user who pressed it to hurry the upgrade along could not tell that
    ``aisquare upgrade`` never ran (round 4 of #259). ``--reopen`` is asq's own, to open it
    again once the command is done, and not asked of a command typed in a shell.
    """
    from aisquare.core import selfcli
    from aisquare.services import install_route

    args = [arg for arg in hand_off if arg != "--reopen"]
    button = _HANDED_OVER.get(args[0], args[0]) if args else "the hand-over"
    command = install_route.command_line(["aisquare", *selfcli.global_flags(), *args])
    return f"⚠ {button} did not run: asq quit at the Ctrl-C — `{command}` runs it"


def run_ui(**options: Any) -> None:
    """Run the fleet UI until the user quits; then say what its last saves could not land,
    see out a phone's write that still runs, saying so (a Ctrl-C there quits at once), and
    hand the terminal to the command Update or Uninstall asked for, if one did, once the R
    panel's own writes of ``remote.json`` have landed and the pushes sent to the phones have
    gone out.

    A Remote that was on stops on a thread of its own at quit (``on_unmount``), so the
    screen did not freeze while uvicorn and ngrok wound down; the process waits for
    them here, with the terminal back, and never ends before its ngrok. A Ctrl-C in
    that wait quits at once as well, its ngrok stopped first: a phone's write still
    running is what holds a stop up (uvicorn waits for the request), and raised out of
    here the Ctrl-C was Click's "Aborted!", after which Python's exit waited for the
    write all the same, silently. What the quit could not save is said before either
    wait, so a quit at once keeps it; an Update or Uninstall it drops is said as it quits
    (:func:`_hand_off_undone`). A hangup or a SIGTERM ends the process as ever, its ngrok
    first (``ngrok_ends_with``).
    """
    from aisquare.services import remote_server

    app = FleetApp(**options)
    with ngrok_ends_with():
        app.run()
        # Read with getattr so an app still needs only `run()`, `unsaved` and `remote` to be
        # run here.
        hand_off = getattr(app, "hand_off", None)
        undone = None if hand_off is None else _hand_off_undone(hand_off)
        for line in app.unsaved:
            stderr_console().print(f"⚠ {line}", markup=False, highlight=False)
        try:
            if not app.remote.wait_until_off(REMOTE_QUIT_QUIET_SECONDS):
                stderr_console().print("stopping Remote (its server and ngrok)…", markup=False)
                app.remote.wait_until_off()
            if hand_off is not None:
                # The R panel's writes of remote.json (the write switch, a revoke) run on a
                # thread an exit joins and the exec skips; the stop above waited for them
                # only when this asq served Remote (round 4 of #259).
                app.remote.writes_done()
        except KeyboardInterrupt:
            try:  # the thread that would stop ngrok ends with the process
                app.remote.stop_tunnel_now()
            finally:
                remote_server._remote_quit_now(undone)
        # A phone's write still running would hold the exit as long as it runs: said, not silent.
        remote_server.remote_wait_for_writes(undone)
        if hand_off is not None:
            # The "Remote is off" a turn-off or the quit's auto-off sent the phones goes out on
            # a daemon thread, which an exit waits for (`push_drain`, at exit) and the exec
            # would kill mid-send (round 5 of #259).
            pushes = sys.modules.get("aisquare.services.remote_push")  # loaded by all that push
            try:
                if pushes is not None:
                    pushes.push_drain()
                # A copy made just before the press is still queued or being written on the
                # clipboard thread: `flush` runs at exit, and the exec runs no exit handler,
                # so pbcopy got it cut short or never (review of #266, round 3).
                clipboard.flush()
            except KeyboardInterrupt:
                remote_server._remote_quit_now(undone)
    # The terminal is ours again, and Remote, its ngrok, the panel's writes, the phones' and
    # the pushes are done: give it to the command, whose exec skips what an exit would run.
    if hand_off is not None:
        from aisquare.core import selfcli

        selfcli.exec_self(hand_off)
