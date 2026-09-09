"""The ui-tester: a first-class role that verifies user-facing work in a real browser.

Why a role and not a line in the runner's briefing: frontend tickets are most of
what PlatformQA produces, "opened the page and measured" is a different skill and
a different toolset from "ran the suite", and a name lets the planner route to
it, the manager spawn it only when UI is involved, and the runner stay the plain
code-and-tests checker.

Why the role OWNS ``--chrome``: the operator who set this up passed ``--chrome``
in a personal alias. The next operator will not. A tool the role needs is the
role's business, so ``RoleProfile.default_args`` carries it and ``launch`` adds it
wherever the role starts — a hand-started window, ``team spawn``, a fleet window —
only for the default binary, never twice, and never over an explicit
``--no-chrome``.

What the CLI can and cannot know about the browser: MCP servers and plugins are
declared in files on disk, so ``doctor`` reads them. The Claude in Chrome
extension lives in the browser and is invisible from a terminal; the row says so
instead of guessing, and the briefing tells the role to check what answers before
it starts and to reopen — never pass — a UI task it could not open in a browser.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli import launch as launch_cli
from aisquare.cli.app import app
from aisquare.cli.ui.sidebar import ROLE_ICON
from aisquare.core import agents as agent_core
from aisquare.core import harness
from aisquare.core.config import (
    AppConfig,
    ExplainabilitySettings,
    ExplainabilityTarget,
    _default_fleet_roles,
    save_config,
)
from aisquare.models import CheckStatus
from aisquare.services import diagnostics
from aisquare.services import explainability as explainability_service
from aisquare.services import explainability_ops as ops
from aisquare.services.fleet import FLEET_ROLES

ROLE = "ui-tester"


# ── the role exists everywhere a role must ────────────────────────────────────


def test_the_role_is_wired_into_every_list_that_enumerates_roles() -> None:
    assert ROLE in launch_cli.ROLES, "launchable by name"
    assert ROLE in harness.ROLE_PROFILES, "has a model ladder"
    assert ROLE in FLEET_ROLES, "the manager can spawn it"
    assert ROLE in _default_fleet_roles(), "the fleet has a launch shape for it"
    assert ROLE in ExplainabilitySettings().roles, "its identity is registered by default"
    assert ROLE in ROLE_ICON, "the fleet UI can draw it"
    assert ROLE in harness._LANE, "the lane rule knows what it does instead"


def test_the_ladder_is_the_verifiers_ladder() -> None:
    profile = harness.ROLE_PROFILES[ROLE]
    assert profile.ladder == ["sonnet", "opus"], "same as runner and reviewer"
    assert profile.effort_offset == 0
    assert profile.default_args == ["--chrome"]


def test_a_seat_is_briefed_as_the_role() -> None:
    assert harness.base_role("ui-tester3") == ROLE
    text = " ".join(harness.role_cycle("ui-tester3", "abcd1234"))
    assert text.startswith("Your standing cycle (ui-tester)")
    assert "Stay in your lane (ui-tester)" in text


# ── the briefing ──────────────────────────────────────────────────────────────


def test_the_briefing_verifies_in_a_real_browser_and_measures() -> None:
    text = " ".join(harness.role_cycle(ROLE, "abcd1234"))
    assert "task next --status review" in text
    assert '"UI: …"' in text, "it takes the tasks the planner marked for it"
    assert "REAL browser" in text
    for tool in ("Claude in Chrome", "Chrome DevTools MCP", "Playwright MCP"):
        assert tool in text, tool
    assert "BEFORE you start" in text, "it checks which tools answer first"
    assert "MEASURE" in text and "screenshots" in text
    assert "never pass a visual requirement by reading code" in text


def test_the_briefing_degrades_honestly_without_a_browser() -> None:
    text = " ".join(harness.role_cycle(ROLE, "abcd1234"))
    assert "No browser tool answers?" in text
    assert "UI not browser-verified in this window" in text
    assert "never done" in text, "a UI task without a browser is reopened, not passed"


def test_the_briefing_is_read_only_and_pre_fills_the_session_id() -> None:
    text = " ".join(harness.role_cycle(ROLE, "abcd1234"))
    assert "never edit" in text and "never push" in text
    assert "--as abcd1234" in text


def test_the_other_roles_know_the_ui_tester_exists() -> None:
    planner = " ".join(harness.role_cycle("planner", "abcd1234"))
    assert '"UI: …"' in planner and "browser steps" in planner, "the planner marks UI work"
    runner = " ".join(harness.role_cycle("runner", "abcd1234"))
    assert "belongs to the ui-tester" in runner
    assert "not browser-verified" in runner, "a runner alone says what it did not check"
    manager = " ".join(harness.role_cycle("manager", "abcd1234"))
    assert "fleet spawn ui-tester" in manager
    reviewer = " ".join(harness.role_cycle("reviewer", "abcd1234"))
    assert "no ui-tester evidence" in reviewer and "request-changes" in reviewer


# ── --chrome is the role's, applied exactly once, opt-out wins ───────────────


def test_default_args_apply_to_the_default_binary_only() -> None:
    assert harness.role_default_args(ROLE, binary="claude", args=[]) == ["--chrome"]
    assert harness.role_default_args(ROLE, binary="/usr/local/bin/claude", args=[]) == ["--chrome"]
    assert harness.role_default_args(ROLE, binary="claude-next", args=[]) == []
    assert harness.role_default_args(ROLE, binary="/opt/wrap/agent", args=[]) == []


def test_default_args_never_duplicate_and_never_override_an_opt_out() -> None:
    assert harness.role_default_args(ROLE, binary="claude", args=["--chrome"]) == []
    assert harness.role_default_args(ROLE, binary="claude", args=["--no-chrome"]) == []
    assert harness.role_default_args(ROLE, binary="claude", args=["--resume"]) == ["--chrome"]


def test_roles_without_defaults_get_nothing() -> None:
    for role in ("coder", "planner", "runner", "reviewer", "validator", "manager", "tester"):
        assert harness.role_default_args(role, binary="claude", args=[]) == [], role
    assert harness.role_default_args("stenographer", binary="claude", args=[]) == []


@pytest.fixture
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake_exec(binary: str, argv: list[str], env: dict[str, str]) -> None:
        captured.update(binary=binary, argv=argv, env=env)

    monkeypatch.setattr(launch_cli, "_exec", fake_exec)
    monkeypatch.setattr(shutil, "which", lambda cmd: f"/usr/local/bin/{cmd}")
    return captured


def test_launch_adds_chrome_for_the_ui_tester_and_for_nobody_else(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any]
) -> None:
    assert runner.invoke(app, ["launch", ROLE]).exit_code == 0
    assert spy["argv"] == ["claude", "--chrome"]
    assert runner.invoke(app, ["launch", "coder"]).exit_code == 0
    assert spy["argv"] == ["claude"]


def test_launch_keeps_the_operators_word_on_chrome(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any]
) -> None:
    assert runner.invoke(app, ["launch", ROLE, "--no-chrome"]).exit_code == 0
    assert spy["argv"] == ["claude", "--no-chrome"], "an opt-out is never overridden"
    assert runner.invoke(app, ["launch", ROLE, "--chrome", "--resume"]).exit_code == 0
    assert spy["argv"] == ["claude", "--chrome", "--resume"], "never twice"


def test_launch_adds_nothing_to_another_agent_binary(
    runner: CliRunner, work_dir: Path, spy: dict[str, Any]
) -> None:
    assert runner.invoke(app, ["launch", ROLE, "--command", "claude-next"]).exit_code == 0
    assert spy["argv"] == ["claude-next"], "Claude Code's flag is not another agent's"


def test_spawn_prints_the_role_flag_in_the_pasteable_command(
    runner: CliRunner, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`team spawn` prints a command meant to be pasted; a printed command that
    silently launches with different flags than the fleet's window would is the
    failure its own comment warns about."""
    monkeypatch.setenv("AISQUARE_HARNESS_PROBE", "0")
    result = runner.invoke(app, ["team", "spawn", ROLE])
    assert result.exit_code == 0, result.output
    assert "--chrome" in result.output
    assert "--model sonnet" in result.output


