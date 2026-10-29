"""Agent detection and connect (ingesting an agent's existing context)."""

from __future__ import annotations

import errno
import importlib.metadata
import json
import os
import shlex
import stat
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import agents, paths, selfcli
from tests.fsperms import can_deny_writes, can_symlink, unwritable


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


def test_connect_not_installed_fails(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    # The suite's agent home has no ~/.claude, and here no `claude` is on PATH either
    # (with one, connect makes the directory: test_agent_adapters.py). Cursor, the old
    # example here, is refused as not connectable before presence is asked.
    monkeypatch.setattr(agents, "on_path", lambda name: None)
    result = runner.invoke(app, ["agents", "connect", "claude-code"])
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
        "StopFailure",
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
    fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr("aisquare.core.agents.sys.argv", [str(fake_bin)])
    monkeypatch.setattr("aisquare.core.agents.shutil.which", lambda _: None)
    monkeypatch.setattr(agents, "current_install", lambda: tmp_path / "no-venv" / "aisquare")
    result = runner.invoke(app, ["agents", "connect", "claude-code"])
    assert result.exit_code == 0, result.output
    settings = json.loads((fake_home / ".claude" / "settings.json").read_text(encoding="utf-8"))
    command = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert command == f"{fake_bin.resolve()} hook session-start"

    # 2) With no usable argv0 and nothing on PATH: python -P -m aisquare, never bare
    #    (-P so a project's own aisquare/ cannot shadow the hook — #81).
    import sys as real_sys

    monkeypatch.setattr("aisquare.core.agents.sys.argv", ["pytest"])
    result = runner.invoke(app, ["agents", "connect", "claude-code"])
    assert result.exit_code == 0, result.output
    settings = json.loads((fake_home / ".claude" / "settings.json").read_text(encoding="utf-8"))
    command = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert command == f"{real_sys.executable} -P -m aisquare hook session-start"


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


def test_hooks_without_the_context_timeout_are_installed_but_short(
    runner: CliRunner, fake_home: Path
) -> None:
    """A settings file written before the context hooks carried ``timeout`` is
    INSTALLED — the hooks fire — and separately short of the ceiling the CI hook
    may wait for. Calling that "not installed" made ``doctor`` misdescribe a
    working install (0.6.0's settings.json, or a hand-edited one) and send the
    operator to a command that would rewrite entries they had chosen.
    """
    from aisquare.core import agents as agent_core

    assert runner.invoke(app, ["agents", "connect", "claude-code"]).exit_code == 0
    path = fake_home / ".claude" / "settings.json"
    settings = json.loads(path.read_text(encoding="utf-8"))
    for event in ("SessionStart", "UserPromptSubmit"):
        for group in settings["hooks"][event]:
            for item in group["hooks"]:
                item.pop("timeout", None)
    path.write_text(json.dumps(settings), encoding="utf-8")

    assert agent_core.hooks_installed("claude-code") is True, "the hooks are there and firing"
    assert agent_core.hook_timeout_shortfall("claude-code") == ["SessionStart", "UserPromptSubmit"]

    runner.invoke(app, ["agents", "connect", "claude-code"])

    assert agent_core.hook_timeout_shortfall("claude-code") == []
    refreshed = json.loads(path.read_text(encoding="utf-8"))
    assert all(
        item["timeout"] == agent_core.CONTEXT_HOOK_TIMEOUT_SECONDS
        for group in refreshed["hooks"]["UserPromptSubmit"]
        for item in group["hooks"]
    )


def test_a_longer_operator_timeout_is_kept_not_reconciled_down(
    runner: CliRunner, fake_home: Path
) -> None:
    """180 is a deliberate choice with more headroom than we need. Exact
    equality read it as "not installed" and ``connect`` overwrote it."""
    from aisquare.core import agents as agent_core

    assert runner.invoke(app, ["agents", "connect", "claude-code"]).exit_code == 0
    path = fake_home / ".claude" / "settings.json"
    settings = json.loads(path.read_text(encoding="utf-8"))
    for group in settings["hooks"]["UserPromptSubmit"]:
        for item in group["hooks"]:
            item["timeout"] = 180
    path.write_text(json.dumps(settings), encoding="utf-8")

    assert agent_core.hooks_installed("claude-code") is True
    assert agent_core.hook_timeout_shortfall("claude-code") == [], "180 clears our ceiling"

    runner.invoke(app, ["agents", "connect", "claude-code"])

    refreshed = json.loads(path.read_text(encoding="utf-8"))
    kept = [
        item["timeout"]
        for group in refreshed["hooks"]["UserPromptSubmit"]
        for item in group["hooks"]
    ]
    assert kept == [180], "connect must not reduce a ceiling the operator raised"


def test_spaced_install_path_roundtrips_through_hooks(
    runner: CliRunner, fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from aisquare.core import agents as agent_core

    spaced = tmp_path / "some dir" / "aisquare"
    spaced.parent.mkdir(parents=True)
    spaced.write_text("#!/bin/sh\n", encoding="utf-8")
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


def test_settings_json_is_written_in_utf8_as_claude_code_writes_it(
    runner: CliRunner, fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``json.dumps`` escaped every non-ASCII character, so a hook naming
    ``~/Développement/…/aisquare`` was stored as ``D\\u00e9veloppement``, which the
    plugin's launcher cannot read a program out of (review of #257). A lone surrogate,
    which UTF-8 cannot hold, keeps its escape instead of costing connect a traceback."""
    program = tmp_path / "Développement" / "aisquare"
    program.parent.mkdir(parents=True)
    program.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr("aisquare.core.agents.sys.argv", [str(program)])
    settings_path = fake_home / ".claude" / "settings.json"
    mine = '"env": {"GREETING": "café", "ODD": "\\udce9"}'
    settings_path.write_text("{" + mine + "}", encoding="utf-8")

    connected = runner.invoke(app, ["agents", "connect", "claude-code"])
    written = settings_path.read_text(encoding="utf-8")
    disconnected = runner.invoke(app, ["agents", "disconnect", "claude-code"])
    left = settings_path.read_text(encoding="utf-8")

    assert connected.exit_code == 0 and disconnected.exit_code == 0, connected.output
    assert agents.hook_commands("claude-code") == [], "disconnect found every hook it wrote"
    assert "Développement" in written and "\\u00e9" not in written, written
    assert '"GREETING": "café"' in left and '"ODD": "\\udce9"' in left, left
    assert json.loads(left) == {"env": {"GREETING": "café", "ODD": "\udce9"}}


MINE = {"permissions": {"allow": ["Bash(ls)"]}, "env": {"GREETING": "café"}}
"""Settings of the user's own, which no write of the hooks may cost them."""


def _full_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every file opened for writing is opened, so truncated, and then the disk is full."""
    real_open = Path.open

    def opened(self: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        handle = real_open(self, mode, *args, **kwargs)
        if "w" in mode:
            handle.close()
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC), str(self))
        return handle

    monkeypatch.setattr(Path, "open", opened)


@pytest.mark.parametrize("writer", ["install_hooks", "remove_hooks"])
def test_a_settings_write_cut_off_partway_leaves_every_setting_as_it_was(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, writer: str
) -> None:
    """Rewritten in place, a write that failed after the truncate (a full disk, a kill
    during connect or uninstall) left a settings.json that is not JSON: every surface then
    refused it and Claude Code ran without the user's settings (round 19 of #257). It is
    replaced now, so the file holds all of the old text or all of the new."""
    claude = fake_home / ".claude"
    settings_path = claude / "settings.json"
    settings_path.write_text(json.dumps(MINE), encoding="utf-8")
    if writer == "remove_hooks":
        agents.install_hooks("claude-code", claude)
    before = settings_path.read_bytes()
    _full_disk(monkeypatch)

    with pytest.raises(OSError, match="No space left"):
        getattr(agents, writer)("claude-code", claude)

    monkeypatch.undo()
    assert settings_path.read_bytes() == before, "the old text, whole"
    assert sorted(p.name for p in claude.iterdir()) == ["CLAUDE.md", "settings.json"], "no temp"


@pytest.mark.skipif(not can_symlink(), reason="needs a symlink")
def test_a_linked_settings_json_has_the_file_it_points_at_replaced(fake_home: Path) -> None:
    """A dotfiles link stays a link: the file it points at gets the hooks, as save_config
    writes a linked config.toml, rather than a new file replacing the link."""
    dotfiles = fake_home / "dotfiles"
    dotfiles.mkdir()
    real = dotfiles / "claude-settings.json"
    real.write_text(json.dumps(MINE), encoding="utf-8")
    link = fake_home / ".claude" / "settings.json"
    link.symlink_to(real)

    agents.install_hooks("claude-code", link.parent)

    assert link.is_symlink() and link.resolve() == real.resolve()
    assert agents.hook_commands("claude-code", link.parent), "the hooks are in the linked file"
    assert json.loads(real.read_text(encoding="utf-8"))["env"] == MINE["env"]
    assert sorted(p.name for p in dotfiles.iterdir()) == ["claude-settings.json"], "no temp"


@pytest.mark.skipif(not can_deny_writes(), reason="needs a folder this user cannot write")
def test_a_settings_json_in_a_folder_that_takes_no_new_file_is_written_in_place(
    fake_home: Path,
) -> None:
    """No temp can be made beside it, but the file itself may be written: the one write
    there is, as before (round 19 of #257)."""
    claude = fake_home / ".claude"
    settings_path = claude / "settings.json"
    settings_path.write_text(json.dumps(MINE), encoding="utf-8")
    inode = settings_path.stat().st_ino

    with unwritable(claude):
        agents.install_hooks("claude-code", claude)

    assert agents.hook_commands("claude-code", claude)
    assert json.loads(settings_path.read_text(encoding="utf-8"))["env"] == MINE["env"]
    if sys.platform != "win32":
        assert settings_path.stat().st_ino == inode, "in place"


@pytest.mark.skipif(not can_deny_writes(), reason="needs a file this user cannot write")
def test_a_settings_json_this_user_may_not_write_is_not_replaced(fake_home: Path) -> None:
    """Its folder would take the temp, and the rename would replace a file its owner made
    read-only: refused as the in-place write refused it, and left as it was."""
    settings_path = fake_home / ".claude" / "settings.json"
    settings_path.write_text(json.dumps(MINE), encoding="utf-8")
    before = settings_path.read_bytes()
    settings_path.chmod(0o444)
    try:
        with pytest.raises(PermissionError):
            agents.install_hooks("claude-code", settings_path.parent)
    finally:
        settings_path.chmod(0o644)

    assert settings_path.read_bytes() == before


@pytest.mark.skipif(not hasattr(os, "link"), reason="needs a hard link")
def test_a_hard_linked_settings_json_is_written_in_place_so_both_names_get_the_hooks(
    fake_home: Path,
) -> None:
    """Round 20 of #257: a dotfiles tool that hard-links rather than symlinks. Renamed
    over, the settings.json name got a new file and the other name kept the old one,
    without the hooks; written in place, both names are the one file."""
    dotfiles = fake_home / "dotfiles"
    dotfiles.mkdir()
    real = dotfiles / "claude-settings.json"
    real.write_text(json.dumps(MINE), encoding="utf-8")
    settings_path = fake_home / ".claude" / "settings.json"
    os.link(real, settings_path)

    agents.install_hooks("claude-code", settings_path.parent)

    assert os.path.samefile(real, settings_path), "still one file"
    assert agents.hook_commands("claude-code", settings_path.parent)
    assert real.read_text(encoding="utf-8") == settings_path.read_text(encoding="utf-8")
    assert sorted(p.name for p in settings_path.parent.iterdir()) == [
        "CLAUDE.md",
        "settings.json",
    ], "no temp"


@pytest.mark.parametrize("refused", [errno.EBUSY, errno.EXDEV], ids=["EBUSY", "EXDEV"])
def test_a_settings_json_mounted_on_its_own_is_written_in_place(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, refused: int
) -> None:
    """Round 20 of #257: `docker run -v ~/.claude/settings.json:…` mounts the file alone,
    and a rename onto a mount point is refused with EBUSY (EXDEV across devices), which is
    no PermissionError: connect, uninstall and refresh-hooks failed where the in-place
    write had worked. The rename is refused here as the kernel refuses it."""
    settings_path = fake_home / ".claude" / "settings.json"
    settings_path.write_text(json.dumps(MINE), encoding="utf-8")
    real_replace = os.replace

    def replace(source: Any, destination: Any) -> None:
        if Path(destination) == settings_path.resolve():
            raise OSError(refused, os.strerror(refused), str(destination))
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", replace)

    agents.install_hooks("claude-code", settings_path.parent)

    monkeypatch.undo()
    assert agents.hook_commands("claude-code", settings_path.parent)
    assert json.loads(settings_path.read_text(encoding="utf-8"))["env"] == MINE["env"]
    assert sorted(p.name for p in settings_path.parent.iterdir()) == [
        "CLAUDE.md",
        "settings.json",
    ], "no temp"


def _another_group() -> int | None:
    """A group this user is in besides the one its new files get, if it has one."""
    if sys.platform == "win32":  # mypy narrows on this, not on hasattr: no getegid there
        return None
    return next((g for g in os.getgroups() if g != os.getegid()), None)


@pytest.mark.skipif(_another_group() is None, reason="needs a second group to give the file")
def test_a_settings_json_of_another_group_keeps_its_group(fake_home: Path) -> None:
    """Round 20 of #257: replaced, the file's owner and group became this user's and its
    primary group, its mode kept but read against another group, so a file shared
    through its group stopped being writable by that group. Written in place, it keeps
    both; so does a file another user owns (the two tests below)."""
    settings_path = fake_home / ".claude" / "settings.json"
    settings_path.write_text(json.dumps(MINE), encoding="utf-8")
    group = _another_group()
    assert group is not None
    if sys.platform == "win32":  # mypy narrows on this, not on the skipif that keeps it off
        pytest.skip("no os.chown on Windows")
    os.chown(settings_path, -1, group)
    inode = settings_path.stat().st_ino

    agents.install_hooks("claude-code", settings_path.parent)

    assert agents.hook_commands("claude-code", settings_path.parent)
    assert settings_path.stat().st_gid == group
    assert settings_path.stat().st_ino == inode, "in place"


def _replaced_targets(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every path a rename lands on from here on, as ``os.replace`` was asked it."""
    replaced: list[str] = []
    real_replace = os.replace

    def replace(source: Any, destination: Any) -> None:
        replaced.append(str(destination))
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", replace)
    return replaced


def _as_root() -> bool:
    """Whether this suite runs as root, which alone may give a file to another user."""
    if sys.platform == "win32":  # mypy narrows on this: no geteuid there
        return False
    return os.geteuid() == 0


@pytest.mark.skipif(not _as_root(), reason="only root can give a file to another user")
def test_a_settings_json_another_user_owns_keeps_its_owner(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 20 of #257: replaced, a file another user owns became the writer's, here
    root's, and its owner lost it. Written in place, it keeps its owner. CI runs as an
    ordinary user, so the test after this one emulates the owner, where `_as_it_was`
    reads it; this one runs where the suite runs as root (a container, `unshare -r`)."""
    if sys.platform == "win32":  # mypy narrows on this, not on the skipif that keeps it off
        pytest.skip("no os.chown on Windows")
    settings_path = fake_home / ".claude" / "settings.json"
    settings_path.write_text(json.dumps(MINE), encoding="utf-8")
    owner = 65534  # nobody
    os.chown(settings_path, owner, -1)
    inode = settings_path.stat().st_ino
    replaced = _replaced_targets(monkeypatch)

    agents.install_hooks("claude-code", settings_path.parent)

    assert agents.hook_commands("claude-code", settings_path.parent)
    assert settings_path.stat().st_uid == owner, "its owner's still"
    assert settings_path.stat().st_ino == inode and replaced == [], "in place"


def test_a_settings_json_another_user_owns_is_written_in_place_as_its_owner_reads(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The test above, where no root can make the file: its owner is emulated where
    `_as_it_was` reads it, the target's stat, so every suite asks whether an owner other
    than the temp's keeps the rename off (follow-up of #257). The temp, and every other
    file, reads as it is."""
    settings_path = fake_home / ".claude" / "settings.json"
    settings_path.write_text(json.dumps(MINE), encoding="utf-8")
    target = settings_path.resolve()
    real_stat = Path.stat

    def stat_of_anothers(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        found = real_stat(self, follow_symlinks=follow_symlinks)
        if self != target:
            return found
        fields = list(found)
        fields[stat.ST_UID] = found.st_uid + 1
        return os.stat_result(fields)

    monkeypatch.setattr(Path, "stat", stat_of_anothers)
    replaced = _replaced_targets(monkeypatch)

    agents.install_hooks("claude-code", settings_path.parent)

    monkeypatch.undo()
    assert replaced == [], "in place: no rename over it"
    assert agents.hook_commands("claude-code", settings_path.parent)
    assert json.loads(settings_path.read_text(encoding="utf-8"))["env"] == MINE["env"]
    assert sorted(p.name for p in settings_path.parent.iterdir()) == [
        "CLAUDE.md",
        "settings.json",
    ], "no temp"


def _inherited_dacl(folder: Path) -> str | None:
    """The DACL a new file in ``folder`` starts with, as a temp beside a file does."""
    fresh = folder / "fresh.json"
    fresh.write_text("{}", encoding="utf-8")
    try:
        return paths.dacl_sddl(fresh)
    finally:
        fresh.unlink()


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS ACLs")
def test_a_settings_json_with_a_dacl_of_its_own_is_written_in_place_and_keeps_it(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 20 of #257: on NTFS a rename keeps the temp's security descriptor, which is the
    DACL its folder hands down, so a settings.json given a grant of its own lost it.
    `_as_it_was` compares the two DACLs and writes in place where they differ. Real ACLs,
    on the windows leg (follow-up of #257)."""
    from tests import winacl

    settings_path = fake_home / ".claude" / "settings.json"
    settings_path.write_text(json.dumps(MINE), encoding="utf-8")
    winacl.grant_other_principal(settings_path)
    before = paths.dacl_sddl(settings_path)
    assert before is not None and before != _inherited_dacl(settings_path.parent), (
        "premise: a DACL its folder does not hand down"
    )
    replaced = _replaced_targets(monkeypatch)

    agents.install_hooks("claude-code", settings_path.parent)

    monkeypatch.undo()
    assert replaced == [], "in place: no rename over it"
    assert agents.hook_commands("claude-code", settings_path.parent)
    assert paths.dacl_sddl(settings_path) == before, "its own DACL, kept"
    assert winacl.OTHER_PRINCIPAL_TRUSTEE in winacl.dacl_trustees(settings_path)


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS ACLs")
def test_a_settings_json_whose_dacl_denies_this_user_writing_is_left_as_it_was(
    fake_home: Path,
) -> None:
    """Round 20 of #257: `os.access` on Windows reads the read-only flag alone, so a file
    whose DACL denies this user writing passed it, and the rename, which needs no write
    to the file, replaced it all the same. The DACLs differ, so it is written in place,
    which the DACL refuses: a PermissionError, and the file as it was. Real ACLs, on the
    windows leg (follow-up of #257)."""
    from tests import winacl

    settings_path = fake_home / ".claude" / "settings.json"
    settings_path.write_text(json.dumps(MINE), encoding="utf-8")
    before = settings_path.read_bytes()
    me = winacl.current_user_sid()
    winacl.deny_writing(settings_path, me)
    try:
        assert os.access(settings_path, os.W_OK), "premise: os.access does not read the DACL"
        with pytest.raises(PermissionError):
            agents.install_hooks("claude-code", settings_path.parent)
    finally:
        winacl.undo_denials(settings_path, me)

    assert settings_path.read_bytes() == before
    assert sorted(p.name for p in settings_path.parent.iterdir()) == [
        "CLAUDE.md",
        "settings.json",
    ], "no temp"


def test_a_settings_json_nobody_else_shares_is_still_replaced(fake_home: Path) -> None:
    """The control: one name, this user's own, in a folder that takes a temp. Replaced,
    as round 19 made it, so a write cut off partway keeps every setting."""
    settings_path = fake_home / ".claude" / "settings.json"
    settings_path.write_text(json.dumps(MINE), encoding="utf-8")
    replaced: list[str] = []
    real_replace = os.replace

    def replace(source: Any, destination: Any) -> None:
        replaced.append(str(destination))
        real_replace(source, destination)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "replace", replace)
        agents.install_hooks("claude-code", settings_path.parent)

    assert replaced == [str(settings_path.resolve())]
    assert agents.hook_commands("claude-code", settings_path.parent)


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


# --- Windows: hook commands the native shell can actually run ----------------


def _win32(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend to be Windows, so POSIX CI still covers the Windows paths."""
    monkeypatch.setattr("aisquare.core.agents.sys.platform", "win32")


def test_windows_hook_commands_are_not_posix_quoted(monkeypatch: pytest.MonkeyPatch) -> None:
    """``shlex.quote`` is wrong on Windows: a backslash makes it quote the path.

    ``cmd.exe`` has no single-quote syntax, so it looks for a program whose
    name literally starts with ``'`` and the hook never runs.
    """
    _win32(monkeypatch)
    exe = r"C:\Python312\Scripts\aisquare.EXE"
    monkeypatch.setattr("aisquare.core.agents.sys.argv", ["pytest"])
    monkeypatch.setattr("aisquare.core.agents.shutil.which", lambda _name: exe)
    monkeypatch.setattr(agents, "_starts", lambda program: str(program) == exe)

    command = agents._aisquare_command()

    assert command == exe
    assert "'" not in command


def test_windows_hook_commands_round_trip_through_the_matcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What connect writes, connect and disconnect must recognise again.

    If these two halves disagree, connect stops seeing its own hooks and
    appends a duplicate on every run, and disconnect can never remove them.
    """
    _win32(monkeypatch)
    monkeypatch.setattr("aisquare.core.agents.sys.argv", ["pytest"])
    monkeypatch.setattr(agents, "_starts", lambda program: "Python312" in str(program))
    for exe in (
        r"C:\Python312\Scripts\aisquare.EXE",
        r"C:\Program Files\Python312\Scripts\asq.exe",  # a path needing quotes
    ):
        monkeypatch.setattr("aisquare.core.agents.shutil.which", lambda _name, _e=exe: _e)
        command = f"{agents._aisquare_command()} hook session-start"
        assert agents._is_aisquare_hook_command(command), command


def test_windows_recognises_the_exe_suffixed_console_script(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """argv0 is ``aisquare.exe`` on Windows, which never matched the bare name."""
    _win32(monkeypatch)
    exe = tmp_path / "aisquare.EXE"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setattr("aisquare.core.agents.sys.argv", [str(exe)])
    monkeypatch.setattr("aisquare.core.agents.shutil.which", lambda _name: None)

    assert agents._aisquare_command() == str(exe.resolve())


def test_windows_still_ignores_third_party_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Windows matcher must stay as strict as the POSIX one."""
    _win32(monkeypatch)
    for command in (
        r"C:\tools\webhook.exe stop",
        r"C:\Users\me\bin\my-hook.exe stop",
        "webhook stop",
        r"C:\tools\notaisquare.exe hook stop",
    ):
        assert not agents._is_aisquare_hook_command(command), command


@pytest.mark.skipif(os.name == "nt", reason="a #! interpreter is a POSIX shape")
@pytest.mark.parametrize("installed", [True, False], ids=["this-install", "none-beside-it"])
def test_connect_run_as_a_module_never_writes_back_a_program_that_cannot_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, installed: bool
) -> None:
    """Every button runs the CLI as `python -P -m aisquare`, so argv0 is not a console
    script and PATH was asked: a stale install first on it, whose Python is gone, was
    written back, and Connect never cleared "cannot start" (review of #257). The console
    script beside this interpreter comes first, then PATH, each only when it can start."""
    dead = tmp_path / "stale" / "bin" / "aisquare"
    dead.parent.mkdir(parents=True)
    dead.write_text(f"#!{tmp_path / 'gone' / 'python3.11'}\n", encoding="utf-8")
    dead.chmod(0o755)
    here = tmp_path / "venv" / "bin" / "aisquare"
    if installed:
        here.parent.mkdir(parents=True)
        here.write_text("#!/bin/sh\n", encoding="utf-8")
        here.chmod(0o755)
    monkeypatch.setattr("aisquare.core.agents.sys.platform", "linux")
    monkeypatch.setattr("aisquare.core.agents.sys.argv", ["/x/aisquare/__main__.py"])
    monkeypatch.setattr(agents, "current_install", lambda: here)
    which: dict[str, str] = {"aisquare": str(dead)}
    monkeypatch.setattr("aisquare.core.agents.shutil.which", which.get)

    command = agents._aisquare_command()
    alive = tmp_path / "live" / "aisquare"
    alive.parent.mkdir()
    alive.write_text("#!/bin/sh\n", encoding="utf-8")
    alive.chmod(0o755)
    monkeypatch.setattr(agents, "current_install", lambda: tmp_path / "no-venv" / "aisquare")
    which["aisquare"] = str(alive)
    control = agents._aisquare_command()

    module_form = " ".join(shlex.quote(part) for part in selfcli.argv_for([]))
    assert command == (str(here) if installed else module_form), command
    assert str(dead) not in command
    assert control == str(alive), "control: a PATH program that can start is still used"


def test_this_install_is_located_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Round 21 of #257: Connect's command, every hook binary's comparison, the doctor's
    install row and each "this install is" line located the distribution and walked its
    RECORD again, for an answer that cannot change within the process."""
    real = importlib.metadata.distribution
    located: list[str] = []

    def distribution(name: str) -> Any:
        located.append(name)
        return real(name)

    monkeypatch.setattr("aisquare.core.agents.importlib.metadata.distribution", distribution)

    first = agents.current_install()
    agents._aisquare_command()
    agents._same_install(agents.HookBinary(Path("/elsewhere/bin/aisquare")))

    assert agents.current_install() == first
    assert located == ["aisquare-cli"], "located once"


def test_posix_quoting_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """The POSIX path must keep quoting exactly as before."""
    monkeypatch.setattr("aisquare.core.agents.sys.platform", "linux")
    exe = "/opt/my tools/bin/aisquare"
    monkeypatch.setattr("aisquare.core.agents.sys.argv", ["pytest"])
    monkeypatch.setattr("aisquare.core.agents.shutil.which", lambda _name: exe)
    # Paths compared, not strings: on Windows str(Path(exe)) has backslashes.
    monkeypatch.setattr(agents, "_starts", lambda program: Path(program) == Path(exe))

    command = agents._aisquare_command()

    assert command == "'/opt/my tools/bin/aisquare'"
    assert agents._is_aisquare_hook_command(f"{command} hook stop")


def test_connect_gives_the_context_hooks_room_for_the_ci_ceiling(
    runner: CliRunner, fake_home: Path
) -> None:
    """Claude Code cancels a command hook at 60 s by default and discards its
    output; the CI test bed's prompt call may wait up to the descriptor's
    ceiling (60 s today) before degrading. The two context hooks therefore
    carry a longer timeout, and the bookkeeping hooks keep the default."""
    from aisquare.core.agents import CONTEXT_HOOK_TIMEOUT_SECONDS

    runner.invoke(app, ["agents", "connect", "claude-code"])
    settings = json.loads((fake_home / ".claude" / "settings.json").read_text(encoding="utf-8"))
    for event in ("SessionStart", "UserPromptSubmit"):
        entry = settings["hooks"][event][0]["hooks"][0]
        assert entry["timeout"] == CONTEXT_HOOK_TIMEOUT_SECONDS, event
    for event in ("Stop", "SessionEnd", "Notification", "StopFailure"):
        assert "timeout" not in settings["hooks"][event][0]["hooks"][0], event
    assert CONTEXT_HOOK_TIMEOUT_SECONDS > 60
