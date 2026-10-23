"""A restart starts an agent with no worktree at its project's root, as on main.

Review of #240, R1-F10, and of #267, item (b). ``_respawn`` came to replay the row's
recorded ``cwd`` for every role, where main started every non-worktree replacement at
``project.root``. Only the captain needs its own folder replayed (its brain, under the
home); for every other role the recorded folder is where the project's root WAS, and a
project re-rooted since restarted its agents in the old folder.

Driven as ``tests/test_fleet_service.py`` drives the fleet: the real store in an isolated
home and that suite's in-memory ``FakeTmux``.
"""

from __future__ import annotations

from pathlib import Path

from aisquare.core.store import store_session
from aisquare.models import ProjectInfo
from aisquare.services import fleet as fleet_service
from tests import test_fleet_service as fleet_suite
from tests.test_fleet_service import FakeTmux

# The fleet suite's fixtures, bound here so pytest finds them for this module's tests.
tmux = fleet_suite.tmux
clock = fleet_suite.clock
claude_on_path = fleet_suite.claude_on_path
plain_project = fleet_suite.plain_project


def test_a_restart_starts_a_plain_agent_at_its_projects_root_not_its_recorded_folder(
    tmux: FakeTmux, claude_on_path: Path, plain_project: ProjectInfo, tmp_path: Path
) -> None:
    agent = fleet_service.spawn(plain_project, "coder", worktree=False).agent
    assert Path(agent.cwd) == plain_project.root
    elsewhere = tmp_path / "where-the-root-was"
    elsewhere.mkdir()
    with store_session() as store:
        store.upsert_fleet_agent(agent.model_copy(update={"cwd": elsewhere}))

    restarted = fleet_service.restart(plain_project, agent.label).started

    assert Path(restarted.cwd) == plain_project.root
    assert Path(tmux.spawned[-1]["cwd"]) == plain_project.root
