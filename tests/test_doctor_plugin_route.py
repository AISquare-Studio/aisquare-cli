"""Doctor and ``agents`` know the Claude Code plugin route.

THE DEFECT THIS PINS. On a machine that installed only the aisquare plugin
(``/plugin install aisquare@aisquare-cli``), the claude-code row read "hooks are missing
or outdated" and offered ``aisquare agents connect claude-code``. That hint is a
one-click button in the UI, and pressing it installs the settings.json hooks beside the
plugin's. Measured in the 9.3 investigation, before the launcher learned to stand down:
the context injected twice, and 2 prompt rows and 2 metric rows per prompt.

What each state reads now, per config dir:

- plugin only: connected, naming the plugin and the version it was installed at;
- settings.json hooks and the plugin: a warning. The plugin's hooks stand down, so
  nothing doubles, but two routes drift apart, and choosing one is the operator's
  call, so that fix is never a button;
- neither, or a plugin that is disabled or not installed: unchanged, with the
  one-click ``agents connect``.

The plugin's records are what Claude Code 2.1.292 writes: ``enabledPlugins`` in
settings.json, and ``plugins/installed_plugins.json`` (``{"version": 2, "plugins":
{id: [records]}}``). Reading them creates no aisquare state.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import agents as agent_core
from aisquare.core import paths
from aisquare.models import CheckStatus, DoctorCheck
from aisquare.services import agents as agents_service
from aisquare.services import diagnostics
from aisquare.services.onboarding import fix_commands

_CONNECT = ("agents", "connect", "claude-code")


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


@pytest.fixture
def claude(isolated_agent_home: Path) -> Path:
    """Claude Code installed in ~/.claude, with nothing of aisquare's in it yet."""
    directory = isolated_agent_home / ".claude"
    directory.mkdir(parents=True)
    return directory


def _install_plugin(
    config_dir: Path, *, enabled: bool = True, recorded: bool = True, version: str | None = "0.9.0"
) -> None:
    """What ``/plugin install aisquare@aisquare-cli`` leaves in a config dir."""
    config_dir.mkdir(parents=True, exist_ok=True)
    settings_path = config_dir / "settings.json"
    settings = json.loads(settings_path.read_text("utf-8")) if settings_path.exists() else {}
    settings.setdefault("enabledPlugins", {})[agent_core.CLAUDE_PLUGIN_ID] = enabled
    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    if not recorded:
        return
    record: dict[str, str] = {
        "scope": "user",
        "installPath": str(config_dir / "plugins" / "cache" / "aisquare-cli" / "aisquare"),
        "installedAt": "2026-10-07T14:10:02.801Z",
    }
    if version is not None:
        record["version"] = version
    installed = {"version": 2, "plugins": {agent_core.CLAUDE_PLUGIN_ID: [record]}}
    (config_dir / "plugins").mkdir(exist_ok=True)
    (config_dir / "plugins" / "installed_plugins.json").write_text(
        json.dumps(installed), encoding="utf-8"
    )


def _connect(runner: CliRunner, config_dir: Path | None = None) -> None:
    argv = [*_CONNECT] + ([] if config_dir is None else ["--config-dir", str(config_dir)])
    result = runner.invoke(app, argv)
    assert result.exit_code == 0, result.output


def _buttons(check: DoctorCheck) -> list[tuple[str, ...]]:
    return [fix.argv[:3] for fix in fix_commands([check])]


def test_a_plugin_only_install_reads_connected(claude: Path) -> None:
    _install_plugin(claude)

    check = diagnostics._check_claude_code()

    assert check.status is CheckStatus.ok, check
    assert "connected (through the aisquare plugin 0.9.0)" in check.detail
    assert check.fix is None and _buttons(check) == []
    assert agents_service.claude_code_connected() is True


def test_neither_route_still_offers_the_one_click_connect(claude: Path) -> None:
    """The control for the test above: the same directory with no plugin."""
    check = diagnostics._check_claude_code()

    assert check.status is CheckStatus.warn
    assert _buttons(check) == [_CONNECT]
    assert agents_service.claude_code_connected() is False


def test_both_routes_warn_and_name_both_ways_out_without_a_button(
    runner: CliRunner, claude: Path
) -> None:
    _connect(runner)
    _install_plugin(claude)

    check = diagnostics._check_claude_code()

    assert check.status is CheckStatus.warn
    assert f"runs aisquare two ways in: {claude}" in check.detail
    assert f"aisquare agents disconnect claude-code --config-dir {claude}" in (check.fix or "")
    assert f"/plugin uninstall {agent_core.CLAUDE_PLUGIN_ID}" in (check.fix or "")
    assert _buttons(check) == [], "keeping one route is a choice, not a one-click fix"
    assert agents_service.claude_code_connected() is True


