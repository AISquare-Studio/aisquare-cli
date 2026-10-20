"""The Agent view: the agent info line, Stop and Restart, over the agent's pane.

The info line (:func:`info_line`) is the one the Manager tab shows too: who the
agent is, its state, the model and effort it runs, the account it runs on with
that account's session use, and its pane — one builder, so the two never drift
apart again. It is a one-line :class:`rich.text.Text` built from a
:class:`FleetAgentStatus`; every field is DATA and is appended as text, never as
markup, so a label or a model name with brackets in it reaches the screen intact
(tests/test_console_markup.py's rule). The shell calls
:meth:`AgentView.refresh_status` on its poll, and :meth:`AgentView.show_usage`
when its slow headroom read lands; a changed ``pane_id`` (a restart) re-attaches
the pane.
"""

from __future__ import annotations

import time
from collections.abc import Mapping

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Button, Static
from textual.worker import Worker, WorkerState

from aisquare.cli import fleet as fleet_cli
from aisquare.cli.ui.click_guard import GuardedButton
from aisquare.cli.ui.sidebar import ROLE_ICON, STATE_CHIP, session_percent_text
from aisquare.cli.ui.terminal import PANE_GONE, TerminalPane
from aisquare.core.tmux import TmuxServer
from aisquare.models import FleetAgent, FleetAgentStatus
from aisquare.services import claude_accounts as accounts_service
from aisquare.services import fleet as fleet_service
from aisquare.services import team as team_service

SEPARATOR = "  "
LABELS_TTL = 30.0
"""How long a view keeps the slot labels before asking the registry again."""
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


LABEL_STYLE = "bold"
DETAIL_STYLE = "italic"
"""Not dim: the ``💤`` and ``·`` chips are, and the detail follows them."""
MODEL_STYLE = "dim cyan"
EFFORT_STYLE = "cyan"
ACCOUNT_STYLE = "blue"
PANE_STYLE = "dim italic"
"""Dim, and italic so it never runs into a dim ``💤`` or ``·`` chip with nothing between."""


def info_line(
    status: FleetAgentStatus,
    labels: Mapping[int, str] | None = None,
    usage: Mapping[int, float] | None = None,
) -> Text:
    """``🔨 coder-auth  ▶ working  claude-opus-5-5 high  dev · 87%  %4`` — one agent at a glance.

    The Agent view's header and the Manager tab's first line, both: the role icon and the
    label, the state chip (``💤 exited(N)`` once it ended), the state's detail, the model
    and the effort the session reported (``TeamSession.effort``, left out while unknown),
    the account (:func:`account_text`, its NAME alone — no ``#slot``) with its five-hour
    window percent USED from ``usage`` (slot → percent, the slow headroom read's
    ``session_by_slot``), and the pane id. No role word, task, working directory or tmux
    command: the sidebar row says the role, and the attach command is the Settings tab's.

    Each field is a style of its own and no two neighbours share one, so each reads apart
    at a glance; every one is appended as data.
    """
    agent = status.agent
    session = status.session
    chip, chip_style = STATE_CHIP.get(status.state, ("·", "dim"))
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(f"{ROLE_ICON.get(agent.role, '🤖')} ")
    text.append(agent.label, style=LABEL_STYLE)
    text.append(SEPARATOR)
    exited = status.state == "exited" and agent.exit_status is not None
    code = f"({agent.exit_status})" if exited else ""
    text.append(f"{chip} {status.state}{code}", style=chip_style)
    # An exited row's detail is its exit code, which the chip has just said.
    if status.detail and not (exited and status.detail == f"exit {agent.exit_status}"):
        text.append(SEPARATOR)
        text.append(status.detail, style=DETAIL_STYLE)
    if session is not None and session.model:
        text.append(SEPARATOR)
        text.append(session.model, style=MODEL_STYLE)
    if session is not None and session.effort:
        text.append(" " if session.model else SEPARATOR)
        text.append(session.effort, style=EFFORT_STYLE)
    account = account_text(status, labels)
    if account:
        text.append(SEPARATOR)
        text.append(account, style=ACCOUNT_STYLE)
        slot = agent.account_slot
        percent = (usage or {}).get(slot) if slot is not None else None
        if percent is not None:
            text.append(" · ", style="dim")
            text.append_text(session_percent_text(percent))
    text.append(SEPARATOR)
    text.append(agent.pane_id, style=PANE_STYLE)
    return text


