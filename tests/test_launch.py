"""``aisquare launch <role>`` — the ergonomic replacement for ``AISQUARE_ROLE=… claude``."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli import launch as launch_cli
from aisquare.cli.app import app
from aisquare.core.orchestrator import team_project
from aisquare.core.store import store_session


@pytest.fixture
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Intercept the exec so the agent is never really launched."""
    captured: dict[str, Any] = {}

    def fake_exec(binary: str, argv: list[str], env: dict[str, str]) -> None:
        captured.update(binary=binary, argv=argv, env=env)

    monkeypatch.setattr(launch_cli, "_exec", fake_exec)
    monkeypatch.setattr(shutil, "which", lambda cmd: f"/usr/local/bin/{cmd}")
    return captured


def test_launch_execs_the_agent_with_the_role_in_env(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any]
) -> None:
    result = runner.invoke(app, ["launch", "coder"])

    assert result.exit_code == 0, result.output
    assert spy["binary"] == "/usr/local/bin/claude"
    assert spy["argv"] == ["claude"]
    assert spy["env"]["AISQUARE_ROLE"] == "coder"


def test_launch_forwards_extra_arguments_to_the_agent(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any]
) -> None:
    result = runner.invoke(app, ["launch", "runner", "--model", "opus", "-p", "go"])

    assert result.exit_code == 0, result.output
    # The role is consumed; everything after it reaches the agent untouched.
    assert spy["argv"] == ["claude", "--model", "opus", "-p", "go"]
    assert spy["env"]["AISQUARE_ROLE"] == "runner"


def test_launch_activates_the_project(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any]
) -> None:
    # A role launch is the opt-in for the repo — the board must exist afterwards.
    with store_session() as store:
        assert not store.team_active(team_project(work_dir).id)

    assert runner.invoke(app, ["launch", "planner"]).exit_code == 0

    with store_session() as store:
        assert store.team_active(team_project(work_dir).id)


def test_launch_rejects_an_unknown_role(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any]
) -> None:
    result = runner.invoke(app, ["launch", "reviewer"])

    assert result.exit_code == 1
    assert "unknown role" in result.output
    assert not spy, "nothing should be exec'd for an invalid role"


def test_launch_reports_a_missing_agent_binary(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda cmd: None)

    result = runner.invoke(app, ["launch", "coder"])

    assert result.exit_code == 1
    assert "not on your PATH" in result.output


def test_launch_honours_a_custom_agent_command(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any]
) -> None:
    result = runner.invoke(app, ["launch", "coder", "--command", "claude-next"])

    assert result.exit_code == 0, result.output
    assert spy["argv"] == ["claude-next"]


def test_launch_account_sets_the_config_dir(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any], tmp_path: Path
) -> None:
    account = tmp_path / ".claude-account1"
    account.mkdir()

    result = runner.invoke(app, ["launch", "coder", "--account", str(account)])

    assert result.exit_code == 0, result.output
    assert spy["env"]["CLAUDE_CONFIG_DIR"] == str(account)
    assert spy["env"]["AISQUARE_ROLE"] == "coder"


def test_launch_rejects_a_missing_account_dir(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any], tmp_path: Path
) -> None:
    # A typo must not silently start a fresh, unauthenticated profile.
    result = runner.invoke(app, ["launch", "coder", "--account", str(tmp_path / "typo")])

    assert result.exit_code == 1
    assert "no such config directory" in result.output
    assert not spy


def test_launch_without_account_leaves_the_ambient_config_dir_alone(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/from/the/shell")

    assert runner.invoke(app, ["launch", "coder"]).exit_code == 0
    assert spy["env"]["CLAUDE_CONFIG_DIR"] == "/from/the/shell"


def test_launch_respects_the_master_off_switch(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_TEAM", "0")

    result = runner.invoke(app, ["launch", "coder"])

    assert result.exit_code == 1
    assert not spy, "the orchestrator is off — nothing should be launched"
