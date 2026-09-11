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
from textual.widgets import Button, Input, Select, Static, TextArea
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
    PersonaScreen #persona-box { width: 90%; max-width: 110; height: 90%;
        background: $surface; border: heavy $accent; padding: 1; }
    PersonaScreen .persona-actions { height: auto; }
    PersonaScreen .persona-actions Button { min-width: 8; width: 1fr; }
    PersonaScreen #persona-output-wrap { height: 1fr; border: solid $primary; padding: 0 1; }
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
            yield Static(f"AI Square commands · {name}", markup=False)
            yield Static(
                "These controls change role narration only. Your agent keeps working. "
                "Original conversation and work instructions stay unchanged. "
                "Press Right at the end of a command to accept its suggestion.",
                id="persona-help",
            )
            yield Select[str]([], prompt="Choose a character", id="persona-pack")
            yield Input(
                self.role or "", placeholder="Role (blank = whole project)", id="persona-role"
            )
            with Horizontal(classes="persona-actions"):
                yield Button("Use", id="persona-use", variant="primary")
                yield Button("Preview", id="persona-preview")
                yield Button("Off", id="persona-off")
                yield Button("Reset role", id="persona-reset")
                yield Button("Edit", id="persona-edit")
            yield Input(
                placeholder="/persona add ./character.json · /persona use studio",
                id="persona-command",
            )
            with Horizontal(classes="persona-actions"):
                yield Button("Run command", id="persona-run")
                yield Button("Refresh", id="persona-refresh")
                yield Button("Close", id="persona-close")
            with VerticalScroll(id="persona-output-wrap"):
                yield Static("", id="persona-output", markup=False)

    def on_mount(self) -> None:
        self.refresh_packs()
        self.dispatch_persona_command("/persona status")
        self.query_one("#persona-command", Input).focus()

    def refresh_packs(self) -> None:
        try:
            packs = personas.list_packs()
            picker = self.query_one("#persona-pack", Select)
            previous = picker.value
            picker.set_options([(f"{p.name} ({p.reference})", p.reference) for p in packs])
            self.query_one("#persona-command", Input).suggester = SuggestFromList(
                [
                    "/persona status",
                    "/persona list",
                    "/persona off",
                    "/persona reset",
                    "/persona add ",
                    "/persona add --name ",
                    "/persona add --url https://",
                    *[f"/persona use {p.reference}" for p in packs],
                    *[f"/persona preview {p.reference}" for p in packs],
                    *[f"/persona edit {p.reference}" for p in packs],
                    *[f"/persona export {p.reference} --output " for p in packs],
                ]
            )
            choices = {p.reference for p in packs}
            if previous in choices:
                picker.value = previous
            elif packs:
                picker.value = packs[0].reference
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
            if (
                self.project is None
                and "--global" not in words
                and "--project" not in words
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
        action = event.button.id
        if action == "persona-close":
            self.action_close()
            return
        if action == "persona-refresh":
            self.refresh_packs()
            self.dispatch_persona_command("/persona status")
            return
        if action == "persona-run":
            self.dispatch_persona_command(self.query_one("#persona-command", Input).value)
            return
        selected = self.query_one("#persona-pack", Select).value
        role = self.query_one("#persona-role", Input).value.strip()
        if action == "persona-reset" and not role:
            self.show_output(
                "Enter the role to reset. Use /persona reset explicitly to reset the whole project."
            )
            return
        words = ["/persona", (action or "").removeprefix("persona-")]
        if action in ("persona-use", "persona-preview", "persona-edit"):
            if not isinstance(selected, str):
                self.show_output("Choose a character first.")
                return
            words.append(selected)
        if role and action in ("persona-use", "persona-off", "persona-reset", "persona-preview"):
            words += ["--role", role]
        self.dispatch_persona_command(shlex.join(words))

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
