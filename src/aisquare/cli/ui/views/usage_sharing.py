"""Opt-in anonymous usage sharing in the TUI: the switch on the Usage & stats page, the one-time
question, and the dialog that shows exactly what would be sent.

**The switch** (:class:`SharingBox`) fills the slot at the top of the Usage & stats page. It is
one button, *Turn sharing on* / *Turn sharing off*, under a line saying what is and is never
shared, and the state beneath: off ("Nothing leaves this machine"), on (when it was last sent,
and whether the server linked it to the signed-in AISquare account), or forced off by the
environment (``DO_NOT_TRACK``, ``AISQUARE_NO_TELEMETRY``, ``CI``) with the button greyed and the
reason named. A :class:`~aisquare.cli.ui.click_guard.GuardedButton`, not a ``Switch``: a click
that lands on it as the page appears must not be how someone agrees to share. Turning it on
sends the first shipment at once; turning it off purges what was kept and sends the one opt-out
notice. Both run off the UI thread (:data:`SHARE_WORKER`).

**The question** (:class:`ConsentDialog`) is asked once, by the shell, after onboarding and only
where nothing forces sharing off: *Share anonymous usage* / *No thanks* / *See what would be
sent*. *No thanks* has the focus; Escape is *No thanks*. Either answer is recorded, and it is not
asked again; so is an explicit *Turn sharing off* elsewhere.

**What is sent** (:class:`PreviewDialog`) is the body the next shipment would post, built by the
same function that builds it (the transcripts read first, as a shipment reads them), shown as the
JSON it is. While sharing is off it is captioned as what it is, a SAMPLE of the last 31 days with
placeholder ids: nothing before the day sharing is turned on is ever sent. Looking sends nothing.

``sharing`` (:class:`Sharing`) is how the widgets reach the service; tests hand them a fake.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static
from textual.worker import Worker, WorkerState

from aisquare.cli.ui.click_guard import GuardedButton
from aisquare.models import SharingStatus, ShipReport
from aisquare.services import usage_share

SHARE_WORKER = "usage-share"
"""The worker that reads the sharing's state and turns it on or off: it writes the config and
the sharing's state, so an update waits for it rather than cutting it off."""
SHIP_WORKER = "usage-share-ship"
"""The worker that sends a shipment (the shell's, on a tick): nothing is lost by cutting it off,
a day not acknowledged being sent again."""
PREVIEW_WORKER = "usage-share-preview"
"""The worker that builds the preview: it reads, and nothing is lost by cutting it off."""

ABOUT = (
    "Counts only: tokens, sessions, which commands and pages you use, and a salted hash per "
    "repository. Never names, paths, remotes, prompts or arguments. Off by default."
)
ABOUT_PREVIEW = (
    "Exactly what the next shipment would send, built by the code that builds it. Looking sends "
    "nothing. Repositories are salted hashes."
)
ABOUT_SAMPLE = (
    "Sharing is off, so this is a SAMPLE: your last 31 days, with placeholder ids, to show what "
    "would be sent. Nothing is sent while sharing is off, and once it is on only days from then "
    "on are sent. Repositories are salted hashes."
)
CONSENT_TEXT = (
    "Share anonymous usage with AISquare?\n\n"
    "It tells us how aisquare is used, so we can make it better: daily counts of tokens, "
    "sessions, turns and agents, which commands and pages get used, and a salted hash and a few "
    "coarse traits per repository (its host, main language, size).\n\n"
    "We never collect repository names, paths or remotes, prompts, file names, emails, account "
    "aliases or command arguments. If you are signed in to AISquare, it is linked to your "
    "account; if not, it is anonymous. Only days from the moment you say yes are sent, nothing "
    "from before. Turn it off at any time: Settings ▸ Usage & stats, or "
    "`aisquare metrics share off`."
)


def _answer_the_sharing_question(yes: bool) -> ShipReport | None:
    """Record the answer to the question; on a yes, turn sharing on and send the first shipment."""
    if not yes:
        usage_share.record_consent("declined")
        return None
    usage_share.record_consent("share")
    return usage_share.ship(force=True)


@dataclass
class Sharing:
    """What the widgets and the shell ask of ``services.usage_share``: a seam for tests."""

    status: Callable[[], SharingStatus] = usage_share.status
    enable: Callable[[], SharingStatus] = usage_share.enable
    disable: Callable[[], ShipReport] = usage_share.disable
    ship: Callable[[], ShipReport] = field(default=lambda: usage_share.ship(force=True))
    """Send now (the switch just turned on)."""
    ship_due: Callable[[], ShipReport] = usage_share.ship
    """Send if one is due (the shell's tick)."""
    preview: Callable[[], str] = usage_share.preview
    is_sample: Callable[[], bool] = usage_share.is_sample
    """Whether the preview is a SAMPLE (sharing never on) and not the next POST."""
    consent_due: Callable[[], bool] = usage_share.consent_due
    answer: Callable[[bool], ShipReport | None] = _answer_the_sharing_question


