"""The Project view: Manager · Board · AISquare · Settings tabs.

One ``TabbedContent`` per selected project (docs/plans/fleet-tui.md §4.2). The
Manager tab is where goal intake happens — the user types to the manager's real
Claude Code session exactly as they would to any other, so the tab is the
manager's ``TerminalPane`` and nothing more, plus a **Start manager** button
while the project has none. The Board tab is ``cli.ui.board.BoardPanel``, the
widgets of ``aisquare board -w``; AISquare (``ExplainabilityView``, under its
"Explainability" heading — the first of the tab's sections) and Settings are
the two forms in this package.

There is no Doctor tab: the doctor is the app's, on its own page, run at start
and when that page opens — not once per project view, nor per tab activation.

The shell pushes fresh data with :meth:`ProjectView.refresh_status`. The plan
names that push ``refresh(status_snapshot)``, and Textual owns ``refresh`` on
every widget — it is the repaint, called with ``Region``s from inside the
framework — so :meth:`ProjectView.refresh` accepts a snapshot too and routes it
to ``refresh_status`` before repainting; either spelling works. On its own the
view reads the fleet service once at mount, so it also works standalone — which
is how the tests drive it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import ClassVar, Self

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.geometry import Region
from textual.widgets import Button, Static, TabbedContent, TabPane
from textual.worker import Worker, WorkerState

from aisquare.cli.ui.board import BoardPanel
from aisquare.cli.ui.terminal import PANE_GONE, TerminalPane
from aisquare.cli.ui.views.agent import InfoLine, SlotLabels, shown_pane
from aisquare.cli.ui.views.explainability import ExplainabilityView
from aisquare.cli.ui.views.settings import SettingsView
from aisquare.core.tmux import TmuxServer
from aisquare.models import FleetAgentStatus, ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services import project as project_service

SPAWN_WORKER = "spawn-manager"
"""Name of the worker that starts the manager; how its result is told apart."""

MANAGER_HINT = "type to the manager as you would to any Claude session · F12 returns to the sidebar"
"""The dim line under the manager's info line: how to use the tab. The tmux attach command is
not here: it is the project's, and the Settings tab shows it."""


def no_manager_text(
    project: ProjectInfo, unavailable: str | None, *, exited: FleetAgentStatus | None = None
) -> Text:
    """The Manager tab's header when the project has no live manager.

    ``exited`` is a manager row that ENDED while its window still stands (#138:
    the listing keeps it so the last screen stays readable). "No manager yet"
    would be false for that project; the header says what happened and names
    both ways back — the button for a new session, the row's **Restart** for
    the same one.
    """
    name = project.root.name or project.id
    text = Text()
    if exited is not None:
        status = exited.agent.exit_status
        suffix = f" ({status})" if status is not None else ""
        text.append(f"{name}'s manager exited{suffix}.\n", style="bold")
        restart = project_service.fleet_command("restart", project, "manager")
        text.append(
            "Start manager begins a new session. Restart on its sidebar row brings the same "
            f"session back (its last screen is still there); {restart} does the same from a "
            "shell.",
        )
    else:
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


def exited_manager(agents: Sequence[FleetAgentStatus]) -> FleetAgentStatus | None:
    """The latest ENDED manager row among ``agents`` — listed while its window stands (#138)."""
    ended = [
        status
        for status in agents
        if status.agent.label == fleet_service.MANAGER_LABEL and status.agent.ended_at is not None
    ]
    return max(ended, key=lambda s: s.agent.ended_at or s.agent.created_at, default=None)


