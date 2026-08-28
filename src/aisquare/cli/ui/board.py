"""The Board tab: the widgets of ``aisquare board -w``, lifted so the project view can host them.

Phase 6 moves the sessions/tasks/feed/detail widgets out of ``cli.watch`` into
here and has ``watch.run_watch`` compose them; until then this is a placeholder
that keeps the tab's contract (``BoardPanel(project)``).
"""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

from aisquare.models import ProjectInfo


class BoardPanel(Static):
    """Sessions, tasks and the live feed for one project."""

    def __init__(self, project: ProjectInfo, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.project = project

    def on_mount(self) -> None:
        self.update(
            Text(
                f"(board for {self.project.root.name or self.project.id} — lands in Phase 6; "
                "until then: aisquare board -w)",
                style="dim",
            )
        )