def status_text(status: SharingStatus) -> str:
    """The line under the switch."""
    if status.forced_off:
        return f"Off — forced: {status.forced_off}. Nothing leaves this machine."
    if not status.enabled:
        return "Off — nothing leaves this machine."
    head = "On"
    if status.last_sent_at is None:
        head += " — nothing sent yet"
    else:
        sent = status.last_sent_at.astimezone().strftime("%Y-%m-%d %H:%M")
        head += f" — last sent {sent}"
    if status.signed_in:
        link = (
            "linked to your AISquare account"
            if status.linked
            else "signed in; the next shipment is linked to your AISquare account"
        )
    else:
        link = "anonymous (sign in to AISquare to link it to your account)"
    return f"{head} · {link}"


class SharingBox(Vertical):
    """The switch, its line of explanation, its state, and *What's shared*."""

    DEFAULT_CSS = """
    SharingBox { height: auto; border: round $primary-darken-2; padding: 0 1; margin-bottom: 1; }
    SharingBox #usage-share-title { height: 1; text-style: bold; }
    SharingBox #usage-share-about { height: auto; color: $text-muted; }
    SharingBox #usage-share-row { height: auto; margin-top: 1; }
    SharingBox #usage-share-row Button { margin-right: 2; }
    SharingBox #usage-share-status { height: auto; margin-top: 1; }
    """

    def __init__(self, *, sharing: Sharing | None = None, id: str | None = None) -> None:
        super().__init__(id=id)
        self._sharing = sharing or Sharing()
        self.status: SharingStatus | None = None
        """The state last read: what the button and the line show."""

    def compose(self) -> ComposeResult:
        yield Static(Text("Share anonymous usage"), id="usage-share-title")
        yield Static(Text(ABOUT), id="usage-share-about")
        with Horizontal(id="usage-share-row"):
            yield GuardedButton(
                "Turn sharing on", id="usage-share-toggle", compact=True, disabled=True
            )
            yield Button("What's shared", id="usage-share-preview", compact=True)
        yield Static(Text("reading…", style="dim"), id="usage-share-status")

    def on_mount(self) -> None:
        self.refresh_status()

    # --- reading and flipping ------------------------------------------------------------

    @property
    def busy(self) -> bool:
        return any(w.name == SHARE_WORKER and not w.is_finished for w in self.workers)

    def refresh_status(self) -> None:
        """Read where sharing stands, off the UI thread (a no-op while a switch is flipping)."""
        if not self.busy:
            self._flip_sharing("status")

    def _flip_sharing(self, action: str) -> None:
        sharing = self._sharing

        def flip_sharing_job() -> tuple[str, SharingStatus, ShipReport | None]:
            if action == "enable":
                sharing.enable()
                report: ShipReport | None = sharing.ship()
            elif action == "disable":
                report = sharing.disable()
            else:
                report = None
            return action, sharing.status(), report

        self.query_one("#usage-share-toggle", Button).disabled = True
        self.run_worker(
            flip_sharing_job,
            name=SHARE_WORKER,
            group=SHARE_WORKER,
            thread=True,
            exit_on_error=False,
        )

    @on(Worker.StateChanged)
    def _share_worker_done(self, event: Worker.StateChanged) -> None:
        if event.worker.name != SHARE_WORKER:
            return
        status_line = self.query_one("#usage-share-status", Static)
        if event.state is WorkerState.ERROR:
            status_line.update(Text(f"Could not change sharing: {event.worker.error}", style="red"))
            self.query_one("#usage-share-toggle", Button).disabled = self.status is None
            return
        if event.state is not WorkerState.SUCCESS:
            return
        result = event.worker.result
        if result is None:
            return
        _action, status, _report = result
        self.show(status)  # whatever the shipment did: sharing says nothing of failing

    def show(self, status: SharingStatus) -> None:
        """Draw ``status``: the button's label and whether it can be pressed, and the line."""
        self.status = status
        toggle = self.query_one("#usage-share-toggle", Button)
        toggle.label = "Turn sharing off" if status.enabled else "Turn sharing on"
        toggle.variant = "default" if status.enabled else "primary"
        toggle.disabled = status.forced_off is not None
        toggle.refresh(layout=True)
        self.query_one("#usage-share-status", Static).update(Text(status_text(status)))

    @on(Button.Pressed, "#usage-share-toggle")
    def _toggle(self, event: Button.Pressed) -> None:
        event.stop()
        if self.status is None or self.busy:
            return
        self._flip_sharing("disable" if self.status.enabled else "enable")

    @on(Button.Pressed, "#usage-share-preview")
    def _preview(self, event: Button.Pressed) -> None:
        event.stop()
        self.app.push_screen(PreviewDialog(self._sharing))


