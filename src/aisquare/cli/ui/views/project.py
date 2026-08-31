"""The Project view: Manager · Board · Doctor · Explainability · Settings tabs.

One ``TabbedContent`` per selected project (docs/plans/fleet-tui.md §4.2). The
Manager tab is where goal intake happens — the user types to the manager's real
Claude Code session exactly as they would to any other, so the tab is the
manager's ``TerminalPane`` and nothing more, plus a **Start manager** button
while the project has none. The Board tab is ``cli.ui.board.BoardPanel``, the
widgets of ``aisquare board -w``; Explainability and Settings are the two forms
in this package.

Doctor is a ``DoctorView`` this view fills itself, with ``diagnostics.doctor``
in the project's root as cwd — so its one-click fixes are the project's fixes,
not the UI's cwd's. It runs when the tab is ACTIVATED and not before: a doctor
is seconds of git/tmux/claude probing, and opening a project must not pay for
a tab nobody looked at. The shell may push instead, through
:meth:`ProjectView.show_doctor`; the app-level ``#doctor`` view is a different
widget with a different scope (whatever the sidebar last selected).

The shell pushes fresh data with :meth:`ProjectView.refresh_status`. The plan
names that push ``refresh(status_snapshot)``, and Textual owns ``refresh`` on
every widget — it is the repaint, called with ``Region``s from inside the
framework — so :meth:`ProjectView.refresh` accepts a snapshot too and routes it
to ``refresh_status`` before repainting; either spelling works. On its own the
view reads the fleet service once at mount, so it also works standalone — which
is how the tests drive it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import ClassVar, Self

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.geometry import Region
from textual.widgets import Button, Static, TabbedContent, TabPane
from textual.worker import Worker, WorkerState

from aisquare.cli.ui.board import BoardPanel
from aisquare.cli.ui.terminal import TerminalPane
from aisquare.cli.ui.views.doctor import DoctorView
from aisquare.cli.ui.views.explainability import ExplainabilityView
from aisquare.cli.ui.views.settings import SettingsView
from aisquare.core.tmux import TmuxServer
from aisquare.models import CheckStatus, DoctorCheck, FleetAgentStatus, ProjectInfo
from aisquare.services import diagnostics
from aisquare.services import fleet as fleet_service

SPAWN_WORKER = "spawn-manager"
"""Name of the worker that starts the manager; how its result is told apart."""

DOCTOR_WORKER = "project-doctor"
"""Name of the worker that runs the Doctor tab's checks.

Deliberately NOT ``doctor``: ``FleetApp.on_worker_state_changed`` claims that
name for its own app-level run, and a ``Worker.StateChanged`` bubbles from here
up to the app.
"""

ProjectDoctor = Callable[[Path], list[DoctorCheck]]
"""``project root -> checks``: how the Doctor tab runs the doctor for a project."""


def project_doctor(root: Path) -> list[DoctorCheck]:
    """The default: this project's checks, in-process, with its root as cwd."""
    return diagnostics.doctor(cwd=root)


_STATE_CHIP: dict[str, tuple[str, str]] = {
    "working": ("▶", "green"),
    "waiting": ("⏸", "yellow"),
    "attention": ("🔔 NEEDS YOU", "bold red"),
    "exited": ("💤", "dim"),
    "lost": ("✗", "red"),
    "unknown": ("·", "dim"),
}


def manager_text(status: FleetAgentStatus) -> Text:
    """The Manager tab's header: who is running, in what state, where."""
    agent = status.agent
    text = Text()
    text.append("🧭 manager", style="bold")
    chip, style = _STATE_CHIP.get(status.state, ("·", "dim"))
    text.append(f"  {chip} {status.state}", style=style)
    if status.state == "exited" and agent.exit_status is not None:
        text.append(f"({agent.exit_status})", style=style)
    if status.detail:
        text.append(f"  {status.detail}", style="dim")
    if status.session is not None and status.session.model:
        text.append(f"  {status.session.model}", style="dim cyan")
    text.append(f"  {agent.pane_id}", style="dim")
    if status.tmux_session:
        text.append(
            f"  ·  tmux -L {agent.tmux_socket} attach -t ={status.tmux_session}", style="dim"
        )
    text.append(
        "\ntype to the manager as you would to any Claude session · F12 returns to the sidebar",
        style="dim",
    )
    return text


