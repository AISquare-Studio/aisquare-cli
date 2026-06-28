"""Agent detection and connect (ingesting an agent's existing context)."""

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
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake home with Claude Code installed, so detection is deterministic."""
    home = tmp_path / "home"
    claude = home / ".claude"
    claude.mkdir(parents=True)
    (claude / "CLAUDE.md").write_text("# Prefs\nuse tabs\n# Tools\nuse ruff\n", encoding="utf-8")
    monkeypatch.setattr("aisquare.core.agents._home", lambda: home)
    return home


def _json(output: str) -> Any:
    return json.loads(output)


def test_scan_detects_claude_code(runner: CliRunner, fake_home: Path) -> None:
    result = runner.invoke(app, ["--json", "agents", "scan"])
    assert result.exit_code == 0, result.output
    agents = {agent["name"]: agent for agent in _json(result.stdout)}
    assert agents["claude-code"]["detected"] is True
    assert agents["cursor"]["detected"] is False


def test_connect_ingests_claude_context(runner: CliRunner, fake_home: Path) -> None:
    result = runner.invoke(app, ["agents", "connect", "claude-code"])
    assert result.exit_code == 0, result.output
    assert "connected claude-code" in result.stdout
    listed = runner.invoke(app, ["--json", "context", "list"])
    texts = " ".join(entry["text"] for entry in _json(listed.stdout))
    assert "use tabs" in texts
    assert "use ruff" in texts


def test_connect_is_idempotent(runner: CliRunner, fake_home: Path) -> None:
    runner.invoke(app, ["agents", "connect", "claude-code"])
    second = runner.invoke(app, ["--json", "agents", "connect", "claude-code"])
    assert _json(second.stdout)["imported"] == 0


def test_connect_marks_connected(runner: CliRunner, fake_home: Path) -> None:
    runner.invoke(app, ["agents", "connect", "claude-code"])
    listed = runner.invoke(app, ["--json", "agents", "list"])
    agents = {agent["name"]: agent for agent in _json(listed.stdout)}
    assert agents["claude-code"]["connected"] is True


def test_connect_unknown_agent_fails(runner: CliRunner, fake_home: Path) -> None:
    result = runner.invoke(app, ["agents", "connect", "ghost"])
    assert result.exit_code == 1
    assert "unknown agent" in result.output


def test_connect_not_installed_fails(runner: CliRunner, fake_home: Path) -> None:
    result = runner.invoke(app, ["agents", "connect", "cursor"])
    assert result.exit_code == 1
    assert "not installed" in result.output


def test_disconnect_keeps_context(runner: CliRunner, fake_home: Path) -> None:
    runner.invoke(app, ["agents", "connect", "claude-code"])
    result = runner.invoke(app, ["agents", "disconnect", "claude-code"])
    assert result.exit_code == 0, result.output
    assert "disconnected claude-code" in result.stdout
    listed = runner.invoke(app, ["--json", "agents", "list"])
    agents = {agent["name"]: agent for agent in _json(listed.stdout)}
    assert agents["claude-code"]["connected"] is False
