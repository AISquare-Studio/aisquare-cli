"""Local persona controls. Input here never reaches a working agent's terminal."""

from __future__ import annotations

import json
import shlex
from typing import ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.suggester import SuggestFromList
from textual.widgets import Button, Input, Static, TextArea
from textual.worker import Worker, WorkerState

from aisquare.core.personas import PersonaPack
from aisquare.models import ProjectInfo
from aisquare.services import personas


class PersonaEditor(ModalScreen[None]):
    """Edit a local pack, including starter wording created from a description."""

    CSS = """
    PersonaEditor { align: center middle; }
    PersonaEditor #persona-editor-box { width: 90%; height: 90%; background: $surface;
        border: heavy $accent; padding: 1; }
    PersonaEditor #persona-json { height: 1fr; }
    PersonaEditor .editor-actions { height: auto; }
    PersonaEditor #editor-error { height: auto; color: $error; }
    """
    BINDINGS: ClassVar = [("escape", "cancel", "cancel")]

    def __init__(self, pack: PersonaPack) -> None:
        super().__init__()
        self.pack = pack

    def compose(self) -> ComposeResult:
        with Vertical(id="persona-editor-box"):
            yield Static(
                "Local character editor — edit message patterns, then save. A description alone "
                "does not generate a voice. Starter wording remains until you edit it."
            )
            yield TextArea(self.pack.model_dump_json(indent=2), id="persona-json")
            yield Static("", id="editor-error", markup=False)
            with Horizontal(classes="editor-actions"):
                yield Button("Save character", id="save-persona", variant="primary")
                yield Button("Cancel", id="cancel-persona")

    @on(Button.Pressed, "#save-persona")
    def save_pack(self) -> None:
        try:
            saved = personas.save_draft(self.query_one("#persona-json", TextArea).text)
        except (ValueError, OSError) as exc:
            self.query_one("#editor-error", Static).update(str(exc))
            return
        self.notify(saved.message + " Choose Use to activate it.", markup=False)
        self.dismiss(None)

    @on(Button.Pressed, "#cancel-persona")
    def action_cancel(self) -> None:
        self.dismiss(None)


class PersonaScreen(ModalScreen[None]):
    """One parser powers the shell, slash commands, and these picker buttons."""

    CSS = """
    PersonaScreen { align: center middle; }
    PersonaScreen #persona-box { width: 80%; max-width: 92; height: auto; max-height: 90%;
        background: $surface; border: heavy $accent; padding: 1; }
    PersonaScreen .persona-actions { height: auto; }
    PersonaScreen .persona-actions Button { min-width: 8; }
    PersonaScreen #persona-output-wrap { height: 1fr; min-height: 6; border: solid $primary;
        padding: 0 1; }
    PersonaScreen #persona-output { height: auto; }
    PersonaScreen #persona-help { height: auto; color: $text-muted; }
    """
    BINDINGS: ClassVar = [("escape", "close", "close")]

    def __init__(self, project: ProjectInfo | None = None, *, role: str | None = None) -> None:
        super().__init__()
        self.project = project
        self.role = role
        self._busy = False

    def compose(self) -> ComposeResult:
        with Vertical(id="persona-box"):
            name = self.project.root.name if self.project is not None else "global defaults"
            yield Static(f"Persona · {name}", markup=False)
            yield Static(
                "Type one command and press Enter (or Run). The usual ones:\n"
                "  /persona list                  the styles you can use\n"
                "  /persona use answer-first      switch to a style\n"
                "  /persona voice on              let the agents talk in it  (off = plain)\n"
                "  /persona off                   stop styling this project\n"
                "With voice on this changes how the agents WORD replies — never the facts, "
                "the board records, or their work. Press Right to accept a suggestion.",
                id="persona-help",
                markup=False,
            )
            yield Input(
                self.role or "",
                placeholder="/persona use answer-first",
                id="persona-command",
            )
            with Horizontal(classes="persona-actions"):
                yield Button("Run", id="persona-run", variant="primary")
                yield Button("Close", id="persona-close")
            with VerticalScroll(id="persona-output-wrap"):
                yield Static("", id="persona-output", markup=False)

    def on_mount(self) -> None:
        self.refresh_packs()
        self.dispatch_persona_command("/persona status")
        self.query_one("#persona-command", Input).focus()

    def refresh_packs(self) -> None:
        """Rebuild the command box's autocomplete from the packs on disk."""
        try:
            packs = personas.list_packs()
            self.query_one("#persona-command", Input).suggester = SuggestFromList(
                [
                    "/persona status",
                    "/persona list",
                    "/persona off",
                    "/persona reset",
                    "/persona voice on",
                    "/persona voice off",
                    "/persona add ",
                    "/persona add --name ",
                    "/persona add --url https://",
                    *[f"/persona use {p.reference}" for p in packs],
                    *[f"/persona preview {p.reference}" for p in packs],
                    *[f"/persona edit {p.reference}" for p in packs],
                    *[f"/persona export {p.reference} --output " for p in packs],
                ]
            )
        except (ValueError, OSError) as exc:
            self.show_output(str(exc))

    def show_output(self, text: str) -> None:
        self.query_one("#persona-output", Static).update(Text(text))

    @on(Input.Submitted, "#persona-command")
    def submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.dispatch_persona_command(event.value)

    def dispatch_persona_command(self, command: str) -> None:
        if self._busy:
            return
        if not command.strip():
            command = "/persona"
        try:
            words = shlex.split(command)
            action = words[1] if len(words) > 1 and words[0] == "/persona" else "picker"
            scoped = any(word == "--global" or word.startswith("--project") for word in words)
            if (
                self.project is None
                and not scoped
                and action in {"picker", "status", "use", "off", "reset"}
            ):
                command += " --global"
        except ValueError as exc:
            self.show_output(str(exc))
            return
        self._busy = True
        self.show_output("Working locally…")
        self.run_worker(
            lambda: personas.run_persona_command(command, self.project),
            name="persona-command",
            thread=True,
            exit_on_error=False,
        )

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "persona-close":
            self.action_close()
        elif event.button.id == "persona-run":
            self.dispatch_persona_command(self.query_one("#persona-command", Input).value)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name != "persona-command":
            return
        event.stop()
        if event.state is WorkerState.SUCCESS:
            receipt = event.worker.result
            if isinstance(receipt, personas.PersonaReceipt):
                details = json.dumps(receipt.data, ensure_ascii=False, indent=2)
                self.show_output(receipt.message + ("\n" + details if receipt.data else ""))
                if receipt.editor is not None:
                    self.app.push_screen(
                        PersonaEditor(receipt.editor), lambda _: self.refresh_packs()
                    )
                self.refresh_packs()
        elif event.state is WorkerState.ERROR:
            self.show_output(str(event.worker.error))
        if event.state in (WorkerState.SUCCESS, WorkerState.ERROR, WorkerState.CANCELLED):
            self._busy = False

    def action_close(self) -> None:
        if self._busy:
            self.show_output("Please wait for the current import or command to finish.")
            return
        self.dismiss(None)
