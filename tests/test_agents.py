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
    result = runner.invoke(app, ["agents", "connect", "claude-code", "--config-dir", str(alt)])
    assert result.exit_code == 0, result.output
    settings = json.loads((alt / "settings.json").read_text(encoding="utf-8"))
    assert set(settings["hooks"]) == {
        "SessionStart",
        "UserPromptSubmit",
        "SessionEnd",
        "Stop",
        "Notification",
    }
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


def test_third_party_hooks_containing_our_words_survive_connect(
    runner: CliRunner, fake_home: Path
) -> None:
    # "webhook stop" and "my-hook stop" are NOT ours — connect/disconnect
    # must never rewrite them away (substring matching once did).
    settings_path = fake_home / ".claude" / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [{"hooks": [{"type": "command", "command": "webhook stop --id 7"}]}],
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": "~/bin/my-hook stop"}]}
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    runner.invoke(app, ["agents", "connect", "claude-code"])
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    stop_commands = [h["hooks"][0]["command"] for h in settings["hooks"]["Stop"]]
    start_commands = [h["hooks"][0]["command"] for h in settings["hooks"]["SessionStart"]]
    assert "webhook stop --id 7" in stop_commands
    assert "~/bin/my-hook stop" in start_commands
    runner.invoke(app, ["agents", "disconnect", "claude-code"])
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert "webhook stop --id 7" in [h["hooks"][0]["command"] for h in settings["hooks"]["Stop"]]