class SlotLabels:
    """The slot labels a view names accounts by, asked of the registry OFF the UI thread.

    A store open is a blocking call with a busy timeout of seconds — the Accounts page
    runs its writes as thread workers for exactly that reason — so a view never opens it
    on the event loop: it paints the last good map (the built-in names before the first
    answer) and repaints when the worker answers (review of #205, second round). The
    Agent view and the Manager tab each hold one; at most one read per ``LABELS_TTL``.
    """

    def __init__(self) -> None:
        self.labels: Mapping[int, str] = {}
        self._asked_at: float | None = None

    def ask(self, node: Widget) -> None:
        """Read the labels in a thread worker of ``node``'s, unless one was asked lately."""
        now = time.monotonic()
        if self._asked_at is not None and now - self._asked_at < LABELS_TTL:
            return
        self._asked_at = now
        node.run_worker(
            accounts_service.slot_labels,
            name=LABELS_WORKER,
            group=LABELS_WORKER,
            exclusive=True,
            thread=True,
            exit_on_error=False,
        )

    def answered(self, event: Worker.StateChanged) -> bool:
        """Take the map a read of :meth:`ask`'s answered; whether there is one to paint."""
        if event.worker.group != LABELS_WORKER or event.state is not WorkerState.SUCCESS:
            return False
        result = event.worker.result
        if not isinstance(result, dict):
            return False
        self.labels = result
        return True


def shown_pane(status: FleetAgentStatus) -> str | None:
    """The pane a view of ``status`` shows and types into — ``None`` for a ``lost`` row, and
    for one the listing held as another server's.

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

    A row that recorded its server's start is not ``lost`` under the next server but held
    (``FleetAgentStatus.held``), and reads ``unknown`` (another tmux server on its socket):
    its id is the next server's all the same, so it gets no pane either. Told by the state
    word alone, the tabs showed and typed into another agent's pane under it (review of
    #259, round 1).
    """
    return None if status.state == "lost" or status.held else status.agent.pane_id


STOP_WORKER = "agent-stop"
RESTART_WORKER = "agent-restart"
#: States in which there is a process to stop; anything else is a row to restart. The
#: service's own set, which `fleet restart|stop --all` acts on.
_STOPPABLE: frozenset[str] = fleet_service.PROCESS_STATES
#: Where **Stop** is offered: a process to stop, or an exited agent's dead window —
#: `remain-on-exit` keeps it for the last screen, and Stop on the 💤 row removes it,
#: which takes the row off the listing (``fleet stop`` on an ended row). Without it a
#: 💤 row whose restart is refused (a coder whose task is done) could not be cleared.
_SHOWS_STOP: frozenset[str] = _STOPPABLE | {"exited"}


