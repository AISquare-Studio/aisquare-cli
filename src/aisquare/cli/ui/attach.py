"""The target picker — who runs a persona: the second of two steps.

docs/plans/spawn-personas.md §4.6 and §4.7 (P7). One filterable list in three
sections, in the order the intent asks for: **Agents** (this project's live
agents, each with the persona it runs), **Binds** (the seats ``aisquare team
bind`` pinned in ``team.profiles``, each with the account its environment points
at) and **Accounts** (every coding agent's account slots, each naming its agent:
slots are one ladder). ``existing`` puts Agents
first and highlights the first agent; ``new`` puts Binds, then Accounts, then
Agents. Every section stays selectable either way — the owner's "the same
screen allows both" — and the filter narrows all three at once, because a
machine with two dozen binds needs one.

The picker answers WHO; it does not act. It dismisses with a :class:`Target`
and the screen that opened it decides what that means — the Personas tab
attaches to an agent (``fleet_service.attach_persona``) or opens the Spawn
dialog preset for a bind or an account; the Spawn dialog's *Pick…* fills its
own fields — so there is one picker and no second spawn or attach path.

Two footer actions make a target without leaving the flow. **+ New bind** is
:class:`NewBindScreen`, saved through ``services.settings.bind_role`` — the
writer ``aisquare team bind`` uses — after which the list is read again with the
new bind selected. Its Agent field picks the coding agent (blank: the binary's), and
the Binary and Account fields follow it: that agent's binary until one is typed, and
its slots only, whose environment is the agent's own (``CODEX_HOME`` for a Codex slot,
``claude_accounts.launch_env``). **+ New account** dismisses with a ``new-account`` target; its
owner posts :class:`NewAccountRequested` and the app opens the Accounts page's
own add-account flow. That is a navigation: the page does not hand the flow back
to the picker when the sign-in lands (v1, recorded in the plan's §10).

The rows are read in a thread worker — ``list_agents`` asks tmux and the accounts
read opens every slot's files — so the screen opens at once. Every read is
looked up on its module at call time, so a test replaces each with a recorder.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Literal

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Select, Static, TextArea
from textual.widgets.option_list import Option
from textual.worker import Worker, WorkerState

from aisquare.core import claude_accounts as accounts_core
from aisquare.core.agent_adapters import DEFAULT_AGENT, adapter_for_binary, adapters, get_adapter
from aisquare.core.config import RoleLaunchProfile, load_config
from aisquare.core.names import name_key
from aisquare.models import AccountsOverview, ClaudeAccountStatus, FleetAgentStatus, ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services import settings as settings_service

Intent = Literal["existing", "new"]
TargetKind = Literal["agent", "bind", "account", "new-account"]
Section = Literal["agents", "binds", "accounts"]

SECTION_ORDER: dict[Intent, tuple[Section, ...]] = {
    "existing": ("agents", "binds", "accounts"),
    "new": ("binds", "accounts", "agents"),
}
SECTION_TITLES: dict[Section, str] = {
    "agents": "Agents — attach to a running agent",
    "binds": "Binds — spawn as a bound teammate",
    "accounts": "Accounts — spawn on an account",
}

TARGETS_WORKER = "attach-targets"

SEAT_RULE = (
    "a seat is a role (coder, tester, …), a numbered seat of one (coder2), or a name "
    "already bound — the names `aisquare launch` accepts"
)
ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
BINARYS_AGENT = ""
"""The New bind form's Agent ``(the binary's)``: no ``--agent``, the binary says which."""


class NewAccountRequested(Message):
    """Open the Accounts page's add-account flow — the picker's *+ New account*."""


@dataclass(frozen=True)
class Target:
    """Who runs it: a live agent, a bound seat, an account slot — or a slot still to add."""

    kind: TargetKind
    key: str = ""
    """The agent's label, the bind's seat, or the account's slot."""
    role: str | None = None
    agent: str | None = None
    """The coding agent: a live agent's, a bind's own ``--agent``, an account's (its slot is
    that agent's alone); ``None`` leaves it to the role."""
    binary: str | None = None
    account: str | None = None
    persona: str | None = None
    """For an agent: the persona it runs now — what an attach replaces."""


