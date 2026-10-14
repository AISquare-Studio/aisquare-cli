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

The ``[team]`` values on this form are the AGENT and the ACCOUNT per role
(#145): the selects beside each role write ``team.profiles.<role>.agent`` and
``.account`` — exactly what ``aisquare team bind <role> --agent/--account``
write — through the same one writer. They are here rather than on the Accounts
page because they are properties of the role; the account is the middle rung of
the account ladder a launch walks (``--account`` > role binding > project
default > machine default), and it is an account of the role's agent.

Permission is each agent's own vocabulary until a neutral one lands: Claude
Code's ``permission_mode``, Codex's ``sandbox``, ``approval_policy`` and
``approve_for_me``. Under each role, the flags its next ``fleet spawn`` passes
(:func:`launch_line`, the adapter's own ``fleet_args``), for the agent the form
says it runs on, so a value is never saved without its meaning shown — or the
refusal a spawn would meet.
"""

from __future__ import annotations

import re
import shlex
from typing import ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Input, Label, Select, Static, Switch
from textual.worker import Worker, WorkerState

from aisquare.core import claude_accounts as accounts_core
from aisquare.core import codenames, paths
from aisquare.core.agent_adapters import adapters, get_adapter
from aisquare.core.agent_adapters.codex import APPROVAL_POLICIES
from aisquare.core.agent_adapters.types import fleet_extra_args
from aisquare.core.config import (
    CLAUDE_PERMISSION_MODES,
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
    *((mode, mode) for mode in CLAUDE_PERMISSION_MODES),
    ("(no flag)", ""),
)
"""Claude Code's ``--permission-mode`` choices (``core.config``), plus "pass no flag"."""

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
SANDBOX_OPTIONS = [
    ("Role default", ""),
    *[(s, s) for s in ("read-only", "workspace-write", "danger-full-access")],
]
APPROVAL_OPTIONS = [("Native default", ""), *[(s, s) for s in APPROVAL_POLICIES]]
"""Codex's ``--ask-for-approval`` values (0.162.1 has no ``untrusted``; one saved by an
older version still shows, as ``(custom)``, and a spawn names why it refuses it)."""
APPROVE_FOR_ME_OPTIONS: list[tuple[str, str]] = [
    ("approve-for-me: role default", ""),
    ("approve-for-me: on", "on"),
    ("approve-for-me: off", "off"),
]
"""``fleet.roles.<role>.approve_for_me``: unset (on for every role but a reviewer's seats,
unless a sandbox or approval is chosen), or set either way."""
_APPROVE_VALUES: dict[str, bool | None] = {"": None, "on": True, "off": False}
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
    return _options(current, list(PERMISSION_MODES))


def _options(current: str, options: list[tuple[str, str]]) -> list[tuple[str, str]]:
    if current not in {value for _, value in options}:
        return [*options, (f"{current} (custom)", current)]
    return options


def _agent_options(current: str) -> list[tuple[str, str]]:
    return _options(current, [("Use default", ""), *((a.label, a.id) for a in adapters())])


def approve_value(setting: bool | None) -> str:
    """``approve_for_me`` as its select's value."""
    return "" if setting is None else "on" if setting else "off"


def launch_line(role: str, agent: str, settings: FleetRoleSettings) -> Text:
    """What ``fleet spawn <role>`` passes for permission on ``agent`` with ``settings``.

    The adapter's own ``fleet_args``, given what ``services.fleet.spawn`` gives it with no
    per-spawn flag: Claude Code the role's ``permission_mode``; an agent with sandboxed
    permissions (Codex) its ``sandbox``, ``approval_policy`` and ``approve_for_me``,
    never Claude's mode. ``--name`` is left out (it is the agent's label, not its
    permission). A combination the adapter refuses reads as that refusal, which is what
    the spawn would say.
    """
    text = Text(f"  {role} → ", style="dim")
    try:
        adapter = get_adapter(agent)
    except ValueError:
        text.append(f"{agent} is not an agent this version knows", style="yellow")
        return text
    sandboxed = adapter.capabilities.sandbox_permissions
    try:
        flags = adapter.fleet_args(
            role,
            role,
            None if sandboxed else (settings.permission_mode or None),
            sandbox=settings.sandbox if sandboxed else None,
            approval=settings.approval_policy if sandboxed else None,
            approve_for_me=settings.approve_for_me if sandboxed else None,
        )
    except ValueError as exc:
        text.append(f"{adapter.label}: refused at spawn — {exc}", style="red")
        return text
    if "--name" in flags:
        at = flags.index("--name")
        flags = flags[:at] + flags[at + 2 :]
    flags += fleet_extra_args(adapter, settings.extra_args, settings.agent_args)
    text.append(f"{adapter.label} ", style="dim")
    text.append(shlex.join(flags) if flags else "(no permission flag)", style="dim")
    return text


NO_ACCOUNT = ""
"""The select's value for "this role expresses no preference" — the binding's ``account = None``."""


def account_options(
    accounts: list[ClaudeAccount], current: str | None, agent: str = accounts_core.AGENT
) -> list[tuple[str, str]]:
    """``(label, value)`` per account, plus the no-binding row first; ``current`` always shows.

    ``agent`` is the role's: a slot of another agent says whose it is, because a launch
    refuses it for this role.

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
        # Another agent's slot says whose it is: a launch refuses one for the wrong agent.
        if account.agent != agent:
            who += f" · {accounts_core.agent_label(account.agent)}"
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
    SettingsView .section { text-style: bold; margin-top: 1; }
    SettingsView .launch-line { height: auto; margin-bottom: 1; }
    SettingsView #settings-buttons Button { margin-right: 1; }
    SettingsView #settings-note { color: $text-muted; margin-top: 1; height: auto; }
    """
    ROLE_SECTION: ClassVar[str] = (
        "roles — agent, permission, worktree and account per role (→ the flags a spawn passes)"
    )
    ACCOUNTS_SECTION: ClassVar[str] = "accounts — how launches pick one; what a usage limit does"

    def __init__(self, project: ProjectInfo, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.project = project
        config = self._read_config()
        self._config = config
        self.fleet = config.fleet
        self.accounts = config.accounts
        self._roles: list[str] = role_order(self.fleet)
        self._account_bindings: dict[str, str] = settings_service.role_account_bindings(config)
        self._accounts: list[ClaudeAccount] = []
        """The slots the account selects offer — filled by :meth:`_load_accounts`'s worker."""

    def _read_config(self) -> AppConfig:
        """The file, read ONCE for every section on the form; unreadable reads as the defaults.

        The form read it three times over — ``[fleet]``, ``[accounts]`` and the
        role bindings each opened it (review of #205, fourth round) — with the
        same fail-open each: a broken ``config.toml`` costs the customisation,
        never the tab. Why it could not be read is kept for the form's first line
        (``_config_error``), so defaults shown for a broken file are not saved over it
        unknowingly.
        """
        try:
            config = load_config()
            self._config_error = ""
        except Exception as exc:
            config = AppConfig()
            self._config_error = (
                f"Cannot read {paths.config_path()}: {exc}. Showing defaults; "
                "repair the file and reload before saving."
            )
        return config

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
            options = account_options(self._accounts, bound, self._role_agent(role))
            select.set_options(options)
            offered = {value for _label, value in options}
            select.value = shown if shown in offered else (bound or NO_ACCOUNT)

    # --- layout ----------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        name = self.project.root.name or self.project.id
        yield Static(Text(f"fleet settings — {name}", style="bold"), id="settings-title")
        config = self._config
        yield Static(Text(self._config_error), id="settings-config-error")
        with Horizontal(classes="row"):
            yield Label("user coding agent")
            yield Select(
                _agent_options(config.agents.default or ""),
                value=config.agents.default or "",
                allow_blank=False,
                id="default-agent",
            )
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
                binding = config.team.profiles.get(role, RoleLaunchProfile())
                yield Select(
                    _agent_options(binding.agent or ""),
                    value=binding.agent or "",
                    allow_blank=False,
                    id=f"family-{suffix}",
                )
                yield Select(
                    permission_options(settings.permission_mode),
                    value=settings.permission_mode,
                    allow_blank=False,
                    id=f"perm-{suffix}",
                )
                yield Label("worktree", classes="worktree-label")
                yield Switch(settings.worktree, id=f"worktree-{suffix}")
                bound = self._account_bindings.get(role)
                yield Select(
                    account_options(self._accounts, bound),
                    value=bound or NO_ACCOUNT,
                    allow_blank=False,
                    id=f"acct-{suffix}",
                    classes="account-select",
                )
            with Horizontal(classes="row"):
                yield Label("Codex scope / approval")
                yield Select(
                    _options(settings.sandbox or "", SANDBOX_OPTIONS),
                    value=settings.sandbox or "",
                    allow_blank=False,
                    id=f"sandbox-{suffix}",
                )
                yield Select(
                    _options(settings.approval_policy or "", APPROVAL_OPTIONS),
                    value=settings.approval_policy or "",
                    allow_blank=False,
                    id=f"approval-{suffix}",
                )
                yield Select(
                    APPROVE_FOR_ME_OPTIONS,
                    value=approve_value(settings.approve_for_me),
                    allow_blank=False,
                    id=f"afm-{suffix}",
                    classes="account-select",
                )
            yield Static(id=f"launch-{suffix}", classes="launch-line")
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
            yield Label("switch at (% used)")
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
        self._paint_launch_lines()

    # --- what a spawn would pass -------------------------------------------------------

    def _selected(self, selector: str) -> str | None:
        """A select's value, ``None`` when it has none (blank, or not on the form)."""
        try:
            value = self.query_one(selector, Select).value
        except Exception:
            return None
        return value if isinstance(value, str) else None

    def _role_agent(self, role: str) -> str:
        """The agent the form says ``role`` runs on: its own pick, the user default, or
        Claude Code. (A project default or ``AISQUARE_CODING_AGENT`` can still say otherwise
        at a launch; ``aisquare team harness`` shows the whole ladder.)"""
        family = self._selected(f"#family-{widget_suffix(role)}")
        if family is None:
            family = self._config.team.profiles.get(role, RoleLaunchProfile()).agent or ""
        default = self._selected("#default-agent")
        if default is None:
            default = self._config.agents.default or ""
        return family or default or accounts_core.AGENT

    def _form_role(self, role: str) -> FleetRoleSettings:
        """``role``'s settings as the form holds them now."""
        suffix = widget_suffix(role)
        settings = self.fleet.roles.get(role, FleetRoleSettings())
        mode = self._selected(f"#perm-{suffix}")
        sandbox = self._selected(f"#sandbox-{suffix}")
        approval = self._selected(f"#approval-{suffix}")
        approve = self._selected(f"#afm-{suffix}")
        return settings.model_copy(
            update={
                "permission_mode": settings.permission_mode if mode is None else mode,
                "sandbox": settings.sandbox if sandbox is None else (sandbox or None),
                "approval_policy": (
                    settings.approval_policy if approval is None else (approval or None)
                ),
                "approve_for_me": (
                    settings.approve_for_me if approve is None else _APPROVE_VALUES.get(approve)
                ),
            }
        )

    def _paint_launch_lines(self) -> None:
        for role in self._roles:
            try:
                line = self.query_one(f"#launch-{widget_suffix(role)}", Static)
            except Exception:  # a role bound since this form was composed
                continue
            line.update(launch_line(role, self._role_agent(role), self._form_role(role)))

    @on(Select.Changed)
    def _form_changed(self, event: Select.Changed) -> None:
        """A pick changes what a spawn passes; an agent pick changes whose accounts fit."""
        if not self.is_mounted:
            return
        self._paint_launch_lines()
        control = event.select.id or ""
        if control == "default-agent" or control.startswith("family-"):
            self._fill_account_selects()

    # --- reading -----------------------------------------------------------------------

    @on(Button.Pressed, "#reload-settings")
    def reload_form(self) -> None:
        """Discard edits: show what the file holds (the roles list can change with it)."""
        config = self._read_config()
        self._config = config
        self.fleet = config.fleet
        self.query_one("#settings-config-error", Static).update(Text(self._config_error))
        self.query_one("#default-agent", Select).set_options(
            _agent_options(config.agents.default or "")
        )
        self.query_one("#default-agent", Select).value = config.agents.default or ""
        self._roles = role_order(self.fleet)
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
                account = self.query_one(f"#acct-{suffix}", Select)
            except Exception:  # a role bound since this form was composed; shown after a reopen
                continue
            select.set_options(permission_options(settings.permission_mode))
            select.value = settings.permission_mode
            switch.value = settings.worktree
            family = config.team.profiles.get(role, RoleLaunchProfile()).agent or ""
            for field, value, options in (
                ("family", family, _agent_options(family)),
                ("sandbox", settings.sandbox or "", SANDBOX_OPTIONS),
                ("approval", settings.approval_policy or "", APPROVAL_OPTIONS),
                ("afm", approve_value(settings.approve_for_me), APPROVE_FOR_ME_OPTIONS),
            ):
                control = self.query_one(f"#{field}-{suffix}", Select)
                control.set_options(_options(value, options))
                control.value = value
            bound = self._account_bindings.get(role)
            account.set_options(account_options(self._accounts, bound, self._role_agent(role)))
            account.value = bound or NO_ACCOUNT
        self._paint_launch_lines()
        self._load_accounts()  # the slots as they are now, off the UI thread

    # --- writing -----------------------------------------------------------------------

    def _optional_selection(self, selector: str, current: str | None) -> str | None:
        value = self.query_one(selector, Select).value
        return (value or None) if isinstance(value, str) else current

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
            mode = self._optional_selection(f"#perm-{suffix}", existing.permission_mode) or ""
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
            return "switch at must be between 1 and 100 (a percentage of a usage window)"
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
        """Fold the account selects the operator CHANGED into ``config.team.profiles``.

        A role whose select says "no binding" and whose profile holds nothing
        else has its profile REMOVED, not emptied: `team bind --clear` is one
        pop for the same reason, and an empty ``[team.profiles.coder]`` table
        would make ``_declared_roles`` think the operator declared a role.

        A select still showing the binding the form was read with is left
        alone: ``config`` is the file as it is NOW, and the form's copy may be
        stale. Written back regardless, it undid every binding changed since
        the tab was read — among them the re-pointing ``accounts remove`` does
        (``_retarget_bindings``) so that a removed slot's number is not
        inherited by the next login in that slot, run from the Accounts page
        of this same app, which the kept-alive tab never hears about. The next
        ``add`` reuses the slot, and the role launched on a stranger's login
        without a word (final review of #203, accounts F3).
        """
        for role in self._roles:
            value = self.query_one(f"#acct-{widget_suffix(role)}", Select).value
            chosen = value if isinstance(value, str) and value != NO_ACCOUNT else None
            if chosen == self._account_bindings.get(role):
                continue  # untouched on the form: the file's binding stands
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
        config.agents.default = self._optional_selection("#default-agent", config.agents.default)
        for role in self._roles:
            suffix = widget_suffix(role)
            profile = config.team.profiles.get(role, RoleLaunchProfile())
            family = self._optional_selection(f"#family-{suffix}", profile.agent)
            if family or role in config.team.profiles:
                config.team.profiles[role] = profile.model_copy(update={"agent": family})
            config.fleet.roles[role].sandbox = self._optional_selection(
                f"#sandbox-{suffix}", config.fleet.roles[role].sandbox
            )
            config.fleet.roles[role].approval_policy = self._optional_selection(
                f"#approval-{suffix}", config.fleet.roles[role].approval_policy
            )
            approve = self._selected(f"#afm-{suffix}")
            if approve is not None:  # a select with no value keeps the file's
                config.fleet.roles[role].approve_for_me = _APPROVE_VALUES.get(approve)
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
