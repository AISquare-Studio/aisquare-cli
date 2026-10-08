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


def _hooks_text(program: Path | str, *, trailing_comma: bool = False) -> str:
    """settings.json text holding aisquare's six hook groups; optionally not valid JSON."""
    hooks = {
        event: [{"hooks": [{"type": "command", "command": f"{program} hook {verb}"}]}]
        for event, verb in _EVENTS
    }
    text = json.dumps({"hooks": hooks}, indent=2)
    return text[: text.rindex("\n}")] + ",\n}" if trailing_comma else text


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


@pytest.fixture
def default_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """The home as ``~/.aisquare`` would be: aisquare's by name, so --purge may delete it.

    The suite always runs with AISQUARE_HOME set, which --purge refuses outright.
    """
    monkeypatch.setattr(lifecycle, "custom_home", lambda _home: False)


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
    default_home: None,
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
    assert (plan["home"]["exists"], plan["home"]["action"]) == (False, "keep"), "no home here"


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
    default_home: None,
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


@pytest.mark.parametrize("listening", [True, False], ids=["server-listening", "no-server"])
def test_without_tmux_a_live_row_blocks_only_while_its_server_listens(
    tool: Tool,
    world: World,
    runner: CliRunner,
    isolated_agent_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    listening: bool,
) -> None:
    """A tmux that left PATH is no evidence the agents stopped (review of #253): the
    row's socket is asked. Nobody listening — a stale row — must not trap someone who
    cannot run `fleet shutdown` either."""
    monkeypatch.setattr(lifecycle, "_tmux_on_path", lambda: False)
    asked: list[str] = []

    def server_listening(name: str) -> bool:
        asked.append(name)
        return listening

    monkeypatch.setattr(lifecycle, "_server_listening", server_listening)
    _initialised(runner, tmp_path)
    _live_agent(tmp_path / "repo")
    _hooked(isolated_agent_home / ".claude", tool.script)

    result = runner.invoke(app, ["uninstall", "--yes"])

    assert asked == ["asq"], "the row's own socket is the one asked"
    if listening:
        assert result.exit_code == 1 and world.events == []
        assert "aisquare fleet shutdown --all --yes" in result.stderr
    else:
        assert result.exit_code == 0, result.output
        assert world.events[-1][0] == "package"


def test_the_probe_asks_the_socket_file_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe needs no tmux: a listener on tmux's own path is a server; a stale
    file nobody listens on, or no file at all, is none."""
    import socket as socket_module
    import tempfile

    if not hasattr(socket_module, "AF_UNIX") or not hasattr(os, "getuid"):
        pytest.skip("AF_UNIX sockets and a POSIX uid are what tmux needs")
    base = Path(tempfile.mkdtemp(prefix="asq-", dir="/tmp"))  # short: AF_UNIX paths are small
    monkeypatch.setenv("TMUX_TMPDIR", str(base))
    path = base / f"tmux-{os.getuid()}" / "probe"
    path.parent.mkdir()
    server = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
    try:
        assert lifecycle._server_listening("probe") is False, "no file: no server"
        server.bind(str(path))
        server.listen(1)
        assert lifecycle._server_listening("probe") is True
        server.close()
        assert lifecycle._server_listening("probe") is False, "a stale file: no server"
    finally:
        server.close()
        path.unlink(missing_ok=True)
        path.parent.rmdir()
        base.rmdir()


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


def test_a_home_aisquare_home_moved_is_never_purged_whatever_its_names(
    user_home: Path, tmp_path: Path
) -> None:
    """Review of #253: the guard told foreign entries apart by NAME, so a user's
    ``projects/my-startup`` and ``screenshots/passport.png`` in AISQUARE_HOME=~/work
    passed as aisquare's. No name proves anything; a moved home is never purged."""
    work = _home_with_markers(tmp_path / "work")
    (work / "projects" / "my-startup").mkdir(parents=True)
    (work / "projects" / "my-startup" / "main.py").write_text("", encoding="utf-8")
    (work / "screenshots").mkdir()
    (work / "screenshots" / "passport.png").write_bytes(b"png")

    custom = lifecycle.purge_refusal(work, custom=True)
    default = lifecycle.purge_refusal(work, custom=False)

    assert custom is not None and "AISQUARE_HOME moved the home" in custom
    assert default is None, "the default ~/.aisquare is aisquare's by name"


def test_only_a_moved_home_counts_as_custom(
    user_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    default = user_home / ".aisquare"
    monkeypatch.setenv(paths.HOME_ENV_VAR, str(tmp_path / "elsewhere"))
    moved = lifecycle.custom_home(tmp_path / "elsewhere")
    monkeypatch.setenv(paths.HOME_ENV_VAR, str(default))
    spelled_out = lifecycle.custom_home(default)
    monkeypatch.delenv(paths.HOME_ENV_VAR)
    unset = lifecycle.custom_home(default)

    assert (moved, spelled_out, unset) == (True, False, False)


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
    """Negative control for every refusal above: the home the CLI makes is accepted as
    the default ~/.aisquare — and refused once AISQUARE_HOME has moved it."""
    _initialised(runner, tmp_path)
    home = paths.aisquare_home()

    assert lifecycle.purge_refusal(home, custom=False) is None, sorted(
        p.name for p in home.iterdir()
    )
    assert lifecycle.purge_refusal(home, custom=True) is not None, "moved: never purged"


def test_purge_deletes_the_home_and_never_follows_a_link_inside_it(
    tool: Tool,
    world: World,
    default_home: None,
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


def _plugin_installed(config_dir: Path, *, enabled: bool = True) -> Path:
    """The aisquare plugin installed in ``config_dir``, enabled unless told otherwise:
    the two records Claude Code keeps (tests/test_doctor_plugin_route.py has them measured)."""
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "settings.json").write_text(
        json.dumps({"enabledPlugins": {agent_core.CLAUDE_PLUGIN_ID: enabled}}), encoding="utf-8"
    )
    (config_dir / "plugins").mkdir(exist_ok=True)
    record = {"scope": "user", "version": "0.9.0"}
    (config_dir / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"version": 2, "plugins": {agent_core.CLAUDE_PLUGIN_ID: [record]}}),
        encoding="utf-8",
    )
    return config_dir


