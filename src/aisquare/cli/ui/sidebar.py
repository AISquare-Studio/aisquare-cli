"""The left pane: Fleet ▸ projects (alternating background) ▸ agents ▸ Doctor.

Phase 1/4 (docs/plans/fleet-tui.md §4.1) builds the real cards and rows; the
messages below are the contract the app listens for and will not change.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Static

from aisquare.models import FleetAgentStatus, ProjectInfo

ROLE_ICON: dict[str, str] = {
    "manager": "🧭",
    "planner": "🧭",
    "coder": "🔨",
    "tester": "🧪",
    "runner": "🧪",
    "reviewer": "👀",
    "validator": "🛡",
    "remote": "📡",
}
STATE_CHIP: dict[str, tuple[str, str]] = {
    "working": ("▶", "green"),
    "waiting": ("⏸", "yellow"),
    "attention": ("🔔", "bold red"),
    "exited": ("💤", "dim"),
    "lost": ("✗", "red"),
    "unknown": ("·", "dim"),
}


class AddProject(Message):
    """The + beside Fleet."""


class ProjectSelected(Message):
    def __init__(self, project_id: str) -> None:
        self.project_id = project_id
        super().__init__()


class AgentSelected(Message):
    def __init__(self, project_id: str, agent_id: str) -> None:
        self.project_id = project_id
        self.agent_id = agent_id
        super().__init__()


class SpawnAgent(Message):
    def __init__(self, project_id: str) -> None:
        self.project_id = project_id
        super().__init__()


class DoctorSelected(Message):
    def __init__(self, project_id: str | None) -> None:
        self.project_id = project_id
        super().__init__()


class Sidebar(Vertical):
    """Header, the scrolling project list, and the Doctor section at the bottom."""

    DEFAULT_CSS = """
    Sidebar { width: 30; min-width: 24; border-right: solid $primary; }
    Sidebar #fleet-header { height: 1; padding: 0 1; text-style: bold; }
    Sidebar #projects { height: 1fr; }
    Sidebar #doctor-section {
        height: auto; max-height: 6; border-top: solid $primary; padding: 0 1;
    }
    Sidebar .project-card { padding: 0 1; }
    Sidebar .project-card.even { background: $surface; }
    Sidebar .project-card.odd { background: $panel; }
    """

    def compose(self) -> ComposeResult:
        yield Static("Fleet                       +", id="fleet-header")
        yield VerticalScroll(id="projects")
        yield Static("Doctor", id="doctor-section")

    def show_projects(
        self, projects: list[ProjectInfo], agents: dict[str, list[FleetAgentStatus]]
    ) -> None:
        """Rebuild the project list (Phase 1 replaces this with real cards)."""
        holder = self.query_one("#projects", VerticalScroll)
        holder.remove_children()
        for index, project in enumerate(projects):
            text = Text()
            text.append(f"🗂 {project.root.name or project.id}")
            if project.codename:
                text.append(f"  {project.codename}", style="dim")
            for status in agents.get(project.id, []):
                icon = ROLE_ICON.get(status.agent.role, "🤖")
                chip, style = STATE_CHIP.get(status.state, ("·", "dim"))
                text.append(f"\n   {icon} {status.agent.label} ")
                text.append(chip, style=style)
            card = Static(text, classes=f"project-card {'even' if index % 2 == 0 else 'odd'}")
            holder.mount(card)

    def show_doctor_summary(self, ok: int, warn: int, fail: int) -> None:
        summary = Text("Doctor  ", style="bold")
        summary.append(f"✓ {ok}  ", style="green")
        summary.append(f"⚠ {warn}  ", style="yellow")
        summary.append(f"✗ {fail}", style="red")
        self.query_one("#doctor-section", Static).update(summary)
