"""The Stop dialog — the agent view's Stop button and the sidebar's ``x``, over
``services.fleet.stop``.

docs/plans/fleet-tui.md §4.2 promised the agent view its "actions"; this is the
first of them. The dialog is one question in front of the one command the CLI
already runs (``aisquare fleet stop <label>``, ``--force``), and it OWNS that
call: a refusal is an answer to show, not a crash, so it lands here on the
status line with the dialog still open and the agent untouched — the rule the
Spawn dialog follows for its own service (``spawn.py``, "a FleetError is an
answer to show").

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
"""

from __future__ import annotations

import inspect
from typing import ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static
from textual.worker import Worker, WorkerState

from aisquare.models import FleetAgent, FleetAgentStatus, ProjectInfo
from aisquare.services import fleet as fleet_service

STOP_WORKER = "agent-stop"

_BOX_CSS = """
StopAgentScreen { align: center middle; }
StopAgentScreen > Vertical { width: 72; max-width: 92%; height: auto; border: heavy $accent;
                             background: $surface; padding: 0 1; }
StopAgentScreen .picker-header { height: auto; padding: 1 0; }
StopAgentScreen .picker-note { height: auto; }
StopAgentScreen .picker-buttons { height: auto; align-horizontal: right; padding-bottom: 1; }
StopAgentScreen .picker-buttons Button { margin-left: 2; }
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


class StopAgentScreen(ModalScreen[FleetAgent | None]):
    """One question before an agent is stopped; dismisses with the ended row, or ``None``."""

    DEFAULT_CSS = _BOX_CSS
    BINDINGS: ClassVar = [Binding("escape", "cancel", "cancel")]

    def __init__(self, project: ProjectInfo, status: FleetAgentStatus) -> None:
        super().__init__()
        self.project = project
        self.status = status
        self._stopping = False

    @property
    def label(self) -> str:
        return self.status.agent.label

    def is_manager(self) -> bool:
        """The project's manager: ``spawn`` reserves this label for it (``MANAGER_LABEL``)."""
        return self.label == fleet_service.MANAGER_LABEL

    def question(self) -> Text:
        """The whole question as data — the label is the user's, brackets and all."""
        text = Text()
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
            with Horizontal(classes="picker-buttons"):
                yield Button("Stop", id="stop-confirm", variant="primary")
                yield Button("Force", id="stop-force", variant="warning")
                yield Button("Cancel", id="stop-cancel")

    @on(Button.Pressed, "#stop-confirm")
    def _stop(self) -> None:
        self._run(force=False)

    @on(Button.Pressed, "#stop-force")
    def _force(self) -> None:
        self._run(force=True)

    def _run(self, *, force: bool) -> None:
        """One stop at a time, off the UI thread: tmux, the grace wait and the kill all block."""
        if self._stopping:
            return
        self._set_stopping(True)
        label = self.label
        self.run_worker(
            lambda: fleet_service.stop(self.project, label, force=force),
            name=STOP_WORKER,
            group=STOP_WORKER,
            thread=True,
            exit_on_error=False,  # a FleetError is an answer to show, not a crash
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
        self._note(f"stopping {self.label} …" if running else None, style="dim")

    def _note(self, text: str | None, *, style: str = "bold red") -> None:
        note = self.query_one("#stop-status", Static)
        note.update(Text(text, style=style) if text else "")
        note.display = bool(text)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name != STOP_WORKER:
            return
        if event.state is WorkerState.SUCCESS:
            agent = event.worker.result
            if isinstance(agent, FleetAgent):
                self.dismiss(agent)
                return
            self._refused(f"the stop answered without an agent ({type(agent).__name__})")
        elif event.state is WorkerState.ERROR:
            error = event.worker.error
            self._refused(
                refusal(error) if error is not None else "the stop failed, with no reason"
            )
        elif event.state is WorkerState.CANCELLED:
            self._set_stopping(False)

    def _refused(self, reason: str) -> None:
        """The service said no (or broke): say why, stay open, leave the agent alone."""
        self._set_stopping(False)
        self._note(reason)
