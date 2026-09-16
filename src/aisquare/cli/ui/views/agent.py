"""The Agent view: a header (label, role, state, task, cwd, exit) over the agent's pane.

docs/plans/fleet-tui.md §4.2: "header (label, role, state, model, cwd or
worktree, task) + actions + the ``TerminalPane``". The header is a one-line
:class:`rich.text.Text` built from a :class:`FleetAgentStatus` — every field
is DATA and is appended as text, never as markup, so a label or a path with
brackets in it reaches the screen intact (tests/test_console_markup.py's
rule). The shell calls :meth:`AgentView.refresh_status` on its poll; a changed
``pane_id`` (a restart) re-attaches the pane.

**Stop** is the first of the actions §4.2 promised. The button only asks: it
posts :class:`StopAgent` and the shell opens the dialog (``cli/ui/stop.py``),
which owns the service call. It shows while the agent HAS a process, and
``sidebar.ALIVE_STATES`` is that rule already — the constant the card's "agents
alive" chip counts by — so this asks it rather than keeping a second list that
could drift from it. An exited (💤) or lost (✗) row therefore offers no Stop:
there is no process to stop, and a lost row is ``fleet reap``'s business.
"""

from __future__ import annotations

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Static

from aisquare.cli.ui.sidebar import ALIVE_STATES, ROLE_ICON, STATE_CHIP, StopAgent
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
    """One agent: who it is, what can be done to it, then the live session."""

    DEFAULT_CSS = """
    AgentView #agent-bar { height: 1; }
    AgentView #agent-header { width: 1fr; height: 1; padding: 0 1; }
    AgentView #agent-bar Button { min-width: 9; margin: 0 1 0 0; }
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
        with Horizontal(id="agent-bar"):
            yield Static(header_text(self.status), id="agent-header")
            yield Button("Stop", id="agent-stop", compact=True)
        yield TerminalPane(
            self.status.agent.pane_id,
            server=self.server,
            escape_key=self.escape_key,
            id="agent-pane",
        )

    def on_mount(self) -> None:
        self._paint_stop()

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
        self._paint_stop()
        if status.agent.pane_id != previous.agent.pane_id:
            self.pane.attach(status.agent.pane_id)

    def _paint_stop(self) -> None:
        """Stop shows while there is a process to stop — ``ALIVE_STATES`` IS that rule."""
        stop = self.query_one("#agent-stop", Button)
        stop.display = self.status.state in ALIVE_STATES
        stop.tooltip = "/exit, a grace period, then the window is killed (aisquare fleet stop)"

    @on(Button.Pressed, "#agent-stop")
    def _stop(self, event: Button.Pressed) -> None:
        event.stop()
        agent = self.status.agent
        self.post_message(StopAgent(agent.project_id, agent.id))
