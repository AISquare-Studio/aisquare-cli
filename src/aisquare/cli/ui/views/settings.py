"""The project's Settings tab: what belongs to this project, not to the machine.

Its NICKNAME — the name every list shows (``services.project.display_name``),
what ``aisquare project rename`` sets — is edited here: Enter or Save writes it,
and a blank field clears it back to the folder name. Its codename is shown, not
edited: it names the tmux session and the branches, and ``aisquare fleet
rename`` is where it changes. Everything machine-wide — roles, the fleet, how
launches pick an account — is App settings (``views.app_settings``).

The write opens the store, so it runs in a thread worker (:data:`NICKNAME_WORKER`)
and the tab answers when it lands: the field and the title show what the store
now holds, and :class:`~aisquare.cli.ui.sidebar.ProjectRenamed` tells the rest
of the app to show the new name. A rename made anywhere else — a card, ``project
rename`` in a shell — reaches the tab with the app's next frame
(:meth:`SettingsView.show_project`), and Save always writes what the field says:
the tab's own copy of the project is never what decides there is nothing to save.
"""

from __future__ import annotations

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import Button, Input, Label, Static
from textual.worker import Worker, WorkerState

from aisquare.cli.ui.sidebar import ProjectRenamed
from aisquare.models import ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services import project as project_service

NICKNAME_WORKER = "settings-nickname"
"""The worker the nickname is written in: it writes the store."""


def fleet_id_line(project: ProjectInfo) -> Text:
    """The read-only line naming the project's codename and what it names."""
    codename = project.codename
    if codename is None:
        return Text(
            "fleet id: none yet — the first agent started here gives the project one "
            "(it names the tmux session and the fleet/<id>/… branches)",
            style="dim",
        )
    return Text(
        f"fleet id: {codename} — tmux session {fleet_service.session_name(codename)}, "
        f"branches fleet/{codename}/…",
        style="dim",
    )


class SettingsView(VerticalScroll):
    """One project's own settings: its nickname, and its codename shown."""

    DEFAULT_CSS = """
    SettingsView { padding: 1 2; }
    SettingsView .row { height: auto; margin-bottom: 1; }
    SettingsView .row Label { width: 12; padding-top: 1; }
    SettingsView .row Input { width: 1fr; max-width: 60; }
    SettingsView .row Button { margin-left: 1; }
    SettingsView #fleet-id { height: auto; }
    SettingsView #settings-note { color: $text-muted; margin-top: 1; height: auto; }
    """

    def __init__(self, project: ProjectInfo, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.project = project

    def show_project(self, project: ProjectInfo) -> None:
        """The project as the store has it now: the title, the codename line and the field.

        The field follows only while it still shows the nickname the tab last had —
        what the operator is in the middle of typing is theirs, and Save writes it.
        """
        if project == self.project:
            return
        before = self.project.nickname or ""
        self.project = project
        try:
            field = self.query_one("#nickname", Input)
            title = self.query_one("#settings-title", Static)
            line = self.query_one("#fleet-id", Static)
        except NoMatches:  # not composed yet: compose reads self.project
            return
        if field.value.strip() == before:
            field.value = project.nickname or ""
        field.placeholder = project.root.name or project.id
        title.update(self._title())
        line.update(fleet_id_line(project))

    def _title(self) -> Text:
        return Text(
            f"project settings — {project_service.display_name(self.project)}", style="bold"
        )

    def compose(self) -> ComposeResult:
        yield Static(self._title(), id="settings-title")
        with Horizontal(classes="row"):
            yield Label("nickname")
            yield Input(
                value=self.project.nickname or "",
                placeholder=self.project.root.name or self.project.id,
                max_length=project_service.NICKNAME_MAX,
                id="nickname",
            )
            yield Button("Save", id="save-nickname", variant="primary")
        yield Static(fleet_id_line(self.project), id="fleet-id")
        yield Static(
            Text(
                "The nickname is the name every list shows; blank shows the folder name. "
                "`aisquare project rename` sets the same. The settings for every project — "
                "roles, the fleet, accounts — are in App settings."
            ),
            id="settings-note",
        )

    # --- the nickname --------------------------------------------------------------------

    @on(Input.Submitted, "#nickname")
    @on(Button.Pressed, "#save-nickname")
    def _save_nickname(self) -> None:
        """Write the field as the nickname, off the UI thread; blank clears it.

        Always written, never compared with the tab's copy of the project first: a
        rename made elsewhere may not have reached it, and writing the same name
        again changes nothing.
        """
        wanted = self.query_one("#nickname", Input).value.strip()
        project = self.project
        self.run_worker(
            lambda: project_service.set_nickname(project, wanted or None),
            name=NICKNAME_WORKER,
            group=NICKNAME_WORKER,
            exclusive=True,
            thread=True,
            exit_on_error=False,
        )

    @on(Worker.StateChanged)
    def _nickname_saved(self, event: Worker.StateChanged) -> None:
        if event.worker.name != NICKNAME_WORKER:
            return
        if event.state is WorkerState.ERROR:
            error = event.worker.error
            if isinstance(error, KeyError):
                reason = "the project is no longer registered"
            elif isinstance(error, ValueError):
                reason = str(error)
            else:
                reason = f"{type(error).__name__}: {error}"
            self.notify(
                f"the nickname was not saved: {reason}", severity="error", timeout=8, markup=False
            )
            return
        if event.state is not WorkerState.SUCCESS:
            return
        updated = event.worker.result
        if not isinstance(updated, ProjectInfo):
            return
        self.project = updated
        self.query_one("#nickname", Input).value = updated.nickname or ""
        self.query_one("#settings-title", Static).update(self._title())
        folder = updated.root.name or updated.id
        if updated.nickname:
            said = f"✓ {folder} is now called {updated.nickname}"
        else:
            said = f"✓ {folder} shows its folder name again"
        self.notify(said, timeout=5, markup=False)
        self.post_message(ProjectRenamed(updated))
