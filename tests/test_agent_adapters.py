"""Coding-agent adapters (roadmap 9.1, Doctor half): one "connected?" check, no empty connections.

``services.agents.claude_code_connected`` is the single answer the doctor's Connect
button, the Welcome view and the Claude Code plugin route all ask, so the hooks are
never offered, or installed, twice. ``agents connect`` refuses an agent aisquare has
no hooks for (Codex, Cursor) instead of recording a connection that installs
nothing. Each claim has its negative control in the same test.
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
from tests.test_no_traceback_on_a_damaged_store import damaged_store  # noqa: F401


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


# --------------------------------------------------------------------------- the refusal


@pytest.mark.parametrize(("name", "label"), [("codex", "Codex"), ("cursor", "Cursor")])
def test_connect_refuses_an_agent_it_has_no_hooks_for(
    runner: CliRunner, isolated_agent_home: Path, name: str, label: str
) -> None:
    """It exited 0 and wrote the agent into agents.json as connected, installing nothing."""
    agent_dir = isolated_agent_home / f".{name}"
    agent_dir.mkdir(parents=True)

    human = runner.invoke(app, ["agents", "connect", name])
    machine = runner.invoke(app, ["--json", "agents", "connect", name])

    assert (human.exit_code, machine.exit_code) == (1, 1)
    assert f"✗ aisquare can't connect {label} yet; support is planned for 0.10" in human.output
    assert json.loads(machine.stdout) == {"error": "unsupported_agent", "ref": name}
    assert not paths.aisquare_home().exists(), "a refusal must not build the aisquare home"
    assert list(agent_dir.iterdir()) == [], f"nothing may be written under ~/.{name}"

    (isolated_agent_home / ".claude").mkdir()
    _connect(runner)  # control: the same harness records a connection that is real
    registry = json.loads(paths.agents_registry_path().read_text(encoding="utf-8"))
    assert registry["connected"] == ["claude-code"], registry


def test_an_absent_unsupported_agent_gets_the_same_answer(runner: CliRunner) -> None:
    """ "Not installed" would send someone to install Codex, and it still could not connect."""
    refused = runner.invoke(app, ["agents", "connect", "codex"])
    absent = runner.invoke(app, ["agents", "connect", "claude-code"])

    assert refused.exit_code == 1
    assert "can't connect Codex yet" in refused.output
    assert "not installed" not in refused.output
    assert "not installed" in absent.output, "control: an absent connectable agent says so"


def test_init_reports_the_refusal_as_a_note_and_records_nothing(
    runner: CliRunner, isolated_agent_home: Path
) -> None:
    (isolated_agent_home / ".codex").mkdir(parents=True)

    result = runner.invoke(app, ["--json", "init", "--yes", "--no-onboard", "--agent", "codex"])

    assert result.exit_code == 0, result.output
    notes = json.loads(result.stdout)["notes"]
    assert "Could not connect codex: aisquare can't connect Codex yet" in " ".join(notes), notes
    listed = runner.invoke(app, ["--json", "agents", "list"])
    connected = {agent["name"]: agent["connected"] for agent in json.loads(listed.stdout)}
    assert connected == {"claude-code": False, "cursor": False, "codex": False}, connected


def test_the_refusal_never_reaches_a_damaged_store(
    runner: CliRunner,
    isolated_agent_home: Path,
    damaged_store: str,  # noqa: F811 — pytest resolves fixtures by NAME, so the import must keep it
) -> None:
    """Refused before the store is opened: one JSON object, no traceback, the file untouched."""
    (isolated_agent_home / ".codex").mkdir(parents=True)
    (isolated_agent_home / ".claude").mkdir(parents=True)
    before = paths.db_path().read_bytes()

    refused = runner.invoke(app, ["--json", "agents", "connect", "codex"])

    assert isinstance(refused.exception, SystemExit), repr(refused.exception)
    assert refused.exit_code == 1
    assert json.loads(refused.stdout) == {"error": "unsupported_agent", "ref": "codex"}
    assert paths.db_path().read_bytes() == before

    # Control: a real connect does reach the store. A file that is no database stops
    # it there; a zeroed page its queries never read does not (the shapes differ).
    reached = json.loads(runner.invoke(app, ["--json", "agents", "connect", "claude-code"]).stdout)
    if damaged_store == "at-open":
        assert reached.get("error") == "store_unopenable", reached
    else:
        assert reached.get("name") == "claude-code" and "error" not in reached, reached
