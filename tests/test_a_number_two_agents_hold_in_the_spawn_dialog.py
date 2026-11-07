"""The Spawn dialog and its picker where two agents' accounts hold one number (#100's reopen, B).

The picker's Accounts rows were ``account:<number>``: two ``account:5`` options, and Textual's
DuplicateID took asq down as the picker painted (tester-r9f's failure 7). The dialog's
``_slot_agent`` and the picker's ``target_for`` took the first account of a number for either.
Now an account whose number another agent's holds is ``codex:5`` there: its option, the target
it picks, the value the dialog sends (a bare ``5`` is refused for both), and its agent.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

import pytest
from textual.app import App
from textual.pilot import Pilot
from textual.widgets import OptionList

from aisquare.cli.ui.attach import AttachTargetScreen, Target
from aisquare.cli.ui.spawn import SpawnDialog
from aisquare.models import AccountsOverview, ProjectInfo
from aisquare.services import fleet as fleet_service
from tests.test_ui_spawn import (  # noqa: F401 - the autouse fixtures and `git_project`, by name
    UNTOUCHED,
    SpawnRecorder,
    agents_overview,
    drive,
    git_project,
    no_real_tmux,
    offered,
    select,
    settle,
    spawns,
    teammates,
)

T = TypeVar("T")
CLAUDE, CODEX = "claude-code", "codex"


def collided() -> AccountsOverview:
    """Claude Code 2, Codex 5, and the Claude Code account 0.9.2 numbered 5 after a rollback."""
    return agents_overview((2, CLAUDE), (5, CODEX), (5, CLAUDE))


class Picker(App[None]):
    def __init__(self, project: ProjectInfo) -> None:
        super().__init__()
        self.project = project

    def on_mount(self) -> None:
        self.push_screen(
            AttachTargetScreen(self.project, persona=None, intent="new", accounts=collided)
        )


def _picked(
    project: ProjectInfo, scenario: Callable[[AttachTargetScreen], Coroutine[Any, Any, T]]
) -> T:
    async def run() -> T:
        host = Picker(project)
        async with host.run_test(size=(120, 50)) as pilot:
            await settle(pilot)
            picker = host.screen
            assert isinstance(picker, AttachTargetScreen)
            return await scenario(picker)

    return asyncio.run(run())


def test_the_pickers_accounts_list_both_and_each_picks_its_own(
    git_project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fleet_service, "list_agents", lambda project, **_: [])

    async def scenario(picker: AttachTargetScreen) -> list[Any]:
        listing = picker.query_one("#picker-list", OptionList)
        ids = [listing.get_option_at_index(i).id for i in range(listing.option_count)]
        return [
            [oid for oid in ids if (oid or "").startswith("account:")],
            picker.target_for("account:codex:5"),
            picker.target_for("account:claude-code:5"),
            picker.target_for("account:2"),
        ]

    ids, codex, claude, two = _picked(git_project, scenario)

    assert ids == ["account:2", "account:codex:5", "account:claude-code:5"]
    assert codex == Target("account", "codex:5", agent=CODEX, account="codex:5")
    assert claude == Target("account", "claude-code:5", agent=CLAUDE, account="claude-code:5")
    assert two == Target("account", "2", agent=CLAUDE, account="2"), "an unshared one as it was"


def test_the_dialog_offers_each_agent_its_own_and_spawns_on_the_one_picked(
    git_project: ProjectInfo,  # noqa: F811
    spawns: SpawnRecorder,  # noqa: F811
) -> None:
    async def scenario(pilot: Pilot[None], host: Any, dialog: SpawnDialog) -> list[Any]:
        seen: list[Any] = [offered(dialog, "account")]
        select(dialog, "agent").value = CODEX
        await settle(pilot)
        seen.append(offered(dialog, "account"))
        select(dialog, "account").value = "codex:5"
        await pilot.click("#spawn-submit")
        await settle(pilot)
        return seen

    claude, codex = drive(git_project, scenario, accounts=collided)

    assert claude == ["", "2", "claude-code:5"], "the role's agent: Claude Code's, 5 named as its"
    assert codex == ["", "codex:5"]
    [(_, role, kwargs)] = spawns.calls
    assert role == "coder" and kwargs == {**UNTOUCHED, "agent": CODEX, "account": "codex:5"}


def test_a_preset_codex_5_brings_codex_and_a_bare_5_brings_neither(
    git_project: ProjectInfo,  # noqa: F811
    spawns: SpawnRecorder,  # noqa: F811
) -> None:
    async def scenario(pilot: Pilot[None], host: Any, dialog: SpawnDialog) -> list[Any]:
        return [
            select(dialog, "agent").value,
            select(dialog, "account").value,
            dialog._slot_agent("codex:5"),
            dialog._slot_agent("claude-code:5"),
            dialog._slot_agent("5"),
            dialog._slot_agent("2"),
        ]

    preset = drive(git_project, scenario, accounts=collided, presets={"account": "codex:5"})
    bare = drive(git_project, scenario, accounts=collided, presets={"account": "5"})

    assert preset[:2] == [CODEX, "codex:5"]
    assert preset[2:] == [CODEX, CLAUDE, None, CLAUDE], "a shared bare number names neither"
    assert bare[0] != CODEX, "never the first account the number happened to list"
