"""Settings' *Remote control*: Remote on and off where the owner looks for it (#33).

Remote lived only in the ``R`` modal (``views.remote``) and the palette, and on a fresh
install the owner did not find it. This section is the same Remote, over the app's ONE
:class:`~aisquare.cli.ui.remote_control.RemoteController` (``FleetApp.remote``), never a
second one: what it turns on, the ``R`` modal shows on, and the other way round.

**Basic**, always shown:

- *Off*: *Turn on remote control*. When something Remote needs is missing, a short
  checklist comes first (:func:`prerequisites`), each line with its command and a Copy
  button: the remote extra (``remote_install_hint``), ngrok, and an ngrok authtoken.
  Without the extra nothing can start, so the checklist stands in for the button; with it,
  *Turn on anyway* is offered beside *Check again*, as a Remote without ngrok still serves
  this machine, and an authtoken kept where the check does not look still signs ngrok in.
- *On*: the link, copied by itself once ngrok's link (the one a phone can reach) arrives
  after a turn-on here, with a toast; *Copy*; the QR; the password; *Turn off*.

**Advanced**, collapsed until clicked: *Regenerate password*, *Allow write actions*,
*Auto-off* and the connected devices with *Revoke*, as the modal has them.

The controller is the model and every control calls it and repaints from it, on a
one-second tick while the section shows, as the modal does; the public link arrives from
ngrok's log on a thread of the controller's. Self-contained, so a later Settings layout
moves it with the line that mounts it (``views.settings_hub``).

:class:`RemoteChip` is the sidebar's ``📡 remote · N devices`` while Remote is on: a click
opens the ``R`` modal.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Button, Checkbox, Collapsible, DataTable, Select, Static
from textual.widgets.data_table import ColumnKey

from aisquare.cli.ui.clipboard import copy_and_tell
from aisquare.cli.ui.remote_control import AUTO_OFF_CHOICES, READ_ONLY_REASON, RemoteController
from aisquare.cli.ui.sidebar import Activatable
from aisquare.cli.ui.ticks import while_running
from aisquare.cli.ui.views.remote import (
    ELSEWHERE,
    RemotePanel,
    _auto_off_label,
    _auto_off_time,
    _device_cells,
    _link_text,
    qr_art,
)
from aisquare.services.ngrok_tunnel import ngrok_default_config
from aisquare.services.remote_server import REMOTE_SERVER_NEEDS, remote_install_hint

TICK_S = 1.0
"""How often the section repaints from the controller while it shows, as the modal does."""
CHIP_TICK_S = 2.0
"""How often the sidebar's chip looks whether Remote is on, and how many devices are in."""

AUTHTOKEN_COMMAND = "ngrok config add-authtoken <token>"
AUTHTOKEN_FROM = "https://dashboard.ngrok.com/get-started/your-authtoken"
_AUTHTOKEN_LINE = re.compile(r"^\s*authtoken\s*:\s*['\"]?[^\s'\"#]", re.MULTILINE)
"""A config line that sets an authtoken: ``authtoken: <value>``, top-level (v2) or under
``agent:`` (v3)."""

OFF_ABOUT = "the fleet on your phone: a link, its QR code and a password"
NEED_TIPS = {
    "ngrok": "ngrok gives the phone its link: https://ngrok.com/download",
    "authtoken": f"Your authtoken is at {AUTHTOKEN_FROM}",
}
"""Where each checklist line leads, under the pointer: the lines stay short at 80 columns."""
CHECKLIST_TITLE = "Remote needs these first:"


def ngrok_install_command(platform: str = sys.platform) -> str:
    """The one command that installs ngrok here, as ngrok's own download page gives it."""
    if platform == "darwin":
        return "brew install ngrok"
    if platform == "win32":
        return "winget install ngrok -s msstore"
    return "sudo snap install ngrok"


@dataclass(frozen=True)
class Prerequisite:
    """One line of the checklist: what Remote needs, the command that gets it, and whether it
    is there."""

    key: str
    what: str
    command: str
    met: bool


