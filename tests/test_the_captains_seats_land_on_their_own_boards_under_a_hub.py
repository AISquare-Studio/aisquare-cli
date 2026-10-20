"""Under an exported hub, the captain's own row is the home board's and its seats their own.

The 0.8.x merge into rc/captain-v1 kept main 0.8.1's hub rule whole (docs/fleet.md, "Under a
hub: one board, many fleet projects"): a seat is recorded on the board the hub its spawner
honours gives, and its window is given that hub. The RC's #230 rule, every window on its own
fleet's root, is retired. Two things the captain needs stay true under main's rule:

- **The captain's own row is the home board's.** The home is no checkout, and ``.aisquare``
  is itself a project-root marker, so from the home's root a window with no hub reads
  ``$HOME``'s board, and under a shell's hub the hub's. Its window is given the home's root,
  whatever hub its starter honours (``fleet._spawn_board_and_hub``).
- **A seat the captain spawns is its own project's.** The captain's processes carry the
  home's root as their hub, which is how the captain lands on the home board. Honoured as a
  hub for its seats, every seat it spawned read the home board, where neither the seat's
  manager nor its teammates read. The captain's ``spawn`` tool names no hub
  (``actions.spawn``, ``fleet.spawn(hub="")``).

A shell's own hub still holds for everyone else: the control below.

The fleet is the real one, over the fleet suite's fake tmux, in a real git checkout.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="the [serve] extra is not installed")

from aisquare.core import orchestrator
from aisquare.core.store import store_session
from aisquare.models import FleetAgent, ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services.captain import actions, brain
from aisquare.services.captain import state as captain_state
from tests import test_fleet_service as fleet_suite
from tests.test_fleet_service import FakeTmux

# The fleet suite's fixtures, bound here so pytest finds them for this module's tests.
tmux = fleet_suite.tmux
clock = fleet_suite.clock
claude_on_path = fleet_suite.claude_on_path
repo = fleet_suite.repo
project = fleet_suite.project


@pytest.fixture
def hub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProjectInfo:
    """A hub this process honours, as the captain's own processes carry the home's root."""
    root = (tmp_path / "hub").resolve()
    root.mkdir()
    monkeypatch.setenv(orchestrator.TEAM_HUB_ENV_VAR, str(root))
    return orchestrator.team_project(None)


def _row(project: ProjectInfo, label: str) -> FleetAgent:
    with store_session() as store:
        row = store.fleet_agent_by_label(project.id, label, live_only=True)
    assert row is not None
    return row


def _window_hub(fake: FakeTmux, index: int = -1) -> object:
    env = fake.spawned[index]["env"]
    assert isinstance(env, dict)
    return env.get(orchestrator.TEAM_HUB_ENV_VAR)


def test_under_an_exported_hub_a_seat_the_captain_spawns_lands_on_its_own_projects_board(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, hub: ProjectInfo
) -> None:
    name = project.root.name
    said = json.loads(
        actions.spawn(
            name, "coder", confirm=True, utterance=f"spawn a coder in {name}, no worktree"
        )
    )

    seat = _row(project, said["label"])
    own = orchestrator.directory_project(project.root).id
    assert seat.board == own != hub.id, "the seat reads its own project's board"
    assert _window_hub(tmux) == "", "its window is given no hub"
    assert seat.launch_spec is not None and seat.launch_spec.hub == "", "nor does its restart"


def test_a_shells_own_hub_still_holds_for_a_spawn_that_is_not_the_captains(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, hub: ProjectInfo
) -> None:
    """The control: main's rule, untouched. A spawn from a shell that honours a hub reads
    the hub's board, and its window is given the hub."""
    seat = fleet_service.spawn(project, "coder", worktree=False).agent

    assert seat.board == hub.id
    assert _window_hub(tmux) == str(hub.root)


def test_under_an_exported_hub_the_captains_own_row_stays_on_the_home_board(
    tmux: FakeTmux, claude_on_path: Path, hub: ProjectInfo
) -> None:
    captain = brain.start().agent
    home = captain_state.home_project()

    assert captain.project_id == home.id
    assert captain.board == home.id != hub.id, "the captain reads the home board"
    assert _window_hub(tmux) == str(home.root), "its window is given the home's root"
    assert captain.launch_spec is not None and captain.launch_spec.hub == str(home.root)


def test_a_restart_keeps_the_captain_on_the_home_board_under_any_hub(
    tmux: FakeTmux, claude_on_path: Path, hub: ProjectInfo
) -> None:
    """A restart replays the board and the hub the row recorded, whoever restarts it and
    under whatever hub (main's ``_board_and_hub``)."""
    home = captain_state.home_project()
    brain.start()

    again = fleet_service.restart(home, fleet_service.CAPTAIN_LABEL).started

    assert again.board == home.id
    assert _window_hub(tmux) == str(home.root)
