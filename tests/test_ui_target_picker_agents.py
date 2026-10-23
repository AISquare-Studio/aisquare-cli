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
from textual.app import App
from textual.pilot import Pilot
from textual.widgets import Button, Input, OptionList, Select

from aisquare.cli.ui.attach import AttachTargetScreen, NewBindScreen, Target
from aisquare.core import claude_accounts as accounts_core
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
from tests.ui_workers import settle_page

T = TypeVar("T")


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


async def wait_for(pilot: Pilot[Any], kind: type[Any]) -> Any:
    for _ in range(100):
        if isinstance(pilot.app.screen, kind):
            return pilot.app.screen
        await pilot.pause(0.05)
    raise AssertionError(f"{kind.__name__} never opened")


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
    assert shown["account:2"] == "2 · Claude Code · slot2@example.com · plan unknown"
    assert shown["account:5"] == "5 · Codex · slot5@example.com · plan unknown"
    # The bind's account is read from its agent's home variable, not CLAUDE_CONFIG_DIR.
    assert shown["bind:codex2"] == "codex2 · codex · account 5 · slot5@example.com"
    assert account == Target("account", "5", agent="codex", account="5")
    assert bind == Target("bind", "codex2", role="codex2", agent="codex", binary="codex")


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
        form.query_one("#bind-agent", Select).value = "codex"
        await pilot.pause()
        seen += [form.query_one("#bind-binary", Input).value, [v for _, v in accounts._options]]
        accounts.value = "5"
        form.query_one("#bind-seat", Input).value = "coder4"
        await pilot.pause()
        form.query_one("#bind-save", Button).press()
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
        for value in ("codex", "claude-code"):
            agent.value = value
            await pilot.pause()
            seen.append(binary.value)
        binary.value = "my-codex"
        agent.value = "codex"
        await pilot.pause()
        seen.append(binary.value)
        return seen

    assert drive(project, scenario) == ["codex", "claude", "my-codex"]