def no_manager_text(project: ProjectInfo, unavailable: str | None) -> Text:
    """The Manager tab's header when the project has no live manager."""
    name = project.root.name or project.id
    text = Text()
    text.append(f"{name} has no manager yet.\n", style="bold")
    text.append(
        "Start one to task this project in prose: it plans, spawns coders, testers and "
        "reviewers on the board, and reports back when the goal is met.",
    )
    if unavailable:
        text.append(f"\nfleet unavailable: {unavailable}", style="bold red")
    return text


def manager_status(agents: Sequence[FleetAgentStatus]) -> FleetAgentStatus | None:
    """The live manager among ``agents``, if any."""
    for status in agents:
        if status.agent.label == fleet_service.MANAGER_LABEL and status.agent.ended_at is None:
            return status
    return None


class ManagerTab(Vertical):
    """The manager's live session — or the button that starts one."""

    DEFAULT_CSS = """
    ManagerTab { height: 1fr; }
    ManagerTab #manager-header { height: auto; padding: 0 1; }
    ManagerTab #start-manager { margin: 1 1; }
    ManagerTab #manager-pane { height: 1fr; }
    """

    def __init__(self, project: ProjectInfo, *, escape_key: str, id: str | None = None) -> None:
        super().__init__(id=id)
        self.project = project
        self.escape_key = escape_key
        self.status: FleetAgentStatus | None = None

    def compose(self) -> ComposeResult:
        yield Static(id="manager-header")
        yield Button("Start manager", id="start-manager", variant="primary")
        yield TerminalPane(None, escape_key=self.escape_key, id="manager-pane")

    def on_mount(self) -> None:
        self.refresh_from_service()

    def refresh_from_service(self) -> None:
        """Ask the fleet service who the manager is (the standalone path)."""
        try:
            manager = fleet_service.manager_of(self.project)
        except fleet_service.FleetError as exc:
            self.show(None, unavailable=str(exc))
            return
        if manager is None:
            self.show(None)
            return
        try:
            status = fleet_service.status_of(manager)
        except fleet_service.FleetError:
            status = FleetAgentStatus(agent=manager)
        self.show(status)

    def show(self, status: FleetAgentStatus | None, *, unavailable: str | None = None) -> None:
        """Render ``status`` — the pane when there is a manager, the button when not."""
        self.status = status
        header = self.query_one("#manager-header", Static)
        button = self.query_one("#start-manager", Button)
        pane = self.query_one("#manager-pane", TerminalPane)
        if status is None:
            header.update(no_manager_text(self.project, unavailable))
            button.display = True
            pane.display = False
            if pane.pane_id is not None:
                pane.attach(None)
            return
        header.update(manager_text(status))
        button.display = False
        pane.display = True
        if pane.pane_id != status.agent.pane_id:
            pane.server = TmuxServer(status.agent.tmux_socket)
            pane.attach(status.agent.pane_id)

    @on(Button.Pressed, "#start-manager")
    def _start_manager(self) -> None:
        """Spawn the manager off the UI thread; the result arrives as a worker state."""
        self.query_one("#start-manager", Button).disabled = True
        # exit_on_error=False: a FleetError is an answer to show, not a crash.
        self.run_worker(self._spawn_manager, name=SPAWN_WORKER, thread=True, exit_on_error=False)

    def _spawn_manager(self) -> fleet_service.SpawnReceipt:
        return fleet_service.spawn(self.project, "manager")

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name != SPAWN_WORKER:
            return
        if event.state is WorkerState.SUCCESS:
            receipt = event.worker.result
            if isinstance(receipt, fleet_service.SpawnReceipt):
                agent = receipt.agent
                where = f"{receipt.tmux_session} {agent.pane_id}"
                self.notify(
                    f"✓ spawned {agent.label} ({agent.id}) → {where}",
                    timeout=6,
                    markup=False,
                )
                for note in receipt.notes:
                    self.notify(note, severity="warning", timeout=8, markup=False)
            self.refresh_from_service()
        elif event.state is WorkerState.ERROR:
            self.notify(
                f"could not start the manager: {event.worker.error}",
                severity="error",
                timeout=8,
                markup=False,
            )
        if event.state in (WorkerState.SUCCESS, WorkerState.ERROR, WorkerState.CANCELLED):
            self.query_one("#start-manager", Button).disabled = False


