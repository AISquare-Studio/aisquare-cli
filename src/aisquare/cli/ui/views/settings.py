"""The Settings tab: a form over ``[fleet]`` — every default the plan calls a default.

docs/plans/fleet-tui.md §3.10, §4.2: permission mode, worktree and persona per
role, the escape key, ``max_agents_per_project``, the worktree root, the
native-agent-teams switch and the project's codename are all user-changeable
here, written through ``core.config.save_config`` (the one writer) and re-read
after every save so the form shows what the file holds. Precedence: a per-spawn
flag beats this file beats the built-in default — the form edits the middle
rung. There is no environment rung for ``[fleet]``: no value in that section is
read from an env var (the orchestrator's own knobs are a different surface).

The persona per role (docs/plans/spawn-personas.md §3.8, P4) lists this project's
personas — ``core.personas.catalogue(project.root)``, the set ``fleet spawn``
checks a default against — plus a configured name the catalogue lacks, shown as
``<name> (custom)`` so saving the form never silently drops it (the spawn then
refuses it, naming the key).

Model, effort and binary per role are NOT here on purpose: they live in
``team harness`` / ``team bind`` / ``AISQUARE_MODEL_<ROLE>`` — one home per
concept, which is the rule that deleted ``bins`` in #56.

The one ``[team]`` value on this form is the ACCOUNT per role (#145): the
select beside each role writes ``team.profiles.<role>.account`` — exactly what
``aisquare team bind <role> --account`` writes — through the same one writer.
It is here rather than on the Accounts page because it is a property of the
role, and it is the middle rung of the account ladder a launch walks
(``--account`` > role binding > project default > machine default).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Input, Label, Select, Static, Switch
from textual.worker import Worker, WorkerState

from aisquare.core import claude_accounts as accounts_core
from aisquare.core import codenames, paths, personas
from aisquare.core.config import (
    AccountsSettings,
    AppConfig,
    FleetRoleSettings,
    FleetSettings,
    RoleLaunchProfile,
    load_config,
    save_config,
)
from aisquare.models import ClaudeAccount, ProjectInfo
from aisquare.services import claude_accounts as accounts_service
from aisquare.services import fleet as fleet_service
from aisquare.services import settings as settings_service

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

NO_PERSONA = ""
"""A role's ``(none)``: no ``persona`` key is written for it."""

DEFAULT_ESCAPE_KEY = FleetSettings().escape_key
DEFAULT_WORKTREE_DIR = FleetSettings().worktree_dir
PICK_MODES: tuple[tuple[str, str], ...] = (
    ("the default account", "default"),
    ("the account with headroom", "headroom"),
)
"""``[accounts] pick`` (#146): how a launch chooses when nothing names an account."""
ON_LIMIT_MODES: tuple[tuple[str, str], ...] = (
    ("wait for the reset (Claude Code continues by itself)", "wait"),
    ("switch to the account with headroom", "switch"),
)
"""``[accounts] on_limit``: what the fleet does when an agent's turn ends on a usage limit."""
_ID_SAFE = re.compile(r"[^A-Za-z0-9_-]")
ACCOUNTS_WORKER = "settings-accounts"


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


def persona_options(current: str | None, names: Iterable[str]) -> list[tuple[str, str]]:
    """``(none)`` and the persona names, with a configured name they lack appended so it shows."""
    options = [("(none)", NO_PERSONA), *((name, name) for name in names)]
    if current and current not in {value for _, value in options}:
        options.append((f"{current} (custom)", current))
    return options


NO_ACCOUNT = ""
"""The select's value for "this role expresses no preference" — the binding's ``account = None``."""


def account_options(accounts: list[ClaudeAccount], current: str | None) -> list[tuple[str, str]]:
    """``(label, value)`` per account, plus the no-binding row first; ``current`` always shows.

    Values are SLOT NUMBERS as strings: the binding stores a reference the
    resolver reads, and the slot is the one spelling that cannot be renamed
    out from under it. A binding written by hand as an alias or an email is
    shown as its own row (``work (as bound)``) so the form never silently
    rewrites what the operator typed just by being opened and saved.
    """
    options: list[tuple[str, str]] = [("(no account binding)", NO_ACCOUNT)]
    for account in accounts:
        identity = accounts_core.identity(account)
        who = f" · {identity.email}" if identity else " · not signed in"
        options.append((f"{account.slot} · {accounts_core.label(account)}{who}", str(account.slot)))
    if current and current not in {value for _, value in options}:
        options.append((f"{current} (as bound)", current))
    return options


