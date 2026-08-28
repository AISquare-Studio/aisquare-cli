"""The Agent view: a header (label, role, state, cwd, task) over the agent's terminal pane."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from aisquare.cli.ui.terminal import TerminalPane
from aisquare.models import FleetAgentStatus


def header_text(status: FleetAgentStatus) -> Text:
    agent = status.agent
    text = Text()
    text.append(f"{agent.label}", style="bold")
    text.append(f"  {agent.role}", style="cyan")
    text.append(f"  {status.state}", style="yellow")
    if agent.task_id:
        text.append(f"  task {agent.task_id[-8:]}", style="dim")
    text.append(f"  {agent.cwd}", style="dim")
    return text


class AgentView(Vertical):
    """One agent: who it is, then the live session."""

    DEFAULT_CSS = """
    AgentView #agent-header { height: 1; padding: 0 1; }
    """

    def __init__(self, status: FleetAgentStatus, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.status = status

    def compose(self) -> ComposeResult:
        yield Static(header_text(self.status), id="agent-header")
        yield TerminalPane(self.status.agent.pane_id, id="agent-pane")
