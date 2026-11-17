"""The project's Settings tab: what belongs to this project, not to the machine.

Its NICKNAME — the name every list shows (``services.project.display_name``),
what ``aisquare project rename`` sets — is edited here: Enter or Save writes it,
and a blank field clears it back to the folder name. The codename follows it, as it
follows ``project rename`` (``services.fleet.rename_project``): ``Billing v2`` makes it
``billing-v2`` and the tmux session ``asq-billing-v2``; ``aisquare fleet rename`` sets one
of your own. It is shown, not edited; asq names no command to attach a terminal to the
project's tmux session (R13), which is the shell's ``fleet attach``. Its *Pause agents…* and
*Resume agents…*, from the shell's agent frames (:meth:`SettingsView.show_agents`), ask it to
pause or resume the project's agents (:class:`~aisquare.cli.ui.views.fleet_bulk.BulkAsked`),
the one that would act on none greyed, and its *Start fresh…* to clear the project's board
and fleet (:class:`~aisquare.cli.ui.fresh.StartFresh`), as the card's menu does.
Everything machine-wide — roles, the fleet, how launches pick an account — is App settings
(``views.app_settings``).

The write opens the store and asks tmux, so it runs in a thread worker
(:data:`NICKNAME_WORKER`) and the tab answers when it lands: the field and the title show
what the store
now holds, and :class:`~aisquare.cli.ui.sidebar.ProjectRenamed` tells the rest
of the app to show the new name. A rename made anywhere else — a card, ``project
rename`` in a shell — reaches the tab with the app's next frame
(:meth:`SettingsView.show_project`), and Save always writes what the field says:
the tab's own copy of the project is never what decides there is nothing to save.
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import Button, Input, Label, Static
from textual.worker import Worker, WorkerState

from aisquare.cli.ui.fresh import StartFresh
from aisquare.cli.ui.sidebar import ProjectRenamed
from aisquare.cli.ui.views.fleet_bulk import BulkAsked, applicable
from aisquare.models import FleetAgentStatus, ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services import project as project_service

NICKNAME_WORKER = "settings-nickname"
"""The worker the nickname and the codename it gives are written in: it writes the store and
renames the tmux session."""


def fleet_id_line(project: ProjectInfo) -> Text:
    """The read-only line naming the project's codename and what it names."""
    codename = project.codename
    if codename is None:
        return Text(
            "fleet id: none yet — the first agent started here gives the project one "
            "(it names the fleet/<id>/… branches)",
            style="dim",
        )
    return Text(
        f"fleet id: {codename} — branches fleet/{codename}/…; renaming the project renames it",
        style="dim",
    )


