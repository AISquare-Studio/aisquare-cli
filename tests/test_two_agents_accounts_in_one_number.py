"""Two agents' accounts in one slot number, after a rollback (seq 11271, #100).

0.9.2 numbers its own accounts by Claude Code's slots alone (``next_free_slot`` looks only at
``claude-accounts/``), so after a rollback its ``accounts add`` could take a Codex account's
number. Upgraded again, that new Claude Code account read the Codex account's rank, rank 1
and the launch default, and ``--account 5`` named two accounts. Now:

1. the newer of the two is read unranked, last, and the Codex account keeps its rank;
2. a bare number that names two accounts is refused, naming both and how to name one;
3. a Codex account this release adds keeps its number from 0.9.2 (a file in
   ``claude-accounts/``, which no release lists and 0.9.2's ``next_free_slot`` reads as
   taken);
4. the move-back warning says so where this home has such accounts.

0.9.2's own writes are replayed with the calls it makes: its slot folder and marker, and the
registry row its next read makes (``upsert_claude_account``, byte-identical to 0.9.2's but
for the commit helper).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import claude_accounts as core
from aisquare.core.config import RoleLaunchProfile, load_config, save_config
from aisquare.core.store import store_session
from aisquare.models import ClaudeAccount
from aisquare.services import agent_launch, lifecycle
from aisquare.services import claude_accounts as service
from tests.test_a_removed_slots_place_goes_with_it import ticking  # noqa: F401 - fixture
from tests.test_codex_accounts import _sign_in as _codex_sign_in
from tests.test_one_priority_order_across_agents import (  # noqa: F401 - fixture, by name
    _claude,
    machine,
)

CODEX = "codex"


def _order() -> list[tuple[str, int]]:
    return [(account.agent, account.slot) for account in service.list_accounts()]


def _next_free_slot_092() -> int:
    """0.9.2's ``next_free_slot``: the lowest number from 2 with no ``claude-accounts/<n>``."""
    slot = core.FIRST_MANAGED_SLOT
    taken = {account.slot for account in core.managed_accounts()}
    while slot in taken or (core.accounts_root() / str(slot)).exists():
        slot += 1
    return slot


def _add_on_092() -> ClaudeAccount:
    """0.9.2's ``accounts add`` and its next read: the slot folder and marker it makes, and the
    registry row its read makes."""
    slot = _next_free_slot_092()
    folder = core.accounts_root() / str(slot)
    folder.mkdir(parents=True, mode=0o700)
    (folder / core.MARKER).write_text(
        json.dumps({"slot": slot, "created_at": "2026-11-06T00:00:00+00:00"}), encoding="utf-8"
    )
    with store_session() as store:
        store.upsert_claude_account(slot, folder)
    found = next(a for a in core.managed_accounts() if a.slot == slot)
    return found


def _codex_account_of_an_earlier_build() -> ClaudeAccount:
    """A Codex account made before this release reserved its number (as 11271's was)."""
    account = core.create_account(CODEX)
    _codex_sign_in(account.config_dir, "codex@example.com")
    (core.accounts_root() / str(account.slot)).unlink()
    return account


@pytest.fixture
def collided(machine: Path, ticking: None) -> tuple[ClaudeAccount, ClaudeAccount]:  # noqa: F811
    """11271's repro: Claude Code slots read [1,4,2,3]; a Codex account takes 5 and is moved
    to the top; rolled back, 0.9.2 adds an account, which takes 5 too."""
    for email in ("two@example.com", "three@example.com", "four@example.com"):
        _claude(email)
    service.reorder(["1", "4", "2", "3"])
    codex = _codex_account_of_an_earlier_build()
    assert codex.slot == 5
    service.move("5", "top")
    assert _order() == [(CODEX, 5), ("claude-code", 1), *(("claude-code", n) for n in (4, 2, 3))]
    added = _add_on_092()
    assert added.slot == 5, "0.9.2 took the Codex account's number"
    return codex, added


def test_the_account_0_9_2_added_in_a_codex_accounts_number_is_last_and_never_the_default(
    collided: tuple[ClaudeAccount, ClaudeAccount],
) -> None:
    order = _order()

    assert order == [
        (CODEX, 5),
        ("claude-code", 1),
        ("claude-code", 4),
        ("claude-code", 2),
        ("claude-code", 3),
        ("claude-code", 5),
    ]
    assert _order() == order, "and so on every read after the repair"
    service.move("4", "top")  # a later reorder writes the ranks again, newer than both rows
    assert _order() == [
        ("claude-code", 4),
        (CODEX, 5),
        ("claude-code", 1),
        ("claude-code", 2),
        ("claude-code", 3),
        ("claude-code", 5),
    ], "the number's rank is still the Codex account's, the one it was written for"
    assert service.choose(None, role="coder", agent="claude-code").account is not None
    picked = service.choose(None, role="coder", agent="claude-code").account
    assert picked is not None and picked.slot != 5, "the default is never the newer one"


