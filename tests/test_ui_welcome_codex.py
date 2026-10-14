"""Welcome's step 2 and 3 for Codex, driven headless through Textual's Pilot.

The page with every seam scripted (``tests.test_ui_welcome.Machine``) in each state a
Codex-only or both-agents machine can be in: installed or not, signed in or not,
connected or not, hooks switched off, the trust step pending, and the choice between
two installed agents. One test runs the real shell for what only it does: *Sign in*
opens the Accounts page with ``codex login`` started on the manager's account. Each
behaviour's control is the other state, or Claude Code's page unchanged.
"""

from __future__ import annotations

import dataclasses
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from textual.pilot import Pilot

from aisquare.cli.ui.app import FleetApp
from aisquare.cli.ui.sidebar import AccountsSelected
from aisquare.cli.ui.views.accounts import AccountsView
from aisquare.cli.ui.views.welcome import WelcomeView, agent_button, claude_text
from aisquare.core import claude_accounts as accounts_core
from aisquare.models import FleetAgentStatus
from aisquare.services import first_run
from aisquare.services.first_run import ClaudeState
from tests.test_ui_welcome import (  # noqa: F401  (fixtures: pytest finds them by name here)
    MISSING,
    READY,
    Host,
    Machine,
    _ready_machine,
    card,
    fleet_rows,
    hosted,
    in_shell,
    no_real_tmux,
    press,
    scripted,
    visible,
)
from tests.ui_workers import settle_page

HOME = Path("/home/dev/.codex")
CODEX_MISSING = ClaudeState(wanted="codex", source="default", agent="codex", home=HOME)
CODEX_UNHOOKED = dataclasses.replace(
    CODEX_MISSING,
    binary="/opt/bin/codex",
    version="0.162.1",
    signed_in=True,
    account="0 · plain codex",
    account_slot=0,
    installed=("codex",),
    inferred=True,
)
CODEX_PENDING = dataclasses.replace(CODEX_UNHOOKED, connected=True, trust_pending=True)
CODEX_TRUSTED = dataclasses.replace(CODEX_PENDING, trust_pending=False)
BOTH = dataclasses.replace(READY, installed=("claude-code", "codex"), choosable=True)
NEITHER = dataclasses.replace(MISSING, choosable=True)


def _step_two(page: WelcomeView) -> tuple[str, str]:
    return card(page, "claude-title"), card(page, "claude-status")


async def _look(pilot: Pilot[None], page: WelcomeView, host: Host) -> tuple[str, str]:
    return _step_two(page)


# --------------------------------------------------------------------------- the four facts


def test_a_codex_only_machine_shows_codex_its_version_sign_in_hooks_and_trust_step() -> None:
    title, text = hosted(Machine(claude=[CODEX_PENDING]), _look)
    assert title.startswith("2  Codex") and "✓" in title
    assert "✓ Codex 0.162.1 · /opt/bin/codex" in text
    assert "signed in (0 · plain codex)" in text
    assert "✓ connected" in text and str(HOME) in text
    assert "/hooks" in text and "trust them" in text
    assert "Claude Code" not in title + text
    # Control: once a hook ran, the trust line is gone and the rest stands.
    _title, trusted = hosted(Machine(claude=[CODEX_TRUSTED]), _look)
    assert "/hooks" not in trusted and "✓ connected" in trusted


def test_the_hooks_home_is_named_in_full_as_this_platform_spells_it() -> None:
    """On the Windows runner the account home sat under the user's folder and step 2 named
    it ``~/AppData/.../codex-accounts/2``, a spelling neither cmd nor Explorer takes; it is
    named in full, as the binary and a hooks-off config.toml are, wherever it lives."""
    home = Path.home() / ".aisquare" / "codex-accounts" / "2"
    for state in (CODEX_UNHOOKED, CODEX_TRUSTED):
        text = claude_text(dataclasses.replace(state, home=home), platform=sys.platform).plain
        assert f" in {home}" in text and "~" not in text, text


def test_codex_missing_names_its_installer_and_check_again() -> None:
    async def go(pilot: Pilot[None], page: WelcomeView, host: Host) -> tuple[str, bool, bool]:
        return (
            card(page, "claude-status"),
            visible(page, "claude-check"),
            visible(page, "fleet-manager"),
        )

    text, check, start = hosted(Machine(claude=[CODEX_MISSING]), go)
    assert "Codex is not installed" in text
    assert text.index(first_run.CODEX_INSTALL_COMMAND) < text.index("npm install -g @openai/codex")
    assert accounts_core.INSTALL_COMMAND not in text  # Claude Code's install is not offered
    assert check and not start


def test_with_neither_installed_both_install_commands_are_named() -> None:
    title, text = hosted(Machine(claude=[NEITHER]), _look)
    assert title.startswith("2  Coding agent")
    assert "No coding agent is installed" in text
    assert accounts_core.INSTALL_COMMAND in text and first_run.CODEX_INSTALL_COMMAND in text
    assert text.index("Claude Code:") < text.index(accounts_core.INSTALL_COMMAND)
    assert text.index("Codex:") < text.index(first_run.CODEX_INSTALL_COMMAND)
    # Control: Claude Code chosen by name (not choosable) names its own install only.
    _title, claude_only = hosted(Machine(claude=[MISSING]), _look)
    assert first_run.CODEX_INSTALL_COMMAND not in claude_only


