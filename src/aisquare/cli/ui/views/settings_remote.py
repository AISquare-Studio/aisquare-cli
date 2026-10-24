"""Settings' *Remote control*: Remote on and off where the owner looks for it (#33).

Remote lived only in the ``R`` modal (``views.remote``) and the palette, and on a fresh
install the owner did not find it. This section is the same Remote, over the app's ONE
:class:`~aisquare.cli.ui.remote_control.RemoteController` (``FleetApp.remote``), never a
second one: what it turns on, the ``R`` modal shows on, and the other way round.

**Basic**, always shown:

- *Off*: *Turn on remote control*. With no ngrok token yet, :class:`TokenSlot` stands in
  its place: *Paste your ngrok token*, where to get a free one, and *Save*. Remote needs
  nothing else from the owner (the owner, 2026-10-23: Remote works out of the box, #34).
  Only an install whose ngrok is missing gets one more line, with a fix in the app, never
  a command to run (:class:`NgrokMissing`).
- *On*: the link, copied by itself once ngrok's link (the one a phone can reach) arrives
  after a turn-on here, with a toast; *Copy*; the QR; the password; *Turn off*.
- The token slot shows on and off alike while ngrok refused the token Remote's tunnel signed
  in with, or there was none (``RemoteController.tunnel_error``), as *Replace your ngrok
  token* where there is one; *Change ngrok token* under Advanced opens it too. A saved
  token starts again a tunnel that is not up (review of #274, F-1).

**What Remote needs** (#34: ngrok bundled, its token pasted here), in one place:
:func:`ngrok_found` (is there an ngrok to start: the bundled SDK, else one on PATH),
:func:`has_authtoken` (is a token configured), :func:`store_ngrok_token` (check a pasted one
with ngrok and keep it, or raise :class:`TokenRefused` with why) and :meth:`NgrokMissing.fix`
(look again, for a damaged install's ngrok). All of it is ``services.ngrok_sdk``'s.

**Advanced**, collapsed until clicked: *Change ngrok token*, *Regenerate password*, *Allow
write actions*, *Auto-off* and the connected devices with *Revoke*, as the modal has them.

The controller is the model and every control calls it and repaints from it, on a
one-second tick while the section shows, as the modal does; the public link arrives from
ngrok's log on a thread of the controller's. What Remote needs is looked at on a thread
worker (:meth:`RemoteSection.remote_check`). Self-contained, so a later Settings layout
moves it with the line that mounts it: App settings' own *Remote* tab now (the owner's
placement, #41), whose ``.section`` and ``.about`` rules give its heading and help lines
that form's format.

:class:`RemoteChip` is the sidebar's ``📡 remote · N devices`` while Remote is on: a click
opens the ``R`` modal.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Button, Checkbox, Collapsible, DataTable, Input, Select, Static
from textual.widgets.data_table import ColumnKey
from textual.worker import Worker, WorkerState

from aisquare.cli.ui.clipboard import copy_and_tell
from aisquare.cli.ui.remote_control import AUTO_OFF_CHOICES, READ_ONLY_REASON, RemoteController
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
from aisquare.services import ngrok_sdk
from aisquare.services.ngrok_sdk import BAD_TOKEN
from aisquare.services.ngrok_tunnel import AUTHTOKEN_FROM as AUTHTOKEN_FROM
from aisquare.services.ngrok_tunnel import AUTHTOKEN_HINT

TICK_S = 1.0
"""How often the section repaints from the controller while it shows, as the modal does."""
CHIP_TICK_S = 2.0
"""How often the sidebar's chip looks whether Remote is on, and how many devices are in."""

TOKEN_WORKER = "settings-remote-token"
"""The worker that saves a pasted ngrok token (:func:`store_ngrok_token`)."""
CHECK_WORKER = "settings-remote-check"
"""The worker that looks at what Remote needs (:func:`remote_needs`)."""

