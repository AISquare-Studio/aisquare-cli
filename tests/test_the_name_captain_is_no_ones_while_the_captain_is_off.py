"""With the experimental captain off, the NAME ``captain`` is special nowhere.

Review of #240, R1-F1 and R1-F2. Off, everything else is main 0.8.2 with #201's personas,
and on main ``captain`` is a label like any other and, once the owner declares
``[team.profiles.captain]``, a role like any other. At 06b8d175 it was neither:

- the label was reserved for the captain (``fleet._fixed_label``), so ``fleet spawn coder
  --label captain`` was refused, and a restart or switch of a coder already labelled
  ``captain`` stopped it and then could not start it again (R1-F1);
- a declared ``captain`` role got the captain's refusals and briefing: ``launch captain``
  by hand exited 1 ("the captain lives on the home board"), ``fleet spawn captain`` was
  refused for the home and for its MCP flags, and a session started as it was briefed as
  the captain, with no task protocol and its state forced to ``waiting`` (R1-F2).

On, every one of those refusals stands (``experimental.captain_role``).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli import launch as launch_cli
from aisquare.cli.app import app
from aisquare.core import experimental, orchestrator
from aisquare.core.config import AppConfig, RoleLaunchProfile, TeamSettings, save_config
from aisquare.core.store import store_session
from aisquare.models import ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services import team as team_service
from tests import test_fleet_service as fleet_suite
from tests.test_fleet_service import FakeTmux

# The fleet suite's fixtures, bound here so pytest finds them for this module's tests.
tmux = fleet_suite.tmux
clock = fleet_suite.clock
claude_on_path = fleet_suite.claude_on_path
repo = fleet_suite.repo
project = fleet_suite.project


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Intercept the exec so the agent is never really launched (``tests/test_launch.py``)."""
    captured: dict[str, Any] = {}

    def fake_exec(binary: str, argv: list[str], env: dict[str, str]) -> None:
        captured.update(binary=binary, argv=argv, env=env)

    monkeypatch.setattr(launch_cli, "_exec", fake_exec)
    monkeypatch.setattr(shutil, "which", lambda cmd: f"/usr/local/bin/{cmd}")
    return captured


@pytest.fixture
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


@pytest.fixture
def declared_captain() -> None:
    """``[team.profiles.captain] bin = "echo"``: the reviewer's reproduction."""
    save_config(AppConfig(team=TeamSettings(profiles={"captain": RoleLaunchProfile(bin="echo")})))


# --- R1-F1: the label -----------------------------------------------------------------


def test_off_a_coder_may_be_labelled_captain_and_restarted_and_switched_under_it(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    captain_off: None,
) -> None:
    fleet_suite._two_slots_with_usage(monkeypatch, work=95, personal=10)
    agent = fleet_service.spawn(
        project, "coder", label="captain", worktree=False, account="2"
    ).agent
    assert agent.label == "captain" and agent.role == "coder"

    restarted = fleet_service.restart(project, "captain")

    assert restarted.was_running is True, "it was running: stopped, then started again"
    assert restarted.started.label == "captain" and restarted.started.role == "coder"

    switched = fleet_service.switch(project, "captain", to="3")

    assert switched.started.label == "captain" and switched.started.account_slot == 3
    [live] = fleet_service.list_agents(project)
    assert live.agent.id == switched.started.id, "never stopped and left down"


@pytest.mark.needs_captain
def test_on_the_label_captain_is_the_captains(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """The control: on, the label stays reserved, as it was."""
    with pytest.raises(fleet_service.FleetError, match="reserved for the captain"):
        fleet_service.spawn(project, "coder", label="captain", worktree=False)
    assert tmux.spawned == []


# --- R1-F2: a declared role named captain ---------------------------------------------


@pytest.mark.parametrize("window", ["in a fleet window", "by hand"])
def test_off_launch_of_a_declared_captain_execs_its_bin(
    runner: CliRunner,
    work_dir: Path,
    spy: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    captain_off: None,
    declared_captain: None,
    window: str,
) -> None:
    if window == "in a fleet window":
        monkeypatch.setenv(orchestrator.FLEET_AGENT_ENV_VAR, "agt_some_fleet_seat")
    else:
        monkeypatch.delenv(orchestrator.FLEET_AGENT_ENV_VAR, raising=False)

    result = runner.invoke(app, ["launch", "captain"])

    assert result.exit_code == 0, result.output
    assert "home board" not in result.output
    assert spy["argv"][0] == "echo", "its declared bin, as at 04064cba"


def test_off_fleet_spawn_of_a_declared_captain_is_an_ordinary_spawn(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    captain_off: None,
    declared_captain: None,
) -> None:
    # Not the home, and no `--strict-mcp-config --tools ''`: both were the captain's.
    agent = fleet_service.spawn(project, "captain", worktree=False).agent

    assert agent.role == "captain" and agent.project_id == project.id
    assert len(tmux.spawned) == 1

    restarted = fleet_service.restart(project, agent.label)

    assert restarted.started.role == "captain" and restarted.started.label == agent.label


def test_off_a_session_started_as_a_declared_captain_gets_the_ordinary_briefing(
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
    captain_off: None,
    declared_captain: None,
) -> None:
    monkeypatch.setenv("AISQUARE_ROLE", "captain")

    text = team_service.hook_session_start("declared-captain-1", project.root, "startup")

    assert "aisquare task claim" in text, "the board's task protocol, as for any role"
    assert "attention()" not in text, "not the captain's cycle"
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    team_service.hook_session_start("plain-coder-1", project.root, "startup")
    with store_session() as store:
        session = store.get_session("declared-captain-1")
        plain = store.get_session("plain-coder-1")
    # Not forced to anything: the state any role's start gives (#113: a Claude Code start
    # sits at its prompt, `waiting`, for every role alike).
    assert session is not None and plain is not None
    assert session.state == plain.state, "not the captain's own state"


@pytest.mark.needs_captain
def test_on_the_captains_refusals_stand(
    runner: CliRunner,
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
    spy: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    declared_captain: None,
) -> None:
    """The control: on, a declared ``captain`` changes nothing — it is the captain."""
    assert experimental.captain_enabled()
    monkeypatch.delenv(orchestrator.FLEET_AGENT_ENV_VAR, raising=False)
    monkeypatch.chdir(project.root)

    result = runner.invoke(app, ["launch", "captain"])

    assert result.exit_code == 1 and "`aisquare captain` starts it" in result.output
    assert not spy
    with pytest.raises(fleet_service.FleetError, match="lives on the home board"):
        fleet_service.spawn(project, "captain", worktree=False)
    assert tmux.spawned == []

    monkeypatch.setenv("AISQUARE_ROLE", "captain")
    text = team_service.hook_session_start("captain-on-1", project.root, "startup")
    assert "attention()" in text and "aisquare task claim" not in text
    with store_session() as store:
        session = store.get_session("captain-on-1")
    assert session is not None and session.state == "waiting"
