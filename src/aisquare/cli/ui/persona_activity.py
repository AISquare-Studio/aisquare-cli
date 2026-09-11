"""A display-only view of saved events, outside the captured agent terminal."""

from __future__ import annotations

import json

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Collapsible, Static

from aisquare.core.store import store_session
from aisquare.models import ProjectInfo
from aisquare.services import personas
from aisquare.services import team as team_service


class PersonaActivity(Vertical):
    DEFAULT_CSS = """
    PersonaActivity { height: auto; max-height: 17; }
    PersonaActivity Collapsible { padding: 0; }
    PersonaActivity .persona-tools { height: 3; }
    PersonaActivity .persona-tools Button { min-width: 12; }
    PersonaActivity .persona-events-wrap { height: auto; max-height: 9; }
    PersonaActivity .persona-events { height: auto; padding: 0 1; }
    """

    def __init__(
        self,
        project: ProjectInfo | None = None,
        *,
        project_id: str | None = None,
        role: str | None = None,
        session_id: str | None = None,
    ) -> None:
        super().__init__()
        self.project = project
        self.project_id = project_id
        self.role = role
        self.session_id = session_id
        self.original = ""
        self._previous = ""

    def compose(self) -> ComposeResult:
        with Collapsible(title="Role narration · original records preserved", collapsed=False):
            with Horizontal(classes="persona-tools"):
                yield Button("Persona…", classes="open-persona")
                yield Button("Copy originals", classes="copy-persona-original")
            with VerticalScroll(classes="persona-events-wrap"):
                yield Static("No activity yet.", classes="persona-events", markup=False)

    def on_mount(self) -> None:
        self.refresh_activity()
        self.set_interval(2, self.refresh_activity)

    def refresh_activity(self) -> None:
        if not self.is_mounted or not self.is_on_screen:
            return
        try:
            if self.project is None and self.project_id is not None:
                with store_session() as store:
                    self.project = store.get_project(self.project_id)
                if self.project is None:
                    return
            project, sessions, _, events = team_service.board_data(events=30, project=self.project)
            self.project = project
            roles = {session.id: session.role for session in sessions}
            if self.session_id is not None:
                events = [event for event in events if event.session_id == self.session_id]
            # Newest first: the record a person is waiting for must not sit below the fold.
            events = events[-3:][::-1]
            self.original = json.dumps([e.model_dump(mode="json") for e in events], indent=2)
            lines = [
                personas.render_event(
                    event, project, role=roles.get(event.session_id or "", self.role)
                )
                for event in events
            ]
            text = (
                "\n".join(
                    line
                    if len(line) <= 1500
                    else line[:1500]
                    + "\n[Display shortened; Copy originals contains the full record.]"
                    for line in lines
                )
                or "No activity yet."
            )
            self._show_text(text)
        except Exception as exc:
            # A damaged pack/store must never close or steal focus from an agent.
            self._show_text(f"Activity unavailable: {exc}")

    def _show_text(self, text: str) -> None:
        if text != self._previous:
            self.query_one(".persona-events", Static).update(Text(text))
            self._previous = text

    @on(Button.Pressed, ".open-persona")
    def open_personas(self, event: Button.Pressed) -> None:
        from aisquare.cli.ui.personas import PersonaScreen
        from aisquare.cli.ui.terminal import TerminalPane

        event.stop()
        # Clicking the Persona button moves focus away from the terminal before
        # opening the modal. Restore that nearby agent explicitly on dismissal;
        # a Board-only activity panel has no terminal and retains ordinary focus.
        parent = self.parent
        pane = next(iter(parent.query(TerminalPane)), None) if parent is not None else None

        def restore_terminal(_: None) -> None:
            if pane is not None and pane.is_mounted and pane.is_on_screen and pane.attached:
                pane.focus()

        self.app.push_screen(PersonaScreen(self.project, role=self.role), restore_terminal)

    @on(Button.Pressed, ".copy-persona-original")
    def copy_original(self, event: Button.Pressed) -> None:
        event.stop()
        self.app.copy_to_clipboard(self.original)
        self.notify("Original activity records copied.")