OFF_ABOUT = "the fleet on your phone: a link, its QR code and a password"
TITLE = ("Remote control", "the fleet on your phone")
"""The section heading: its name, and what it is, dim (``app_settings.section_title``)."""
TOKEN_TITLE = "Paste your ngrok token"
REPLACE_TITLE = "Replace your ngrok token"
TOKEN_ABOUT = "Remote reaches your phone through ngrok: a free account's token, once."
ENV_TOKEN = (
    "NGROK_AUTHTOKEN is set where asq started: ngrok signs in with it, not with a token saved here"
)
"""The one line beside the token field while ``NGROK_AUTHTOKEN`` overrides what it saves."""
NGROK_MISSING = ngrok_sdk.SDK_MISSING
TOKEN_TROUBLE = (BAD_TOKEN, AUTHTOKEN_HINT)
"""What a tunnel says that ended for want of a token ngrok takes: the token slot shows."""


# --- what Remote needs (#34: services.ngrok_sdk) ---------------------------------------------


class TokenRefused(Exception):
    """Why a pasted token was not saved, in plain words: the line beside the field says it."""


def ngrok_found() -> bool:
    """Whether there is an ngrok to start: the bundled one (``ngrok_sdk``), which every install
    has but a damaged one, else an ``ngrok`` on PATH."""
    return ngrok_sdk.sdk() is not None or shutil.which("ngrok") is not None


def has_authtoken(
    environ: Mapping[str, str] = os.environ, configs: list[Path] | None = None
) -> bool:
    """Whether ngrok has a token to sign in with (``ngrok_sdk.find_authtoken``):
    ``NGROK_AUTHTOKEN``, the one pasted here, or an ``authtoken:`` in the human's own ngrok
    config (``configs``: ngrok 3's and ngrok 2's when not given)."""
    return ngrok_sdk.find_authtoken(environ, configs) is not None


def token_from_environment(environ: Mapping[str, str] = os.environ) -> bool:
    """Whether ``NGROK_AUTHTOKEN`` is set: ngrok signs in with it, whatever is saved here."""
    return bool(environ.get("NGROK_AUTHTOKEN", "").strip())


def remote_needs() -> tuple[bool, bool, bool]:
    """What Remote needs, as it is now: an ngrok (:func:`ngrok_found`, which imports the SDK's
    native library), a token (:func:`has_authtoken`, which parses ngrok's own configs), and
    whether ``NGROK_AUTHTOKEN`` overrides a saved one. On a worker (:data:`CHECK_WORKER`)."""
    return ngrok_found(), has_authtoken(), token_from_environment()


def store_ngrok_token(token: str) -> None:
    """Save ``token`` where Remote's ngrok reads it, or raise :class:`TokenRefused` saying why.

    ngrok's service is asked first whether it takes the token (``ngrok_sdk.check_token``);
    one it refuses is not kept. Kept in the aisquare home, owner-only. The token is never
    said: not in a toast, a log or an error. On a worker (:data:`TOKEN_WORKER`).
    """
    token = token.strip()
    if not token:
        raise TokenRefused("paste the token first")
    if any(character.isspace() for character in token):
        raise TokenRefused("that is not one token: it has spaces in it")
    refused = ngrok_sdk.check_token(token)
    if refused is not None:
        raise TokenRefused(refused)
    try:
        ngrok_sdk.save_token(token)
    except OSError as exc:
        raise TokenRefused(f"the token could not be saved: {type(exc).__name__}") from None


