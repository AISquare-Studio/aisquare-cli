"""``doctor`` grades WHICH aisquare the Claude Code hooks run, and finds dirs on disk (#84).

Measured 2026-09-04: every hook in ``~/.claude`` and ``~/.claude3`` named
``/home/work/work/aisquare-cli/.venv/bin/aisquare``, an editable install of a
0.3-era checkout, while the live ``aisquare`` on PATH was 0.4.0rc1 (now 0.6.0).
Doctor said ``✓ claude-code: Claude Code 2.1.260 connected (all lifecycle hooks
installed)`` throughout, because ``_is_aisquare_hook_command`` grades the TEXT
of a hook and the text was ours. Every board update for weeks ran old code.

Two gaps, both closed and both pinned here:

* the binary: per config dir, resolve the program the hooks name and compare
  it to this install — by path, or by running ``<path> --version`` when the
  path differs. Stale, missing and unreadable all warn, with the fix line.
* the directories: ``$CLAUDE_CONFIG_DIR``, ``~/.claude`` and ``~/.claude*`` on
  disk are graded even when this ``AISQUARE_HOME`` never connected them, and
  labelled so.

The probe is a registered spawn seam; conftest stubs it suite-wide (a hook
written under pytest names whatever PATH has, which is ambient state). Tests of
the probe itself use ``_REAL_PROBE``, captured at import before any fixture
runs, against scripts they write.
"""

from __future__ import annotations

import json
import shlex
import stat
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import agents
from aisquare.core.version import __version__
from aisquare.models import CheckStatus
from aisquare.services import diagnostics

_REAL_PROBE = agents.hook_binary_version

#: The version the stale checkout in #84 reported.
OLD = "0.3.0rc1"


# --- fixtures and fakes ------------------------------------------------------------------


def _write_hooks(
    config_dir: Path, command: str, *, events: Sequence[tuple[str, str]] = agents._HOOKS
) -> Path:
    """A ``settings.json`` with our hooks, all naming ``command`` — the shape connect writes."""
    config_dir.mkdir(parents=True, exist_ok=True)
    settings = config_dir / "settings.json"
    hooks = {
        event: [{"hooks": [{"type": "command", "command": f"{command} hook {sub}"}]}]
        for event, sub in events
    }
    settings.write_text(json.dumps({"hooks": hooks}, indent=2) + "\n", encoding="utf-8")
    return settings


def _fake_aisquare(path: Path, *, prints: str | None = None, exit_code: int = 0) -> Path:
    """An executable that answers ``--version`` the way another install would."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["#!/bin/sh"]
    if prints is not None:
        lines.append(f"echo '{prints}'")
    lines.append(f"exit {exit_code}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _real_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo conftest's stub: actually run the binaries these tests write."""
    monkeypatch.setattr(agents, "hook_binary_version", _REAL_PROBE)


def _never_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """The verdict must come from paths alone; a spawn is the failure."""

    def boom(argv: Sequence[str], **_kwargs: object) -> str | None:
        raise AssertionError(f"the version probe ran for {list(argv)}")

    monkeypatch.setattr(agents, "hook_binary_version", boom)


def _this_install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Put this install's console script at a temp path that exists.

    ``current_install()`` is the script beside ``sys.executable``; here that is
    whatever env runs the suite, so the tests pin it to a script they own.
    """
    current = _fake_aisquare(
        tmp_path / "this-install" / "bin" / "aisquare", prints=f"aisquare {__version__}"
    )
    monkeypatch.setattr(agents, "current_install", lambda: current)
    return current


def _connect(config_dir: Path) -> None:
    """Record ``config_dir`` as connected in this (temp) AISQUARE_HOME."""
    agents.set_connected("claude-code", True, config_dir)


# --- the binary the hooks name -----------------------------------------------------------


def test_hooks_naming_this_install_are_green_without_a_spawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_agent_home: Path
) -> None:
    current = _this_install(monkeypatch, tmp_path)
    _never_probe(monkeypatch)
    config = isolated_agent_home / ".claude"
    _write_hooks(config, str(current))
    _connect(config)

    check = diagnostics._check_claude_code()

    assert check.status is CheckStatus.ok
    assert "all lifecycle hooks installed" in check.detail
    assert "not connected in this home" not in check.detail, "a recorded dir is not a discovery"


def test_the_console_script_beside_this_interpreter_is_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unpinned comparison, against the real ``current_install()``."""
    current = agents.current_install()
    if not current.exists():
        pytest.skip(f"no console script beside {sys.executable}; the env is not an install")
    _never_probe(monkeypatch)

    state, version = agents.classify_hook_binary(agents.HookBinary(current))

    assert (state, version) == (agents.HOOK_BINARY_CURRENT, __version__)


