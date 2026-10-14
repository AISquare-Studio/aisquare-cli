"""Start a terminal teammate using the shared fleet service.

The dialog is ``aisquare fleet spawn`` with its choices as controls: the role, the
coding agent (``--agent``; blank is the role's, the project's or the user's default),
the account (``--account``; blank is the account ladder's answer), the permission —
one ``WRITE:APPROVALS`` value every agent maps to its own flags (``--permission-mode
workspace:auto``), or the agent's own vocabulary (Claude Code's ``--permission-mode``;
Codex's ``--sandbox`` and ``--approval``); blank is the role's setting — a label and a
first task. It launches through ``services.fleet.spawn``, the call the command makes, with
the same arguments, so a refusal reads as the command's.

The controls follow the agent the launch will start: the one picked, else the one
``agent_launch.resolve`` gives the role in this project. Only that agent's accounts
are offered (a launch refuses another agent's), and only its permission controls
are shown. What the role's settings would pass is under them (``settings.launch_line``).
"""

from __future__ import annotations

import asyncio
from typing import ClassVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static
from textual.worker import Worker, WorkerState

from aisquare.cli.ui.views.settings import (
    APPROVAL_OPTIONS,
    INTENTS,
    SANDBOX_OPTIONS,
    launch_line,
)
from aisquare.core import claude_accounts as accounts_core
from aisquare.core.agent_adapters import adapters, get_adapter
from aisquare.core.config import CLAUDE_PERMISSION_MODES, FleetRoleSettings
from aisquare.models import ClaudeAccount, ProjectInfo
from aisquare.services import agent_launch, fleet
from aisquare.services import claude_accounts as accounts_service

DEFAULT = ""
"""Every select's "let the role, the project or the machine decide" value."""
INTENT_CHOICES: list[tuple[str, str]] = [
    ("Permission (every agent): role default", DEFAULT),
    *((intent, intent) for intent in INTENTS),
]
"""One value every agent maps to its own flags; chosen, it replaces the agent's own below."""
PERMISSION_OPTIONS: list[tuple[str, str]] = [
    ("Permission: role default", DEFAULT),
    *((mode, mode) for mode in CLAUDE_PERMISSION_MODES),
]
SPAWN_SANDBOX_OPTIONS: list[tuple[str, str]] = [
    ("Sandbox: role default", DEFAULT),
    *SANDBOX_OPTIONS[1:],
]
SPAWN_APPROVAL_OPTIONS: list[tuple[str, str]] = [
    ("Approval: role default", DEFAULT),
    *APPROVAL_OPTIONS[1:],
]
FACTS_WORKER = "spawn-facts"
"""The worker that reads the role's agent and the accounts, off the UI thread."""


def account_choices(accounts: list[ClaudeAccount], agent: str) -> list[tuple[str, str]]:
    """``(label, slot)`` for ``agent``'s accounts, after the ladder's own answer."""
    choices: list[tuple[str, str]] = [("Account: role / project / machine default", DEFAULT)]
    for account in accounts:
        if account.agent != agent:
            continue  # the launch refuses another agent's account for this one
        identity = accounts_core.identity(account)
        who = identity.email if identity else "not signed in"
        choices.append(
            (f"{account.slot} · {accounts_core.label(account)} · {who}", str(account.slot))
        )
    return choices