def has_authtoken(
    environ: Mapping[str, str] = os.environ, configs: list[Path] | None = None
) -> bool:
    """Whether ngrok has an authtoken to sign in with: ``NGROK_AUTHTOKEN``, or an
    ``authtoken:`` in its own config, where ``ngrok config add-authtoken`` writes it, or in
    the one ngrok 2 kept (``~/.ngrok2/ngrok.yml``)."""
    if environ.get("NGROK_AUTHTOKEN", "").strip():
        return True
    if configs is None:
        configs = [ngrok_default_config(environ=environ), Path.home() / ".ngrok2" / "ngrok.yml"]
    for config in configs:
        try:
            text = config.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _AUTHTOKEN_LINE.search(text):
            return True
    return False


def prerequisites() -> list[Prerequisite]:
    """What Remote needs, in the order to get it: the remote extra, ngrok on PATH, and ngrok's
    authtoken (:func:`has_authtoken`). Each read as it is now, on the calling thread."""
    extra = all(importlib.util.find_spec(name) is not None for name in REMOTE_SERVER_NEEDS)
    return [
        Prerequisite("extra", "the remote extra", "" if extra else remote_install_hint(), extra),
        Prerequisite("ngrok", "ngrok", ngrok_install_command(), shutil.which("ngrok") is not None),
        Prerequisite(
            "authtoken",
            "an ngrok authtoken (dashboard.ngrok.com)",
            AUTHTOKEN_COMMAND,
            has_authtoken(),
        ),
    ]


def shown(widget: Widget) -> bool:
    """Whether ``widget`` is on the screen in front: displayed, and every container around
    it too (a page the content switcher put away is not), under no dialog."""
    if not widget.is_attached or widget.screen is not widget.app.screen:
        return False
    return all(node.display for node in (widget, *widget.ancestors) if isinstance(node, Widget))