def test_a_stale_binary_is_a_warning_that_names_both_installs_and_the_fix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_agent_home: Path
) -> None:
    """The #84 row: hooks at an old checkout, doctor run from the live install."""
    current = _this_install(monkeypatch, tmp_path)
    _real_probe(monkeypatch)
    old = _fake_aisquare(
        tmp_path / "aisquare-cli" / ".venv" / "bin" / "aisquare", prints=f"aisquare {OLD}"
    )
    config = isolated_agent_home / ".claude"
    _write_hooks(config, str(old))
    _connect(config)

    check = diagnostics._check_claude_code()

    assert check.status is CheckStatus.warn
    for needle in (str(config), str(old), f"({OLD})", str(current), f"({__version__})"):
        assert needle in check.detail, f"{needle!r} missing from {check.detail!r}"
    assert check.fix == f"aisquare agents connect claude-code --config-dir {config}"


def test_the_cli_row_renders_the_warning_and_the_fix_line(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_agent_home: Path
) -> None:
    _this_install(monkeypatch, tmp_path)
    _real_probe(monkeypatch)
    old = _fake_aisquare(tmp_path / "old" / "bin" / "aisquare", prints=f"aisquare {OLD}")
    config = isolated_agent_home / ".claude"
    _write_hooks(config, str(old))
    _connect(config)

    text = runner.invoke(app, ["doctor"], catch_exceptions=False).output
    payload = json.loads(runner.invoke(app, ["--json", "doctor"], catch_exceptions=False).stdout)

    assert "⚠ claude-code:" in text and OLD in text
    assert f"→ aisquare agents connect claude-code --config-dir {config}" in text
    row = next(row for row in payload if row["name"] == "claude-code")
    assert row["status"] == "warn" and str(old) in row["detail"]


def test_a_missing_binary_is_flagged_and_nothing_is_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_agent_home: Path
) -> None:
    """A deleted worktree's venv: the hooks fail every session, silently."""
    _this_install(monkeypatch, tmp_path)
    _never_probe(monkeypatch)
    gone = tmp_path / "deleted-worktree" / ".venv" / "bin" / "aisquare"
    config = isolated_agent_home / ".claude"
    _write_hooks(config, str(gone))
    _connect(config)

    check = diagnostics._check_claude_code()

    assert check.status is CheckStatus.warn
    assert f"{gone}, which does not exist" in check.detail
    assert check.fix == f"aisquare agents connect claude-code --config-dir {config}"


def test_a_binary_that_will_not_say_its_version_is_not_trusted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_agent_home: Path
) -> None:
    """Unreadable is a warning, not a pass: a green row on an unverifiable binary is the bug."""
    _this_install(monkeypatch, tmp_path)
    _real_probe(monkeypatch)
    mute = _fake_aisquare(tmp_path / "mute" / "aisquare", exit_code=1)
    config = isolated_agent_home / ".claude"
    _write_hooks(config, str(mute))
    _connect(config)

    check = diagnostics._check_claude_code()

    assert check.status is CheckStatus.warn
    assert f"{mute}, whose version could not be read" in check.detail
    assert check.fix == f"aisquare agents connect claude-code --config-dir {config}"


