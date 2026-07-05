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


def _hook_commands(settings_path: Path) -> list[str]:
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    hooks = settings.get("hooks", {})
    return [
        item["command"]
        for event in ("SessionStart", "UserPromptSubmit")
        for group in hooks.get(event, [])
        for item in group["hooks"]
    ]


def test_connect_installs_claude_code_hooks(runner: CliRunner, fake_home: Path) -> None:
    result = runner.invoke(app, ["agents", "connect", "claude-code"])
    assert result.exit_code == 0, result.output
    assert "hooks installed" in result.stdout
    commands = _hook_commands(fake_home / ".claude" / "settings.json")
    assert any("hook session-start" in command for command in commands)
    assert any("hook user-prompt-submit" in command for command in commands)


def test_connect_preserves_existing_hooks(runner: CliRunner, fake_home: Path) -> None:
    settings_path = fake_home / ".claude" / "settings.json"
    settings_path.write_text(
        json.dumps(
            {"hooks": {"PostToolUse": [{"hooks": [{"type": "command", "command": "mine"}]}]}}
        ),
        encoding="utf-8",
    )
    runner.invoke(app, ["agents", "connect", "claude-code"])
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings["hooks"]["PostToolUse"][0]["hooks"][0]["command"] == "mine"  # untouched


def test_disconnect_removes_hooks(runner: CliRunner, fake_home: Path) -> None:
    runner.invoke(app, ["agents", "connect", "claude-code"])
    runner.invoke(app, ["agents", "disconnect", "claude-code"])
    assert _hook_commands(fake_home / ".claude" / "settings.json") == []


def test_connect_targets_an_alternate_config_dir(runner: CliRunner, fake_home: Path) -> None:
    # Parallel Claude installs (CLAUDE_CONFIG_DIR aliases, e.g. ~/.claude4)
    # must receive the hooks in THEIR settings file, not ~/.claude's.
    alt = fake_home / ".claude4"
    alt.mkdir()
    (alt / "CLAUDE.md").write_text("# alt rules\n", encoding="utf-8")
    result = runner.invoke(
        app, ["agents", "connect", "claude-code", "--config-dir", str(alt)]
    )
    assert result.exit_code == 0, result.output
    settings = json.loads((alt / "settings.json").read_text(encoding="utf-8"))
    assert set(settings["hooks"]) == {"SessionStart", "UserPromptSubmit", "SessionEnd"}
    assert not (fake_home / ".claude" / "settings.json").exists()

    disconnect = runner.invoke(
        app, ["agents", "disconnect", "claude-code", "--config-dir", str(alt)]
    )
    assert disconnect.exit_code == 0
    settings = json.loads((alt / "settings.json").read_text(encoding="utf-8"))
    assert "hooks" not in settings


def test_claude_config_dir_env_is_honoured(
    runner: CliRunner, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alt = fake_home / ".claude-env"
    alt.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(alt))
    result = runner.invoke(app, ["agents", "connect", "claude-code"])
    assert result.exit_code == 0, result.output
    assert (alt / "settings.json").exists()


def test_hook_commands_are_never_a_bare_name(
    runner: CliRunner, fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A bare `aisquare` dies in hook shells with "/bin/sh: aisquare: not found".
    # 1) The running executable wins, even when PATH knows nothing about it.
    fake_bin = tmp_path / "somewhere" / "aisquare"
    fake_bin.parent.mkdir(parents=True)
    fake_bin.write_text("#!/bin/sh\n")
    monkeypatch.setattr("aisquare.core.agents.sys.argv", [str(fake_bin)])
    monkeypatch.setattr("aisquare.core.agents.shutil.which", lambda _: None)
    result = runner.invoke(app, ["agents", "connect", "claude-code"])
    assert result.exit_code == 0, result.output
    settings = json.loads((fake_home / ".claude" / "settings.json").read_text(encoding="utf-8"))
    command = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert command == f"{fake_bin.resolve()} hook session-start"

    # 2) With no usable argv0 and nothing on PATH: python -m aisquare, never bare.
    import sys as real_sys

    monkeypatch.setattr("aisquare.core.agents.sys.argv", ["pytest"])
    result = runner.invoke(app, ["agents", "connect", "claude-code"])
    assert result.exit_code == 0, result.output
    settings = json.loads((fake_home / ".claude" / "settings.json").read_text(encoding="utf-8"))
    command = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert command == f"{real_sys.executable} -m aisquare hook session-start"
