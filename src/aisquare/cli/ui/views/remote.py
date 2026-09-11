"""``RemotePanel`` — the ``m`` modal: Remote on/off, password, link + QR, devices.

PLAN §5 step 1: "press ``m`` → Remote on → modal shows link, QR, password". The
dialog is a view over the app's one :class:`RemoteController` (see
``remote_control.py``): every control calls the controller and then repaints
from it, and a one-second tick repaints too, because the public URL arrives
from ngrok's log on a background thread. Opened like the theme picker
(``push_screen``), closed with Esc.

The QR is segno's compact terminal rendering (half-block characters, ~18 rows
for an ngrok URL) of exactly the text in the link row — one string feeds both,
so what the phone scans is what the human reads.
"""

from __future__ import annotations

import io
from typing import Any, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Label, Select, Static, Switch

from aisquare.cli.ui.remote_control import (
    AUTO_OFF_CHOICES,
    READ_ONLY_REASON,
    RemoteController,
)

QR_UNAVAILABLE = "QR unavailable — pip install segno"


def qr_text(url: str) -> str:
    """The QR for ``url`` as compact terminal art; a one-line notice when segno is absent."""
    try:
        import segno
    except ImportError:
        return QR_UNAVAILABLE
    buffer = io.StringIO()
    segno.make(url, error="l").terminal(out=buffer, compact=True, border=1)
    return buffer.getvalue().rstrip("\n")


