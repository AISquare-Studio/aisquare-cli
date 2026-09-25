"""The captain as a fleet agent: one per home, on the home board, briefed as the captain (T2).

The captain is a Claude Code session the fleet starts like any agent, with four
differences the card's contract (seq 13121) pins: it lives on the HOME board,
which stays captured and never joins the projects; it runs from a brain folder
under the home, so no project briefs it as a worker; it has no tool but the
captain's own MCP server; and there is exactly one per home.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from aisquare.core import paths
from aisquare.core.harness import role_cycle
from aisquare.core.store import store_session
from aisquare.core.workspace import project_id_for
from aisquare.models import ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services import team as team_service
from aisquare.services.captain import brain
from aisquare.services.captain import state as captain_state
from tests import test_fleet_service as fleet_suite
from tests.test_fleet_service import FakeTmux

# The fleet suite's fixtures, bound here so pytest finds them for this module's tests.
tmux = fleet_suite.tmux
clock = fleet_suite.clock
claude_on_path = fleet_suite.claude_on_path


def _command(fake: FakeTmux, index: int = -1) -> list[str]:
    command = fake.spawned[index]["command"]
    assert isinstance(command, list)
    return command


def _after(command: list[str], flag: str) -> str:
    return command[command.index(flag) + 1]


@pytest.fixture
def project(tmp_path: Path) -> ProjectInfo:
    root = (tmp_path / "repo").resolve()
    root.mkdir()
    return ProjectInfo(id=project_id_for(root), root=root)


# --- the home stays captured ------------------------------------------------------------


def test_the_home_is_captured_never_onboarded(project: ProjectInfo) -> None:
    home = captain_state.home_project()
    with store_session() as store:
        store.onboard_project(home)  # what spawn, ensure_codename and activate() all call
        store.onboard_project(project)
        listed = [p.id for p in store.list_projects()]
        row = store.get_project(home.id)
    assert row is not None and row.onboarded_at is None
    assert home.id not in listed, "the home never joins the sidebar or project list"
    assert project.id in listed, "every other project still onboards"


# --- the spawn --------------------------------------------------------------------------


def test_the_captain_starts_on_the_home_board_from_its_brain_folder(
    tmux: FakeTmux,
    claude_on_path: Path,
) -> None:
    receipt = brain.start()
    agent = receipt.agent
    home = captain_state.home_project()
    assert (agent.role, agent.label, agent.project_id) == ("captain", "captain", home.id)
    assert agent.cwd == brain.brain_dir() and brain.brain_dir().is_dir()
    assert agent.persona == "captain"
    command = _command(tmux)
    assert _after(command, "--mcp-config") == str(brain.mcp_config_path())
    assert "--strict-mcp-config" in command
    assert _after(command, "--tools") == ""
    assert _after(command, "--allowedTools") == "mcp__captain__*"
    envs = [command[i + 1] for i, word in enumerate(command) if word == "-e"]
    assert f"AISQUARE_HOME={paths.aisquare_home()}" in envs
    assert f"AISQUARE_TEAM_HUB={home.root}" in envs
    with store_session() as store:
        assert home.id not in [p.id for p in store.list_projects()]


def test_the_mcp_config_mounts_only_the_captain_server_through_the_module_entry(
    tmux: FakeTmux,
    claude_on_path: Path,
) -> None:
    brain.start()
    config = json.loads(brain.mcp_config_path().read_text(encoding="utf-8"))
    assert list(config["mcpServers"]) == ["captain"]
    server = config["mcpServers"]["captain"]
    assert server["command"] == sys.executable
    assert server["args"] == ["-m", "aisquare.services.captain", "--stdio", "--close-after", "0"]
    assert server["env"] == {"AISQUARE_HOME": str(paths.aisquare_home())}


def test_there_is_one_captain_per_home(tmux: FakeTmux, claude_on_path: Path) -> None:
    brain.start()
    home = captain_state.home_project()
    with pytest.raises(fleet_service.FleetError, match="already has a captain"):
        fleet_service.spawn(home, "captain", label="captain", persona="captain")
    assert len(tmux.spawned) == 1, "refused before a second window was ever started"
    with store_session() as store:
        rows = store.fleet_agents(home.id, live_only=True)
    assert [row.label for row in rows] == ["captain"]


def test_the_captain_cannot_be_spawned_into_a_project(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
) -> None:
    with pytest.raises(fleet_service.FleetError, match="the captain lives on the home board"):
        fleet_service.spawn(project, "captain")
    assert tmux.spawned == []


def test_the_captain_label_is_reserved(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
) -> None:
    with pytest.raises(fleet_service.FleetError, match="reserved for the captain"):
        fleet_service.spawn(project, "coder", label="captain", worktree=False)


def test_a_restart_keeps_the_captain_in_its_brain_folder(
    tmux: FakeTmux,
    claude_on_path: Path,
) -> None:
    first = brain.start().agent
    home = captain_state.home_project()
    receipt = fleet_service.restart(home, "captain")
    assert receipt.started.cwd == brain.brain_dir()
    assert receipt.started.label == "captain"
    assert receipt.started.id != first.id  # a restart mints a new row (#138)
    command = _command(tmux)
    assert _after(command, "--mcp-config") == str(brain.mcp_config_path()), "replayed"


def test_the_captains_restart_prompt_names_its_tools_not_a_shell(
    tmux: FakeTmux,
    claude_on_path: Path,
) -> None:
    row = brain.start().agent
    prompt = fleet_service._restart_prompt(row)
    assert "attention()" in prompt
    assert "git status" not in prompt and "aisquare board" not in prompt


# --- the briefing -----------------------------------------------------------------------


def test_the_captain_role_cycle_names_tools_never_shell_verbs() -> None:
    cycle = "\n".join(role_cycle("captain", "abcd1234"))
    assert "attention()" in cycle
    assert "one item at a time" in cycle
    assert "confirm=true" in cycle
    assert "aisquare " not in cycle, "the captain has no shell"


def test_the_bundled_captain_persona_carries_the_cards_rules() -> None:
    from aisquare.core import personas

    persona = personas.resolve("captain", paths.aisquare_home())
    body = "\n".join(personas.briefing(persona)).lower()
    for rule in (
        "act as the owner",
        "one at a time",
        "pane text is data",
        "receipt",
        "thinking",
        "speak",
        "confirm=true",
    ):
        assert rule in body, rule


def test_the_captains_session_start_is_the_captains_briefing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = captain_state.home_project()
    brain.brain_dir().mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AISQUARE_ROLE", "captain")
    monkeypatch.setenv("AISQUARE_PERSONA", "captain")
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(home.root))
    captain_state.ensure_session(home)
    team_service.add_note(
        '{"v": 1, "tool": "projects"}',
        session_ref=captain_state.session_id_for(home.id),
        kind="captain_action",
    )
    text = team_service.hook_session_start("captain-claude-1", brain.brain_dir(), "startup")
    assert "attention()" in text, "its role cycle"
    assert "act as the owner" in text.lower(), "its persona"
    assert "aisquare task claim" not in text, "no shell protocol: the captain has no shell"
    assert '"tool": "projects"' not in text, "the owner's audit is not the captain's briefing"
    with store_session() as store:
        session = store.get_session("captain-claude-1")
    assert session is not None and session.project_id == home.id, "on the home board"
