"""A removed slot's place in the one order goes with it (review r40 of #113, finding 2).

``forget_arrangement`` dropped the slot's registry row and left its ``account_rank`` row, so
the next ``accounts add``, which takes the lowest free number, took the removed slot's place
too: rank 1, the machine default, when that is where it was. 0.9.2 put a new account last.
r40's repro, run on both: order ``2 3 1``, remove 2, add one → 0.9.2 ``[3, 1, 2]``. A store
that already holds such an orphan (an account removed by a build before this fix) is
repaired on read: a rank on a slot the registry did not know is not read.
"""

from __future__ import annotations

from pathlib import Path

from aisquare.core import claude_accounts as core
from aisquare.core.store import store_session
from aisquare.services import claude_accounts as service
from tests.test_one_priority_order_across_agents import (  # noqa: F401 - fixture, by name
    _claude,
    machine,
)


def _order() -> list[int]:
    return [account.slot for account in service.list_accounts()]


def _ordered_2_3_1() -> list[int]:
    two, three = _claude("two@example.com"), _claude("three@example.com")
    assert (two.slot, three.slot) == (2, 3)
    service.reorder(["2", "3", "1"])
    assert _order() == [2, 3, 1]
    return [two.slot, three.slot]


def test_an_account_added_after_a_removal_goes_last_as_on_0_9_2(machine: Path) -> None:  # noqa: F811
    _ordered_2_3_1()
    two = service.resolve("2")

    service.remove(two)
    with store_session() as store:
        assert store.account_ranks() == {3: 1, 1: 2}, "its rank went with it, read or not"
    added = core.create_account()  # `accounts add` takes the lowest free number

    assert added.slot == 2
    assert _order() == [3, 1, 2], "0.9.2's order: the new account last, slot 3 the default"
    with store_session() as store:
        assert store.account_ranks() == {3: 1, 1: 2, 2: 3}


def test_a_store_holding_an_orphan_rank_is_repaired_on_read(machine: Path) -> None:  # noqa: F811
    """As a build before the fix left it: slot 2's row gone with its directory, its rank kept,
    and no read of the order between that and the next add."""
    _ordered_2_3_1()
    two = service.resolve("2")
    core.remove_account(two)
    with store_session() as store:
        store.delete_claude_account(two.slot)
        assert store.account_ranks()[two.slot] == 1, "the orphan, rank 1"

    added = core.create_account()

    assert added.slot == 2
    assert _order() == [3, 1, 2]
