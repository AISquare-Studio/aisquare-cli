"""The Settings hub: what *⚙ Settings* in the sidebar opens.

Seven links, each a button with one line saying what is behind it — *App
settings* (the machine-wide settings, ``views.app_settings``), *Remote control* and
*Captain* (App settings again, on its *Remote* tab and on the *Agent settings* tab the
captain's box tops: one obvious way in to each, #41), *Accounts* (the AISquare sign-in
and the Claude Code accounts, ``views.accounts``), *Usage & stats* (what the Claude Code
sessions on this machine spent, ``views.usage_stats``), *Agent resources* (what each live
agent consumes, ``views.resources``) and *Doctor* (the checks, which opening it runs). A
link posts the message the app routes; the pages it opens keep *⚙ Settings* highlighted in
the sidebar. ↑/↓ move between the links, Enter or a click opens one.

Under them, *Fleet*: *Restart all…* and *Stop all…*, for every running agent of every
project, and *Pause all…* and *Resume all…*, ``fleet pause --all`` and ``fleet resume --all``
(``views.fleet_bulk``). Each asks first, in a dialog that lists who. The action then runs off
the UI thread, with a toast per agent as it is done and one that sums it up. Every button is
greyed while it runs.

Remote control and the Captain box were sections of this page (#33). They live in App
settings' tabs now, in its format, where the owner placed them (#41).
"""

from __future__ import annotations

import contextlib
from typing import ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Button, Static
from textual.worker import Worker, WorkerState

from aisquare.cli.ui.click_guard import GuardedButton
from aisquare.cli.ui.sidebar import (
    SETTINGS_ICON,
    AccountsSelected,
    AppSettingsSelected,
    DoctorSelected,
    ResourcesSelected,
    UsageStatsSelected,
    doctor_badge,
)
from aisquare.cli.ui.views.app_settings import AGENT_TAB, REMOTE_TAB
from aisquare.cli.ui.views.fleet_bulk import (
    BULK_WORKER,
    ConfirmBulk,
    agent_toast,
    bulk_running,
    report_toast,
)
from aisquare.services import fleet as fleet_service

LINKS: tuple[tuple[str, str, str], ...] = (
    (
        "app-settings",
        "App settings",
        "agent settings (roles, the fleet, accounts' rules) and your preferences",
    ),
    ("remote", "Remote control", "the fleet on your phone: a link, its QR code and a password"),
    ("captain", "Captain", "the experimental captain, on or off (App settings ▸ Agent settings)"),
    ("accounts", "Accounts", "the AISquare sign-in and the Claude Code accounts"),
    (
        "usage-stats",
        "Usage & stats",
        "tokens, cost, repositories and accounts across all your Claude Code sessions",
    ),
    (
        "resources",
        "Agent resources",
        "CPU, memory, processes and tokens of every running agent, live while open",
    ),
    ("doctor", "Doctor", "the checks — they run when the page opens"),
)
"""The hub's links, top to bottom: each one's id stem, label and line of description."""

FLEET_ACTIONS: tuple[tuple[fleet_service.BulkAction, str], ...] = (
    ("restart", "Restart all…"),
    ("stop", "Stop all…"),
    ("pause", "Pause all…"),
    ("resume", "Resume all…"),
)
"""The Fleet section's buttons, ``#fleet-<action>-all``: each action and its label."""
FLEET_ABOUT = (
    "every running agent in every project, or the managers alone; exited (💤) agents are "
    "left as they are. Pause keeps each agent's claim, worktree and session, and Resume "
    "brings back exactly the paused ones"
)


