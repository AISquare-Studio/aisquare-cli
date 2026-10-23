"""The ⬆ row's dialog: update aisquare to the release the daily check found, or say how.

It opens only when the row is clicked or activated, never on its own: a dialog that rose
by itself would take the keys the owner is typing into a pane. *Restart & update* answers
``True`` and the shell hands the terminal to ``aisquare upgrade --one-click``, the path
Doctor's Update takes (``FleetApp.on_doctor_view_hand_off``). Cancel or Escape answers
``False`` and nothing changes.

An install asq does not update itself (Homebrew, pip, pipx) gets the command that does,
from the same plan ``aisquare upgrade`` builds (:attr:`UpgradePlan.command`,
:attr:`UpgradePlan.reason`), and a Close button: there is nothing to restart into.

Every string reaches the screen as a :class:`rich.text.Text` built with ``append``: a
command or a Remote's name is data, never markup.
"""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from aisquare.services.lifecycle import UpgradePlan

PLAN_WORKER = "update-plan"
"""The read the row starts before the dialog opens: how this install upgrades, and whether
Remote is on in another process (:func:`~aisquare.services.lifecycle.upgrade_plan`)."""
REATTACH_WORKER = "update-reattach"
"""The reopened asq's look at the agents the press recorded, restarting any that are gone."""
CHECK_WORKER = "update-check"
"""The daily release check (``services.updates.check``)."""


def agents_line(live: int) -> str:
    """What the update does to the ``live`` agents: nothing, and what happens if one is gone."""
    if live == 0:
        return "No agent is running."
    if live == 1:
        return (
            "1 agent keeps running; if its pane is gone afterwards, it is restarted on its session."
        )
    return (
        f"{live} agents keep running; any whose pane is gone afterwards is restarted on its "
        "session."
    )


def remote_line(plan: UpgradePlan) -> str | None:
    """:attr:`UpgradePlan.remote_warning`, short: a Remote on in another process."""
    if plan.remote is None:
        return None
    return (
        f"⚠ Remote is on ({plan.remote}): it can fail while the install is replaced, and runs "
        f"{plan.current} until it is started again."
    )


class UpdateScreen(ModalScreen[bool]):
    """Update aisquare ``plan.current`` → ``version``: Restart & update, or Cancel; Esc cancels."""

    DEFAULT_CSS = """
    UpdateScreen { align: center middle; }
    UpdateScreen > Vertical { width: 52; max-width: 92%; height: auto; border: heavy $accent;
                              border-title-color: $text; background: $surface; padding: 0 1; }
    UpdateScreen .update-text { height: auto; }
    UpdateScreen #update-buttons { height: auto; align-horizontal: center; padding: 1 0 0 0; }
    UpdateScreen #update-buttons Button { margin: 0 1; }
    """
    BINDINGS: ClassVar = [Binding("escape", "cancel", "cancel")]

    def __init__(self, plan: UpgradePlan, version: str, live: int) -> None:
        super().__init__()
        self.plan = plan
        self.version = version
        """The release the row offered: the newest the daily check found."""
        self.live = live
        """How many agents are alive at the moment the dialog opened."""

    def body(self) -> Text:
        """What the dialog says, above its buttons."""
        text = Text()
        if not self.plan.runnable:
            text.append(f"asq does not update this install itself ({self.plan.reason}).")
            if self.plan.command:
                text.append(" Update it with:\n\n")
                text.append(self.plan.command, style="bold")
                text.append("\n\nthen start asq again. The agents keep running.")
            return text
        text.append(f"asq closes, installs {self.version} and reopens here.\n")
        text.append(agents_line(self.live))
        remote = remote_line(self.plan)
        if remote is not None:
            text.append("\n")
            text.append(remote, style="yellow")
        return text

    def compose(self) -> ComposeResult:
        box = Vertical(id="update-box")
        box.border_title = f"Update aisquare {self.plan.current} → {self.version}"
        with box:
            yield Static(self.body(), id="update-text", classes="update-text")
            with Horizontal(id="update-buttons"):
                if self.plan.runnable:
                    yield Button("Restart & update", id="update-confirm", variant="primary")
                    yield Button("Cancel", id="update-cancel")
                else:
                    yield Button("Close", id="update-cancel")

    def on_mount(self) -> None:
        self.query_one("#update-cancel", Button).focus()

    @on(Button.Pressed, "#update-confirm")
    def _confirm(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(True)

    @on(Button.Pressed, "#update-cancel")
    def _cancel(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)
