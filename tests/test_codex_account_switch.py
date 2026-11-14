"""A Codex agent's hand-over, end to end on the fleet's fake tmux: fresh, onto a Codex slot.

``tests/test_codex_accounts.py`` pins the decision (which account, ``fresh``) with
the respawn stubbed out; this runs the real stop and respawn, so the replacement's
own command line says what the switch promised: Codex again, on the Codex slot with
room, with no ``--resume`` — a rollout under the old ``CODEX_HOME`` is not resumable
from another home — the hand-off prompt as its argument, and the claims kept for it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aisquare.core import agents
from aisquare.core import claude_accounts as core
from aisquare.core.store import store_session
from aisquare.models import ProjectInfo, TeamSession, TeamTask
from aisquare.services import delivery
from aisquare.services import fleet as fleet_service
from aisquare.services import team as team_service
from tests import fakebin
from tests.test_codex_accounts import _rollout, _sign_in, _token_count, _window
from tests.test_fleet_service import (  # noqa: F401 - fixtures, by name
    FakeTmux,
    _command,
    _first_lines,
    _flag,
    clock,
    project,
    repo,
    tmux,
)


@pytest.fixture
def codex_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    script = fakebin.executable_fake(
        tmp_path / "codex-bin",
        "codex",
        windows='echo fake codex: "%*"\nset /p line=',
        posix='echo "fake codex: $*"\nread line',
    )
    fakebin.prepend_to_path(script.parent, monkeypatch)
    return script


def test_a_limited_codex_agent_is_switched_fresh_onto_the_codex_slot_with_room(
    tmux: FakeTmux,  # noqa: F811 — pytest resolves fixtures by NAME, so the import must keep it
    codex_on_path: Path,
    project: ProjectInfo,  # noqa: F811 — pytest resolves fixtures by NAME
    tmp_path: Path,
) -> None:
    now = datetime.now(tz=UTC)
    full = core.create_account("codex")
    roomy = core.create_account("codex")
    core.create_account()  # a Claude slot between them on the ladder: never the target
    for slot, email in ((full, "full@example.com"), (roomy, "roomy@example.com")):
        _sign_in(slot.config_dir, email)
    _rollout(full.config_dir, [_token_count(now, _window(100, 300, now + timedelta(hours=2)))])
    _rollout(roomy.config_dir, [_token_count(now, _window(15, 300, now + timedelta(hours=4)))])

    agent = fleet_service.spawn(
        project, "coder", agent="codex", worktree=False, account=str(full.slot)
    ).agent
    assert agent.agent == "codex" and agent.account_slot == full.slot
    assert _flag(_command(tmux), "--account") == str(full.slot)
    session_id = "codex-session-one"
    rollout = full.config_dir / "sessions" / "rollout-old.jsonl"
    rollout.write_text("{}\n", encoding="utf-8")
    with store_session() as store:
        store.upsert_session(
            TeamSession(
                id=session_id,
                project_id=project.id,
                role="coder",
                started_at=now,
                last_seen_at=now,
                transcript_path=str(rollout),
                agent="codex",
                account=str(full.config_dir),
            )
        )
        stored = store.get_fleet_agent(agent.id)
        assert stored is not None
        store.upsert_fleet_agent(stored.model_copy(update={"session_id": session_id}))
        store.mark_limited(session_id, now + timedelta(hours=2))
        task, _created = store.upsert_task(
            TeamTask(
                id="tsk_codex1",
                project_id=project.id,
                key="codex-task",
                title="Codex task",
                role="coder",
                created_at=now,
                updated_at=now,
            )
        )
    assert team_service.claim_task(task.id, session_ref=session_id).status == "doing"

    receipt = fleet_service.switch(project, agent.label, reason="session limit", automatic=True)

    assert (receipt.from_slot, receipt.to_slot, receipt.resumed) == (full.slot, roomy.slot, False)
    command = _command(tmux)
    assert _flag(command, "--account") == str(roomy.slot)  # the Codex slot with room
    assert "--resume" not in command and str(rollout) not in command  # fresh, never resumed
    assert receipt.started.agent == "codex" and receipt.started.account_slot == roomy.slot
    assert any("cannot resume a thread" in note for note in receipt.notes)
    # The hand-off prompt: Codex takes it as its positional argument, never typed in, from
    # the file its launch reads (P0-W1a): never in the window's command.
    assert not [t for t in tmux.typed if t[1] == "paste"] and delivery.FIRST_PROMPT_FLAG in command
    assert not any("under another Codex account" in part for part in command), command
    [first] = _first_lines(tmux, receipt.started.pane_id)
    assert "under another Codex account" in first, first
    with store_session() as store:
        moved = store.get_task(task.id)
        kinds = [e.kind for e in store.recent_events(project.id, limit=10)]
    # The claims wait for the replacement, never back to the pool: Codex's session id
    # binds at its own start hook, which takes them over then (no hook runs here).
    assert moved is not None and moved.status == "doing"
    assert "switched" in kinds and "task_released" not in kinds and "agent_exited" not in kinds


def test_a_switch_leaves_the_plain_codex_home_a_codex_agent_with_no_slot_runs_on(
    tmux: FakeTmux,  # noqa: F811 — pytest resolves fixtures by NAME, so the import must keep it
    codex_on_path: Path,
    project: ProjectInfo,  # noqa: F811 — pytest resolves fixtures by NAME
    tmp_path: Path,
) -> None:
    """The Codex twin of gate F2 of #113 (#123): a Codex agent its spawn put on no account
    runs on the plain codex home, slot 0, which its session's config dir names. The switch
    leaves that slot, as a Claude Code agent's leaves the plain claude's slot 1: ``--to 0``
    is refused with nothing stopped, and with no ``--to`` the move is to the Codex slot,
    though the plain home has the more room. Read off the record's slot alone, the plain
    home was no account to leave."""
    now = datetime.now(tz=UTC)
    roomy = core.create_account("codex")  # an account of Codex added: its plain home counts
    plain = core.default_account(agent="codex").config_dir
    plain.mkdir(parents=True, exist_ok=True)
    assert agents.install_hooks("codex", plain)  # connected, as `agents connect codex` leaves it
    agent = fleet_service.spawn(project, "coder", agent="codex", worktree=False).agent
    assert agent.agent == "codex" and agent.account_slot is None
    for home, email in ((plain, "plain@example.com"), (roomy.config_dir, "roomy@example.com")):
        _sign_in(home, email)
    _rollout(plain, [_token_count(now, _window(5, 300, now + timedelta(hours=4)))], name="p")
    _rollout(roomy.config_dir, [_token_count(now, _window(15, 300, now + timedelta(hours=4)))])
    session_id = "codex-plain-session"
    with store_session() as store:
        store.upsert_session(
            TeamSession(
                id=session_id,
                project_id=project.id,
                role="coder",
                started_at=now,
                last_seen_at=now,
                agent="codex",
                account=str(plain),
            )
        )
        stored = store.get_fleet_agent(agent.id)
        assert stored is not None
        store.upsert_fleet_agent(stored.model_copy(update={"session_id": session_id}))
    plain_slot = core.kind("codex").plain_slot
    spawned = len(tmux.spawned)

    with pytest.raises(fleet_service.FleetError, match=rf"already runs on .*\(slot {plain_slot}\)"):
        fleet_service.switch(project, agent.label, to=str(plain_slot))
    assert len(tmux.killed) == 0 and len(tmux.spawned) == spawned

    receipt = fleet_service.switch(project, agent.label)

    assert (receipt.from_slot, receipt.to_slot) == (plain_slot, roomy.slot)
    assert _flag(_command(tmux), "--account") == str(roomy.slot)