class SettingsHubView(VerticalScroll, can_focus=False):
    """Settings: a title, then one link per :data:`LINKS` with what it opens."""

    DEFAULT_CSS = """
    SettingsHubView { padding: 1 2; }
    SettingsHubView #settings-title { height: 1; text-style: bold; margin-bottom: 1; }
    SettingsHubView .hub-link { width: 18; margin: 0 2 0 0; }
    SettingsHubView .hub-row { height: auto; margin-bottom: 1; }
    SettingsHubView .hub-about { width: 1fr; padding: 0 0; color: $text-muted; }
    SettingsHubView #hub-fleet-title { height: 1; text-style: bold; margin: 1 0 1 0; }
    SettingsHubView #hub-fleet-buttons { height: auto; }
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
                yield Static(Text(about), classes="hub-about", id=f"hub-{stem}-about")
        yield Static(Text("Fleet"), id="hub-fleet-title")
        with Vertical(classes="hub-row"):
            with Horizontal(id="hub-fleet-buttons"):
                for action, label in FLEET_ACTIONS:
                    yield GuardedButton(
                        label,
                        id=f"fleet-{action}-all",
                        classes="hub-link fleet-action",
                        compact=True,
                        variant="error" if action == "stop" else "default",
                    )
            yield Static(Text(FLEET_ABOUT), classes="hub-about", id="hub-fleet-about")

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._action: fleet_service.BulkAction = "restart"
        """What the Fleet worker running (or last run) does: its error toast's verb."""

    def on_mount(self) -> None:
        self._paint_fleet()

    def show_findings(self, failing: int, warning: int) -> None:
        """What the newest doctor run found, under the Doctor link; nothing when all clear."""
        about = Text(next(line for stem, _, line in LINKS if stem == "doctor"))
        badge = doctor_badge(failing, warning)
        if badge:
            about.append(f" · the last run found {badge}")
        self.query_one("#hub-doctor-about", Static).update(about)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        stem = (event.button.id or "").removeprefix("hub-")
        message: Message | None = {
            "app-settings": AppSettingsSelected(),
            "remote": AppSettingsSelected(REMOTE_TAB),
            "captain": AppSettingsSelected(AGENT_TAB),
            "accounts": AccountsSelected(),
            "usage-stats": UsageStatsSelected(),
            "resources": ResourcesSelected(),
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

    # --- Fleet: every running agent at once ------------------------------------------

    @property
    def bulk_running(self) -> bool:
        """Whether a restart, stop, pause or resume of many agents is still running: this page's,
        or a card's *Pause agents* the shell runs (:func:`~.fleet_bulk.bulk_running`)."""
        return bulk_running(self.app)

    def _paint_fleet(self) -> None:
        """Every Fleet button greyed while any action runs: one at a time, never two."""
        busy = self.bulk_running
        for action, _ in FLEET_ACTIONS:
            self.query_one(f"#fleet-{action}-all", Button).disabled = busy

    @on(Button.Pressed, ".fleet-action")
    def _fleet_action(self, event: Button.Pressed) -> None:
        event.stop()
        wanted = (event.button.id or "").removeprefix("fleet-").removesuffix("-all")
        action = next((action for action, _ in FLEET_ACTIONS if action == wanted), None)
        if action is not None:
            self.ask(action)

    def ask(self, action: fleet_service.BulkAction) -> None:
        """The dialog for ``action``; on confirm, the action on what it showed."""
        if self.bulk_running:
            self.notify(
                "agents are being restarted, stopped, paused or resumed — wait for that first",
                severity="warning",
                timeout=5,
                markup=False,
            )
            return

        def confirmed(plan: fleet_service.BulkPlan | None) -> None:
            if plan is not None and plan.acts:
                self._run_bulk(plan)

        self.app.push_screen(ConfirmBulk(action), confirmed)

    def _run_bulk(self, plan: fleet_service.BulkPlan) -> None:
        """``fleet_service.bulk`` on the plan the dialog showed, off the UI thread.

        Each receipt comes back onto the UI thread as it is made (:meth:`_agent_done`). A
        toast that cannot be shown (asq quitting) costs the toast, never the agents after
        it: the service raising out of ``on_receipt`` would end the loop there.
        """
        app, action = self.app, plan.action
        self._action = action

        def tell(receipt: fleet_service.BulkReceipt) -> None:
            with contextlib.suppress(Exception):
                app.call_from_thread(agent_toast, self, receipt, action)

        self.run_worker(
            lambda: fleet_service.bulk(
                action, managers_only=plan.managers_only, shown=plan, on_receipt=tell
            ),
            name=BULK_WORKER,
            group=BULK_WORKER,
            thread=True,
            exit_on_error=False,
        )
        self._paint_fleet()

    @on(Worker.StateChanged)
    def _bulk_done(self, event: Worker.StateChanged) -> None:
        if event.worker.name != BULK_WORKER:
            return
        if event.state not in (WorkerState.SUCCESS, WorkerState.ERROR, WorkerState.CANCELLED):
            return
        report_toast(self, event, self._action)
        self._paint_fleet()
        refresh = getattr(self.app, "refresh_data", None)
        if callable(refresh):
            refresh()