def test_partial_install_is_reported_not_healthy(runner: CliRunner, fake_home: Path) -> None:
    from aisquare.core import agents as agent_core

    # A pre-Stop/Notification-era install: only the two original events.
    settings_path = fake_home / ".claude" / "settings.json"
    old = "/some/old/path/aisquare"
    settings_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": f"{old} hook session-start"}]}
                    ],
                    "UserPromptSubmit": [
                        {
                            "hooks": [
                                {"type": "command", "command": f"{old} hook user-prompt-submit"}
                            ]
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    assert agent_core.hooks_installed("claude-code") is False  # partial ≠ installed
    runner.invoke(app, ["agents", "connect", "claude-code"])  # the documented fix
    assert agent_core.hooks_installed("claude-code") is True


def test_spaced_install_path_roundtrips_through_hooks(
    runner: CliRunner, fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from aisquare.core import agents as agent_core

    spaced = tmp_path / "some dir" / "aisquare"
    spaced.parent.mkdir(parents=True)
    spaced.write_text("#!/bin/sh\n")
    monkeypatch.setattr("aisquare.core.agents.sys.argv", [str(spaced)])
    assert runner.invoke(app, ["agents", "connect", "claude-code"]).exit_code == 0
    assert agent_core.hooks_installed("claude-code") is True  # quoted path matches
    # Reconnect must not duplicate groups (the matcher recognizes its own).
    runner.invoke(app, ["agents", "connect", "claude-code"])
    settings = json.loads((fake_home / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert len(settings["hooks"]["SessionStart"]) == 1
    assert runner.invoke(app, ["agents", "disconnect", "claude-code"]).exit_code == 0
    settings = json.loads((fake_home / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert "hooks" not in settings  # removable too


def test_disconnect_warns_when_nothing_was_removed(runner: CliRunner, fake_home: Path) -> None:
    result = runner.invoke(app, ["agents", "disconnect", "claude-code"])
    assert result.exit_code == 0
    assert "no aisquare hooks found" in result.output


# --- parallel installs: one agent, several config dirs -----------------------


def _connect(runner: CliRunner, config_dir: Path | None = None) -> Any:
    argv = ["agents", "connect", "claude-code"]
    if config_dir is not None:
        argv += ["--config-dir", str(config_dir)]
    return runner.invoke(app, argv)


def _sites(runner: CliRunner) -> dict[str, bool]:
    listed = runner.invoke(app, ["--json", "agents", "list"])
    agents = {agent["name"]: agent for agent in _json(listed.stdout)}
    return {site["config_dir"]: site["hooks_installed"] for site in agents["claude-code"]["sites"]}


def test_every_connected_config_dir_is_tracked(runner: CliRunner, fake_home: Path) -> None:
    alt = fake_home / ".claude-account1"
    alt.mkdir()
    _connect(runner)
    _connect(runner, alt)

    sites = _sites(runner)
    assert sites == {str(fake_home / ".claude"): True, str(alt): True}


def test_a_broken_sibling_config_dir_is_reported(runner: CliRunner, fake_home: Path) -> None:
    # The bug this guards: doctor checked only the ambient dir, so a sibling
    # install whose hooks had been removed still reported a healthy ✓.
    alt = fake_home / ".claude-account1"
    alt.mkdir()
    _connect(runner)
    _connect(runner, alt)
    settings = alt / "settings.json"
    data = json.loads(settings.read_text(encoding="utf-8"))
    data.pop("hooks")
    settings.write_text(json.dumps(data), encoding="utf-8")

    assert _sites(runner) == {str(fake_home / ".claude"): True, str(alt): False}

    doctor = runner.invoke(app, ["doctor"])
    assert "claude-code" in doctor.output
    assert str(alt) in doctor.output, "doctor must name the unhooked directory"


def test_doctor_is_green_when_every_dir_is_hooked(runner: CliRunner, fake_home: Path) -> None:
    alt = fake_home / ".claude-account1"
    alt.mkdir()
    _connect(runner)
    _connect(runner, alt)

    doctor = runner.invoke(app, ["doctor"])
    assert "2 config dirs" in doctor.output


def test_disconnecting_one_dir_keeps_the_others(runner: CliRunner, fake_home: Path) -> None:
    alt = fake_home / ".claude-account1"
    alt.mkdir()
    _connect(runner)
    _connect(runner, alt)

    runner.invoke(app, ["agents", "disconnect", "claude-code", "--config-dir", str(alt)])

    assert _sites(runner) == {str(fake_home / ".claude"): True}
    listed = runner.invoke(app, ["--json", "agents", "list"])
    agents = {agent["name"]: agent for agent in _json(listed.stdout)}
    assert agents["claude-code"]["connected"] is True, "the default dir is still connected"


def test_disconnecting_the_last_dir_marks_the_agent_disconnected(
    runner: CliRunner, fake_home: Path
) -> None:
    _connect(runner)
    runner.invoke(app, ["agents", "disconnect", "claude-code"])

    listed = runner.invoke(app, ["--json", "agents", "list"])
    agents = {agent["name"]: agent for agent in _json(listed.stdout)}
    assert agents["claude-code"]["connected"] is False
    assert agents["claude-code"]["sites"] == []


def test_a_legacy_registry_still_reports_one_site(runner: CliRunner, fake_home: Path) -> None:
    # Registries written before multi-dir tracking held only a bare name.
    from aisquare.core import paths

    _connect(runner)
    paths.agents_registry_path().write_text(
        json.dumps({"connected": ["claude-code"]}), encoding="utf-8"
    )

    assert _sites(runner) == {str(fake_home / ".claude"): True}


def test_doctor_checks_the_ambient_dir_even_when_the_registry_has_sites(
    runner: CliRunner, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recorded sites healthy + ambient CLAUDE_CONFIG_DIR unhooked -> warn, named.

    The ambient dir is the one a `claude` from THIS shell would actually use.
    Registry health says nothing about it: with every recorded site hooked,
    doctor reported a clean bill while the user's next session started
    unhooked. Ambient must be checked as sites UNION {ambient}, not either-or.
    """
    _connect(runner)  # default dir: hooked and recorded
    ambient = fake_home / ".claude-fresh"
    ambient.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(ambient))

    doctor = runner.invoke(app, ["doctor"])

    assert str(ambient) in doctor.output, "the unhooked ambient dir must be named"
    assert "missing" in doctor.output
