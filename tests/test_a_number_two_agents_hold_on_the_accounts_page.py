"""asq's Accounts page on a home where two agents' accounts hold one number (#100's reopen, B).

Its rows were keyed by number alone: ``account-row-5`` twice, and Textual's DuplicateIds took
asq down as the page opened (tester-r9f's failure 7). Now a shared number's rows are keyed by
agent and number (``account-row-5-codex``), and every button and the rename field act on their
own row's account, named ``codex:5``, which the services refuse for no other. A place in the
order for the one listed last is refused there as on the command line.

The shell reads the real registry (``fake_home``) on every frame; no tmux, no network.
"""

from __future__ import annotations

from pathlib import Path

from textual.pilot import Pilot

from aisquare.cli.ui.views.accounts import AccountsView
from aisquare.models import ClaudeAccount
from aisquare.services import claude_accounts as service
from tests.test_a_removed_slots_place_goes_with_it import ticking  # noqa: F401 - fixture
from tests.test_naming_one_of_two_accounts_in_one_number import (  # noqa: F401 - fixtures
    REFUSED_RANK,
    _row,
    signed,
)
from tests.test_one_priority_order_across_agents import machine  # noqa: F401 - fixture
from tests.test_two_agents_accounts_in_one_number import _order, collided  # noqa: F401
from tests.test_ui_accounts import (  # noqa: F401 - the autouse fixtures, by name
    accounts_read,
    drive_registry,
    fleet_app,
    no_network,
    no_real_tmux,
    notice,
    open_accounts,
    press,
    settle,
)
from tests.ui_workers import settle_until

CLAUDE, CODEX = "claude-code", "codex"
Pair = tuple[ClaudeAccount, ClaudeAccount]


async def _both_rows(pilot: Pilot[None]) -> AccountsView:
    app = fleet_app(pilot)
    view = await open_accounts(pilot)
    await settle_until(
        app, lambda: {"5-codex", "5-claude-code"} <= {row.key for row in view.rows()}
    )
    return view


async def _acted(pilot: Pilot[None]) -> None:
    app = fleet_app(pilot)
    await settle(app)
    await accounts_read(app)


def test_the_page_lists_both_accounts_of_a_number_each_its_own_row(signed: Pair) -> None:  # noqa: F811
    async def go(pilot: Pilot[None]) -> list[object]:
        view = await _both_rows(pilot)
        return [
            [row.key for row in view.rows()],
            [row.ref for row in view.rows()],
            [row.id for row in view.rows()],
        ]

    keys, refs, ids = drive_registry(go)

    assert keys == ["1", "4", "5-codex", "2", "3", "5-claude-code"], "in the one order"
    assert refs == ["1", "4", "codex:5", "2", "3", "claude-code:5"]
    assert ids == [f"account-row-{key}" for key in keys]


def test_each_rows_buttons_and_name_act_on_its_own_account(signed: Pair) -> None:  # noqa: F811
    async def go(pilot: Pilot[None]) -> list[str]:
        view = await _both_rows(pilot)
        said: list[str] = []
        await press(pilot, "#account-toggle-5-claude-code")
        await _acted(pilot)
        said.append(notice(view))
        view.open_rename("5-codex")
        view.commit_rename("5-codex", "spare")
        await _acted(pilot)
        said.append(notice(view))
        await press(pilot, "#account-up-5-codex")
        await _acted(pilot)
        said.append(notice(view))
        return said

    disabled, renamed, moved = drive_registry(go)

    assert disabled == "✓ slot claude-code:5 disabled — never picked automatically"
    assert (_row(CLAUDE, 5).disabled, _row(CODEX, 5).disabled) == (True, False)
    assert renamed == "✓ slot codex:5 is now called spare"
    assert (_row(CODEX, 5).alias, _row(CLAUDE, 5).alias) == ("spare", None)
    assert moved == "✓ slot codex:5 moved up in the priority order"
    assert _order()[:3] == [(CLAUDE, 1), (CODEX, 5), (CLAUDE, 4)]


def test_a_place_for_the_one_listed_last_is_refused_on_the_page_too(signed: Pair) -> None:  # noqa: F811
    async def go(pilot: Pilot[None]) -> str:
        view = await _both_rows(pilot)
        await press(pilot, "#account-up-5-claude-code")
        await _acted(pilot)
        return notice(view)

    before = _order()

    said = drive_registry(go)

    assert said == f"✗ {REFUSED_RANK}"
    assert _order() == before


def test_remove_on_a_row_removes_that_rows_account(signed: Pair) -> None:  # noqa: F811
    async def go(pilot: Pilot[None]) -> list[str]:
        view = await _both_rows(pilot)
        await press(pilot, "#account-remove-5-claude-code")
        await _acted(pilot)
        await settle_until(fleet_app(pilot), lambda: "5-claude-code" not in _keys(view))
        return _keys(view)

    keys = drive_registry(go)

    assert [(a.agent, a.slot) for a in service.list_accounts() if a.slot == 5] == [(CODEX, 5)]
    assert keys == ["1", "4", "5", "2", "3"], "one account in 5 again: its row keyed by 5"


def _keys(view: AccountsView) -> list[str]:
    return [row.key for row in view.rows()]


def test_a_claude_only_pages_rows_are_keyed_by_number_as_they_were(machine: Path) -> None:  # noqa: F811
    from tests.test_one_priority_order_across_agents import _claude

    _claude("two@example.com")

    async def go(pilot: Pilot[None]) -> list[object]:
        view = await open_accounts(pilot)
        await settle_until(fleet_app(pilot), lambda: "2" in _keys(view))
        return [_keys(view), [row.id for row in view.rows()]]

    keys, ids = drive_registry(go)

    assert keys == ["1", "2"] and ids == ["account-row-1", "account-row-2"]