def test_the_same_version_at_another_path_is_current(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_agent_home: Path
) -> None:
    """A shim, a symlinked parent, a second venv at the same version: same code, green."""
    _this_install(monkeypatch, tmp_path)
    _real_probe(monkeypatch)
    twin = _fake_aisquare(tmp_path / "shims" / "aisquare", prints=f"aisquare {__version__}")
    config = isolated_agent_home / ".claude"
    _write_hooks(config, str(twin))
    _connect(config)

    check = diagnostics._check_claude_code()

    assert check.status is CheckStatus.ok, check.detail


def test_the_python_m_fallback_shape_is_graded_by_its_interpreter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_agent_home: Path
) -> None:
    """``<python> -m aisquare hook <event>`` is what connect writes with no console script."""
    config = isolated_agent_home / ".claude"
    _connect(config)

    _never_probe(monkeypatch)
    _write_hooks(config, f"{shlex.quote(sys.executable)} -m aisquare")
    assert diagnostics._check_claude_code().status is CheckStatus.ok

    _real_probe(monkeypatch)
    other = _fake_aisquare(tmp_path / "other-venv" / "bin" / "python", prints=f"aisquare {OLD}")
    _write_hooks(config, f"{other} -m aisquare")
    check = diagnostics._check_claude_code()
    assert check.status is CheckStatus.warn
    assert f"{other} ({OLD})" in check.detail


def test_a_dir_is_graded_by_its_worst_hook(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_agent_home: Path
) -> None:
    current = _this_install(monkeypatch, tmp_path)
    _never_probe(monkeypatch)
    gone = tmp_path / "gone" / "aisquare"
    config = isolated_agent_home / ".claude"
    config.mkdir(parents=True)
    hooks = {
        event: [{"hooks": [{"type": "command", "command": f"{current} hook {sub}"}]}]
        for event, sub in agents._HOOKS
    }
    hooks["Stop"] = [{"hooks": [{"type": "command", "command": f"{gone} hook stop"}]}]
    (config / "settings.json").write_text(json.dumps({"hooks": hooks}), encoding="utf-8")

    site = agents.hook_site_health("claude-code", config, recorded=True)

    assert site.hooks_installed is True, "every event has one of our hooks"
    assert (site.binary_state, site.binary) == (agents.HOOK_BINARY_MISSING, gone)


def test_one_binary_named_by_two_dirs_is_probed_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_agent_home: Path
) -> None:
    calls: list[list[str]] = []

    def counting(argv: Sequence[str], **_kwargs: object) -> str | None:
        calls.append(list(argv))
        return OLD

    monkeypatch.setattr(agents, "hook_binary_version", counting)
    old = _fake_aisquare(tmp_path / "old" / "aisquare", prints=f"aisquare {OLD}")
    for name in (".claude", ".claude3"):
        _write_hooks(isolated_agent_home / name, str(old))
        _connect(isolated_agent_home / name)

    sites = agents.hook_sites("claude-code")

    assert [site.binary_state for site in sites] == [agents.HOOK_BINARY_STALE] * 2
    assert calls == [[str(old), "--version"]]


# --- config dirs found on disk, not connected in this home ------------------------------


def test_a_sibling_dir_on_disk_is_discovered_and_labelled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_agent_home: Path
) -> None:
    """A fresh AISQUARE_HOME knows no sites; ``~/.claude3`` with our hooks is graded anyway."""
    current = _this_install(monkeypatch, tmp_path)
    _never_probe(monkeypatch)
    sibling = isolated_agent_home / ".claude3"
    _write_hooks(sibling, str(current))
    # Nothing recorded, and no ~/.claude at all: before #84 this was "not detected".

    check = diagnostics._check_claude_code()

    assert check.status is CheckStatus.ok
    assert f"{sibling} found on disk, not connected in this home" in check.detail


def test_a_stale_sibling_on_disk_gets_its_own_fix_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_agent_home: Path
) -> None:
    _this_install(monkeypatch, tmp_path)
    _real_probe(monkeypatch)
    old = _fake_aisquare(tmp_path / "old" / "aisquare", prints=f"aisquare {OLD}")
    sibling = isolated_agent_home / ".claude3"
    _write_hooks(sibling, str(old))

    check = diagnostics._check_claude_code()

    assert check.status is CheckStatus.warn
    assert f"{sibling} (found on disk, not connected in this home)" in check.detail
    assert check.fix == f"aisquare agents connect claude-code --config-dir {sibling}"


