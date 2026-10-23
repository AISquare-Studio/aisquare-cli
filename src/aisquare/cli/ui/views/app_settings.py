"""App settings: the machine-wide settings, whatever project is open.

Two tabs. *Agent settings* (shown first) is everything ``~/.aisquare/config.toml``
holds that is not about one project: permission mode, worktree, persona and account
per role, the fleet's escape key, agent cap, worktree root and native-teams switch,
how launches pick an account, what a usage limit does and what the accounts share.
*User preferences* is ``[ui]``, how ``asq`` shows things to the person at it: today
*Always show project directory* (``ui.always_show_project_path``). Save, Reload and
Reset all sit under the tabs and act on both. The project's own
Settings tab keeps what IS the project's (its nickname). Written through
``core.config.save_config`` — the one writer, which keeps only what differs
from the system defaults — and re-read
after every save so the form shows what the file holds. Precedence: a per-spawn
flag beats this file beats the system default; no value here is read from the
environment.

Reset all settings to default asks first, then moves the file aside
(``services.settings.reset_settings``) and names the backup it kept; moving it
back undoes the reset.

Model, effort and binary per role are NOT here on purpose: they live in
``team harness`` / ``team bind`` / ``AISQUARE_MODEL_<ROLE>`` — one home per
concept, which is the rule that deleted ``bins`` in #56. The one ``[team]``
value here is the ACCOUNT per role (#145): the select beside each role writes
``team.profiles.<role>.account`` — exactly what ``aisquare team bind <role>
--account`` writes — the middle rung of the account ladder a launch walks
(``--account`` > role binding > project default > machine default).

The persona per role (docs/plans/spawn-personas.md §3.8, P4) writes
``[fleet.roles.<role>].persona``, a machine-wide default, so the select lists the
personas every project can resolve: the user layer and the bundled ones
(``core.personas.catalogue(None)``), never one project's own. A configured name the
list lacks is shown as ``<name> (custom)``, so saving the form never silently drops
it; ``fleet spawn`` refuses it where it does not resolve, naming the key.

Save writes only the fields changed on the form since it was read, on either tab: the
file as it is then holds the rest, a ``config set`` or ``config reset`` made from a shell
meanwhile included.

Every re-read of the file (Save, Reload, Reset all) posts :class:`UiSettingsChanged`, so
the app, which holds ``[ui]`` for the sidebar rather than reading the file every tick,
follows at once.

Reached from *⚙ Settings ▸ App settings*: the Settings hub opens
``AppSettingsView(id="app-settings")``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import ClassVar, NamedTuple

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static, Switch, TabbedContent, TabPane
from textual.worker import Worker, WorkerState

from aisquare.core import claude_accounts as accounts_core
from aisquare.core import paths, personas
from aisquare.core.config import (
    CLAUDE_PERMISSION_MODES,
    AccountsSettings,
    AppConfig,
    FleetRoleSettings,
    FleetSettings,
    RoleLaunchProfile,
    UiSettings,
    load_config,
    save_config,
)
from aisquare.models import ClaudeAccount
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
_ID_KEPT = re.compile(r"[A-Za-z0-9-]")
"""The characters of a role name its widget ids keep as they are (:func:`widget_suffix`)."""
ACCOUNTS_WORKER = "app-settings-accounts"
"""The worker that reads the slots the account selects offer."""
_INPUTS = (
    "escape-key",
    "max-agents",
    "worktree-dir",
    "accounts-switch-at",
    "accounts-wait-minutes",
)
_SELECTS = ("accounts-pick", "accounts-on-limit")
"""The form's fields beside the per-role rows, by id: what :meth:`AppSettingsView._changed`
compares with what they showed when the form was read."""
_ACCOUNT_SWITCHES = {
    "accounts-share-trust": "share_trust",
    "accounts-share-settings": "share_settings",
}
"""``[accounts]``'s two switches, by id → field: what the accounts share
(``core.claude_sharing``)."""
_UI_SWITCHES = {"ui-always-show-path": "always_show_project_path"}
"""*User preferences*' switches, by id → ``[ui]`` field."""
AGENT_TAB = "tab-agent-settings"
"""The *Agent settings* tab's pane id: roles, the fleet, accounts. Shown first."""
PREFERENCES_TAB = "tab-user-preferences"
"""The *User preferences* tab's pane id: ``[ui]``."""


