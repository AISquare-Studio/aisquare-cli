"""Coding-agent adapters: the one "is Claude Code connected?" check (roadmap 9.1, Doctor half).

``services.agents.claude_code_connected`` is the single answer the doctor's Connect
button, the Welcome view and the Claude Code plugin route all ask, so the hooks are
never offered, or installed, twice. Each claim below has its negative control in
the same test: connected and not, this directory and another, readable and not.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import paths
from aisquare.services import agents as agents_service


@pytest.fixture
def claude_home(isolated_agent_home: Path) -> Path:
    """Claude Code installed (its config directory exists) and never connected."""
    claude = isolated_agent_home / ".claude"
    claude.mkdir(parents=True)
    return claude


def _connect(runner: CliRunner, config_dir: Path | None = None) -> None:
    argv = ["agents", "connect", "claude-code"]
    if config_dir is not None:
        argv += ["--config-dir", str(config_dir)]
    result = runner.invoke(app, argv)
    assert result.exit_code == 0, result.output


# --------------------------------------------------------------------------- the check


def test_connected_follows_the_hooks_connect_installs(runner: CliRunner, claude_home: Path) -> None:
    assert agents_service.claude_code_connected() is False, "installed is not connected"

    _connect(runner)
    assert agents_service.claude_code_connected() is True

    assert runner.invoke(app, ["agents", "disconnect", "claude-code"]).exit_code == 0
    assert agents_service.claude_code_connected() is False, "disconnect takes it back"


def test_connected_is_answered_per_config_dir(
    runner: CliRunner, claude_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parallel installs keep their own settings.json, so each directory has its own answer."""
    other = claude_home.parent / ".claude-account1"
    other.mkdir()
    _connect(runner, other)

    assert agents_service.claude_code_connected(other) is True
    assert agents_service.claude_code_connected(claude_home) is False
    assert agents_service.claude_code_connected() is False, "~/.claude was never connected"

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(other))
    assert agents_service.claude_code_connected() is True, (
        "no argument means the directory a claude started from this shell reads"
    )


def test_a_partial_or_foreign_install_is_not_connected(
    runner: CliRunner, claude_home: Path
) -> None:
    """Connect is what completes an older install, so a missing event answers False."""
    _connect(runner)
    settings_path = claude_home / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings["hooks"].pop("StopFailure")
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    assert agents_service.claude_code_connected() is False, "one event short is not connected"

    foreign = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "webhook stop"}]}]}}
    settings_path.write_text(json.dumps(foreign), encoding="utf-8")
    assert agents_service.claude_code_connected() is False, "another tool's hook is not ours"

    _connect(runner)
    assert agents_service.claude_code_connected() is True, "control: reconnecting completes it"


def test_the_hooks_on_disk_answer_not_the_registry(runner: CliRunner, claude_home: Path) -> None:
    """agents.json records what this home connected; the hooks are what actually runs (#84)."""
    _connect(runner)
    settings_path = claude_home / "settings.json"
    hooked = settings_path.read_text(encoding="utf-8")
    settings_path.write_text("{}", encoding="utf-8")
    assert paths.agents_registry_path().exists(), "the registry still records the connection"
    assert agents_service.claude_code_connected() is False, (
        "a record with no hooks connects nothing"
    )

    settings_path.write_text(hooked, encoding="utf-8")
    shutil.rmtree(paths.aisquare_home())
    assert agents_service.claude_code_connected() is True, (
        "hooks run without any home recording them"
    )


def test_asking_reads_only_and_never_raises(claude_home: Path) -> None:
    """The Welcome view and the doctor ask this on every refresh: no state, no traceback."""
    home = paths.aisquare_home()
    assert not home.exists(), "the fixture starts from a home that does not exist"
    settings_path = claude_home / "settings.json"

    assert agents_service.claude_code_connected(claude_home.parent / ".claude-missing") is False
    assert agents_service.claude_code_connected() is False, "no settings.json yet"
    assert not settings_path.exists(), "asking must not create the file it reads"

    settings_path.write_text("{not json", encoding="utf-8")
    assert agents_service.claude_code_connected() is False
    settings_path.write_bytes(b"\xff\xfe not utf-8")
    assert agents_service.claude_code_connected() is False, "undecodable bytes answer False"
    settings_path.unlink()
    settings_path.mkdir()
    assert agents_service.claude_code_connected() is False, "an unreadable path answers False"

    assert not home.exists(), "asking created the aisquare home"


def test_a_non_list_event_in_settings_json_is_no_hooks_not_a_traceback(
    runner: CliRunner, claude_home: Path
) -> None:
    """``{"hooks": {"Stop": 5}}`` made ``doctor`` print a traceback, and the check raise."""
    settings_path = claude_home / "settings.json"
    settings_path.write_text(json.dumps({"hooks": {"Stop": 5, "SessionEnd": True}}), "utf-8")

    assert agents_service.claude_code_connected() is False
    result = runner.invoke(app, ["--json", "doctor"])
    assert result.exception is None or isinstance(result.exception, SystemExit), repr(
        result.exception
    )
    row = next(row for row in json.loads(result.stdout) if row["name"] == "claude-code")
    assert row["status"] == "warn" and "agents connect claude-code" in row["fix"], row

    _connect(runner)  # control: connect writes each event back as a list of our group
    assert agents_service.claude_code_connected() is True
