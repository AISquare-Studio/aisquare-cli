"""The Captain view: the captain's agent view, with a bar of its own (T4).

The captain is a fleet agent (T2), so its view IS the agent view — the header,
Stop and Restart, the live ``TerminalPane`` over its pane — with one more bar
under the header:

- **the thinking indicator** — ``thinking`` while T1's busy flag is set (the
  captain's ``thinking on`` tool, written to state.json by ANOTHER process) or
  while its pane reads ``working``; ``idle`` otherwise. The flag can change while
  the row's state does not, and the shell feeds a view only on a changed status,
  so the view reads the flag on its own interval (:data:`THINKING_TICK_S`).
- **What's up** — the quick action: it types :data:`WHAT_IS_UP` into the pane
  through ``fleet.tell``, the one delivery the fleet has. Offered only while the
  captain waits at its prompt: ``tell`` files anything else as a board note, and
  the captain has no shell to read one (T2's delivery rule) — so the button is
  greyed while it works, never a note it would not see.

The voice controls — mic, mode, speaker — join this bar with T3's page
(``services.captain.voice``, ``services.captain.speaker``).
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.timer import Timer
from textual.widgets import Button, Static
from textual.worker import Worker, WorkerState

from aisquare.cli.ui.views.agent import AgentView
from aisquare.models import FleetAgentStatus
from aisquare.services import fleet as fleet_service

WHAT_IS_UP = "what is up"
"""The quick action's words: the captain's persona answers them from the attention queue."""

THINKING_TICK_S = 1.0
"""How often the view reads the busy flag — T3's page polls state at the same pace."""

TELL_WORKER = "captain-whats-up"


def thinking(status: FleetAgentStatus, *, busy: bool) -> bool:
    """Whether the captain is thinking: its busy flag, or a pane that is working."""
    return busy or status.state == "working"


def thinking_text(on: bool) -> Text:
    if on:
        return Text("● thinking", style="bold yellow")
    return Text("○ idle", style="dim")


def _busy() -> bool:
    """T1's busy flag. A flag that cannot be read is not thinking — the pane state still is."""
    from aisquare.services.captain import state as captain_state

    try:
        return captain_state.busy_since() is not None
    except Exception:  # state.json unreadable for a moment: the next tick reads it again
        return False


class CaptainView(AgentView):
    """The captain's agent view: the same header, actions and pane, plus the captain bar."""

    DEFAULT_CSS = """
    CaptainView #captain-bar { height: 1; }
    CaptainView #captain-thinking { width: 14; height: 1; padding: 0 1; }
    CaptainView #captain-bar Button { min-width: 11; margin: 0 1 0 0; }
    """

    def __init__(self, status: FleetAgentStatus, **options: Any) -> None:
        super().__init__(status, **options)
        self.thinking_timer: Timer | None = None

    def compose_bars(self) -> ComposeResult:
        with Horizontal(id="captain-bar"):
            yield Static(thinking_text(False), id="captain-thinking")
            yield Button(
                "What's up",
                id="captain-whats-up",
                compact=True,
                tooltip=f"Type “{WHAT_IS_UP}” into the captain's pane — while it waits for you",
            )

    def on_mount(self) -> None:
        super().on_mount()
        self.paint_thinking()
        self._paint_quick_action()
        self.thinking_timer = self.set_interval(THINKING_TICK_S, self.paint_thinking)

    def refresh_status(self, status: FleetAgentStatus) -> None:
        super().refresh_status(status)
        if self.is_mounted:
            self.paint_thinking()
            self._paint_quick_action()

    def paint_thinking(self) -> None:
        """Repaint the indicator from the flag and the pane state (the interval's callback)."""
        on = thinking(self.status, busy=_busy())
        self.query_one("#captain-thinking", Static).update(thinking_text(on))

    def _paint_quick_action(self) -> None:
        busy = any(
            worker.node is self and worker.name == TELL_WORKER and not worker.is_finished
            for worker in self.workers
        )
        button = self.query_one("#captain-whats-up", Button)
        button.disabled = busy or self.status.state != "waiting"

    @on(Button.Pressed, "#captain-whats-up")
    def _whats_up(self, event: Button.Pressed) -> None:
        event.stop()
        agent = self.status.agent
        self.run_worker(
            lambda: fleet_service.tell(fleet_service.project_of(agent), agent.label, WHAT_IS_UP),
            name=TELL_WORKER,
            group=TELL_WORKER,
            exclusive=True,
            thread=True,
            exit_on_error=False,
        )
        self._paint_quick_action()

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name != TELL_WORKER:
            super().on_worker_state_changed(event)
            return
        if event.state not in (WorkerState.SUCCESS, WorkerState.ERROR, WorkerState.CANCELLED):
            return
        if event.state is WorkerState.ERROR:
            self.notify(
                f"could not ask the captain: {event.worker.error}",
                severity="error",
                timeout=8,
                markup=False,
            )
        else:
            result = event.worker.result
            if isinstance(result, fleet_service.TellResult) and not result.delivered:
                self.notify(
                    f"the captain did not get it — {result.how}",
                    severity="warning",
                    timeout=8,
                    markup=False,
                )
            elif isinstance(result, fleet_service.TellResult):
                self.notify(f"asked the captain: {WHAT_IS_UP}", timeout=4, markup=False)
        self._paint_quick_action()