class SettingsView(VerticalScroll):
    """The ``[fleet]`` form for one project."""

    DEFAULT_CSS = """
    SettingsView { padding: 1 2; }
    SettingsView .row { height: auto; margin-bottom: 1; }
    SettingsView .row Label { width: 24; padding-top: 1; }
    SettingsView .row Select { width: 26; }
    SettingsView .row .account-select { width: 40; margin-left: 2; }
    SettingsView .row Input { width: 26; }
    SettingsView .row Switch { margin-left: 1; }
    SettingsView .row .worktree-label { width: 10; margin-left: 2; }
    SettingsView .row .persona-label { width: 9; margin-left: 2; }
    SettingsView .section { text-style: bold; margin-top: 1; }
    SettingsView #settings-buttons Button { margin-right: 1; }
    SettingsView #settings-note { color: $text-muted; margin-top: 1; height: auto; }
    SettingsView #settings-personas-unavailable { display: none; color: $warning; height: auto; }
    SettingsView #settings-personas-unavailable.shown { display: block; }
    """
    ROLE_SECTION: ClassVar[str] = "roles — permission mode, worktree, account and persona per role"
    ACCOUNTS_SECTION: ClassVar[str] = "accounts — how launches pick one; what a usage limit does"

    def __init__(self, project: ProjectInfo, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.project = project
        config = self._read_config()
        self.fleet = config.fleet
        self.accounts = config.accounts
        self._roles: list[str] = role_order(self.fleet)
        self._account_bindings: dict[str, str] = settings_service.role_account_bindings(config)
        self._accounts: list[ClaudeAccount] = []
        """The slots the account selects offer — filled by :meth:`_load_accounts`'s worker."""
        self._persona_names, self._personas_unavailable = self._read_persona_names()

    @staticmethod
    def _read_config() -> AppConfig:
        """The file, read ONCE for every section on the form; unreadable reads as the defaults.

        The form read it three times over — ``[fleet]``, ``[accounts]`` and the
        role bindings each opened it (review of #205, fourth round) — with the
        same fail-open each: a broken ``config.toml`` costs the customisation,
        never the tab.
        """
        try:
            return load_config()
        except Exception:
            return AppConfig()

    @staticmethod
    def _read_accounts() -> list[ClaudeAccount]:
        try:
            return accounts_service.list_accounts()
        except Exception:  # no accounts to offer is a form with one row, not a crash
            return []

    def _load_accounts(self) -> None:
        """Read the slots for the account selects OFF the UI thread.

        ``list_accounts`` opens ``context.db`` (a busy timeout of seconds), scans
        the account directories and may write the reconcile, and the form did
        it in its constructor, on the event loop (review of #205, fourth round)
        — work the Accounts page and the agent header keep in thread workers.
        The selects compose with the bindings alone and gain the slots when the
        worker answers (:meth:`_accounts_read`).
        """
        self.run_worker(
            self._read_accounts,
            name=ACCOUNTS_WORKER,
            group=ACCOUNTS_WORKER,
            exclusive=True,
            thread=True,
            exit_on_error=False,
        )

    @on(Worker.StateChanged)
    def _accounts_read(self, event: Worker.StateChanged) -> None:
        if event.worker.group != ACCOUNTS_WORKER:
            return
        if event.state is WorkerState.SUCCESS and isinstance(event.worker.result, list):
            self._accounts = event.worker.result
            self._fill_account_selects()

    def _fill_account_selects(self) -> None:
        """Offer the slots the worker read, keeping what each select shows.

        Before the first answer a select offers "no binding" and the binding
        itself (``account_options`` keeps a value it does not know), and both
        survive the new options, so what the operator picked meanwhile stays.
        A slot picked from an earlier answer that this one no longer has — it
        was removed in between — falls back to the binding rather than being
        set as a value the select would refuse.
        """
        for role in self._roles:
            try:
                select = self.query_one(f"#acct-{widget_suffix(role)}", Select)
            except Exception:  # a role bound since this form was composed; shown after a reopen
                continue
            shown = select.value
            bound = self._account_bindings.get(role)
            options = account_options(self._accounts, bound)
            select.set_options(options)
            offered = {value for _label, value in options}
            select.value = shown if shown in offered else (bound or NO_ACCOUNT)

    def _read_persona_names(self) -> tuple[list[str], str | None]:
        """This project's persona names — or none, and the reason, which the form shows.

        ``catalogue`` never raises for one bad directory, so whatever reaches the
        ``except`` is a real failure (an unreadable layer, a bug). It costs the list,
        never the tab, and it is said under the roles, the way the Spawn dialog says
        it under its Persona field.
        """
        try:
            found, _invalid = personas.catalogue(self.project.root)
        except Exception as exc:
            return [], f"personas unavailable — {type(exc).__name__}: {exc}"
        return [persona.name for persona in found], None

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
        yield Static(
            Text(self._personas_unavailable or ""),
            id="settings-personas-unavailable",
            classes="shown" if self._personas_unavailable else "",
        )
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
                yield Label("persona", classes="persona-label")
                yield Select(
                    persona_options(settings.persona, self._persona_names),
                    value=settings.persona or NO_PERSONA,
                    allow_blank=False,
                    id=f"persona-{suffix}",
                )
                bound = self._account_bindings.get(role)
                yield Select(
                    account_options(self._accounts, bound),
                    value=bound or NO_ACCOUNT,
                    allow_blank=False,
                    id=f"acct-{suffix}",
                    classes="account-select",
                )
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
        yield Static(self.ACCOUNTS_SECTION, classes="section")
        with Horizontal(classes="row"):
            yield Label("launches pick")
            yield Select(
                PICK_MODES, value=self.accounts.pick, allow_blank=False, id="accounts-pick"
            )
        with Horizontal(classes="row"):
            yield Label("switch at (% of 5 h)")
            yield Input(value=str(self.accounts.switch_at), type="integer", id="accounts-switch-at")
        with Horizontal(classes="row"):
            yield Label("on a usage limit")
            yield Select(
                ON_LIMIT_MODES,
                value=self.accounts.on_limit,
                allow_blank=False,
                id="accounts-on-limit",
            )
        with Horizontal(classes="row"):
            yield Label("wait if reset within (min)")
            yield Input(
                value=str(self.accounts.wait_if_reset_within_minutes),
                type="integer",
                id="accounts-wait-minutes",
            )
        with Horizontal(id="settings-buttons", classes="row"):
            yield Button("Save", id="save-settings", variant="primary")
            yield Button("Reload", id="reload-settings")
        yield Static(
            Text(
                f"saved to {paths.config_path()} — a per-spawn flag still wins over "
                "these (no [fleet] value is read from the environment). Model, effort "
                "and binary per role live in `aisquare team harness` and "
                "`aisquare team bind`. The account per role is `team bind <role> "
                "--account`; the machine and project defaults are on the Accounts page "
                "and `aisquare accounts default`.",
            ),
            id="settings-note",
        )

    def on_mount(self) -> None:
        self._load_accounts()

    # --- reading -----------------------------------------------------------------------

    @on(Button.Pressed, "#reload-settings")
    def reload_form(self) -> None:
        """Discard edits: show what the file holds (the roles list can change with it)."""
        config = self._read_config()
        self.fleet = config.fleet
        self._roles = role_order(self.fleet)
        self._persona_names, self._personas_unavailable = self._read_persona_names()
        unavailable = self.query_one("#settings-personas-unavailable", Static)
        unavailable.update(Text(self._personas_unavailable or ""))
        unavailable.set_class(bool(self._personas_unavailable), "shown")
        self._account_bindings = settings_service.role_account_bindings(config)
        self.query_one("#codename", Input).value = self.project.codename or ""
        self.query_one("#escape-key", Input).value = self.fleet.escape_key
        self.query_one("#max-agents", Input).value = str(self.fleet.max_agents_per_project)
        self.query_one("#worktree-dir", Input).value = self.fleet.worktree_dir
        self.query_one("#native-teams", Switch).value = self.fleet.disable_native_agent_teams
        self.accounts = config.accounts
        self.query_one("#accounts-pick", Select).value = self.accounts.pick
        self.query_one("#accounts-switch-at", Input).value = str(self.accounts.switch_at)
        self.query_one("#accounts-on-limit", Select).value = self.accounts.on_limit
        self.query_one("#accounts-wait-minutes", Input).value = str(
            self.accounts.wait_if_reset_within_minutes
        )
        for role in self._roles:
            settings = self.fleet.roles.get(role, FleetRoleSettings())
            suffix = widget_suffix(role)
            try:
                select = self.query_one(f"#perm-{suffix}", Select)
                switch = self.query_one(f"#worktree-{suffix}", Switch)
                persona = self.query_one(f"#persona-{suffix}", Select)
                account = self.query_one(f"#acct-{suffix}", Select)
            except Exception:  # a role bound since this form was composed; shown after a reopen
                continue
            select.set_options(permission_options(settings.permission_mode))
            select.value = settings.permission_mode
            switch.value = settings.worktree
            persona.set_options(persona_options(settings.persona, self._persona_names))
            persona.value = settings.persona or NO_PERSONA
            bound = self._account_bindings.get(role)
            account.set_options(account_options(self._accounts, bound))
            account.value = bound or NO_ACCOUNT
        self._load_accounts()  # the slots as they are now, off the UI thread

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
            existing = current.roles.get(role, FleetRoleSettings())
            value = self.query_one(f"#perm-{suffix}", Select).value
            mode = value if isinstance(value, str) else existing.permission_mode
            chosen = self.query_one(f"#persona-{suffix}", Select).value
            persona = chosen if isinstance(chosen, str) else (existing.persona or NO_PERSONA)
            roles[role] = existing.model_copy(
                update={
                    "permission_mode": mode,
                    "worktree": self.query_one(f"#worktree-{suffix}", Switch).value,
                    "persona": persona or None,
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

    def _form_accounts(self, current: AccountsSettings) -> AccountsSettings | str:
        """``current`` with the ``[accounts]`` form applied — or why the form is not valid."""
        raw_switch = self.query_one("#accounts-switch-at", Input).value.strip()
        raw_wait = self.query_one("#accounts-wait-minutes", Input).value.strip()
        try:
            switch_at = int(raw_switch)
            wait_minutes = int(raw_wait)
        except ValueError:
            return "switch at and wait-if-reset-within must be whole numbers"
        if not 1 <= switch_at <= 100:
            return "switch at must be between 1 and 100 (a percentage of the five-hour window)"
        if wait_minutes < 0:
            return "wait if reset within cannot be negative"
        pick = self.query_one("#accounts-pick", Select).value
        on_limit = self.query_one("#accounts-on-limit", Select).value
        return current.model_copy(
            update={
                "pick": pick if isinstance(pick, str) else current.pick,
                "switch_at": switch_at,
                "on_limit": on_limit if isinstance(on_limit, str) else current.on_limit,
                "wait_if_reset_within_minutes": wait_minutes,
            }
        )

    def _apply_account_bindings(self, config: AppConfig) -> None:
        """Fold the account selects into ``config.team.profiles`` — the binding's one home.

        A role whose select says "no binding" and whose profile holds nothing
        else has its profile REMOVED, not emptied: `team bind --clear` is one
        pop for the same reason, and an empty ``[team.profiles.coder]`` table
        would make ``_declared_roles`` think the operator declared a role.
        """
        for role in self._roles:
            value = self.query_one(f"#acct-{widget_suffix(role)}", Select).value
            chosen = value if isinstance(value, str) and value != NO_ACCOUNT else None
            profile = config.team.profiles.get(role)
            if chosen is None:
                if profile is None:
                    continue
                profile.account = None
                if not (profile.bin or profile.env or profile.args):
                    config.team.profiles.pop(role, None)
            else:
                if profile is None:
                    profile = config.team.profiles.setdefault(role, RoleLaunchProfile())
                profile.account = chosen

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
        self._apply_account_bindings(config)
        accounts = self._form_accounts(config.accounts)
        if isinstance(accounts, str):
            self.notify(accounts, severity="error", timeout=6, markup=False)
            return
        config.accounts = accounts
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
