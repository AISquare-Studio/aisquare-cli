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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from aisquare.core import claude_accounts as core
from aisquare.core import paths
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


EXPIRED = ClaudeUsage(
    available=False,
    needs_login=True,
    reason="the stored token has expired — open a session on this account to refresh it",
)
"""What an account idle for hours reads: signed in, its token to be refreshed by a session."""
UNREACHABLE = ClaudeUsage(available=False, reason="could not reach the usage endpoint")
THROTTLED = ClaudeUsage(available=False, rate_limited=True, reason="rate-limited until 14:32")
Reading = float | ClaudeUsage | None
"""A slot's scripted reading: percent used of one window, a reading as it is, or ``None``
(unreadable)."""


def _reading(value: Reading, *, week_resets: datetime | None = None) -> ClaudeUsage:
    if isinstance(value, ClaudeUsage):
        return value
    if value is None:
        return ClaudeUsage(available=False, reason="unread")
    windows = [UsageWindow(name="session", minutes=300, used_percent=value)]
    if week_resets is not None:
        windows.append(
            UsageWindow(name="week", minutes=10080, used_percent=value, resets_at=week_resets)
        )
    return ClaudeUsage(available=True, windows=tuple(windows))


def _usage(
    monkeypatch: pytest.MonkeyPatch,
    used: Mapping[int, Reading],
    *,
    resets: Mapping[int, datetime] | None = None,
) -> list[list[int]]:
    """Each slot's reading (:data:`Reading`; ``resets`` the day its week resets); the slots
    of every read are recorded, one list per read."""
    reads: list[list[int]] = []

    def cached(accounts: Iterable[ClaudeAccount], **_: Any) -> dict[int, ClaudeUsage]:
        listed = list(accounts)
        reads.append([a.slot for a in listed])
        return {
            a.slot: _reading(used.get(a.slot), week_resets=(resets or {}).get(a.slot))
            for a in listed
        }

    monkeypatch.setattr(service, "cached_usage", cached)
    monkeypatch.setattr(service, "read_usage", lambda accounts, **k: cached(accounts))
    return reads


def _split_usage(
    monkeypatch: pytest.MonkeyPatch, *, cached: Mapping[int, Reading], live: Mapping[int, Reading]
) -> tuple[list[list[int]], list[list[int]]]:
    """The cache's readings and a live read's, apart; the slots of each read, per kind."""
    cached_reads: list[list[int]] = []
    live_reads: list[list[int]] = []

    def reader(used: Mapping[int, Reading], reads: list[list[int]]) -> Any:
        def read(accounts: Iterable[ClaudeAccount], **_: Any) -> dict[int, ClaudeUsage]:
            listed = list(accounts)
            reads.append([a.slot for a in listed])
            return {a.slot: _reading(used.get(a.slot)) for a in listed}

        return read

    monkeypatch.setattr(service, "cached_usage", reader(cached, cached_reads))
    monkeypatch.setattr(service, "read_usage", reader(live, live_reads))
    return cached_reads, live_reads


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
    assert selected.pick is not None and selected.pick.account is None, "no reading decided it"
    choice = service.choose(None, role="coder", agent=CODEX, pick=selected.pick)
    assert choice.account is not None and choice.account.slot == codex.slot


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


# --------------------------------------------------------------------------- an unread account
#
# Review of #14, M1: an account whose usage cannot be read is UNMEASURED, not FULL. The
# state an account idle for hours normally reads (an expired token, which a session on the
# account refreshes) must not hand an unpinned launch to a lower agent's account.


