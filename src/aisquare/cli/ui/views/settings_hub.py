"""The Settings hub: what *⚙ Settings* in the sidebar opens.

Three links, each a button with one line saying what is behind it — *App
settings* (the machine-wide settings, ``views.app_settings``), *Accounts* (the
AISquare sign-in and the Claude Code accounts, ``views.accounts``) and *Doctor*
(the checks, which opening it runs). A link posts the message the app routes;
the pages it opens keep *⚙ Settings* highlighted in the sidebar. ↑/↓ move between
the links, Enter or a click opens one.
"""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Button, Static

from aisquare.cli.ui.sidebar import (
    SETTINGS_ICON,
    AccountsSelected,
    AppSettingsSelected,
    DoctorSelected,
)

LINKS: tuple[tuple[str, str, str], ...] = (
    (
        "app-settings",
        "App settings",
        "the machine-wide settings: roles, the fleet, accounts' rules",
    ),
    ("accounts", "Accounts", "the AISquare sign-in and the Claude Code accounts"),
    ("doctor", "Doctor", "the checks — they run when the page opens"),
)
"""The hub's links, top to bottom: each one's id stem, label and line of description."""


class SettingsHubView(Vertical):
    """Settings: a title, then one link per :data:`LINKS` with what it opens."""

    DEFAULT_CSS = """
    SettingsHubView { padding: 1 2; }
    SettingsHubView #settings-title { height: 1; text-style: bold; margin-bottom: 1; }
    SettingsHubView .hub-link { width: 18; margin: 0 2 0 0; }
    SettingsHubView .hub-row { height: auto; margin-bottom: 1; }
    SettingsHubView .hub-about { width: 1fr; padding: 0 0; color: $text-muted; }
    """

    BINDINGS: ClassVar = [
        Binding("down", "step(1)", "next", show=False),
        Binding("up", "step(-1)", "previous", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Static(Text(f"{SETTINGS_ICON} Settings"), id="settings-title")
        for stem, label, about in LINKS:
            with Vertical(classes="hub-row"):
                yield Button(label, id=f"hub-{stem}", classes="hub-link", compact=True)
                yield Static(Text(about), classes="hub-about")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        stem = (event.button.id or "").removeprefix("hub-")
        message: Message | None = {
            "app-settings": AppSettingsSelected(),
            "accounts": AccountsSelected(),
            "doctor": DoctorSelected(None),
        }.get(stem)
        if message is not None:
            event.stop()
            self.post_message(message)

    def action_step(self, delta: int) -> None:
        """↑/↓: the link above or below the focused one, stopping at the ends."""
        links = list(self.query(".hub-link").results(Button))
        focused = self.app.focused
        if isinstance(focused, Button) and focused in links:
            at = links.index(focused)
        else:
            at = -1 if delta > 0 else len(links)
        links[max(0, min(len(links) - 1, at + delta))].focus()

    def focus_first(self) -> None:
        """Put the keyboard on the first link, as the page opens from the keyboard."""
        self.query_one(f"#hub-{LINKS[0][0]}", Button).focus()
