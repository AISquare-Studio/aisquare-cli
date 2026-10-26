"""One priority order over every agent's accounts, and the agent a launch runs from it.

The owner's rule (PR #113, r17 item 7): accounts of any agent are ordered in ONE list,
and a launch that nothing pins runs the agent of the best account in it — the first with
room, by the same rule a hand-over picks an account by. Pins still win: ``--agent``, a
role's agent, a named binary, a project's default, and an account the launch names. The
App settings select is the fallback when no account is usable. Usage is scripted here
(``cached_usage`` per slot), never read: the rule is what is under test, each agent's own
reading is pinned beside its adapter.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import pytest

from aisquare.core import claude_accounts as core
from aisquare.core.config import AccountsSettings, RoleLaunchProfile, load_config, save_config
from aisquare.core.orchestrator import team_project
from aisquare.core.store import store_session
from aisquare.models import ClaudeAccount, ClaudeUsage, UsageWindow
from aisquare.services import agent_launch
from aisquare.services import claude_accounts as service
from tests.test_claude_accounts import _sign_in as _claude_sign_in
from tests.test_codex_accounts import _sign_in as _codex_sign_in

CLAUDE = core.AGENT
CODEX = "codex"
_REAL_WHICH = shutil.which


# --------------------------------------------------------------------------- helpers


@pytest.fixture
def machine(fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Both agents installed, a store in place, usage scripted (nothing read)."""
    _installed(monkeypatch, claude="/bin/claude", codex="/bin/codex")
    _usage(monkeypatch, {})
    with store_session():
        pass
    return fake_home


def _installed(monkeypatch: pytest.MonkeyPatch, **found: str | None) -> None:
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name, *a, **k: found[name] if name in found else _REAL_WHICH(name, *a, **k),
    )


def _usage(monkeypatch: pytest.MonkeyPatch, used: Mapping[int, float | None]) -> list[list[int]]:
    """Each slot's reading, ``used`` percent of one window (``None``: unreadable); the
    slots of every read are recorded, one list per read."""
    reads: list[list[int]] = []

    def cached(accounts: Iterable[ClaudeAccount], **_: Any) -> dict[int, ClaudeUsage]:
        listed = list(accounts)
        reads.append([a.slot for a in listed])
        return {
            a.slot: (
                ClaudeUsage(available=False, reason="unread")
                if used.get(a.slot) is None
                else ClaudeUsage(
                    available=True,
                    windows=(UsageWindow(name="session", minutes=300, used_percent=used[a.slot]),),
                )
            )
            for a in listed
        }

    monkeypatch.setattr(service, "cached_usage", cached)
    monkeypatch.setattr(service, "read_usage", lambda accounts, **k: cached(accounts))
    return reads


def _claude(email: str) -> ClaudeAccount:
    account = core.create_account()
    _claude_sign_in(account, email)
    return account


def _codex(email: str) -> ClaudeAccount:
    account = core.create_account(CODEX)
    _codex_sign_in(account.config_dir, email)
    return account


def _order() -> list[int]:
    return [a.slot for a in service.list_accounts()]


def _default_agent(agent: str | None) -> None:
    config = load_config()
    config.agents.default = agent
    save_config(config)


def _pick(**overrides: Any) -> None:
    config = load_config()
    config.accounts = AccountsSettings(**overrides)
    save_config(config)


# --------------------------------------------------------------------------- the one order


def test_the_order_starts_with_the_users_agent_and_moves_across_agents(machine: Path) -> None:
    claude = _claude("c@example.com")
    codex = _codex("x@example.com")
    plain = core.kind(CLAUDE).plain_slot

    assert _order() == [plain, claude.slot, codex.slot], "the user's agent (Claude Code) first"
    assert [a.position for a in service.list_accounts()] == [1, 2, 3], "one rank across agents"

    service.move(str(codex.slot), "top")
    assert _order() == [codex.slot, plain, claude.slot]
    service.move(str(claude.slot), "up")
    assert _order() == [codex.slot, claude.slot, plain]
    service.reorder([str(plain)])
    assert _order() == [plain, codex.slot, claude.slot]

    # Each agent's own ranking follows the one order (the builds before v43 read it).
    with store_session() as store:
        assert [r.slot for r in store.claude_accounts()] == [plain, claude.slot]


