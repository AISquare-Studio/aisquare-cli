"""Where the CLI and ``asq`` speak of accounts, they speak of every agent that has them (task
#109, found by SMOKE7 of #113): Codex slots are accounts as Claude Code's are, and the help
of ``fleet spawn --account`` and ``team bind --account``, and the Settings hub's line, named
only Claude Code's while each took a Codex slot (``team bind validator --agent codex
--account 3`` then ``fleet spawn validator`` ran on Codex slot 3, measured)."""

from __future__ import annotations

import pytest

from aisquare.cli.ui.views.settings_hub import LINKS
from aisquare.core.agent_adapters import get_adapter
from aisquare.core.claude_accounts import account_agents
from tests.cli_tree import all_nodes


def _option_help(path: tuple[str, ...], option: str) -> str:
    (command,) = [command for at, command in all_nodes() if at == path]
    (param,) = [param for param in command.params if option in getattr(param, "opts", ())]
    assert param.help, (path, option)
    return " ".join(str(param.help).split())


def test_the_settings_hubs_accounts_line_names_every_agent_with_accounts() -> None:
    (about,) = [line for stem, _label, line in LINKS if stem == "accounts"]
    assert len(account_agents()) > 1
    for agent in account_agents():
        assert get_adapter(agent).label in about, about


@pytest.mark.parametrize("path", [("fleet", "spawn"), ("team", "bind")], ids=" ".join)
def test_an_account_option_is_one_of_the_agents_own(path: tuple[str, ...]) -> None:
    said = _option_help(path, "--account")
    assert not said.startswith("Claude"), said
    for agent in account_agents():
        assert get_adapter(agent).label in said, said


def test_a_bound_account_wins_over_its_own_agents_home_in_the_bindings_env() -> None:
    said = _option_help(("team", "bind"), "--account")
    for agent in account_agents():
        accounts = get_adapter(agent).accounts
        assert accounts is not None and accounts.home_env in said, said
