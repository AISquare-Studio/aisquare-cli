"""A hand-off acts on the row it was given, not on whoever carries the label now.

Review of #240, "also confirmed": hand-off by label. Two rows of a project can carry one
label over time, an exited coder-1 and the coder-1 started since, and the Spawn dialog's
*Hand off from* can show the older one (both, or the one it listed before the other
started). ``hand_off`` resolved its source by label, which names the NEWEST row, so
choosing the older teammate forked, or stopped and restarted, the newer one.

The contract (the service half; the dialog passes the row it shows). With ``agent_id`` the
hand-off acts on exactly that fleet row: it must exist, be this project's and carry the
label, or the hand-off is refused, naming the mismatch, before anything is stopped or
made. A fork forks THAT row's conversation and commit, live or ended, whatever newer row
shares its label. A take-over acts on it only while it is still the row its label names;
replaced since, it is refused and neither row is touched. With ``agent_id=None`` the label
resolves as it always did.

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
from tests.test_fleet_service import FakeTmux, _coder, _command, _flag, _git, _on_disk

# The fleet suite's fixtures, bound here so pytest finds them for this module's tests.
tmux = fleet_suite.tmux
clock = fleet_suite.clock
claude_on_path = fleet_suite.claude_on_path
repo = fleet_suite.repo
project = fleet_suite.project
plain_project = fleet_suite.plain_project


def _head(tree: Path) -> str:
    return _git("rev-parse", "HEAD", cwd=tree)


def _exited(tmux: FakeTmux, project: ProjectInfo, tmp_path: Path) -> tuple[FleetAgent, Path]:
    """coder-1, which ran in the project's root and has exited: its conversation is on
    disk, and the picker lists its 💤 row."""
    agent = _coder(project)
    transcript = _on_disk(agent, tmp_path)
    tmux.die(agent.pane_id, 0)
    listed = {status.agent.id: status.state for status in fleet_service.handoff_sources(project)}
    assert agent.label == "coder-1" and listed[agent.id] == "exited"
    return agent, transcript


def _replaced(
    tmux: FakeTmux, project: ProjectInfo, tmp_path: Path
) -> tuple[FleetAgent, FleetAgent, Path]:
    """An exited coder-1 and the live coder-1 started since, in one project.

    The older one ran in the root; the newer one works in the label's worktree, a commit
    ahead and with no conversation on disk yet, so whose conversation and whose commit a
    hand-off took can be told apart."""
    old, transcript = _exited(tmux, project, tmp_path)
    new = fleet_service.spawn(project, "coder", label="coder-1", worktree=True).agent
    tree = Path(new.cwd)
    (tree / "newer.txt").write_text("committed by the newer coder-1\n", encoding="utf-8")
    _git("add", "newer.txt", cwd=tree)
    _git("commit", "-q", "-m", "work by the newer coder-1", cwd=tree)
    assert (old.label, new.label) == ("coder-1", "coder-1") and old.id != new.id
    assert _head(tree) != _head(project.root)
    return old, new, transcript


def _world(tmux: FakeTmux, *projects: ProjectInfo) -> dict[str, object]:
    """Everything a refused hand-off must leave as it found it."""
    with store_session() as store:
        rows = [
            (row.id, row.label, row.ended_at)
            for known in projects
            for row in store.fleet_agents(known.id)
        ]
    return {
        "rows": rows,
        "windows started": len(tmux.spawned),
        "windows killed": list(tmux.killed),
        "typed": list(tmux.typed),
    }


def test_a_fork_by_row_forks_the_exited_teammate_it_was_given_not_the_newer_one(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, tmp_path: Path
) -> None:
    """The pin: the owner chose the EXITED coder-1. By label that forked the live one's
    (empty) conversation at the live one's commit. By row, the fork resumes the exited
    row's transcript and is cut at the commit that row's tree is on, and the live coder-1
    is left as it was."""
    old, new, transcript = _replaced(tmux, project, tmp_path)
    killed = list(tmux.killed)

    receipt = fleet_service.hand_off(project, "coder-1", agent_id=old.id)

    fork = receipt.started.agent
    command = _command(tmux)
    assert (receipt.mode, receipt.source.id, receipt.resumed) == ("fork", old.id, True)
    assert _flag(command, "--resume") == str(transcript) and "--fork-session" in command
    assert _head(Path(fork.cwd)) == _head(project.root) != _head(Path(new.cwd))
    assert fork.label == "coder-2" and fork.id not in (old.id, new.id)
    with store_session() as store:
        live = store.get_fleet_agent(new.id)
    assert live is not None and live.ended_at is None, "the live coder-1 keeps running"
    assert [text for pane, _kind, text in tmux.typed if pane == new.pane_id] == []
    assert tmux.killed == killed


def test_a_take_over_by_row_of_a_teammate_replaced_since_is_refused_with_nothing_stopped(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, tmp_path: Path
) -> None:
    """The same choice with *Take over*. By label it sent the LIVE coder-1 ``/exit`` and
    restarted it: the wrong agent. The exited row is no longer the one its label names, so
    there is nothing a take-over of it may act on: refused, naming both rows, with neither
    touched."""
    old, new, _transcript = _replaced(tmux, project, tmp_path)
    before = _world(tmux, project)

    with pytest.raises(FleetError) as refused:
        fleet_service.hand_off(project, "coder-1", agent_id=old.id, mode="take_over")

    assert old.id in str(refused.value) and new.id in str(refused.value)
    assert _world(tmux, project) == before


@pytest.mark.parametrize("mode", ["fork", "take_over"])
@pytest.mark.parametrize("given", ["another project's row", "no row's id", "another label's row"])
def test_a_row_that_is_not_this_projects_teammate_under_that_label_is_refused_first(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    plain_project: ProjectInfo,
    given: str,
    mode: fleet_service.HandoffMode,
) -> None:
    """The row must exist, be this project's and carry the label. Each mismatch is a
    refusal that names it, before anything is stopped or made, and never a quiet fall back
    to the label's own row, which is running right there."""
    here = _coder(project)  # coder-1: what the label alone would name
    if given == "another project's row":
        elsewhere = fleet_service.spawn(plain_project, "coder", worktree=False).agent
        assert elsewhere.label == here.label
        agent_id, named = elsewhere.id, [elsewhere.id]
    elif given == "no row's id":
        agent_id = "agt_" + "0" * 26
        named = [agent_id]
    else:
        other = _coder(project, label="tester-1")
        agent_id, named = other.id, ["'tester-1'", "'coder-1'"]
    before = _world(tmux, project, plain_project)

    with pytest.raises(FleetError) as refused:
        fleet_service.hand_off(project, "coder-1", agent_id=agent_id, mode=mode)

    assert all(word in str(refused.value) for word in named), str(refused.value)
    assert _world(tmux, project, plain_project) == before


