"""The Project view: Manager · Board · Doctor · Explainability · Settings tabs."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.widgets import Static, TabbedContent, TabPane

from aisquare.cli.ui.board import BoardPanel
from aisquare.cli.ui.terminal import TerminalPane
from aisquare.cli.ui.views.doctor import DoctorView
from aisquare.models import ProjectInfo


class ProjectView(TabbedContent):
    """One project: its manager's pane first, then the board, doctor and settings."""

    def __init__(self, project: ProjectInfo, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.project = project

    def compose(self) -> ComposeResult:
        with TabPane("Manager", id="tab-manager"):
            yield TerminalPane(None, id="manager-pane")
        with TabPane("Board", id="tab-board"):
            yield BoardPanel(self.project, id="board-panel")
        with TabPane("Doctor", id="tab-doctor"):
            yield DoctorView(id="project-doctor")
        with TabPane("Explainability", id="tab-explainability"):
            yield Static("(explainability — lands in Phase 6)", classes="dim")
        with TabPane("Settings", id="tab-settings"):
            yield Static("(settings — lands in Phase 6)", classes="dim")
