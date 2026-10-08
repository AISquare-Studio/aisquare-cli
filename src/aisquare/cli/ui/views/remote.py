"""``RemotePanel`` — the ``R`` modal: Remote on/off, password, link + QR, devices.

PLAN §5 step 1: "press ``m`` → Remote on → modal shows link, QR, password" (``R``
now: the sidebar took ``m`` for marking cards). The
dialog is a view over the app's one :class:`RemoteController` (see
``remote_control.py``): every control calls the controller and then repaints
from it, and a one-second tick repaints too, because the public URL arrives
from ngrok's log on a background thread. Opened like the theme picker
(``push_screen``), closed with Esc.

The QR is segno's compact terminal rendering (half-block characters, ~18 rows
for an ngrok URL) of exactly the text in the link row — one string feeds both,
so what the phone scans is what the human reads — in colours of its own, never
the theme's (:data:`QR_COLOURS`).
"""

from __future__ import annotations

import io
from datetime import tzinfo
from typing import Any, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Label, Select, Static, Switch
from textual.widgets.data_table import ColumnKey

from aisquare.cli.ui.remote_control import (
    AUTO_OFF_CHOICES,
    READ_ONLY_REASON,
    RemoteController,
)
from aisquare.services.remote_server import _remote_instant

QR_UNAVAILABLE = "QR unavailable — pip install segno"
LOCAL_ZONE: tzinfo | None = None
"""The zone the panel says its times in: ``None`` is this machine's own (a seam for tests)."""
QR_COLOURS = "#ffffff on #000000"
"""The QR's own colours: light glyphs on a dark ground, whatever the theme.

segno's compact art draws the LIGHT modules as block glyphs and leaves the dark
ones to the background, so it reads right only light-on-dark. In the theme's
colours, every light theme (``t`` offers five) and an ANSI one on a light
terminal drew it reflectance-reversed, its quiet zone a dark frame: a scanner
without inversion support could not read it (sweep of #243). Not "black on
white", which reverses it in every theme."""