def remote_shown(widget: Widget) -> bool:
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
    RemoteSection .remote-about { height: auto; color: $text-muted; }
    RemoteSection .remote-buttons { height: auto; margin: 1 0 0 0; }
    RemoteSection .remote-buttons Button { margin: 0 2 0 0; }
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
    RemoteSection .remote-adv-row Button { margin: 0 2 0 0; }
    """

    def __init__(self, controller: RemoteController | None = None, *, id: str | None = None):
        super().__init__(id=id)
        self._controller = controller
        self.ngrok: bool | None = None
        """Whether there is an ngrok to start, as last looked (:func:`ngrok_found`); ``None``
        until the first look answers."""
        self.token: bool | None = None
        """Whether an ngrok token is configured, as last looked (:func:`has_authtoken`)."""
        self.env_token = False
        """Whether ``NGROK_AUTHTOKEN`` is set, as last looked (:func:`token_from_environment`)."""
        self._replacing = False
        """*Change ngrok token* (Advanced) opened the token slot, until a token is saved."""
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
        # Here, not at the top: App settings, which holds the form's format, mounts this.
        from aisquare.cli.ui.views.app_settings import section_title

        yield Static(section_title(*TITLE), id="settings-remote-title", classes="section")
        with Vertical(id="settings-remote-off"):
            yield Static(Text(OFF_ABOUT), id="settings-remote-about", classes="remote-about about")
            yield NgrokMissing(id="settings-remote-ngrok-missing")
        with Vertical(id="settings-remote-on-head"):
            yield Static("", id="settings-remote-state")
            yield Static("", id="settings-remote-status")
        # On and off alike: under why Remote has no tunnel, whichever view says it.
        yield TokenSlot(id="settings-remote-token")
        with Horizontal(classes="remote-buttons", id="settings-remote-turn-on-row"):
            yield Button(
                "Turn on remote control",
                id="settings-remote-turn-on",
                variant="primary",
                compact=True,
            )
        with Vertical(id="settings-remote-on"):
            yield Static("", id="settings-remote-link")
            with Horizontal(classes="remote-buttons"):
                yield Button("Copy link", id="settings-remote-copy", compact=True)
                yield Button(
                    "Turn off", id="settings-remote-turn-off", variant="error", compact=True
                )
            yield Static("", id="settings-remote-qr")
            yield Static("", id="settings-remote-password")
        with Collapsible(title="Advanced", collapsed=True, id="settings-remote-advanced"):
            # One above the other: side by side they ran two columns past the tab at 80x24
            # once the opened fold brought the scrollbar (#44, m4).
            with Vertical(classes="remote-adv-row"):
                yield Button("Change ngrok token", id="settings-remote-change-token", compact=True)
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
        self.repaint()
        self.remote_check()
        self.set_interval(TICK_S, self.remote_tick)

    # --- what Remote needs --------------------------------------------------------------------

    def remote_check(self) -> None:
        """Look again at what Remote needs (an ngrok, its token), off the UI thread, and paint
        once it answers (:meth:`_remote_checked`).

        It ran on Textual's thread at Settings' first open and at each turn-off: the SDK's
        import loads a native library (a third of a second cold) and the token is looked
        for in up to two YAML files of ngrok's (review of #274, m6)."""
        self.run_worker(
            remote_needs,
            name=CHECK_WORKER,
            group=CHECK_WORKER,
            exclusive=True,
            thread=True,
            exit_on_error=False,
        )

    @on(Worker.StateChanged)
    def _remote_checked(self, event: Worker.StateChanged) -> None:
        needs = event.worker.result
        if event.worker.name != CHECK_WORKER or not isinstance(needs, tuple):
            return
        self.ngrok, self.token, self.env_token = needs
        self.repaint()

    def _paint_needs(self, running: bool) -> None:
        """The token slot while there is no token, or ngrok refused the one there is (on and
        off alike), or *Change ngrok token* asked for it; off, *Turn on* while there is a token,
        and the ngrok line only while ngrok is missing. Neither the slot for want of a token
        nor *Turn on* before the first look answers."""
        self.query_one(NgrokMissing).display = self.ngrok is False
        trouble = self.controller.tunnel_error()
        slot = self.query_one(TokenSlot)
        slot.display = self.token is False or trouble in TOKEN_TROUBLE or self._replacing
        slot.remote_paint(
            replacing=bool(self.token) or trouble == BAD_TOKEN, env_token=self.env_token
        )
        self.query_one("#settings-remote-turn-on-row").display = not running and bool(self.token)

    def on_token_saved(self, event: TokenSaved) -> None:
        event.stop()
        self._replacing = False
        self.controller.token_saved()  # a tunnel not up, or signing in, starts with the new one
        self.remote_check()

    def on_ngrok_looked_for(self, event: NgrokLookedFor) -> None:
        event.stop()
        self.remote_check()

    # --- paint from the controller ----------------------------------------------------------

    @while_running
    def remote_tick(self) -> None:
        """The one-second tick: the link to copy once it arrives, and a paint while shown."""
        self._copy_when_public()
        if remote_shown(self):
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
            self.remote_check()  # back to Off: look again
        self._was_running = running
        self.query_one("#settings-remote-off").display = not running
        self.query_one("#settings-remote-on-head").display = running
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
        self._paint_needs(running)
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
        if not collapsed and remote_shown(self):  # the devices are read only where they show
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

    @on(Button.Pressed, "#settings-remote-copy")
    def _copy_link(self, event: Button.Pressed) -> None:
        event.stop()
        url = self.controller.link_url()
        if url is not None:
            copy_and_tell(self.app, url, "Link copied", sent="the link")

    @on(Button.Pressed, "#settings-remote-change-token")
    def _change_token(self, event: Button.Pressed) -> None:
        """Open the token slot to replace the token there is (or close it again)."""
        event.stop()
        self._replacing = not self._replacing
        self.repaint()
        if self._replacing:
            field = self.query_one("#settings-remote-token-field", Input)
            field.focus()
            field.scroll_visible(animate=False)

    @on(Button.Pressed, "#settings-remote-regen")
    def _regenerate(self, event: Button.Pressed) -> None:
        event.stop()
        self.controller.regenerate_password(wait=False)  # said once done (on_done)
        self.repaint()

    @on(Button.Pressed, "#settings-remote-revoke")
    def _remote_revoke(self, event: Button.Pressed) -> None:
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


