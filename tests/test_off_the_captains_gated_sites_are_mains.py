"""With the captain off, every site its switch gates behaves as on main.

Review of #240, R2-m3: nine gated sites are reachable with the captain off — a role the
owner declared as ``captain`` reaches them — and none had an off pin, so each could have
been reverted to ``role == "captain"`` alone with the off suite still green. Each test
here drives one or more of them with the captain off and a declared ``captain`` role, and
asks for what main gives any other role:

- spawn's one-per-home rule, the captain's window environment and its board and hub
  (``fleet.spawn``, ``fleet._spawn_board_and_hub``);
- the label race's refusal (``fleet._relabel``);
- the restart and hand-off prompts (``fleet._restart_prompt``, ``fleet._handoff_prompt``);
- the Spawn dialog's fixed label and its captain start (``ui/spawn``; its pin is in
  ``tests/test_ui_spawn.py``, whose fixtures stand in for the fleet);
- the agent view a row opens (``ui/app._agent_view``).

Driven as ``tests/test_fleet_service.py`` drives the fleet (the real store in an isolated
home, real git, its in-memory ``FakeTmux``).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from aisquare.cli.ui.app import FleetApp
from aisquare.cli.ui.views.agent import AgentView
from aisquare.cli.ui.views.captain import CaptainView
from aisquare.core.config import AppConfig, RoleLaunchProfile, TeamSettings, save_config
from aisquare.core.store import store_session
from aisquare.models import FleetAgent, FleetAgentStatus, ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services.captain import state as captain_state
from tests import test_fleet_service as fleet_suite
from tests.test_fleet_service import FakeTmux
from tests.test_ui_shell import refreshed

# The fleet suite's fixtures, bound here so pytest finds them for this module's tests.
tmux = fleet_suite.tmux
clock = fleet_suite.clock
claude_on_path = fleet_suite.claude_on_path
repo = fleet_suite.repo
project = fleet_suite.project


@pytest.fixture
def declared_captain(captain_off: None) -> None:
    """Off, and ``[team.profiles.captain]`` declared: the reviewer's reproduction."""
    save_config(AppConfig(team=TeamSettings(profiles={"captain": RoleLaunchProfile(bin="echo")})))


def _window(spawned: dict[str, Any]) -> dict[str, Any]:
    """What a spawned window was given, less what is that agent's own."""
    env = {k: v for k, v in spawned["env"].items() if k != "AISQUARE_FLEET_AGENT"}
    return {"cwd": spawned["cwd"], "env": env}


def test_off_a_declared_captain_spawns_as_a_coder_does_and_more_than_once(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, declared_captain: None
) -> None:
    """One per home, the home's ``AISQUARE_HOME`` and survey switch in the window, and the
    project's root as its hub: all the captain's, none a declared role's."""
    fleet_service.spawn(project, "coder", worktree=False)
    first = fleet_service.spawn(project, "captain", worktree=False).agent
    second = fleet_service.spawn(project, "captain", worktree=False).agent

    assert first.label != second.label, "no one-per-home rule"
    coder_window, captain_window = _window(tmux.spawned[0]), _window(tmux.spawned[1])
    assert captain_window == coder_window, "the same board, hub and environment as a coder"
    assert "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY" not in captain_window["env"]


def test_off_a_declared_captain_that_loses_its_label_is_relabelled_as_any_role(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, declared_captain: None
) -> None:
    agent = fleet_service.spawn(project, "captain", worktree=False).agent
    notes: list[str] = []
    with store_session() as store:
        moved = fleet_service._relabel(agent, project, store, wanted=None, notes=notes)

    assert moved.label != agent.label and moved.role == "captain"


def test_off_a_declared_captains_restart_and_hand_off_prompts_are_any_roles(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, declared_captain: None
) -> None:
    """On the fleet suite's ``FakeTmux`` and stand-in ``claude``, as the tests above: without
    them its spawn asked the machine's own tmux, which Windows CI has none of (#51)."""
    agent = fleet_suite._coder(project).model_copy(update={"role": "captain"})

    resumed = fleet_service._restart_prompt(agent)
    taken_over = fleet_service._handoff_prompt(agent, None, [], "it stopped")

    for prompt in (resumed, taken_over):
        assert "attention()" not in prompt and "You are the captain" not in prompt
        assert agent.label in prompt


def test_off_a_home_row_whose_role_is_captain_opens_an_ordinary_agent_view(
    declared_captain: None,
) -> None:
    home = captain_state.home_project()
    row = FleetAgent(
        id="agt_declared_captain",
        project_id=home.id,
        label="captain",
        role="captain",
        pane_id="%1",
        cwd=home.root,
        created_at=datetime.now(tz=UTC),
    )

    async def run() -> tuple[object, AgentView]:
        app = FleetApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await refreshed(app)
            assert app.snapshot is not None, "a frame was read"
            view = app._agent_view(FleetAgentStatus(agent=row, state="waiting"), "view-x")
            return app.snapshot.home, view

    home_read, view = asyncio.run(run())
    # Off, the frame reads no home (``_read_frame``), so no row is the home's: the one gate
    # ``_agent_view`` needs (review of #240, R2-n2: its own captain_on test was redundant).
    assert home_read is None
    assert type(view) is AgentView and not isinstance(view, CaptainView)