def qr_art(url: str) -> Text:
    """The QR for ``url`` in :data:`QR_COLOURS`; the notice, plain, when segno is absent."""
    art = qr_text(url)
    return Text(art) if art == QR_UNAVAILABLE else Text(art, style=QR_COLOURS)


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
    #remote-unlocks { height: auto; color: $error; }
    #remote-link { width: 100%; height: auto; }
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
        self._device_rows: list[tuple[str, ...]] = []
        """The devices table's cells as last painted, a row per device, its id first."""
        self._device_columns: list[ColumnKey] = []
        self._qr_url: str | None = ""
        """The link the QR was last drawn for; ``""`` before the first paint."""
        self._painted: dict[str, bool] = {}
        """What each switch was last painted to show, by id. A switch showing anything else
        was moved by the user since, and its ``Changed`` is still on its way."""

    # --- layout ---------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        state = self.controller.state
        writes = self.controller.write_actions_allowed()
        with VerticalScroll(id="remotebox"):
            yield Static("Remote control — the fleet on your phone · Esc closes", id="remotehint")
            with Horizontal(classes="row"):
                yield Label("Remote")
                yield Switch(self.controller.running, id="remote-on")
                yield Static("", id="remote-state")
            yield Static("", id="remote-status")
            # Copy sits on the LABEL row and the link gets a line of its own.
            # Sharing one row cost the demo its most important control: a real
            # ngrok link is ~84 characters, which with the 22-wide label runs
            # past the 104-wide dialog and pushed Copy off the edge — measured,
            # the button was laid out at x=135 inside a box ending at x=151 and
            # painted nowhere. On its own line the link can be any length (it
            # wraps instead of shoving a neighbour out) and Copy cannot move.
            with Horizontal(classes="row"):
                yield Label("Link")
                yield Button("Copy", id="remote-copy", compact=True)
            yield Static("", id="remote-link")
            yield Static("", id="remote-qr")
            with Horizontal(classes="row"):
                yield Label("Password")
                yield Static("", id="remote-password")
                yield Button("Regenerate", id="remote-regen", compact=True)
            with Horizontal(classes="row"):
                yield Label("Allow write actions")
                yield Switch(writes, id="remote-allow-write")
                yield Static("", id="remote-write-hint")
            with Horizontal(classes="row"):
                yield Label("Auto-off", id="remote-auto-off-label")
                yield Select(
                    [(_auto_off_label(minutes), minutes) for minutes in AUTO_OFF_CHOICES],
                    value=state.auto_off_minutes,
                    allow_blank=False,
                    id="remote-auto-off",
                )
            yield Label("Connected devices")
            yield Static("", id="remote-unlocks")
            yield DataTable(id="remote-devices", cursor_type="row", zebra_stripes=True)
            with Horizontal(classes="row"):
                yield Button("Revoke selected", id="remote-revoke", compact=True)

    def on_mount(self) -> None:
        table = self.query_one("#remote-devices", DataTable)
        self._device_columns = table.add_columns("id", "device", "last seen", "expires", "")
        self.repaint()
        self.set_interval(1.0, self.repaint)
        self.query_one("#remote-on", Switch).focus()

    # --- paint from the controller -------------------------------------------------------------

    def repaint(self, *, heard: str | None = None) -> None:
        """Paint everything from the controller; the one-second tick runs it too.

        On Textual's own thread, so a tick reads ``remote_server_status()`` once, for
        the devices and the failed unlocks both, and draws the link and its QR only when
        the link changed: every tick used to encode the QR anew (about 4 ms of segno)
        and read the status twice, each read three digests of ``remote.json``.
        ``heard`` is the switch whose ``Changed`` was just handled (:meth:`_paint_switch`).
        """
        controller = self.controller
        running = controller.running
        writes = controller.write_actions_allowed()
        status = controller.remote_status()
        self._paint_switch("remote-on", running, heard)
        self._paint_switch("remote-allow-write", writes, heard)
        self.query_one("#remote-state", Static).update(self._state_text())
        # Text, never a str, which is read as markup: the sentences carry exception text
        # and paths, where "[b]" was a tag and "[/b]" a MarkupError out of the repaint.
        self.query_one("#remote-status", Static).update(Text(controller.status_line()))
        self.query_one("#remote-password", Static).update(
            Text(controller.password() or "—", style="bold")
        )
        self.query_one("#remote-write-hint", Static).update(
            "writes reach the fleet" if writes else READ_ONLY_REASON
        )
        self.query_one("#remote-unlocks", Static).update(self._unlocks_text(status))
        url = controller.link_url()
        if url != self._qr_url:
            self._qr_url = url
            self.query_one("#remote-link", Static).update(Text(url or "turn Remote on for a link"))
            self.query_one("#remote-qr", Static).update(qr_art(url) if url else "")
        self.query_one("#remote-regen", Button).disabled = not running
        self.query_one("#remote-copy", Button).disabled = url is None
        self._paint_devices(controller.devices(status))

    def _paint_switch(self, switch_id: str, value: bool, heard: str | None) -> None:
        """Show the controller's ``value`` on a switch, posting no ``Changed`` of its own.

        Each value written back used to come round as a ``Changed`` the handler took for
        the user's, and with two presses in flight they never ran out: the handler flipped
        the controller, this flipped the switch back, its echo flipped the controller again,
        starting uvicorn and ngrok and revoking every phone, for as long as the panel stayed
        open (sweep of #243). A switch the user moved since the last paint keeps the user's
        word until :meth:`on_switch_changed` has handled it (``heard``): the one-second tick
        landing between a press and its ``Changed`` turned it back, and the press was lost.
        """
        switch = self.query_one(f"#{switch_id}", Switch)
        if heard != switch_id and switch.value != self._painted.get(switch_id, switch.value):
            return
        with self.prevent(Switch.Changed):
            switch.value = value
        self._painted[switch_id] = value

    def _state_text(self) -> Text:
        controller = self.controller
        if not controller.running:
            return Text("off", style="dim")
        text = Text("on", style="bold green")
        if controller.public_url is None:
            text.append("  · local only — no tunnel yet", style="dim")
        deadline = controller.adopt_server_deadline()  # a phone's extension shows here too
        if deadline is not None:
            text.append(f"  · auto-off at {deadline.astimezone(LOCAL_ZONE):%H:%M}", style="dim")
        elif controller.state.auto_off_minutes is None:
            # Never: say so, rather than leave the slot the timer usually fills empty —
            # "on" with nothing after it reads like the timer simply has not armed yet.
            text.append("  · no auto-off", style="dim")
        return text

    def _unlocks_text(self, status: dict[str, Any]) -> Text:
        """Wrong passphrases lately, and, once they paused new unlocks, what to do about it."""
        failed, until = self.controller.unlock_failures(status)
        if not failed:
            return Text("")
        if until is None:
            return Text(f"{failed} failed unlocks in 30 min", style="dim")
        return Text(
            f"{failed} failed unlocks in 30 min — new unlocks paused; rotate the link: "
            "aisquare remote regenerate-password --new-link"
        )

    def _paint_devices(self, devices: list[dict[str, Any]]) -> None:
        """The devices table, kept to ``devices``. The same devices get their changed cells
        replaced in place, so the cursor and the scroll stay where the user put them; it
        was left alone instead, and a phone signed out by a day idle still read "signed
        in", one back on its old "last seen": the columns a revoke is decided from. Other
        devices rebuild it, the cursor kept on the device it was on, or the row it was."""
        table = self.query_one("#remote-devices", DataTable)
        rows = [_device_cells(device) for device in devices]
        ids = [row[0] for row in rows]
        self.query_one("#remote-revoke", Button).disabled = not ids
        if ids == [row[0] for row in self._device_rows] and table.row_count == len(rows):
            for row, painted in zip(rows, self._device_rows, strict=True):
                for column, cell, was in zip(self._device_columns, row, painted, strict=True):
                    if cell != was:  # "signed out" is wider than "signed in"
                        table.update_cell(row[0], column, Text(cell), update_width=True)
            self._device_rows = rows
            return
        at = table.cursor_row
        on = self._device_rows[at][0] if 0 <= at < len(self._device_rows) else None
        self._device_rows = rows
        table.clear()
        for row in rows:
            # Text cells, never str: a str cell is parsed as Rich markup, and a User-Agent
            # is the phone's own text — `x [/b]` raised MarkupError and took the TUI down.
            table.add_row(*(Text(cell) for cell in row), key=row[0])
        if rows:
            table.move_cursor(row=ids.index(on) if on in ids else min(max(at, 0), len(rows) - 1))

    # --- the controls ---------------------------------------------------------------------------

    def on_switch_changed(self, event: Switch.Changed) -> None:
        """A switch moved. Act on what it shows now, only when that DISAGREES with the
        controller; a repaint posts no ``Changed`` of its own (:meth:`_paint_switch`).

        A ``Changed`` whose value the switch no longer shows is a press a later one has
        taken back, still on its way, and acting on it is what flipped Remote on and off
        for as long as the panel stayed open: two presses before the first was handled
        (impatience while Remote stops, key repeat, keys batched over SSH) are the human's
        net word, which the last of them carries. Comparing against the controller
        needs no flag and cannot go stale: a real toggle never matches it.
        """
        switch = event.switch
        if event.value != switch.value:
            return
        if switch.id == "remote-on" and event.value != self.controller.running:
            # Never waiting for a Remote to stop: its server and ngrok take seconds to wind
            # down, on a thread of their own, and the status line says when they are done.
            if event.value:
                self.controller.turn_on(wait=False)
            else:
                self.controller.turn_off(wait=False)
        elif (
            switch.id == "remote-allow-write"
            and event.value != self.controller.write_actions_allowed()
        ):
            self.controller.set_allow_write(event.value)
            if event.value:
                self.notify("Write actions are ON for remote devices", severity="warning")
        self.repaint(heard=switch.id)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "remote-auto-off" or event.value not in AUTO_OFF_CHOICES:
            return
        if event.value == self.controller.state.auto_off_minutes:
            return  # the Select announcing its initial value at mount — not a change
        self.controller.set_auto_off(event.value)
        self.repaint()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "remote-regen":
            if self.controller.regenerate_password() is not None:
                self.notify("New password — every device has to unlock again")
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
        if not 0 <= row < len(self._device_rows):
            return
        device_id = self._device_rows[row][0]
        if self.controller.revoke_device(device_id):  # else the status line says why not
            self.notify(f"Revoked {device_id}")

    def action_close_panel(self) -> None:
        self.dismiss(None)


