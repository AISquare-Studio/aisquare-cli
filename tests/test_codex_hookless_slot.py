"""No Codex agent starts blind on a slot without aisquare's hooks (SMOKE12 O1 of #113).

On real Codex 0.166.0, ``fleet switch coder2 --to 2`` onto a Codex slot made by hand (a
login copied in, past the hook install ``accounts add`` does) said nothing and started the
agent there with no ``hooks.json``: it ran its hand-off prompt, but no session registered,
its claim stayed on the ended session, and no board update reached it. The next switch then
brought that ended session back to the board as a live coder (``mark_handover`` cleared its
``ended_at``), where it stayed. A spawn, restart or switch now installs the hooks where the
window starts before anything is stopped or started, or refuses saying why; and a hand-over
mark leaves an ended session ended. On the fleet's fake tmux, as ``test_codex_account_switch``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from aisquare.core import agents
from aisquare.core import claude_accounts as core
from aisquare.core.agent_adapters import codex_config
from aisquare.core.store import store_session
from aisquare.models import ClaudeAccount, FleetAgent, ProjectInfo, TeamSession
from aisquare.services import fleet as fleet_service
from tests.test_codex_account_switch import codex_on_path  # noqa: F401 - fixture, by name
from tests.test_codex_accounts import _sign_in
from tests.test_fleet_service import (  # noqa: F401 - fixtures, by name
    FakeTmux,
    clock,
    project,
    repo,
    tmux,
)


def _slot(email: str) -> ClaudeAccount:
    """A Codex slot as a hand-made rig leaves one: signed in, no hooks."""
    slot = core.create_account("codex")
    _sign_in(slot.config_dir, email)
    assert not (slot.config_dir / "hooks.json").exists()
    return slot


def _bound(work: ProjectInfo, agent: FleetAgent, slot: ClaudeAccount, sid: str) -> None:
    """The agent's row bound to session ``sid``, as its first hook binds a Codex thread."""
    now = datetime.now(tz=UTC)
    rollout = slot.config_dir / "sessions" / f"rollout-{sid}.jsonl"
    rollout.parent.mkdir(parents=True, exist_ok=True)
    rollout.write_text("{}\n", encoding="utf-8")
    with store_session() as store:
        store.upsert_session(
            TeamSession(
                id=sid,
                project_id=work.id,
                role="coder",
                started_at=now,
                last_seen_at=now,
                transcript_path=str(rollout),
                agent="codex",
                account=str(slot.config_dir),
            )
        )
        stored = store.get_fleet_agent(agent.id)
        assert stored is not None
        store.upsert_fleet_agent(stored.model_copy(update={"session_id": sid}))


def _trusted(home: Path) -> int:
    """How many of aisquare's hooks in ``home`` its config.toml trusts, as Codex reads it."""
    return sum(
        1
        for key in codex_config.hook_states(home)
        if key.startswith(str((home / "hooks.json").resolve()))
    )


def _installed_note(home: Path) -> str:
    return f"Codex's hooks were not installed in {home} — installed them before it started there"


