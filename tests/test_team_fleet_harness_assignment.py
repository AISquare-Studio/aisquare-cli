"""The ``--task`` assignment respects the task's real state, on every path it reaches the agent.

Two instructions reach a ``fleet spawn <role> --task`` worker within seconds of each
other: the kickoff prompt typed into its pane, and the ``<aisquare-assignment>`` block
the SessionStart hook injects. Both must say the same thing, and what they say must
follow the task's ACTUAL claim and status — a worker told to claim a task it already
holds, or one another live session holds, or one that is already in review, is a
worker with a standing instruction it cannot satisfy. ``AISQUARE_TASK_ID`` is fixed in
the pane's environment, so the block is re-injected on every resume; a wrong one is
wrong for the session's whole life.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aisquare.core import store as store_module
from aisquare.core.store import store_session
from aisquare.models import ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services import team
from tests.test_fleet_service import FakeTmux, claude_on_path, project, repo, tmux  # noqa: F401

CLAIM = "Claim THIS task"
INSPECT = "Inspect THIS task and its evidence"


@pytest.fixture
def work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.chdir(root)
    team.activate()
    return root


def _start(work: Path, session: str, role: str, task_id: str, source: str = "startup") -> str:
    """The session-start context a worker launched with this role and task receives."""
    with pytest.MonkeyPatch.context() as env:
        env.setenv("AISQUARE_ROLE", role)
        env.setenv("AISQUARE_TASK_ID", task_id)
        return team.hook_session_start(session, work, source)


def _assignment(context: str) -> str:
    start = context.index("<aisquare-assignment>")
    end = context.index("</aisquare-assignment>")
    return context[start:end]


# --- the session-start block -------------------------------------------------------------


def test_an_unclaimed_todo_task_tells_a_coder_seat_to_claim_it(work: Path) -> None:
    task, _ = team.add_task("Build login", role="coder", cwd=work, detail="the contract")
    for seat in ("coder", "coder2"):
        block = _assignment(_start(work, f"w-{seat}", seat, task.id))
        assert f"{CLAIM}: `asq task claim {task.id} --as w-{seat}"[:60] in block, seat
        assert "[todo]" in block and "unclaimed" in block
    # Nothing was claimed by reading the context: claims stay an explicit command.
    assert team.show_task(task.id).claimed_by is None


def test_a_non_coder_inspects_an_unclaimed_task_and_is_never_told_to_claim(work: Path) -> None:
    task, _ = team.add_task("Build login", role="coder", cwd=work)
    for role in ("tester", "reviewer", "ui-tester", "validator", "manager"):
        block = _assignment(_start(work, f"w-{role}", role, task.id))
        assert CLAIM not in block, role
        assert INSPECT in block and "Preserve its existing ownership" in block, role


def test_a_task_this_session_already_holds_is_continued_not_reclaimed(work: Path) -> None:
    task, _ = team.add_task("Build login", role="coder", cwd=work)
    first = _start(work, "w1", "coder", task.id)
    assert CLAIM in first
    team.claim_task(task.id, session_ref="w1")
    # /compact, /clear and resume all re-run session-start with the same env.
    resumed = _assignment(_start(work, "w1", "coder", task.id, source="resume"))
    assert CLAIM not in resumed
    assert "already yours" in resumed and "do not claim it again" in resumed
    assert "[doing @w1]" in resumed


def test_a_task_another_live_session_holds_names_the_holder_and_says_stop(work: Path) -> None:
    task, _ = team.add_task("Build login", role="coder", cwd=work)
    other = _start(work, "other-coder", "coder", task.id)
    assert CLAIM in other
    team.claim_task(task.id, session_ref="other-coder")
    block = _assignment(_start(work, "w2", "coder", task.id))
    assert CLAIM not in block
    assert "owned by other-co (coder)" in block
    assert "Do not claim it" in block and "report and stop" in block
    # A verifier assigned the same task is told the same owner, not to inspect a claim.
    tester = _assignment(_start(work, "t1", "tester", task.id))
    assert "owned by other-co (coder)" in tester and CLAIM not in tester


def test_a_lapsed_claim_by_a_gone_session_is_claimable_again(work: Path) -> None:
    task, _ = team.add_task("Build login", role="coder", cwd=work)
    _start(work, "gone", "coder", task.id)
    team.claim_task(task.id, session_ref="gone")
    with store_session() as store:
        # The lease is the liveness signal the store's own claim_task honours:
        # an expired one means the previous claimant is presumed gone.
        expired = (datetime.now(tz=UTC) - timedelta(minutes=5)).isoformat()
        store._conn.execute(  # type: ignore[attr-defined]
            "UPDATE team_task SET claim_expires_at = ? WHERE id = ?", (expired, task.id)
        )
        store._conn.commit()  # type: ignore[attr-defined]
    block = _assignment(_start(work, "w3", "coder", task.id))
    assert CLAIM in block
    assert "lapsed" in block and "gone" in block


@pytest.mark.parametrize("status", ["review", "done", "dropped"])
def test_a_finished_task_is_inspected_only(work: Path, status: str) -> None:
    task, _ = team.add_task("Build login", role="coder", cwd=work)
    _start(work, "w1", "coder", task.id)
    team.claim_task(task.id, session_ref="w1")
    if status == "review":
        team.review_task(task.id, session_ref="w1")
    else:
        with store_session() as store:
            store.set_task_status(task.id, status)  # type: ignore[arg-type]
    for role in ("coder", "tester"):
        block = _assignment(_start(work, f"w-{role}-{status}", role, task.id))
        assert CLAIM not in block, (role, status)
        assert f"[{status}" in block
        assert "Do not claim or edit it" in block, (role, status)
        assert "report and stop" in block, (role, status)


def test_a_blocked_task_asks_for_the_blocker_not_a_claim(work: Path) -> None:
    task, _ = team.add_task("Build login", role="coder", cwd=work)
    _start(work, "w1", "coder", task.id)
    team.claim_task(task.id, session_ref="w1")
    team.block_task(task.id, reason="needs spec", session_ref="w1")
    block = _assignment(_start(work, "w4", "coder", task.id))
    assert CLAIM not in block
    assert "blocked" in block and "report what would unblock it, and stop" in block


# --- a hand-set AISQUARE_TASK_ID ------------------------------------------------------------


def test_an_ambiguous_task_prefix_keeps_the_board_and_cycle_and_rules(work: Path) -> None:
    team.add_task("Build login", role="coder", cwd=work)
    team.add_task("Build logout", role="coder", cwd=work)
    # Every task id starts with `tsk_`, so a hand-exported prefix this short is
    # ambiguous — the store raises rather than guess.
    context = _start(work, "w5", "coder", "tsk_")
    assert "<aisquare-team>" in context and "Your standing cycle (coder)" in context
    assert "Working rules:" in context
    assert "Assigned task 'tsk_' is ambiguous" in context
    assert "STOP and report; do not pick another" in context
    assert "<aisquare-assignment>" not in context
    with store_session() as store:
        assert store.get_session("w5") is not None, "the session row was still registered"


def test_a_store_error_resolving_the_task_is_a_notice_not_an_empty_context(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task, _ = team.add_task("Build login", role="coder", cwd=work)

    def broken(self: object, ref: str) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store_module.SqliteStore, "get_task", broken)
    context = _start(work, "w6", "coder", task.id)
    assert "Your standing cycle (coder)" in context
    assert f"Assigned task {task.id!r} could not be read" in context
    assert "database is locked" in context
    assert "STOP and report; do not pick another" in context


# --- the fleet kickoff typed into the pane --------------------------------------------------


def _kickoff(server: FakeTmux, receipt: fleet_service.SpawnReceipt) -> str:
    pasted = [text for pane, kind, text in server.typed if pane == receipt.agent.pane_id]
    assert pasted, "a --task spawn types a kickoff"
    return pasted[0]


def _fleet_task(info: ProjectInfo, title: str) -> str:
    with store_session() as store:
        store.ensure_project(info)
    task, _ = team.add_task(title, role="coder", cwd=info.root)
    return task.id


def test_the_kickoff_tells_coders_to_claim_and_everyone_else_to_inspect(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    task = _fleet_task(project, "Wire the auth flow")
    coder = _kickoff(tmux, fleet_service.spawn(project, "coder", task_id=task))
    assert "Claim it before editing" in coder and task in coder
    tmux.typed.clear()
    # Three verifying roles plus the coder fill the default cap of four agents.
    for role in ("tester", "reviewer", "ui-tester"):
        prompt = _kickoff(tmux, fleet_service.spawn(project, role, task_id=task, worktree=False))
        assert "Claim" not in prompt, role
        assert "Inspect it and its evidence" in prompt, role
        assert "preserve its existing ownership" in prompt and "do not claim it" in prompt, role
        assert task in prompt, role
        tmux.typed.clear()


def test_the_kickoff_and_the_session_start_block_agree(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
) -> None:
    """The two instructions are the same protocol, worded from the same vocabulary."""
    task = _fleet_task(project, "Wire the auth flow")
    for role, verb in (("coder", "Claim"), ("tester", "Inspect")):
        prompt = _kickoff(tmux, fleet_service.spawn(project, role, task_id=task, worktree=False))
        block = _assignment(_start(project.root, f"hook-{role}", role, task))
        assert verb in prompt and verb.upper() in block.upper(), role
        assert ("Claim" in prompt) == (CLAIM in block), role
        tmux.typed.clear()
