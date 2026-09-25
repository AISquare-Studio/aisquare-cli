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

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import ContentSwitcher, Footer, Static
from textual.worker import Worker, WorkerState

from aisquare.cli.ui.attach import NewAccountRequested
from aisquare.cli.ui.autosave import Autosave
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
from aisquare.cli.ui.receiver import listen_for_ui, stop_listening_for_ui
from aisquare.cli.ui.sidebar import (
    ALIVE_STATES,
    AccountsSelected,
    AddProject,
    AgentSelected,
    CaptainRequested,
    DoctorSelected,
    ProjectSelected,
    ResizeSidebar,
    Sidebar,
    SpawnAgent,
    StopAgent,
    accounts_summary_text,
)
from aisquare.cli.ui.spawn import SpawnCompleted, SpawnDialog
from aisquare.cli.ui.stop import StopAgentScreen
from aisquare.cli.ui.terminal import EscapeToSidebar, SelectionHost, TerminalPane
from aisquare.cli.ui.theme import ThemePicker, restore_theme, theme_autosave
from aisquare.cli.ui.views.accounts import AccountsChanged, AccountsView, read_session, summarise
from aisquare.cli.ui.views.agent import AgentRestarted, AgentView
from aisquare.cli.ui.views.captain import CaptainView
from aisquare.cli.ui.views.doctor import DoctorRefreshed, DoctorView
from aisquare.cli.ui.views.onboard import OnboardFailed, OnboardView, ProjectOnboarded
from aisquare.cli.ui.views.project import ProjectView
from aisquare.cli.ui.views.welcome import WelcomeView
from aisquare.core import paths
from aisquare.core.console import stderr_console
from aisquare.core.store import ContextStore, store_session
from aisquare.core.workspace import project_id_for
from aisquare.models import (
    AccountsOverview,
    CheckStatus,
    DoctorCheck,
    FleetAgent,
    FleetAgentStatus,
    ProjectInfo,
)
from aisquare.services import claude_accounts as accounts_service
from aisquare.services import diagnostics, project_groups
from aisquare.services import fleet as fleet_service

DoctorRunner = Callable[[], list[DoctorCheck]]
"""What the Doctor section runs: ``diagnostics.doctor`` in production, a stub in tests."""

AccountsReader = Callable[[], AccountsOverview]
"""What the Accounts section reads: ``claude_accounts.overview`` in production, a stub in tests."""

_DoctorReport = tuple[Path | None, list[DoctorCheck]]
"""What the doctor worker hands back: the scope it ran for, and its checks."""

_DOCTOR_WORKER = "doctor"
_CHECK_SYMBOL = {CheckStatus.ok: "✓", CheckStatus.warn: "⚠", CheckStatus.fail: "✗"}
_CHECK_STYLE = {CheckStatus.ok: "green", CheckStatus.warn: "yellow", CheckStatus.fail: "bold red"}

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
"""``ui_state`` key for what is open: ``project:<id>``, ``agent:<project>/<id>``,
``accounts`` or ``doctor:<project or ''>`` (#144)."""
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
    home: ProjectInfo | None = None
    """The captain's home board (T2): never one of ``projects`` and never a project to
    the shell — no card, no project page, no Doctor scope. Its agent, the captain, is
    in ``agents``, so selecting it resolves; :meth:`board` answers the home for the
    actions on that row."""

    def project(self, project_id: str) -> ProjectInfo | None:
        """A listed project — never the home board.

        Every caller takes the answer for a project: the page ``stop_finished``
        goes back to, the Doctor's scope, a restored selection's fallback. The
        home answered here opened a project page of ``$AISQUARE_HOME`` — "has no
        manager yet", with a Start-manager button onto the home board.
        """
        return next((p for p in self.projects if p.id == project_id), None)

    def board(self, project_id: str) -> ProjectInfo | None:
        """The board an agent's row lives on: a listed project, or the captain's home.

        Only for an action on the row itself — Stop needs the ``ProjectInfo`` of
        the captain's board as much as any agent's.
        """
        if self.is_home(project_id):
            return self.home
        return self.project(project_id)

    def is_home(self, project_id: str) -> bool:
        return self.home is not None and self.home.id == project_id

    def agent(self, project_id: str, agent_id: str) -> FleetAgentStatus | None:
        return next((s for s in self.agents.get(project_id, []) if s.agent.id == agent_id), None)


