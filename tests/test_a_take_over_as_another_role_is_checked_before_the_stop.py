"""A take-over that changes the role is checked before its source is stopped.

Review of #240, finding 6. For a role change ``restart`` asked the role, the new role's
binary, the persona and the account before it stopped the source, and left the rest of
spawn's rules for that role (its label, one manager per project, the worktree and the
branch) to ``spawn``, which runs after the stop. *Take over* with Role ``manager`` from a
worktree coder holding a task sent the coder ``/exit``; spawn then forced the label to
``manager`` and failed on ``git worktree add``, the task's branch being checked out in the
tree the coder had just left; the abandoned hand-over returned the task to the pool, and
the project had no live agent. A coder with no task came back as a manager in a brand-new
tree, its uncommitted work left behind in the old one.

The acting manager's ruling. Every check ``spawn`` would apply for the new role runs before
the stop, and a take-over carries on under its source's label, in its source's tree: a
role change that would move either is refused, saying so, with the source still live,
still holding its task, nothing sent to its pane, and no worktree or branch made. A role
change that keeps both works as it did, and a take-over with no role change is untouched.

Driven as ``tests/test_fleet_service.py`` drives the fleet: the real store in an isolated
home, real git under ``tmp_path``, and that suite's in-memory ``FakeTmux``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aisquare.core.store import store_session
from aisquare.models import FleetAgent, ProjectInfo, TeamTask
from aisquare.services import fleet as fleet_service
from aisquare.services import team as team_service
from aisquare.services.fleet import FleetError
from tests import test_fleet_service as fleet_suite
from tests.test_fleet_service import (
    FakeTmux,
    _add_task,
    _coder,
    _events,
    _git,
    _on_disk,
    _settings,
    _task_now,
)

# The fleet suite's fixtures, bound here so pytest finds them for this module's tests.
tmux = fleet_suite.tmux
clock = fleet_suite.clock
claude_on_path = fleet_suite.claude_on_path
repo = fleet_suite.repo
project = fleet_suite.project

KEEPS = "a take-over keeps its source's label and tree"
"""What every refusal of a role that would move the agent says."""


def _branch(tree: Path) -> str:
    return _git("rev-parse", "--abbrev-ref", "HEAD", cwd=tree)


def _holding(project: ProjectInfo, source: FleetAgent, tmp_path: Path) -> TeamTask:
    """The source on the board, its transcript on disk, its task claimed and in work."""
    assert source.task_id is not None and source.session_id is not None
    _on_disk(source, tmp_path)
    team_service.claim_task(source.task_id, session_ref=source.session_id)
    held = _task_now(source.task_id)
    assert (held.status, held.claimed_by) == ("doing", source.session_id)
    return held


def _world(tmux: FakeTmux, project: ProjectInfo) -> dict[str, object]:
    """Everything a refused take-over must leave as it found it."""
    root = project.root
    with store_session() as store:
        rows = [
            (row.id, row.label, row.role, row.ended_at) for row in store.fleet_agents(project.id)
        ]
    return {
        "typed": list(tmux.typed),
        "windows killed": list(tmux.killed),
        "windows started": len(tmux.spawned),
        "rows": rows,
        "worktrees": _git("worktree", "list", "--porcelain", cwd=root).splitlines(),
        "branches": _git("branch", "--list", "--format=%(refname:short)", cwd=root).splitlines(),
        "board": {kind: _events(project, kind) for kind in ("agent_exited", "task_released")},
    }


def _untouched(
    tmux: FakeTmux, project: ProjectInfo, source: FleetAgent, before: dict[str, object]
) -> None:
    """The acting manager's pin for a refused take-over, one line per clause."""
    after = _world(tmux, project)
    assert after["typed"] == before["typed"], "nothing was sent to the source's pane"
    assert after["windows killed"] == before["windows killed"]
    assert after["rows"] == before["rows"], "the source's row is as it was"
    if source.task_id is not None:
        held = _task_now(source.task_id)
        assert (held.status, held.claimed_by) == ("doing", source.session_id), (
            "the source still holds its task"
        )
    assert after["worktrees"] == before["worktrees"], "no worktree was made"
    assert after["branches"] == before["branches"], "no branch was made"
    assert after == before


def test_a_take_over_as_the_manager_leaves_a_coder_holding_its_task_running(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, tmp_path: Path
) -> None:
    """The finding's repro: a worktree coder holding task T, no manager live, *Take over*
    with Role ``manager``. The coder was sent ``/exit`` as a hand-over; spawn then forced
    the label to ``manager`` and ``git worktree add .aisquare-worktrees/manager <T's
    branch>`` failed, that branch being checked out in the coder's tree; the abandoned
    hand-over returned T to the pool, and the project was left with no live agent."""
    task = _add_task(project, "Ship auth")
    source = fleet_service.spawn(project, "coder", task_id=task.id, worktree=True).agent
    _holding(project, source, tmp_path)
    before = _world(tmux, project)

    with pytest.raises(FleetError) as refused:
        fleet_service.hand_off(project, source.label, mode="take_over", role="manager")

    _untouched(tmux, project, source, before)
    said = str(refused.value)
    assert KEEPS in said and "'manager'" in said and "nothing was stopped" in said
    [listed] = fleet_service.list_agents(project)
    assert listed.agent.id == source.id and listed.state != "exited"