def test_an_enabled_plugin_is_named_with_its_removal_and_a_purge_waits_for_it(
    tool: Tool,
    world: World,
    default_home: None,
    runner: CliRunner,
    user_home: Path,
    isolated_agent_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The plugin's launcher runs aisquare through uvx once the package is gone. The plan
    read only settings.json hooks, so it said there was nothing in a plugin user's Claude
    Code, and a purged home came back at the next session (review of #257)."""
    monkeypatch.setattr(agent_core, "plugin_route_supported", lambda: True)  # the route's rule
    _initialised(runner, tmp_path)
    site = _plugin_installed(isolated_agent_home / ".claude")
    removal = agent_core.claude_plugin_command("uninstall", site)
    before = _snapshot(tmp_path)

    plan = _one_object(runner.invoke(app, ["--json", "uninstall"]).stdout)
    purge = runner.invoke(app, ["--json", "uninstall", "--yes", "--purge"])
    untouched = _snapshot(tmp_path) == before and world.events == []
    plain = runner.invoke(app, ["uninstall", "--yes"])
    _plugin_installed(site, enabled=False)
    disabled = _one_object(runner.invoke(app, ["--json", "uninstall"]).stdout)

    assert removal.endswith("claude plugin uninstall aisquare@aisquare-cli"), removal
    scopes = [(plugin.pop("scope"), plugin.pop("project")) for plugin in plan["plugins"]]
    assert scopes == [("user", None)], "enabled in the config dir itself: user scope"
    assert plan["plugins"] == [{"config_dir": str(site), "version": "0.9.0", "remove": removal}]
    refused = _one_object(purge.stdout)
    assert (purge.exit_code, refused["error"]) == (1, "plugin_enabled"), refused
    assert removal in refused["detail"] and untouched, "refused with the command, nothing touched"
    assert plain.exit_code == 0, plain.output
    assert f"the aisquare plugin is still enabled in {site}" in plain.stdout, plain.stdout
    assert f"remove it: {removal}" in plain.stdout and world.events[-1][0] == "package"
    assert disabled["plugins"] == [], "control: a disabled plugin runs nothing"


def test_a_plugin_inside_the_home_a_purge_deletes_does_not_hold_the_purge_up(
    tool: Tool,
    world: World,
    default_home: None,
    runner: CliRunner,
    user_home: Path,
    isolated_agent_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fleet slot gets the plugin from `/plugin install` typed in its pane. Its config dir
    is inside the home, so the purge deletes it and nothing can run it afterwards: the purge
    was refused for it all the same (review of #257)."""
    monkeypatch.setattr(agent_core, "plugin_route_supported", lambda: True)  # the route's rule
    _initialised(runner, tmp_path)
    slot = _plugin_installed(paths.claude_accounts_dir() / "2")

    plan = _one_object(runner.invoke(app, ["--json", "uninstall", "--purge"]).stdout)
    kept = _one_object(runner.invoke(app, ["--json", "uninstall"]).stdout)
    result = runner.invoke(app, ["uninstall", "--yes", "--purge"])

    assert [plugin["config_dir"] for plugin in plan["plugins"]] == [str(slot)]
    assert plan["refusal"] is None, plan["refusal"]
    assert kept["plugins"] == plan["plugins"], "control: without --purge the slot stays, listed"
    assert result.exit_code == 0, result.output
    assert not paths.aisquare_home().exists() and world.events[-1][0] == "package"
    assert "the aisquare plugin is still enabled" not in result.stdout, result.stdout


def _repo_plugin_installed(
    config_dir: Path, repo: Path, scope: str, *, enabled: bool = True
) -> Path:
    """What `claude plugin install aisquare@aisquare-cli --scope <project|local>`, run in
    ``repo``, leaves (measured on Claude Code 2.1.294): the key in the REPOSITORY's
    settings file, none in the config dir's, and a record there naming the scope and repo."""
    settings = repo / ".claude" / ("settings.json" if scope == "project" else "settings.local.json")
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        json.dumps({"enabledPlugins": {agent_core.CLAUDE_PLUGIN_ID: enabled}}), encoding="utf-8"
    )
    installed = config_dir / "plugins" / "installed_plugins.json"
    installed.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(installed.read_text("utf-8")) if installed.is_file() else {"plugins": {}}
    data["version"] = 2
    records = data["plugins"].setdefault(agent_core.CLAUDE_PLUGIN_ID, [])
    records.append({"scope": scope, "projectPath": str(repo), "version": "0.8.0"})
    installed.write_text(json.dumps(data), encoding="utf-8")
    if not (config_dir / "settings.json").exists():
        (config_dir / "settings.json").write_text('{"enabledPlugins": {}}', encoding="utf-8")
    return repo


@pytest.mark.parametrize("scope", ["project", "local"])
def test_a_plugin_a_repository_enables_is_named_with_its_removal_and_a_purge_waits_for_it(
    tool: Tool,
    world: World,
    default_home: None,
    runner: CliRunner,
    user_home: Path,
    isolated_agent_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
) -> None:
    """`/plugin install` offers project and local scope, which enable the plugin in the
    repository's settings file and not the config dir's: uninstall never saw it, so --purge
    went ahead and the next session in that repository made the home again (sweep of #257).
    The record sits in a hookless ~/.claude* sibling, so the command names that dir too."""
    monkeypatch.setattr(agent_core, "plugin_route_supported", lambda: True)  # the route's rule
    _initialised(runner, tmp_path)
    config = isolated_agent_home / ".claude-c2"
    repo = _repo_plugin_installed(config, tmp_path / "repo", scope)
    removal = agent_core.claude_plugin_command("uninstall", config, scope=scope, project=repo)
    before = _snapshot(tmp_path)

    plan = _one_object(runner.invoke(app, ["--json", "uninstall"]).stdout)
    purge = runner.invoke(app, ["--json", "uninstall", "--yes", "--purge"])
    untouched = _snapshot(tmp_path) == before and world.events == []
    plain = runner.invoke(app, ["uninstall", "--yes"])
    _repo_plugin_installed(config, repo, scope, enabled=False)
    disabled = _one_object(runner.invoke(app, ["--json", "uninstall", "--purge"]).stdout)

    assert removal.startswith(f"cd {repo}") and str(config) in removal, removal
    assert removal.endswith(f"claude plugin uninstall aisquare@aisquare-cli --scope {scope}")
    assert plan["plugins"] == [
        {
            "config_dir": str(config),
            "version": "0.8.0",
            "scope": scope,
            "project": str(repo),
            "remove": removal,
        }
    ]
    refused = _one_object(purge.stdout)
    assert (purge.exit_code, refused["error"]) == (1, "plugin_enabled"), refused
    assert removal in refused["detail"] and untouched, "refused with the command, nothing touched"
    assert plain.exit_code == 0, plain.output
    assert f"the aisquare plugin is still enabled in {repo} ({scope} scope)" in plain.stdout
    assert f"remove it: {removal}" in plain.stdout, plain.stdout
    assert disabled["plugins"] == [] and disabled["refusal"] is None, "control: off there now"


def test_removing_the_user_scope_plugin_leaves_a_repositorys_holding_the_purge(
    tool: Tool,
    world: World,
    default_home: None,
    runner: CliRunner,
    user_home: Path,
    isolated_agent_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a user-scope install beside a project-scope one, the plan named only the bare
    `claude plugin uninstall aisquare@aisquare-cli`, which acts on the user scope. Run as
    told, the project install stayed enabled and --purge went ahead (sweep of #257)."""
    monkeypatch.setattr(agent_core, "plugin_route_supported", lambda: True)  # the route's rule
    _initialised(runner, tmp_path)
    claude = _plugin_installed(isolated_agent_home / ".claude")
    repo = _repo_plugin_installed(claude, tmp_path / "repo", "project")

    both = _one_object(runner.invoke(app, ["--json", "uninstall", "--purge"]).stdout)
    # What the user-scope removal does: the config dir's key and its record go.
    (claude / "settings.json").write_text('{"enabledPlugins": {}}', encoding="utf-8")
    installed = json.loads((claude / "plugins" / "installed_plugins.json").read_text("utf-8"))
    records = installed["plugins"][agent_core.CLAUDE_PLUGIN_ID]
    installed["plugins"][agent_core.CLAUDE_PLUGIN_ID] = [r for r in records if r["scope"] != "user"]
    (claude / "plugins" / "installed_plugins.json").write_text(json.dumps(installed), "utf-8")
    after = _one_object(runner.invoke(app, ["--json", "uninstall", "--purge"]).stdout)

    assert [(p["scope"], p["project"]) for p in both["plugins"]] == [
        ("user", None),
        ("project", str(repo)),
    ]
    assert len({p["remove"] for p in both["plugins"]}) == 2, "one command per install"
    assert [(p["scope"], p["project"]) for p in after["plugins"]] == [("project", str(repo))]
    assert after["refusal"]["error"] == "plugin_enabled", after["refusal"]
    assert f"cd {repo}" in after["refusal"]["message"], after["refusal"]


@pytest.mark.parametrize(
    "record",
    [
        {"scope": ["project"], "projectPath": "REPO"},
        {"scope": "project"},
        {"scope": "project", "projectPath": 7},
        {"scope": "project", "projectPath": ""},
        {"scope": "project", "projectPath": "."},
        {"scope": "managed", "projectPath": "REPO"},
        "not a record",
    ],
    ids=[
        "scope-a-list",
        "no-path",
        "path-a-number",
        "path-empty",
        "path-relative",
        "other-scope",
        "a-string",
    ],
)
def test_a_malformed_install_record_finds_nothing_and_never_raises(
    isolated_agent_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record: object
) -> None:
    """installed_plugins.json is Claude Code's file, read as it is: a record of a shape this
    reader does not know is no install, never a traceback in uninstall's plan. Run from
    inside the repository, so a path read relative to the cwd would find it."""
    claude = isolated_agent_home / ".claude"
    repo = _repo_plugin_installed(claude, tmp_path / "repo", "project")
    monkeypatch.chdir(repo)
    measured = agent_core.claude_repo_plugins(claude)
    installed = claude / "plugins" / "installed_plugins.json"
    if isinstance(record, dict) and record.get("projectPath") == "REPO":
        record = {**record, "projectPath": str(repo)}
    installed.write_text(
        json.dumps({"version": 2, "plugins": {agent_core.CLAUDE_PLUGIN_ID: [record]}}), "utf-8"
    )

    assert [plugin.project for plugin in measured] == [repo], "control: the measured shape"
    assert agent_core.claude_repo_plugins(claude) == []


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


def test_a_recorded_config_dir_this_user_cannot_enter_is_reported_not_raised(
    tool: Tool, world: World, runner: CliRunner, isolated_agent_home: Path
) -> None:
    """An exists() before the read raised PermissionError on 3.11/3.12 for a directory this
    user cannot enter (a backup left at mode 000), so the plan never printed; on 3.13 it
    read as a directory with no hooks (review of #257)."""
    if sys.platform == "win32" or not can_deny_reads():
        pytest.skip("needs a directory this user cannot enter")
    old = _hooked(isolated_agent_home / ".claude-old", tool.script)
    _record(old)
    old.chmod(0)
    try:
        result = runner.invoke(app, ["--json", "uninstall"])
    finally:
        old.chmod(0o700)

    assert result.exit_code == 0, result.output
    unreadable = _one_object(result.stdout)["unreadable"]
    assert [site["config_dir"] for site in unreadable] == [str(old)], unreadable
    assert "its settings.json could not be read" in unreadable[0]["reason"], unreadable


# --- review of #253, round 1 -------------------------------------------------------------


def test_a_users_hook_sharing_a_group_with_ours_is_kept_with_its_matcher(
    tool: Tool, world: World, runner: CliRunner, isolated_agent_home: Path
) -> None:
    """Finding 1: the user's command shared a group with aisquare's, and the whole
    group went. Only aisquare's ENTRY goes now; the group keeps its other keys."""
    site = _hooked(isolated_agent_home / ".claude", tool.script, foreign=False)
    settings = _settings(site)
    settings["hooks"]["Stop"][0]["matcher"] = "*"
    settings["hooks"]["Stop"][0]["hooks"].append({"type": "command", "command": "notify-send done"})
    (site / "settings.json").write_text(json.dumps(settings), encoding="utf-8")

    result = runner.invoke(app, ["uninstall", "--yes"])

    assert result.exit_code == 0, result.output
    assert _settings(site)["hooks"] == {
        "Stop": [{"matcher": "*", "hooks": [{"type": "command", "command": "notify-send done"}]}]
    }


def test_reinstalling_hooks_keeps_a_users_hook_in_a_shared_group(isolated_agent_home: Path) -> None:
    """The same rule on the way in: `agents connect` and the upgrade's refresh rewrite
    our entry and must not take the user's with the group."""
    site = _hooked(isolated_agent_home / ".claude", "/x/aisquare", foreign=False)
    settings = _settings(site)
    settings["hooks"]["Stop"][0]["hooks"].append({"type": "command", "command": "notify-send done"})
    (site / "settings.json").write_text(json.dumps(settings), encoding="utf-8")

    agent_core.install_hooks("claude-code", site)

    stop = _settings(site)["hooks"]["Stop"]
    assert {"type": "command", "command": "notify-send done"} in stop[0]["hooks"]
    assert len(agent_core.hook_commands("claude-code", site)) == len(_EVENTS)


def test_the_fail_open_warnings_reach_a_yes_run(
    tool: Tool, world: World, runner: CliRunner, isolated_agent_home: Path, tmp_path: Path
) -> None:
    """Finding 3: under --yes no plan is printed, so the report must carry them."""
    _initialised(runner, tmp_path)
    paths.db_path().write_bytes(b"this is not a sqlite database")
    _hooked(isolated_agent_home / ".claude", tool.script)

    machine = runner.invoke(app, ["--json", "uninstall", "--yes"])
    human = runner.invoke(app, ["uninstall", "--yes"])

    assert machine.exit_code == 0, machine.output
    assert _one_object(machine.stdout)["fleet_error"]
    assert "⚠ the fleet's agents could not be counted" in human.stdout


def test_a_settings_file_that_is_not_utf8_is_reported_not_raised(
    tool: Tool, world: World, runner: CliRunner, isolated_agent_home: Path, tmp_path: Path
) -> None:
    """Finding 4: UTF-16 (PowerShell 5.1's default) in a recorded directory, and
    Latin-1 in a ~/.claude* sibling, crashed the plan — --dry-run included."""
    recorded = tmp_path / "recorded"
    recorded.mkdir()
    (recorded / "settings.json").write_bytes(_hooks_text(tool.script).encode("utf-16"))
    sibling = isolated_agent_home / ".claude-old"
    sibling.mkdir(parents=True)
    (sibling / "settings.json").write_bytes(b'{"note": "caf\xe9"}')  # nothing of ours
    _record(recorded)

    result = runner.invoke(app, ["--json", "uninstall", "--dry-run"])

    assert result.exit_code == 0, result.output
    plan = _one_object(result.stdout)
    assert [entry["config_dir"] for entry in plan["unreadable"]] == [str(recorded)]


@pytest.mark.parametrize(
    ("spec", "runs"),
    [
        ({"command": "aisquare", "args": ["serve", "--stdio"]}, True),
        ({"command": "C:\\Tools\\asq.exe", "args": ["serve"]}, True),
        ({"command": "uvx", "args": ["--from", "aisquare-cli", "aisquare", "serve"]}, True),
        ({"command": "/usr/bin/python3", "args": ["-m", "aisquare", "serve"]}, True),
        (
            {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", "/home/u/Code/AISquare"],
            },
            False,
        ),
        ({"command": "node", "args": ["server.js", "--config", "/etc/aisquare.json"]}, False),
    ],
    ids=["program", "windows-program", "uvx", "python-m", "a-path-named-aisquare", "a-file"],
)
def test_an_mcp_server_runs_aisquare_only_by_program_name(spec: dict[str, Any], runs: bool) -> None:
    """Finding 5: a path or file merely named aisquare is not aisquare."""
    assert lifecycle._runs_aisquare(spec) is runs


def test_a_refused_purge_says_why_under_json(
    tool: Tool,
    world: World,
    runner: CliRunner,
    user_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding 6: the reason reaches a JSON consumer, not only stderr."""
    shared = _home_with_markers(tmp_path / "shared")
    (shared / "taxes-2025.pdf").write_text("", encoding="utf-8")
    monkeypatch.setenv(paths.HOME_ENV_VAR, str(shared))

    result = runner.invoke(app, ["--json", "uninstall", "--yes", "--purge"])

    error = _one_object(result.stdout)
    assert error["error"] == "purge_refused"
    assert "AISQUARE_HOME moved the home" in error["detail"]


def test_off_a_terminal_a_refused_plan_exits_1_without_sending_them_to_yes(
    tool: Tool,
    world: World,
    runner: CliRunner,
    user_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding 7: "re-run with --yes" pointed straight at the refusal."""
    shared = _home_with_markers(tmp_path / "shared")
    (shared / "notes.txt").write_text("mine", encoding="utf-8")
    monkeypatch.setenv(paths.HOME_ENV_VAR, str(shared))

    result = runner.invoke(app, ["uninstall", "--purge"])

    assert result.exit_code == 1
    assert "✗ it will not start" in result.stdout
    assert "re-run with --yes" not in result.stdout


def test_the_kept_home_connects_nothing_even_where_hooks_were_already_gone(
    tool: Tool, world: World, runner: CliRunner, isolated_agent_home: Path, tmp_path: Path
) -> None:
    """Finding 8: a recorded directory whose hooks were removed by hand, or that no
    longer exists, and a codex record (which never had hooks), are unrecorded too."""
    hooked = _hooked(isolated_agent_home / ".claude", tool.script)
    gone = tmp_path / "deleted-claude"
    _record(hooked, gone)
    registry = json.loads(paths.agents_registry_path().read_text("utf-8"))
    registry["connected"].append("codex")
    registry["connections"]["codex"] = [str(isolated_agent_home / ".codex")]
    paths.agents_registry_path().write_text(json.dumps(registry), encoding="utf-8")

    result = runner.invoke(app, ["uninstall", "--yes"])

    assert result.exit_code == 0, result.output
    assert agent_core.connected_dirs("claude-code") == []
    assert agent_core.connected_dirs("codex") == []


def test_mcp_servers_in_the_default_claude_json_are_found_without_hooks_there(
    tool: Tool,
    world: World,
    isolated_agent_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding 9: a plain `claude mcp add` writes ~/.claude.json, read even when
    ~/.claude carries no hooks and the shell points somewhere else."""
    (isolated_agent_home / ".claude").mkdir(parents=True)
    (isolated_agent_home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"aisquare": {"command": "aisquare", "args": ["serve"]}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(_hooked(tmp_path / "claude-work", tool.script)))

    plan = lifecycle.uninstall_plan()

    assert [(entry.name, entry.file) for entry in plan.mcp] == [
        ("aisquare", isolated_agent_home / ".claude.json")
    ]


def test_an_agents_json_that_cannot_be_updated_is_said(
    tool: Tool,
    world: World,
    runner: CliRunner,
    isolated_agent_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding 10: the failure used to be swallowed into a field nothing read."""
    site = _hooked(isolated_agent_home / ".claude", tool.script)
    _record(site)

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(13, "Permission denied", str(paths.agents_registry_path()))

    monkeypatch.setattr(agent_core, "set_connected", refuse)

    result = runner.invoke(app, ["uninstall", "--yes"])

    assert result.exit_code == 0, result.output
    assert "⚠ agents.json still lists connections" in result.stdout
    assert "Permission denied" in result.stdout


def test_an_unreadable_site_is_a_failure_so_the_package_stays(
    tool: Tool,
    world: World,
    runner: CliRunner,
    unreadable_site: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Max review, finding 1: an unreadable site may still hold our hooks, which would
    then name a program that is gone, with no uninstall left to retry with."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(unreadable_site))

    result = runner.invoke(app, ["--json", "uninstall", "--yes"])

    assert result.exit_code == 1
    report = _one_object(result.stdout)
    assert {"config_dir": str(unreadable_site), "removed": False} == {
        key: report["hooks"][0][key] for key in ("config_dir", "removed")
    }
    assert report["package"]["runs"] is False and world.execs == []


def test_a_partial_purge_leaves_the_markers_so_the_retry_is_recognised(
    tool: Tool,
    world: World,
    default_home: None,
    runner: CliRunner,
    isolated_agent_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Max review, finding 5: the markers are deleted LAST."""
    _initialised(runner, tmp_path)
    home = paths.aisquare_home()
    (home / "projects" / "p1").mkdir(parents=True)
    import shutil

    real_rmtree = shutil.rmtree

    def rmtree(path: Any, *args: Any, **kwargs: Any) -> None:
        if Path(path).name == "projects":
            raise PermissionError(13, "Permission denied", str(path))
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr("aisquare.services.lifecycle.shutil.rmtree", rmtree)
    _hooked(isolated_agent_home / ".claude", tool.script)

    result = runner.invoke(app, ["uninstall", "--yes", "--purge"])

    assert result.exit_code == 1
    assert (home / "config.toml").is_file(), "a marker went before the failure"
    assert lifecycle.purge_refusal(home, custom=False) is None, "the retry must be accepted"
    assert world.execs == []


@pytest.mark.parametrize("keychain", [True, False], ids=["macos", "elsewhere"])
def test_purge_says_the_keychain_keeps_the_slots_tokens_on_macos(
    tool: Tool,
    world: World,
    default_home: None,
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    keychain: bool,
) -> None:
    """Max review, finding 6: on macOS the slots' tokens live in the Keychain, which a
    purge does not touch — said, rather than promised away."""
    monkeypatch.setattr(accounts_core, "keychain_platform", lambda: keychain)
    _initialised(runner, tmp_path)
    slot = paths.claude_accounts_dir() / "2"
    slot.mkdir(parents=True)
    (slot / accounts_core.MARKER).write_text('{"slot": 2}', encoding="utf-8")

    human = runner.invoke(app, ["uninstall", "--purge", "--dry-run"])
    machine = runner.invoke(app, ["--json", "uninstall", "--purge"])

    said = "their sign-in tokens stay in the macOS Keychain" in human.stdout
    assert said is keychain
    assert _one_object(machine.stdout)["home"]["keychain_tokens_kept"] is keychain


def test_a_purge_that_does_not_happen_still_updates_agents_json(
    tool: Tool,
    world: World,
    default_home: None,
    runner: CliRunner,
    isolated_agent_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Max review, finding 8: a requested purge skipped after a failure kept the home
    AND every record of the sites this run had cleaned."""
    clean = _hooked(isolated_agent_home / ".claude", tool.script)
    stuck = _hooked(isolated_agent_home / ".claude-c2", tool.script)
    _record(clean, stuck)
    recording = agent_core.remove_hooks

    def remove_hooks(name: str, config_dir: Path | None = None) -> bool:
        if config_dir == stuck:
            raise PermissionError(13, "Permission denied")
        return recording(name, config_dir)

    monkeypatch.setattr(agent_core, "remove_hooks", remove_hooks)

    result = runner.invoke(app, ["uninstall", "--yes", "--purge"])

    assert result.exit_code == 1
    assert paths.aisquare_home().is_dir()
    assert agent_core.connected_dirs("claude-code") == [stuck], "the cleaned site is unrecorded"


def test_the_plan_neither_migrates_the_store_nor_recreates_the_home(
    tool: Tool, world: World, runner: CliRunner, tmp_path: Path
) -> None:
    """Max review, finding 9: the plan counted live agents through store_session,
    which runs ensure_home and the migrations."""
    import sqlite3

    _initialised(runner, tmp_path)
    home = paths.aisquare_home()
    for directory in (home / "cache", home / "log"):
        for child in sorted(directory.rglob("*"), reverse=True):
            child.unlink() if child.is_file() else child.rmdir()
        directory.rmdir()
    with sqlite3.connect(paths.db_path()) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        connection.execute(f"PRAGMA user_version = {version - 1}")
    connection.close()

    result = runner.invoke(app, ["--json", "uninstall", "--dry-run"])

    assert result.exit_code == 0, result.output
    with sqlite3.connect(paths.db_path()) as connection:
        after = connection.execute("PRAGMA user_version").fetchone()[0]
    connection.close()
    assert after == version - 1, "the plan migrated the store"
    assert not (home / "cache").exists() and not (home / "log").exists()


def test_upgrade_and_uninstall_agree_on_which_uv_tools_they_touch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Max review, finding 12: one check, so the two cannot drift."""
    monkeypatch.setattr(install_route, "find_uv", lambda: "/usr/bin/uv")
    prefix = tmp_path / "tools" / "aisquare-cli-old"
    (prefix / "bin").mkdir(parents=True)
    (prefix / install_route.RECEIPT_NAME).write_text(
        '[tool]\nrequirements = [{ name = "aisquare-cli" }]\n', encoding="utf-8"
    )
    route = install_route.classify(
        Facts(
            prefix=prefix,
            base_prefix=tmp_path / "base",
            executable=prefix / "bin" / "python",
            platform="linux",
            python_version="3.13",
        )
    )

    upgrade, uninstall = install_route.not_automated(route), install_route.not_removable(route)

    assert upgrade is not None and upgrade == uninstall
    assert "aisquare-cli-old" in upgrade


# --- review of #254, on this branch's code ------------------------------------------------


@pytest.mark.parametrize("ours", [True, False], ids=["our-hooks", "only-theirs"])
def test_hooks_in_a_file_that_is_not_valid_json_keep_the_package(
    tool: Tool,
    world: World,
    runner: CliRunner,
    isolated_agent_home: Path,
    ours: bool,
) -> None:
    """#254's review, finding 1: a trailing comma made the file read as "no hooks",
    and the package went while Claude Code might still run them. A broken file with
    nothing of ours in it is not this command's business (negative control)."""
    site = isolated_agent_home / ".claude"
    site.mkdir(parents=True)
    program = tool.script if ours else "/usr/local/bin/notify-send"
    text = _hooks_text(program, trailing_comma=True).replace(" hook ", " hook " if ours else " ")
    (site / "settings.json").write_text(text, encoding="utf-8")
    _record(site)

    result = runner.invoke(app, ["uninstall", "--yes"])

    if ours:
        assert result.exit_code == 1
        assert "so aisquare cannot rewrite it safely" in result.stdout
        assert world.execs == [], "the package stays while hooks may still call it"
    else:
        assert result.exit_code == 0, result.output
        assert world.events[-1][0] == "package"


def test_a_sibling_whose_hooks_only_show_leniently_keeps_the_package(
    tool: Tool, world: World, runner: CliRunner, isolated_agent_home: Path
) -> None:
    """#254's review, finding 2: an unrecorded ~/.claude* whose Latin-1 settings.json
    holds our hooks read as having none, so the package went under live hooks."""
    sibling = isolated_agent_home / ".claude-latin"
    sibling.mkdir(parents=True)
    text = _hooks_text(tool.script).replace('"hooks": {', '"note": "caf\u00e9", "hooks": {', 1)
    assert "\u00e9" in text, "the fixture must hold a byte that is not UTF-8 once encoded"
    (sibling / "settings.json").write_bytes(text.encode("latin-1"))

    plan = runner.invoke(app, ["--json", "uninstall"])
    run = runner.invoke(app, ["uninstall", "--yes"])

    assert [entry["config_dir"] for entry in _one_object(plan.stdout)["unreadable"]] == [
        str(sibling)
    ]
    assert run.exit_code == 1 and world.execs == []


def test_hooks_in_a_settings_json_this_user_may_not_write_keep_the_package(
    tool: Tool, world: World, runner: CliRunner, isolated_agent_home: Path
) -> None:
    """The plan offered to take out hooks it could not write out of a read-only
    settings.json, and the run then failed on the write (review of #257)."""
    site = _hooked(isolated_agent_home / ".claude", tool.script)
    settings_path = site / "settings.json"
    settings_path.chmod(0o444)
    if os.access(settings_path, os.W_OK):
        settings_path.chmod(0o644)
        pytest.skip("this user can write a read-only file (root)")
    try:
        plan = _one_object(runner.invoke(app, ["--json", "uninstall"]).stdout)
        result = runner.invoke(app, ["uninstall", "--yes"])
    finally:
        settings_path.chmod(0o644)

    assert plan["hooks"] == [], plan
    assert [entry["config_dir"] for entry in plan["unreadable"]] == [str(site)], plan
    assert "its settings.json cannot be rewritten" in plan["unreadable"][0]["reason"], plan
    assert result.exit_code == 1 and world.execs == [], "the package stays while hooks call it"
    assert agent_core.hook_commands("claude-code", site), "the hooks are untouched"


def test_a_blocked_site_keeps_the_package_and_the_home_in_the_plan_as_in_the_run(
    tool: Tool,
    world: World,
    default_home: None,
    runner: CliRunner,
    user_home: Path,
    isolated_agent_home: Path,
    tmp_path: Path,
) -> None:
    """The question left out what a blocked site rules out (#254), but the plan and its
    --json still promised the package's removal and, under --purge, the home's deletion,
    which the run kept; it also opened with "find no aisquare hooks" above a directory
    holding them (review of #257)."""
    _initialised(runner, tmp_path)
    broken = isolated_agent_home / ".claude"
    broken.mkdir(parents=True)
    (broken / "settings.json").write_text(_hooks_text(tool.script, trailing_comma=True), "utf-8")

    plan = _one_object(runner.invoke(app, ["--json", "uninstall", "--purge"]).stdout)
    human = runner.invoke(app, ["uninstall", "--purge", "--dry-run"]).stdout
    ran = _one_object(runner.invoke(app, ["--json", "uninstall", "--purge", "--yes"]).stdout)

    assert plan["package"]["runs"] is False, plan["package"]
    assert "cannot be taken out" in str(plan["package"]["reason"]), plan["package"]
    assert plan["home"]["action"] == "keep", plan["home"]
    assert "DELETE" not in human and "find no aisquare hooks" not in human, human
    assert f"then stop: the package and {paths.aisquare_home()} stay" in human, human
    assert (ran["package"]["runs"], ran["home"]["deleted"]) == (False, False), "as the run does"
    assert paths.aisquare_home().is_dir() and world.execs == []


def test_the_question_never_offers_what_an_unreadable_site_rules_out(
    tool: Tool,
    world: World,
    runner: CliRunner,
    isolated_agent_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#254's review, finding 3: asq's Uninstall button asks this question, and it said
    "nothing for aisquare to remove here" — or offered to remove the package — while
    the run would keep the package for a site it could not check."""
    monkeypatch.setattr("aisquare.cli.install._stdin_is_a_terminal", lambda: True)
    asked: list[str] = []

    def confirm(text: str, **_: object) -> bool:
        asked.append(text)
        return False

    monkeypatch.setattr("aisquare.cli.install.typer.confirm", confirm)
    broken = isolated_agent_home / ".claude"
    broken.mkdir(parents=True)
    (broken / "settings.json").write_text(_hooks_text(tool.script, trailing_comma=True), "utf-8")
    _record(broken)

    only_unreadable = runner.invoke(app, ["uninstall"])
    _hooked(isolated_agent_home / ".claude-c2", tool.script)
    with_a_readable_site = runner.invoke(app, ["uninstall"])

    assert only_unreadable.exit_code == 1
    assert "nothing can be removed until" in only_unreadable.stdout
    assert "nothing for aisquare to remove here" not in only_unreadable.stdout
    assert with_a_readable_site.exit_code == 0, with_a_readable_site.output
    assert asked == [
        "Remove aisquare's hooks from 1 directory "
        "(the package stays: the hooks in 1 other directory cannot be taken out)?"
    ]


# --- sweep of #257 -----------------------------------------------------------------------------


def _retired_slot_with_broken_hooks(program: Path | str) -> Path:
    """A slot `accounts remove` retired with aisquare's hooks still in it: its settings.json
    was not valid JSON, so they could not be taken out first, and that failure is swallowed."""
    slot = paths.claude_accounts_dir() / "2.removed-20261001T120000Z"
    slot.mkdir(parents=True)
    (slot / "settings.json").write_text(_hooks_text(program, trailing_comma=True), "utf-8")
    return slot


def test_hooks_a_purge_deletes_with_the_home_do_not_hold_the_purge_up(
    tool: Tool,
    world: World,
    default_home: None,
    runner: CliRunner,
    user_home: Path,
    isolated_agent_home: Path,
    tmp_path: Path,
) -> None:
    """A retired slot's hooks cannot be taken out, but --purge deletes the slot with the home:
    the purge was refused for them all the same, exit 1, and the plan said they "still call"
    the package (sweep of #257). Without --purge they stay, so they keep it (control)."""
    _initialised(runner, tmp_path)
    slot = _retired_slot_with_broken_hooks(tool.script)

    plain = _one_object(runner.invoke(app, ["--json", "uninstall"]).stdout)
    plan = _one_object(runner.invoke(app, ["--json", "uninstall", "--purge"]).stdout)
    human = runner.invoke(app, ["uninstall", "--purge", "--dry-run"]).stdout
    result = runner.invoke(app, ["uninstall", "--yes", "--purge"])

    assert [(site["config_dir"], site["blocks"]) for site in plain["unreadable"]] == [
        (str(slot), True)
    ]
    assert plain["package"]["runs"] is False, "control: no purge, so the hooks stay and keep it"
    assert [(site["config_dir"], site["blocks"]) for site in plan["unreadable"]] == [
        (str(slot), False)
    ]
    assert (plan["package"]["runs"], plan["home"]["action"]) == (True, "delete"), plan
    assert f"they are deleted with {paths.aisquare_home()}" in human, human
    assert "then stop" not in human and "DELETE" in human, human
    assert result.exit_code == 0, result.output
    assert not paths.aisquare_home().exists() and world.events[-1][0] == "package"


def test_a_slot_the_purge_did_not_delete_is_reported_with_its_hooks(
    tool: Tool,
    world: World,
    default_home: None,
    runner: CliRunner,
    user_home: Path,
    isolated_agent_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its hooks go only with the home. When another site fails and the purge is not
    attempted, they are still there, and the report names them with the rest."""
    _initialised(runner, tmp_path)
    slot = _retired_slot_with_broken_hooks(tool.script)
    stuck = _hooked(isolated_agent_home / ".claude", tool.script)

    def refuse(name: str, config_dir: Path | None = None) -> bool:
        raise PermissionError(13, "Permission denied", str(config_dir))

    monkeypatch.setattr(agent_core, "remove_hooks", refuse)

    result = runner.invoke(app, ["--json", "uninstall", "--yes", "--purge"])

    report = _one_object(result.stdout)
    assert result.exit_code == 1
    assert {(hook["config_dir"], hook["removed"]) for hook in report["hooks"]} == {
        (str(stuck), False),
        (str(slot), False),
    }
    assert report["home"]["deleted"] is False and paths.aisquare_home().is_dir()
    assert world.execs == []


def test_a_slot_that_links_out_of_the_home_still_holds_the_purge_up(
    tool: Tool,
    world: World,
    default_home: None,
    runner: CliRunner,
    user_home: Path,
    isolated_agent_home: Path,
    tmp_path: Path,
) -> None:
    """The purge unlinks a slot that is a link and leaves what it points at: hooks there
    outlive the purge, so they still hold it up. "Inside the home" is where a directory
    really is, never the path that names it."""
    if not can_symlink():
        pytest.skip("this machine cannot create symlinks")
    _initialised(runner, tmp_path)
    broken = tmp_path / "elsewhere" / "claude-old"  # reached through the slot alone
    broken.mkdir(parents=True)
    (broken / "settings.json").write_text(_hooks_text(tool.script, trailing_comma=True), "utf-8")
    slot = paths.claude_accounts_dir() / "3"
    slot.parent.mkdir(parents=True, exist_ok=True)
    slot.symlink_to(broken, target_is_directory=True)

    plan = _one_object(runner.invoke(app, ["--json", "uninstall", "--purge"]).stdout)

    assert [(site["config_dir"], site["blocks"]) for site in plan["unreadable"]] == [
        (str(slot), True)
    ]
    assert (plan["package"]["runs"], plan["home"]["action"]) == (False, "keep"), plan


@pytest.mark.parametrize("case", ["clean", "blocked", "fails-at-run"])
def test_the_run_report_says_why_the_package_stays_whenever_it_does(
    tool: Tool,
    world: World,
    runner: CliRunner,
    isolated_agent_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    """The run's --json gave {"runs": false, "reason": null} for a blocked or failed run,
    where the plan's --json, for the same state, gave the reason (sweep of #257)."""
    site = isolated_agent_home / ".claude"
    if case == "blocked":
        site.mkdir(parents=True)
        (site / "settings.json").write_text(_hooks_text(tool.script, trailing_comma=True), "utf-8")
    else:
        _hooked(site, tool.script)
    if case == "fails-at-run":

        def refuse(name: str, config_dir: Path | None = None) -> bool:
            raise PermissionError(13, "Permission denied", str(config_dir))

        monkeypatch.setattr(agent_core, "remove_hooks", refuse)

    plan = _one_object(runner.invoke(app, ["--json", "uninstall"]).stdout)["package"]
    ran = runner.invoke(app, ["--json", "uninstall", "--yes"])
    report = _one_object(ran.stdout)["package"]

    assert ran.exit_code == (0 if case == "clean" else 1), ran.output
    assert report["runs"] is (case == "clean"), report
    assert (report["reason"] is None) is report["runs"], "a reason exactly when it does not run"
    if case == "blocked":
        assert report["reason"] == plan["reason"], (plan, report)


@pytest.mark.parametrize("home", [True, False], ids=["a-home", "no-home"])
def test_the_json_plan_deletes_the_home_exactly_when_the_text_plan_does(
    tool: Tool,
    world: World,
    default_home: None,
    runner: CliRunner,
    user_home: Path,
    isolated_agent_home: Path,
    tmp_path: Path,
    home: bool,
) -> None:
    """With --purge and no home, the --json plan said "action": "delete" while the text
    plan had no DELETE line and the run deleted nothing (sweep of #257)."""
    if home:
        _initialised(runner, tmp_path)
    _hooked(isolated_agent_home / ".claude", tool.script)

    machine = _one_object(runner.invoke(app, ["--json", "uninstall", "--purge"]).stdout)["home"]
    human = runner.invoke(app, ["uninstall", "--purge", "--dry-run"]).stdout

    assert machine["exists"] is home, machine
    assert machine["action"] == ("delete" if home else "keep"), machine
    assert ("DELETE" in human) is home, human