class RemoteSection(Vertical):
    """Remote control in Settings: Off (checklist or *Turn on*), On (link, QR, password,
    *Turn off*), and a collapsed *Advanced*. A view over ``controller``."""

    DEFAULT_CSS = """
    RemoteSection { height: auto; }
    RemoteSection Vertical { height: auto; }
    RemoteSection #settings-remote-title { height: 1; text-style: bold; margin: 1 0 1 0; }
    RemoteSection .remote-about { height: auto; color: $text-muted; }
    RemoteSection .remote-buttons { height: auto; margin: 1 0 0 0; }
    RemoteSection .remote-buttons Button { margin: 0 2 0 0; }
    RemoteSection .remote-need { height: auto; }
    RemoteSection .remote-need Static { width: 1fr; }
    RemoteSection .remote-need Button { margin: 0 0 0 2; }
    RemoteSection #settings-remote-checklist { height: auto; }
    RemoteSection #settings-remote-checklist-title { height: 1; }
    RemoteSection #settings-remote-state { height: auto; }
    RemoteSection #settings-remote-status { height: auto; color: $warning; }
    RemoteSection #settings-remote-link { height: auto; margin-top: 1; }
    RemoteSection #settings-remote-qr { height: auto; width: auto; margin-top: 1; }
    RemoteSection #settings-remote-password { height: auto; margin-top: 1; }
    RemoteSection #settings-remote-advanced { margin-top: 1; }
    RemoteSection #settings-remote-write-hint { height: auto; color: $text-muted; }
    RemoteSection #settings-remote-auto-off { width: 18; }
    RemoteSection #settings-remote-devices { height: 6; }
    RemoteSection .remote-adv-row { height: auto; margin-bottom: 1; }
    RemoteSection .remote-adv-row Static { width: auto; padding: 0 1 0 0; }
    """

    def __init__(self, controller: RemoteController | None = None, *, id: str | None = None):
        super().__init__(id=id)
        self._controller = controller
        self.needs: list[Prerequisite] = []
        """The checklist as last checked: at mount, on *Check again*, and as Remote goes off."""
        self._copy_armed = False
        """A turn-on here waits for ngrok's link, to copy it once it arrives."""
        self._was_running: bool | None = None
        self._drawn: tuple[str | None, bool] | None = None
        """The link the link line and the QR were last drawn for, and whether it was ngrok's."""
        self._painted: dict[str, bool] = {}
        """What the write box was last painted to show (``RemotePanel._painted``'s rule)."""
        self._device_rows: list[tuple[str, ...]] = []
        self._device_columns: list[ColumnKey] = []

    @property
    def controller(self) -> RemoteController:
        """The app's one Remote: the one the ``R`` modal is a view over too."""
        if self._controller is None:
            self._controller = self.app.remote  # type: ignore[attr-defined]
        return self._controller

    # --- layout -----------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static(Text("Remote control"), id="settings-remote-title")
        with Vertical(id="settings-remote-off"):
            yield Static(Text(OFF_ABOUT), id="settings-remote-about", classes="remote-about")
            with Vertical(id="settings-remote-checklist"):
                yield Static(Text(CHECKLIST_TITLE), id="settings-remote-checklist-title")
                for key in ("extra", "ngrok", "authtoken"):
                    with Horizontal(id=f"settings-remote-need-{key}", classes="remote-need"):
                        yield Static("", id=f"settings-remote-need-{key}-text")
                        yield Button(
                            "Copy",
                            id=f"settings-remote-need-{key}-copy",
                            classes="remote-need-copy",
                            compact=True,
                        )
            with Horizontal(classes="remote-buttons"):
                yield Button(
                    "Turn on remote control",
                    id="settings-remote-turn-on",
                    variant="primary",
                    compact=True,
                )
                yield Button("Check again", id="settings-remote-check", compact=True)
        with Vertical(id="settings-remote-on"):
            yield Static("", id="settings-remote-state")
            yield Static("", id="settings-remote-status")
            yield Static("", id="settings-remote-link")
            with Horizontal(classes="remote-buttons"):
                yield Button("Copy link", id="settings-remote-copy", compact=True)
                yield Button(
                    "Turn off", id="settings-remote-turn-off", variant="error", compact=True
                )
            yield Static("", id="settings-remote-qr")
            yield Static("", id="settings-remote-password")
        with Collapsible(title="Advanced", collapsed=True, id="settings-remote-advanced"):
            with Horizontal(classes="remote-adv-row"):
                yield Button("Regenerate password", id="settings-remote-regen", compact=True)
            with Vertical(classes="remote-adv-row"):
                yield Checkbox(
                    "Allow write actions",
                    self.controller.write_actions_allowed(),
                    id="settings-remote-write",
                    compact=True,
                )
                yield Static("", id="settings-remote-write-hint")
            with Horizontal(classes="remote-adv-row"):
                yield Static(Text("Auto-off"))
                yield Select(
                    [(_auto_off_label(minutes), minutes) for minutes in AUTO_OFF_CHOICES],
                    value=self.controller.state.auto_off_minutes,
                    allow_blank=False,
                    compact=True,
                    id="settings-remote-auto-off",
                )
            yield Static(Text("Connected devices"))
            yield DataTable(id="settings-remote-devices", cursor_type="row", zebra_stripes=True)
            with Horizontal(classes="remote-adv-row"):
                yield Button("Revoke selected", id="settings-remote-revoke", compact=True)

    def on_mount(self) -> None:
        table = self.query_one("#settings-remote-devices", DataTable)
        self._device_columns = table.add_columns("id", "device", "last seen", "expires", "")
        self.check()
        self.set_interval(TICK_S, self.tick)

    # --- the checklist ----------------------------------------------------------------------

    def check(self) -> None:
        """Look again at what Remote needs, and paint."""
        self.needs = prerequisites()
        self.repaint()

    def _paint_checklist(self) -> None:
        missing = [need for need in self.needs if not need.met]
        self.query_one("#settings-remote-checklist").display = bool(missing)
        for need in self.needs:
            line = self.query_one(f"#settings-remote-need-{need.key}", Horizontal)
            line.display = not need.met
            line.tooltip = NEED_TIPS.get(need.key)
            self.query_one(f"#settings-remote-need-{need.key}-text", Static).update(
                Text.assemble("✗ ", need.what, "  ", (need.command, "bold"))
            )
        extra = next((need.met for need in self.needs if need.key == "extra"), True)
        turn_on = self.query_one("#settings-remote-turn-on", Button)
        # Without the extra nothing can start; without ngrok or its token, Remote still
        # serves this machine, and a token kept elsewhere still signs ngrok in.
        turn_on.display = extra
        turn_on.label = "Turn on anyway" if missing else "Turn on remote control"
        self.query_one("#settings-remote-check", Button).display = bool(missing)

    # --- paint from the controller ----------------------------------------------------------

    @while_running
    def tick(self) -> None:
        """The one-second tick: the link to copy once it arrives, and a paint while shown."""
        self._copy_when_public()
        if shown(self):
            self.repaint()

    def _copy_when_public(self) -> None:
        """Copy ngrok's link once it arrives after a turn-on here, and say so: the link the
        phone needs, without a press of *Copy*."""
        if not self._copy_armed:
            return
        controller = self.controller
        if not controller.running:
            self._copy_armed = False
            return
        url, public = controller.remote_link()
        if url is not None and public:
            self._copy_armed = False
            copy_and_tell(self.app, url, "Remote link copied", sent="the link")

    def repaint(self, *, heard: str | None = None) -> None:
        """Paint everything from the controller; ``heard`` as in ``RemotePanel.repaint``."""
        controller = self.controller
        running = controller.running
        if self._was_running and not running:
            self.needs = prerequisites()  # back to Off: what it needs may have changed
        self._was_running = running
        self.query_one("#settings-remote-off").display = not running
        self.query_one("#settings-remote-on").display = running
        if running:
            self._paint_on()
        else:
            elsewhere = controller.served_elsewhere()
            about = Text(ELSEWHERE, style="bold yellow") if elsewhere else Text(OFF_ABOUT)
            status = controller.status_line()
            if status:
                about.append("\n" + status, style="yellow")
            self.query_one("#settings-remote-about", Static).update(about)
            self._paint_checklist()
        self._paint_advanced(heard)

    def _paint_on(self) -> None:
        controller = self.controller
        state = Text("● on", style="bold green")
        if controller.public_origin is None:
            state.append("  · this machine only until ngrok is up", style="dim")
        deadline = controller.adopt_server_deadline()
        if deadline is not None:
            state.append(f"  · auto-off at {_auto_off_time(deadline)}", style="dim")
        elif controller.state.auto_off_minutes is None:
            state.append("  · no auto-off", style="dim")
        self.query_one("#settings-remote-state", Static).update(state)
        status = self.query_one("#settings-remote-status", Static)
        said = controller.status_line()
        status.update(Text(said))
        status.display = bool(said)
        url, public = controller.remote_link()
        if (url, public) != self._drawn:
            self._drawn = (url, public)
            self.query_one("#settings-remote-link", Static).update(_link_text(url, public=public))
            self.query_one("#settings-remote-qr", Static).update(
                qr_art(url) if url and public else ""
            )
        self.query_one("#settings-remote-copy", Button).disabled = url is None
        self.query_one("#settings-remote-password", Static).update(
            Text.assemble("Password  ", (controller.password() or "—", "bold"))
        )

    def _paint_advanced(self, heard: str | None) -> None:
        controller = self.controller
        writes = controller.write_actions_allowed()
        box = self.query_one("#settings-remote-write", Checkbox)
        if heard == box.id or box.value == self._painted.get(box.id or "", box.value):
            with self.prevent(Checkbox.Changed):
                box.value = writes
            self._painted[box.id or ""] = writes
        self.query_one("#settings-remote-write-hint", Static).update(
            Text("writes reach the fleet" if writes else READ_ONLY_REASON)
        )
        self.query_one("#settings-remote-regen", Button).disabled = not controller.running
        collapsed = self.query_one("#settings-remote-advanced", Collapsible).collapsed
        if not collapsed and shown(self):  # the devices are read only where they show
            self._paint_devices(controller.devices())

    def _paint_devices(self, devices: list[dict[str, Any]]) -> None:
        """The devices table, rebuilt only when what it shows changed, the cursor kept on the
        device it was on."""
        table = self.query_one("#settings-remote-devices", DataTable)
        rows = [_device_cells(device) for device in devices]
        self.query_one("#settings-remote-revoke", Button).disabled = not rows
        if rows == self._device_rows:
            return
        at = table.cursor_row
        was = self._device_rows[at][0] if 0 <= at < len(self._device_rows) else None
        self._device_rows = rows
        table.clear()
        for row in rows:
            table.add_row(*(Text(cell) for cell in row), key=row[0])  # Text: a UA is no markup
        ids = [row[0] for row in rows]
        if rows:
            table.move_cursor(row=ids.index(was) if was in ids else min(max(at, 0), len(rows) - 1))

    # --- the controls -----------------------------------------------------------------------

    @on(Button.Pressed, "#settings-remote-turn-on")
    def _turn_on(self, event: Button.Pressed) -> None:
        event.stop()
        self._copy_armed = True
        self.controller.turn_on(wait=False)
        self.repaint()

    @on(Button.Pressed, "#settings-remote-turn-off")
    def _turn_off(self, event: Button.Pressed) -> None:
        event.stop()
        self._copy_armed = False
        self.controller.turn_off(wait=False)
        self.repaint()

    @on(Button.Pressed, "#settings-remote-check")
    def _check_again(self, event: Button.Pressed) -> None:
        event.stop()
        self.check()

    @on(Button.Pressed, "#settings-remote-copy")
    def _copy_link(self, event: Button.Pressed) -> None:
        event.stop()
        url = self.controller.link_url()
        if url is not None:
            copy_and_tell(self.app, url, "Link copied", sent="the link")

    @on(Button.Pressed, ".remote-need-copy")
    def _copy_command(self, event: Button.Pressed) -> None:
        event.stop()
        key = (event.button.id or "").removeprefix("settings-remote-need-").removesuffix("-copy")
        need = next((need for need in self.needs if need.key == key), None)
        if need is not None:
            copy_and_tell(self.app, need.command, "Command copied", sent="the command")

    @on(Button.Pressed, "#settings-remote-regen")
    def _regenerate(self, event: Button.Pressed) -> None:
        event.stop()
        self.controller.regenerate_password(wait=False)  # said once done (on_done)
        self.repaint()

    @on(Button.Pressed, "#settings-remote-revoke")
    def _revoke(self, event: Button.Pressed) -> None:
        event.stop()
        row = self.query_one("#settings-remote-devices", DataTable).cursor_row
        if 0 <= row < len(self._device_rows):
            self.controller.revoke_device(self._device_rows[row][0], wait=False)
        self.repaint()

    @on(Checkbox.Changed, "#settings-remote-write")
    def _allow_write(self, event: Checkbox.Changed) -> None:
        """The write box, acted on only when it disagrees with the controller and still shows
        what it said (``RemotePanel.on_switch_changed``'s rule)."""
        event.stop()
        if event.value != event.checkbox.value:
            return
        if event.value != self.controller.write_actions_allowed():
            self.controller.set_allow_write(event.value, wait=False)
            if event.value:
                self.notify("Write actions are ON for remote devices", severity="warning")
        self.repaint(heard=event.checkbox.id)

    @on(Select.Changed, "#settings-remote-auto-off")
    def _auto_off(self, event: Select.Changed) -> None:
        event.stop()
        if event.value not in AUTO_OFF_CHOICES:
            return
        if event.value == self.controller.state.auto_off_minutes:
            return  # the Select announcing its initial value at mount
        self.controller.set_auto_off(event.value, wait=False)
        self.repaint()

    @on(Collapsible.Expanded, "#settings-remote-advanced")
    def _advanced_opened(self, event: Collapsible.Expanded) -> None:
        event.stop()
        self.repaint()


class RemoteChip(Activatable):
    """``📡 remote · N devices`` in the sidebar while this asq's Remote is on; hidden while it
    is off. A click opens the ``R`` modal."""

    DEFAULT_CSS = """
    RemoteChip { color: $success; }
    """

    ALLOW_SELECT = False

    def __init__(self, controller: RemoteController, *, id: str | None = None) -> None:
        super().__init__(Text(""), id=id)
        self.controller = controller
        self.tooltip = "Remote control is on — open it (R)"

    def on_mount(self) -> None:
        self.display = False
        self.tick()
        self.set_interval(CHIP_TICK_S, self.tick)

    @while_running
    def tick(self) -> None:
        controller = self.controller
        self.display = controller.running
        if not controller.running:
            return
        signed_in = sum(1 for device in controller.devices() if device.get("signed_in"))
        noun = "device" if signed_in == 1 else "devices"
        self.update(Text(f"📡 remote · {signed_in} {noun}"))

    def activate(self) -> None:
        self.app.push_screen(RemotePanel(self.controller))