class SettingsView(VerticalScroll):
    """One project's own settings: its nickname and its codename shown. How to attach to its
    tmux session from a terminal is the shell's (``fleet attach``): asq names no command (R13)."""

    DEFAULT_CSS = """
    SettingsView { padding: 1 2; }
    SettingsView .row { height: auto; margin-bottom: 1; }
    SettingsView .row Label { width: 12; padding-top: 1; }
    SettingsView .row Input { width: 1fr; max-width: 60; }
    SettingsView .row Button { margin-left: 1; }
    SettingsView #fleet-id { height: auto; }
    SettingsView #attach-line { height: auto; margin-top: 1; }
    SettingsView #fresh-row, SettingsView #pause-row { margin-top: 1; }
    SettingsView #fresh-row Button, SettingsView #pause-row #pause-agents { margin-left: 0; }
    SettingsView #fresh-about, SettingsView #pause-about {
        width: 1fr; padding: 1 0 0 2; color: $text-muted;
    }
    SettingsView #settings-note { color: $text-muted; margin-top: 1; height: auto; }
    """

    def __init__(self, project: ProjectInfo, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.project = project
        self.agents: list[FleetAgentStatus] | None = None
        """The project's agent rows as the shell's last frame listed them; ``None`` before
        the first."""
        self.fleet_paused = False
        """Whether the project's ``fleet-paused`` signal read on in that frame: Resume clears
        it, with no agent paused too."""

    def show_agents(
        self, agents: Sequence[FleetAgentStatus], *, fleet_paused: bool = False
    ) -> None:
        """The project's rows from the shell's frame: the attach line follows its session, and
        Pause / Resume what they would act on (``fleet_paused``: its signal reads on)."""
        self.agents = list(agents)
        self.fleet_paused = fleet_paused
        if not self.is_mounted:  # not composed yet: compose reads self.agents
            return
        self._paint_pause()

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
        can = applicable(self.agents, fleet_paused=self.fleet_paused)
        with Horizontal(classes="row", id="pause-row"):
            yield Button("⏸ Pause agents…", id="pause-agents", disabled=not can["pause"])
            yield Button("▶ Resume agents…", id="resume-agents", disabled=not can["resume"])
            yield Static(
                Text(
                    "Pause exits its running agents and keeps each one's claim, worktree and "
                    "session; Resume starts exactly those again. Each asks first."
                ),
                id="pause-about",
            )
        with Horizontal(classes="row", id="fresh-row"):
            yield Button("↺ Start fresh…", id="start-fresh", variant="warning")
            yield Static(
                Text(
                    "Clear its board and fleet for the next piece of work: a new blank "
                    "manager starts. It shows what goes and asks first."
                ),
                id="fresh-about",
            )
        yield Static(
            Text(
                "The nickname is the name every list shows; blank shows the folder name. "
                "The fleet id follows it. The settings for every project — roles, the fleet, "
                "accounts — are in App settings ▸ Agent settings."
            ),
            id="settings-note",
        )

    def _paint_pause(self) -> None:
        """Pause and Resume greyed where they would act on nothing (``applicable``)."""
        can = applicable(self.agents, fleet_paused=self.fleet_paused)
        for action in ("pause", "resume"):
            with contextlib.suppress(NoMatches):
                self.query_one(f"#{action}-agents", Button).disabled = not can[action]

    @on(Button.Pressed, "#pause-agents")
    def _pause(self, event: Button.Pressed) -> None:
        """*Pause agents…*: the shell asks, as the card's menu has it ask."""
        event.stop()
        self.post_message(BulkAsked("pause", self.project.id))

    @on(Button.Pressed, "#resume-agents")
    def _resume(self, event: Button.Pressed) -> None:
        """*Resume agents…*: the shell asks, as the card's menu has it ask."""
        event.stop()
        self.post_message(BulkAsked("resume", self.project.id))

    @on(Button.Pressed, "#start-fresh")
    def _start_fresh(self, event: Button.Pressed) -> None:
        """*Start fresh…*: the shell asks, as the card's menu has it ask."""
        event.stop()
        self.post_message(StartFresh(self.project.id))

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

        def rename() -> tuple[ProjectInfo, list[str], bool]:
            notes: list[str] = []
            before = project.codename
            renamed = fleet_service.rename_project(
                project, wanted or None, notes=notes, follow_unchanged=False
            )
            works = bool(before) and fleet_service.old_codename_works(renamed.id, before or "")
            return renamed, notes, works

        self.run_worker(
            rename,
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
        result = event.worker.result
        if not isinstance(result, tuple):
            return
        updated, notes, _old_works = result
        if not isinstance(updated, ProjectInfo):
            return
        before = self.project.codename
        self.project = updated
        self.query_one("#nickname", Input).value = updated.nickname or ""
        self.query_one("#settings-title", Static).update(self._title())
        self.query_one("#fleet-id", Static).update(fleet_id_line(updated))
        folder = updated.root.name or updated.id
        if updated.nickname:
            said = f"✓ {folder} is now called {updated.nickname}"
        else:
            said = f"✓ {folder} shows its folder name again"
        if updated.codename:
            said += f" · fleet codename {updated.codename}"
            if before and before != updated.codename:
                said += f" (was {before})"  # the `--project` spellings are the shell's (R13)
        self.notify(said, timeout=6, markup=False)
        for note in notes:
            self.notify(note, severity="warning", timeout=10, markup=False)
        self.post_message(ProjectRenamed(updated))
