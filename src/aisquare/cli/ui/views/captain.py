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

- **the voice controls**, over T3's page (``services.captain.voice`` and
  ``speaker``), each writing the one key in state.json the page and the CLI write
  too, so the three always agree and a connected page follows within a second:
  **Mode** flips ``captain_voice_mode`` between ``focus`` (hold to talk) and
  ``listen`` (always listening), unset reading as focus (13178 Q1, 13179);
  **Speaker** flips ``captain_speaker``; **Mic** prints — never starts (13178
  Q2, Phase 2 starts it) — the page's URL and QR and, when nothing serves it, the
  command that does. The writes run off the UI thread (a held state lock waits
  seconds); the reads are lock-free and ride the thinking tick, so a change the
  page makes shows here too.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from typing import Any, ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
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
VOICE_WORKER = "captain-voice-switch"
MIC_WORKER = "captain-mic"

START_COMMAND = "aisquare captain voice"
"""What serves the page (T3's ``captain voice``; ``--mode``/``--speaker`` write the same keys)."""

PROBE_TIMEOUT_S = 0.3


def thinking(status: FleetAgentStatus, *, busy: bool) -> bool:
    """Whether the captain is thinking: its busy flag, or a pane that is working."""
    return busy or status.state == "working"


def thinking_text(on: bool) -> Text:
    if on:
        return Text("● thinking", style="bold yellow")
    return Text("○ idle", style="dim")


def page_serving(port: int) -> bool:
    """Whether something answers on the voice page's loopback port — a connect, nothing sent.

    T3 records no running server; the port is the fact. Loopback only: the page
    binds nothing else (T3's ``LOOPBACK_HOSTS``).
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=PROBE_TIMEOUT_S):
            return True
    except OSError:
        return False


@dataclass(frozen=True)
class MicInfo:
    """What the Mic prints: where the page is, its QR, whether it serves, how to start it."""

    url: str
    qr: list[str] | None
    serving: bool
    adb_reverse: str
    mode: str
    speaker: bool


def mic_info() -> MicInfo:
    """Read everything the Mic prints — off the UI thread (a connect, the token file)."""
    from aisquare.services.captain import speaker, voice
    from aisquare.services.mcp_server import serve_token

    port = voice.DEFAULT_PORT
    url = voice.voice_url(port, serve_token())
    return MicInfo(
        url=url,
        qr=voice.qr_lines(url),
        serving=page_serving(port),
        adb_reverse=voice.adb_reverse(port),
        mode=voice.voice_mode() or "focus",
        speaker=speaker.speaker_on(),
    )


class MicScreen(ModalScreen[None]):
    """The captain's voice page, printed: its URL and QR, and the command when none serves it."""

    DEFAULT_CSS = """
    MicScreen { align: center middle; }
    MicScreen #mic-box { width: auto; max-width: 96%; height: auto; max-height: 96%;
                         border: heavy $accent; background: $surface; padding: 0 2; }
    MicScreen .mic-line { height: auto; width: auto; }
    MicScreen #mic-qr { height: auto; width: auto; padding: 1 0; }
    MicScreen #mic-buttons { height: auto; align-horizontal: right; padding: 1 0; }
    """
    BINDINGS: ClassVar = [Binding("escape", "close", "close")]

    def __init__(self, info: MicInfo) -> None:
        super().__init__()
        self.info = info

    def compose(self) -> ComposeResult:
        info = self.info
        with Vertical(id="mic-box"):
            yield Static(Text("Captain voice page", style="bold"), classes="mic-line")
            yield Static(Text(info.url), id="mic-url", classes="mic-line")
            if info.qr:
                yield Static(Text("\n".join(info.qr)), id="mic-qr")
            else:
                yield Static(
                    Text("(no QR: segno is not installed — pip install 'aisquare-cli[voice]')",
                         style="dim"),
                    id="mic-qr",
                )  # fmt: skip
            if info.serving:
                yield Static(
                    Text("● serving now — open the URL", style="green"), classes="mic-line"
                )
            else:
                yield Static(
                    Text(f"○ not running — start it: {START_COMMAND}", style="yellow"),
                    classes="mic-line",
                )
            yield Static(
                Text(
                    f"mode: {info.mode} · speaker: {'on' if info.speaker else 'off'}", style="dim"
                ),
                classes="mic-line",
            )
            yield Static(
                Text(f"Android over USB: {info.adb_reverse}, then open the same URL there",
                     style="dim"),
                classes="mic-line",
            )  # fmt: skip
            with Horizontal(id="mic-buttons"):
                yield Button("Close", id="mic-close", variant="primary")

    @on(Button.Pressed, "#mic-close")
    def _close(self) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