def test_an_unread_top_account_keeps_its_place_so_the_launch_keeps_its_agent(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude = _claude("c@example.com")
    codex = _codex("x@example.com")
    _usage(monkeypatch, {claude.slot: EXPIRED, codex.slot: 20.0})

    selected = agent_launch.resolve("coder")

    assert (selected.adapter.id, selected.source) == (CLAUDE, "accounts")
    assert selected.pick is not None and selected.pick.account is None, "no reading decided it"
    assert any("keeps its place" in note for note in selected.pick.notes), selected.pick.notes
    choice = service.choose(None, role="coder", agent=CLAUDE, pick=selected.pick)
    assert choice.account is not None and choice.account.slot == claude.slot


@pytest.mark.parametrize(
    ("top", "next_", "agent"),
    [
        (95.0, 20.0, CODEX),
        (EXPIRED, 20.0, CLAUDE),
        (UNREACHABLE, 20.0, CLAUDE),
        (THROTTLED, 20.0, CLAUDE),
        (95.0, None, CODEX),
        (None, None, CLAUDE),
        (95.0, 90.0, CODEX),
    ],
    ids=[
        "measured over the line",
        "expired token",
        "unreachable endpoint",
        "endpoint asked to wait",
        "over the line, the next unread",
        "neither read",
        "both over the line: the most room",
    ],
)
def test_a_lower_agent_wins_only_over_an_account_measured_over_the_line(
    machine: Path, monkeypatch: pytest.MonkeyPatch, top: Reading, next_: Reading, agent: str
) -> None:
    claude = _claude("c@example.com")
    codex = _codex("x@example.com")
    _usage(monkeypatch, {claude.slot: top, codex.slot: next_})

    assert agent_launch.resolve("coder").adapter.id == agent


def test_an_account_at_a_named_limit_gives_its_place_to_the_next_agent(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude = _claude("c@example.com")
    codex = _codex("x@example.com")
    _usage(monkeypatch, {claude.slot: EXPIRED, codex.slot: None})
    assert agent_launch.resolve("coder").adapter.id == CLAUDE

    service.remember_limit(claude.slot, "weekly", datetime.now(tz=UTC) + timedelta(days=2))

    assert agent_launch.resolve("coder").adapter.id == CODEX


def test_the_top_codex_account_keeps_its_place_when_nothing_was_recorded_for_it(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude = _claude("c@example.com")
    codex = _codex("x@example.com")
    service.move(str(codex.slot), "top")
    _usage(monkeypatch, {codex.slot: None, claude.slot: 10.0})

    assert agent_launch.resolve("coder").adapter.id == CODEX


def test_reset_soonest_compares_the_readings_there_are_and_never_prefers_an_unread_account(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude = _claude("c@example.com")
    codex = _codex("x@example.com")
    now = datetime.now(tz=UTC)
    _pick(prefer="reset-soonest")
    _usage(
        monkeypatch,
        {claude.slot: 10.0, codex.slot: 10.0},
        resets={claude.slot: now + timedelta(days=5), codex.slot: now + timedelta(days=1)},
    )
    assert agent_launch.resolve("coder").adapter.id == CODEX, "the week that ends first"

    _pick(prefer="priority")
    assert agent_launch.resolve("coder").adapter.id == CLAUDE, "the first in the order"

    _pick(prefer="reset-soonest")
    _usage(monkeypatch, {claude.slot: EXPIRED, codex.slot: 10.0}, resets={codex.slot: now})
    assert agent_launch.resolve("coder").adapter.id == CLAUDE, "unread is not passed over"


def test_reset_soonest_among_two_measured_accounts_never_passes_over_an_unread_top_one(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tester-14's 3-account case: an unread Claude on top, two measured Codex accounts below
    whose weeks reset on different days. ``reset-soonest`` takes, of the accounts it can
    compare, the one that resets first; it does not do so over an unmeasured first account,
    which keeps its place (a guard only a case with TWO measured accounts reaches: with one,
    there is nothing to compare)."""
    claude = _claude("c@example.com")
    far = _codex("far@example.com")
    near = _codex("near@example.com")
    now = datetime.now(tz=UTC)
    _pick(prefer="reset-soonest")
    _usage(
        monkeypatch,
        {claude.slot: EXPIRED, far.slot: 10.0, near.slot: 10.0},
        resets={far.slot: now + timedelta(days=5), near.slot: now + timedelta(days=1)},
    )

    selected = agent_launch.resolve("coder")

    assert selected.adapter.id == CLAUDE, "the unread account on top keeps its place"
    assert selected.pick is not None and selected.pick.account is None, "no reading decided it"

    _usage(
        monkeypatch,
        {claude.slot: 10.0, far.slot: 10.0, near.slot: 10.0},
        resets={
            claude.slot: now + timedelta(days=7),
            far.slot: now + timedelta(days=5),
            near.slot: now + timedelta(days=1),
        },
    )
    again = agent_launch.resolve("coder")
    assert again.adapter.id == CODEX, "control: with all three measured, the week that ends first"
    assert again.pick is not None and again.pick.account is not None
    assert again.pick.account.slot == near.slot


def test_an_unread_account_is_never_a_hand_overs_target(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The asymmetry is the point: a launch keeps an unread account's place, a hand-over is
    leaving a limited account and moves only to one it MEASURED."""
    claude = _claude("c@example.com")
    codex = _codex("x@example.com")
    _usage(monkeypatch, {claude.slot: EXPIRED, codex.slot: 20.0})

    moved = service.choose_any(live=True)

    assert moved.agent == CODEX and moved.account is not None and moved.account.slot == codex.slot


def test_nothing_measured_the_order_picks_the_agent_and_choose_lands_on_its_first_account(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #14, minor 3: with no reading to decide, ``pick.account`` is ``None`` and the
    launch's ladder ends on rank 1 of that agent's accounts: the order's own choice."""
    _claude("c@example.com")
    top = _codex("x@example.com")
    second = _codex("y@example.com")
    service.reorder([str(top.slot), str(second.slot)])
    _usage(monkeypatch, {})

    for pick in ("headroom", "default"):
        _pick(pick=pick)
        selected = agent_launch.resolve("coder")
        assert selected.adapter.id == CODEX
        choice = service.choose(None, role="coder", agent=CODEX, pick=selected.pick)
        assert (choice.account.slot if choice.account else None, choice.source) == (
            top.slot,
            "preferred",
        ), pick


def test_a_roles_binding_to_a_disabled_account_names_no_agent(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #14, nit 6: ``choose`` skips a disabled binding, so the agent goes on to the
    order too; the account a launch NAMES (``--account``) is the operator's word, disabled
    or not."""
    claude = _claude("c@example.com")
    codex = _codex("x@example.com")
    _usage(monkeypatch, {claude.slot: 10.0, codex.slot: 10.0})
    config = load_config()
    config.team.profiles["coder"] = RoleLaunchProfile(account=str(codex.slot))
    save_config(config)
    assert agent_launch.resolve("coder").adapter.id == CODEX, "bound, enabled: its agent"

    service.set_disabled(str(codex.slot), True)

    selected = agent_launch.resolve("coder")
    assert (selected.adapter.id, selected.source) == (CLAUDE, "accounts")
    named = agent_launch.resolve("coder", account=str(codex.slot))
    assert (named.adapter.id, named.source) == (CODEX, "account")


def test_a_display_asks_the_order_alone_and_reads_no_usage(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude = _claude("c@example.com")
    codex = _codex("x@example.com")
    service.move(str(codex.slot), "top")
    reads = _usage(monkeypatch, {codex.slot: 99.0, claude.slot: 5.0})

    shown = agent_launch.resolve("coder", measure=False)

    assert (shown.adapter.id, shown.source) == (CODEX, "accounts")
    assert reads == [], "a display never reads usage"
    assert agent_launch.resolve("coder").adapter.id == CLAUDE, "a launch does, and moves on"
    assert len(reads) == 1


# --------------------------------------------------------------------------- the pick's inputs


def test_the_hand_offs_pick_never_re_picks_the_account_it_is_leaving(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The excluded account has headroom, so only the exclusion keeps it from being picked."""
    claude = _claude("c@example.com")
    codex = _codex("x@example.com")
    service.move(str(codex.slot), "top")
    _usage(monkeypatch, {codex.slot: 5.0, claude.slot: 40.0})
    assert service.choose_any(live=True).agent == CODEX

    moved = service.choose_any(exclude=[codex.slot], live=True)

    assert moved.agent == CLAUDE and moved.account is not None
    assert moved.account.slot == claude.slot
    listed = service.list_accounts()
    assert codex.slot not in [a.slot for a in service.usable_accounts(listed, exclude=[codex.slot])]
    assert codex.slot in [a.slot for a in service.usable_accounts(listed)]


def test_a_disabled_account_is_never_usable_whatever_the_pick_reads(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude = _claude("c@example.com")
    codex = _codex("x@example.com")
    service.move(str(codex.slot), "top")
    service.set_disabled(str(codex.slot), True)
    listed = service.list_accounts()
    assert codex.slot not in [a.slot for a in service.usable_accounts(listed)]

    for settings in ({"pick": "default"}, {"pick": "headroom"}):
        _pick(**settings)
        _usage(monkeypatch, {})
        assert agent_launch.resolve("coder").adapter.id == CLAUDE, settings
        assert service.choose_any(agents=[CODEX, CLAUDE]).agent == CLAUDE
    assert claude.slot in [a.slot for a in service.usable_accounts(listed)]


def test_a_hand_over_reads_live_and_a_launch_through_the_cache(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude = _claude("c@example.com")
    codex = _codex("x@example.com")
    service.move(str(codex.slot), "top")
    cached, live = _split_usage(
        monkeypatch,
        cached={codex.slot: 99.0, claude.slot: 10.0},
        live={codex.slot: 10.0, claude.slot: 10.0},
    )

    by_hand_over = service.choose_any(live=True)
    assert by_hand_over.agent == CODEX and (len(live), len(cached)) == (1, 0)

    by_launch = service.choose_any()
    assert by_launch.agent == CLAUDE and (len(live), len(cached)) == (1, 1)


def test_the_agent_rung_is_asked_for_the_pick_and_the_accounts_settings(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``prefer`` and ``switch_at`` reach the rung's pick, as they reach ``choose_any``'s."""
    claude = _claude("c@example.com")
    codex = _codex("x@example.com")
    _pick(switch_at=50)
    _usage(monkeypatch, {claude.slot: 60.0, codex.slot: 40.0})

    assert agent_launch.resolve("coder").adapter.id == CODEX, "60% is over a 50% line"

    _pick(switch_at=70)
    assert agent_launch.resolve("coder").adapter.id == CLAUDE


def test_a_limit_mark_for_the_launchs_family_sends_it_past_the_account_the_rung_picked(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rung ran before the model was known: an Opus mark on the account it picked shuts
    that account to an Opus launch, which then takes the headroom rung's answer."""
    first = _claude("c@example.com")
    second = _claude("d@example.com")
    codex = _codex("x@example.com")
    _usage(monkeypatch, {first.slot: 10.0, second.slot: 20.0, codex.slot: 30.0})
    selected = agent_launch.resolve("coder")
    assert selected.pick is not None and selected.pick.account is not None
    assert selected.pick.account.slot == first.slot
    plain = service.choose(None, role="coder", agent=CLAUDE, pick=selected.pick, family="opus")
    assert plain.account is not None and plain.account.slot == first.slot

    service.remember_limit(first.slot, "opus", datetime.now(tz=UTC) + timedelta(days=2))
    marked = service.choose(None, role="coder", agent=CLAUDE, pick=selected.pick, family="opus")

    assert marked.account is not None and marked.account.slot == second.slot
    assert marked.source == "headroom"


def test_a_machine_with_no_store_is_not_given_one_by_the_agent_rung(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _installed(monkeypatch, claude="/bin/claude", codex="/bin/codex")
    _usage(monkeypatch, {})
    _codex("x@example.com")
    assert not paths.db_path().exists()

    pick = service.agent_pick()

    assert (pick.agent, pick.account) == (None, None)
    assert not paths.db_path().exists(), "a pick makes no store"
    selected = agent_launch.resolve("coder")
    assert (selected.adapter.id, selected.source) == (CLAUDE, "default")


# --------------------------------------------------------------------------- the rungs around it


def test_the_accounts_come_before_the_inherited_family_before_the_operators_choice(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude = _claude("c@example.com")
    _usage(monkeypatch, {claude.slot: 10.0})
    monkeypatch.setenv(agent_launch.LAUNCH_AGENT_ENV, CODEX)
    monkeypatch.setenv(agent_launch.ACTIVE_AGENT_ENV, CODEX)
    assert agent_launch.resolve("coder").source == "accounts", "a usable account is first"
    assert agent_launch.resolve("coder").adapter.id == CLAUDE

    service.set_disabled(str(claude.slot), True)  # no account is usable now
    monkeypatch.setenv(agent_launch.ACTIVE_AGENT_ENV, CLAUDE)
    inherited = agent_launch.resolve("coder")
    assert (inherited.adapter.id, inherited.source) == (CODEX, "inherited")

    monkeypatch.delenv(agent_launch.LAUNCH_AGENT_ENV)
    monkeypatch.setenv(agent_launch.ACTIVE_AGENT_ENV, CODEX)
    _default_agent(CLAUDE)
    operator = agent_launch.resolve("coder")
    assert (operator.adapter.id, operator.source) == (CODEX, "operator"), "over the user's"


def test_a_named_wrapper_that_names_no_agent_stays_the_default_agents_with_accounts_present(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude = _claude("c@example.com")
    codex = _codex("x@example.com")
    service.move(str(codex.slot), "top")
    _usage(monkeypatch, {codex.slot: 1.0, claude.slot: 1.0})
    assert agent_launch.resolve("coder").adapter.id == CODEX

    selected = agent_launch.resolve("coder", binary="/opt/bin/my-wrapper")

    assert (selected.adapter.id, selected.source) == (CLAUDE, "default")
    assert selected.binary.binary == "/opt/bin/my-wrapper"
    assert selected.pick is None


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
