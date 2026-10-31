"""The Stop dialog — the agent view's Stop button and the sidebar's ``x``, over
``services.fleet.stop``.

docs/plans/fleet-tui.md §4.2 promised the agent view its "actions"; this is the
first of them. The dialog is one question in front of the one command the CLI
already runs (``aisquare fleet stop <label>``, ``--force``), and it OWNS that
call: a refusal is an answer to show, not a crash, so it lands here on the
status line with the dialog still open and the agent untouched — the rule the
Spawn dialog follows for its own service (``spawn.py``, "a FleetError is an
answer to show").

On a 💤 exited row — offered since the release train's `remain-on-exit` keeps
a dead window for its last screen (#138) — the same command removes the window
and the row leaves the listing; the question says so and offers no *Force*.

That matters more here than it does for a spawn. ``stop`` refuses on purpose
when tmux cannot CONFIRM the pane died: the row is left live and the operator is
told, rather than being shown "✓ stopped" over an agent that is still running
(``services/fleet.py``, ``_verify_gone``). A dialog that closed on that refusal
would turn the service's honesty back into the lie it was written to prevent.

Everything the service says reaches the screen as a :class:`rich.text.Text`
(``tests/test_console_markup.py``'s rule): a refusal quotes a label and a socket
path, and a ``[`` in either is data, never markup.

Nothing here writes to the board. Ending an agent's row is the service's
business and what it records is the service's to record; a board write from the
UI would be a second account of one fact.

**Don't ask again** (``[ui] confirm_stop``). The tick, unticked as the dialog opens,
is saved when Stop or Force is pressed with it ticked
(``settings_service.stop_without_asking``) — at the press, whatever the stop then
answers: it is an answer to the question, not to the stop. Cancel and Escape save nothing. From then
on the shell stops at once, with no dialog (``FleetApp.on_stop_agent``), through
:func:`start_stop` and :func:`stop_answer` — the call and the reading of its answer
this dialog makes, so the two paths cannot drift. A file that cannot be read is not
written over: the stop goes on, and a toast says the dialog will ask again.
"""

from __future__ import annotations

import inspect
from typing import Any, ClassVar

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.dom import DOMNode
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Static
from textual.worker import Worker, WorkerState

from aisquare.cli.ui.sidebar import ALIVE_STATES
from aisquare.cli.ui.views.app_settings import UiSettingsChanged
from aisquare.core.config import UiSettings
from aisquare.models import FleetAgent, FleetAgentStatus, ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services import settings as settings_service

STOP_WORKER = "agent-stop"
DONT_ASK = "stop-dont-ask"
"""The dialog's *Don't ask again* tick: ``[ui] confirm_stop``."""
ASK_AGAIN_WHERE = "⚙ Settings ▸ App settings ▸ User preferences ▸ Advanced"
"""Where *Ask before stopping an agent* is: the toast that says the dialog will not ask
again names it, since nothing else on screen will once the dialog has gone."""

_BOX_CSS = """
StopAgentScreen { align: center middle; }
StopAgentScreen > Vertical { width: 72; max-width: 92%; height: auto; border: heavy $accent;
                             background: $surface; padding: 0 1; }
StopAgentScreen .picker-header { height: auto; padding: 1 0; }
StopAgentScreen .picker-note { height: auto; }
StopAgentScreen .picker-buttons { height: auto; align-horizontal: right; padding-bottom: 1; }
StopAgentScreen .picker-buttons Button { margin-left: 2; }
StopAgentScreen #stop-dont-ask { border: none; height: 1; padding: 0 1; margin-bottom: 1; }
StopAgentScreen #stop-dont-ask:focus { text-style: bold reverse; }
"""


def grace_seconds() -> float:
    """How long ``stop`` waits after ``/exit`` — asked of the service, never copied.

    The dialog promises the user a number of seconds. Typing that number here
    would make the sentence true only until someone changed the service's
    default, and the screen is the last place that would notice.

    Rename ``grace`` on the service — or stand ``stop`` in with a double that
    does not carry that parameter — and this raises ``KeyError: grace`` while the
    dialog is composed, loudly and on purpose. A typed fallback would swallow the
    rename and go back to promising a number nobody maintains, which is the drift
    this function exists to prevent.

    The double is the likelier meeting. Every recorder in
    ``tests/test_ui_shell.py`` carries the full signature (``project, label, *,
    force=False, grace=5.0``), so this suite never hits it — but a one-off fake
    written for its return value alone will, and the dialog then fails to OPEN
    rather than failing to stop. That is how it was met during P21's live
    verification, and the fake was the thing that was wrong. Every test that
    opens the dialog composes it, so a rename turns them red on the spot rather
    than reaching an operator.
    """
    return float(inspect.signature(fleet_service.stop).parameters["grace"].default)