class SpawnScreen(ModalScreen[fleet.SpawnReceipt | None]):
    DEFAULT_CSS = """
    SpawnScreen { align: center middle; }
    SpawnScreen > Vertical {
        width: 72; height: auto; padding: 1 2; border: round $accent; background: $surface;
    }
    SpawnScreen Select, SpawnScreen Input { margin-bottom: 1; }
    SpawnScreen #spawn-launch { height: auto; margin-bottom: 1; }
    SpawnScreen #spawn-error { color: $error; height: auto; }
    """
    BINDINGS: ClassVar = [("escape", "cancel", "Cancel")]

    def __init__(self, project: ProjectInfo) -> None:
        super().__init__()
        self.project = project
        self.busy = False
        self.role_agents: dict[str, str] = {}
        """The agent each role launches on with no ``--agent``, as ``agent_launch`` says."""
        self.accounts: list[ClaudeAccount] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"Start a teammate in {self.project.root.name}")
            yield Select(
                [(role, role) for role in fleet.FLEET_ROLES],
                value="coder",
                allow_blank=False,
                id="spawn-role",
            )
            yield Select(
                [("Use role / project default", DEFAULT), *((a.label, a.id) for a in adapters())],
                value=DEFAULT,
                allow_blank=False,
                id="spawn-family",
            )
            yield Select(
                account_choices([], accounts_core.AGENT),
                value=DEFAULT,
                allow_blank=False,
                id="spawn-account",
            )
            yield Select(INTENT_CHOICES, value=DEFAULT, allow_blank=False, id="spawn-intent")
            yield Select(
                PERMISSION_OPTIONS, value=DEFAULT, allow_blank=False, id="spawn-permission"
            )
            yield Select(
                SPAWN_SANDBOX_OPTIONS, value=DEFAULT, allow_blank=False, id="spawn-sandbox"
            )
            yield Select(
                SPAWN_APPROVAL_OPTIONS, value=DEFAULT, allow_blank=False, id="spawn-approval"
            )
            yield Static("", id="spawn-launch")
            yield Input(placeholder="Label (optional)", id="spawn-label")
            yield Input(placeholder="Initial task (optional)", id="spawn-prompt")
            yield Static("", id="spawn-error", markup=False)
            yield Button("Start agent", variant="primary", id="spawn-submit")
            yield Button("Cancel", id="spawn-cancel")

    def on_mount(self) -> None:
        self._paint()
        root = self.project.root

        def facts() -> tuple[dict[str, str], list[ClaudeAccount]]:
            agents: dict[str, str] = {}
            for role in fleet.FLEET_ROLES:
                try:
                    agents[role] = agent_launch.resolve(role, cwd=root).adapter.id
                except Exception:  # the spawn says why; the dialog still opens
                    continue
            try:
                accounts = accounts_service.list_accounts()
            except Exception:  # no accounts to offer is the ladder's choice, not a crash
                accounts = []
            return agents, accounts

        self.run_worker(
            facts, name=FACTS_WORKER, group=FACTS_WORKER, thread=True, exit_on_error=False
        )

    @on(Worker.StateChanged)
    def _facts_read(self, event: Worker.StateChanged) -> None:
        if event.worker.group != FACTS_WORKER or event.state is not WorkerState.SUCCESS:
            return
        result = event.worker.result
        if isinstance(result, tuple):
            self.role_agents, self.accounts = result
            self._paint()

    # --- the agent the launch starts ------------------------------------------------

    def _value(self, selector: str) -> str:
        value = self.query_one(selector, Select).value
        return value if isinstance(value, str) else DEFAULT

    def launch_agent(self) -> str:
        """The agent this launch starts: the one picked, else the role's."""
        picked = self._value("#spawn-family")
        role = self._value("#spawn-role")
        return picked or self.role_agents.get(role) or accounts_core.AGENT

    def _paint(self) -> None:
        """Offer what fits the agent the launch starts: its accounts, its permission words."""
        agent = self.launch_agent()
        try:
            adapter = get_adapter(agent)
            sandboxed = adapter.capabilities.sandbox_permissions
        except ValueError:
            sandboxed = False
        # One value for every agent replaces the agent's own words, which then hide.
        intent = self._value("#spawn-intent")
        self.query_one("#spawn-permission", Select).display = not sandboxed and not intent
        self.query_one("#spawn-sandbox", Select).display = sandboxed and not intent
        self.query_one("#spawn-approval", Select).display = sandboxed and not intent
        account = self.query_one("#spawn-account", Select)
        shown = account.value
        options = account_choices(self.accounts, agent)
        account.set_options(options)
        account.value = shown if shown in {value for _, value in options} else DEFAULT
        role = self._value("#spawn-role")
        try:
            settings = fleet.role_settings(role)
        except Exception:  # a config that will not parse: the spawn says why
            settings = FleetRoleSettings()
        overrides: dict[str, object] = {
            "permission_mode": self._value("#spawn-permission") or settings.permission_mode,
            "sandbox": self._value("#spawn-sandbox") or settings.sandbox,
            "approval_policy": self._value("#spawn-approval") or settings.approval_policy,
        }
        if intent:
            overrides["permission"] = intent
        elif (self._value("#spawn-permission") and not sandboxed) or (
            sandboxed and (self._value("#spawn-sandbox") or self._value("#spawn-approval"))
        ):
            # The agent's own words chosen for this spawn win over the role's one value,
            # as they do in `fleet spawn`.
            overrides["permission"] = None
        if self._value("#spawn-sandbox") or self._value("#spawn-approval"):
            # A sandbox or a policy chosen for this spawn wins over the role's setting.
            overrides["approve_for_me"] = None
        line = launch_line(role, agent, settings.model_copy(update=overrides))
        self.query_one("#spawn-launch", Static).update(line)

    @on(Select.Changed)
    def _changed(self, event: Select.Changed) -> None:
        if event.select.id != "spawn-account":  # the account changes no other control
            self._paint()

    # --- buttons ------------------------------------------------------------------------

    def action_cancel(self) -> None:
        if not self.busy:
            self.dismiss(None)

    @on(Button.Pressed, "#spawn-cancel")
    def cancel(self) -> None:
        self.action_cancel()

    @on(Button.Pressed, "#spawn-submit")
    async def submit(self) -> None:
        if self.busy:
            return
        role = self._value("#spawn-role")
        family = self._value("#spawn-family") or None
        account = self._value("#spawn-account") or None
        label = self.query_one("#spawn-label", Input).value.strip() or None
        prompt = self.query_one("#spawn-prompt", Input).value.strip() or None
        try:
            sandboxed = get_adapter(self.launch_agent()).capabilities.sandbox_permissions
        except ValueError:
            sandboxed = False
        # One value for every agent, as `fleet spawn --permission-mode WRITE:APPROVALS`
        # takes it; else each agent's own words: --permission-mode for Claude Code,
        # --sandbox and --approval for Codex. The other agent's are never sent.
        intent = self._value("#spawn-intent") or None
        permission: str | None
        sandbox: str | None
        approval: str | None
        if intent is not None:
            permission, sandbox, approval = intent, None, None
        else:
            permission = None if sandboxed else (self._value("#spawn-permission") or None)
            sandbox = (self._value("#spawn-sandbox") or None) if sandboxed else None
            approval = (self._value("#spawn-approval") or None) if sandboxed else None
        self.busy = True
        self.query_one("#spawn-submit", Button).disabled = True
        try:
            receipt = await asyncio.to_thread(
                fleet.spawn,
                self.project,
                role,
                agent=family,
                account=account,
                permission_mode=permission,
                sandbox=sandbox,
                approval_policy=approval,
                label=label,
                prompt=prompt,
            )
        except Exception as exc:
            self.query_one("#spawn-error", Static).update(Text(str(exc)))
        else:
            self.dismiss(receipt)
        finally:
            self.busy = False
            self.query_one("#spawn-submit", Button).disabled = False
