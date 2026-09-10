"""The Settings tab: a form over ``[fleet]`` — every default the plan calls a default.

docs/plans/fleet-tui.md §3.10, §4.2: permission mode and worktree per role, the
escape key, ``max_agents_per_project``, the worktree root, the native-agent-teams
switch and the project's codename are all user-changeable here, written through
``core.config.save_config`` (the one writer) and re-read after every save so the
form shows what the file holds. Precedence: a per-spawn flag beats this file
beats the built-in default — the form edits the middle rung. There is no
environment rung for ``[fleet]``: no value in that section is read from an env
var (the orchestrator's own knobs are a different surface).

Model, effort and binary per role are NOT here on purpose: they live in
``team harness`` / ``team bind`` / ``AISQUARE_MODEL_<ROLE>`` — one home per
concept, which is the rule that deleted ``bins`` in #56.
"""

from __future__ import annotations

import re
from typing import ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Input, Label, Select, Static, Switch

from aisquare.core import codenames, paths
from aisquare.core.config import (
    AppConfig,
    FleetRoleSettings,
    FleetSettings,
    load_config,
    save_config,
)
from aisquare.models import ProjectInfo
from aisquare.services import fleet as fleet_service

PERMISSION_MODES: tuple[tuple[str, str], ...] = (
    ("auto", "auto"),
    ("acceptEdits", "acceptEdits"),
    ("bypassPermissions", "bypassPermissions"),
    ("manual", "manual"),
    ("dontAsk", "dontAsk"),
    ("plan", "plan"),
    ("(no flag)", ""),
)
"""Claude Code's ``--permission-mode`` choices (2.1.250), plus "pass no flag"."""

DEFAULT_ESCAPE_KEY = FleetSettings().escape_key
DEFAULT_WORKTREE_DIR = FleetSettings().worktree_dir
_ID_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def role_order(fleet: FleetSettings) -> list[str]:
    """The fleet's roles first, in their order; any bound extra roles after, sorted."""
    known = [role for role in fleet_service.FLEET_ROLES if role in fleet.roles]
    extra = sorted(role for role in fleet.roles if role not in fleet_service.FLEET_ROLES)
    missing = [role for role in fleet_service.FLEET_ROLES if role not in fleet.roles]
    return known + missing + extra


def widget_suffix(role: str) -> str:
    """A role name as the tail of a widget id (ids allow ``[A-Za-z0-9_-]`` only)."""
    return _ID_SAFE.sub("-", role)


def permission_options(current: str) -> list[tuple[str, str]]:
    """The mode list, with a value the list does not know appended so it still shows."""
    options = list(PERMISSION_MODES)
    if current not in {value for _, value in options}:
        options.append((f"{current} (custom)", current))
    return options