def test_the_readme_account_naming_is_discovered_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_agent_home: Path
) -> None:
    """``~/.claude-account1`` is the README's own example; ``[0-9]`` alone would miss it."""
    current = _this_install(monkeypatch, tmp_path)
    _never_probe(monkeypatch)
    account = isolated_agent_home / ".claude-account1"
    _write_hooks(account, str(current))

    assert [site.config_dir for site in agents.hook_sites("claude-code")] == [account]


def test_claude_config_dir_is_discovered_even_when_never_connected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_agent_home: Path
) -> None:
    _this_install(monkeypatch, tmp_path)
    _real_probe(monkeypatch)
    old = _fake_aisquare(tmp_path / "old" / "aisquare", prints=f"aisquare {OLD}")
    elsewhere = tmp_path / "profiles" / "work"  # not under the home, not named .claude*
    _write_hooks(elsewhere, str(old))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(elsewhere))

    check = diagnostics._check_claude_code()

    assert check.status is CheckStatus.warn
    assert f"{elsewhere} (found on disk, not connected in this home)" in check.detail
    assert check.fix == f"aisquare agents connect claude-code --config-dir {elsewhere}"


def test_a_dir_without_our_hooks_is_not_shown_and_a_file_is_not_a_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_agent_home: Path
) -> None:
    current = _this_install(monkeypatch, tmp_path)
    _never_probe(monkeypatch)
    config = isolated_agent_home / ".claude"
    _write_hooks(config, str(current))
    _connect(config)
    stranger = isolated_agent_home / ".claude7"
    stranger.mkdir()
    (stranger / "settings.json").write_text('{"hooks": {"Stop": []}}', encoding="utf-8")
    (isolated_agent_home / ".claude.json").write_text("{}", encoding="utf-8")  # Claude's own file

    check = diagnostics._check_claude_code()

    assert check.status is CheckStatus.ok
    assert "config dirs" not in check.detail, "one dir, not two"
    assert ".claude7" not in check.detail and ".claude.json" not in check.detail


def test_two_spellings_of_one_dir_count_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_agent_home: Path
) -> None:
    """Recorded as ``~/.claude``, reached as ``$CLAUDE_CONFIG_DIR`` via a symlink: one site."""
    current = _this_install(monkeypatch, tmp_path)
    _never_probe(monkeypatch)
    config = isolated_agent_home / ".claude"
    _write_hooks(config, str(current))
    _connect(config)
    link = tmp_path / "claude-link"
    link.symlink_to(config, target_is_directory=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(link))

    sites = agents.hook_sites("claude-code")

    assert [(site.config_dir, site.recorded) for site in sites] == [(config, True)]


def test_a_recorded_dir_is_graded_even_when_the_ambient_dir_does_not_exist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_agent_home: Path
) -> None:
    """Before: no ``~/.claude`` meant "not detected", whatever the registry knew."""
    _this_install(monkeypatch, tmp_path)
    _real_probe(monkeypatch)
    old = _fake_aisquare(tmp_path / "old" / "aisquare", prints=f"aisquare {OLD}")
    recorded = isolated_agent_home / ".claude4"
    _write_hooks(recorded, str(old))
    _connect(recorded)
    assert not (isolated_agent_home / ".claude").exists()

    check = diagnostics._check_claude_code()

    assert check.status is CheckStatus.warn and str(recorded) in check.detail


def test_nothing_on_disk_is_still_not_detected(isolated_agent_home: Path) -> None:
    check = diagnostics._check_claude_code()

    assert check.status is CheckStatus.ok
    assert check.detail == "Claude Code not detected on this machine"


# --- read-only ---------------------------------------------------------------------------


