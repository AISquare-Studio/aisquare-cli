"""Start a terminal teammate using the shared fleet service."""

from __future__ import annotations

import asyncio
from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static

from aisquare.core.agent_adapters import adapters
from aisquare.models import ProjectInfo
from aisquare.services import fleet


class SpawnScreen(ModalScreen[fleet.SpawnReceipt | None]):
    DEFAULT_CSS = """
    SpawnScreen { align: center middle; }
    SpawnScreen > Vertical {
        width: 64; height: auto; padding: 1 2; border: round $accent; background: $surface;
    }
    SpawnScreen Select, SpawnScreen Input { margin-bottom: 1; }
    SpawnScreen #spawn-error { color: $error; height: auto; }
    """
    BINDINGS: ClassVar = [("escape", "cancel", "Cancel")]

    def __init__(self, project: ProjectInfo) -> None:
        super().__init__()
        self.project = project
        self.busy = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"Start a teammate in {self.project.root.name}")
            yield Select(
                [(role, role) for role in fleet.FLEET_ROLES],
                value="coder",
                allow_blank=False,
                id="spawn-role",
            )
            yield Select(
                [("Use role / project default", ""), *((a.label, a.id) for a in adapters())],
                value="",
                allow_blank=False,
                id="spawn-family",
            )
            yield Input(placeholder="Label (optional)", id="spawn-label")
            yield Input(placeholder="Initial task (optional)", id="spawn-prompt")
            yield Static("", id="spawn-error", markup=False)
            yield Button("Start agent", variant="primary", id="spawn-submit")
            yield Button("Cancel", id="spawn-cancel")

    def action_cancel(self) -> None:
        if not self.busy:
            self.dismiss(None)

    @on(Button.Pressed, "#spawn-cancel")
    def cancel(self) -> None:
        self.action_cancel()

    @on(Button.Pressed, "#spawn-submit")
    async def submit(self) -> None:
        if self.busy:
            return
        role = str(self.query_one("#spawn-role", Select).value)
        family = str(self.query_one("#spawn-family", Select).value) or None
        label = self.query_one("#spawn-label", Input).value.strip() or None
        prompt = self.query_one("#spawn-prompt", Input).value.strip() or None
        self.busy = True
        self.query_one("#spawn-submit", Button).disabled = True
        try:
            receipt = await asyncio.to_thread(
                fleet.spawn, self.project, role, agent=family, label=label, prompt=prompt
            )
        except Exception as exc:
            self.query_one("#spawn-error", Static).update(str(exc))
        else:
            self.dismiss(receipt)
        finally:
            self.busy = False
            self.query_one("#spawn-submit", Button).disabled = False