@dataclass(frozen=True)
class Targets:
    """One read of everything the picker lists."""

    agents: list[FleetAgentStatus] = field(default_factory=list)
    binds: dict[str, RoleLaunchProfile] = field(default_factory=dict)
    accounts: list[ClaudeAccountStatus] | None = None
    """``None`` when this picker was given no accounts reader, or the read failed."""
    problems: list[str] = field(default_factory=list)


def read_targets(project: ProjectInfo, accounts: Callable[[], AccountsOverview] | None) -> Targets:
    """Agents, binds and accounts — each read fails open, with its reason kept."""
    problems: list[str] = []
    try:
        agents = [s for s in fleet_service.list_agents(project) if s.agent.ended_at is None]
    except Exception as exc:  # tmux or the store said no: the other sections still help
        agents = []
        problems.append(f"agents unavailable — {type(exc).__name__}: {exc}")
    try:
        binds = dict(load_config().team.profiles)
    except Exception as exc:  # a broken config costs the binds, never the picker
        binds = {}
        problems.append(f"binds unavailable — {type(exc).__name__}: {exc}")
    statuses: list[ClaudeAccountStatus] | None = None
    if accounts is not None:
        try:
            statuses = list(accounts().accounts)
        except Exception as exc:  # a slot we cannot read costs the section
            problems.append(f"accounts unavailable — {type(exc).__name__}: {exc}")
    return Targets(agents=agents, binds=binds, accounts=statuses, problems=problems)


def agent_persona(status: FleetAgentStatus) -> str | None:
    """The persona an agent runs: its fleet row's, else its session's."""
    if status.agent.persona:
        return status.agent.persona
    return status.session.persona if status.session is not None else None


def agent_text(status: FleetAgentStatus) -> str:
    """``coder-auth · coder · waiting · skeptic`` — the badge only when there is a persona."""
    agent = status.agent
    persona = agent_persona(status)
    badge = f" · {persona}" if persona else ""
    return f"{agent.label} · {agent.role} · {status.state}{badge}"


def account_text(status: ClaudeAccountStatus, *, agent: bool = False) -> str:
    """``2 · me@example.com · max · session 12%`` — slot, who, plan, usage when read: its
    window that limits, by name (``week 90%``, ``gemini-2.5-pro daily 70%``), the figure the
    sidebar shows beside its agents (``ClaudeUsage.ranked_percent``); with ``agent``, the
    slot's agent after its number (``5 · Codex · …``), for a list that holds more than one
    agent's slots."""
    who = status.identity.email if status.identity is not None else "not signed in"
    parts = [str(status.account.slot), who, status.subscription or "plan unknown"]
    if agent:
        parts.insert(1, accounts_core.agent_label(status.account.agent))
    usage = status.usage
    limiting = usage.limiting if usage is not None else None
    if usage is not None and limiting is not None and usage.ranked_percent is not None:
        parts.append(f"{limiting.name} {usage.ranked_percent:.0f}%")
    return " · ".join(parts)


def bind_account(profile: RoleLaunchProfile, statuses: Iterable[ClaudeAccountStatus] | None) -> str:
    """Which account a bind launches on: its ``account`` (``team bind --account``, which a
    launch resolves after ``env`` and so runs on), else the home variable of the agent
    whose slot its environment names (``CLAUDE_CONFIG_DIR``, ``CODEX_HOME``, …)."""
    listed = list(statuses or ())
    if profile.account:
        return _account_named(profile.account.strip(), profile.agent, listed)
    for agent in accounts_core.account_agents():
        raw = profile.env.get(accounts_core.kind(agent).home_env, "").strip()
        if not raw:
            continue
        directory = Path(os.path.expandvars(os.path.expanduser(raw)))
        slot = accounts_core.managed_slot(directory, agent)
        if slot is None:
            return directory.name or raw
        return _account_text(slot, listed)
    return "this shell's account"


