"""The Agent view: a header (label, role, state, task, cwd, exit) over the agent's pane.

docs/plans/fleet-tui.md §4.2: "header (label, role, state, model, cwd or
worktree, task) + actions + the ``TerminalPane``". The header is a one-line
:class:`rich.text.Text` built from a :class:`FleetAgentStatus` — every field
is DATA and is appended as text, never as markup, so a label or a path with
brackets in it reaches the screen intact (tests/test_console_markup.py's
rule). The shell calls :meth:`AgentView.refresh_status` on its poll; a changed
``pane_id`` (a restart) re-attaches the pane. Actions (stop, restart, open in
tmux, transcript) are the shell's buttons and land with it.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from aisquare.cli.ui.sidebar import ROLE_ICON, STATE_CHIP
from aisquare.cli.ui.terminal import TerminalPane
from aisquare.core.tmux import TmuxServer
from aisquare.models import FleetAgentStatus
from aisquare.services import fleet as fleet_service

SEPARATOR = "  "


def header_text(status: FleetAgentStatus) -> Text:
    """One line: ``🔨 coder-auth  coder  ▶ working  task 01k…  ~/repo ⎇  exited 1``."""
    agent = status.agent
    chip, chip_style = STATE_CHIP.get(status.state, ("·", "dim"))
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(f"{ROLE_ICON.get(agent.role, '🤖')} ")
    text.append(agent.label, style="bold")
    text.append(SEPARATOR + agent.role, style="cyan")
    text.append(SEPARATOR)
    text.append(f"{chip} {status.state}", style=chip_style)
    if status.detail:
        text.append(f" ({status.detail})", style="dim")
    if status.session is not None and status.session.model:
        text.append(SEPARATOR + status.session.model, style="dim")
    if agent.task_id:
        text.append(SEPARATOR + f"task {agent.task_id[-8:]}", style="dim")
    text.append(SEPARATOR + str(agent.cwd), style="dim")
    if agent.worktree:
        text.append(" ⎇", style="dim")
    if agent.exit_status is not None:
        text.append(SEPARATOR + f"exited {agent.exit_status}", style="bold red")
    return text


class AgentView(Vertical):
    """One agent: who it is, then the live session."""

    DEFAULT_CSS = """
    AgentView #agent-header { height: 1; padding: 0 1; }
    AgentView #agent-pane { height: 1fr; }
    """

    def __init__(
        self,
        status: FleetAgentStatus,
        *,
        server: TmuxServer | None = None,
        escape_key: str | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.status = status
        # Both are defaults the caller may override (§3.10): the agent's own
        # socket names its server; the escape key comes from ``[fleet]``.
        self.server = server or TmuxServer(status.agent.tmux_socket)
        self.escape_key = escape_key or fleet_service.settings().escape_key

    def compose(self) -> ComposeResult:
        yield Static(header_text(self.status), id="agent-header")
        yield TerminalPane(
            self.status.agent.pane_id,
            server=self.server,
            escape_key=self.escape_key,
            id="agent-pane",
        )

    @property
    def pane(self) -> TerminalPane:
        return self.query_one("#agent-pane", TerminalPane)

    def refresh_status(self, status: FleetAgentStatus) -> None:
        """New facts about the same agent: redraw the header, re-attach on a new pane."""
        previous = self.status
        self.status = status
        if not self.is_mounted:
            return
        self.query_one("#agent-header", Static).update(header_text(status))
        if status.agent.pane_id != previous.agent.pane_id:
            self.pane.attach(status.agent.pane_id)