class ManagerTab(Vertical):
    """The manager's live session — or the button that starts one."""

    # ``-info``: the header shows the manager's info line
    # (:class:`~aisquare.cli.ui.views.agent.InfoLine`, the Agent view's) and is fitted as the
    # Agent view's header is — one row, its label giving way first, then cut at its end with
    # ``…``, so a long detail gives way rather than wrapping the line onto two or three rows.
    # The hint is a widget of its own and wraps: under the one rule, a 57-cell header (a
    # 90-column terminal) cut it before "F12 returns to the sidebar" (review of #267). The
    # prose that says there is no manager wraps, as it always did.
    DEFAULT_CSS = """
    ManagerTab { height: 1fr; }
    ManagerTab #manager-header { height: auto; padding: 0 1; }
    ManagerTab #manager-header.-info { text-wrap: nowrap; text-overflow: ellipsis; }
    ManagerTab #manager-hint { height: auto; padding: 0 1; }
    ManagerTab #start-manager { margin: 1 1; }
    ManagerTab #manager-pane { height: 1fr; }
    """

    def __init__(
        self,
        project: ProjectInfo,
        *,
        escape_key: str,
        usage: Mapping[int, float] | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.project = project
        self.escape_key = escape_key
        self.status: FleetAgentStatus | None = None
        self.labels = SlotLabels()
        """The manager's account named as the Agent view names it, read off the UI thread."""
        self.usage: Mapping[int, float] = usage or {}
        """Slot → session percent USED, as the shell's slow headroom read last handed it."""

    def compose(self) -> ComposeResult:
        yield InfoLine(id="manager-header")
        hint = Static(Text(MANAGER_HINT, style="dim"), id="manager-hint")
        hint.display = False
        yield hint
        yield Button("Start manager", id="start-manager", variant="primary")
        yield TerminalPane(
            None, escape_key=self.escape_key, placeholder=PANE_GONE, id="manager-pane"
        )

    def on_mount(self) -> None:
        self.refresh_from_service()

    def show_usage(self, usage: Mapping[int, float]) -> None:
        """The accounts' session use from the shell's headroom read — never the 2 s tick."""
        if usage != self.usage:
            self.usage = usage
            self._paint_manager()

    @on(Worker.StateChanged)
    def _labels_answered(self, event: Worker.StateChanged) -> None:
        if self.labels.answered(event):
            self._paint_manager()

    def _paint_manager(self) -> None:
        """Repaint the header for the manager on show — the no-manager text is left as it is."""
        if self.status is not None and self.is_mounted:
            header = self.query_one("#manager-header", InfoLine)
            header.show_line(self.status, self.labels.labels, self.usage)

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
            status = FleetAgentStatus(agent=manager, socket_at=fleet_service.socket_at(manager))
        self.show(status)

    def show(
        self,
        status: FleetAgentStatus | None,
        *,
        unavailable: str | None = None,
        exited: FleetAgentStatus | None = None,
    ) -> None:
        """Render ``status`` — the pane when there is a manager, the button when not.

        ``exited`` is the ended manager row the snapshot still lists (#138); it
        only changes what the header above the button says. A ``lost`` manager, or one
        held as another server's, keeps its header and gets no pane
        (:func:`~aisquare.cli.ui.views.agent.shown_pane`): after a reboot or a
        ``kill-server`` its id is the next server's, most often ANOTHER project's
        manager, and this tab showed that one and typed into it.
        """
        self.status = status
        header = self.query_one("#manager-header", InfoLine)
        button = self.query_one("#start-manager", Button)
        pane = self.query_one("#manager-pane", TerminalPane)
        header.set_class(status is not None, "-info")
        self.query_one("#manager-hint", Static).display = status is not None
        if status is None:
            header.show_text(no_manager_text(self.project, unavailable, exited=exited))
            button.display = True
            pane.display = False
            if pane.pane_id is not None:
                pane.attach(None)
            return
        header.show_line(status, self.labels.labels, self.usage)
        self.labels.ask(self)
        button.display = False
        pane.display = True
        if (wanted := shown_pane(status)) != pane.pane_id:
            if wanted is not None:
                # The row's own server, where its spawn said it listened: `-L` by the name
                # alone, from another TMUX_TMPDIR, shows and types into another's %N.
                pane.server = TmuxServer(status.agent.tmux_socket).at(status.socket_at)
            pane.attach(wanted)

    @on(Button.Pressed, "#start-manager")
    def _start_manager(self) -> None:
        """Spawn the manager off the UI thread; the result arrives as a worker state."""
        self.query_one("#start-manager", Button).disabled = True
        # Measured here, on the UI thread, and handed to the worker: a widget's
        # size is read off the compositor, which is not thread-safe.
        size = self._pane_size()
        # exit_on_error=False: a FleetError is an answer to show, not a crash.
        self.run_worker(
            lambda: self._spawn_manager(size),
            name=SPAWN_WORKER,
            thread=True,
            exit_on_error=False,
        )

    def _spawn_manager(self, size: tuple[int, int] | None) -> fleet_service.SpawnReceipt:
        # The window is born the size of the pane that is about to show it
        # (#149): Claude Code's diff panel opens by itself past 144 columns, and
        # a window spawned at the old 200x50 default grew one before the pane's
        # first resize could shrink it — a panel the UI then could not close.
        return fleet_service.spawn(self.project, "manager", size=size)

    def _pane_size(self) -> tuple[int, int] | None:
        """The size the manager pane will have; ``None`` before the tab has one (the default).

        Read off the tab, not the pane: the button that asks only shows while
        the pane is hidden (:meth:`show`), and a hidden widget has no size. The
        pane takes the tab's full width — the number #149 is about — and the
        rows under the header; that height is an estimate, which the pane's
        first attach corrects (``TerminalPane._sync_size``).
        """
        width, height = self.content_size
        return (width, height - 3) if width > 0 and height > 3 else None

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
                    done = note in receipt.shared
                    self.notify(
                        note,
                        severity="information" if done else "warning",
                        timeout=8,
                        markup=False,
                    )
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
    """One project: its manager's pane first, then the board, AISquare and settings.

    ``usage`` is the shell's last per-slot session use (``HeadroomSummary.session_by_slot``),
    so a view opened between two headroom reads shows the figure at once.
    """

    TAB_IDS: ClassVar[tuple[str, ...]] = (
        "tab-manager",
        "tab-board",
        "tab-explainability",
        "tab-settings",
    )

    def __init__(
        self,
        project: ProjectInfo,
        *,
        id: str | None = None,
        refresh_seconds: float = 2.0,
        usage: Mapping[int, float] | None = None,
    ) -> None:
        super().__init__(id=id)
        self.project = project
        self.refresh_seconds = refresh_seconds
        # TabbedContent composes ITSELF from the panes handed to it — this is
        # what `with TabbedContent(): yield TabPane(...)` does at compose time,
        # so a subclass adds its panes here rather than overriding compose().
        escape_key = fleet_service.settings().escape_key
        panes = (
            TabPane(
                "Manager",
                ManagerTab(project, escape_key=escape_key, usage=usage, id="manager-tab"),
                id="tab-manager",
            ),
            TabPane(
                "Board",
                BoardPanel(project, interval=refresh_seconds, id="board-panel"),
                id="tab-board",
            ),
            TabPane(
                "AISquare",
                # ``project``: the key this tab shows and attaches is the one
                # THIS page's launches use (#141) — the project they join from
                # its root — not the ``project switch`` pin's.
                ExplainabilityView(project, id="project-explainability"),
                id="tab-explainability",
            ),
            TabPane("Settings", SettingsView(project, id="project-settings"), id="tab-settings"),
        )
        for pane in panes:
            self.compose_add_child(pane)
        self._pending: list[FleetAgentStatus] | None = None
        """A snapshot pushed before the tabs mounted, applied at mount."""

    def refresh_project(self, project: ProjectInfo) -> None:
        """Take the project as the store has it now — renamed from a card or the CLI, say.

        The Settings tab shows its nickname and codename, so it is handed the fresh
        row too; the other tabs read only the id and root, which do not change.
        """
        self.project = project
        for settings in self.query(SettingsView):
            settings.show_project(project)

    def refresh_status(self, agents: Sequence[FleetAgentStatus]) -> None:
        """Take the shell's fresh snapshot of this project's agents.

        The Manager tab shows the manager; the Settings tab, the tmux attach command. Safe
        before mount: the snapshot waits for the tabs and lands then.
        """
        tabs = self.query(ManagerTab)
        if not tabs:
            self._pending = list(agents)
            return
        tabs.first().show(manager_status(agents), exited=exited_manager(agents))
        for settings in self.query(SettingsView):
            settings.show_agents(agents)

    def show_usage(self, usage: Mapping[int, float]) -> None:
        """The accounts' session use from the shell's slow headroom read, for the Manager tab."""
        for tab in self.query(ManagerTab):
            tab.show_usage(usage)

    def on_mount(self) -> None:
        if self._pending is not None:
            pending, self._pending = self._pending, None
            self.refresh_status(pending)

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
