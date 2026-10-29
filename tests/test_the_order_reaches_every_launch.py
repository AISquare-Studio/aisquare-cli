"""The agent rung's answer reaches every launch that asks for it (review of #14, survivors).

``agent_launch.resolve`` picks the agent of an unpinned launch and, when a reading decided
it, the account too; each caller that launches hands that pick on to
``claude_accounts.choose``, so one usage read serves both, and hands the account it was asked
to name to ``resolve``. A caller that dropped either still passed every test: the launch ran,
it only read usage twice or took the wrong agent's account. Usage is scripted and counted.
"""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import claude_accounts as core
from aisquare.services import agent_launch, persona_import
from aisquare.services import claude_accounts as service
from tests.test_one_priority_order_across_agents import (  # noqa: F401 - fixture, by name
    CLAUDE,
    CODEX,
    _claude,
    _codex,
    _usage,
    machine,
)


@pytest.fixture
def launched(machine: Path, monkeypatch: pytest.MonkeyPatch) -> Mock:  # noqa: F811
    """``aisquare launch`` with its ``exec`` taken: the call that would have started the agent."""
    execute = Mock()
    monkeypatch.setattr(import_module("aisquare.cli.launch"), "_exec", execute)
    monkeypatch.setattr(
        agent_launch, "executable", lambda selected: f"/opt/bin/{selected.binary.binary}"
    )
    return execute


def test_a_launch_runs_the_account_its_agent_rung_picked_on_one_usage_read(
    launched: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude = _claude("c@example.com")
    codex = _codex("x@example.com")
    other = _codex("y@example.com")  # two of its agent: a second pick would read usage again
    service.reorder([str(codex.slot), str(other.slot)])
    reads = _usage(monkeypatch, {codex.slot: 10.0, other.slot: 50.0, claude.slot: 10.0})

    result = CliRunner().invoke(app, ["launch", "coder"])

    assert result.exit_code == 0, result.output
    env = launched.call_args.args[2]
    assert env["CODEX_HOME"] == str(codex.config_dir), "Codex's, on the account the pick chose"
    assert len(reads) == 1, "the pick's one read served the agent and its account"


def test_a_launch_that_names_an_account_runs_that_accounts_agent(
    launched: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The order puts Claude Code first, and the account named is Codex's: naming it names
    its agent, and the launch is not refused as Claude Code's with another agent's account."""
    claude = _claude("c@example.com")
    codex = _codex("x@example.com")
    _usage(monkeypatch, {codex.slot: 10.0, claude.slot: 10.0})
    assert agent_launch.resolve("coder").adapter.id == CLAUDE

    result = CliRunner().invoke(app, ["launch", "coder", "--account", str(codex.slot)])

    assert result.exit_code == 0, result.output
    assert launched.call_args.args[2]["CODEX_HOME"] == str(codex.config_dir)


@pytest.mark.usefixtures("machine")
def test_the_manager_engine_takes_the_account_the_agent_rung_picked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude = _claude("c@example.com")
    codex = _codex("x@example.com")
    other = _codex("y@example.com")  # two of its agent: a second pick would read usage again
    service.reorder([str(codex.slot), str(other.slot)])
    reads = _usage(monkeypatch, {codex.slot: 10.0, other.slot: 50.0, claude.slot: 10.0})
    selected = persona_import.manager()
    assert selected.adapter.id == CODEX and selected.pick is not None

    _env, home = persona_import._manager_env(selected)

    assert home == codex.config_dir
    assert len(reads) == 1, "the pick's one read served the agent and its account"


@pytest.mark.usefixtures("machine")
def test_accounts_add_names_the_new_accounts_agent_not_the_orders() -> None:
    """The line it prints to open the account as a role asks for the role's agent with that
    account named: a Codex account is Codex's whatever the order's first is."""
    _claude("c@example.com")
    codex = _codex("x@example.com")
    accounts_cli = import_module("aisquare.cli.accounts")

    assert accounts_cli._role_agent("coder", str(codex.slot)) == CODEX
    assert accounts_cli._role_agent("coder") == CLAUDE, "no account named: the order's first"
    assert core.find_account(codex.slot) is not None
