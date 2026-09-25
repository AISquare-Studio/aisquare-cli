"""The Agent view: a header (label, role, state, task, cwd, exit) over the agent's pane.

docs/plans/fleet-tui.md §4.2: "header (label, role, state, model, cwd or
worktree, task) + actions + the ``TerminalPane``". The header is a one-line
:class:`rich.text.Text` built from a :class:`FleetAgentStatus` — every field
is DATA and is appended as text, never as markup, so a label or a path with
brackets in it reaches the screen intact (tests/test_console_markup.py's
rule). The shell calls :meth:`AgentView.refresh_status` on its poll; a changed
``pane_id`` (a restart) re-attaches the pane.

**Stop** and **Restart** are the actions §4.2 promised. Stop only asks: it
posts :class:`StopAgent` and the shell opens the dialog (``cli/ui/stop.py``),
which owns the service call — one confirmation, *Force* included, over the
same ``services.fleet.stop`` the CLI runs. It shows where
``sidebar.STOP_STATES`` says: while the agent HAS a process (``ALIVE_STATES``,
the constant the card's "agents alive" chip counts by), and on a 💤 exited
row, whose dead window `remain-on-exit` kept for the last screen (#138) — Stop
there removes it and the row leaves the listing. A ✗ lost row offers no Stop:
it is ``fleet reap``'s business. **Restart** (#138) runs the service off the
UI thread; the shell's next frame is what repaints, never an optimistic guess.
A restart resumes the agent's own session when its transcript is on disk, so
the manager comes back knowing its intake and its coders; the view then
selects the new row.
"""

from __future__ import annotations

import time
from collections.abc import Mapping

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, Static
from textual.worker import Worker, WorkerState

from aisquare.cli import fleet as fleet_cli
from aisquare.cli.ui.sidebar import ALIVE_STATES, ROLE_ICON, STATE_CHIP, STOP_STATES, StopAgent
from aisquare.cli.ui.terminal import TerminalPane
from aisquare.core.tmux import TmuxServer
from aisquare.models import FleetAgent, FleetAgentStatus
from aisquare.services import claude_accounts as accounts_service
from aisquare.services import fleet as fleet_service
from aisquare.services import team as team_service

SEPARATOR = "  "
LABELS_TTL = 30.0
"""How long the header keeps the slot labels before asking the registry again."""
LABELS_WORKER = "agent-account-labels"


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


RESTART_WORKER = "agent-restart"


class AgentView(Vertical):
    """One agent: who it is, the two actions that change it, then the live session.

    Stop asks the shell for its dialog (:class:`StopAgent`); the dialog runs the
    stop and owns its answer, including a refusal. Restart runs the service off
    the UI thread here and the view then selects the new row (#138).
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
        self._labels: Mapping[int, str] = {}
        self._labels_asked_at: float | None = None

    def _refresh_labels(self) -> None:
        """Ask the registry for the slot labels OFF the UI thread, at most every LABELS_TTL s.

        A store open is a blocking call with a busy timeout of seconds — the
        Accounts page runs its writes as thread workers for exactly that
        reason — so the header never opens it on the event loop: it paints the
        last good map (the built-in names before the first answer) and repaints
        when the worker answers (review of #205, second round).
        """
        now = time.monotonic()
        if self._labels_asked_at is not None and now - self._labels_asked_at < LABELS_TTL:
            return
        self._labels_asked_at = now
        self.run_worker(
            accounts_service.slot_labels,
            name=LABELS_WORKER,
            group=LABELS_WORKER,
            exclusive=True,
            thread=True,
            exit_on_error=False,
        )

    @on(Worker.StateChanged)
    def _labels_answered(self, event: Worker.StateChanged) -> None:
        if event.worker.group != LABELS_WORKER:
            return
        if event.state is WorkerState.SUCCESS and isinstance(event.worker.result, dict):
            self._labels = event.worker.result
            if self.is_mounted:
                self.query_one("#agent-header", Static).update(
                    header_text(self.status, self._labels)
                )

    def compose(self) -> ComposeResult:
        with Horizontal(id="agent-bar"):
            yield Static(header_text(self.status, self._labels), id="agent-header")
            yield Button("Stop", id="agent-stop", compact=True)
            yield Button("Restart", id="agent-restart", compact=True, variant="primary")
        yield from self.compose_bars()
        yield TerminalPane(
            self.status.agent.pane_id,
            server=self.server,
            escape_key=self.escape_key,
            id="agent-pane",
        )

    def compose_bars(self) -> ComposeResult:
        """More bars between the header and the pane — the captain's (``views/captain.py``,
        T4); an ordinary agent has none."""
        yield from ()

    def on_mount(self) -> None:
        self._paint_actions()
        self._refresh_labels()

    @property
    def pane(self) -> TerminalPane:
        return self.query_one("#agent-pane", TerminalPane)

    def refresh_status(self, status: FleetAgentStatus) -> None:
        """New facts about the same agent: redraw the header, re-attach on a new pane."""
        previous = self.status
        self.status = status
        if not self.is_mounted:
            return
        self.query_one("#agent-header", Static).update(header_text(status, self._labels))
        self._paint_actions()
        self._refresh_labels()
        if status.agent.pane_id != previous.agent.pane_id:
            self.pane.attach(status.agent.pane_id)

    def _paint_actions(self) -> None:
        """Stop where ``STOP_STATES`` says — a process, or a dead window; Restart always (an
        exited row is exactly its case).

        Greyed while THIS view's own restart runs, and only then: ``self.workers``
        is the app's whole list, and a finished worker is still in it when its
        ``StateChanged`` arrives — nothing else repaints the buttons afterwards (the shell
        feeds a view only when its status changed), so a failed restart would otherwise
        stay greyed with no way to retry it. A stop runs inside its dialog, which is
        modal: neither button can be pressed until it has answered.
        """
        busy = any(
            worker.node is self and worker.name == RESTART_WORKER and not worker.is_finished
            for worker in self.workers
        )
        stop = self.query_one("#agent-stop", Button)
        stop.display = self.status.state in STOP_STATES
        stop.disabled = busy
        stop.tooltip = (
            "/exit, a grace period, then the window is killed (aisquare fleet stop)"
            if self.status.state in ALIVE_STATES
            else "Remove the dead window tmux kept for the last screen; the row leaves the "
            "listing (aisquare fleet stop)"
        )
        restart = self.query_one("#agent-restart", Button)
        restart.disabled = busy
        restart.label = "Restart" if self.status.state in ALIVE_STATES else "Restart (resume)"
        restart.tooltip = (
            "Start it again under this label — same role, task, worktree, account and "
            "persona; its session is resumed from the transcript when that is on disk "
            "(aisquare fleet restart)"
        )

    @on(Button.Pressed, "#agent-stop")
    def _stop(self, event: Button.Pressed) -> None:
        event.stop()
        agent = self.status.agent
        self.post_message(StopAgent(agent.project_id, agent.id))

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
        if event.worker.name != RESTART_WORKER:
            return
        if event.state not in (WorkerState.SUCCESS, WorkerState.ERROR, WorkerState.CANCELLED):
            return
        label = self.status.agent.label
        if event.state is WorkerState.ERROR:
            self.notify(
                f"could not restart {label}: {event.worker.error}",
                severity="error",
                timeout=8,
                markup=False,
            )
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