def _account_named(ref: str, agent: str | None, statuses: list[ClaudeAccountStatus]) -> str:
    """``account 3 · me@example.com`` for a bind's ``account``: a slot, an alias or an
    email, matched in the accounts already read as ``services.claude_accounts.resolve``
    matches them (the bind's agent's first); ``account <ref>`` when none is."""
    ordered = sorted(statuses, key=lambda status: status.account.agent != agent)
    for status in ordered:
        account, identity = status.account, status.identity
        if (
            account.slot == accounts_core.slot_number(ref)
            or (account.alias is not None and name_key(account.alias) == name_key(ref))
            or (identity is not None and "@" in ref and identity.email.lower() == ref.lower())
        ):
            return _account_text(account.slot, statuses)
    return f"account {ref}"


def _account_text(slot: int, statuses: list[ClaudeAccountStatus]) -> str:
    for status in statuses:
        if status.account.slot == slot and status.identity is not None:
            return f"account {slot} · {status.identity.email}"
    return f"account {slot}"


def bind_binary(profile: RoleLaunchProfile) -> str:
    """What a bind runs: its own binary, else its agent's, else the default agent's."""
    return profile.bin or get_adapter(profile.agent or DEFAULT_AGENT).binary


def bind_text(
    seat: str, profile: RoleLaunchProfile, statuses: Iterable[ClaudeAccountStatus] | None
) -> str:
    """``coder2 · claude2 · account 2 · me@example.com`` — seat, binary, account."""
    return f"{seat} · {bind_binary(profile)} · {bind_account(profile, statuses)}"


_BOX_CSS = """
{name} {{ align: center middle; }}
{name} > Vertical {{ width: 96; max-width: 96%; height: auto; max-height: 94%;
                     border: heavy $accent; background: $surface; padding: 0 1; }}
{name} .picker-header {{ height: auto; padding: 1 0; }}
{name} .picker-row {{ height: auto; }}
{name} .picker-row > Label {{ width: 18; padding-top: 1; }}
{name} .picker-row > Input {{ width: 1fr; }}
{name} .picker-row > Select {{ width: 1fr; }}
{name} .picker-note {{ height: auto; }}
{name} .picker-buttons {{ height: auto; align-horizontal: right; padding-bottom: 1; }}
{name} .picker-buttons Button {{ margin-left: 2; }}
"""