def _auto_off_label(minutes: int | None) -> str:
    """What the Auto-off picker shows for a choice; ``None`` is Never."""
    return "Never" if minutes is None else f"{minutes} min"


def _device_cells(device: dict[str, Any]) -> tuple[str, ...]:
    """One device as the table shows it: id, browser, last seen, sign-in end, state."""
    return (
        str(device["id"]),
        _short_cell(device.get("ua"), 40) or "unknown device",
        _device_time(device.get("last_seen")),
        _device_time(device.get("expires_at")),
        "signed in" if device.get("signed_in") else "signed out",
    )


def _device_time(value: Any) -> str:
    """A device's time as the auto-off line says its own: in this machine's zone, with the
    date (``Oct 14 20:58``), since a sign-in ends a week on, on the same weekday.

    The server's UTC stamps were cut to 19 characters, the offset and the seconds' last
    digit gone: in Los Angeles at 20:58 a phone seen that second read ``2026-10-08T03:58:0…``,
    tomorrow, two rows under "auto-off at 21:58" (sweep of #243). Read as the server reads
    them (:func:`~aisquare.services.remote_server._remote_instant`: a stamp without its offset
    is UTC); one that is no time at all shows as it came.
    """
    at = _remote_instant(value)
    if at is None:
        return _short_cell(value, 19) or "—"
    here = at.astimezone(LOCAL_ZONE)
    return f"{here:%b} {here.day} {here:%H:%M}"


def _short_cell(value: Any, width: int) -> str:
    text = str(value) if value is not None else ""
    return text if len(text) <= width else text[: width - 1] + "…"