class SettingsView(VerticalScroll):
    """The ``[fleet]`` form for one project."""

    DEFAULT_CSS = """
    SettingsView { padding: 1 2; }
    SettingsView .row { height: auto; margin-bottom: 1; }
    SettingsView .row Label { width: 24; padding-top: 1; }
    SettingsView .row Select { width: 26; }
    SettingsView .row Input { width: 26; }
    SettingsView .row Switch { margin-left: 1; }
    SettingsView .row .worktree-label { width: 10; margin-left: 2; }
    SettingsView .section { text-style: bold; margin-top: 1; }
    SettingsView #settings-buttons Button { margin-right: 1; }
    SettingsView #settings-note { color: $text-muted; margin-top: 1; height: auto; }
    """
    ROLE_SECTION: ClassVar[str] = "roles — permission mode and worktree per role"

    def __init__(self, project: ProjectInfo, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.project = project
        self.fleet = fleet_service.settings()
        self._roles: list[str] = role_order(self.fleet)

    # --- layout ----------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        name = self.project.root.name or self.project.id
        yield Static(Text(f"fleet settings — {name}", style="bold"), id="settings-title")
        with Horizontal(classes="row"):
            yield Label("codename")
            yield Input(
                value=self.project.codename or "",
                placeholder="amber-otter",
                id="codename",
            )
            yield Button("Rename", id="rename-codename")
        yield Static(self.ROLE_SECTION, classes="section")
        for role in self._roles:
            settings = self.fleet.roles.get(role, FleetRoleSettings())
            suffix = widget_suffix(role)
            with Horizontal(classes="row"):
                yield Label(role)
                yield Select(
                    permission_options(settings.permission_mode),
                    value=settings.permission_mode,
                    allow_blank=False,
                    id=f"perm-{suffix}",
                )
                yield Label("worktree", classes="worktree-label")
                yield Switch(settings.worktree, id=f"worktree-{suffix}")
        yield Static("fleet", classes="section")
        with Horizontal(classes="row"):
            yield Label("escape key")
            yield Input(
                value=self.fleet.escape_key, placeholder=DEFAULT_ESCAPE_KEY, id="escape-key"
            )
        with Horizontal(classes="row"):
            yield Label("max agents per project")
            yield Input(
                value=str(self.fleet.max_agents_per_project), type="integer", id="max-agents"
            )
        with Horizontal(classes="row"):
            yield Label("worktree root")
            yield Input(
                value=self.fleet.worktree_dir, placeholder=DEFAULT_WORKTREE_DIR, id="worktree-dir"
            )
        with Horizontal(classes="row"):
            yield Label("native agent teams off")
            yield Switch(self.fleet.disable_native_agent_teams, id="native-teams")
        with Horizontal(id="settings-buttons", classes="row"):
            yield Button("Save", id="save-settings", variant="primary")
            yield Button("Reload", id="reload-settings")
        yield Static(
            Text(
                f"saved to {paths.config_path()} — a per-spawn flag still wins over "
                "these (no [fleet] value is read from the environment). Model, effort "
                "and binary per role live in `aisquare team harness` and "
                "`aisquare team bind`.",
            ),
            id="settings-note",
        )

    # --- reading -----------------------------------------------------------------------

    @on(Button.Pressed, "#reload-settings")
    def reload_form(self) -> None:
        """Discard edits: show what the file holds (the roles list can change with it)."""
        self.fleet = fleet_service.settings()
        self._roles = role_order(self.fleet)
        self.query_one("#codename", Input).value = self.project.codename or ""
        self.query_one("#escape-key", Input).value = self.fleet.escape_key
        self.query_one("#max-agents", Input).value = str(self.fleet.max_agents_per_project)
        self.query_one("#worktree-dir", Input).value = self.fleet.worktree_dir
        self.query_one("#native-teams", Switch).value = self.fleet.disable_native_agent_teams
        for role in self._roles:
            settings = self.fleet.roles.get(role, FleetRoleSettings())
            suffix = widget_suffix(role)
            try:
                select = self.query_one(f"#perm-{suffix}", Select)
                switch = self.query_one(f"#worktree-{suffix}", Switch)
            except Exception:  # a role bound since this form was composed; shown after a reopen
                continue
            select.set_options(permission_options(settings.permission_mode))
            select.value = settings.permission_mode
            switch.value = settings.worktree

    # --- writing -----------------------------------------------------------------------

    def _form_fleet(self, current: FleetSettings) -> FleetSettings | str:
        """``current`` with the form's values applied — or the reason the form is not valid."""
        raw = self.query_one("#max-agents", Input).value.strip()
        try:
            max_agents = int(raw)
        except ValueError:
            return f"max agents per project must be a whole number, not {raw!r}"
        if max_agents < 1:
            return "max agents per project must be at least 1"
        roles = dict(current.roles)
        for role in self._roles:
            suffix = widget_suffix(role)
            value = self.query_one(f"#perm-{suffix}", Select).value
            mode = (
                value
                if isinstance(value, str)
                else current.roles.get(role, FleetRoleSettings()).permission_mode
            )
            existing = current.roles.get(role, FleetRoleSettings())
            roles[role] = existing.model_copy(
                update={
                    "permission_mode": mode,
                    "worktree": self.query_one(f"#worktree-{suffix}", Switch).value,
                }
            )
        return current.model_copy(
            update={
                "roles": roles,
                "escape_key": self.query_one("#escape-key", Input).value.strip()
                or DEFAULT_ESCAPE_KEY,
                "max_agents_per_project": max_agents,
                "worktree_dir": self.query_one("#worktree-dir", Input).value.strip()
                or DEFAULT_WORKTREE_DIR,
                "disable_native_agent_teams": self.query_one("#native-teams", Switch).value,
            }
        )

    @on(Button.Pressed, "#save-settings")
    def _save_fleet_settings(self) -> None:
        """Write the form through the one config writer, then show what landed."""
        try:
            config: AppConfig = load_config()
        except Exception as exc:  # a broken config.toml: say so, change nothing
            self.notify(
                f"config unreadable — nothing saved: {exc}",
                severity="error",
                timeout=8,
                markup=False,
            )
            return
        result = self._form_fleet(config.fleet)
        if isinstance(result, str):
            self.notify(result, severity="error", timeout=6, markup=False)
            return
        config.fleet = result
        try:
            written = save_config(config)
        except OSError as exc:  # the operator's filesystem saying no — the foreseeable failure
            self.notify(
                f"could not write the config: {exc}",
                severity="error",
                timeout=8,
                markup=False,
            )
            return
        self.reload_form()
        self.notify(f"✓ fleet settings saved to {written}", timeout=5, markup=False)

    @on(Button.Pressed, "#rename-codename")
    def _rename_codename(self) -> None:
        """``fleet rename``: validated here first, so an invalid name never reaches tmux."""
        wanted = self.query_one("#codename", Input).value.strip()
        if not codenames.is_codename(wanted):
            self.notify(
                f"{wanted!r} is not a valid codename — two lowercase words of 3 to 7 letters "
                "joined by '-', like amber-otter",
                severity="error",
                timeout=6,
                markup=False,
            )
            return
        if wanted == self.project.codename:
            self.notify(f"the codename is already {wanted}", timeout=4, markup=False)
            return
        notes: list[str] = []
        try:
            updated = fleet_service.rename(self.project, wanted, notes=notes)
        except fleet_service.FleetError as exc:
            self.notify(str(exc), severity="error", timeout=8, markup=False)
            return
        self.project = updated
        self.query_one("#codename", Input).value = updated.codename or ""
        self.notify(
            f"✓ {updated.root.name or updated.id} is now {updated.codename} "
            f"({fleet_service.session_name(updated.codename or '')})",
            timeout=5,
            markup=False,
        )
        # A tmux rename the server refused is swallowed by the service (the row
        # is what everything else reads) — but it costs `fleet attach`, so the
        # cost is shown rather than left for the escape hatch to reveal.
        for note in notes:
            self.notify(note, severity="warning", timeout=10, markup=False)
