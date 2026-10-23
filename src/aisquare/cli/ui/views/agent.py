"""The Agent view: the agent info line, Stop and Restart, over the agent's pane.

The info line (:func:`info_line`) is the one the Manager tab shows too: who the
agent is, its state, the account it runs on with that account's session use, the
model and effort it runs, and its pane — one builder, so the two never drift
apart again. It is a one-line :class:`rich.text.Text` built from a
:class:`FleetAgentStatus`; every field is DATA and is appended as text, never as
markup, so a label or a model name with brackets in it reaches the screen intact
(tests/test_console_markup.py's rule). The shell calls
:meth:`AgentView.refresh_status` on its poll, and :meth:`AgentView.show_usage`
when its slow headroom read lands; a changed ``pane_id`` (a restart) re-attaches
the pane.

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
from aisquare.cli.ui.sidebar import (
    ROLE_ICON,
    STATE_CHIP,
    STOP_STATES,
    StopAgent,
    session_percent_text,
)
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
"""Not dim: the pane id after it is, and so are the ``💤`` and ``·`` chips it can follow."""
MODEL_STYLE = "dim cyan"
EFFORT_STYLE = "cyan"
ACCOUNT_STYLE = "blue"
PANE_STYLE = "dim italic"
"""Dim, and italic so it never runs into a dim ``💤`` or ``·`` chip with nothing between."""


LABEL_KEEP = 6
"""The label characters a line too narrow for its row keeps before its ``…``: below that the
label stays at this and the line is cut at its end as any other (the owner's ruling 3)."""


def info_line(
    status: FleetAgentStatus,
    labels: Mapping[int, str] | None = None,
    usage: Mapping[int, float] | None = None,
    *,
    width: int | None = None,
) -> Text:
    """``🔨 coder-auth  ▶ working  dev · 87%  claude-opus-5-5 high  %4`` — one agent at a glance.

    The Agent view's header and the Manager tab's first line, both:
    ``<icon> <label>  <chip> <state>[(exit)]  <account> · <pct>%  <model> <effort>  [detail]
    <pane id>``. The role icon and the label; the state chip (``💤 exited(N)`` once it
    ended); the account (:func:`account_text`, its NAME alone — no ``#slot``) with its
    five-hour window percent USED from ``usage`` (slot → percent, the slow headroom read's
    ``session_by_slot``); the model and the effort the session reported
    (``TeamSession.effort``, left out while unknown); the state's detail; and the pane id.
    No role word, task, working directory or tmux command: the sidebar row says the role,
    and the attach command is the Settings tab's.

    The order is the owner's, ruled on #267: a line too long for its row is cut at its end,
    so what matters most at a glance comes first. The account and its use right after the
    state — in a 90-column terminal the Agent view's line gets about 38 cells, and a model
    name before them pushed them off. The detail last but the pane id: it has no bound (the
    folder-trust prompt's names a command and a path), so it is what gives way first.

    ``width`` is the row's, once it has one (:class:`InfoLine`). A line wider than it gives
    way in its LABEL first, as the sidebar's rows do: the label is ellipsised so that
    ``<icon> <label…>  <chip> <state>  <account> · <pct>%`` and the ``…`` that cuts the rest
    fit — a spawned agent's ``coder-01m5d34v`` pushed ``work · 42%`` off 37 cells. It keeps
    :data:`LABEL_KEEP` characters at least; a row too narrow even then is cut at its end
    (``plain claude · 42%`` does not fit beside ``coder-…`` there). A line that fits keeps
    its whole label.

    Each field is a style of its own and no two neighbours share one, so each reads apart
    at a glance; every one is appended as data.
    """
    label = Text(status.agent.label, style=LABEL_STYLE)
    text, essential = _info_line(status, label, labels, usage)
    if width is not None and text.cell_len > width:
        # The label's room: the row, less what must stay whole after it, less one cell for
        # the ``…`` that cuts the rest — the pane id always follows.
        room = width - 1 - (essential - label.cell_len)
        if label.cell_len > room:
            label.truncate(max(room, LABEL_KEEP + 1), overflow="ellipsis")
            text, _ = _info_line(status, label, labels, usage)
    return text


def _info_line(
    status: FleetAgentStatus,
    label: Text,
    labels: Mapping[int, str] | None,
    usage: Mapping[int, float] | None,
) -> tuple[Text, int]:
    """:func:`info_line` with ``label`` as given, and the cells up to the end of what a narrow
    row keeps whole: the label, the state, and the account with its use."""
    agent = status.agent
    session = status.session
    chip, chip_style = STATE_CHIP.get(status.state, ("·", "dim"))
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(f"{ROLE_ICON.get(agent.role, '🤖')} ")
    text.append_text(label)
    text.append(SEPARATOR)
    exited = status.state == "exited" and agent.exit_status is not None
    code = f"({agent.exit_status})" if exited else ""
    text.append(f"{chip} {status.state}{code}", style=chip_style)
    account = account_text(status, labels)
    if account:
        text.append(SEPARATOR)
        text.append(account, style=ACCOUNT_STYLE)
        slot = agent.account_slot
        percent = (usage or {}).get(slot) if slot is not None else None
        if percent is not None:
            text.append(" · ", style="dim")
            text.append_text(session_percent_text(percent))
    essential = text.cell_len
    if session is not None and session.model:
        text.append(SEPARATOR)
        text.append(session.model, style=MODEL_STYLE)
    if session is not None and session.effort:
        text.append(" " if session.model else SEPARATOR)
        text.append(session.effort, style=EFFORT_STYLE)
    # An exited row's detail is its exit code, which the chip has already said.
    if status.detail and not (exited and status.detail == f"exit {agent.exit_status}"):
        text.append(SEPARATOR)
        text.append(status.detail, style=DETAIL_STYLE)
    text.append(SEPARATOR)
    text.append(agent.pane_id, style=PANE_STYLE)
    return text, essential


class InfoLine(Static):
    """A header that paints :func:`info_line` fitted to its own width, and again on a resize.

    The Agent view's header and the Manager tab's both: one widget, so the two give way
    alike. :meth:`show_text` paints anything else (the Manager tab's no-manager prose), and
    a resize leaves that as it is.
    """

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._line: tuple[FleetAgentStatus, Mapping[int, str], Mapping[int, float]] | None = None
        self._painted: tuple[object, ...] | None = None

    def show_line(
        self, status: FleetAgentStatus, labels: Mapping[int, str], usage: Mapping[int, float]
    ) -> None:
        """Paint ``status``'s info line, and keep it fitted to the width from here on."""
        self._line = (status, labels, usage)
        self._paint()

    def show_text(self, text: Text) -> None:
        """Paint ``text`` as it is; a resize does not touch it."""
        self._line = self._painted = None
        self.update(text)

    def on_resize(self) -> None:
        self._paint(layout=False)

    def _paint(self, *, layout: bool = True) -> None:
        if self._line is None:
            return
        width = self.content_size.width or None
        wanted = (*self._line, width)
        if wanted != self._painted:
            self._painted = wanted
            status, labels, usage = self._line
            self.update(info_line(status, labels, usage, width=width), layout=layout)


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


RESTART_WORKER = "agent-restart"
#: States in which there is a process to stop; anything else is a row to restart. The
#: service's own set, which `fleet restart|stop --all` acts on.
_STOPPABLE: frozenset[str] = fleet_service.PROCESS_STATES


class AgentView(Vertical):
    """One agent: its info line, the two actions that change it, then the live session.

    **Stop** and **Restart** (#138) are the actions §4.2 promised and this view
    never had: a manager ended with ctrl+c inside its window showed 💤 forever
    with nothing to click, and ``fleet spawn manager`` refused until a hand-run
    ``reap``. Stop asks the shell for its dialog (:class:`StopAgent`); the dialog
    runs the stop and owns its answer, including a refusal. Restart runs the
    service off the UI thread; the shell's next frame is what repaints, never an
    optimistic guess. A restart resumes the agent's own
    session when its transcript is on disk, so the manager comes back knowing
    its intake and its coders; the shell then shows the new row if this view is
    still the one on screen (:class:`AgentRestarted`). Both buttons ignore a mouse
    press that lands just as the view came up or moved under the pointer
    (:class:`~aisquare.cli.ui.click_guard.GuardedButton`).
    """

    # ``text-wrap`` / ``text-overflow`` on the header, not on its ``Text``: Textual drops the
    # Text's own ``no_wrap`` and ellipsis (see ``sidebar.Activatable``), and a line wider
    # than the bar word-wrapped onto a second row its height clipped — the account and its
    # use vanished with no ``…``. Cut at the end, the info line gives way from its detail.
    DEFAULT_CSS = """
    AgentView #agent-bar { height: 1; }
    AgentView #agent-header {
        width: 1fr; height: 1; padding: 0 1; text-wrap: nowrap; text-overflow: ellipsis;
    }
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

    def _paint_info(self) -> None:
        if self.is_mounted:
            header = self.query_one("#agent-header", InfoLine)
            header.show_line(self.status, self.labels.labels, self.usage)

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
            header = InfoLine(id="agent-header")
            header.show_line(self.status, self.labels.labels, self.usage)
            yield header
            # Guarded (``click_guard``): a click that was aimed at what this view replaced
            # must not stop or restart the agent it shows instead.
            yield GuardedButton("Stop", id="agent-stop", compact=True)
            yield GuardedButton("Restart", id="agent-restart", compact=True, variant="primary")
        yield from self.compose_bars()
        yield TerminalPane(
            shown_pane(self.status),
            server=self.server,
            escape_key=self.escape_key,
            placeholder=PANE_GONE,
            label=self.status.agent.label,
            id="agent-pane",
        )

    def compose_bars(self) -> ComposeResult:
        """More bars between the header and the pane — the captain's (``views/captain.py``,
        T4); an ordinary agent has none."""
        yield from ()

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
        self.pane.label = status.agent.label
        if (wanted := shown_pane(status)) != self.pane.pane_id:
            self.pane.attach(wanted)

    def _paint_actions(self) -> None:
        """Stop where ``STOP_STATES`` says — a process, or a dead window; Restart always (an
        exited row is exactly its case).

        Greyed while THIS view's own restart runs, and only then (a stop runs inside its
        own modal dialog): ``self.workers``
        is the app's whole list, and a finished worker is still in it when its
        ``StateChanged`` arrives — nothing else repaints the buttons afterwards (the shell
        feeds a view only when its status changed), so a failed restart would otherwise
        stay greyed with no way to retry it.
        """
        busy = any(
            worker.node is self and worker.name == RESTART_WORKER and not worker.is_finished
            for worker in self.workers
        )
        stop = self.query_one("#agent-stop", Button)
        stop.display = self.status.state in STOP_STATES
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
            "Start it again under this label — same role, task, worktree, account and "
            "persona; its session is resumed from the transcript when that is on disk (as "
            "`fleet restart` does)"
        )

    @on(Button.Pressed, "#agent-stop")
    def _stop(self, event: Button.Pressed) -> None:
        event.stop()
        agent = self.status.agent
        # THIS row's id: the dialog pins the stop to it (``agent_id``), never to whoever
        # holds the label now — the view outlives its row.
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
