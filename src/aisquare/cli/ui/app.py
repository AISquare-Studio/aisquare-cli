"""``FleetApp`` — the two-pane shell, and ``run_ui`` which bare ``asq`` calls.

Left: the ``Sidebar``. Right: a ``ContentSwitcher`` over the views. State is
never held here beyond what is on screen: projects come from the store, agents
from the fleet service, both re-read on a timer exactly as ``board -w`` does.
Phase 1 (docs/plans/fleet-tui.md §9) fills this out; the skeleton already opens
at a terminal and lists the registered projects.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import ContentSwitcher

from aisquare.cli.ui.sidebar import AddProject, Sidebar
from aisquare.cli.ui.views.welcome import WelcomeView
from aisquare.core.store import store_session
from aisquare.models import FleetAgentStatus, ProjectInfo
from aisquare.services import fleet as fleet_service


class FleetApp(App[None]):
    """One `asq` view over every project, agent and session."""

    TITLE = "aisquare"
    CSS = """
    #main { height: 1fr; }
    #content { width: 1fr; }
    """
    BINDINGS: ClassVar = [("q", "quit", "quit")]

    def __init__(self, *, refresh_seconds: float = 2.0) -> None:
        super().__init__()
        self.refresh_seconds = refresh_seconds

    def compose(self) -> ComposeResult:
        with Horizontal(id="main"):
            yield Sidebar(id="sidebar")
            with ContentSwitcher(id="content", initial="welcome"):
                yield WelcomeView(id="welcome")

    def on_mount(self) -> None:
        self.refresh_data()
        self.set_interval(self.refresh_seconds, self.refresh_data)

    def refresh_data(self) -> None:
        """Re-read projects and agents; keep the last frame if the store is busy."""
        try:
            with store_session() as store:
                projects = store.list_projects()
        except Exception:  # the store is briefly unavailable — keep what is shown
            return
        agents: dict[str, list[FleetAgentStatus]] = {}
        for project in projects:
            try:
                agents[project.id] = fleet_service.list_agents(project)
            except fleet_service.FleetError:
                agents[project.id] = []
        self.query_one(Sidebar).show_projects(projects, agents)

    def on_add_project(self, event: AddProject) -> None:
        self.notify("onboarding lands in Phase 2", timeout=3)

    @staticmethod
    def projects() -> list[ProjectInfo]:
        with store_session() as store:
            return store.list_projects()


def run_ui() -> None:
    """Run the fleet UI until the user quits."""
    FleetApp().run()