def _voice_state() -> tuple[str, bool]:
    """The mode (unset reads as focus) and the speaker switch; lock-free reads of state.json."""
    from aisquare.services.captain import speaker, voice

    try:
        return voice.voice_mode() or "focus", speaker.speaker_on()
    except Exception:  # a moment's unreadable file: the defaults, and the next tick reads again
        return "focus", True


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
            yield Button(
                "Mic",
                id="captain-mic",
                compact=True,
                tooltip="The voice page: its URL and QR, and how to start it",
            )
            yield Button(
                "Mode: focus",
                id="captain-mode",
                compact=True,
                tooltip="focus: hold to talk · listen: always listening (the page follows)",
            )
            yield Button(
                "Speaker: on",
                id="captain-speaker",
                compact=True,
                tooltip="Whether the captain's replies are spoken",
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
        """Repaint the bar from state.json and the pane state (the interval's callback): the
        indicator, and the mode and speaker the page may have switched."""
        on = thinking(self.status, busy=_busy())
        self.query_one("#captain-thinking", Static).update(thinking_text(on))
        mode, speaking = _voice_state()
        self.query_one("#captain-mode", Button).label = f"Mode: {mode}"
        self.query_one("#captain-speaker", Button).label = f"Speaker: {'on' if speaking else 'off'}"

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

    @on(Button.Pressed, "#captain-mode")
    def _flip_mode(self, event: Button.Pressed) -> None:
        event.stop()
        mode, _ = _voice_state()
        wanted = "listen" if mode == "focus" else "focus"

        def write() -> None:
            from aisquare.services.captain import voice

            voice.set_voice_mode("listen" if wanted == "listen" else "focus")

        self._switch(write)

    @on(Button.Pressed, "#captain-speaker")
    def _flip_speaker(self, event: Button.Pressed) -> None:
        event.stop()
        _, speaking = _voice_state()

        def write() -> None:
            from aisquare.services.captain import speaker

            speaker.set_speaker(not speaking)

        self._switch(write)

    def _switch(self, write: Any) -> None:
        self.run_worker(
            write,
            name=VOICE_WORKER,
            group=VOICE_WORKER,
            thread=True,
            exit_on_error=False,
        )

    @on(Button.Pressed, "#captain-mic")
    def _mic(self, event: Button.Pressed) -> None:
        event.stop()
        self.run_worker(
            mic_info,
            name=MIC_WORKER,
            group=MIC_WORKER,
            exclusive=True,
            thread=True,
            exit_on_error=False,
        )

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name in (VOICE_WORKER, MIC_WORKER):
            self._voice_answered(event)
            return
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

    def _voice_answered(self, event: Worker.StateChanged) -> None:
        if event.state is WorkerState.ERROR:
            what = "the voice page" if event.worker.name == MIC_WORKER else "the switch"
            self.notify(
                f"could not read {what}: {event.worker.error}"
                if event.worker.name == MIC_WORKER
                else f"could not change {what}: {event.worker.error}",
                severity="error",
                timeout=8,
                markup=False,
            )
            return
        if event.state is not WorkerState.SUCCESS:
            return
        if event.worker.name == MIC_WORKER and isinstance(event.worker.result, MicInfo):
            self.app.push_screen(MicScreen(event.worker.result))
        else:
            self.paint_thinking()
