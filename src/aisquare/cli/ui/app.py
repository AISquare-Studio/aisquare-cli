"""``FleetApp`` — the two-pane shell, and ``run_ui`` which bare ``asq`` calls.

docs/plans/fleet-tui.md §4, §4.2, §4.3, §3.8. Left: the ``Sidebar``. Right: a
``ContentSwitcher`` over the views — Welcome and Doctor from the start, Onboard on
the first ``+``,
one ``ProjectView`` / ``AgentView`` per selection, created the first time it is
asked for and kept (a view hosts a live terminal pane; re-creating it on every
click would restart that pane's render loop).

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
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import ContentSwitcher, Footer, Static
from textual.worker import Worker, WorkerState

from aisquare.cli.ui.sidebar import (
    AddProject,
    AgentSelected,
    DoctorSelected,
    ProjectSelected,
    Sidebar,
    SpawnAgent,
)
from aisquare.cli.ui.terminal import EscapeToSidebar
from aisquare.cli.ui.theme import ThemePicker, remember_theme, restore_theme
from aisquare.cli.ui.views.agent import AgentView
from aisquare.cli.ui.views.doctor import DoctorRefreshed, DoctorView
from aisquare.cli.ui.views.onboard import OnboardFailed, OnboardView, ProjectOnboarded
from aisquare.cli.ui.views.project import ProjectView
from aisquare.cli.ui.views.welcome import WelcomeView
from aisquare.core.store import store_session
from aisquare.models import CheckStatus, DoctorCheck, FleetAgentStatus, ProjectInfo
from aisquare.services import diagnostics
from aisquare.services import fleet as fleet_service

DoctorRunner = Callable[[], list[DoctorCheck]]
"""What the Doctor section runs: ``diagnostics.doctor`` in production, a stub in tests."""

_DoctorReport = tuple[Path | None, list[DoctorCheck]]
"""What the doctor worker hands back: the scope it ran for, and its checks."""

_DOCTOR_WORKER = "doctor"
_CHECK_SYMBOL = {CheckStatus.ok: "✓", CheckStatus.warn: "⚠", CheckStatus.fail: "✗"}
_CHECK_STYLE = {CheckStatus.ok: "green", CheckStatus.warn: "yellow", CheckStatus.fail: "bold red"}


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
            ("click", "select a project, an agent, the Doctor section; + onboards a project"),
            ("↑ ↓ Enter", "move over the sidebar and open the row under the cursor"),
            (self.escape_key.upper(), "hand focus from an agent's pane back to the sidebar"),
            ("t", "themes (applied live, autosaved)"),
            ("r", "refresh now"),
            ("F1", "command palette"),
            ("q", "quit — from the sidebar; inside a pane every key goes to the agent"),
        ):
            text.append(f"  {key:<10}", style="bold cyan")
            text.append(f" {what}\n")
        text.append("\nEsc closes this", style="dim")
        with Vertical(id="helpbox"):
            yield Static(text)

    def action_close_help(self) -> None:
        self.dismiss(None)


class FleetApp(App[None], inherit_bindings=False):
    """One `asq` view over every project, agent and session."""

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
        Binding("question_mark", "help", "help", key_display="?"),
    ]
    SIDEBAR_ACTIONS: ClassVar[frozenset[str]] = frozenset(
        {"quit", "pick_theme", "refresh_now", "help", "command_palette", "change_theme"}
    )
    """Actions that are live only while focus is in the sidebar (§4.3)."""

    def __init__(
        self,
        *,
        refresh_seconds: float = 2.0,
        doctor: DoctorRunner | None = diagnostics.doctor,
        escape_key: str | None = None,
    ) -> None:
        super().__init__()
        self.refresh_seconds = refresh_seconds
        self._doctor = doctor
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

    # --- layout -------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="main"):
            yield Sidebar(id="sidebar")
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
        self.refresh_data()
        self.set_interval(self.refresh_seconds, self.refresh_data)
        self.run_doctor()

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
            remember_theme(theme_name)

    # --- help / refresh ---------------------------------------------------------------

    def action_help(self) -> None:
        self.push_screen(HelpScreen(self.escape_key))

    def action_refresh_now(self) -> None:
        self.refresh_data()
        self.run_doctor()

    # --- data ---------------------------------------------------------------------------

    def refresh_data(self) -> None:
        """Re-read projects and agents; keep (and label) the last frame if the store is busy."""
        sidebar = self.sidebar
        try:
            with store_session() as store:
                projects = store.list_projects()
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
        sidebar.show_projects(projects, agents, notices=notices)
        self._feed_open_views(self.snapshot)

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

    async def on_agent_selected(self, event: AgentSelected) -> None:
        status = self.snapshot.agent(event.project_id, event.agent_id) if self.snapshot else None
        if status is None:
            self.notify("that agent is no longer listed", severity="warning", timeout=4)
            return
        view_id = f"agent-{status.agent.id}"
        await self._show(view_id, lambda: AgentView(status, id=view_id))
        self.sidebar.select(f"agent:{status.agent.id}")
        self._set_doctor_scope(event.project_id)

    def on_spawn_agent(self, event: SpawnAgent) -> None:
        # The Spawn dialog is Phase 7 (§9); until it lands the CLI is the way.
        self.notify(
            "the spawn dialog is not built yet — from a terminal: aisquare fleet spawn <role>",
            timeout=6,
        )

    async def on_doctor_selected(self, event: DoctorSelected) -> None:
        await self._show("doctor")
        self.sidebar.select("doctor")
        self.doctor_scope = event.project_id
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
    """Run the fleet UI until the user quits."""
    FleetApp(**options).run()