def test_connect_runs_for_codex_and_opens_step_three(tmp_path: Path) -> None:
    machine, _ = _ready_machine(tmp_path, claude=[CODEX_UNHOOKED])

    async def go(pilot: Pilot[None], page: WelcomeView, host: Host) -> tuple[bool, bool, str]:
        before = visible(page, "fleet-manager")
        machine.claude = [CODEX_PENDING]
        await press(pilot, page, "claude-connect")
        return before, visible(page, "fleet-manager"), card(page, "claude-status")

    waiting, ready, text = hosted(machine, go)
    assert not waiting and ready  # step 3 waited for the hooks, not for the trust step
    assert [state.agent for state in machine.connected_states if state] == ["codex"]
    assert "✓ connected" in text


def test_hooks_switched_off_are_named_instead_of_offering_connect() -> None:
    switch = f"`hooks = false` under [features] in {HOME / 'config.toml'}"
    off = dataclasses.replace(CODEX_UNHOOKED, switch=switch)

    async def go(pilot: Pilot[None], page: WelcomeView, host: Host) -> tuple[str, bool]:
        return card(page, "claude-status"), visible(page, "claude-connect")

    text, connect = hosted(Machine(claude=[off]), go)
    assert switch in text and "Connect cannot change that" in text and not connect
    assert hosted(Machine(claude=[CODEX_UNHOOKED]), go)[1]  # control: missing hooks connect


def test_sign_in_asks_for_the_managers_account_and_opens_accounts_on_it() -> None:
    unsigned = dataclasses.replace(CODEX_PENDING, signed_in=False)
    machine = Machine(claude=[unsigned], sign_in_slot=0)

    async def go(pilot: Pilot[None], page: WelcomeView, host: Host) -> tuple[str, list[Any]]:
        text = card(page, "claude-status")
        await press(pilot, page, "claude-sign-in")
        return text, list(host.received)

    text, received = hosted(machine, go)
    assert "not signed in (0 · plain codex)" in text and "`codex login`" in text
    assert [state.agent for state in machine.sign_ins] == ["codex"]
    opened = [m for m in received if isinstance(m, AccountsSelected)]
    assert len(opened) == 1 and opened[0].sign_in == 0
    # Control: Claude Code's Sign in only opens the page, as it always did.
    claude = dataclasses.replace(READY, signed_in=False)
    plain = Machine(claude=[claude])
    _text, claude_received = hosted(plain, go)
    assert not plain.sign_ins
    assert [m.sign_in for m in claude_received if isinstance(m, AccountsSelected)] == [None]


# --------------------------------------------------------------------------- the choice


def test_with_both_installed_the_pick_is_offered_and_saved_as_the_default() -> None:
    machine = Machine(claude=[BOTH], after_choice=[CODEX_TRUSTED])

    async def go(pilot: Pilot[None], page: WelcomeView, host: Host) -> tuple[Any, ...]:
        before = card(page, "agent-choice-note"), visible(page, agent_button("codex"))
        claude_offered = visible(page, agent_button("claude-code"))
        await press(pilot, page, agent_button("codex"))
        await settle_page(host)
        after = _step_two(page)
        return (*before, claude_offered, *after, visible(page, agent_button("claude-code")))

    note, codex_offered, claude_offered, title, text, offered_after = hosted(machine, go)
    assert "Claude Code and Codex are both installed" in note
    assert codex_offered and claude_offered  # nothing saved yet: either pick is saved
    assert machine.chosen == ["codex"]
    assert title.startswith("2  Codex") and "✓ Codex 0.162.1" in text
    assert not offered_after  # CODEX_TRUSTED: the only agent installed, nothing to pick


def test_one_agent_installed_offers_no_choice() -> None:
    async def go(pilot: Pilot[None], page: WelcomeView, host: Host) -> tuple[bool, bool]:
        return visible(page, agent_button("codex")), visible(page, agent_button("claude-code"))

    assert hosted(Machine(claude=[CODEX_TRUSTED]), go) == (False, False)
    assert hosted(Machine(claude=[READY]), go) == (False, False)


# --------------------------------------------------------------------------- step 3


def test_step_three_names_codex_and_its_hooks_review(tmp_path: Path) -> None:
    machine, _ = _ready_machine(tmp_path, claude=[CODEX_PENDING])

    async def go(pilot: Pilot[None], page: WelcomeView, host: Host) -> str:
        return card(page, "fleet-status")

    text = hosted(machine, go)
    assert "Codex asks once whether you trust this folder" in text
    assert "/hooks" in text and "Claude Code" not in text
    trusted, _ = _ready_machine(tmp_path, claude=[CODEX_TRUSTED])
    assert "/hooks" not in hosted(trusted, go)


# --------------------------------------------------------------------------- in the shell


def test_the_shell_opens_accounts_with_codex_login_started_on_that_slot(
    fleet_rows: dict[str, list[FleetAgentStatus]],  # noqa: F811 — pytest resolves fixtures by NAME
    scripted: Callable[[Machine], None],  # noqa: F811 — pytest resolves fixtures by NAME
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    machine, _ = _ready_machine(
        tmp_path, claude=[dataclasses.replace(CODEX_PENDING, signed_in=False)], sign_in_slot=0
    )
    scripted(machine)
    begun: list[int | None] = []
    monkeypatch.setattr(
        AccountsView, "begin_claude_sign_in", lambda self, slot, **_k: begun.append(slot)
    )

    async def go(pilot: Pilot[None], app: FleetApp, page: WelcomeView) -> str:
        await press(pilot, page, "claude-sign-in")
        await settle_page(app)
        return str(app.content.current)

    assert in_shell(machine, go) == "accounts"
    assert begun == [0]
