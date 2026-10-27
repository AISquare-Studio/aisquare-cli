"""The project's Settings tab: what belongs to this project, not to the machine.

Its NICKNAME — the name every list shows (``services.project.display_name``),
what ``aisquare project rename`` sets — is edited here: Enter or Save writes it,
and a blank field clears it back to the folder name. Its codename is shown, not
edited: it names the tmux session and the branches, and ``aisquare fleet
rename`` is where it changes. Under it, the command that attaches a terminal to
the project's tmux session (:func:`attach_line`), from the shell's agent frames
(:meth:`SettingsView.show_agents`). Its *Start fresh…* asks the shell to clear the
project's board and fleet (:class:`~aisquare.cli.ui.fresh.StartFresh`), as the card's menu
does. Everything machine-wide — roles, the fleet, how launches pick an account — is App
settings (``views.app_settings``).

The write opens the store, so it runs in a thread worker (:data:`NICKNAME_WORKER`)
and the tab answers when it lands: the field and the title show what the store
now holds, and :class:`~aisquare.cli.ui.sidebar.ProjectRenamed` tells the rest
of the app to show the new name. A rename made anywhere else — a card, ``project
rename`` in a shell — reaches the tab with the app's next frame
(:meth:`SettingsView.show_project`), and Save always writes what the field says:
the tab's own copy of the project is never what decides there is nothing to save.
"""

from __future__ import annotations

from collections.abc import Sequence

from rich.style import StyleType
from rich.text import Span, Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import Button, Input, Label, Static
from textual.worker import Worker, WorkerState

from aisquare.cli.ui.fresh import StartFresh
from aisquare.cli.ui.sidebar import ProjectRenamed
from aisquare.models import FleetAgentStatus, ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services import install_route
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


def attach_command(status: FleetAgentStatus) -> str | None:
    """``tmux -L asq attach -t =asq-<codename>`` for ``status``'s session, or ``None`` without one.

    Where the fleet asks its server: from a shell under another TMUX_TMPDIR, ``-L`` by the
    name reaches another server, and attaches to its session — so a row recorded at a path is
    attached by that path (``-S``). Quoted as every printed path is: a TMUX_TMPDIR or
    ``[fleet] tmux_socket`` with a space or a ``$`` in it pasted as another command, or
    another path (round 29 of #257).
    """
    if not status.tmux_session:
        return None
    path = status.socket_at
    where = ["-L", status.agent.tmux_socket] if path is None else ["-S", str(path)]
    return install_route.command_line(["tmux", *where, "attach", "-t", f"={status.tmux_session}"])


def attached_status(agents: Sequence[FleetAgentStatus]) -> FleetAgentStatus | None:
    """The row whose tmux session a terminal attaches to, in the order ``aisquare fleet
    attach`` asks the servers (``services.fleet.attach_argv``): the rows that have not ended,
    the manager first, then the ended ones whose window still stands, the latest to end first.

    An ENDED manager is listed while its window stands (#138), and ranked by its label alone
    it named its server ahead of a live coder's — not the server ``fleet attach`` reaches
    (review of #267, 1). Not a ``lost`` row either (its pane, and maybe its session, is
    gone), nor one held as another server's: what answers at its socket is not the server
    it started on.
    """
    usable = [s for s in agents if s.tmux_session and s.state != "lost" and not s.held]
    live = sorted(
        (s for s in usable if s.agent.ended_at is None),
        key=lambda s: s.agent.label != fleet_service.MANAGER_LABEL,
    )
    ended = sorted(
        (s for s in usable if s.agent.ended_at is not None),
        key=lambda s: s.agent.ended_at or s.agent.created_at,
        reverse=True,
    )
    return next(iter([*live, *ended]), None)


def attach_line(agents: Sequence[FleetAgentStatus] | None) -> Text:
    """``Attach in a terminal: tmux -L asq attach -t =asq-<codename>`` — or a dim note when the
    project has no live tmux session to attach to.

    ``None`` is no frame yet: nothing is said rather than a "no session" that may be false.
    """
    if agents is None:
        return Text()
    status = attached_status(agents)
    command = attach_command(status) if status is not None else None
    if command is None:
        return Text(
            "Attach in a terminal: no live tmux session — the project's first agent starts one",
            style="dim",
        )
    return Text.assemble(("Attach in a terminal: ", "bold"), command)


class SettingsView(VerticalScroll):
    """One project's own settings: its nickname, its codename shown, and how to attach to it."""

    DEFAULT_CSS = """
    SettingsView { padding: 1 2; }
    SettingsView .row { height: auto; margin-bottom: 1; }
    SettingsView .row Label { width: 12; padding-top: 1; }
    SettingsView .row Input { width: 1fr; max-width: 60; }
    SettingsView .row Button { margin-left: 1; }
    SettingsView #fleet-id { height: auto; }
    SettingsView #attach-line { height: auto; margin-top: 1; }
    SettingsView #fresh-row { margin-top: 1; }
    SettingsView #fresh-row Button { margin-left: 0; }
    SettingsView #fresh-about { width: 1fr; padding: 1 0 0 2; color: $text-muted; }
    SettingsView #settings-note { color: $text-muted; margin-top: 1; height: auto; }
    """

    def __init__(self, project: ProjectInfo, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.project = project
        self.agents: list[FleetAgentStatus] | None = None
        """The project's agent rows as the shell's last frame listed them; ``None`` before
        the first."""
        self._attach_painted: tuple[str, list[Span], StyleType] | None = None
        """The attach line as last painted: a frame that changes nothing repaints nothing.
        Its text, spans and style — ``Text``'s own ``==`` leaves the style out."""

    def show_agents(self, agents: Sequence[FleetAgentStatus]) -> None:
        """The project's rows from the shell's frame: the attach line follows its session."""
        self.agents = list(agents)
        try:
            line = self.query_one("#attach-line", Static)
        except NoMatches:  # not composed yet: compose reads self.agents
            return
        wanted = attach_line(self.agents)
        painted = (wanted.plain, wanted.spans, wanted.style)
        if painted != self._attach_painted:
            self._attach_painted = painted
            line.update(wanted)

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
        attach = attach_line(self.agents)
        self._attach_painted = (attach.plain, attach.spans, attach.style)
        yield Static(attach, id="attach-line")
        with Horizontal(classes="row", id="fresh-row"):
            yield Button("↺ Start fresh…", id="start-fresh", variant="warning")
            yield Static(
                Text(
                    "Clear its board and fleet for the next piece of work: a new blank "
                    "manager starts. It shows what goes and asks first."
                ),
                id="fresh-about",
            )
        # The command in bold, not in backticks: the note is a ``Text``, which shows them.
        yield Static(
            Text.assemble(
                "The nickname is the name every list shows; blank shows the folder name. ",
                ("aisquare project rename", "bold"),
                " sets the same. The settings for every project — roles, the fleet, "
                "accounts — are in App settings ▸ Agent settings.",
            ),
            id="settings-note",
        )

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