class AgentView(Vertical):
    """One agent: its info line, the two actions that change it, then the live session.

    **Stop** and **Restart** (#138) are the actions §4.2 promised and this view
    never had: a manager ended with ctrl+c inside its window showed 💤 forever
    with nothing to click, and ``fleet spawn manager`` refused until a hand-run
    ``reap``. Both run the service off the UI thread; the shell's next frame is
    what repaints, never an optimistic guess. A restart resumes the agent's own
    session when its transcript is on disk, so the manager comes back knowing
    its intake and its coders; the shell then shows the new row if this view is
    still the one on screen (:class:`AgentRestarted`). Both buttons ignore a mouse
    press that lands just as the view came up or moved under the pointer
    (:class:`~aisquare.cli.ui.click_guard.GuardedButton`).
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
        usage: Mapping[int, float] | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.status = status
        # Both are defaults the caller may override (§3.10): the agent's own
        # socket names its server, asked where its spawn said it listened
        # (`FleetAgentStatus.socket_at`); the escape key comes from ``[fleet]``.
        self.server = server or TmuxServer(status.agent.tmux_socket).at(status.socket_at)
        self.escape_key = escape_key or fleet_service.settings().escape_key
        self.labels = SlotLabels()
        self.usage: Mapping[int, float] = usage or {}
        """Slot → session percent USED, as the shell's slow headroom read last handed it."""

    def _info(self) -> Text:
        return info_line(self.status, self.labels.labels, self.usage)

    def _paint_info(self) -> None:
        if self.is_mounted:
            self.query_one("#agent-header", Static).update(self._info())

    @on(Worker.StateChanged)
    def _labels_answered(self, event: Worker.StateChanged) -> None:
        if self.labels.answered(event):
            self._paint_info()

    def show_usage(self, usage: Mapping[int, float]) -> None:
        """The accounts' session use from the shell's headroom read — never the 2 s tick."""
        if usage != self.usage:
            self.usage = usage
            self._paint_info()

    def compose(self) -> ComposeResult:
        with Horizontal(id="agent-bar"):
            yield Static(self._info(), id="agent-header")
            # Guarded (``click_guard``): a click that was aimed at what this view replaced
            # must not stop or restart the agent it shows instead.
            yield GuardedButton("Stop", id="agent-stop", compact=True)
            yield GuardedButton("Restart", id="agent-restart", compact=True, variant="primary")
        yield TerminalPane(
            shown_pane(self.status),
            server=self.server,
            escape_key=self.escape_key,
            placeholder=PANE_GONE,
            id="agent-pane",
        )

    def on_mount(self) -> None:
        self._paint_actions()
        self.labels.ask(self)

    @property
    def pane(self) -> TerminalPane:
        return self.query_one("#agent-pane", TerminalPane)

    def refresh_status(self, status: FleetAgentStatus) -> None:
        """New facts about the same agent: redraw the info line, re-attach on a new pane.

        Detached from a row that turned ``lost`` or was held as another server's
        (:func:`shown_pane`), whose id may name another agent's pane by now.
        """
        self.status = status
        if not self.is_mounted:
            return
        self._paint_info()
        self._paint_actions()
        self.labels.ask(self)
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
            "/exit, a grace period, then the window is killed (as `fleet stop` does)"
            if self.status.state in _STOPPABLE
            else "Remove the dead window tmux kept for the last screen; the row leaves the "
            "listing (as `fleet stop` does)"
        )
        restart = self.query_one("#agent-restart", Button)
        restart.disabled = busy
        restart.label = "Restart" if self.status.state in _STOPPABLE else "Restart (resume)"
        restart.tooltip = (
            "Start it again under this label — same role, task, worktree and account; its "
            "session is resumed from the transcript when that is on disk (as `fleet restart` "
            "does)"
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
                # The shell says it: it knows the project to name, and whether this view
                # is still the one on screen by now (:class:`AgentRestarted`).
                self.post_message(AgentRestarted(receipt))
        self._paint_actions()
        refresh = getattr(self.app, "refresh_data", None)
        if callable(refresh):
            refresh()


class AgentRestarted(Message):
    """A restart replaced a row with a new one; the shell says so, and shows the new row
    only when the replaced row's view is the one on screen.

    A restart runs for seconds, and the user may have moved on before it ends: shown
    unconditionally, the new row took the screen back from whatever the user had opened
    since, its Stop and Restart under the pointer that was aimed at the other view's
    pane (user feedback, 2026-10-19: a manager destroyed that way more than once).
    """

    def __init__(self, receipt: fleet_service.RestartReceipt) -> None:
        super().__init__()
        self.receipt = receipt

    @property
    def agent(self) -> FleetAgent:
        """The row the restart started."""
        return self.receipt.started

    @property
    def replaced(self) -> FleetAgent:
        """The row it replaced: the one whose view, if shown, follows to the new row."""
        return self.receipt.replaced
