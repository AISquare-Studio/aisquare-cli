"""Another agent's plain home is one of the accounts only once aisquare has taken it on
(review r40 of #113, finding 1).

A Claude Code user who merely has Codex installed and logged in (``codex login`` outside
aisquare) had ``~/.codex`` listed as a Codex account (slot 0), signed in: the accounts rung
of an unpinned launch, above ``[agents] default``, sent ``fleet spawn``/``launch`` to it
whenever their Claude account was at a limit, and a one-account limit with ``on_limit =
"switch"`` crossed to it, the brief going to another provider. 0.9.2 stayed on Claude Code
and waited for the reset. Now that home counts once aisquare has connected it (``agents
connect``, install.sh ``--agent codex``) or added an account of its agent (``accounts add
--agent codex``), or arranged it itself (``core.plain_home_counts``); until then a Claude
Code user's routing and hand-over are 0.9.2's. Claude Code's own plain slot is listed
always, as it always was.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aisquare.core import agents
from aisquare.core import claude_accounts as core
from aisquare.core.store import store_session
from aisquare.models import FleetAgent, ProjectInfo, TeamSession
from aisquare.services import agent_launch, handoff
from aisquare.services import agents as agents_service
from aisquare.services import claude_accounts as service
from aisquare.services import team as team_service
from tests.test_codex_account_switch import codex_on_path  # noqa: F401 - fixture, by name
from tests.test_codex_accounts import _sign_in as _codex_sign_in
from tests.test_fleet_service import (  # noqa: F401 - fixtures, by name
    claude_on_path,
    clock,
    project,
    repo,
    tmux,
)
from tests.test_hand_off_across_agents import _claude_slot
from tests.test_one_priority_order_across_agents import (  # noqa: F401 - fixture, by name
    _claude,
    machine,
)

CLAUDE = core.AGENT
CODEX = "codex"


def _logged_in_plain_codex() -> Path:
    """``~/.codex`` as ``codex login`` outside aisquare leaves it: signed in, no hooks."""
    plain = core.default_account(agent=CODEX).config_dir
    plain.mkdir(parents=True, exist_ok=True)
    _codex_sign_in(plain, "mine@example.com")
    assert not agents.hooks_installed(CODEX, plain)
    return plain


def _listed() -> list[tuple[str, int]]:
    return [(account.agent, account.slot) for account in core.list_accounts()]


def _plain_codex() -> tuple[str, int]:
    return CODEX, core.kind(CODEX).plain_slot


def test_a_logged_in_codex_never_connected_takes_no_launch_until_connected_or_added(
    machine: Path,  # noqa: F811
    codex_on_path: Path,  # noqa: F811 - `handoff.other_agents` asks PATH itself
) -> None:
    """One Claude Code account at its 5-hour limit, and Codex logged in outside aisquare: the
    unpinned launch stays on Claude Code (0.9.2's least-bad pick), nothing would cross. Once
    connected, the home is an account and takes it; disconnected, it is none again; once an
    account of Codex is added, the plain home is one too."""
    claude = _claude("c@example.com")
    service.remember_limit(claude.slot, "session", datetime.now(tz=UTC) + timedelta(hours=3))
    plain = _logged_in_plain_codex()

    assert _plain_codex() not in _listed(), "Codex installed and logged in is not an account"
    assert core.find_account(core.kind(CODEX).plain_slot) is not None, "named, it is found"
    assert service.agent_pick().agent == CLAUDE
    assert agent_launch.resolve("coder").adapter.id == CLAUDE
    assert not handoff.somewhere_across(claude.slot, CLAUDE), "no crossing to it"
    assert not service.somewhere_to_hand_over(claude.slot, agent=CODEX)

    agents_service.connect(CODEX, plain)  # `agents connect codex`, install.sh --agent codex
    assert _plain_codex() in _listed()
    assert agent_launch.resolve("coder").adapter.id == CODEX
    assert handoff.somewhere_across(claude.slot, CLAUDE)

    agents_service.disconnect(CODEX, plain)
    assert _plain_codex() not in _listed(), "disconnected, it is the user's own again"
    assert agent_launch.resolve("coder").adapter.id == CLAUDE
    assert not handoff.somewhere_across(claude.slot, CLAUDE)

    core.create_account(CODEX)  # `accounts add --agent codex` makes a slot of Codex's
    assert _plain_codex() in _listed(), "Codex's accounts are aisquare's: its plain home too"


def test_arranging_the_plain_codex_by_its_number_adds_it(machine: Path) -> None:  # noqa: F811
    """Named in an arrangement (an alias, the order, a default), the plain home is added:
    the operator asked for it by its number."""
    _logged_in_plain_codex()
    number = str(core.kind(CODEX).plain_slot)
    assert _plain_codex() not in _listed()

    service.set_alias(number, "codex-home")

    assert _plain_codex() in _listed()
    assert service.resolve("codex-home").slot == core.kind(CODEX).plain_slot


def _limited_claude_session(work: ProjectInfo, slot: int, config_dir: Path) -> TeamSession:
    now = datetime.now(tz=UTC)
    session = TeamSession(
        id="claude-limited",
        project_id=work.id,
        role="coder",
        started_at=now,
        last_seen_at=now,
        agent=CLAUDE,
        account=str(config_dir),
    )
    with store_session() as store:
        store.upsert_session(session)
        store.upsert_fleet_agent(
            FleetAgent(
                id="agt_claude",
                project_id=work.id,
                label="coder-1",
                role="coder",
                agent=CLAUDE,
                binary="claude",
                pane_id="%9",
                session_id=session.id,
                cwd=work.root,
                account_slot=slot,
                created_at=now - timedelta(hours=1),
            )
        )
    return session


def test_a_one_account_limit_waits_with_a_codex_aisquare_never_connected(
    project: ProjectInfo,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``on_limit = "switch"`` with one Claude Code account starts no hand-over worker while
    Codex is merely logged in, as 0.9.2 waited; connected, the worker is started (it may
    cross, ``cross_agent_handover``)."""
    from aisquare.services import hooks

    claude = _claude_slot(monkeypatch, 20)
    plain = _logged_in_plain_codex()
    session = _limited_claude_session(project, claude.slot, claude.config_dir)
    started: list[list[str]] = []
    monkeypatch.setattr(hooks, "_detach", started.append)
    failure = team_service.TurnFailure(session=session, error="rate_limit", notice=None)

    plan = hooks._plan_hand_over(session.id, error="rate_limit", message=None)
    assert plan is not None and not plan.go, "one Claude Code account: wait, as 0.9.2 did"
    hooks._hand_over_if_configured(failure, plan)
    assert started == []

    agents_service.connect(CODEX, plain)
    plan = hooks._plan_hand_over(session.id, error="rate_limit", message=None)
    assert plan is not None and plan.go
    hooks._hand_over_if_configured(failure, plan)
    assert len(started) == 1 and "hand-over" in started[0]