class ProjectView(TabbedContent):
    """One project: its manager's pane first, then the board, doctor, tracing and settings."""

    TAB_IDS: ClassVar[tuple[str, ...]] = (
        "tab-manager",
        "tab-board",
        "tab-doctor",
        "tab-explainability",
        "tab-settings",
    )

    def __init__(
        self,
        project: ProjectInfo,
        *,
        id: str | None = None,
        refresh_seconds: float = 2.0,
        doctor: ProjectDoctor | None = None,
    ) -> None:
        super().__init__(id=id)
        self.project = project
        self.refresh_seconds = refresh_seconds
        self._doctor: ProjectDoctor = doctor if doctor is not None else project_doctor
        # TabbedContent composes ITSELF from the panes handed to it — this is
        # what `with TabbedContent(): yield TabPane(...)` does at compose time,
        # so a subclass adds its panes here rather than overriding compose().
        escape_key = fleet_service.settings().escape_key
        panes = (
            TabPane(
                "Manager",
                ManagerTab(project, escape_key=escape_key, id="manager-tab"),
                id="tab-manager",
            ),
            TabPane(
                "Board",
                BoardPanel(project, interval=refresh_seconds, id="board-panel"),
                id="tab-board",
            ),
            TabPane(
                "Doctor",
                # ``cwd``: the fixes this tab offers run in THIS project's root
                # (§5.6 — the UI process never chdirs), and a project-scoped fix
                # renders disabled without one.
                DoctorView(cwd=project.root, id="project-doctor"),
                id="tab-doctor",
            ),
            TabPane(
                "Explainability",
                ExplainabilityView(id="project-explainability"),
                id="tab-explainability",
            ),
            TabPane("Settings", SettingsView(project, id="project-settings"), id="tab-settings"),
        )
        for pane in panes:
            self.compose_add_child(pane)
        self._pending: list[FleetAgentStatus] | None = None
        """A snapshot pushed before the tabs mounted, applied at mount."""

    def refresh_status(self, agents: Sequence[FleetAgentStatus]) -> None:
        """Take the shell's fresh snapshot of this project's agents.

        Safe before mount: the snapshot waits for the tabs and lands then.
        """
        tabs = self.query(ManagerTab)
        if not tabs:
            self._pending = list(agents)
            return
        tabs.first().show(manager_status(agents))

    def on_mount(self) -> None:
        if self._pending is not None:
            pending, self._pending = self._pending, None
            self.refresh_status(pending)

    # --- the Doctor tab ----------------------------------------------------------------

    def show_doctor(self, checks: Sequence[DoctorCheck]) -> None:
        """Fill the Doctor tab with ``checks`` — the tab's own run, or the shell's push.

        Safe before the tabs mount and safe with no Doctor tab: nothing to fill
        is not an error, and the next activation runs the checks again.
        """
        views = self.query(DoctorView)
        if views:
            views.first().show(list(checks), cwd=self.project.root)

    @on(TabbedContent.TabActivated, pane="#tab-doctor")
    def run_project_doctor(self) -> None:
        """Run this project's checks off the UI thread; the worker state paints them.

        Lazy and exclusive: the tab is the only trigger, and re-opening it
        refreshes the report instead of stacking runs.
        """
        root = self.project.root
        self.run_worker(
            lambda: self._doctor(root),
            name=DOCTOR_WORKER,
            group=DOCTOR_WORKER,
            exclusive=True,
            thread=True,
            exit_on_error=False,  # a crashed doctor is a report, not an app crash
        )

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name != DOCTOR_WORKER:
            return  # ManagerTab's spawn worker bubbles through here too
        if event.state is WorkerState.SUCCESS:
            result = event.worker.result
            self.show_doctor(list(result) if isinstance(result, list) else [])
        elif event.state is WorkerState.ERROR:
            error = event.worker.error
            self.show_doctor(
                [
                    DoctorCheck(
                        name="doctor",
                        status=CheckStatus.fail,
                        detail=f"the checks crashed: {type(error).__name__}: {error}",
                        fix="Run it in a terminal for the full traceback: aisquare doctor",
                    )
                ]
            )

    def refresh(
        self,
        *regions: Region | Sequence[FleetAgentStatus],
        repaint: bool = True,
        layout: bool = False,
        recompose: bool = False,
    ) -> Self:
        """Textual's repaint — and the shell's push, when handed an agent snapshot.

        ``Region`` is a tuple, so it is told apart first; anything else in
        ``regions`` is a snapshot for :meth:`refresh_status`. A bare
        ``refresh()`` stays exactly the framework's.
        """
        real: list[Region] = []
        for item in regions:
            if isinstance(item, Region):
                real.append(item)
            else:
                self.refresh_status(item)
        return super().refresh(*real, repaint=repaint, layout=layout, recompose=recompose)