class TokenSaved(Message):
    """A pasted ngrok token was saved: the section looks again at what Remote needs."""


class NgrokLookedFor(Message):
    """:meth:`NgrokMissing.fix` ran: the section looks again."""


class TokenSlot(Vertical):
    """*Paste your ngrok token*: the field, where a free one comes from, *Save*, and why a
    token was refused, beside the field. The section shows it while no token is configured,
    in the place of *Turn on remote control*, and as *Replace your ngrok token* while ngrok
    refuses the one there is (:meth:`RemoteSection._paint_needs`), with a line saying so
    where ``NGROK_AUTHTOKEN`` overrides what it saves.

    *Save* (or Enter) hands the field to :func:`store_ngrok_token` on a worker; the field
    hides what is typed, and the token is said nowhere.
    """

    DEFAULT_CSS = """
    TokenSlot { height: auto; margin: 1 0 0 0; }
    TokenSlot #settings-remote-token-title { height: 1; text-style: bold; }
    TokenSlot .remote-token-about { height: auto; color: $text-muted; }
    TokenSlot #settings-remote-token-row { height: auto; margin: 1 0 0 0; }
    TokenSlot #settings-remote-token-field { width: 1fr; max-width: 60; }
    TokenSlot #settings-remote-token-row Button { margin: 0 0 0 2; }
    TokenSlot #settings-remote-token-problem { height: auto; color: $error; }
    TokenSlot #settings-remote-token-env { height: auto; color: $warning; }
    """

    def compose(self) -> ComposeResult:
        yield Static(Text(TOKEN_TITLE), id="settings-remote-token-title")
        yield Static(Text(TOKEN_ABOUT), classes="remote-token-about")
        yield Static(Text(ENV_TOKEN), id="settings-remote-token-env")
        yield Static(
            Text.assemble("A free one: ", (AUTHTOKEN_FROM, f"link {AUTHTOKEN_FROM}")),
            id="settings-remote-token-link",
            classes="remote-token-about",
        )
        with Horizontal(id="settings-remote-token-row"):
            yield Input(
                placeholder="your ngrok token",
                password=True,
                id="settings-remote-token-field",
                compact=True,
            )
            yield Button("Save", id="settings-remote-token-save", variant="primary", compact=True)
            yield Button("Get one", id="settings-remote-token-open", compact=True)
        yield Static("", id="settings-remote-token-problem")

    def on_mount(self) -> None:
        self.query_one("#settings-remote-token-problem").display = False
        self.query_one("#settings-remote-token-env").display = False

    def remote_paint(self, *, replacing: bool, env_token: bool) -> None:
        """*Replace your ngrok token* where there is one, else *Paste…*; the line saying that
        ``NGROK_AUTHTOKEN`` wins over what is saved here, while it is set."""
        title = REPLACE_TITLE if replacing else TOKEN_TITLE
        shown = self.query_one("#settings-remote-token-title", Static)
        if str(shown.content) != title:
            shown.update(Text(title))
        self.query_one("#settings-remote-token-env").display = env_token

    @on(Button.Pressed, "#settings-remote-token-open")
    def _remote_open_dashboard(self, event: Button.Pressed) -> None:
        event.stop()
        self.app.open_url(AUTHTOKEN_FROM)

    @on(Button.Pressed, "#settings-remote-token-save")
    def _save_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.remote_save_token()

    @on(Input.Submitted, "#settings-remote-token-field")
    def _save_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.remote_save_token()

    def remote_save_token(self) -> None:
        """Save what the field holds, off the UI thread; one save at a time."""
        if any(
            worker.node is self and worker.name == TOKEN_WORKER and not worker.is_finished
            for worker in self.workers
        ):
            return
        token = self.query_one("#settings-remote-token-field", Input).value
        self.query_one("#settings-remote-token-save", Button).disabled = True
        self.run_worker(
            lambda: store_ngrok_token(token),
            name=TOKEN_WORKER,
            group=TOKEN_WORKER,
            thread=True,
            exit_on_error=False,
        )

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name != TOKEN_WORKER or not event.worker.is_finished:
            return
        event.stop()
        self.query_one("#settings-remote-token-save", Button).disabled = False
        problem = self.query_one("#settings-remote-token-problem", Static)
        error = event.worker.error
        if event.state is WorkerState.SUCCESS:
            self.query_one("#settings-remote-token-field", Input).value = ""
            problem.display = False
            self.notify("✓ ngrok token saved", timeout=5, markup=False)
            self.post_message(TokenSaved())
            return
        if isinstance(error, TokenRefused):
            said = str(error)
        elif event.state is WorkerState.ERROR:  # never the token: only what kind of failure
            said = f"the token could not be saved: {type(error).__name__}"
        else:
            return
        problem.update(Text(said))
        problem.display = True