def test_doctor_never_rewrites_settings_even_when_they_are_stale(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_agent_home: Path
) -> None:
    _this_install(monkeypatch, tmp_path)
    _real_probe(monkeypatch)
    old = _fake_aisquare(tmp_path / "old" / "aisquare", prints=f"aisquare {OLD}")
    recorded = _write_hooks(isolated_agent_home / ".claude", str(old))
    _connect(isolated_agent_home / ".claude")
    discovered = _write_hooks(isolated_agent_home / ".claude3", str(old))
    before = (recorded.read_bytes(), discovered.read_bytes())

    runner.invoke(app, ["doctor"], catch_exceptions=False)
    runner.invoke(app, ["doctor", "--fix", "--yes"], catch_exceptions=False)

    assert (recorded.read_bytes(), discovered.read_bytes()) == before


# --- the pieces --------------------------------------------------------------------------


def test_hook_binary_parses_the_shapes_connect_writes(tmp_path: Path) -> None:
    spaced = tmp_path / "with space" / "aisquare"
    python = Path(sys.executable)

    assert agents.hook_binary(f"{shlex.quote(str(spaced))} hook stop") == agents.HookBinary(spaced)
    assert agents.hook_binary(f"{shlex.quote(sys.executable)} -m aisquare hook stop") == (
        agents.HookBinary(python, module_form=True)
    )
    assert agents.hook_binary("webhook stop") is None
    assert agents.hook_binary("/home/me/bin/my-hook hook stop") is None, "not our program"
    assert agents.HookBinary(spaced).version_argv() == [str(spaced), "--version"]
    assert agents.HookBinary(python, module_form=True).version_argv() == [
        sys.executable,
        "-m",
        "aisquare",
        "--version",
    ]


def test_a_bare_name_resolves_through_path_like_the_hooks_shell_would(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Hooks written before the bare-name fix are still on disk somewhere."""
    found = _fake_aisquare(tmp_path / "bin" / "aisquare")
    monkeypatch.setattr(
        "aisquare.core.agents.shutil.which",
        lambda name: str(found) if name == "aisquare" else None,
    )
    assert agents.hook_binary("aisquare hook stop") == agents.HookBinary(found)

    monkeypatch.setattr("aisquare.core.agents.shutil.which", lambda name: None)
    unfound = agents.hook_binary("aisquare hook stop")
    assert unfound == agents.HookBinary(Path("aisquare"))
    assert agents.classify_hook_binary(unfound) == (agents.HOOK_BINARY_MISSING, None)


def test_the_probe_reads_a_version_out_of_whatever_the_binary_prints(tmp_path: Path) -> None:
    script = _fake_aisquare(tmp_path / "a", prints="aisquare 1.2.3rc4+local")

    assert _REAL_PROBE([str(script), "--version"]) == "1.2.3rc4+local"


def test_the_probe_answers_none_for_every_way_the_question_can_fail(tmp_path: Path) -> None:
    silent = _fake_aisquare(tmp_path / "silent", prints="no digits here")
    failing = _fake_aisquare(tmp_path / "failing", prints="aisquare 9.9.9", exit_code=3)
    slow = tmp_path / "slow"
    slow.write_text("#!/bin/sh\nsleep 5\necho 'aisquare 9.9.9'\n", encoding="utf-8")
    slow.chmod(0o755)

    assert _REAL_PROBE([str(silent), "--version"]) is None, "no version in the output"
    assert _REAL_PROBE([str(failing), "--version"]) is None, "a non-zero exit is not an answer"
    assert _REAL_PROBE([str(tmp_path / "missing"), "--version"]) is None, "will not start"
    assert _REAL_PROBE([str(slow), "--version"], timeout=0.2) is None, "hung past the timeout"


def test_the_probe_reads_this_very_install() -> None:
    """The one real spawn: the parse holds against the CLI's actual ``--version`` line."""
    assert _REAL_PROBE([sys.executable, "-m", "aisquare", "--version"]) == __version__


def test_the_suite_stub_answers_as_this_install() -> None:
    """conftest's premise, asserted: under the suite every probe reports this version."""
    assert agents.hook_binary_version(["/nowhere/aisquare", "--version"]) == __version__