def refusal(exc: BaseException) -> str:
    """What a refused stop says: a fleet rule as written, anything else with its class.

    A ``FleetError`` is already a sentence written for the operator. A
    ``KeyError`` is not — ``str()`` on one is a bare quoted id — so anything else
    is named by its class as well as its text.
    """
    if isinstance(exc, fleet_service.FleetError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def start_stop(
    node: DOMNode, project: ProjectInfo, agent: FleetAgent, *, force: bool
) -> Worker[Any]:
    """Stop ``agent`` on a thread worker of ``node``: tmux, the grace wait and the kill
    all block. The dialog's call, and the shell's when it does not ask.

    Pinned to THIS row (``agent_id``), never to whoever holds the label now: the view
    outlives its row, and a 💤 view's Stop by label stopped the replacement (review of
    #138).
    """
    label, agent_id = agent.label, agent.id
    return node.run_worker(
        lambda: fleet_service.stop(project, label, force=force, agent_id=agent_id, actor="user"),
        name=STOP_WORKER,
        group=STOP_WORKER,
        thread=True,
        exit_on_error=False,  # a FleetError is an answer to show, not a crash
    )


def stop_answer(worker: Worker[Any], state: WorkerState) -> fleet_service.StopReceipt | str:
    """What a stop worker that finished (``SUCCESS`` or ``ERROR``) answered: the service's
    receipt, or why there is none, in the service's words (:func:`refusal`)."""
    if state is WorkerState.SUCCESS:
        receipt = worker.result
        if isinstance(receipt, fleet_service.StopReceipt):
            return receipt
        return f"the stop answered without a receipt ({type(receipt).__name__})"
    error = worker.error
    return refusal(error) if error is not None else "the stop failed, with no reason"


def warn_release_failed(app: App[Any], receipt: fleet_service.StopReceipt) -> None:
    """``fleet stop`` prints this and exits 1: a claim left with the ended session is not a
    clean stop, and the screen must not read as one."""
    if receipt.release_failed:
        app.notify(f"claims: {receipt.release_failed}", severity="warning", timeout=8, markup=False)


class StopAgentScreen(ModalScreen[FleetAgent | None]):
    """One question before an agent is stopped; dismisses with the ended row, or ``None``."""

    DEFAULT_CSS = _BOX_CSS
    BINDINGS: ClassVar = [Binding("escape", "cancel", "cancel")]

    def __init__(self, project: ProjectInfo, status: FleetAgentStatus) -> None:
        super().__init__()
        self.project = project
        self.status = status
        self._stopping = False
        self.saved_ui: UiSettings | None = None
        """``[ui]`` as *Don't ask again* saved it, once it has; ``None`` until then."""

    @property
    def label(self) -> str:
        return self.status.agent.label

    def is_manager(self) -> bool:
        """The project's manager: ``spawn`` reserves this label for it (``MANAGER_LABEL``)."""
        return self.label == fleet_service.MANAGER_LABEL

    def has_process(self) -> bool:
        """Whether there is a process to ``/exit`` — ``ALIVE_STATES``, the button's own rule.

        On a 💤 exited row the window is dead and kept only for its last screen
        (`remain-on-exit`, #138): the stop removes it and the row leaves the
        listing, with no ``/exit`` and no grace, so the question says that and
        *Force* — which only skips the ``/exit`` — has nothing to offer.
        """
        return self.status.state in ALIVE_STATES

    def question(self) -> Text:
        """The whole question as data — the label is the user's, brackets and all."""
        text = Text()
        if self.status.state == "paused":
            text.append("Stop ")
            text.append(self.label, style="bold")
            text.append(" for good?\n")
            text.append(
                "It is paused, with no process. Stop ends the pause: the claims it kept go "
                "back to the board, and a resume leaves it.",
                style="dim",
            )
            return text
        if not self.has_process():
            text.append("Remove ")
            text.append(self.label, style="bold")
            text.append("'s dead window?\n")
            text.append(
                "The agent has exited; tmux kept its window for the last screen. "
                "Stop removes it and the row leaves the listing.",
                style="dim",
            )
            return text
        text.append("Stop ")
        text.append(self.label, style="bold")
        text.append("?\n")
        text.append(f"/exit, then the window is killed after {grace_seconds():g} s. ", style="dim")
        text.append("Force skips the /exit.", style="dim")
        if self.is_manager():
            text.append(
                "\nThis is the project's manager; the agents it started keep running.",
                style="yellow",
            )
        return text

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.question(), classes="picker-header", id="stop-question")
            yield Static(id="stop-status", classes="picker-note")
            yield Checkbox("Don't ask again", False, id=DONT_ASK)
            with Horizontal(classes="picker-buttons"):
                yield Button("Stop", id="stop-confirm", variant="primary")
                force = Button("Force", id="stop-force", variant="warning")
                force.display = self.has_process()
                yield force
                yield Button("Cancel", id="stop-cancel")

    # Names no other function in the tree has: tests/test_config_writes_stay_in_the_cli.py
    # follows calls by NAME to ``save_config`` (*Don't ask again* reaches it), and a
    # ``_run`` here put every caller of any ``_run`` — hooks and the MCP server among them —
    # in reach of a config write.
    @on(Button.Pressed, "#stop-confirm")
    def _stop_pressed(self) -> None:
        self._stop_confirmed(force=False)

    @on(Button.Pressed, "#stop-force")
    def _force_pressed(self) -> None:
        self._stop_confirmed(force=True)

    def _stop_confirmed(self, *, force: bool) -> None:
        """One stop at a time, off the UI thread: tmux, the grace wait and the kill all block."""
        if self._stopping:
            return
        self._set_stopping(True)
        self._save_dont_ask()
        start_stop(self, self.project, self.status.agent, force=force)

    def _save_dont_ask(self) -> None:
        """Ticked at a Stop or a Force: ``[ui] confirm_stop = false``, once, and the shell told
        (``UiSettingsChanged``), which stops at once from then on and shows the Settings
        switch off. An unreadable file is left as it is: the stop goes on regardless."""
        if self.saved_ui is not None or not self.query_one(f"#{DONT_ASK}", Checkbox).value:
            return
        try:
            self.saved_ui = settings_service.stop_without_asking()
        except Exception as exc:
            self.app.notify(
                f"Don't ask again was not saved, so Stop will ask again: {refusal(exc)}",
                severity="warning",
                timeout=8,
                markup=False,
            )
            return
        self.app.post_message(UiSettingsChanged(self.saved_ui))
        self.app.notify(
            f"Stop will not ask again. {ASK_AGAIN_WHERE} turns it back on.",
            timeout=10,
            markup=False,
        )

    @on(Button.Pressed, "#stop-cancel")
    def action_cancel(self) -> None:
        if self._stopping:
            # The /exit is already typed and the kill is on its way: closing now
            # would leave the service's answer — including a refusal — nowhere to
            # land. The Spawn dialog waits out its own spawn for the same reason.
            return
        self.dismiss(None)

    def _set_stopping(self, running: bool) -> None:
        self._stopping = running
        for button in self.query(Button):
            button.disabled = running
        self.query_one(f"#{DONT_ASK}", Checkbox).disabled = running or self.saved_ui is not None
        self._note(f"stopping {self.label} …" if running else None, style="dim")

    def _note(self, text: str | None, *, style: str = "bold red") -> None:
        note = self.query_one("#stop-status", Static)
        note.update(Text(text, style=style) if text else "")
        note.display = bool(text)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name != STOP_WORKER:
            return
        if event.state in (WorkerState.SUCCESS, WorkerState.ERROR):
            answer = stop_answer(event.worker, event.state)
            if isinstance(answer, fleet_service.StopReceipt):
                warn_release_failed(self.app, answer)
                self.dismiss(answer.agent)
                return
            self._refused(answer)
        elif event.state is WorkerState.CANCELLED:
            self._set_stopping(False)

    def _refused(self, reason: str) -> None:
        """The service said no (or broke): say why, stay open, leave the agent alone."""
        self._set_stopping(False)
        self._note(reason)