class PreviewDialog(ModalScreen[None]):
    """*What's shared*: the body the next shipment would post, as the JSON it is."""

    CSS = """
    PreviewDialog { align: center middle; }
    PreviewDialog #preview-box {
        width: 90%; height: 90%; border: heavy $primary; background: $surface; padding: 1 2;
    }
    PreviewDialog #preview-title { height: 1; text-style: bold; }
    PreviewDialog #preview-about { height: auto; color: $text-muted; margin-bottom: 1; }
    PreviewDialog #preview-scroll { height: 1fr; }
    PreviewDialog #preview-buttons { height: auto; margin-top: 1; align: right middle; }
    """
    BINDINGS: ClassVar = [("escape", "close", "close")]

    def __init__(self, sharing: Sharing | None = None) -> None:
        super().__init__()
        self._sharing = sharing or Sharing()
        self.body: str | None = None
        """The JSON as built; ``None`` while it is being built."""
        self.sample: bool | None = None
        """Whether it is a sample (sharing never on) and not the next shipment's body."""

    def compose(self) -> ComposeResult:
        with Vertical(id="preview-box"):
            yield Static(Text("What is shared"), id="preview-title")
            yield Static(Text("Building what would be sent…", style="dim"), id="preview-about")
            with VerticalScroll(id="preview-scroll"):
                yield Static(Text("building…", style="dim"), id="preview-body")
            with Horizontal(id="preview-buttons"):
                yield Button("Close", id="preview-close")

    def on_mount(self) -> None:
        self.query_one("#preview-close", Button).focus()
        sharing = self._sharing

        def build() -> tuple[str, bool]:
            return sharing.preview(), sharing.is_sample()

        self.run_worker(
            build,
            name=PREVIEW_WORKER,
            group=PREVIEW_WORKER,
            exclusive=True,
            thread=True,
            exit_on_error=False,
        )

    @on(Worker.StateChanged)
    def _built(self, event: Worker.StateChanged) -> None:
        if event.worker.name != PREVIEW_WORKER:
            return
        body = self.query_one("#preview-body", Static)
        if event.state is WorkerState.SUCCESS and isinstance(event.worker.result, tuple):
            self.body, self.sample = event.worker.result
            body.update(Text(self.body))
            self.query_one("#preview-about", Static).update(
                Text(ABOUT_SAMPLE if self.sample else ABOUT_PREVIEW)
            )
        elif event.state is WorkerState.ERROR:
            body.update(Text(f"could not build it: {event.worker.error}", style="red"))

    @on(Button.Pressed, "#preview-close")
    def _close(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


class ConsentDialog(ModalScreen[str]):
    """The one-time question. Answers ``share`` or ``declined``; Escape is *No thanks*."""

    CSS = """
    ConsentDialog { align: center middle; }
    ConsentDialog #consent-box {
        width: 80; height: auto; max-height: 90%; border: heavy $primary; background: $surface;
        padding: 1 2;
    }
    ConsentDialog #consent-title { height: 1; text-style: bold; margin-bottom: 1; }
    ConsentDialog #consent-text { height: auto; }
    ConsentDialog #consent-buttons { height: auto; margin-top: 1; align: right middle; }
    ConsentDialog #consent-buttons Button { margin-left: 2; }
    """
    BINDINGS: ClassVar = [("escape", "decline", "no thanks")]

    def __init__(self, sharing: Sharing | None = None) -> None:
        super().__init__()
        self._sharing = sharing or Sharing()

    def compose(self) -> ComposeResult:
        with Vertical(id="consent-box"):
            yield Static(Text("Help improve AISquare?"), id="consent-title")
            yield Static(Text(CONSENT_TEXT), id="consent-text")
            with Horizontal(id="consent-buttons"):
                yield Button("See what would be sent", id="consent-see")
                yield GuardedButton("Share anonymous usage", id="consent-share", variant="primary")
                yield Button("No thanks", id="consent-no")

    def on_mount(self) -> None:
        self.query_one("#consent-no", Button).focus()

    @on(Button.Pressed, "#consent-share")
    def _share(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss("share")

    @on(Button.Pressed, "#consent-no")
    def _no(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss("declined")

    @on(Button.Pressed, "#consent-see")
    def _see(self, event: Button.Pressed) -> None:
        event.stop()
        self.app.push_screen(PreviewDialog(self._sharing))

    def action_decline(self) -> None:
        self.dismiss("declined")


__all__ = [
    "CONSENT_TEXT",
    "PREVIEW_WORKER",
    "SHARE_WORKER",
    "SHIP_WORKER",
    "ConsentDialog",
    "PreviewDialog",
    "Sharing",
    "SharingBox",
    "status_text",
]
