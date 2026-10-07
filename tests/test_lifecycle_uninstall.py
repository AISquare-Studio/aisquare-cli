"""``aisquare uninstall``: what it finds, what it refuses, what it removes — and in what order.

The destructive steps are asserted on the FILES, never on return values: a
snapshot of every file under the test's tree before and after, so "the dry run
changed nothing" is a statement about the disk. Every dangerous path the purge
guard refuses ($HOME, /, an ancestor of $HOME) is checked on the guard function
itself, never by pointing a real ``uninstall --purge`` at it.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import agents as agent_core
from aisquare.core import claude_accounts as accounts_core
from aisquare.core import paths
from aisquare.core.orchestrator import team_project
from aisquare.core.store import store_session
from aisquare.models import FleetAgent
from aisquare.services import install_route, lifecycle
from aisquare.services.install_route import Facts
from tests.fsperms import can_deny_reads, can_symlink
from tests.installer_seams import no_real_installer  # noqa: F401 — autouse, applied by import

_EVENTS = (
    ("SessionStart", "session-start"),
    ("UserPromptSubmit", "user-prompt-submit"),
    ("SessionEnd", "session-end"),
    ("Stop", "stop"),
    ("Notification", "notification"),
    ("StopFailure", "stop-failure"),
)

FOREIGN = {"hooks": [{"type": "command", "command": "webhook stop"}]}


def _hooked(directory: Path, program: Path | str, *, foreign: bool = True) -> Path:
    """A Claude Code directory with aisquare's six hook groups — and, by default, one
    hook of the user's own that must survive."""
    directory.mkdir(parents=True, exist_ok=True)
    hooks: dict[str, list[dict[str, Any]]] = {
        event: [{"hooks": [{"type": "command", "command": f"{program} hook {verb}"}]}]
        for event, verb in _EVENTS
    }
    if foreign:
        hooks["Stop"].insert(0, FOREIGN)
    settings = {"theme": "dark", "hooks": hooks}
    (directory / "settings.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return directory


def _settings(directory: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((directory / "settings.json").read_text("utf-8"))
    return loaded


def _record(*directories: Path) -> None:
    paths.ensure_home()
    paths.agents_registry_path().write_text(
        json.dumps(
            {
                "connected": ["claude-code"],
                "connections": {"claude-code": [str(directory) for directory in directories]},
            }
        ),
        encoding="utf-8",
    )


def _snapshot(root: Path) -> dict[str, bytes]:
    """Every file, directory and link under ``root``, by path — what "unchanged" means."""
    found: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            found[str(path)] = b"-> " + os.readlink(path).encode()
        elif path.is_file():
            found[str(path)] = path.read_bytes()
        elif path.is_dir():
            found[str(path) + os.sep] = b""
    return found


def _one_object(stdout: str) -> dict[str, Any]:
    lines = [line for line in stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f"--json must print exactly one object, got: {stdout!r}"
    parsed = json.loads(lines[0])
    assert isinstance(parsed, dict)
    return parsed


@dataclass
class World:
    """Records what reached the outside world, in order."""

    events: list[tuple[str, str]] = field(default_factory=list)
    execs: list[tuple[list[str], dict[str, str], bool]] = field(default_factory=list)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    recorded = World()
    real_remove = agent_core.remove_hooks

    def remove_hooks(name: str, config_dir: Path | None = None) -> bool:
        recorded.events.append(("hooks", str(config_dir)))
        return real_remove(name, config_dir)

    def exec_replace(argv: Any, *, env: Any, stdout_to_stderr: bool) -> None:
        recorded.events.append(("package", " ".join(argv)))
        recorded.execs.append((list(argv), dict(env), stdout_to_stderr))

    monkeypatch.setattr(agent_core, "remove_hooks", remove_hooks)
    monkeypatch.setattr(install_route, "exec_replace", exec_replace)
    monkeypatch.setattr(lifecycle, "_tmux_on_path", lambda: True)
    return recorded


@dataclass
class Tool:
    prefix: Path
    script: Path


@pytest.fixture
def tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Tool:
    """A uv tool install, the way the installer leaves one, as the RUNNING install."""
    prefix = tmp_path / "uv" / "tools" / "aisquare-cli"
    (prefix / "bin").mkdir(parents=True)
    (prefix / install_route.RECEIPT_NAME).write_text(
        '[tool]\nrequirements = [{ name = "aisquare-cli" }, { name = "tiktoken" }]\n'
        'python = "3.13"\n',
        encoding="utf-8",
    )
    script = prefix / "bin" / "aisquare"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    found = Facts(
        prefix=prefix,
        base_prefix=tmp_path / "base",
        executable=prefix / "bin" / "python",
        platform="linux",
        python_version="3.13",
    )
    monkeypatch.setattr(install_route, "facts", lambda: found)
    monkeypatch.setattr(install_route, "find_uv", lambda: "/usr/bin/uv")
    return Tool(prefix, script)


@pytest.fixture
def user_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The user's home as the purge guard sees it — a temp directory, never the real one."""
    home = tmp_path / "home" / "user"
    home.mkdir(parents=True)
    monkeypatch.setattr(lifecycle, "_user_home", lambda: home)
    return home


def _initialised(runner: CliRunner, tmp_path: Path) -> None:
    """A home the CLI made itself: config.toml, context.db, a registered project."""
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    result = runner.invoke(app, ["init", str(project), "--yes", "--no-onboard"])
    assert result.exit_code == 0, result.output


# --- what the plan finds ---------------------------------------------------------------


def test_the_plan_finds_every_kind_of_hook_site(
    tool: Tool,
    world: World,
    isolated_agent_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """~/.claude, a sibling ~/.claude-c2, $CLAUDE_CONFIG_DIR elsewhere, a recorded
    directory, and a managed account slot inside the home."""
    default = _hooked(isolated_agent_home / ".claude", tool.script)
    sibling = _hooked(isolated_agent_home / ".claude-c2", tool.script)
    ambient = _hooked(tmp_path / "elsewhere" / "claude-config", tool.script)
    recorded = _hooked(tmp_path / "recorded" / "claude", tool.script)
    _record(recorded)
    slot = _hooked(paths.claude_accounts_dir() / "2", tool.script)
    (slot / accounts_core.MARKER).write_text('{"slot": 2}', encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(ambient))

    plan = lifecycle.uninstall_plan()

    found = {site.config_dir for site in plan.hooks}
    assert found == {default, sibling, ambient, recorded, slot}
    assert all(site.programs == (str(tool.script),) for site in plan.hooks)


def test_a_directory_with_only_the_users_own_hooks_is_not_in_the_plan(
    tool: Tool, world: World, isolated_agent_home: Path
) -> None:
    """Negative control: `webhook stop` is not ours, so its directory is not touched."""
    theirs = isolated_agent_home / ".claude"
    theirs.mkdir(parents=True)
    (theirs / "settings.json").write_text(
        json.dumps({"hooks": {"Stop": [FOREIGN]}}), encoding="utf-8"
    )

    plan = lifecycle.uninstall_plan()

    assert plan.hooks == ()


def test_the_plan_names_the_logins_a_purge_would_delete(
    tool: Tool, world: World, runner: CliRunner, tmp_path: Path
) -> None:
    _initialised(runner, tmp_path)
    slot = paths.claude_accounts_dir() / "2"
    slot.mkdir(parents=True)
    (slot / accounts_core.MARKER).write_text('{"slot": 2}', encoding="utf-8")
    (slot / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": "dev@example.com"}}), encoding="utf-8"
    )

    plan = lifecycle.uninstall_plan(purge=True)

    assert plan.accounts == ("slot 2: dev@example.com",)
    assert plan.home_exists and paths.db_path().name in plan.home_entries


def test_mcp_servers_that_run_aisquare_are_listed_and_never_edited(
    tool: Tool, world: World, runner: CliRunner, isolated_agent_home: Path
) -> None:
    default = _hooked(isolated_agent_home / ".claude", tool.script)
    beside = isolated_agent_home / ".claude.json"
    beside.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "aisquare": {"command": "aisquare", "args": ["serve", "--stdio"]},
                    "playwright": {"command": "npx", "args": ["@playwright/mcp@latest"]},
                },
                "projects": {
                    "/work/repo": {
                        "mcpServers": {
                            "memory": {"command": "/usr/bin/python3", "args": ["-m", "aisquare"]}
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    before = beside.read_bytes()

    plan = lifecycle.uninstall_plan()
    result = runner.invoke(app, ["uninstall", "--yes"])

    assert {(entry.name, entry.project) for entry in plan.mcp} == {
        ("aisquare", None),
        ("memory", "/work/repo"),
    }, "playwright does not run aisquare"
    assert result.exit_code == 0, result.output
    assert "claude mcp remove <name>" in result.stdout
    assert beside.read_bytes() == before, ".claude.json is Claude Code's; uninstall only reads it"
    assert agent_core.hook_commands("claude-code", default) == []


# --- nothing changes until it is asked to ----------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [["uninstall", "--dry-run"], ["--json", "uninstall"], ["uninstall"], ["uninstall", "--purge"]],
    ids=["dry-run", "json-without-yes", "off-a-terminal", "purge-off-a-terminal"],
)
def test_the_plan_alone_leaves_every_file_byte_identical(
    tool: Tool,
    world: World,
    runner: CliRunner,
    isolated_agent_home: Path,
    tmp_path: Path,
    argv: list[str],
) -> None:
    _initialised(runner, tmp_path)
    _hooked(isolated_agent_home / ".claude", tool.script)
    before = _snapshot(tmp_path)

    result = runner.invoke(app, argv)

    assert result.exit_code == 0, result.output
    assert _snapshot(tmp_path) == before, "a plan must not change a single file"
    assert world.events == [], "nothing was removed and no package step ran"


@pytest.mark.parametrize(
    "argv", [["uninstall", "--dry-run"], ["--json", "uninstall"]], ids=["dry-run", "json"]
)
def test_the_plan_modes_never_ask_even_at_a_terminal(
    tool: Tool,
    world: World,
    runner: CliRunner,
    isolated_agent_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
) -> None:
    """Off a terminal every path without --yes ends as a plan, which would hide a
    plan mode that fell through. At a terminal, with every question answered yes,
    only the plan modes themselves keep the disk as it was."""
    _hooked(isolated_agent_home / ".claude", tool.script)
    monkeypatch.setattr("aisquare.cli.install._stdin_is_a_terminal", lambda: True)
    asked: list[str] = []

    def confirm(text: str, **_: object) -> bool:
        asked.append(text)
        return True

    monkeypatch.setattr("aisquare.cli.install.typer.confirm", confirm)
    before = _snapshot(tmp_path)

    result = runner.invoke(app, argv)

    assert result.exit_code == 0, result.output
    assert asked == [], "a plan mode must not ask — a yes would remove things"
    assert _snapshot(tmp_path) == before and world.events == []


def test_json_without_yes_is_one_plan_object(
    tool: Tool, world: World, runner: CliRunner, isolated_agent_home: Path
) -> None:
    site = _hooked(isolated_agent_home / ".claude", tool.script)

    result = runner.invoke(app, ["--json", "uninstall", "--purge"])

    assert result.exit_code == 0, result.output
    plan = _one_object(result.stdout)
    assert plan["dry_run"] is True
    assert plan["hooks"] == [{"config_dir": str(site), "programs": [str(tool.script)]}]
    assert plan["package"]["command"] == "uv tool uninstall aisquare-cli"
    assert plan["package"]["runs"] is True
    assert plan["home"]["action"] == "delete"


def test_at_a_terminal_it_asks_and_no_means_nothing_is_removed(
    tool: Tool,
    world: World,
    runner: CliRunner,
    isolated_agent_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _hooked(isolated_agent_home / ".claude", tool.script)
    monkeypatch.setattr("aisquare.cli.install._stdin_is_a_terminal", lambda: True)
    asked: list[tuple[str, bool]] = []

    def confirm(text: str, *, default: bool = True, **_: object) -> bool:
        asked.append((text, default))
        return False

    monkeypatch.setattr("aisquare.cli.install.typer.confirm", confirm)
    before = _snapshot(tmp_path)

    declined = runner.invoke(app, ["uninstall"])

    assert declined.exit_code == 0, declined.output
    assert asked == [("Remove aisquare's hooks from 1 directory and remove the package?", False)]
    assert "nothing removed" in declined.stdout
    assert _snapshot(tmp_path) == before and world.events == []

    monkeypatch.setattr("aisquare.cli.install.typer.confirm", lambda *_a, **_k: True)
    agreed = runner.invoke(app, ["uninstall"])

    assert agreed.exit_code == 0, agreed.output
    assert world.events[-1][0] == "package"


# --- the run ---------------------------------------------------------------------------


def test_yes_removes_every_aisquare_hook_keeps_the_users_and_removes_the_package_last(
    tool: Tool,
    world: World,
    runner: CliRunner,
    isolated_agent_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default = _hooked(isolated_agent_home / ".claude", tool.script)
    ambient = _hooked(tmp_path / "elsewhere" / "claude-config", tool.script)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(ambient))
    _record(default, ambient)
    foreign_only = isolated_agent_home / ".claude-mine"
    foreign_only.mkdir()
    (foreign_only / "settings.json").write_text('{"hooks": {"Stop": [ ]}}\n', encoding="utf-8")
    untouched = (foreign_only / "settings.json").read_bytes()

    result = runner.invoke(app, ["uninstall", "--yes"])

    assert result.exit_code == 0, result.output
    for site in (default, ambient):
        assert agent_core.hook_commands("claude-code", site) == []
        kept = _settings(site)
        assert kept["theme"] == "dark", "settings outside the hooks stay"
        assert kept["hooks"] == {"Stop": [FOREIGN]}, "the user's own hook stays exactly"
    assert (foreign_only / "settings.json").read_bytes() == untouched, "no hooks of ours, no write"
    kinds = [kind for kind, _ in world.events]
    assert kinds == ["hooks", "hooks", "package"], "the package goes LAST, after every hook site"
    [(argv, env, to_stderr)] = world.execs
    assert argv == ["uv", "tool", "uninstall", "aisquare-cli"]
    assert env["UV_TOOL_DIR"] == str(tool.prefix.parent), "uv removes THIS tool, not another"
    assert to_stderr is False
    assert paths.aisquare_home().is_dir(), "the home is kept without --purge"
    assert agent_core.connected_dirs("claude-code") == [], "the kept home connects nothing"


def test_uninstalling_with_no_home_does_not_create_one(
    tool: Tool, world: World, runner: CliRunner, isolated_agent_home: Path, isolated_home: Path
) -> None:
    site = _hooked(isolated_agent_home / ".claude", tool.script)
    assert not isolated_home.exists()

    result = runner.invoke(app, ["uninstall", "--yes"])

    assert result.exit_code == 0, result.output
    assert agent_core.hook_commands("claude-code", site) == []
    assert not isolated_home.exists(), (
        "uninstall created the aisquare home — something went through set_connected/"
        "ensure_home (agents_service.disconnect does)"
    )
    assert world.events[-1][0] == "package"


def test_under_json_the_report_is_one_object_and_uv_is_pointed_at_stderr(
    tool: Tool, world: World, runner: CliRunner, isolated_agent_home: Path
) -> None:
    site = _hooked(isolated_agent_home / ".claude", tool.script)

    result = runner.invoke(app, ["--json", "uninstall", "--yes"])

    assert result.exit_code == 0, result.output
    report = _one_object(result.stdout)
    assert report["dry_run"] is False
    assert report["hooks"] == [{"config_dir": str(site), "removed": True, "error": None}]
    assert report["package"]["runs"] is True
    assert world.execs[0][2] is True, "uv's stdout must not follow the report onto stdout"


def test_a_route_aisquare_does_not_remove_is_told_its_command(
    world: World, runner: CliRunner, isolated_agent_home: Path, tmp_path: Path
) -> None:
    """The suite's own route is editable — the real detection: the hooks go, the
    package step is printed, and nothing is exec'd."""
    site = _hooked(isolated_agent_home / ".claude", tmp_path / "checkout" / "aisquare")

    result = runner.invoke(app, ["uninstall", "--yes"])

    assert result.exit_code == 0, result.output
    assert agent_core.hook_commands("claude-code", site) == []
    assert "to finish, remove the package:" in result.stdout
    assert " uninstall " in result.stdout and "aisquare-cli" in result.stdout
    assert world.execs == []


def test_windows_never_removes_its_own_package(
    tool: Tool,
    world: World,
    runner: CliRunner,
    isolated_agent_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    windows = Facts(
        prefix=tool.prefix,
        base_prefix=tool.prefix.parent,
        executable=tool.prefix / "bin" / "python",
        platform="win32",
        python_version="3.13",
    )
    monkeypatch.setattr(install_route, "facts", lambda: windows)
    _hooked(isolated_agent_home / ".claude", tool.script)

    result = runner.invoke(app, ["uninstall", "--yes"])

    assert result.exit_code == 0, result.output
    assert world.execs == []
    assert "to finish, remove the package: uv tool uninstall aisquare-cli" in result.stdout


def test_a_site_that_cannot_be_cleaned_keeps_the_package_and_the_home(
    tool: Tool,
    world: World,
    runner: CliRunner,
    isolated_agent_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail open per directory — and a hook left anywhere means the package stays, so a
    session never fires a hook at a program that is gone, and the command is still
    there to run again."""
    _initialised(runner, tmp_path)
    stuck = _hooked(isolated_agent_home / ".claude", tool.script)
    fine = _hooked(isolated_agent_home / ".claude-c2", tool.script)
    recording = agent_core.remove_hooks

    def remove_hooks(name: str, config_dir: Path | None = None) -> bool:
        if config_dir == stuck:
            raise PermissionError(13, "Permission denied", str(stuck / "settings.json"))
        return recording(name, config_dir)

    monkeypatch.setattr(agent_core, "remove_hooks", remove_hooks)

    result = runner.invoke(app, ["uninstall", "--yes", "--purge"])

    assert result.exit_code == 1
    assert agent_core.hook_commands("claude-code", fine) == [], "the other site still went"
    assert agent_core.hook_commands("claude-code", stuck) != []
    assert "Permission denied" in result.stdout
    assert "the package was NOT removed" in result.stdout
    assert world.execs == []
    assert paths.aisquare_home().is_dir(), "no purge after a failure above it"


def test_the_report_itself_withholds_the_package_after_any_failure(
    tool: Tool, world: World, isolated_agent_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The service's own answer, not only the CLI's early exit: a caller that reads
    ``package_runs`` (the TUI's hand-off, 9.1d) must get "no" after a failure."""
    stuck = _hooked(isolated_agent_home / ".claude", tool.script)
    _hooked(isolated_agent_home / ".claude-c2", tool.script)
    recording = agent_core.remove_hooks

    def remove_hooks(name: str, config_dir: Path | None = None) -> bool:
        if config_dir == stuck:
            raise PermissionError(13, "Permission denied")
        return recording(name, config_dir)

    monkeypatch.setattr(agent_core, "remove_hooks", remove_hooks)
    plan = lifecycle.uninstall_plan()

    report = lifecycle.uninstall(plan)

    assert report.failed and not report.package_runs
    assert [hook.ok for hook in report.hooks].count(False) == 1


def test_a_clean_run_lets_the_package_go(
    tool: Tool, world: World, isolated_agent_home: Path
) -> None:
    """Negative control for the one above."""
    _hooked(isolated_agent_home / ".claude", tool.script)

    report = lifecycle.uninstall(lifecycle.uninstall_plan())

    assert not report.failed and report.package_runs


# --- the fleet -------------------------------------------------------------------------


def _live_agent(root: Path, label: str = "coder-1") -> None:
    root.mkdir(parents=True, exist_ok=True)
    project = team_project(root)
    with store_session() as store:
        store.ensure_project(project)
        store.upsert_fleet_agent(
            FleetAgent(
                id=f"agt_{label}",
                project_id=project.id,
                label=label,
                role="coder",
                pane_id="%1",
                cwd=project.root,
                created_at=datetime.now(tz=UTC),
            )
        )


def test_it_refuses_while_fleet_agents_are_live_and_names_the_shutdown(
    tool: Tool,
    world: World,
    runner: CliRunner,
    isolated_agent_home: Path,
    tmp_path: Path,
) -> None:
    _initialised(runner, tmp_path)
    _live_agent(tmp_path / "repo")
    _hooked(isolated_agent_home / ".claude", tool.script)
    before = _snapshot(tmp_path)

    result = runner.invoke(app, ["--json", "uninstall", "--yes"])

    assert result.exit_code == 1
    assert _one_object(result.stdout)["error"] == "fleet_running"
    human = runner.invoke(app, ["uninstall", "--yes"])
    assert "coder-1 (" in human.stderr, "the live agent is named"
    assert "aisquare fleet shutdown --all --yes" in human.stderr
    assert _snapshot(tmp_path) == before and world.events == []


def test_rows_left_live_with_no_tmux_on_the_machine_do_not_block(
    tool: Tool,
    world: World,
    runner: CliRunner,
    isolated_agent_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative control: without tmux no fleet agent can be running, and a stale row
    must not trap someone who cannot run `fleet shutdown` either."""
    monkeypatch.setattr(lifecycle, "_tmux_on_path", lambda: False)
    _initialised(runner, tmp_path)
    _live_agent(tmp_path / "repo")
    _hooked(isolated_agent_home / ".claude", tool.script)

    result = runner.invoke(app, ["uninstall", "--yes"])

    assert result.exit_code == 0, result.output
    assert world.events[-1][0] == "package"


def test_ended_rows_do_not_count_as_live(
    tool: Tool, world: World, runner: CliRunner, tmp_path: Path
) -> None:
    _initialised(runner, tmp_path)
    _live_agent(tmp_path / "repo")
    with store_session() as store:
        store.end_fleet_agent("agt_coder-1")

    plan = lifecycle.uninstall_plan()

    assert plan.live_agents == () and plan.refusal is None


# --- --purge ---------------------------------------------------------------------------


def _home_with_markers(home: Path) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text("", encoding="utf-8")
    (home / "cache").mkdir(exist_ok=True)
    return home


def test_the_guard_refuses_the_users_home_itself(user_home: Path) -> None:
    _home_with_markers(user_home)

    assert lifecycle.purge_refusal(user_home, custom=True) == f"{user_home} is your home directory"


def test_the_guard_refuses_an_ancestor_of_the_users_home(user_home: Path) -> None:
    parent = _home_with_markers(user_home.parent)

    reason = lifecycle.purge_refusal(parent, custom=True)

    assert reason == f"{parent} contains your home directory"


def test_the_guard_refuses_a_filesystem_root(user_home: Path) -> None:
    """Checked on the guard alone: no test ever points a real purge at a root."""
    root = Path(Path.cwd().anchor)

    reason = lifecycle.purge_refusal(root, custom=True)

    assert reason == f"{root} is the root of a filesystem"


def test_the_guard_refuses_a_directory_with_no_aisquare_markers(
    user_home: Path, tmp_path: Path
) -> None:
    stranger = tmp_path / "stranger"
    (stranger / "cache").mkdir(parents=True)

    reason = lifecycle.purge_refusal(stranger, custom=False)

    assert reason is not None and "is not an aisquare home" in reason


def test_a_custom_home_holding_anything_aisquare_did_not_create_is_refused(
    user_home: Path, tmp_path: Path
) -> None:
    shared = _home_with_markers(tmp_path / "Dropbox")
    (shared / "taxes-2025.pdf").write_text("", encoding="utf-8")

    custom = lifecycle.purge_refusal(shared, custom=True)
    default = lifecycle.purge_refusal(shared, custom=False)

    assert custom is not None and "taxes-2025.pdf" in custom
    assert default is None, "the default ~/.aisquare is aisquare's by name"


def test_the_guard_refuses_a_home_that_is_a_link(user_home: Path, tmp_path: Path) -> None:
    if not can_symlink():
        pytest.skip("this machine cannot create symlinks")
    target = _home_with_markers(tmp_path / "real-home")
    link = tmp_path / "linked-home"
    link.symlink_to(target, target_is_directory=True)

    reason = lifecycle.purge_refusal(link, custom=False)

    assert reason is not None and "is a link" in reason


def test_the_guard_accepts_a_real_aisquare_home(
    user_home: Path, runner: CliRunner, tmp_path: Path
) -> None:
    """Negative control for every refusal above: the home the CLI makes is accepted,
    as a custom AISQUARE_HOME and as the default."""
    _initialised(runner, tmp_path)
    home = paths.aisquare_home()

    assert lifecycle.purge_refusal(home, custom=True) is None, sorted(
        p.name for p in home.iterdir()
    )
    assert lifecycle.purge_refusal(home, custom=False) is None


def test_purge_deletes_the_home_and_never_follows_a_link_inside_it(
    tool: Tool,
    world: World,
    runner: CliRunner,
    user_home: Path,
    isolated_agent_home: Path,
    tmp_path: Path,
) -> None:
    """#198 plans links between account slots and ~/.claude: deleting the home must
    take the LINK, never what it points at."""
    if not can_symlink():
        pytest.skip("this machine cannot create symlinks")
    _initialised(runner, tmp_path)
    claude = _hooked(isolated_agent_home / ".claude", tool.script)
    (claude / "projects").mkdir()
    (claude / "projects" / "transcript.jsonl").write_text("precious\n", encoding="utf-8")
    paths.claude_accounts_dir().mkdir(parents=True, exist_ok=True)
    (paths.claude_accounts_dir() / "3").symlink_to(claude, target_is_directory=True)

    result = runner.invoke(app, ["uninstall", "--yes", "--purge"])

    assert result.exit_code == 0, result.output
    assert not paths.aisquare_home().exists()
    assert (claude / "projects" / "transcript.jsonl").read_text("utf-8") == "precious\n"
    assert _settings(claude)["hooks"] == {"Stop": [FOREIGN]}
    assert world.events[-1][0] == "package"


def test_a_purge_the_guard_refuses_changes_nothing(
    tool: Tool,
    world: World,
    runner: CliRunner,
    user_home: Path,
    isolated_agent_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refused BEFORE anything is touched — hooks included."""
    shared = _home_with_markers(tmp_path / "shared")
    (shared / "notes.txt").write_text("mine", encoding="utf-8")
    monkeypatch.setenv(paths.HOME_ENV_VAR, str(shared))
    _hooked(isolated_agent_home / ".claude", tool.script)
    before = _snapshot(tmp_path)

    result = runner.invoke(app, ["--json", "uninstall", "--yes", "--purge"])

    assert result.exit_code == 1
    assert _one_object(result.stdout)["error"] == "purge_refused"
    assert _snapshot(tmp_path) == before and world.events == []


# --- the non-grading directory list ----------------------------------------------------


def test_hook_dirs_lists_each_directory_once_without_running_anything(
    isolated_agent_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``hook_sites`` asks every hook's program its version; ``hook_dirs`` must not."""
    probed: list[Any] = []
    monkeypatch.setattr(agent_core, "hook_binary_version", lambda argv, **_: probed.append(argv))
    ours = _hooked(isolated_agent_home / ".claude-c3", "/x/aisquare")
    bare = isolated_agent_home / ".claude-c4"
    bare.mkdir()
    recorded = tmp_path / "recorded"
    recorded.mkdir()
    _record(recorded, ours)

    found = agent_core.hook_dirs("claude-code")

    assert found == [recorded, ours], "recorded first, each once; a hookless sibling is not found"
    assert probed == []


@pytest.fixture
def unreadable_site(isolated_agent_home: Path) -> Iterator[Path]:
    if sys.platform == "win32" or not can_deny_reads():
        pytest.skip("needs a settings.json this user cannot read")
    site = _hooked(isolated_agent_home / ".claude", "/x/aisquare")
    (site / "settings.json").chmod(0)
    try:
        yield site
    finally:
        (site / "settings.json").chmod(0o600)


def test_an_unreadable_settings_file_is_reported_not_raised(
    tool: Tool, world: World, unreadable_site: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(unreadable_site))

    plan = lifecycle.uninstall_plan()

    assert [site.config_dir for site in plan.unreadable] == [unreadable_site]
    assert plan.hooks == ()