def test_a_bare_number_two_accounts_hold_is_refused_naming_both(
    collided: tuple[ClaudeAccount, ClaudeAccount],
) -> None:
    with pytest.raises(service.AmbiguousAccount) as refused:
        service.resolve("5")
    said = str(refused.value)
    assert "slot 5 is two accounts" in said
    assert "Codex's account 5 (codex@example.com)" in said
    assert "Claude Code's account 5 (not signed in)" in said
    assert "--agent" in said and "alias" in said

    assert service.resolve("5", agent=CODEX).agent == CODEX
    assert service.resolve("5", agent="claude-code").agent == "claude-code"
    with pytest.raises(ValueError, match="slot 5 is two accounts"):
        agent_launch.resolve("coder", account="5", measure=False)  # `launch coder --account 5`
    pinned = agent_launch.resolve("coder", agent=CODEX, account="5", measure=False)
    assert pinned.adapter.id == CODEX, "--agent names one"
    assert service.choose("5", role="coder", agent=CODEX).account.agent == CODEX  # type: ignore[union-attr]


def test_every_command_that_names_the_number_refuses_it(
    collided: tuple[ClaudeAccount, ClaudeAccount], runner: CliRunner
) -> None:
    for argv in (["accounts", "remove", "5"], ["accounts", "default", "5"]):
        result = runner.invoke(app, argv)
        assert result.exit_code != 0, (argv, result.output)
        assert "slot 5 is two accounts" in " ".join(result.output.split()), argv
    config = load_config()
    config.team.profiles["coder"] = RoleLaunchProfile(account="5")
    save_config(config)
    with pytest.raises(ValueError, match="slot 5 is two accounts"):
        agent_launch.resolve("coder", measure=False)  # a binding that names 5
    assert {(a.agent, a.slot) for a in service.list_accounts() if a.slot == 5} == {
        (CODEX, 5),
        ("claude-code", 5),
    }, "nothing removed"


def test_removing_either_leaves_the_other_its_row_and_place(
    collided: tuple[ClaudeAccount, ClaudeAccount],
) -> None:
    service.set_alias("codex@example.com", "work-codex")
    service.remove(service.resolve("5", agent="claude-code"))

    assert _order()[0] == (CODEX, 5), "the Codex account keeps rank 1"
    assert service.resolve("work-codex").slot == 5, "and its alias"
    assert service.resolve("5").agent == CODEX, "one account in 5 again"


def test_a_codex_account_this_release_adds_keeps_its_number_from_0_9_2(
    machine: Path,  # noqa: F811
) -> None:
    _claude("two@example.com")
    codex = core.create_account(CODEX)
    held = core.accounts_root() / str(codex.slot)

    assert held.is_file(), "the number reserved where 0.9.2 looks"
    assert [a.slot for a in core.managed_accounts()] == [2], "and listed by no release"
    assert _next_free_slot_092() == codex.slot + 1, "0.9.2 skips it"
    assert core.next_free_slot() == codex.slot + 1

    core.remove_account(codex)
    assert not held.exists(), "freed with its account"
    assert _next_free_slot_092() == codex.slot


def test_a_folder_0_9_2_made_in_the_number_is_never_released(
    machine: Path,  # noqa: F811
) -> None:
    codex = _codex_account_of_an_earlier_build()
    added = _add_on_092()
    assert added.slot == codex.slot

    core.remove_account(codex)

    assert added.config_dir.is_dir(), "the Claude Code account 0.9.2 added stays"


def test_the_move_back_warning_names_it_where_there_are_codex_accounts(
    machine: Path,  # noqa: F811
) -> None:
    plan = lifecycle.UpgradePlan.__new__(lifecycle.UpgradePlan)
    object.__setattr__(plan, "target", "0.9.2")
    object.__setattr__(plan, "current", "0.10.0")

    assert plan.store_warning is not None and "Codex" not in plan.store_warning
    core.create_account(CODEX)
    assert plan.store_warning.endswith(
        "it may give it the number of a Codex account, which this release then lists last "
        "and asks which you mean"
    )