# ── doctor: what the machine declares, and what it cannot see ─────────────────


def _sites(*dirs: Path) -> list[Any]:
    return [SimpleNamespace(config_dir=d) for d in dirs]


def test_doctor_reads_declared_browser_tools_from_every_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = tmp_path / "claude-a", tmp_path / "claude-b"
    a.mkdir()
    b.mkdir()
    (a / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"chrome-devtools-mcp@claude-plugins-official": True}})
    )
    (b / ".claude.json").write_text(
        json.dumps({"projects": {"/x": {"mcpServers": {"playwright": {"command": "npx"}}}}})
    )
    monkeypatch.setattr(agent_core, "hook_sites", lambda name: _sites(a, b))
    check = diagnostics._check_browser_tools(tmp_path)
    assert check.status is CheckStatus.ok
    assert "plugin chrome-devtools-mcp" in check.detail
    assert "mcp playwright" in check.detail
    assert "Claude in Chrome cannot be detected" in check.detail, (
        "honest about the one it cannot see"
    )


def test_doctor_reads_the_projects_own_mcp_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "claude"
    home.mkdir()
    project = tmp_path / "repo"
    project.mkdir()
    (project / ".mcp.json").write_text(json.dumps({"mcpServers": {"browser-use": {}}}))
    monkeypatch.setattr(agent_core, "hook_sites", lambda name: _sites(home))
    check = diagnostics._check_browser_tools(project)
    assert check.status is CheckStatus.ok
    assert "mcp browser-use" in check.detail


