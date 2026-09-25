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
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, Static
from textual.worker import Worker, WorkerState

from aisquare.cli import fleet as fleet_cli
from aisquare.cli.ui.sidebar import ROLE_ICON, STATE_CHIP
from aisquare.cli.ui.terminal import PANE_GONE, TerminalPane
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


def shown_pane(status: FleetAgentStatus) -> str | None:
    """The pane a view of ``status`` shows and types into — ``None`` for a ``lost`` row.

    ``lost`` is the listing's word for a live row it found no pane of its own
    for, and one way to be lost leaves ANOTHER agent's pane under the row's
    id: a row that outlived its tmux server (a reboot, a hand-run
    ``kill-server``) keeps an id the next server hands out again
    (``services.fleet._outlived``). The service stopped acting on that pane,
    but a view attached by id still showed that agent's screen under the
    row's ``✗ lost`` header and forwarded every key typed there into it —
    another project's manager, typed at from this one's tab (review of the
    #203 final-round fixes, F1). The other way to be lost, a pane that is
    simply gone, has nothing to show either. ``unknown`` — tmux could not be
    asked — keeps the pane: that is no verdict about it.
    """
    return None if status.state == "lost" else status.agent.pane_id


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
        yield TerminalPane(
            shown_pane(self.status),
            server=self.server,
            escape_key=self.escape_key,
            placeholder=PANE_GONE,
            id="agent-pane",
        )

    def on_mount(self) -> None:
        self._paint_actions()
        self._refresh_labels()

    @property
    def pane(self) -> TerminalPane:
        return self.query_one("#agent-pane", TerminalPane)

    def refresh_status(self, status: FleetAgentStatus) -> None:
        """New facts about the same agent: redraw the header, re-attach on a new pane.

        Detached from a row that turned ``lost`` (:func:`shown_pane`), whose id may
        name another agent's pane by now.
        """
        self.status = status
        if not self.is_mounted:
            return
        self.query_one("#agent-header", Static).update(header_text(status, self._labels))
        self._paint_actions()
        self._refresh_labels()
        if (wanted := shown_pane(status)) != self.pane.pane_id:
            self.pane.attach(wanted)

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
            receipt = event.worker.result
            if isinstance(receipt, fleet_service.StopReceipt) and receipt.release_failed:
                # `fleet stop` prints this and exits 1: a claim left with the ended
                # session is not a clean stop, and the button must not read as one.
                self.notify(
                    f"claims: {receipt.release_failed}",
                    severity="warning",
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