def role_order(fleet: FleetSettings) -> list[str]:
    """The fleet's roles first, in their order; any bound extra roles after, sorted."""
    known = [role for role in fleet_service.FLEET_ROLES if role in fleet.roles]
    extra = sorted(role for role in fleet.roles if role not in fleet_service.FLEET_ROLES)
    missing = [role for role in fleet_service.FLEET_ROLES if role not in fleet.roles]
    return known + missing + extra


def widget_suffix(role: str) -> str:
    """A role name as the tail of a widget id, one to one. Ids allow ``[A-Za-z0-9_-]`` only,
    so any other character, and ``_`` itself, is written ``_<code point in hex>_``.

    Each such character was ``-``, so ``qa.ui`` and ``qa-ui`` had one id, and so did
    ``测试`` and ``审查``. Every lookup found the first row's widgets: the second row was
    shown and never read, and Save wrote the first row's values to both roles (review of
    #259, round 22)."""
    return "".join(ch if _ID_KEPT.fullmatch(ch) else f"_{ord(ch):x}_" for ch in role)


ROLE_ID_PREFIX = "role-"
"""What every per-role widget id starts with, and no other id on the form does."""


def role_widget_id(kind: str, role: str) -> str:
    """The id of ``role``'s ``kind`` widget (``perm``, ``worktree``, ``persona``, ``acct``):
    ``role-<kind>-<suffix>``, :func:`widget_suffix` making the role part one to one.

    Under :data:`ROLE_ID_PREFIX`, so no role name can spell a fixed id. Written
    ``<kind>-<suffix>``, a role named ``dir`` gave its worktree switch the id
    ``worktree-dir``, the worktree root's ``Input``. The two sit in different rows, so
    Textual raised no ``DuplicateIds``, and ``query_one("#worktree-dir", Input)`` found
    the role's ``Switch`` first: ``WrongType`` out of ``on_mount``, and the fleet UI went
    down at App settings (review of #259, round 23)."""
    return f"{ROLE_ID_PREFIX}{kind}-{widget_suffix(role)}"


def permission_options(current: str) -> list[tuple[Text, str]]:
    """The mode list, with a value the list does not know appended so it still shows.

    Each prompt is :class:`Text`: a mode written by hand is free text, and a ``str`` prompt
    is read as markup, so ``x[/y]`` raised ``MarkupError`` when the row rendered (review of
    #259, round 23), as the role's own name did."""
    options = list(PERMISSION_MODES)
    if current not in {value for _, value in options}:
        options.append((f"{current} (custom)", current))
    return [(Text(label), value) for label, value in options]


NO_ACCOUNT = ""
"""The select's value for "this role expresses no preference" — the binding's ``account = None``."""


def account_options(accounts: list[ClaudeAccount], current: str | None) -> list[tuple[Text, str]]:
    """``(label, value)`` per account, plus the no-binding row first; ``current`` always shows.

    Values are SLOT NUMBERS as strings: the binding stores a reference the
    resolver reads, and the slot is the one spelling that cannot be renamed
    out from under it. A binding written by hand as an alias or an email is
    shown as its own row (``work (as bound)``) so the form never silently
    rewrites what the operator typed just by being opened and saved. Prompts are
    :class:`Text`, as :func:`permission_options`' are: an alias and a binding written by
    hand are free text.
    """
    options: list[tuple[str, str]] = [("(no account binding)", NO_ACCOUNT)]
    for account in accounts:
        identity = accounts_core.identity(account)
        who = f" · {identity.email}" if identity else " · not signed in"
        options.append((f"{account.slot} · {accounts_core.label(account)}{who}", str(account.slot)))
    if current and current not in {value for _, value in options}:
        options.append((f"{current} (as bound)", current))
    return [(Text(label), value) for label, value in options]


NO_PERSONA = ""
"""A role's ``(none)``: no ``persona`` key is written for it."""