def test_a_store_that_ranked_nothing_starts_with_the_default_agents_accounts(
    machine: Path,
) -> None:
    _default_agent(CODEX)
    claude = _claude("c@example.com")
    codex = _codex("x@example.com")

    assert _order() == [codex.slot, core.kind(CLAUDE).plain_slot, claude.slot]


def test_an_account_added_later_is_ranked_last_and_the_rest_keep_theirs(machine: Path) -> None:
    _claude("c@example.com")
    codex = _codex("x@example.com")
    service.move(str(codex.slot), "top")
    before = _order()

    later = _claude("later@example.com")

    assert _order() == [*before, later.slot]


# --------------------------------------------------------------------------- the agent rung


def test_an_unpinned_launch_runs_the_agent_of_the_first_account_with_room(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude = _claude("c@example.com")
    codex = _codex("x@example.com")
    service.move(str(codex.slot), "top")
    _usage(monkeypatch, {codex.slot: 10.0, claude.slot: 10.0})

    selected = agent_launch.resolve("coder")

    assert (selected.adapter.id, selected.source) == (CODEX, "accounts")
    assert selected.pick is not None and selected.pick.account is not None
    assert selected.pick.account.slot == codex.slot


def test_a_full_account_on_top_gives_way_to_the_next_agents_account_with_room(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude = _claude("c@example.com")
    codex = _codex("x@example.com")
    service.move(str(codex.slot), "top")
    _usage(monkeypatch, {codex.slot: 99.0, claude.slot: 20.0})

    assert agent_launch.resolve("coder").adapter.id == CLAUDE


def test_with_nothing_measured_the_order_alone_picks(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _claude("c@example.com")
    codex = _codex("x@example.com")
    service.move(str(codex.slot), "top")
    _usage(monkeypatch, {})

    selected = agent_launch.resolve("coder")

    assert (selected.adapter.id, selected.source) == (CODEX, "accounts")
    assert selected.pick is not None and selected.pick.account is None, "choose() picks it"


def test_pick_default_takes_the_first_usable_accounts_agent_with_no_reading(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _claude("c@example.com")
    codex = _codex("x@example.com")
    service.move(str(codex.slot), "top")
    _pick(pick="default")
    reads = _usage(monkeypatch, {})

    assert agent_launch.resolve("coder").adapter.id == CODEX
    assert reads == []


def test_one_agents_accounts_decide_with_no_usage_read(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _codex("x@example.com")
    _codex("y@example.com")
    reads = _usage(monkeypatch, {})

    assert agent_launch.resolve("coder").adapter.id == CODEX
    assert reads == []


def test_an_agent_not_installed_is_passed_over(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude = _claude("c@example.com")
    codex = _codex("x@example.com")
    service.move(str(codex.slot), "top")
    _usage(monkeypatch, {codex.slot: 1.0, claude.slot: 1.0})
    _installed(monkeypatch, claude="/bin/claude", codex=None)

    assert agent_launch.resolve("coder").adapter.id == CLAUDE


def test_a_disabled_or_signed_out_account_is_passed_over(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude = _claude("c@example.com")
    codex = _codex("x@example.com")
    signed_out = core.create_account(CODEX)
    service.reorder([str(signed_out.slot), str(codex.slot)])
    service.set_disabled(str(codex.slot), True)
    _usage(monkeypatch, {codex.slot: 1.0, claude.slot: 1.0, signed_out.slot: 1.0})

    assert agent_launch.resolve("coder").adapter.id == CLAUDE


def test_with_no_usable_account_the_fallback_agent_decides(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core.create_account(CODEX)  # never signed in: an API-key machine's
    _default_agent(CODEX)

    selected = agent_launch.resolve("coder")

    assert (selected.adapter.id, selected.source) == (CODEX, "user")
    _default_agent(None)
    assert agent_launch.resolve("coder").source == "default"


def test_every_pin_wins_over_the_order(
    machine: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    claude = _claude("c@example.com")
    codex = _codex("x@example.com")
    service.move(str(codex.slot), "top")
    _usage(monkeypatch, {codex.slot: 1.0, claude.slot: 1.0})

    assert agent_launch.resolve("coder", agent=CLAUDE).source == "flag"
    assert agent_launch.resolve("coder", account=str(claude.slot)).adapter.id == CLAUDE
    assert agent_launch.resolve("coder", account=str(claude.slot)).source == "account"
    assert agent_launch.resolve("coder", binary="claude").source != "accounts"

    config = load_config()
    config.team.profiles["coder"] = RoleLaunchProfile(agent=CLAUDE)
    save_config(config)
    assert agent_launch.resolve("coder").source == "role"
    config.team.profiles["coder"] = RoleLaunchProfile(account=str(claude.slot))
    save_config(config)
    assert (agent_launch.resolve("coder").adapter.id, agent_launch.resolve("coder").source) == (
        CLAUDE,
        "account",
    )
    del config.team.profiles["coder"]
    save_config(config)

    work = tmp_path / "work"
    work.mkdir()
    project = team_project(work)
    with store_session() as store:
        store.set_meta(f"coding-agent:{project.id}", CLAUDE)
    assert agent_launch.resolve("coder", cwd=work).source == "project"


def test_the_account_the_rung_picked_is_the_launchs_with_no_second_read(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude = _claude("c@example.com")
    codex = _codex("x@example.com")
    second = _codex("y@example.com")
    service.reorder([str(codex.slot), str(second.slot)])
    reads = _usage(monkeypatch, {codex.slot: 95.0, second.slot: 5.0, claude.slot: 5.0})

    selected = agent_launch.resolve("coder")
    choice = service.choose(None, role="coder", agent=selected.adapter.id, pick=selected.pick)

    assert choice.account is not None and choice.account.slot == second.slot
    assert choice.source == "headroom"
    assert len(reads) == 1, "one usage read for the agent and its account"


# --------------------------------------------------------------------------- choose_any


def test_choose_any_is_the_hand_offs_cross_agent_pick(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude = _claude("c@example.com")
    codex = _codex("x@example.com")
    service.move(str(codex.slot), "top")
    _usage(monkeypatch, {codex.slot: 99.0, claude.slot: 99.0})

    leaving = service.choose_any(exclude=[codex.slot], live=True, least_bad=False)
    assert (leaving.agent, leaving.account) == (None, None), "nobody with room"

    _usage(monkeypatch, {codex.slot: 99.0, claude.slot: 30.0})
    moved = service.choose_any(exclude=[codex.slot], live=True, least_bad=False)
    assert moved.agent == CLAUDE and moved.account is not None
    assert moved.account.slot == claude.slot

    only_codex = service.choose_any(agents=[CODEX])
    assert only_codex.agent == CODEX, "least bad among the agents allowed"


# --------------------------------------------------------------------------- the upgrade


@pytest.mark.parametrize("version", [44, 39, 37], ids=["v44", "main-v39", "main-v37"])
def test_an_upgraded_store_keeps_each_agents_order_with_the_default_agent_first(
    fake_home: Path, version: int
) -> None:
    from tests.test_store import _at_version

    first = core.create_account()
    second = core.create_account()
    codex = core.create_account(CODEX)  # main's stores have no row for it: v43 adds the table
    plain = core.kind(CLAUDE).plain_slot
    rows = [
        f"INSERT INTO claude_account (slot, config_dir, position, created_at) VALUES "
        f"({slot}, '{core.find_account(slot).config_dir}', {position}, '2026-10-01')"  # type: ignore[union-attr]
        for position, slot in enumerate([second.slot, plain, first.slot], start=1)
    ]
    if version >= 43:
        rows.append(
            "INSERT INTO agent_account (slot, agent, config_dir, position, created_at) VALUES "
            f"({codex.slot}, '{CODEX}', '{codex.config_dir}', 1, '2026-10-01')"
        )
    _at_version(version, after=";\n".join(rows) + ";")

    order = _order()

    assert order == [second.slot, plain, first.slot, codex.slot]
    with store_session() as store:
        assert store.account_ranks() == {slot: rank for rank, slot in enumerate(order, start=1)}