class NgrokMissing(Vertical):
    """The one line an install whose ngrok is missing gets, with its fix in the app: never a
    command to run in a terminal. Only a damaged install gets it: the bundled ngrok is a core
    dependency (#34). :meth:`fix` looks again, for an ngrok put back meanwhile."""

    DEFAULT_CSS = """
    NgrokMissing { height: auto; margin: 1 0 0 0; }
    NgrokMissing #settings-remote-ngrok-line { height: auto; color: $warning; }
    NgrokMissing Horizontal { height: auto; margin: 1 0 0 0; }
    """

    def compose(self) -> ComposeResult:
        yield Static(Text(f"✗ {NGROK_MISSING}"), id="settings-remote-ngrok-line")
        with Horizontal():
            yield Button("Check again", id="settings-remote-ngrok-fix", compact=True)

    @on(Button.Pressed, "#settings-remote-ngrok-fix")
    def _fix_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.fix()

    def fix(self) -> None:
        """The in-app fix for a missing ngrok, then a look again."""
        self.post_message(NgrokLookedFor())


class RemoteChip(Static):
    """``📡 remote · N devices`` in the sidebar while this asq's Remote is on; hidden while it
    is off. A click opens the ``R`` modal.

    A ``Static``, not one of the sidebar's ``Activatable`` rows: never under the keyboard
    cursor, and none of its names is the sidebar's (``tests/test_remote_names_stay_off_the_
    hook_graph.py``: a remote module shares no name with the rest)."""

    DEFAULT_CSS = """
    RemoteChip { height: 1; color: $success; text-wrap: nowrap; text-overflow: ellipsis; }
    """

    ALLOW_SELECT = False

    def __init__(self, controller: RemoteController, *, id: str | None = None) -> None:
        super().__init__(Text(""), id=id)
        self.controller = controller
        self.tooltip = "Remote control is on — open it (R)"

    def on_mount(self) -> None:
        self.display = False
        self.remote_tick()
        self.set_interval(CHIP_TICK_S, self.remote_tick)

    @while_running
    def remote_tick(self) -> None:
        controller = self.controller
        self.display = controller.running
        if not controller.running:
            return
        signed_in = sum(1 for device in controller.devices() if device.get("signed_in"))
        noun = "device" if signed_in == 1 else "devices"
        self.update(Text(f"📡 remote · {signed_in} {noun}"))

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.app.push_screen(RemotePanel(self.controller))
