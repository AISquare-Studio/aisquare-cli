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
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, Static
from textual.worker import Worker, WorkerState

from aisquare.cli.ui.sidebar import ROLE_ICON, STATE_CHIP
from aisquare.cli.ui.terminal import TerminalPane
from aisquare.core.tmux import TmuxServer
from aisquare.models import FleetAgent, FleetAgentStatus
from aisquare.services import fleet as fleet_service
from aisquare.services import team as team_service

SEPARATOR = "  "


def account_text(status: FleetAgentStatus) -> str:
    """Which Claude account the agent runs under, or ``""`` when nothing says.

    The slot the spawn RESOLVED to comes first (``FleetAgent.account_slot``,
    #145): it is known before the agent has said a word, and it is what the
    operator chose. Failing that, the config directory the session's first
    hook reported (``TeamSession.account``) — the right answer for an agent
    started by hand or before #145 — through the same label the board uses.
    """
    slot = status.agent.account_slot
    if slot is not None:
        return "plain claude" if slot == 1 else f"account {slot}"
    if status.session is not None and status.session.account:
        return team_service.account_label(status.session.account) or ""
    return ""


def header_text(status: FleetAgentStatus) -> Text:
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
    on = account_text(status)
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


STOP_WORKER = "agent-stop"
RESTART_WORKER = "agent-restart"
#: States in which there is a process to stop; anything else is a row to restart.
_STOPPABLE: frozenset[str] = frozenset({"working", "waiting", "attention", "limited", "unknown"})
#: Where **Stop** is offered: a process to stop, or an exited agent's dead window —
#: `remain-on-exit` keeps it for the last screen, and Stop on the 💤 row removes it,
#: which takes the row off the listing (``fleet stop`` on an ended row). Without it a
#: 💤 row whose restart is refused (a coder whose task is done) could not be cleared.
_SHOWS_STOP: frozenset[str] = _STOPPABLE | {"exited"}


class AgentView(Vertical):
    """One agent: who it is, the two actions that change it, then the live session.

    **Stop** and **Restart** (#138) are the actions §4.2 promised and this view
    never had: a manager ended with ctrl+c inside its window showed 💤 forever
    with nothing to click, and ``fleet spawn manager`` refused until a hand-run
    ``reap``. Both run the service off the UI thread; the shell's next frame is
    what repaints, never an optimistic guess. A restart resumes the agent's own
    session when its transcript is on disk, so the manager comes back knowing
    its intake and its coders; the view then selects the new row.
    """

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
            yield Button("Restart", id="agent-restart", compact=True, variant="primary")
        yield TerminalPane(
            self.status.agent.pane_id,
            server=self.server,
            escape_key=self.escape_key,
            id="agent-pane",
        )

    def on_mount(self) -> None:
        self._paint_actions()

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
        self._paint_actions()
        if status.agent.pane_id != previous.agent.pane_id:
            self.pane.attach(status.agent.pane_id)

    def _paint_actions(self) -> None:
        """Stop while there is a process or a dead window; Restart always (an exited row is
        exactly its case).

        Greyed while THIS view's own stop or restart runs, and only then: ``self.workers``
        is the app's whole list, and a finished worker is still in it when its
        ``StateChanged`` arrives — nothing else repaints the buttons afterwards (the shell
        feeds a view only when its status changed), so a failed restart would otherwise
        stay greyed with no way to retry it.
        """
        busy = any(
            worker.node is self
            and worker.name in (STOP_WORKER, RESTART_WORKER)
            and not worker.is_finished
            for worker in self.workers
        )
        stop = self.query_one("#agent-stop", Button)
        stop.display = self.status.state in _SHOWS_STOP
        stop.disabled = busy
        stop.tooltip = (
            "/exit, a grace period, then the window is killed (aisquare fleet stop)"
            if self.status.state in _STOPPABLE
            else "Remove the dead window tmux kept for the last screen; the row leaves the "
            "listing (aisquare fleet stop)"
        )
        restart = self.query_one("#agent-restart", Button)
        restart.disabled = busy
        restart.label = "Restart" if self.status.state in _STOPPABLE else "Restart (resume)"
        restart.tooltip = (
            "Start it again under this label — same role, task, worktree and account; its "
            "session is resumed from the transcript when that is on disk (aisquare fleet restart)"
        )

    @on(Button.Pressed, "#agent-stop")
    def _stop(self, event: Button.Pressed) -> None:
        event.stop()
        agent = self.status.agent
        # Pinned to THIS row (``agent_id``), never to whoever holds the label now: the
        # view outlives its row, and a 💤 view's Stop by label stopped the replacement.
        self.run_worker(
            lambda: fleet_service.stop(
                fleet_service.project_of(agent), agent.label, agent_id=agent.id
            ),
            name=STOP_WORKER,
            group=STOP_WORKER,
            exclusive=True,
            thread=True,
            exit_on_error=False,
        )
        self._paint_actions()

    @on(Button.Pressed, "#agent-restart")
    def _restart(self, event: Button.Pressed) -> None:
        event.stop()
        agent = self.status.agent
        width, height = self.pane.content_size
        size = (width, height) if width > 0 and height > 0 else None
        self.run_worker(
            lambda: fleet_service.restart(
                fleet_service.project_of(agent), agent.label, size=size, agent_id=agent.id
            ),
            name=RESTART_WORKER,
            group=RESTART_WORKER,
            exclusive=True,
            thread=True,
            exit_on_error=False,
        )
        self._paint_actions()

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name not in (STOP_WORKER, RESTART_WORKER):
            return
        if event.state not in (WorkerState.SUCCESS, WorkerState.ERROR, WorkerState.CANCELLED):
            return
        label = self.status.agent.label
        if event.state is WorkerState.ERROR:
            verb = "stop" if event.worker.name == STOP_WORKER else "restart"
            self.notify(
                f"could not {verb} {label}: {event.worker.error}",
                severity="error",
                timeout=8,
                markup=False,
            )
        elif event.worker.name == STOP_WORKER:
            self.notify(f"✓ stopped {label}", timeout=5, markup=False)
        else:
            receipt = event.worker.result
            if isinstance(receipt, fleet_service.RestartReceipt):
                how = "resumed its session" if receipt.resumed else "started fresh"
                self.notify(f"✓ restarted {label} — {how}", timeout=6, markup=False)
                for note in receipt.notes:
                    self.notify(note, severity="warning", timeout=8, markup=False)
                self.post_message(AgentRestarted(receipt.started))
        self._paint_actions()
        refresh = getattr(self.app, "refresh_data", None)
        if callable(refresh):
            refresh()


class AgentRestarted(Message):
    """A restart produced a new row; the shell selects it once its frame lists it."""

    def __init__(self, agent: FleetAgent) -> None:
        super().__init__()
        self.agent = agent
