"""Codex parity for main's 0.9.0 account features (SCOPE #113 090), on scratch accounts of both
agents: #406's rank 1 across agents (the merge of #406 with the one priority order of #14), the
readings #408 reads back off that Codex never has, and *Log in again* on a Codex row.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from aisquare.cli.ui.views.accounts import _sign_in_opens, offers_login_again
from aisquare.core import claude_accounts as accounts_core
from aisquare.core.store import store_session
from aisquare.models import ClaudeUsage
from aisquare.services import claude_accounts as accounts_service
from tests.test_one_priority_order_across_agents import (  # noqa: F401 - fixture, by name
    CLAUDE,
    CODEX,
    _claude,
    _codex,
    _order,
    machine,
)

# --- #406: rank 1 IS the machine default, across agents -----------------------------------

REAL_READ_USAGE = accounts_service.read_usage


def test_rank_one_is_the_machine_default_whatever_its_agent(
    machine: Path,  # noqa: F811
) -> None:
    claude = _claude("c@example.com")
    codex = _codex("x@example.com")
    plain = accounts_core.kind(CLAUDE).plain_slot
    first = accounts_service.machine_default()
    assert first is not None and first.slot == claude.slot, "the plain claude has no login"

    chosen = accounts_service.set_default(str(codex.slot))

    assert chosen is not None and chosen.slot == codex.slot
    assert _order() == [codex.slot, plain, claude.slot], "moved to rank 1 of the ONE order"
    top = accounts_service.machine_default()
    assert top is not None and (top.agent, top.slot) == (CODEX, codex.slot)
    own = accounts_service.machine_default(CLAUDE)
    assert own is not None and own.slot == claude.slot, (
        "a launch pinned to Claude Code keeps its own"
    )
    with pytest.raises(accounts_service.AccountsError, match="cannot be cleared"):
        accounts_service.set_default(None)


def test_a_retired_default_flag_of_either_agent_is_folded_into_the_one_order(
    machine: Path,  # noqa: F811
) -> None:
    claude = _claude("c@example.com")
    claude_two = _claude("c2@example.com")
    codex = _codex("x@example.com")
    codex_two = _codex("x2@example.com")
    plain = accounts_core.kind(CLAUDE).plain_slot
    accounts_service.list_accounts()  # seeds the store: ranks, each agent's rows
    assert _order() == [plain, claude.slot, claude_two.slot, codex.slot, codex_two.slot]

    # What an older build left: a default flagged on the second account of each agent.
    with store_session() as store:
        conn = cast(Any, store)._conn  # the registries' raw rows: the flag has no writer now
        conn.execute("UPDATE claude_account SET is_default = 1 WHERE slot = ?", (claude_two.slot,))
        conn.execute("UPDATE agent_account SET is_default = 1 WHERE slot = ?", (codex_two.slot,))
        conn.commit()

    folded = _order()

    # Each goes ahead of its own agent's other accounts, in the order as a whole.
    assert folded == [claude_two.slot, plain, claude.slot, codex_two.slot, codex.slot]
    with store_session() as store:
        conn = cast(Any, store)._conn
        flagged = sum(
            conn.execute(f"SELECT COUNT(*) FROM {table} WHERE is_default = 1").fetchone()[0]
            for table in ("claude_account", "agent_account")
        )
    assert flagged == 0, "the flags are cleared: once"
    assert _order() == folded, "and a later read changes nothing"


# --- #408, Log in again -------------------------------------------------------------------


def test_a_codex_reading_is_never_recorded_as_a_trend_sample_nor_backed_off(
    machine: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex's reading is the newest one its own sessions recorded, read again on every tick:
    kept as samples it would be one figure a minute (a rate of zero), and there is no endpoint
    to rate-limit (#408's back-off is Claude Code's alone)."""
    codex = _codex("x@example.com")
    reading = ClaudeUsage(available=True, session_percent=30.0, week_percent=40.0)
    monkeypatch.setattr(accounts_service, "usage", lambda account, **_: reading)
    monkeypatch.setattr(accounts_service, "read_usage", REAL_READ_USAGE)  # the fixture scripts it

    read = accounts_service.read_usage([codex], now=datetime.now(tz=UTC))

    assert read[codex.slot].available
    with store_session() as store:
        assert store.usage_samples(codex.slot, since=datetime.now(tz=UTC) - timedelta(days=1)) == []
        assert store.ui_state(accounts_service._BACKOFF_KEY.format(slot=codex.slot)) is None
    assert not accounts_service.is_kept(read[codex.slot])


def test_a_signed_in_codex_row_offers_log_in_again_which_opens_codex_login(
    machine: Path,  # noqa: F811
) -> None:
    codex = _codex("x@example.com")

    status = accounts_service.describe(codex)

    assert offers_login_again(status, None)
    assert "codex login" in _sign_in_opens(CODEX)
