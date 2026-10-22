"""With the experimental captain off, ``captain`` is the unknown role main calls it.

Review of #240, F2. ``launch._role_ok`` took ``captain`` (``UNSEATED``) whatever the
switch said, and ``fleet._role_ok`` defers to it. Off:

- ``aisquare launch captain`` in any fleet window (``AISQUARE_FLEET_AGENT`` set) started an
  agent under a role that does not exist; outside one it refused by naming the off feature
  ("the captain lives on the home board …");
- ``aisquare fleet spawn captain`` refused the same way.

main 0.8.2 refuses all three as ``unknown role 'captain'``. On, the captain is a role
exactly as before. A captain row from a time the switch was on still gets the one line
every captain refusal says when it is restarted or switched (#241, finding 13).
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
from aisquare.models import ProjectInfo
from aisquare.services import fleet as fleet_service
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


@pytest.mark.parametrize("window", ["in a fleet window", "by hand"])
def test_launch_captain_is_an_unknown_role_while_off(
    runner: CliRunner,
    work_dir: Path,
    spy: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    captain_off: None,
    window: str,
) -> None:
    if window == "in a fleet window":
        monkeypatch.setenv(orchestrator.FLEET_AGENT_ENV_VAR, "agt_some_fleet_seat")
    else:
        monkeypatch.delenv(orchestrator.FLEET_AGENT_ENV_VAR, raising=False)

    result = runner.invoke(app, ["launch", "captain"])

    assert result.exit_code == 1, result.output
    assert "unknown role 'captain'" in result.output
    assert "home board" not in result.output, "the refusal names no off feature"
    assert not spy, "nothing is exec'd"


@pytest.mark.needs_captain
def test_on_the_captain_is_a_launch_role_in_its_window_and_refused_by_hand(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: on (the suite's ``captain_on``), as it was."""
    assert experimental.captain_enabled()
    assert launch_cli._role_ok("captain")
    monkeypatch.delenv(orchestrator.FLEET_AGENT_ENV_VAR, raising=False)

    result = runner.invoke(app, ["launch", "captain"])

    assert result.exit_code == 1
    assert "`aisquare captain` starts it" in result.output


def test_the_seat_rule_and_fleet_spawn_say_unknown_role_while_off(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, captain_off: None
) -> None:
    assert fleet_service.role_ok("captain") is False
    with pytest.raises(fleet_service.FleetError) as refused:
        fleet_service.spawn(project, "captain")
    assert str(refused.value).startswith("unknown role 'captain'")
    assert tmux.spawned == []


@pytest.mark.needs_captain
def test_on_the_seat_rule_takes_the_captain(tmux: FakeTmux) -> None:
    """The control: on, ``role_ok`` is what the captain's own start relies on."""
    assert fleet_service.role_ok("captain") is True