def test_a_spawn_onto_a_slot_with_no_hooks_installs_and_trusts_them_first(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    slot = _slot("one@example.com")

    receipt = fleet_service.spawn(
        project, "coder", agent="codex", worktree=False, account=str(slot.slot)
    )

    assert agents.hooks_installed("codex", slot.config_dir)
    assert _trusted(slot.config_dir) == 8, "no 'Hooks need review' to stall its start"
    assert _installed_note(slot.config_dir) in receipt.notes, receipt.notes
    # Installed, the next spawn there has nothing to say.
    again = fleet_service.spawn(
        project, "coder", agent="codex", worktree=False, account=str(slot.slot)
    )
    assert not [note for note in again.notes if "hooks were not installed" in note]


def test_a_switch_onto_a_slot_with_no_hooks_installs_them_before_the_stop(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    here, there = _slot("here@example.com"), _slot("there@example.com")
    agent = fleet_service.spawn(
        project, "coder", agent="codex", worktree=False, account=str(here.slot)
    ).agent
    _bound(project, agent, here, "codex-thread-here")

    receipt = fleet_service.switch(project, agent.label, to=str(there.slot))

    assert receipt.to_slot == there.slot and receipt.started.account_slot == there.slot
    assert agents.hooks_installed("codex", there.config_dir)
    assert _trusted(there.config_dir) == 8
    assert _installed_note(there.config_dir) in receipt.notes, receipt.notes


@pytest.mark.parametrize("verb", ["switch", "restart"])
def test_a_hand_over_onto_a_home_its_hooks_cannot_go_in_is_refused_before_the_stop(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    verb: str,
) -> None:
    """A ``hooks.json`` that is a directory cannot be written: the agent is left running, its
    window unkilled, and the refusal says why and what installs them."""
    here, there = _slot("here@example.com"), _slot("there@example.com")
    agent = fleet_service.spawn(
        project, "coder", agent="codex", worktree=False, account=str(here.slot)
    ).agent
    _bound(project, agent, here, "codex-thread-here")
    target = there if verb == "switch" else here
    hooks = target.config_dir / "hooks.json"
    if hooks.exists():
        hooks.unlink()
    hooks.mkdir()

    with pytest.raises(fleet_service.FleetError) as refused:
        if verb == "switch":
            fleet_service.switch(project, agent.label, to=str(there.slot))
        else:
            fleet_service.restart(project, agent.label)

    said = str(refused.value)
    assert f"Codex's hooks are not installed in {target.config_dir}" in said
    assert "nothing was stopped" in said and "agents connect codex --config-dir" in said
    assert agent.pane_id not in tmux.killed
    assert not [typed for typed in tmux.typed if typed[0] == agent.pane_id], "no /exit typed"
    with store_session() as store:
        row = store.get_fleet_agent(agent.id)
    assert row is not None and row.ended_at is None, "still running where it was"


def test_a_restart_puts_back_hooks_taken_out_since_its_spawn(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    slot = _slot("one@example.com")
    agent = fleet_service.spawn(
        project, "coder", agent="codex", worktree=False, account=str(slot.slot)
    ).agent
    _bound(project, agent, slot, "codex-thread-one")
    agents.remove_hooks("codex", slot.config_dir)
    assert not agents.hooks_installed("codex", slot.config_dir)

    receipt = fleet_service.restart(project, agent.label)

    assert agents.hooks_installed("codex", slot.config_dir)
    assert _installed_note(slot.config_dir) in receipt.notes, receipt.notes


def test_a_switch_of_an_agent_whose_session_ended_leaves_that_session_ended(
    tmux: FakeTmux,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    """The row still names the session that ended when its last hand-over stopped it (its
    replacement, started blind, never registered one). The mark the next switch sets on it
    is no sign of life: it stays ended, and off the board's live sessions."""
    here, there = _slot("here@example.com"), _slot("there@example.com")
    agent = fleet_service.spawn(
        project, "coder", agent="codex", worktree=False, account=str(here.slot)
    ).agent
    _bound(project, agent, here, "codex-thread-ended")
    with store_session() as store:
        store.end_session("codex-thread-ended", release_claims=False)
        ended = store.get_session("codex-thread-ended")
    assert ended is not None and ended.ended_at is not None

    fleet_service.switch(project, agent.label, to=str(there.slot))

    with store_session() as store:
        after = store.get_session("codex-thread-ended")
        live = {s.id for s in store.team_sessions(project.id) if s.ended_at is None}
    assert after is not None and after.ended_at is not None, "revived by the hand-over mark"
    assert "codex-thread-ended" not in live


def test_a_hand_over_mark_on_an_ended_session_marks_it_and_keeps_it_ended() -> None:
    from datetime import timedelta

    now = datetime.now(tz=UTC)
    with store_session() as store:
        store.upsert_session(
            TeamSession(
                id="sess-ended", project_id="prj_x", role="coder", started_at=now, last_seen_at=now
            )
        )
        store.end_session("sess-ended", release_claims=False)
        assert store.mark_handover("sess-ended", held_after=now - timedelta(minutes=5))
        marked = store.get_session("sess-ended")
        store.upsert_session(
            TeamSession(
                id="sess-live", project_id="prj_x", role="coder", started_at=now, last_seen_at=now
            )
        )
        assert store.mark_handover("sess-live", held_after=now - timedelta(minutes=5))
        live = store.get_session("sess-live")
    assert marked is not None and marked.state == "switching" and marked.ended_at is not None
    assert live is not None and live.state == "switching" and live.ended_at is None