class HelpScreen(ModalScreen[None]):
    """The keys, in one place. Esc, q or ? closes."""

    CSS = """
    HelpScreen { align: center middle; }
    #helpbox { width: 64; height: auto; border: heavy $accent; background: $surface;
               padding: 1 2; }
    """
    BINDINGS: ClassVar = [
        ("escape", "close_help", "close"),
        ("q", "close_help", "close"),
        ("question_mark", "close_help", "close"),
    ]

    def __init__(self, escape_key: str) -> None:
        super().__init__()
        self.escape_key = escape_key

    def compose(self) -> ComposeResult:
        text = Text()
        text.append("aisquare fleet — keys\n\n", style="bold")
        for key, what in (
            ("click", "select a project, an agent, Accounts, Doctor; + onboards a project"),
            ("↑ ↓ Enter", "move over the sidebar and open the row under the cursor"),
            # Arranging the sidebar (#140): every one of these has a CLI twin.
            ("shift+↑ ↓", "move the row under the cursor one place"),
            ("g p space", "group · pin · fold the row under the cursor"),
            ("u", "undo the last arrangement; a toast says what"),
            ("shift+click", "mark cards — shift+g groups them, Esc clears"),
            ("drag title", "move a card or a group header to a new place"),
            (self.escape_key.upper(), "hand focus from an agent's pane back to the sidebar"),
            ("wheel", "scroll an agent pane; shift/alt+PgUp/PgDn too, shift+Home/End"),
            ("drag", "select text in a pane (double-click: a word) — copied on release"),
            ("divider", "drag the line beside the sidebar to resize it; double-click puts it back"),
            ("> < =", "from the sidebar: widen, narrow, reset the divider"),
            ("t", "themes (applied live, autosaved)"),
            ("r", "refresh now"),
            ("a", "show or hide the captured directories (never added)"),
            ("F1", "command palette"),
            ("q", "quit — from the sidebar; inside a pane every key goes to the agent"),
        ):
            text.append(f"  {key:<11}", style="bold cyan")
            text.append(f" {what}\n")
        text.append("\nEsc closes this", style="dim")
        with Vertical(id="helpbox"):
            yield Static(text)

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
        Binding("r", "refresh_now", "refresh"),
        Binding("a", "toggle_captured", "captured", show=False),
        Binding("question_mark", "help", "help", key_display="?"),
    ]
    SIDEBAR_ACTIONS: ClassVar[frozenset[str]] = frozenset(
        {
            "quit",
            "pick_theme",
            "refresh_now",
            "help",
            "command_palette",
            "change_theme",
            "toggle_captured",
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
    ) -> None:
        super().__init__()
        self.refresh_seconds = refresh_seconds
        self._doctor = doctor
        self._accounts = accounts
        self.accounts_overview: AccountsOverview | None = None
        """The last Accounts frame that was read; ``None`` before the first or when disabled."""
        self.escape_key = escape_key or fleet_service.settings().escape_key
        self.snapshot: FleetSnapshot | None = None
        """The last frame that was read successfully; ``None`` before the first."""
        self.store_error: str | None = None
        """Why the newest refresh kept the previous frame, or ``None`` when it did not."""
        self.doctor_checks: list[DoctorCheck] = []
        self.doctor_scope: str | None = None
        """The project the Doctor section is about; ``None`` = global checks."""
        self._doctor_worker: Worker[Any] | None = None
        """The newest doctor run; an older one's result is not ours to paint."""
        self._theme_restored = False
        self._theme_autosave = theme_autosave(self)
        self.unsaved: list[str] = []
        """What the quit-time flush could not land (a preference each), for ``run_ui`` to say
        once the screen is gone."""
        self.show_captured = False
        """Whether the sidebar also lists the directories sessions merely ran in (#139)."""
        self._undo: list[project_groups.UndoEntry] = []
        """The layout gestures of this session, newest last; ``u`` reverts the last (#140)."""

    # --- layout -------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Panes(id="main"):
            yield Sidebar(id="sidebar")
            # The partition is a widget, not a border: drag it, or step it with
            # < > = from the sidebar; the width is remembered (#137).
            yield Divider("#sidebar", state_key=SIDEBAR_WIDTH_KEY, id="divider")
            with ContentSwitcher(id="content", initial="welcome"):
                yield WelcomeView(escape_key=self.escape_key, id="welcome")
                # The Onboard view is built on the first `+` (on_add_project): its
                # DirectoryTree scans the home directory and keeps a loader worker
                # alive for its whole life — not a cost to pay at every start-up.
                yield DoctorView(id="doctor")
        yield Footer()

    def on_mount(self) -> None:
        restore_theme(self)
        self._theme_restored = True
        self.show_captured = _ui_state(SHOW_CAPTURED_KEY) == "1"
        self.refresh_data()
        self.set_interval(self.refresh_seconds, self.refresh_data)
        self.run_doctor()
        self._restore_selection()
        # The captain's ui actions (T4): after the first frame, which they resolve against.
        listen_for_ui(self)

    # --- what was open (#144) ---------------------------------------------------------

    def _restore_selection(self) -> None:
        """Reopen the view that was open when the UI last ran, if its row is still there.

        Read from the store's ``ui_state`` (v18), never from a file the UI
        keeps for itself: the theme stays in ``state.json`` because the board
        shares it. A remembered agent whose row has left the frame falls back
        to its project; a project that is gone falls back to the welcome view,
        and the memory is dropped rather than retried every launch. So does a
        captain whose row has left: its board is no project, and
        ``FleetSnapshot.project`` never answers the home (T2).
        """
        remembered = _ui_state(SELECTED_KEY)
        if not remembered or self.snapshot is None:
            return
        kind, _, ident = remembered.partition(":")
        if kind == "agent":
            project_id, _, agent_id = ident.partition("/")
            if self.snapshot.agent(project_id, agent_id) is not None:
                self.post_message(AgentSelected(project_id, agent_id))
                return
            if self.snapshot.project(project_id) is not None:
                self.post_message(ProjectSelected(project_id))
                return
        elif kind == "project" and self.snapshot.project(ident) is not None:
            self.post_message(ProjectSelected(ident))
            return
        elif kind == "accounts":
            self.post_message(AccountsSelected())
            return
        elif kind == "doctor":
            self.post_message(DoctorSelected(ident or None))
            return
        _remember_ui_state(SELECTED_KEY, None)

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

    def watch_theme(self, theme_name: str) -> None:
        # Fires on ANY theme change (our picker or the palette): every change is
        # the save. Not while the saved theme is being restored at mount.
        parent = getattr(super(), "watch_theme", None)
        if parent is not None:
            parent(theme_name)
        if self._theme_restored:
            self._theme_autosave.remember(theme_name)

    def on_unmount(self) -> None:
        stop_listening_for_ui(self)  # first: no ui action lands on a shell that is quitting
        # Every saver — the theme's here, the divider's — started first and joined
        # against ONE deadline, so quit waits once, not once per preference.
        self.unsaved = Autosave.flush_all(self)

    # --- help / refresh ---------------------------------------------------------------

    def action_help(self) -> None:
        self.push_screen(HelpScreen(self.escape_key))

    def action_refresh_now(self) -> None:
        self.refresh_data()
        self.run_doctor()

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

    def refresh_data(self) -> None:
        """Re-read projects and agents; keep (and label) the last frame if the store is busy."""
        sidebar = self.sidebar
        home_id = project_id_for(paths.aisquare_home().resolve())
        try:
            with store_session() as store:
                # The home is captured, never onboarded, so only `a` lists it — and
                # it is still no project: the captain's section shows its board (T2).
                # As a card it doubled the captain's row, whose repeated selection
                # key sent ↓ round a loop above every project, and it offered a
                # spawn row onto the home board.
                projects = [
                    p for p in store.list_projects(all=self.show_captured) if p.id != home_id
                ]
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
        home, captain, captain_notice = self._captain(home_id)
        if home is not None:
            agents[home.id] = [captain] if captain is not None else []
        self.store_error = None
        self.snapshot = FleetSnapshot(projects, agents, notices, home=home)
        sidebar.show_notice(None)
        sidebar.show_captain(captain, notice=captain_notice)
        sidebar.show_projects(projects, agents, notices=notices, groups=groups)
        self._feed_open_views(self.snapshot)
        self.refresh_accounts()

    def _captain(
        self, home_id: str
    ) -> tuple[ProjectInfo | None, FleetAgentStatus | None, str | None]:
        """The home board, its captain's row, and — when the read failed — why.

        Read without writing anything: the home's id is computed, not registered,
        because this runs on every refresh tick and ``captain_state.home_project()``
        would write the row each time.

        **A read that failed is not "no captain".** ``list_agents`` opens its own
        session, so one momentarily locked sqlite db fails this read while the
        projects' succeeded. Answered as ``(None, None)``, it took the LIVE
        captain's row off the screen and told the owner to start one. A failure
        keeps the last frame's home and row instead — Stop and Restart still
        resolve against them — and the notice says why where the "starts one"
        line would be: the project cards' rule, for the captain.
        """
        try:
            with store_session() as store:
                home = store.get_project(home_id)
            if home is None:
                return None, None, None
            statuses = fleet_service.list_agents(home)
        except Exception as exc:  # a bug in the fleet path must not take the view down
            # The cards' wording: a FleetError already says what failed.
            if isinstance(exc, fleet_service.FleetError):
                reason = str(exc)
            else:
                reason = f"{type(exc).__name__}: {exc}"
            last = self.snapshot
            kept = last.home if last is not None else None
            rows = last.agents.get(kept.id, []) if last is not None and kept is not None else []
            return kept, next(iter(rows), None), f"captain unavailable — {reason}"
        captain = next((s for s in statuses if s.agent.role == fleet_service.CAPTAIN_ROLE), None)
        return home, captain, None

    def refresh_accounts(self) -> None:
        """Re-read the Claude accounts and the AISquare session; the section and the page follow.

        Files only — a few small JSON reads — which is why it rides the same
        two-second tick as the store. The usage numbers are the view's own,
        slower business (``AccountsView.refresh_usage``).
        """
        if self._accounts is None:
            return
        sidebar = self.sidebar
        try:
            overview: AccountsOverview | None = self._accounts()
        except Exception:  # a directory we cannot read costs the line, never the frame
            overview = None
        self.accounts_overview = overview
        session_known = True
        try:
            session = read_session()
        except Exception:  # read_session never raises; belt to its braces
            session, session_known = None, False
        summary = summarise(overview, session, session_known=session_known)
        sidebar.show_accounts_summary(accounts_summary_text(summary.aisquare), summary.line)
        if overview is not None:
            for view in self.query(AccountsView):
                view.show(overview)

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
        for agent_view in self.query(AgentView):
            fresh = snapshot.agent(agent_view.status.agent.project_id, agent_view.status.agent.id)
            if fresh is not None and fresh != agent_view.status:
                agent_view.refresh_status(fresh)
        for project_view in self.query(ProjectView):
            fresh_project = snapshot.project(project_view.project.id)
            if fresh_project is None:
                continue  # the row has left the frame: keep what the view has
            if fresh_project != project_view.project:
                # A new codename or root: the view re-reads what depends on it.
                project_view.project = fresh_project
            if snapshot.notices.get(fresh_project.id) is not None:
                continue  # we could not ask — not "the fleet answered: none"
            # ProjectView.show() is the MANAGER status renderer; the snapshot
            # goes through refresh_status, which picks the manager out of it.
            project_view.refresh_status(snapshot.agents.get(fresh_project.id, []))

    # --- doctor -------------------------------------------------------------------------

    def _scoped_project(self) -> ProjectInfo | None:
        """The project the Doctor section is about, or ``None`` for the global checks.

        One place for it: the run needs its root as the doctor's ``cwd``, and the
        views need the same root as the cwd their project-scoped fix buttons run
        in — the two must never disagree.
        """
        snapshot = self.snapshot
        if snapshot is None or not self.doctor_scope:
            return None
        return snapshot.project(self.doctor_scope)

    def _scope_root(self) -> Path | None:
        """The root the Doctor section's checks and fixes belong to right now."""
        project = self._scoped_project()
        return project.root if project is not None else None

    def run_doctor(self) -> None:
        """Run the checks off the UI thread; ``on_worker_state_changed`` paints them.

        The result carries the SCOPE it was run for, and the newest run is
        remembered. Without both, a report that finished while the user was
        selecting another project was painted as the new scope's: the filter was
        the worker NAME alone and ``show_doctor`` re-reads ``_scoped_project()``
        at paint time, so the old project's findings were handed the new
        project's root and a one-click project fix would have run in the wrong
        one — the hazard ``show_doctor``'s own comment warns about. Reachable
        because ``on_project_selected`` awaits ``add_content`` before
        ``_set_doctor_scope``, and a worker that has already left RUNNING is
        past cancelling.
        """
        if self._doctor is None:
            return
        injected: DoctorRunner = self._doctor
        project = self._scoped_project()
        scope: Path | None = project.root if project is not None else None
        # An injected fake keeps its own signature and stays global; it is still
        # tagged with the scope its report is ABOUT, which is what the tab and
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

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name != _DOCTOR_WORKER:
            return
        if event.worker is not self._doctor_worker:
            return  # a superseded run: the newest one owns the section
        if event.state is WorkerState.SUCCESS:
            report = _doctor_report(event.worker.result)
            if report is None or report[0] != self._scope_root():
                return  # a report about a scope the user has left is not ours to paint
            self.show_doctor(report[1])
        elif event.state is WorkerState.ERROR:
            self.show_doctor_failure(f"{type(event.worker.error).__name__}: {event.worker.error}")

    def show_doctor(self, checks: list[DoctorCheck]) -> None:
        self.doctor_checks = checks
        project = self._scoped_project()
        root = project.root if project is not None else None
        self._feed_doctor(self.query_one("#doctor", DoctorView), checks, root)
        # The scoped project's own Doctor tab shows the same report. Only that
        # project's: another open ProjectView's tab must not be handed a report
        # about a different root — its fixes would run in the wrong one.
        if project is not None:
            for view in self.query(ProjectView):
                if view.project.id == project.id:
                    for tab in view.query(DoctorView):
                        self._feed_doctor(tab, checks, root)
        counts = {status: 0 for status in CheckStatus}
        for check in checks:
            counts[check.status] += 1
        worst = sorted(
            (c for c in checks if c.status is not CheckStatus.ok),
            key=lambda c: c.status is not CheckStatus.fail,
        )
        lines: list[Text] = []
        for check in worst:
            line = Text()
            line.append(f"{_CHECK_SYMBOL[check.status]} ", style=_CHECK_STYLE[check.status])
            line.append(f"{check.name}: {check.detail}")
            lines.append(line)
        sidebar = self.sidebar
        sidebar.show_doctor_summary(
            counts[CheckStatus.ok], counts[CheckStatus.warn], counts[CheckStatus.fail], lines=lines
        )
        sidebar.show_doctor_notice(None)

    @staticmethod
    def _feed_doctor(view: DoctorView, checks: list[DoctorCheck], root: Path | None) -> None:
        """Hand a Doctor view the report AND the root its project fixes run in.

        Without the root every ``scope == "project"`` fix renders disabled with
        "(select a project first)" — while a project is selected — so the
        one-click fix of §0 item 4 never worked from the shell. ``show(cwd=None)``
        means "keep the cwd you have", so the global scope is set on the
        attribute: a project's root must not linger once the report is the
        machine-wide one, or a button would run in a project the user has left.
        """
        view.cwd = root
        view.show(checks)

    def show_doctor_failure(self, reason: str) -> None:
        """Doctor itself crashed: say so where the counts would be, and in the view."""
        self.sidebar.show_doctor_notice(f"doctor could not run — {reason}")
        self.query_one("#doctor", DoctorView).show(
            [
                DoctorCheck(
                    name="doctor",
                    status=CheckStatus.fail,
                    detail=f"the checks crashed: {reason}",
                    fix="Run it in a terminal for the full traceback: aisquare doctor",
                )
            ]
        )

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
        self._set_doctor_scope(project.id)
        self._remember_selection(f"project:{project.id}")

    async def on_agent_selected(self, event: AgentSelected) -> None:
        status = self.snapshot.agent(event.project_id, event.agent_id) if self.snapshot else None
        if status is None:
            self.notify("that agent is no longer listed", severity="warning", timeout=4)
            return
        view_id = f"agent-{status.agent.id}"
        await self._show(view_id, lambda: self._agent_view(status, view_id))
        self.sidebar.select(f"agent:{status.agent.id}")
        self._set_doctor_scope(event.project_id)
        self._remember_selection(f"agent:{event.project_id}/{status.agent.id}")
        self._focus_pane(view_id)

    def _agent_view(self, status: FleetAgentStatus, view_id: str) -> AgentView:
        """The view for one agent's row: the captain's has its own bar (T4)."""
        if (
            self.snapshot is not None
            and self.snapshot.is_home(status.agent.project_id)
            and status.agent.role == fleet_service.CAPTAIN_ROLE
        ):
            return CaptainView(status, id=view_id)
        return AgentView(status, id=view_id)

    def on_captain_requested(self, event: CaptainRequested) -> None:
        """The insignia: the captain's view when it is live, else the Spawn dialog preset to it.

        Decided from the frame the insignia was painted from, so the click does what
        the star showed: lit is a live row (``ALIVE_STATES``), dim is none — or one
        that is gone, which a start replaces (``brain.find`` reaps it first).
        """
        # Imported here, not at the top: the shell starts without the captain's modules.
        from aisquare.services.captain import brain
        from aisquare.services.captain import state as captain_state

        snapshot = self.snapshot
        home = snapshot.home if snapshot is not None else None
        rows = snapshot.agents.get(home.id, []) if snapshot is not None and home else []
        captain = next(iter(rows), None)
        if captain is not None and captain.state in ALIVE_STATES:
            self.post_message(AgentSelected(captain.agent.project_id, captain.agent.id))
            return
        if home is None:
            # No home row yet (a store that never saw a captain): registering it is what
            # the start is about to do anyway, on a deliberate click.
            try:
                home = captain_state.home_project()
            except Exception as exc:
                self.notify(
                    f"could not open the captain's board: {type(exc).__name__}: {exc}",
                    severity="error",
                    timeout=8,
                    markup=False,
                )
                return
        self.push_screen(
            SpawnDialog(
                home,
                role=fleet_service.CAPTAIN_ROLE,
                persona=brain.PERSONA,
                accounts=self._accounts,
            ),
            callback=self.spawn_finished,
        )

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
        """A restart minted a new row (#138): show it where the old one was.

        The view that posted this refreshed the frame first, so the row is
        normally in the snapshot already; one more read covers a store that
        was briefly busy. A row still missing is reported, not invented — the
        next tick lists it. The new pane takes the keyboard, as a selected
        agent's does (#147): the Restart button that had it went with the view
        it was on, and focus was left with nobody — where the app's keys are
        live, so a ``q`` typed at the restarted agent quit the UI.
        """
        started = event.agent
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
        await self._show(view_id, lambda: self._agent_view(status, view_id))
        self.sidebar.select(f"agent:{status.agent.id}")
        self._set_doctor_scope(started.project_id)
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
            shown = self.show_captured
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
            self.notify(f"could not undo: {exc}", severity="error", markup=False)
            return
        self.refresh_data()
        self.notify(f"undid: {done}", timeout=4, markup=False)

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

        ``board``, not ``project``: the captain's row stops against the home board,
        which is no listed project (T2).
        """
        project = self.snapshot.board(event.project_id) if self.snapshot else None
        status = self.snapshot.agent(event.project_id, event.agent_id) if self.snapshot else None
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

        The captain's board is no project (T2): going back to it opened a
        project page of ``$AISQUARE_HOME``. There is no page to go back to, so
        the shell goes back to where it starts — the welcome page, nothing
        selected, nothing to reopen at the next launch.
        """
        if agent is None:
            return
        self.notify(f"✓ stopped {agent.label} ({agent.id})", timeout=6, markup=False)
        self.refresh_data()
        view = self.current_view()
        if not (isinstance(view, AgentView) and view.status.agent.id == agent.id):
            return
        if self.snapshot is not None and self.snapshot.is_home(agent.project_id):
            self.content.current = "welcome"
            self.sidebar.select(None)
            self._remember_selection(None)
        else:
            self.post_message(ProjectSelected(agent.project_id))

    def on_spawn_completed(self, event: SpawnCompleted) -> None:
        """A Spawn dialog opened from a persona's *Attach to new*: the same receipt path."""
        self.spawn_finished(event.receipt)

    async def on_new_account_requested(self, event: NewAccountRequested) -> None:
        """The picker's *+ New account*: the Accounts page, its add-account flow started."""
        await self.on_accounts_selected(AccountsSelected())
        self.query_one("#accounts", AccountsView).begin_claude_sign_in(None)

    async def on_accounts_selected(self, event: AccountsSelected) -> None:
        await self._show(
            "accounts", lambda: AccountsView(escape_key=self.escape_key, id="accounts")
        )
        self.sidebar.select("accounts")
        self._remember_selection("accounts")
        if self.accounts_overview is not None:
            self.query_one("#accounts", AccountsView).show(self.accounts_overview)

    def on_accounts_changed(self, event: AccountsChanged) -> None:
        """The page added, removed or signed in something: the section follows at once."""
        self.refresh_accounts()

    async def on_doctor_selected(self, event: DoctorSelected) -> None:
        await self._show("doctor")
        self.sidebar.select("doctor")
        self.doctor_scope = event.project_id
        self._remember_selection(f"doctor:{event.project_id or ''}")
        self.run_doctor()

    async def on_project_onboarded(self, event: ProjectOnboarded) -> None:
        self.refresh_data()
        self.post_message(ProjectSelected(event.project_id))

    def on_onboard_failed(self, event: OnboardFailed) -> None:
        # markup=False: the message carries a path the user chose, and a toast
        # parses markup by default — ``/home/me/[archive]/repo`` would reach the
        # screen as ``/home/me//repo`` and name a directory that did not fail.
        self.notify(f"{event.path}: {event.reason}", severity="error", timeout=8, markup=False)

    def on_doctor_refreshed(self, event: DoctorRefreshed) -> None:
        """A view re-ran the doctor after a one-click fix — follow it.

        Without this the sidebar keeps the pre-fix ✓/⚠/✗ counts and the old
        worst-findings lines until the user presses ``r``. A report about the
        scope we are showing is adopted as it is (no second run of the checks);
        one about another root — the Onboard view's, for the project it just
        registered — means our own report is stale, so we re-run ours.
        """
        if event.cwd == self._scope_root():
            self.show_doctor(list(event.checks))
        else:
            self.run_doctor()

    def _set_doctor_scope(self, project_id: str | None) -> None:
        if (
            project_id is not None
            and self.snapshot is not None
            and self.snapshot.is_home(project_id)
        ):
            # The captain's board is no project (T2). Scoped to it, the Doctor ran
            # the per-project checks with cwd=$AISQUARE_HOME and armed their fixes
            # there; the captain's Doctor is the global one.
            project_id = None
        changed = project_id != self.doctor_scope
        self.doctor_scope = project_id
        self.sidebar.set_doctor_scope(project_id)
        if changed:
            # The counts, the report and the fixes' cwd are all about the scope:
            # a new one makes what is on screen a report about another project.
            self.run_doctor()

    # --- for tests and callers --------------------------------------------------------

    def current_view(self) -> Widget | None:
        """The widget the right pane shows right now."""
        return self.content.visible_content

    @staticmethod
    def projects() -> list[ProjectInfo]:
        with store_session() as store:
            return store.list_projects()


def run_ui(**options: Any) -> None:
    """Run the fleet UI until the user quits; then say what its last saves could not land."""
    app = FleetApp(**options)
    app.run()
    for line in app.unsaved:
        stderr_console().print(f"⚠ {line}", markup=False, highlight=False)
