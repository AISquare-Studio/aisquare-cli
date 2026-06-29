"""End-to-end CLI behaviour for status and doctor."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


@pytest.fixture
def fake_agent_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "agenthome"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setattr("aisquare.core.agents._home", lambda: home)
    return home


def _json(output: str) -> Any:
    return json.loads(output)


def test_status_reports_counts(runner: CliRunner) -> None:
    runner.invoke(app, ["context", "add", "a pref", "--user"])
    runner.invoke(app, ["context", "add", "a convention", "--project"])
    result = runner.invoke(app, ["--json", "status"])
    assert result.exit_code == 0, result.output
    report = _json(result.stdout)
    assert report["initialized"] is True
    assert report["user_entries"] == 1
    assert report["project_entries"] == 1


def test_status_human(runner: CliRunner) -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "aisquare:" in result.stdout
    assert "home:" in result.stdout


def test_doctor_all_ok_after_init(runner: CliRunner, fake_agent_home: Path) -> None:
    runner.invoke(app, ["init", "--no-onboard"])
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "✓ home" in result.stdout
    assert "✓ agents" in result.stdout


def test_doctor_fresh_flags_missing_home(runner: CliRunner) -> None:
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "missing" in result.output