def test_doctor_is_amber_not_red_when_nothing_is_declared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "claude"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps({"enabledPlugins": {"something-else": True}}))
    monkeypatch.setattr(agent_core, "hook_sites", lambda name: _sites(home))
    check = diagnostics._check_browser_tools(tmp_path)
    assert check.status is CheckStatus.warn, "the role still runs; it degrades, so amber"
    assert "not browser-verified" in check.detail
    assert check.fix and "Chrome DevTools MCP" in check.fix


def test_doctor_survives_unreadable_or_malformed_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "claude"
    home.mkdir()
    (home / "settings.json").write_text("{not json")
    (home / ".claude.json").write_text("[]")
    monkeypatch.setattr(agent_core, "hook_sites", lambda name: _sites(home))
    check = diagnostics._check_browser_tools(tmp_path)
    assert check.status is CheckStatus.warn


def test_doctor_lists_the_row(runner: CliRunner, work_dir: Path) -> None:
    result = runner.invoke(app, ["--json", "doctor"])
    assert result.exit_code in (0, 1), result.output
    names = [c["name"] for c in json.loads(result.output)]
    assert "browser tools" in names


# ── register: an upgraded machine is told which launchable role its roster lacks ──


def test_register_names_first_class_roles_missing_from_the_configured_roster(
    runner: CliRunner, work_dir: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = AppConfig()
    config.explainability.enabled = True
    config.explainability.target = "prod"
    config.explainability.roles = ["planner", "coder", "runner"]  # a pre-ui-tester config.toml
    config.explainability.targets = {
        "prod": ExplainabilityTarget(gateway_url="https://gateway.example")
    }
    save_config(config)
    explainability_service.store_api_key("wk-test")
    monkeypatch.setattr(
        ops,
        "register_roster",
        lambda target, names: ops.HttpVerdict(
            ok=True, status=200, detail="HTTP 200", payload={"agents": []}
        ),
    )
    result = runner.invoke(app, ["explainability", "register"])
    assert result.exit_code == 0, result.output
    assert "not in explainability.roles" in result.output
    assert "ui-tester" in result.output
    assert "--role ui-tester" in result.output
