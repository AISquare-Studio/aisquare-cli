"""The target picker on every coding agent (SCOPE #113 0.8.2, A6).

The picker answers who runs a new agent — a bind or an account — and its Accounts section
was Claude Code's slots alone, a bind's account was read from ``CLAUDE_CONFIG_DIR`` only,
and *+ New bind* defaulted its binary to ``claude``. Now: every agent's slots are listed
(slots are one ladder), each naming its agent when the list holds more than one agent's;
a bind's account is read from the home variable of the agent whose slot it names; and
*+ New bind* has an Agent field that the Binary and Account fields follow, so a bind on a
Codex slot carries ``CODEX_HOME`` (``claude_accounts.launch_env``), never
``CLAUDE_CONFIG_DIR``.

Every read is a recorder: no tmux, no real account, no write outside the isolated config.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any, TypeVar

import pytest
from rich.text import Text
from textual.app import App
from textual.pilot import Pilot
from textual.screen import Screen
from textual.widgets import Button, Input, OptionList, Select

from aisquare.cli.ui.attach import AttachTargetScreen, NewBindScreen, Target, args_hint
from aisquare.core import claude_accounts as accounts_core
from aisquare.core.agent_adapters import get_adapter
from aisquare.core.config import RoleLaunchProfile
from aisquare.core.orchestrator import team_project
from aisquare.models import (
    AccountsOverview,
    ClaudeAccount,
    ClaudeAccountStatus,
    ClaudeIdentity,
    ClaudeInstall,
    ProjectInfo,
)
from aisquare.services import fleet as fleet_service
from aisquare.services import settings as settings_service
from tests.test_captain_sidebar import until
from tests.ui_workers import settle_page, settle_until_held

T = TypeVar("T")
S = TypeVar("S", bound=Screen[Any])


def _slot(slot: int, agent: str) -> ClaudeAccountStatus:
    account = ClaudeAccount(
        slot=slot,
        config_dir=accounts_core.accounts_root(agent) / str(slot),
        managed=True,
        agent=agent,
    )
    return ClaudeAccountStatus(
        account=account,
        label=f"account {slot}",
        identity=ClaudeIdentity(email=f"slot{slot}@example.com"),
        signed_in=True,
    )


def mixed() -> AccountsOverview:
    """A Claude Code slot and a Codex slot, as ``claude_accounts.overview`` lists them."""
    return AccountsOverview(
        claude=ClaudeInstall(installed=True, binary="/usr/bin/claude"),
        accounts=[_slot(2, "claude-code"), _slot(5, "codex")],
    )


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProjectInfo:
    monkeypatch.setattr(fleet_service, "list_agents", lambda project, **_: [])
    # Every binary the forms name is "on the PATH": the rule is tested where it lives.
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: f"/usr/bin/{name}")
    return team_project(tmp_path)


class Host(App[None]):
    def __init__(self, project: ProjectInfo) -> None:
        super().__init__()
        self.project = project
        self.results: list[Target | None] = []

    def on_mount(self) -> None:
        self.push_screen(
            AttachTargetScreen(self.project, persona=None, intent="new", accounts=mixed),
            callback=self.results.append,
        )


def drive(
    project: ProjectInfo, scenario: Callable[[Pilot[None], Host], Coroutine[Any, Any, T]]
) -> T:
    async def run() -> T:
        host = Host(project)
        async with host.run_test(size=(120, 50)) as pilot:
            await settle_page(host)
            return await scenario(pilot, host)

    return asyncio.run(run())


def prompts(picker: AttachTargetScreen) -> dict[str, str]:
    listing = picker.query_one("#picker-list", OptionList)
    options = [listing.get_option_at_index(i) for i in range(listing.option_count)]
    return {option.id or "": str(option.prompt).strip() for option in options}


async def wait_for(pilot: Pilot[Any], kind: type[S]) -> S:
    """The screen of ``kind`` once it is up AND mounted whole, every Select's label in it.

    ``app.screen`` is a pushed screen as soon as it is on the stack, and Textual mounts its
    widgets in later turns of the loop. The New bind form came back half mounted, so a test
    set ``#bind-agent`` before the Select had what it shows, and lost two ways (task #79):
    on windows-latest the Select had no SelectCurrent yet, so its watcher posted no Changed
    and the binary never followed (shard 8, CI 41826716083 at e1778119); on py3.12 the test
    went on to its end at once and ``run_test`` tore down the form, whose ``#bind-account``
    Select then mounted on a pruned SelectCurrent: ``NoMatches('#label')`` (shard 2, CI
    41828194627 at 36ebb6aa). A screen is mounted after every widget in it is, and a Select
    shows its value in its SelectCurrent's ``#label``: both are waited for here.
    """

    def mounted() -> bool:
        screen = pilot.app.screen
        return (
            isinstance(screen, kind)
            and screen.is_mounted
            and all(select.query("#label") for select in screen.query(Select))
        )

    await settle_until_held(pilot.app, mounted, what=f"{kind.__name__} mounted", timeout=10)
    screen = pilot.app.screen
    assert isinstance(screen, kind)
    return screen


def test_the_picker_lists_codex_slots_and_reads_a_binds_codex_home(
    project: ProjectInfo,
) -> None:
    codex_home = accounts_core.accounts_root("codex") / "5"
    settings_service.bind_role(
        "codex2", agent="codex", agent_bin="codex", env={"CODEX_HOME": str(codex_home)}
    )

    async def scenario(pilot: Pilot[None], host: Host) -> list[Any]:
        picker = host.screen
        assert isinstance(picker, AttachTargetScreen)
        return [
            prompts(picker),
            picker.target_for("account:5"),
            picker.target_for("bind:codex2"),
        ]

    shown, account, bind = drive(project, scenario)
    assert shown["section:accounts"] == "Accounts — spawn on an account"
    # Each agent's row by its mark and its name (``AgentAdapter.mark``), the mark in its colour.
    assert shown["account:2"] == "2 · ✻ Claude Code · slot2@example.com · plan unknown"
    assert shown["account:5"] == "5 · >_ Codex · slot5@example.com · plan unknown"
    # The bind's account is read from its agent's home variable, not CLAUDE_CONFIG_DIR.
    assert shown["bind:codex2"] == "codex2 · codex · account 5 · slot5@example.com"
    assert account == Target("account", "5", agent="codex", account="5")
    assert bind == Target("bind", "codex2", role="codex2", agent="codex", binary="codex")


def test_the_picker_draws_each_agents_mark_in_the_marks_colour(project: ProjectInfo) -> None:
    """R16-C: where the picker lists both agents' accounts, each account row's mark is drawn in
    its agent's colour (``AgentAdapter.mark``) and nothing else of the row is styled. The row
    under the highlight draws it in the bar's own colours, as the rest of the row: on the bar
    a mark read at 1.1:1 to 2.6:1, Codex's blue one lost in textual-dark's blue bar. The row the
    highlight leaves has its colour back. A list of Claude Code's accounts alone draws its rows
    from their text, as 0.9.2 did (``tests/test_ui_personas.py``: ``1 · me@example.com · max``)."""

    def styled(listing: OptionList) -> dict[str, list[tuple[str, str]]]:
        options = [listing.get_option_at_index(i) for i in range(listing.option_count)]
        drawn: dict[str, list[tuple[str, str]]] = {}
        for option in options:
            if (option.id or "").startswith("account:"):
                prompt = option.prompt
                assert isinstance(prompt, Text), prompt
                drawn[option.id or ""] = [
                    (prompt.plain[span.start : span.end], str(span.style)) for span in prompt.spans
                ]
        return drawn

    async def scenario(pilot: Pilot[None], host: Host) -> list[Any]:
        listing = host.screen.query_one("#picker-list", OptionList)
        first = listing.highlighted_option
        seen: list[Any] = [first.id if first else None, styled(listing)]
        listing.highlighted = listing.get_option_index("account:5")
        await pilot.pause()
        await settle_page(host)
        drawn = {oid: str(listing.get_option(oid).prompt) for oid in ("account:2", "account:5")}
        return [*seen, styled(listing), drawn]

    first, at_rest, moved, drawn = drive(project, scenario)
    assert first == "account:2"
    assert at_rest == {"account:2": [], "account:5": [(">_", "#6BA1F8")]}  # textual-dark's
    assert moved == {"account:2": [("✻", "#DF8B70")], "account:5": []}
    # Unstripped: the row under the bar and the row it left keep the list's two-cell indent,
    # so neither moves sideways (the R16-C verify's mutant M4, seq 11914).
    assert drawn == {
        "account:2": "  2 · ✻ Claude Code · slot2@example.com · plan unknown",
        "account:5": "  5 · >_ Codex · slot5@example.com · plan unknown",
    }


def test_a_new_bind_on_a_codex_slot_carries_codex_home_and_its_agent(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved: list[tuple[str, dict[str, Any]]] = []
    real_bind = settings_service.bind_role

    def bind_role(role: str, **kwargs: Any) -> Any:
        saved.append((role, kwargs))
        return real_bind(role, **kwargs)

    monkeypatch.setattr(settings_service, "bind_role", bind_role)

    async def scenario(pilot: Pilot[None], host: Host) -> list[Any]:
        picker = host.screen
        assert isinstance(picker, AttachTargetScreen)
        await pilot.click("#picker-new-bind")
        form = await wait_for(pilot, NewBindScreen)
        accounts = form.query_one("#bind-account", Select)
        seen: list[Any] = [
            form.query_one("#bind-binary", Input).value,
            [value for _, value in accounts._options],
        ]
        binary = form.query_one("#bind-binary", Input)
        form.query_one("#bind-agent", Select).value = "codex"
        # The binary and the accounts follow on the Select's Changed, a later turn of the loop
        # (``test_new_binds_binary_follows_the_agent_until_one_is_typed`` says the same): one
        # pause returned before it on CI (py3.11, run 40479198453) and read Claude Code's binary.
        # So each step waits for its effect, not for a pause.
        await until(
            pilot,
            lambda: binary.value == "codex" and [v for _, v in accounts._options] == ["", "5"],
            what="codex's binary and accounts",
        )
        seen += [binary.value, [v for _, v in accounts._options]]
        accounts.value = "5"
        form.query_one("#bind-seat", Input).value = "coder4"
        # The form is checked on the inputs' Changed, a later turn too; a disabled Save ignores
        # the press.
        save = form.query_one("#bind-save", Button)
        await until(pilot, lambda: not save.disabled, what="the form valid")
        save.press()
        await wait_for(pilot, AttachTargetScreen)
        await settle_page(host)
        listing = picker.query_one("#picker-list", OptionList)
        assert listing.highlighted is not None
        seen += [
            listing.get_option_at_index(listing.highlighted).id,
            prompts(picker)["bind:coder4"],
        ]
        return seen

    seen = drive(project, scenario)
    # The binary and the accounts follow the agent: Claude Code's first, then Codex's.
    assert seen[:4] == ["claude", ["", "2"], "codex", ["", "5"]]
    codex_home = str(accounts_core.accounts_root("codex") / "5")
    assert saved == [
        (
            "coder4",
            {"agent_bin": "codex", "env": {"CODEX_HOME": codex_home}, "args": [], "agent": "codex"},
        )
    ]
    assert seen[4:] == ["bind:coder4", "coder4 · codex · account 5 · slot5@example.com"]


def test_new_binds_binary_follows_the_agent_until_one_is_typed(project: ProjectInfo) -> None:
    async def scenario(pilot: Pilot[None], host: Host) -> list[str]:
        await pilot.click("#picker-new-bind")
        form = await wait_for(pilot, NewBindScreen)
        agent = form.query_one("#bind-agent", Select)
        binary = form.query_one("#bind-binary", Input)
        seen: list[str] = []
        # The binary follows on the Select's Changed, a later turn of the loop: on Windows CI
        # one pause returned before it (run 40134430454 read 'claude' after picking Codex),
        # so each step waits for its effect (``test_captain_sidebar.until``'s rule).

        async def follows(value: str) -> str:
            agent.value = value
            wanted = get_adapter(value).binary
            await until(pilot, lambda: binary.value == wanted, what=f"{value}'s binary")
            return str(binary.value)

        seen += [await follows("codex"), await follows("claude-code")]
        binary.value = "my-codex"
        agent.value = "codex"
        # Typed, it stays: wait until Codex is the bind's agent, then let the handler run.
        await until(pilot, lambda: form.bind_agent() == "codex", what="codex picked")
        await settle_page(pilot.app)
        seen.append(binary.value)
        return seen

    assert drive(project, scenario) == ["codex", "claude", "my-codex"]


def test_new_binds_extra_args_example_is_the_agents_own_model_flag(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """r20 of #113, N1's sibling: the New bind's Extra args read ``--model opus``, a Claude
    Code model, with Codex picked. Codex's is its own ``model_args``; Claude Code's is 0.9.2's
    bare ``--model opus`` (its ``args_example``, C1 reopen, seq 11753).

    Both agents spell the flag ``--model``, so their examples read alike: the wait for
    Codex's held before the pick was handled, and the test ended with the form half mounted
    (task #79). Here Codex spells it ``-m``, its short flag, so the example is Codex's only
    once the form has followed the pick."""
    codex = get_adapter("codex")
    monkeypatch.setattr(codex, "model_args", lambda model, effort: ["-m", model] if model else [])
    assert args_hint("codex", bare=True) != args_hint("claude-code", bare=True)

    async def scenario(pilot: Pilot[None], host: Host) -> list[str]:
        await pilot.click("#picker-new-bind")
        form = await wait_for(pilot, NewBindScreen)
        field = form.query_one("#bind-args", Input)
        seen = [str(field.placeholder)]
        form.query_one("#bind-agent", Select).value = "codex"
        wanted = args_hint("codex", bare=True)
        await until(pilot, lambda: field.placeholder == wanted, what="codex's example")
        return [*seen, str(field.placeholder)]

    shown = drive(project, scenario)
    assert shown == ["--model opus", "e.g. -m MODEL"]  # 0.9.2's, then Codex's own


class BindHost(App[None]):
    """The *New bind* / *Edit bind* form alone, over ``accounts``."""

    def __init__(
        self,
        accounts: list[ClaudeAccountStatus],
        editing: tuple[str, RoleLaunchProfile] | None,
    ) -> None:
        super().__init__()
        self.accounts = accounts
        self.editing = editing

    def on_mount(self) -> None:
        self.push_screen(NewBindScreen(self.accounts, editing=self.editing))


@pytest.mark.parametrize(
    ("agents", "editing", "shown"),
    [
        (("claude-code",), None, False),  # a Claude-only machine's form is 0.9.2's
        (("claude-code", "codex"), None, True),
        (("claude-code",), ("coder2", RoleLaunchProfile(agent="codex", bin="codex")), True),
        (("claude-code",), ("coder3", RoleLaunchProfile(bin="codex")), True),
        (("claude-code",), ("coder4", RoleLaunchProfile(env={"FOO": "1"})), False),
    ],
    ids=["claude-only", "two-agents", "bound-to-codex", "codex-binary", "claude-binding"],
)
def test_the_bind_form_offers_an_agent_only_beside_another_agents_accounts(
    project: ProjectInfo,
    agents: tuple[str, ...],
    editing: tuple[str, RoleLaunchProfile] | None,
    shown: bool,
) -> None:
    """C1's audit of #120: the *Agent* row was on every New/Edit bind form, offering *Codex
    (beta)* on a machine with Claude Code's accounts alone. It is offered where the accounts
    section names agents (more than one agent's listed) or the binding runs another agent;
    elsewhere the form is 0.9.2's and the bind runs its binary's agent."""
    accounts = [_slot(2 + index, agent) for index, agent in enumerate(agents)]

    async def run() -> tuple[bool, str]:
        host = BindHost(accounts, editing)
        async with host.run_test(size=(120, 50)) as pilot:
            form = await wait_for(pilot, NewBindScreen)
            return bool(form.query_one("#bind-agent-row").display), form.bind_agent()

    row, agent = asyncio.run(run())
    assert row is shown
    if editing is None and not shown:
        assert agent == "claude-code", "the binary's agent, as 0.9.2 ran it"
