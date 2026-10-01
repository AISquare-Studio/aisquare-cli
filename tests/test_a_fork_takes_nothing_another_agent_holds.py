"""A fork takes nothing another agent holds: not its label, its worktree or its branch.

Review of #240, finding 3. The Spawn dialog's *Hand off from* forks by default and sends
``label=None``. ``next_label`` keeps only LIVE agents apart, so a fork of an EXITED
teammate was handed that teammate's own label back, and ``_ensure_worktree`` then reused
the tree or the branch that label already had, ignoring the commit the fork was to be cut
at. The fork ran on top of its source's uncommitted work, or moved the source's tree to
another branch; spawn's ``_supersede`` removed the source's last screen; ``fleet restart
<label>`` meant the fork from then on; and under a label an earlier agent had left, the
fork was that agent's stale branch checked out, not its source's commit.

The acting manager's ruling. A fork's label is one NOTHING holds: no row of the project in
any state, no directory where its worktree would go, no local branch that worktree would
be cut on. The dialog's ``None`` picks the next free label by that rule; a label the owner
typed that something holds is refused, naming the holder, before anything is created. A
spawn with a start point (a fork's) makes its tree new at that commit or refuses; a plain
spawn carries on in its label's tree as it always has.

Driven as ``tests/test_fleet_service.py`` drives the fleet: the real store in an isolated
home, real git under ``tmp_path``, and that suite's in-memory ``FakeTmux``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aisquare.core.store import store_session
from aisquare.models import FleetAgent, ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services.fleet import FleetError
from tests import test_fleet_service as fleet_suite
from tests.test_fleet_service import FakeTmux, _add_task, _codename, _coder, _git

# The fleet suite's fixtures, bound here so pytest finds them for this module's tests.
tmux = fleet_suite.tmux
clock = fleet_suite.clock
claude_on_path = fleet_suite.claude_on_path
repo = fleet_suite.repo
project = fleet_suite.project
plain_project = fleet_suite.plain_project


def _exits(tmux: FakeTmux, project: ProjectInfo, agent: FleetAgent) -> None:
    """The agent's process ends and the dialog's picker lists it: the 💤 row, its dead
    window still on the server."""
    tmux.die(agent.pane_id, 0)
    listed = {status.agent.id: status.state for status in fleet_service.handoff_sources(project)}
    assert listed[agent.id] == "exited"


def _branch(tree: Path) -> str:
    return _git("rev-parse", "--abbrev-ref", "HEAD", cwd=tree)


def _commit(tree: Path, name: str) -> str:
    """Commit one new file in ``tree`` and return the commit it is now on."""
    (tree / name).write_text("committed\n", encoding="utf-8")
    _git("add", name, cwd=tree)
    _git("commit", "-q", "-m", f"add {name}", cwd=tree)
    return _git("rev-parse", "HEAD", cwd=tree)


def _made(tmux: FakeTmux, project: ProjectInfo) -> dict[str, object]:
    """Everything a refused fork must leave as it found it: rows, windows, trees, branches."""
    root = project.root
    with store_session() as store:
        rows = [(row.id, row.label, row.ended_at) for row in store.fleet_agents(project.id)]
    return {
        "rows": rows,
        "windows started": len(tmux.spawned),
        "windows killed": list(tmux.killed),
        "typed": list(tmux.typed),
        "worktrees": _git("worktree", "list", "--porcelain", cwd=root).splitlines(),
        "branches": _git("branch", "--list", "--format=%(refname:short)", cwd=root).splitlines(),
    }


def test_a_fork_of_an_exited_teammate_takes_a_label_and_a_tree_of_its_own(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """The finding's repro: coder-1 has exited, and its 💤 row and dead window are still
    listed. The dialog's Label prefill is ``coder-1`` again (free among LIVE agents) and is
    sent as ``None``. The fork was labelled coder-1 and started in coder-1's tree, on top
    of its uncommitted work; spawn's supersede removed coder-1's last screen; and ``fleet
    restart coder-1`` meant the fork from then on."""
    source = fleet_service.spawn(project, "coder", worktree=True).agent
    tree = Path(source.cwd)
    (tree / "wip.txt").write_text("not committed\n", encoding="utf-8")
    on = _branch(tree)
    _exits(tmux, project, source)
    assert source.label == "coder-1"
    assert fleet_service.next_label(project, "coder") == "coder-1", "the dialog's prefill"

    fork = fleet_service.hand_off(project, source.label).started.agent

    assert fork.label == "coder-2", "a label no row holds, the exited source's included"
    own = Path(fork.cwd)
    assert fork.worktree and own != tree and not (own / "wip.txt").exists()
    assert _branch(own) != on
    # The source is as it was: its tree, its branch, its uncommitted work, its last screen.
    assert _branch(tree) == on and _git("status", "--porcelain", cwd=tree) == "?? wip.txt"
    assert tmux.killed == [] and tmux.facts[source.pane_id].dead
    with store_session() as store:
        named = store.fleet_agent_by_label(project.id, "coder-1", live_only=False)
    assert named is not None and named.id == source.id, "`fleet restart coder-1` is the source"
    states = {status.agent.label: status.state for status in fleet_service.list_agents(project)}
    assert set(states) == {"coder-1", "coder-2"} and states["coder-1"] == "exited"


def test_a_fork_never_moves_its_exited_sources_tree_to_another_branch(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """The source had a task and a clean tree, so its tree is on the TASK's branch, which
    is not the one its label names. Handed the source's label, the fork was started in that
    tree after a ``git checkout -b`` there had put it on the label's branch."""
    task = _add_task(project, "Ship auth")
    source = fleet_service.spawn(
        project, "coder", label="coder-1", task_id=task.id, worktree=True
    ).agent
    tree = Path(source.cwd)
    on = _branch(tree)
    assert on.endswith("-ship-auth"), "the task's branch, not the label's"
    _exits(tmux, project, source)

    fork = fleet_service.hand_off(project, source.label).started.agent

    assert _branch(tree) == on, "the source's tree is still on its task's branch"
    own = Path(fork.cwd)
    assert fork.label == "coder-2" and own != tree
    assert _git("rev-parse", "HEAD", cwd=own) == _git("rev-parse", "HEAD", cwd=tree)
    assert tmux.killed == [] and tmux.facts[source.pane_id].dead


