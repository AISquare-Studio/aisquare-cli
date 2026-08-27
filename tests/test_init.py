"""End-to-end CLI behaviour for `aisquare init`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import credentials
from aisquare.core.paths import config_path, credentials_path, db_path


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    workdir = tmp_path / "project"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    return workdir


def _json(output: str) -> Any:
    return json.loads(output)


def test_init_creates_the_layout_and_registers_the_project(runner: CliRunner) -> None:
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    assert "initialized" in result.stdout
    assert config_path().is_file()
    assert db_path().is_file()
    listed = runner.invoke(app, ["--json", "project", "list"])
    assert len(_json(listed.stdout)) == 1


def test_init_json_report(runner: CliRunner, work_dir: Path) -> None:
    result = runner.invoke(app, ["--json", "init"])
    assert result.exit_code == 0, result.output
    report = _json(result.stdout)
    assert report["already_initialized"] is False
    assert report["project"]["root"] == str(work_dir.resolve())
    assert report["onboarded"] == 0


def test_init_is_idempotent(runner: CliRunner) -> None:
    runner.invoke(app, ["init"])
    second = runner.invoke(app, ["--json", "init"])
    assert _json(second.stdout)["already_initialized"] is True


def test_init_onboards_by_default(runner: CliRunner, work_dir: Path) -> None:
    (work_dir / "pyproject.toml").touch()
    result = runner.invoke(app, ["--json", "init"])
    assert _json(result.stdout)["onboarded"] >= 1


def test_init_no_onboard(runner: CliRunner, work_dir: Path) -> None:
    (work_dir / "pyproject.toml").touch()
    result = runner.invoke(app, ["--json", "init", "--no-onboard"])
    assert _json(result.stdout)["onboarded"] == 0


def test_init_stores_api_key_and_notes_it(runner: CliRunner) -> None:
    """Asserts the key is RETRIEVABLE, not the bytes it is stored in.

    This asserted the file equalled the bare key, which pinned the format that
    was the defect: `serve` keeps its bearer token in the same file, so a
    whole-file bare write erased it and a JSON write erased this key. The
    behaviour that matters to a caller is that the key comes back.
    """
    result = runner.invoke(app, ["init", "--api-key", "sk-test-123"])
    assert result.exit_code == 0, result.output
    assert credentials.load_all()[credentials.API_KEY] == "sk-test-123"
    assert credentials_path().exists()
    assert "Stored API key" in result.stdout


def test_init_unknown_agent_is_noted(runner: CliRunner) -> None:
    result = runner.invoke(app, ["init", "--agent", "bogus", "--local"])
    assert result.exit_code == 0, result.output
    assert "Could not connect bogus" in result.stdout


def test_init_connects_a_detected_agent(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "agenthome"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "CLAUDE.md").write_text("# Prefs\nuse tabs\n", encoding="utf-8")
    monkeypatch.setattr("aisquare.core.agents._home", lambda: home)
    result = runner.invoke(app, ["init", "--agent", "claude-code", "--local"])
    assert result.exit_code == 0, result.output
    assert "Connected claude-code" in result.stdout
    # The Claude Code hooks were installed into its settings.
    settings = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert "SessionStart" in settings["hooks"]
    assert "UserPromptSubmit" in settings["hooks"]
