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

import time
from collections.abc import Mapping

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from aisquare.cli import fleet as fleet_cli
from aisquare.cli.ui.sidebar import ROLE_ICON, STATE_CHIP
from aisquare.cli.ui.terminal import TerminalPane
from aisquare.core.tmux import TmuxServer
from aisquare.models import FleetAgentStatus
from aisquare.services import claude_accounts as accounts_service
from aisquare.services import fleet as fleet_service
from aisquare.services import team as team_service

SEPARATOR = "  "
LABELS_TTL = 30.0
"""How long the header keeps the slot labels before asking the registry again."""


def account_text(status: FleetAgentStatus, labels: Mapping[int, str] | None = None) -> str:
    """Which Claude account the agent runs under, or ``""`` when nothing says.

    The slot the spawn RESOLVED to comes first (``FleetAgent.account_slot``,
    #145): it is known before the agent has said a word, and it is what the
    operator chose. Failing that, the config directory the session's first
    hook reported (``TeamSession.account``) — the right answer for an agent
    started by hand or before #145 — through the same label the board uses.
    ``labels`` (``services.claude_accounts.slot_labels``) names the slot the
    way the launch line and the Accounts page do — the alias, ``plain claude``
    for slot 1 — so one account is not ``work`` there and ``account 2`` here
    (review of #205, finding 10); without them the built-in name is used.
    """
    slot = status.agent.account_slot
    if slot is not None:
        return fleet_cli.slot_label(slot, labels)
    if status.session is not None and status.session.account:
        return team_service.account_label(status.session.account, labels) or ""
    return ""


def header_text(status: FleetAgentStatus, labels: Mapping[int, str] | None = None) -> Text:
    """One line: ``🔨 coder-auth  coder  ▶ working  account 2  task 01k…  ~/repo ⎇  exited 1``."""
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
    on = account_text(status, labels)
    if on:
        text.append(SEPARATOR + on, style="dim")
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
        self._labels: Mapping[int, str] = {}
        self._labels_read_at: float | None = None

    def _account_labels(self) -> Mapping[int, str]:
        """Slot → label, re-read at most every :data:`LABELS_TTL` seconds (one small store read)."""
        now = time.monotonic()
        if self._labels_read_at is None or now - self._labels_read_at >= LABELS_TTL:
            self._labels = accounts_service.slot_labels()
            self._labels_read_at = now
        return self._labels

    def compose(self) -> ComposeResult:
        yield Static(header_text(self.status, self._account_labels()), id="agent-header")
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
        self.query_one("#agent-header", Static).update(header_text(status, self._account_labels()))
        if status.agent.pane_id != previous.agent.pane_id:
            self.pane.attach(status.agent.pane_id)