def test_a_fork_is_cut_at_its_sources_commit_not_on_a_branch_an_earlier_agent_left(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """``reap`` removes an ended agent's merged worktree and keeps its branch. That label
    is free among live agents, so the fork took it, and its worktree was the stale branch
    checked out ("branch … already existed — checked it out"), not a new one cut at the
    commit its source is on."""
    earlier = fleet_service.spawn(project, "coder", label="coder-2", worktree=True).agent
    stale = _branch(Path(earlier.cwd))
    fleet_service.stop(project, "coder-2", force=True)
    assert fleet_service.reap(project).worktrees_removed == [earlier.cwd]
    left_at = _git("rev-parse", stale, cwd=project.root)
    source = fleet_service.spawn(project, "coder", worktree=True).agent
    head = _commit(Path(source.cwd), "done.txt")
    assert source.label == "coder-1" and head != left_at

    fork = fleet_service.hand_off(project, source.label).started.agent

    own = Path(fork.cwd)
    assert _git("rev-parse", "HEAD", cwd=own) == head, "cut at the commit its source is on"
    assert fork.label == "coder-3" and _branch(own) != stale
    assert _git("rev-parse", stale, cwd=project.root) == left_at, "the stale branch never moved"


@pytest.mark.parametrize("held_by", ["a worktree", "a branch"])
def test_the_next_free_label_is_one_no_tree_and_no_branch_holds_either(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, held_by: str
) -> None:
    """No row names coder-2, and it is held all the same: a worktree stands where the
    fork's would go, or the branch that worktree would be cut on exists. The fork was
    started in that tree, or on that branch; it walks past the label now."""
    source = fleet_service.spawn(project, "coder", worktree=True).agent
    head = _commit(Path(source.cwd), "done.txt")
    left = project.root / fleet_service.settings().worktree_dir / "coder-2"
    stale = f"fleet/{_codename(project)}/coder-2"
    if held_by == "a worktree":
        _git("worktree", "add", "-q", str(left), "-b", "left-behind", cwd=project.root)
    else:
        _git("branch", stale, cwd=project.root)

    fork = fleet_service.hand_off(project, source.label).started.agent

    own = Path(fork.cwd)
    assert fork.label == "coder-3" and own != left
    assert _git("rev-parse", "HEAD", cwd=own) == head, "cut at the commit its source is on"
    if held_by == "a worktree":
        assert _branch(left) == "left-behind", "the tree that was there is as it was"
    else:
        assert not left.exists() and _branch(own) != stale


@pytest.mark.parametrize(
    "holder",
    ["a live agent", "an exited agent", "a lost agent", "an ended agent", "a worktree", "a branch"],
)
def test_a_typed_label_something_holds_is_refused_by_name_before_anything_is_made(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, holder: str
) -> None:
    """The owner typed the label. A live agent's was suffixed (``scout-2``) and every other
    holder's was simply taken, with whatever stood under it. Each is a refusal now, naming
    the holder, with no row, window, worktree or branch made and nothing killed."""
    source = _coder(project)  # coder-1, running in the project's root
    root = project.root
    if holder == "a worktree":
        left = root / fleet_service.settings().worktree_dir / "scout"
        _git("worktree", "add", "-q", str(left), "-b", "left-behind", cwd=root)
        named = str(left)
    elif holder == "a branch":
        named = f"fleet/{_codename(project)}/scout"
        _git("branch", named, cwd=root)
    else:
        other = _coder(project, label="scout")
        named = other.id
        if holder == "an exited agent":
            _exits(tmux, project, other)
        elif holder == "a lost agent":
            tmux.vanish(other.pane_id)
        elif holder == "an ended agent":
            fleet_service.stop(project, "scout")
    before = _made(tmux, project)

    with pytest.raises(FleetError) as refused:
        fleet_service.hand_off(project, source.label, label="scout")

    assert named in str(refused.value), "the refusal names what holds the label"
    assert _made(tmux, project) == before


def test_a_typed_label_nothing_holds_is_the_forks(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """The control: a label that is free by every rule is taken as typed."""
    source = _coder(project)

    fork = fleet_service.hand_off(project, source.label, label="scout").started.agent

    assert fork.label == "scout" and Path(fork.cwd).name == "scout"
    assert _branch(Path(fork.cwd)) == f"fleet/{_codename(project)}/scout"


def test_a_forks_numbered_label_is_generated_as_a_spawns_is_not_checked_as_a_typed_one(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control for where the rule is asked: in ``spawn``, where every label is picked.
    A role bound under a name with an underscore has numbered labels that are no valid
    label, and a spawn uses them all the same: only a label somebody typed is checked. The
    fork's is generated the same way, so a teammate of such a role still forks."""
    monkeypatch.setattr("aisquare.cli.launch._declared_roles", lambda: {"data_eng"})
    source = fleet_service.spawn(project, "data_eng", worktree=False).agent
    assert source.label == "data_eng-1" and not fleet_service.is_label(source.label)

    fork = fleet_service.hand_off(project, source.label).started.agent

    assert (fork.label, fork.role) == ("data_eng-2", "data_eng")


def test_a_fork_in_a_plain_folder_leaves_its_exited_source_its_label_and_last_screen(
    tmux: FakeTmux, claude_on_path: Path, plain_project: ProjectInfo
) -> None:
    """Not a git repository: there is no worktree and no branch to hold a label, and the
    fork works beside its source. The rows' rule alone is what keeps the source's label its
    own and spawn's supersede off its dead window."""
    source = fleet_service.spawn(plain_project, "coder", worktree=False).agent
    _exits(tmux, plain_project, source)

    fork = fleet_service.hand_off(plain_project, source.label).started.agent

    assert (source.label, fork.label) == ("coder-1", "coder-2")
    assert not fork.worktree and fork.cwd == plain_project.root
    assert tmux.killed == [] and tmux.facts[source.pane_id].dead


def test_a_fork_as_the_manager_is_refused_while_a_managers_row_holds_its_one_label(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """The manager's label is fixed, so there is no next free one to walk to. A fork of an
    exited manager took its label, and spawn's supersede took its last screen. It is
    refused now, naming the row that holds the label; *Take over* is what brings that
    manager back."""
    manager = fleet_service.spawn(project, "manager").agent
    _exits(tmux, project, manager)
    before = _made(tmux, project)

    with pytest.raises(FleetError) as refused:
        fleet_service.hand_off(project, "manager")

    assert manager.id in str(refused.value)
    assert _made(tmux, project) == before and tmux.facts[manager.pane_id].dead


def test_a_spawn_with_a_start_point_never_starts_in_a_tree_that_is_already_there(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """``start_point`` is a fork's: the commit its own worktree is cut at. A tree already
    standing under the label was reused instead, whatever it held and wherever it was. The
    spawn is refused now, whoever picked the label; a plain spawn (no start point) still
    carries on in its label's tree, which is what a respawn on the same label is for."""
    first = fleet_service.spawn(project, "coder", label="coder-auth").agent
    tree = Path(first.cwd)
    on = _branch(tree)
    fleet_service.stop(project, "coder-auth", force=True)
    head = _git("rev-parse", "HEAD", cwd=project.root)
    before = _made(tmux, project)

    with pytest.raises(FleetError, match="already exists"):
        fleet_service.spawn(project, "coder", label="coder-auth", start_point=head)

    assert _made(tmux, project) == before and _branch(tree) == on
    again = fleet_service.spawn(project, "coder", label="coder-auth")
    assert again.agent.cwd == first.cwd
    assert any("reusing the existing worktree" in note for note in again.notes)


def test_a_spawn_with_a_start_point_never_checks_out_a_branch_that_is_already_there(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """The tree is gone and its branch was left, as ``reap`` leaves a merged one. A spawn
    with a start point checked that branch out as it stood, at whatever commit it was on.
    It is refused now; a plain spawn still checks the branch out and says so."""
    first = fleet_service.spawn(project, "coder", label="coder-auth").agent
    tree = Path(first.cwd)
    on = _branch(tree)
    fleet_service.stop(project, "coder-auth", force=True)
    _git("worktree", "remove", str(tree), cwd=project.root)
    head = _git("rev-parse", "HEAD", cwd=project.root)
    before = _made(tmux, project)

    with pytest.raises(FleetError, match="already exists"):
        fleet_service.spawn(project, "coder", label="coder-auth", start_point=head)

    assert _made(tmux, project) == before and not tree.exists()
    again = fleet_service.spawn(project, "coder", label="coder-auth")
    assert again.agent.cwd == first.cwd and _branch(tree) == on
    assert any("already existed — checked it out" in note for note in again.notes)