def persona_options(current: str | None, names: Iterable[str]) -> list[tuple[Text, str]]:
    """``(none)`` and the persona names, with a configured name they lack appended so it
    shows. Prompts are :class:`Text`, as :func:`permission_options`' are: a name written
    by hand is free text."""
    options = [("(none)", NO_PERSONA), *((name, name) for name in names)]
    if current and current not in {value for _, value in options}:
        options.append((f"{current} (custom)", current))
    return [(Text(label), value) for label, value in options]


class RoleRow(NamedTuple):
    """The four fields of one role's row: permission mode, worktree, persona, account."""

    permission: Select[str]
    worktree: Switch
    persona: Select[str]
    account: Select[str]


class UiSettingsChanged(Message):
    """App settings re-read the file — after a Save, a Reload or a Reset all: ``[ui]`` as
    it now holds it, for the app, which holds what the sidebar reads of it."""

    def __init__(self, ui: UiSettings) -> None:
        super().__init__()
        self.ui = ui


def section_title(title: str, about: str) -> Text:
    """A section's header: its name in bold, then what it holds, dim."""
    return Text.assemble((title, "bold"), ("  " + about, "dim"))


class ConfirmReset(ModalScreen[bool]):
    """ "Reset every setting to its default?" — Reset or Cancel; Esc cancels."""

    CSS = """
    ConfirmReset { align: center middle; }
    #resetbox { width: 64; height: auto; border: heavy $error; background: $surface;
                padding: 1 2; }
    #resetbox Static { height: auto; margin-bottom: 1; }
    #resetbuttons { height: auto; }
    #resetbuttons Button { margin-right: 1; }
    """
    BINDINGS: ClassVar = [("escape", "cancel", "cancel")]

    def compose(self) -> ComposeResult:
        config = paths.config_path()
        with Vertical(id="resetbox"):
            yield Static(Text("Reset all settings to default?", style="bold"))
            yield Static(
                Text(
                    f"{config} moves aside to {config.name}.bak-<UTC date and time> beside "
                    "it, and every setting goes back to its default. Moving that file back "
                    "undoes the reset."
                ),
                id="reset-explained",
            )
            with Horizontal(id="resetbuttons"):
                yield Button("Reset all", id="reset-confirm", variant="error")
                yield Button("Cancel", id="reset-cancel")

    def on_mount(self) -> None:
        self.query_one("#reset-cancel", Button).focus()

    @on(Button.Pressed, "#reset-confirm")
    def _confirm(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#reset-cancel")
    def action_cancel(self) -> None:
        self.dismiss(False)


class AppSettingsView(Vertical):
    """The machine-wide settings form in two tabs — *Agent settings* (roles, fleet,
    accounts) and *User preferences* (``[ui]``) — and under them Save, Reload, Reset all."""

    DEFAULT_CSS = """
    AppSettingsView { padding: 1 2; }
    AppSettingsView #app-settings-tabs { height: 1fr; }
    AppSettingsView #app-settings-tabs > ContentSwitcher { height: 1fr; }
    AppSettingsView #app-settings-tabs TabPane { height: 1fr; padding: 0; }
    AppSettingsView .tab-body { height: 1fr; }
    AppSettingsView .row { height: auto; margin-bottom: 1; }
    AppSettingsView .row Label { width: 24; padding-top: 1; }
    AppSettingsView .row Select { width: 26; }
    AppSettingsView .row .account-select { width: 40; margin-left: 2; }
    AppSettingsView .row Input { width: 26; }
    AppSettingsView .row Switch { margin-left: 1; }
    AppSettingsView .row .worktree-label { width: 10; margin-left: 2; }
    AppSettingsView .row .persona-label { width: 9; margin-left: 2; }
    AppSettingsView .row .persona-select { width: 20; }
    AppSettingsView #settings-personas-unavailable { display: none; color: $warning; height: auto; }
    AppSettingsView #settings-personas-unavailable.shown { display: block; }
    AppSettingsView .section { margin: 1 0 1 0; height: auto; }
    AppSettingsView .row.preference { margin-bottom: 0; }
    AppSettingsView .row .preference-label { width: 32; }
    AppSettingsView .about { color: $text-muted; height: auto; margin-bottom: 1; }
    AppSettingsView #settings-buttons { margin: 1 0 0 0; }
    AppSettingsView #settings-buttons Button { margin-right: 1; }
    AppSettingsView #settings-note { color: $text-muted; height: auto; }
    """
    ROLE_SECTION: ClassVar[tuple[str, str]] = (
        "Roles",
        "permission mode, worktree, persona and account per role",
    )
    FLEET_SECTION: ClassVar[tuple[str, str]] = (
        "Fleet",
        "the escape key, the agent cap, where worktrees go",
    )
    ACCOUNTS_SECTION: ClassVar[tuple[str, str]] = (
        "Accounts",
        "how launches pick one, what a usage limit does, what they share",
    )
    SIDEBAR_SECTION: ClassVar[tuple[str, str]] = ("Sidebar", "how the project list reads")
    ALWAYS_SHOW_PATH_ABOUT: ClassVar[str] = (
        "Show each project's path under its name in the sidebar. "
        "Off: shown only when two projects share a name."
    )

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        config = self._read_config()
        self.fleet = config.fleet
        self.accounts = config.accounts
        self.ui = config.ui
        self._roles: list[str] = role_order(self.fleet)
        self._account_bindings: dict[str, str] = settings_service.role_account_bindings(config)
        self._accounts: list[ClaudeAccount] = []
        """The slots the account selects offer — filled by :meth:`_load_accounts`'s worker."""
        self._shown: dict[str, object] = {}
        """What each field showed when the form was (re)read: Save writes only what differs."""
        self._persona_names, self._personas_unavailable = self._read_persona_names()

    @staticmethod
    def _read_persona_names() -> tuple[list[str], str | None]:
        """The machine-wide persona names — or none, and the reason, which the form shows.

        No project: a role's default is machine-wide, so a persona only one project holds
        would refuse every other project's spawn of that role. ``catalogue`` never raises
        for one bad directory, so whatever reaches the ``except`` is a real failure (an
        unreadable layer, a bug). It costs the list, never the form.
        """
        try:
            found, _invalid = personas.catalogue(None)
        except Exception as exc:
            return [], f"personas unavailable — {type(exc).__name__}: {exc}"
        return [persona.name for persona in found], None

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
            row = self._role_row(role)
            if row is None:
                continue
            select = row.account
            shown = select.value
            bound = self._account_bindings.get(role)
            options = account_options(self._accounts, bound)
            select.set_options(options)
            offered = {value for _label, value in options}
            select.value = shown if shown in offered else (bound or NO_ACCOUNT)

    def _role_row(self, role: str) -> RoleRow | None:
        """``role``'s row on the form — or ``None`` for a role the file gained since the
        form was composed (a ``[fleet.roles.<role>]`` written by hand, then Reload or
        Save), which a reopen shows. Every reader of a role row goes through here: Save
        looked such a role's widgets up unguarded, and the ``NoMatches`` it raised out of
        the button handler took the fleet UI down (review of #259, round 21)."""
        try:
            return RoleRow(
                self.query_one(f"#{role_widget_id('perm', role)}", Select),
                self.query_one(f"#{role_widget_id('worktree', role)}", Switch),
                self.query_one(f"#{role_widget_id('persona', role)}", Select),
                self.query_one(f"#{role_widget_id('acct', role)}", Select),
            )
        except NoMatches:
            return None

    # --- layout ----------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static(Text("App settings", style="bold"), id="settings-title")
        with TabbedContent(id="app-settings-tabs", initial=AGENT_TAB):
            with (
                TabPane("Agent settings", id=AGENT_TAB),
                VerticalScroll(id="agent-settings", classes="tab-body"),
            ):
                yield from self._compose_roles()
                yield from self._compose_fleet()
                yield from self._compose_accounts()
            with (
                TabPane("User preferences", id=PREFERENCES_TAB),
                VerticalScroll(id="user-preferences", classes="tab-body"),
            ):
                yield from self._compose_preferences()
        with Horizontal(id="settings-buttons", classes="row"):
            yield Button("Save", id="save-settings", variant="primary")
            yield Button("Reload", id="reload-settings")
            yield Button("Reset all settings to default", id="reset-settings", variant="error")
        # The commands in bold, not in backticks: the note is a ``Text``, which shows them.
        yield Static(
            Text.assemble(
                f"Saved to {paths.config_path()}, only what differs from the defaults; a "
                "per-spawn flag still wins, and nothing here is read from the environment. "
                "Model, effort and binary per role: ",
                ("aisquare team harness", "bold"),
                " and ",
                ("team bind", "bold"),
                ". The machine and project account defaults: the Accounts page.",
            ),
            id="settings-note",
        )

    def _compose_roles(self) -> ComposeResult:
        yield Static(section_title(*self.ROLE_SECTION), classes="section")
        yield Static(
            Text(self._personas_unavailable or ""),
            id="settings-personas-unavailable",
            classes="shown" if self._personas_unavailable else "",
        )
        for role in self._roles:
            settings = self.fleet.roles.get(role, FleetRoleSettings())
            with Horizontal(classes="row"):
                # Not markup: role names are free TOML keys, `qa[x]` showed as `qa` and
                # `a[/b]c` raised MarkupError out of the render (review of #259, round 23).
                yield Label(role, markup=False)
                yield Select(
                    permission_options(settings.permission_mode),
                    value=settings.permission_mode,
                    allow_blank=False,
                    id=role_widget_id("perm", role),
                )
                yield Label("worktree", classes="worktree-label")
                yield Switch(settings.worktree, id=role_widget_id("worktree", role))
                yield Label("persona", classes="persona-label")
                yield Select(
                    persona_options(settings.persona, self._persona_names),
                    value=settings.persona or NO_PERSONA,
                    allow_blank=False,
                    id=role_widget_id("persona", role),
                    classes="persona-select",
                )
                bound = self._account_bindings.get(role)
                yield Select(
                    account_options(self._accounts, bound),
                    value=bound or NO_ACCOUNT,
                    allow_blank=False,
                    id=role_widget_id("acct", role),
                    classes="account-select",
                )

    def _compose_fleet(self) -> ComposeResult:
        yield Static(section_title(*self.FLEET_SECTION), classes="section")
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

    def _compose_accounts(self) -> ComposeResult:
        yield Static(section_title(*self.ACCOUNTS_SECTION), classes="section")
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
        with Horizontal(classes="row"):
            yield Label("share folder trust")
            yield Switch(self.accounts.share_trust, id="accounts-share-trust")
        with Horizontal(classes="row"):
            yield Label("share settings.json")
            yield Switch(self.accounts.share_settings, id="accounts-share-settings")

    def _compose_preferences(self) -> ComposeResult:
        yield Static(section_title(*self.SIDEBAR_SECTION), classes="section")
        with Horizontal(classes="row preference"):
            yield Label("Always show project directory", classes="preference-label")
            yield Switch(self.ui.always_show_project_path, id="ui-always-show-path")
        yield Static(
            Text(self.ALWAYS_SHOW_PATH_ABOUT), id="ui-always-show-path-about", classes="about"
        )

    def on_mount(self) -> None:
        self._shown = self._field_values()
        self._load_accounts()

    def _field_values(self) -> dict[str, object]:
        """Every field's value by its id — not the account selects, which keep their own
        record of what they were read with (``_account_bindings``)."""
        values: dict[str, object] = {}
        for role in self._roles:
            row = self._role_row(role)
            if row is None:
                continue
            values[role_widget_id("perm", role)] = row.permission.value
            values[role_widget_id("worktree", role)] = row.worktree.value
            values[role_widget_id("persona", role)] = row.persona.value
        for wid in _INPUTS:
            values[wid] = self.query_one(f"#{wid}", Input).value
        for wid in _SELECTS:
            values[wid] = self.query_one(f"#{wid}", Select).value
        values["native-teams"] = self.query_one("#native-teams", Switch).value
        for wid in (*_ACCOUNT_SWITCHES, *_UI_SWITCHES):
            values[wid] = self.query_one(f"#{wid}", Switch).value
        return values

    def _changed(self) -> set[str]:
        """The ids of the fields the operator changed since the form was (re)read.

        Save applies these and nothing else to the file as it is NOW. Applying the whole
        form, a form left open undid what a shell wrote meanwhile: a ``config set``, or
        a ``config reset`` whose every value came back from the form (review of #261, m7).
        """
        now = self._field_values()
        return {wid for wid, value in now.items() if value != self._shown.get(wid, value)}

    # --- reading -----------------------------------------------------------------------

    @on(Button.Pressed, "#reload-settings")
    def reload_form(self) -> None:
        """Discard edits on both tabs: show what the file holds (the roles list can change
        with it), and tell the app what ``[ui]`` holds now (:class:`UiSettingsChanged`)."""
        config = self._read_config()
        self.fleet = config.fleet
        self._roles = role_order(self.fleet)
        self._account_bindings = settings_service.role_account_bindings(config)
        self._persona_names, self._personas_unavailable = self._read_persona_names()
        unavailable = self.query_one("#settings-personas-unavailable", Static)
        unavailable.update(Text(self._personas_unavailable or ""))
        unavailable.set_class(bool(self._personas_unavailable), "shown")
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
        for wid, key in _ACCOUNT_SWITCHES.items():
            self.query_one(f"#{wid}", Switch).value = getattr(self.accounts, key)
        self.ui = config.ui
        for wid, key in _UI_SWITCHES.items():
            self.query_one(f"#{wid}", Switch).value = getattr(self.ui, key)
        for role in self._roles:
            row = self._role_row(role)
            if row is None:
                continue
            settings = self.fleet.roles.get(role, FleetRoleSettings())
            row.permission.set_options(permission_options(settings.permission_mode))
            row.permission.value = settings.permission_mode
            row.worktree.value = settings.worktree
            row.persona.set_options(persona_options(settings.persona, self._persona_names))
            row.persona.value = settings.persona or NO_PERSONA
            bound = self._account_bindings.get(role)
            row.account.set_options(account_options(self._accounts, bound))
            row.account.value = bound or NO_ACCOUNT
        self._shown = self._field_values()
        self._load_accounts()  # the slots as they are now, off the UI thread
        self.post_message(UiSettingsChanged(self.ui))

    # --- writing -----------------------------------------------------------------------

    def _form_fleet(self, current: FleetSettings, changed: set[str]) -> FleetSettings | str:
        """``current`` with the form's ``changed`` fields applied — or why they are not valid."""
        update: dict[str, object] = {}
        if "max-agents" in changed:
            raw = self.query_one("#max-agents", Input).value.strip()
            try:
                max_agents = int(raw)
            except ValueError:
                return f"max agents per project must be a whole number, not {raw!r}"
            if max_agents < 1:
                return "max agents per project must be at least 1"
            update["max_agents_per_project"] = max_agents
        roles = dict(current.roles)
        for role in self._roles:
            row = self._role_row(role)
            if row is None:
                continue
            role_update: dict[str, object] = {}
            value = row.permission.value
            if role_widget_id("perm", role) in changed and isinstance(value, str):
                role_update["permission_mode"] = value
            if role_widget_id("worktree", role) in changed:
                role_update["worktree"] = row.worktree.value
            chosen = row.persona.value
            if role_widget_id("persona", role) in changed and isinstance(chosen, str):
                role_update["persona"] = chosen or None
            if role_update:
                existing = current.roles.get(role, FleetRoleSettings())
                roles[role] = existing.model_copy(update=role_update)
        update["roles"] = roles
        if "escape-key" in changed:
            escape = self.query_one("#escape-key", Input).value.strip()
            update["escape_key"] = escape or DEFAULT_ESCAPE_KEY
        if "worktree-dir" in changed:
            root = self.query_one("#worktree-dir", Input).value.strip()
            update["worktree_dir"] = root or DEFAULT_WORKTREE_DIR
        if "native-teams" in changed:
            update["disable_native_agent_teams"] = self.query_one("#native-teams", Switch).value
        return current.model_copy(update=update)

    def _form_accounts(
        self, current: AccountsSettings, changed: set[str]
    ) -> AccountsSettings | str:
        """``current`` with the ``[accounts]`` fields ``changed`` applied — or why they are not
        valid."""
        switch_at, wait_minutes = current.switch_at, current.wait_if_reset_within_minutes
        try:
            if "accounts-switch-at" in changed:
                switch_at = int(self.query_one("#accounts-switch-at", Input).value.strip())
            if "accounts-wait-minutes" in changed:
                wait_minutes = int(self.query_one("#accounts-wait-minutes", Input).value.strip())
        except ValueError:
            return "switch at and wait-if-reset-within must be whole numbers"
        if not 1 <= switch_at <= 100:
            return "switch at must be between 1 and 100 (a percentage of a usage window)"
        if wait_minutes < 0:
            return "wait if reset within cannot be negative"
        update: dict[str, object] = {
            "switch_at": switch_at,
            "wait_if_reset_within_minutes": wait_minutes,
        }
        for wid, key in (("accounts-pick", "pick"), ("accounts-on-limit", "on_limit")):
            value = self.query_one(f"#{wid}", Select).value
            if wid in changed and isinstance(value, str):
                update[key] = value
        for wid, key in _ACCOUNT_SWITCHES.items():
            if wid in changed:
                update[key] = self.query_one(f"#{wid}", Switch).value
        return current.model_copy(update=update)

    def _form_ui(self, current: UiSettings, changed: set[str]) -> UiSettings:
        """``current`` with the *User preferences* fields ``changed`` applied."""
        update = {
            key: self.query_one(f"#{wid}", Switch).value
            for wid, key in _UI_SWITCHES.items()
            if wid in changed
        }
        return current.model_copy(update=update)

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
            row = self._role_row(role)
            if row is None:
                continue
            value = row.account.value
            chosen = value if isinstance(value, str) and value != NO_ACCOUNT else None
            if chosen == self._account_bindings.get(role):
                continue  # untouched on the form: the file's binding stands
            profile = config.team.profiles.get(role)
            if chosen is None:
                if profile is None:
                    continue
                profile.account = None
                if not profile.carries_launch:
                    config.team.profiles.pop(role, None)
            else:
                if profile is None:
                    profile = config.team.profiles.setdefault(role, RoleLaunchProfile())
                profile.account = chosen

    @on(Button.Pressed, "#save-settings")
    def _save_fleet_settings(self) -> None:
        """Write both tabs' changed fields through the one config writer, then show what
        landed."""
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
        changed = self._changed()
        result = self._form_fleet(config.fleet, changed)
        if isinstance(result, str):
            self.notify(result, severity="error", timeout=6, markup=False)
            return
        config.fleet = result
        self._apply_account_bindings(config)
        accounts = self._form_accounts(config.accounts, changed)
        if isinstance(accounts, str):
            self.notify(accounts, severity="error", timeout=6, markup=False)
            return
        config.accounts = accounts
        config.ui = self._form_ui(config.ui, changed)
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
        self.notify(f"✓ settings saved to {written}", timeout=5, markup=False)

    # --- reset -------------------------------------------------------------------------

    @on(Button.Pressed, "#reset-settings")
    def _ask_to_reset(self) -> None:
        """Every setting back to its default — once the operator says so in the dialog."""
        self.app.push_screen(ConfirmReset(), self._reset_answered)

    def _reset_answered(self, confirmed: bool | None) -> None:
        if not confirmed:
            return
        try:
            backup = settings_service.reset_settings()
        except OSError as exc:  # the operator's filesystem saying no: nothing moved
            self.notify(
                f"could not reset the settings: {exc}", severity="error", timeout=8, markup=False
            )
            return
        self.reload_form()
        if backup is None:
            self.notify(
                "nothing to reset: every setting was at its default already",
                timeout=5,
                markup=False,
            )
        else:
            self.notify(
                f"✓ every setting is back to its default; yours are kept in {backup}",
                timeout=8,
                markup=False,
            )