def test_a_partial_install_beside_the_plugin_is_two_routes_too(
    runner: CliRunner, claude: Path
) -> None:
    """An older install's hooks still run on the events they have; the plugin covers the rest."""
    _connect(runner)
    settings_path = claude / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings["hooks"].pop("StopFailure")
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    _install_plugin(claude)

    check = diagnostics._check_claude_code()

    assert check.status is CheckStatus.warn
    assert "runs aisquare two ways" in check.detail
    assert "missing or outdated" not in check.detail, "the plugin runs StopFailure"


@pytest.mark.parametrize(
    "how",
    [{"enabled": False}, {"recorded": False}],
    ids=["disabled", "enabled-but-not-installed"],
)
def test_a_plugin_that_does_not_run_is_not_a_route(claude: Path, how: dict[str, bool]) -> None:
    """``/plugin disable`` writes false; an enabled key with nothing installed runs nothing."""
    _install_plugin(claude, **how)

    check = diagnostics._check_claude_code()

    assert check.status is CheckStatus.warn
    assert _buttons(check) == [_CONNECT]
    assert agents_service.claude_code_connected() is False


@pytest.mark.parametrize(
    ("relative", "text"),
    [
        ("settings.json", "{not json"),
        ("settings.json", '{"enabledPlugins": ["aisquare@aisquare-cli"]}'),
        ("plugins/installed_plugins.json", "{not json"),
        ("plugins/installed_plugins.json", '{"version": 2, "plugins": ["aisquare@aisquare-cli"]}'),
        ("plugins/installed_plugins.json", '{"version": 2, "plugins": {"aisquare@aisquare-cli": 7}}'),
    ],
    ids=["settings-invalid", "settings-list", "installed-invalid", "installed-list", "record-int"],
)
def test_unreadable_plugin_records_count_as_absent(claude: Path, relative: str, text: str) -> None:
    _install_plugin(claude)
    (claude / relative).write_text(text, encoding="utf-8")

    check = diagnostics._check_claude_code()

    assert check.status is CheckStatus.warn
    assert agent_core.claude_plugin(claude) is None
    assert agents_service.claude_code_connected() is False


def test_a_record_without_a_version_still_counts(claude: Path) -> None:
    _install_plugin(claude, version=None)

    check = diagnostics._check_claude_code()

    assert check.status is CheckStatus.ok
    assert "connected (through the aisquare plugin)" in check.detail


def test_a_plugin_dir_found_on_disk_is_graded_with_the_rest(
    runner: CliRunner, claude: Path, isolated_agent_home: Path
) -> None:
    """A plugin runs whether or not this home ever heard of the directory (#84)."""
    second = isolated_agent_home / ".claude-c2"
    _connect(runner)
    _install_plugin(second)

    check = diagnostics._check_claude_code()

    assert check.status is CheckStatus.ok, check
    assert "connected in 2 config dirs" in check.detail
    assert f"through the aisquare plugin 0.9.0 in {second}" in check.detail
    assert agents_service.claude_code_connected(second) is True
    assert agents_service.claude_code_connected(claude) is True


def test_reading_the_plugin_creates_no_aisquare_state(claude: Path) -> None:
    _install_plugin(claude)
    home = paths.aisquare_home()
    assert not home.exists(), "the premise: nothing set up yet"

    rows = {check.name: check for check in diagnostics.doctor()}

    assert rows["claude-code"].status is CheckStatus.ok
    assert not home.exists(), "doctor created the aisquare home"


def test_disconnect_says_the_plugin_keeps_aisquare_running(
    runner: CliRunner, claude: Path
) -> None:
    _connect(runner)
    _install_plugin(claude)

    result = runner.invoke(app, ["agents", "disconnect", "claude-code"])

    assert result.exit_code == 0, result.output
    assert f"/plugin disable {agent_core.CLAUDE_PLUGIN_ID}" in result.stderr
    assert "no aisquare hooks found" not in result.stderr


def test_disconnect_without_the_plugin_says_nothing_about_it(
    runner: CliRunner, claude: Path
) -> None:
    _connect(runner)

    result = runner.invoke(app, ["agents", "disconnect", "claude-code"])

    assert result.exit_code == 0, result.output
    assert "/plugin" not in result.stderr


def test_connect_beside_the_plugin_says_its_hooks_stand_down(
    runner: CliRunner, claude: Path
) -> None:
    _install_plugin(claude)

    result = runner.invoke(app, ["--json", *_CONNECT])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["hooks_installed"] is True, "stdout stays one JSON object"
    assert "stand down" in result.stderr