class RemotePanel(ModalScreen[None]):
    """Remote on/off · password · allow write · auto-off · devices · link + QR. Esc closes."""

    CSS = """
    RemotePanel { align: center middle; }
    #remotebox { width: 104; max-width: 100%; height: 90%; border: heavy $accent;
                 background: $surface; padding: 0 1; }
    #remotehint { height: 1; color: $text-muted; }
    #remotebox .row { height: auto; min-height: 3; }
    #remotebox .row Static { padding-top: 1; width: auto; }
    #remotebox .row Label { width: 22; padding-top: 1; }
    #remotebox .row Switch { margin-right: 1; }
    #remotebox .row Button { margin-top: 1; margin-left: 2; }
    #remote-status { height: auto; min-height: 1; color: $warning; }
    #remote-link { width: 1fr; }
    #remote-auto-off { width: 16; }
    #remote-auto-off-label { padding-top: 2; }
    #remote-qr { height: auto; width: auto; }
    #remote-devices { height: 8; }
    #remote-write-hint { color: $text-muted; }
    """
    BINDINGS: ClassVar = [("escape", "close_panel", "close")]

    def __init__(self, controller: RemoteController) -> None:
        super().__init__()
        self.controller = controller
        self._syncing = False
        """Set while the switches are being painted FROM the controller, so their
        ``Changed`` messages are not mistaken for the user's."""
        self._device_sids: list[str] = []

    # --- layout ---------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        state = self.controller.state
        with VerticalScroll(id="remotebox"):
            yield Static("Remote control — the fleet on your phone · Esc closes", id="remotehint")
            with Horizontal(classes="row"):
                yield Label("Remote")
                yield Switch(self.controller.running, id="remote-on")
                yield Static("", id="remote-state")
            yield Static("", id="remote-status")
            with Horizontal(classes="row"):
                yield Label("Link")
                yield Static("", id="remote-link")
                yield Button("Copy", id="remote-copy", compact=True)
            yield Static("", id="remote-qr")
            with Horizontal(classes="row"):
                yield Label("Password")
                yield Static("", id="remote-password")
                yield Button("Regenerate", id="remote-regen", compact=True)
            with Horizontal(classes="row"):
                yield Label("Allow write actions")
                yield Switch(state.allow_write, id="remote-allow-write")
                yield Static("", id="remote-write-hint")
            with Horizontal(classes="row"):
                yield Label("Auto-off", id="remote-auto-off-label")
                yield Select(
                    [(f"{minutes} min", minutes) for minutes in AUTO_OFF_CHOICES],
                    value=state.auto_off_minutes,
                    allow_blank=False,
                    id="remote-auto-off",
                )
            yield Label("Connected devices")
            yield DataTable(id="remote-devices", cursor_type="row", zebra_stripes=True)
            with Horizontal(classes="row"):
                yield Button("Revoke selected", id="remote-revoke", compact=True)

    def on_mount(self) -> None:
        table = self.query_one("#remote-devices", DataTable)
        table.add_columns("device", "first seen", "last seen")
        self.repaint()
        self.set_interval(1.0, self.repaint)
        self.query_one("#remote-on", Switch).focus()

    # --- paint from the controller -------------------------------------------------------------

    def repaint(self) -> None:
        controller = self.controller
        running = controller.running
        self._syncing = True
        try:
            self.query_one("#remote-on", Switch).value = running
            self.query_one("#remote-allow-write", Switch).value = controller.state.allow_write
        finally:
            self._syncing = False
        self.query_one("#remote-state", Static).update(self._state_text())
        self.query_one("#remote-status", Static).update(controller.message or "")
        self.query_one("#remote-password", Static).update(
            Text(controller.password() or "—", style="bold")
        )
        self.query_one("#remote-write-hint", Static).update(
            "writes reach the fleet" if controller.state.allow_write else READ_ONLY_REASON
        )
        url = controller.link_url()
        self.query_one("#remote-link", Static).update(Text(url or "turn Remote on for a link"))
        self.query_one("#remote-qr", Static).update(qr_text(url) if url else "")
        self.query_one("#remote-regen", Button).disabled = not running
        self.query_one("#remote-copy", Button).disabled = url is None
        self._paint_devices()

    def _state_text(self) -> Text:
        controller = self.controller
        if not controller.running:
            return Text("off", style="dim")
        text = Text("on", style="bold green")
        if controller.public_url is None:
            text.append("  · local only — no tunnel yet", style="dim")
        if controller.auto_off_at is not None:
            text.append(f"  · auto-off at {controller.auto_off_at:%H:%M}", style="dim")
        return text

    def _paint_devices(self) -> None:
        table = self.query_one("#remote-devices", DataTable)
        devices = self.controller.devices()
        sids = [str(d["sid"]) for d in devices]
        if sids == self._device_sids and table.row_count == len(sids):
            return  # same rows: keep the cursor where the user put it
        self._device_sids = sids
        table.clear()
        for device in devices:
            table.add_row(
                _short(device.get("ua"), 40) or "unknown device",
                _short(device.get("first_seen"), 19) or "—",
                _short(device.get("last_seen"), 19) or "—",
                key=str(device["sid"]),
            )
        self.query_one("#remote-revoke", Button).disabled = not sids

    # --- the controls ---------------------------------------------------------------------------

    def on_switch_changed(self, event: Switch.Changed) -> None:
        if self._syncing:
            return
        if event.switch.id == "remote-on":
            if event.value:
                self.controller.turn_on()
            else:
                self.controller.turn_off()
        elif event.switch.id == "remote-allow-write":
            self.controller.set_allow_write(event.value)
            if event.value:
                self.notify("Write actions are ON for remote devices", severity="warning")
        self.repaint()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "remote-auto-off" or not isinstance(event.value, int):
            return
        if event.value == self.controller.state.auto_off_minutes:
            return  # the Select announcing its initial value at mount — not a change
        self.controller.set_auto_off(event.value)
        self.repaint()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "remote-regen":
            if self.controller.regenerate_password() is not None:
                self.notify("New password — devices already unlocked stay unlocked")
        elif event.button.id == "remote-copy":
            url = self.controller.link_url()
            if url is not None:
                self.app.copy_to_clipboard(url)
                self.notify("Link copied")
        elif event.button.id == "remote-revoke":
            self._revoke_selected()
        self.repaint()

    def _revoke_selected(self) -> None:
        table = self.query_one("#remote-devices", DataTable)
        row = table.cursor_row
        if not self._device_sids or row < 0 or row >= len(self._device_sids):
            return
        sid = self._device_sids[row]
        self.controller.revoke(sid)
        self._device_sids = []  # force the table to be rebuilt on the next paint
        self.notify(f"Revoked {sid[:8]}…")

    def action_close_panel(self) -> None:
        self.dismiss(None)


def _short(value: Any, width: int) -> str:
    text = str(value) if value is not None else ""
    return text if len(text) <= width else text[: width - 1] + "…"