class AttachTargetScreen(ModalScreen[Target | None]):
    """Agents · Binds · Accounts, ordered by intent; dismisses with the chosen :class:`Target`."""

    DEFAULT_CSS = _BOX_CSS.format(name="AttachTargetScreen") + (
        "AttachTargetScreen #picker-list { height: 22; }"
    )
    BINDINGS: ClassVar = [Binding("escape", "cancel", "cancel")]

    def __init__(
        self,
        project: ProjectInfo,
        *,
        persona: str | None,
        intent: Intent,
        accounts: Callable[[], AccountsOverview] | None,
    ) -> None:
        super().__init__()
        self.project = project
        self.persona_name = persona
        self.attach_intent: Intent = intent
        self._accounts = accounts
        self.targets = Targets()
        self._wanted: str | None = None
        """The option id to highlight after the next read (a bind just created)."""

    def _header(self) -> Text:
        text = Text()
        if self.persona_name:
            text.append(f"Attach {self.persona_name}", style="bold")
            text.append(" — who runs it?", style="bold")
        else:
            text.append("Who runs the new agent?", style="bold")
        text.append(
            "\nan agent that is running now, or a new one as a bound teammate or on an account",
            style="dim",
        )
        return text

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._header(), classes="picker-header")
            yield Input(placeholder="filter agents, binds and accounts", id="picker-filter")
            yield OptionList(id="picker-list")
            yield Static(id="picker-status", classes="picker-note")
            with Horizontal(classes="picker-buttons"):
                yield Button("+ New bind", id="picker-new-bind")
                yield Button("+ New account", id="picker-new-account")
                yield Button("Cancel", id="picker-cancel")

    def on_mount(self) -> None:
        self._say("reading agents, binds and accounts …", "dim")
        self.read()
        self.query_one("#picker-list", OptionList).focus()

    def read(self, *, select: str | None = None) -> None:
        """Read every section again in a worker; ``select`` is an option id to land on."""
        self._wanted = select
        project, accounts = self.project, self._accounts
        self.run_worker(
            lambda: read_targets(project, accounts),
            name=TARGETS_WORKER,
            group=TARGETS_WORKER,
            exclusive=True,
            thread=True,
            exit_on_error=False,
        )

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name != TARGETS_WORKER:
            return
        if event.state is WorkerState.SUCCESS and isinstance(event.worker.result, Targets):
            self.targets = event.worker.result
            problems = list(self.targets.problems)
            if self._accounts is None:
                problems.append("accounts are not read from here — + New account opens them")
            self._say("\n".join(problems) or None, "dim")
            self.paint()
        elif event.state is WorkerState.ERROR:
            self._say(f"{type(event.worker.error).__name__}: {event.worker.error}", "red")

    def _say(self, text: str | None, style: str) -> None:
        status = self.query_one("#picker-status", Static)
        status.update(Text(text, style=style) if text else "")
        status.display = bool(text)

    def rows(self, section: Section) -> list[tuple[str, str]]:
        """``(option id, text)`` for one section, before the filter."""
        targets = self.targets
        if section == "agents":
            return [(f"agent:{s.agent.label}", agent_text(s)) for s in targets.agents]
        if section == "binds":
            return [
                (f"bind:{seat}", bind_text(seat, profile, targets.accounts))
                for seat, profile in sorted(targets.binds.items())
            ]
        statuses = targets.accounts or []
        mixed = len({s.account.agent for s in statuses}) > 1
        return [(f"account:{s.account.slot}", account_text(s, agent=mixed)) for s in statuses]

    def paint(self) -> None:
        query = self.query_one("#picker-filter", Input).value.strip().lower()
        options: list[Option] = []
        for section in SECTION_ORDER[self.attach_intent]:
            options.append(
                Option(
                    Text(SECTION_TITLES[section], style="bold"),
                    id=f"section:{section}",
                    disabled=True,
                )
            )
            rows = [(oid, text) for oid, text in self.rows(section) if query in text.lower()]
            if not rows:
                options.append(
                    Option(Text("  (none)", style="dim"), id=f"empty:{section}", disabled=True)
                )
            options.extend(Option(f"  {text}", id=oid) for oid, text in rows)
        listing = self.query_one("#picker-list", OptionList)
        listing.clear_options()
        listing.add_options(options)
        ids = [option.id for option in options]
        enabled = [i for i, option in enumerate(options) if not option.disabled]
        if self._wanted in ids:
            listing.highlighted = ids.index(self._wanted)
        elif enabled:
            listing.highlighted = enabled[0]

    @on(Input.Changed, "#picker-filter")
    def _filtered(self) -> None:
        self.paint()

    def target_for(self, option_id: str) -> Target | None:
        kind, _, key = option_id.partition(":")
        if kind == "agent":
            status = next((s for s in self.targets.agents if s.agent.label == key), None)
            if status is None:
                return None
            agent = status.agent
            return Target(
                "agent",
                key,
                role=agent.role,
                agent=agent.agent,
                binary=agent.binary,
                persona=agent_persona(status),
            )
        if kind == "bind":
            profile = self.targets.binds.get(key)
            return Target(
                "bind",
                key,
                role=key,
                agent=profile.agent if profile else None,
                binary=profile.bin if profile else None,
            )
        if kind == "account":
            slot = next(
                (s for s in self.targets.accounts or [] if str(s.account.slot) == key), None
            )
            return Target("account", key, agent=slot.account.agent if slot else None, account=key)
        return None

    @on(OptionList.OptionSelected, "#picker-list")
    def _chosen(self, event: OptionList.OptionSelected) -> None:
        target = self.target_for(event.option.id or "")
        if target is not None:
            self.dismiss(target)

    @on(Button.Pressed, "#picker-new-bind")
    def _new_bind(self) -> None:
        self.app.push_screen(
            NewBindScreen(self.targets.accounts or []),
            callback=lambda seat: self.read(select=f"bind:{seat}") if seat else None,
        )

    @on(Button.Pressed, "#picker-new-account")
    def _new_account(self) -> None:
        self.dismiss(Target("new-account"))

    @on(Button.Pressed, "#picker-cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class NewBindScreen(ModalScreen[str | None]):
    """``aisquare team bind`` as a form; dismisses with the seat it saved, or ``None``."""

    DEFAULT_CSS = _BOX_CSS.format(name="NewBindScreen") + "NewBindScreen #bind-env { height: 4; }"
    BINDINGS: ClassVar = [Binding("escape", "cancel", "cancel")]

    def __init__(self, accounts: list[ClaudeAccountStatus]) -> None:
        super().__init__()
        self._statuses = accounts
        self._binary_shown = get_adapter(DEFAULT_AGENT).binary
        """The binary the form itself last put in the field; any other is the user's."""
        self._offered = self._account_options(DEFAULT_AGENT)
        """What the Account field offers now (:meth:`_offer_accounts`)."""

    def compose(self) -> ComposeResult:
        with Vertical():
            header = Text("New bind", style="bold")
            header.append(
                " — a seat `aisquare team bind` pins: binary, environment, args", style="dim"
            )
            yield Static(header, classes="picker-header")
            with Horizontal(classes="picker-row"):
                yield Label("Seat")
                yield Input(placeholder="coder2", id="bind-seat")
            with Horizontal(classes="picker-row"):
                yield Label("Agent")
                yield Select(
                    [("(the binary's)", BINARYS_AGENT), *((a.label, a.id) for a in adapters())],
                    value=BINARYS_AGENT,
                    allow_blank=False,
                    id="bind-agent",
                )
            with Horizontal(classes="picker-row"):
                yield Label("Binary")
                yield Input(value=self._binary_shown, id="bind-binary")
            with Horizontal(classes="picker-row"):
                yield Label("Account")
                yield Select(
                    self._account_options(DEFAULT_AGENT),
                    value="",
                    allow_blank=False,
                    id="bind-account",
                )
            with Horizontal(classes="picker-row"):
                yield Label("Env (KEY=VALUE)")
                yield TextArea(id="bind-env")
            with Horizontal(classes="picker-row"):
                yield Label("Extra args")
                yield Input(placeholder="--model opus", id="bind-args")
            yield Static(id="bind-rule", classes="picker-note")
            yield Static(id="bind-status", classes="picker-note")
            with Horizontal(classes="picker-buttons"):
                yield Button("Save", id="bind-save", variant="primary")
                yield Button("Cancel", id="bind-cancel")

    def on_mount(self) -> None:
        self.query_one("#bind-status", Static).display = False
        self._validate()
        self.query_one("#bind-seat", Input).focus()

    def _agent_value(self) -> str:
        value = self.query_one("#bind-agent", Select).value
        return value if isinstance(value, str) else BINARYS_AGENT

    def bind_agent(self) -> str:
        """The agent the bind runs: the one picked, else the binary's, else the default."""
        binary = self.query_one("#bind-binary", Input).value.strip()
        adapter = adapter_for_binary(binary) if binary else None
        return self._agent_value() or (adapter.id if adapter is not None else DEFAULT_AGENT)

    def _account_options(self, agent: str) -> list[tuple[str, str]]:
        """``agent``'s slots: a bind's account is set through that agent's home variable."""
        return [("(leave the account alone)", "")] + [
            (account_text(s), str(s.account.slot))
            for s in self._statuses
            if s.account.agent == agent
        ]

    @on(Select.Changed, "#bind-agent")
    def _agent_changed(self) -> None:
        """The binary follows the agent until one is typed, and the accounts are its own."""
        picked = self._agent_value()
        binary = self.query_one("#bind-binary", Input)
        if picked and binary.value.strip() == self._binary_shown:
            self._binary_shown = get_adapter(picked).binary
            binary.value = self._binary_shown
        self._offer_accounts()

    @on(Input.Changed, "#bind-binary")
    def _binary_changed(self) -> None:
        self._offer_accounts()

    def _offer_accounts(self) -> None:
        select = self.query_one("#bind-account", Select)
        options = self._account_options(self.bind_agent())
        if options == self._offered:
            return
        self._offered = options
        current = select.value
        select.set_options(options)
        select.value = current if current in {value for _, value in options} else ""

    def problems(self) -> list[str]:
        """Every rule the form breaks right now, in field order."""
        found: list[str] = []
        seat = self.query_one("#bind-seat", Input).value.strip()
        # `role_ok` IS the rule spawn applies, by its public name (P13): ask it, never copy it.
        if not seat or not fleet_service.role_ok(seat):
            found.append(SEAT_RULE)
        binary = self.query_one("#bind-binary", Input).value.strip() or self._binary_shown
        if shutil.which(binary) is None:
            found.append(
                f"{binary!r} is not on your PATH — install it or give the executable's path"
            )
        for number, line in enumerate(self.query_one("#bind-env", TextArea).text.splitlines(), 1):
            if not line.strip():
                continue
            key, separator, _value = line.partition("=")
            if not separator or not ENV_KEY.match(key.strip()):
                found.append(f"env line {number}: KEY=VALUE, with KEY a shell variable name")
        try:
            shlex.split(self.query_one("#bind-args", Input).value)
        except ValueError as exc:
            found.append(f"extra args: {exc}")
        return found

    def _validate(self) -> bool:
        found = self.problems()
        rule = self.query_one("#bind-rule", Static)
        rule.update(Text("\n".join(f"✗ {p}" for p in found), style="red") if found else "")
        rule.display = bool(found)
        self.query_one("#bind-save", Button).disabled = bool(found)
        return not found

    @on(Input.Changed)
    @on(TextArea.Changed, "#bind-env")
    def _changed(self) -> None:
        self._validate()

    def bind_kwargs(self) -> tuple[str, dict[str, Any]]:
        """The seat, and exactly what ``bind_role`` is called with."""
        env: dict[str, str] = {}
        slot = self.query_one("#bind-account", Select).value
        status = next((s for s in self._statuses if str(s.account.slot) == slot), None)
        if status is not None:
            # What `launch --account <slot>` sets — none for the default slot.
            env.update(accounts_core.launch_env(status.account))
        for line in self.query_one("#bind-env", TextArea).text.splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip():
                env[key.strip()] = value.strip()
        kwargs: dict[str, Any] = {
            "agent_bin": self.query_one("#bind-binary", Input).value.strip() or self._binary_shown,
            "env": env,
            "args": shlex.split(self.query_one("#bind-args", Input).value),
        }
        if self._agent_value():
            kwargs["agent"] = self._agent_value()  # picked: `team bind --agent`
        return self.query_one("#bind-seat", Input).value.strip(), kwargs

    @on(Button.Pressed, "#bind-save")
    def _save(self) -> None:
        if not self._validate():
            return
        seat, kwargs = self.bind_kwargs()
        try:
            settings_service.bind_role(seat, **kwargs)
        except Exception as exc:  # the config could not be written: say so, stay open
            status = self.query_one("#bind-status", Static)
            status.update(Text(f"{type(exc).__name__}: {exc}", style="bold red"))
            status.display = True
            return
        self.dismiss(seat)

    @on(Button.Pressed, "#bind-cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmAttachScreen(ModalScreen[bool]):
    """One question before a running agent's persona changes; ``True`` attaches."""

    DEFAULT_CSS = _BOX_CSS.format(name="ConfirmAttachScreen")
    BINDINGS: ClassVar = [Binding("escape", "cancel", "cancel")]

    def __init__(self, persona: str, label: str, replaces: str | None) -> None:
        super().__init__()
        self.persona_name = persona
        self.agent_label = label
        self.replaces = replaces

    def compose(self) -> ComposeResult:
        text = Text()
        text.append(f"Attach {self.persona_name} to {self.agent_label}?\n", style="bold")
        if self.replaces and self.replaces != self.persona_name:
            text.append(f"It replaces {self.replaces}.\n")
        text.append(
            "A waiting agent has the briefing typed now; a busy one gets it as a board note.",
            style="dim",
        )
        with Vertical():
            yield Static(text, classes="picker-header", id="attach-question")
            with Horizontal(classes="picker-buttons"):
                yield Button("Attach", id="attach-confirm", variant="primary")
                yield Button("Cancel", id="attach-cancel")

    @on(Button.Pressed, "#attach-confirm")
    def _confirm(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#attach-cancel")
    def action_cancel(self) -> None:
        self.dismiss(False)