def test_without_a_row_the_label_names_its_newest_row_as_it_always_did(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, tmp_path: Path
) -> None:
    """The control: ``agent_id=None`` is today's resolution. The label names the newest row
    that carries it, here the live coder-1, whose commit the fork is cut at."""
    _old, new, _transcript = _replaced(tmux, project, tmp_path)

    receipt = fleet_service.hand_off(project, "coder-1")

    assert (receipt.source.id, receipt.resumed) == (new.id, False)
    assert _head(Path(receipt.started.agent.cwd)) == _head(Path(new.cwd))


def test_a_take_over_by_row_of_the_row_its_label_names_acts_on_that_row(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, tmp_path: Path
) -> None:
    """Given the live coder-1's own row, the take-over is that row's restart: it is the one
    stopped, and the replacement carries on in its tree."""
    _old, new, _transcript = _replaced(tmux, project, tmp_path)

    receipt = fleet_service.hand_off(project, "coder-1", agent_id=new.id, mode="take_over")

    assert receipt.stopped is not None and receipt.stopped.id == new.id
    started = receipt.started.agent
    assert (started.label, started.cwd) == ("coder-1", new.cwd)
    assert (new.pane_id, "literal", "/exit") in tmux.typed


def test_an_exited_teammate_nobody_replaced_is_still_taken_over_by_its_row(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, tmp_path: Path
) -> None:
    """A row that has ended is still the row its label names until a newer one carries the
    label: the 💤 teammate the dialog offers. Taking it over by row starts it again, as
    taking it over by label does, resuming its conversation."""
    old, transcript = _exited(tmux, project, tmp_path)

    receipt = fleet_service.hand_off(project, "coder-1", agent_id=old.id, mode="take_over")

    assert receipt.stopped is not None and receipt.stopped.id == old.id and receipt.resumed
    assert _flag(_command(tmux), "--resume") == str(transcript)
    assert receipt.started.agent.label == "coder-1" and receipt.started.agent.id != old.id