def test_a_take_over_as_the_manager_never_leaves_uncommitted_work_behind_in_the_old_tree(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """A coder with no task. Nothing failed for it: it was stopped and "taken over" as a
    manager labelled ``manager``, in a brand-new tree cut from the root's HEAD, and what it
    had not committed stayed behind in the tree it left."""
    source = fleet_service.spawn(project, "coder", worktree=True).agent
    tree = Path(source.cwd)
    (tree / "wip.txt").write_text("not committed\n", encoding="utf-8")
    before = _world(tmux, project)

    with pytest.raises(FleetError, match=KEEPS):
        fleet_service.hand_off(project, source.label, mode="take_over", role="manager")

    _untouched(tmux, project, source, before)
    assert _git("status", "--porcelain", cwd=tree) == "?? wip.txt"


def test_a_take_over_as_a_second_manager_is_refused_before_its_source_is_stopped(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, tmp_path: Path
) -> None:
    """One manager per project is spawn's rule, and it was asked after the stop: the coder
    was gone, its task back in the pool, and then the take-over was refused."""
    manager = fleet_service.spawn(project, "manager").agent
    task = _add_task(project, "Ship auth")
    source = _coder(project, task_id=task.id)
    _holding(project, source, tmp_path)
    before = _world(tmux, project)

    with pytest.raises(FleetError, match="already has a manager") as refused:
        fleet_service.hand_off(project, source.label, mode="take_over", role="manager")

    _untouched(tmux, project, source, before)
    assert manager.id in str(refused.value)


def test_a_take_over_of_the_manager_as_another_role_is_refused_with_the_manager_running(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, tmp_path: Path
) -> None:
    """The other way round: the manager's label is reserved for its role, so a coder cannot
    keep it. Spawn said so after the manager had been stopped, and the project had none."""
    manager = fleet_service.spawn(project, "manager").agent
    _on_disk(manager, tmp_path)
    before = _world(tmux, project)

    with pytest.raises(FleetError, match="reserved for the manager role") as refused:
        fleet_service.restart(project, "manager", role="coder")

    _untouched(tmux, project, manager, before)
    assert KEEPS in str(refused.value)


def test_a_take_over_as_another_role_that_would_start_in_another_tree_is_refused(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The label stays and the tree would not: ``[fleet] worktree_dir`` moved after the
    coder started, so today its label names a worktree somewhere else. The coder was
    stopped, and ``git worktree add`` there failed on its task's branch, still checked out
    in the tree it had left."""
    task = _add_task(project, "Ship auth")
    source = fleet_service.spawn(project, "coder", task_id=task.id, worktree=True).agent
    _holding(project, source, tmp_path)
    _settings(monkeypatch, worktree_dir=".trees-elsewhere")
    before = _world(tmux, project)

    with pytest.raises(FleetError) as refused:
        fleet_service.hand_off(project, source.label, mode="take_over", role="tester")

    _untouched(tmux, project, source, before)
    assert KEEPS in str(refused.value) and ".trees-elsewhere" in str(refused.value)
    assert not (project.root / ".trees-elsewhere").exists()


def test_a_refused_take_over_of_an_exited_teammate_leaves_its_row_and_last_screen(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """Nothing is left to stop, and the rule is the same: the 💤 coder came back as a
    manager in a new tree, away from what it had not committed."""
    source = fleet_service.spawn(project, "coder", worktree=True).agent
    (Path(source.cwd) / "wip.txt").write_text("not committed\n", encoding="utf-8")
    tmux.die(source.pane_id, 0)
    [listed] = fleet_service.handoff_sources(project)
    assert listed.state == "exited"
    before = _world(tmux, project)

    with pytest.raises(FleetError, match=KEEPS):
        fleet_service.hand_off(project, source.label, mode="take_over", role="manager")

    _untouched(tmux, project, source, before)
    assert tmux.facts[source.pane_id].dead, "its last screen still stands"
    [still] = fleet_service.list_agents(project)
    assert (still.agent.id, still.state) == (source.id, "exited")


@pytest.mark.parametrize("fresh", [False, True])
def test_a_take_over_as_another_role_carries_on_in_its_sources_tree_under_its_label(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, tmp_path: Path, fresh: bool
) -> None:
    """The control: a role that keeps the label and the tree takes over as it did. The
    coder is stopped as a hand-over, and the tester runs under its label, on its task, in
    its tree as it stands: same branch, uncommitted work and all, with the claim."""
    task = _add_task(project, "Ship auth")
    source = fleet_service.spawn(project, "coder", task_id=task.id, worktree=True).agent
    tree = Path(source.cwd)
    (tree / "wip.txt").write_text("not committed\n", encoding="utf-8")
    on = _branch(tree)
    _holding(project, source, tmp_path)

    receipt = fleet_service.hand_off(
        project, source.label, mode="take_over", role="tester", fresh=fresh
    )

    started = receipt.started.agent
    assert receipt.stopped is not None and receipt.stopped.id == source.id
    assert receipt.resumed is not fresh
    assert (started.label, started.role, started.task_id) == (source.label, "tester", task.id)
    assert started.cwd == source.cwd and _branch(tree) == on
    assert _git("status", "--porcelain", cwd=tree) == "?? wip.txt"
    held = _task_now(task.id)
    assert (held.status, held.claimed_by) == ("doing", started.session_id)


def test_a_take_over_with_no_role_change_is_the_restart_it_always_was(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """The control for the other half of the ruling: with the role as it is, by ``None`` or
    by name, the manager is restarted under its fixed label, and no new rule is asked."""
    manager = fleet_service.spawn(project, "manager").agent

    again = fleet_service.hand_off(project, "manager", mode="take_over").started.agent
    named = fleet_service.restart(project, "manager", role="manager").started

    assert (again.label, again.role) == (named.label, named.role) == ("manager", "manager")
    assert len({manager.id, again.id, named.id}) == 3
    [listed] = fleet_service.list_agents(project)
    assert listed.agent.id == named.id
