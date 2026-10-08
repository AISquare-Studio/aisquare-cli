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
import os
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import agents as agent_core
from aisquare.core import paths
from aisquare.models import CheckStatus, DoctorCheck
from aisquare.services import agents as agents_service
from aisquare.services import diagnostics, first_run
from aisquare.services.onboarding import fix_commands
from tests.fsperms import can_deny_reads, can_symlink

_CONNECT = ("agents", "connect", "claude-code")

#: The plugin's hooks run `sh`, so native Windows reads only the settings.json route
#: (core.agents.plugin_route_supported); what it says there is pinned on every
#: platform by test_native_windows_reads_only_the_settings_json_route.
posix_route = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the plugin route runs sh: native Windows reads only the settings.json route",
)


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


@pytest.fixture(autouse=True)
def plugin_runs_this_install(monkeypatch: pytest.MonkeyPatch) -> None:
    """The aisquare the plugin's launcher finds is this install, whatever this PATH holds."""
    monkeypatch.setattr(agent_core, "plugin_runner", agent_core.current_install)


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


@posix_route
def test_a_plugin_only_install_reads_connected(claude: Path) -> None:
    _install_plugin(claude)

    check = diagnostics._check_claude_code()

    assert check.status is CheckStatus.ok, check
    assert "connected (through the aisquare plugin 0.9.0, which runs this install)" in check.detail
    assert check.fix is None and _buttons(check) == []
    assert agents_service.claude_code_connected() is True


def test_neither_route_still_offers_the_one_click_connect(claude: Path) -> None:
    """The control for the test above: the same directory with no plugin."""
    check = diagnostics._check_claude_code()

    assert check.status is CheckStatus.warn
    assert _buttons(check) == [_CONNECT]
    assert agents_service.claude_code_connected() is False


def _repo_plugin(config_dir: Path, repo: Path, scope: str, *, git: str = "dir") -> Path:
    """What ``claude plugin install aisquare@aisquare-cli --scope <scope>``, run in ``repo``,
    leaves (Claude Code 2.1.294): the key in the repository's settings file, none in the
    config dir's, and a record in the config dir naming the scope and the repository.
    ``git`` is the repository's ``.git``: a directory, or a file as in a linked worktree."""
    name = "settings.json" if scope == "project" else "settings.local.json"
    (repo / ".claude").mkdir(parents=True, exist_ok=True)
    (repo / ".claude" / name).write_text(
        json.dumps({"enabledPlugins": {agent_core.CLAUDE_PLUGIN_ID: True}}), encoding="utf-8"
    )
    if git == "dir":
        (repo / ".git").mkdir(exist_ok=True)
    else:
        (repo / ".git").write_text("gitdir: /elsewhere/.git/worktrees/repo\n", encoding="utf-8")
    (repo / "src").mkdir(exist_ok=True)
    record = {"scope": scope, "projectPath": str(repo), "version": "0.8.0"}
    (config_dir / "plugins").mkdir(parents=True, exist_ok=True)
    (config_dir / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"version": 2, "plugins": {agent_core.CLAUDE_PLUGIN_ID: [record]}}),
        encoding="utf-8",
    )
    return repo


@posix_route
@pytest.mark.parametrize("scope", ["project", "local"])
def test_a_repo_scope_plugin_connects_the_sessions_that_load_it(
    runner: CliRunner,
    claude: Path,
    tmp_path: Path,
    work_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
) -> None:
    """Installed with --scope project or local, the plugin is enabled in the repository, not
    in the config dir, so the shared check said "not connected" there: the doctor offered
    Connect, which installs the settings.json hooks beside it, and Welcome agreed (review
    of #257). Claude Code reads project settings from the directory a session starts in,
    and local settings from the root of its repository."""
    repo = _repo_plugin(claude, tmp_path / "repo", scope)

    def asked_from(where: Path) -> tuple[bool, bool, DoctorCheck]:
        monkeypatch.chdir(where)
        listed = runner.invoke(app, ["--json", "agents", "status", "claude-code"]).stdout
        return (
            agents_service.claude_code_connected(),
            json.loads(listed)[0]["connected"],
            diagnostics._check_claude_code(),
        )

    root, below, elsewhere = asked_from(repo), asked_from(repo / "src"), asked_from(work_dir)

    assert root[:2] == (True, True), root
    assert root[2].status is CheckStatus.ok and _buttons(root[2]) == [], root[2]
    assert f"through the aisquare plugin 0.8.0 at {scope} scope in {repo}" in root[2].detail
    loads_below = scope == "local"
    assert below[:2] == (loads_below, loads_below), below
    assert _buttons(below[2]) == ([] if loads_below else [_CONNECT]), below[2]
    assert elsewhere[:2] == (False, False) and _buttons(elsewhere[2]) == [_CONNECT], "control"


@posix_route
@pytest.mark.parametrize("asked_from", ["the repository", "elsewhere"])
def test_partial_hooks_beside_a_repo_scope_plugin_are_judged_on_themselves(
    runner: CliRunner,
    claude: Path,
    tmp_path: Path,
    work_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    asked_from: str,
) -> None:
    """Five of the six hooks, as an install from before StopFailure leaves them, beside a
    project-scope plugin: inside its repository the shared check counted the plugin, so the
    doctor said "all lifecycle hooks installed" with no Connect, and `agents status` gave
    the directory `hooks_installed`, while every other repository ran without StopFailure
    (review of #257). A
    directory with hooks of its own is judged on them, in the repository as outside it."""
    repo = _repo_plugin(claude, tmp_path / "repo", "project")
    _connect(runner)
    settings_path = claude / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings["hooks"].pop("StopFailure"), "the sixth hook was there to drop"
    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    monkeypatch.chdir(repo if asked_from == "the repository" else work_dir)

    listed = json.loads(runner.invoke(app, ["--json", "agents", "status", "claude-code"]).stdout)
    partial = diagnostics._check_claude_code()
    connected = agents_service.claude_code_connected()
    _connect(runner)
    completed = diagnostics._check_claude_code()

    hooked = {site["config_dir"]: site["hooks_installed"] for site in listed[0]["sites"]}
    assert connected is False and hooked.get(str(claude)) is False, listed
    assert partial.status is CheckStatus.warn, partial
    assert "all lifecycle hooks installed" not in partial.detail, partial
    assert _buttons(partial) == [_CONNECT], partial
    assert completed.status is CheckStatus.ok, f"control: Connect completes them: {completed}"


@posix_route
def test_the_first_run_probe_answers_for_the_folder_the_fleet_starts_in(
    claude: Path, tmp_path: Path, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Welcome's step 2 asked about the folder asq started in, not the project step 1 chose,
    where the fleet starts: a project-scope plugin in the chosen repository read as not
    connected, and one where asq started read as connecting a fleet elsewhere (review of
    #257). Asked about the chosen folder, both ways; unchosen, asq's own folder."""
    repo = _repo_plugin(claude, tmp_path / "repo", "project")
    stand_in = str(tmp_path / "bin" / "claude")

    def connected(started: Path, chosen: Path | None) -> bool:
        monkeypatch.chdir(started)
        return first_run.probe_claude(
            sign_in=False, which=lambda name: stand_in, cwd=chosen
        ).connected

    assert connected(work_dir, repo) is True, "the chosen repository's plugin runs its fleet"
    assert connected(repo, work_dir) is False, "the plugin where asq started does not"
    assert (connected(repo, None), connected(work_dir, None)) == (True, False), "control"


@posix_route
def test_the_doctor_asks_about_the_project_it_reports_on(
    claude: Path, tmp_path: Path, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fleet UI's doctor reports on the selected project (`doctor(cwd=root)`), where its
    sessions start, but its claude-code row asked about the folder asq started in, so it
    could contradict Welcome's step 2 beside it (review of #257). Both ways; unscoped,
    asq's own folder."""
    repo = _repo_plugin(claude, tmp_path / "repo", "project")

    def row(started: Path, project: Path | None) -> DoctorCheck:
        monkeypatch.chdir(started)
        return next(
            check for check in diagnostics.doctor(cwd=project) if check.name == "claude-code"
        )

    there, elsewhere, unscoped = row(work_dir, repo), row(repo, work_dir), row(work_dir, None)
    assert there.status is CheckStatus.ok and _buttons(there) == [], there
    assert f"through the aisquare plugin 0.8.0 at project scope in {repo}" in there.detail
    assert "hooks installed" not in there.detail, there
    assert _buttons(elsewhere) == [_CONNECT], elsewhere
    assert _buttons(unscoped) == [_CONNECT], "control: unscoped, the folder asq started in"


@posix_route
@pytest.mark.parametrize("hooks", ["live", "dead"])
def test_hooks_beside_a_repo_scope_plugin_are_graded_as_the_directorys_own(
    runner: CliRunner, claude: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hooks: str
) -> None:
    """A project-scope install covers sessions in its repository alone. Graded as the
    directory's plugin, ~/.claude's own hooks read as "two ways" inside that repository,
    and "keep the plugin" disconnected the hooks every other repository runs on; beside
    dead hooks it said the plugin runs in their place, which holds nowhere else
    (review of #257). The directory is graded on its hooks: live, it is connected (the
    launcher stands down beside them); dead, Connect rewrites them."""
    repo = _repo_plugin(claude, tmp_path / "repo", "project")
    _connect(runner)
    if hooks == "dead":
        _hooks_name(claude, str(tmp_path / "gone" / "aisquare"))
    monkeypatch.chdir(repo)

    row = diagnostics._check_claude_code()

    assert "two ways" not in row.detail and "in their place" not in row.detail, row
    assert "disconnect" not in (row.fix or ""), "never the hooks every other repository runs on"
    if hooks == "live":
        assert row.status is CheckStatus.ok, row
        assert f"at project scope in {repo} stands down beside them" in row.detail, row
    else:
        assert row.status is CheckStatus.warn and _buttons(row) == [_CONNECT], row


@posix_route
@pytest.mark.parametrize("shape", ["worktree", "home"])
def test_local_settings_are_read_from_the_repository_root_only_where_claude_code_does(
    claude: Path,
    tmp_path: Path,
    isolated_agent_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
) -> None:
    """Claude Code reads local settings from the repository's root only when it is a git
    directory this user owns and not the home directory; else from the session's own
    directory. A linked worktree (a ``.git`` file) is not followed to its main repository."""
    if shape == "worktree":
        repo = _repo_plugin(claude, tmp_path / "repo", "local", git="file")
    else:
        repo = _repo_plugin(claude, isolated_agent_home, "local")  # the home is a git repo
    monkeypatch.chdir(repo / "src")
    below = agents_service.claude_code_connected()
    monkeypatch.chdir(repo)
    at_root = agents_service.claude_code_connected()

    assert (below, at_root) == (False, True)


@posix_route
def test_both_routes_warn_and_name_both_ways_out_without_a_button(
    runner: CliRunner, claude: Path
) -> None:
    _connect(runner)
    _install_plugin(claude)

    check = diagnostics._check_claude_code()

    assert check.status is CheckStatus.warn
    assert f"runs aisquare two ways in: {claude}" in check.detail
    assert f"aisquare agents disconnect claude-code --config-dir {claude}" in (check.fix or "")
    assert f"(or keep the hooks: claude plugin uninstall {agent_core.CLAUDE_PLUGIN_ID})" in (
        check.fix or ""
    ), "the ambient dir needs no CLAUDE_CONFIG_DIR"
    assert _buttons(check) == [], "keeping one route is a choice, not a one-click fix"
    assert agents_service.claude_code_connected() is True


@posix_route
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
    ("enabled", "recorded"),
    [(False, True), (True, False)],
    ids=["disabled", "enabled-but-not-installed"],
)
def test_a_plugin_that_does_not_run_is_not_a_route(
    claude: Path, enabled: bool, recorded: bool
) -> None:
    """``/plugin disable`` writes false; an enabled key with nothing installed runs nothing."""
    _install_plugin(claude, enabled=enabled, recorded=recorded)

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
        ("plugins/installed_plugins.json", '{"plugins": {"aisquare@aisquare-cli": 7}}'),
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


@posix_route
def test_a_record_without_a_version_still_counts(claude: Path) -> None:
    _install_plugin(claude, version=None)

    check = diagnostics._check_claude_code()

    assert check.status is CheckStatus.ok
    assert "connected (through the aisquare plugin, which runs this install)" in check.detail


@posix_route
@pytest.mark.parametrize("scope", ["project", "local"])
def test_the_version_named_is_the_user_scope_installs(
    claude: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    """An older release installed for one repository, listed first in
    installed_plugins.json, named its version for the user-scope plugin every other
    session runs, and the uvx pin with it (sweep of #257)."""
    _install_plugin(claude, version="0.8.1")
    installed = claude / "plugins" / "installed_plugins.json"
    records = json.loads(installed.read_text(encoding="utf-8"))
    older = {"scope": scope, "projectPath": str(tmp_path / "repo"), "version": "0.8.0"}
    records["plugins"][agent_core.CLAUDE_PLUGIN_ID].insert(0, older)
    installed.write_text(json.dumps(records), encoding="utf-8")
    monkeypatch.setattr(agent_core, "plugin_runner", lambda: None)
    monkeypatch.setattr(agent_core, "launcher_finds", lambda name: tmp_path / "bin" / name)

    plugin = agent_core.claude_plugin(claude)
    check = diagnostics._check_claude_code()

    assert plugin is not None and plugin.version == "0.8.1", plugin
    assert check.status is CheckStatus.ok, check
    assert "plugin 0.8.1, which runs aisquare-cli==0.8.1 through uvx" in check.detail, check


@posix_route
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


@posix_route
def test_reading_the_plugin_creates_no_aisquare_state(claude: Path) -> None:
    _install_plugin(claude)
    home = paths.aisquare_home()
    assert not home.exists(), "the premise: nothing set up yet"

    rows = {check.name: check for check in diagnostics.doctor()}

    assert rows["claude-code"].status is CheckStatus.ok
    assert not home.exists(), "doctor created the aisquare home"


@posix_route  # on win32 neither mentions the plugin: test_on_native_windows_connect_and_...
def test_disconnect_says_the_plugin_keeps_aisquare_running(runner: CliRunner, claude: Path) -> None:
    _connect(runner)
    _install_plugin(claude)

    result = runner.invoke(app, ["agents", "disconnect", "claude-code"])

    assert result.exit_code == 0, result.output
    assert f"claude plugin disable {agent_core.CLAUDE_PLUGIN_ID}" in result.stderr
    assert "no aisquare hooks found" not in result.stderr


def test_disconnect_without_the_plugin_says_nothing_about_it(
    runner: CliRunner, claude: Path
) -> None:
    _connect(runner)

    result = runner.invoke(app, ["agents", "disconnect", "claude-code"])

    assert result.exit_code == 0, result.output
    assert "/plugin" not in result.stderr


@posix_route  # on win32 neither mentions the plugin: test_on_native_windows_connect_and_...
def test_connect_beside_the_plugin_says_its_hooks_stand_down(
    runner: CliRunner, claude: Path
) -> None:
    _install_plugin(claude)

    result = runner.invoke(app, ["--json", *_CONNECT])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["hooks_installed"] is True, "stdout stays one JSON object"
    assert "stand down" in result.stderr


@posix_route  # on win32 neither mentions the plugin: test_on_native_windows_connect_and_...
def test_init_beside_the_plugin_says_what_connect_says(runner: CliRunner, claude: Path) -> None:
    """Quickstart 1's `init --agent claude-code` installs the hooks as `agents connect` does,
    but said only "Connected" beside an enabled plugin: the two routes the doctor warns
    about, with nothing said when they were made (review of #257). One helper, one note."""
    argv = ["--json", "init", "--yes", "--no-onboard", "--agent", "claude-code"]
    alone = json.loads(runner.invoke(app, argv).stdout)["notes"]
    _install_plugin(claude)

    init = runner.invoke(app, argv)
    connect = runner.invoke(app, [*_CONNECT])
    beside = agents_service.plugin_beside_note("claude-code")

    assert init.exit_code == 0, init.output
    assert beside is not None and f"enabled in {claude} too" in beside, beside
    notes = json.loads(init.stdout)["notes"]
    assert beside in notes and any(n.startswith("Connected claude-code") for n in notes), notes
    assert f"note: {beside}" in connect.stderr, "connect says the same"
    assert not any("plugin" in note for note in alone), f"control: no plugin, no note: {alone}"


def _hooks_name(claude: Path, program: str) -> None:
    """Point every aisquare hook in ``claude``'s settings.json at ``program``."""
    settings_path = claude / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    for groups in settings["hooks"].values():
        for group in groups:
            for item in group["hooks"]:
                subcommand = item["command"].rsplit(" hook ", 1)[1]
                item["command"] = f"{program} hook {subcommand}"
    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")


@posix_route
def test_a_read_only_settings_json_still_says_what_its_hooks_run(
    runner: CliRunner, claude: Path, tmp_path: Path
) -> None:
    """Read-only, as home-manager's link into the Nix store is, a settings.json whose hooks
    name an aisquare that is gone read only as "hooks cannot be written": the row no
    longer said every event fails (review of #257). Both, and still no Connect."""
    _connect(runner)
    gone = tmp_path / "old-venv" / "bin" / "aisquare"
    _hooks_name(claude, str(gone))
    settings = claude / "settings.json"
    settings.chmod(0o444)
    try:
        if os.access(settings, os.W_OK):
            pytest.skip("this user can write a read-only file (root)")
        row = diagnostics._check_claude_code()
    finally:
        settings.chmod(0o644)
    writable = diagnostics._check_claude_code()

    assert row.status is CheckStatus.warn, row
    assert f"point at {gone}, which does not exist" in row.detail, row
    assert f"hooks cannot be written in {claude}" in row.detail, row
    assert "point its hooks at this install" in (row.fix or ""), row.fix
    assert _buttons(row) == [], "Connect cannot rewrite them"
    assert _buttons(writable) == [_CONNECT], "control: writable, Connect rewrites them"


@posix_route
def test_hooks_naming_a_gone_aisquare_beside_the_plugin_say_so(
    runner: CliRunner, claude: Path, tmp_path: Path
) -> None:
    """The CLI uninstalled after `agents connect`, then the plugin installed (review of #249).

    The launcher does not stand down beside a program that is gone, so the plugin is
    the route that runs and the dead hooks fail on every event. "Two ways" would be
    false, and "keep the hooks: uninstall the plugin" would leave nothing running.
    """
    _connect(runner)
    _hooks_name(claude, str(tmp_path / "uninstalled" / "aisquare"))
    _install_plugin(claude)

    check = diagnostics._check_claude_code()

    assert check.status is CheckStatus.warn
    assert "name an aisquare that does not exist" in check.detail
    assert "the aisquare plugin runs in their place" in check.detail
    assert "two ways" not in check.detail and "uninstall" not in (check.fix or "")
    assert f"aisquare agents disconnect claude-code --config-dir {claude}" in (check.fix or "")
    assert _buttons(check) == [], "connecting would keep both routes"


@posix_route
def test_a_stale_aisquare_the_plugin_runs_is_graded(
    claude: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plugin's version is its manifest's; what it RUNS is graded, as a hook's binary is."""
    old = tmp_path / "pipx" / "aisquare"
    old.parent.mkdir()
    old.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(agent_core, "plugin_runner", lambda: old)
    monkeypatch.setattr(agent_core, "hook_binary_version", lambda argv, **_kwargs: "0.6.0")
    _install_plugin(claude)

    check = diagnostics._check_claude_code()

    assert check.status is CheckStatus.warn
    assert f"the aisquare plugin in: {claude} runs {old} (0.6.0)" in check.detail
    assert "first on PATH" in (check.fix or "")


@posix_route
@pytest.mark.parametrize(("uvx", "status"), [(True, CheckStatus.ok), (False, CheckStatus.warn)])
def test_with_no_cli_the_plugin_runs_the_pin_through_uvx_or_nothing(
    claude: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, uvx: bool, status: CheckStatus
) -> None:
    # A real stand-in, not a path that happens to exist: since the doctor's search asks
    # whether the program can start, as the launcher does, a made-up /usr/bin/uvx passed
    # only on a machine whose uv put one there, and failed on CI's runners.
    real_which = shutil.which
    stand_in = tmp_path / "uv-bin" / "uvx"
    stand_in.parent.mkdir()
    stand_in.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    stand_in.chmod(0o755)
    monkeypatch.setattr(agent_core, "plugin_runner", lambda: None)
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name, *args, **kwargs: (
            (str(stand_in) if uvx else None) if name == "uvx" else real_which(name)
        ),
    )
    _install_plugin(claude)

    check = diagnostics._check_claude_code()

    assert check.status is status
    if uvx:
        assert "which runs aisquare-cli==0.9.0 through uvx" in check.detail
    else:
        assert "finds neither aisquare nor uvx on PATH" in check.detail


@posix_route
@pytest.mark.parametrize("where", [".local/bin", ".cargo/bin"])
def test_the_doctor_finds_uvx_where_the_launcher_does(
    claude: Path,
    isolated_agent_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    where: str,
) -> None:
    """The launcher looks for uvx on PATH, then in ~/.local/bin (uv's installer) and
    ~/.cargo/bin; the doctor looked on PATH only, and told a working setup to install uv
    (review of #257)."""
    real_which = shutil.which
    monkeypatch.setattr(agent_core, "plugin_runner", lambda: None)
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name, *args, **kwargs: None if name == "uvx" else real_which(name),
    )
    _install_plugin(claude)
    uvx = isolated_agent_home / where / "uvx"
    uvx.parent.mkdir(parents=True)
    uvx.write_text("#!/bin/sh\n", encoding="utf-8")
    uvx.chmod(0o755)

    check = diagnostics._check_claude_code()

    assert check.status is CheckStatus.ok, check
    assert "which runs aisquare-cli==0.9.0 through uvx" in check.detail


def test_the_plugin_commands_name_the_config_dir_they_act_on(
    isolated_agent_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/plugin` in the usual session acts on ITS dir, so a fleet account dir is named."""
    default = isolated_agent_home / ".claude"
    account = isolated_agent_home / ".claude-c2"
    command = f"claude plugin uninstall {agent_core.CLAUDE_PLUGIN_ID}"

    ambient = agent_core.claude_plugin_command("uninstall", default)
    other = agent_core.claude_plugin_command("uninstall", account)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(account))
    default_from_elsewhere = agent_core.claude_plugin_command("uninstall", default)

    assert ambient == command
    assert other == f"CLAUDE_CONFIG_DIR={account} {command}"
    assert default_from_elsewhere == f"env -u CLAUDE_CONFIG_DIR {command}"


@posix_route
def test_a_settings_json_that_is_not_utf8_costs_its_row_nothing(
    claude: Path, isolated_agent_home: Path
) -> None:
    """A UTF-16 settings.json (Notepad) beside the plugin: no traceback (review of #249)."""
    backup = isolated_agent_home / ".claude-backup"
    backup.mkdir()
    (backup / "settings.json").write_bytes(b"\xff\xfe{\x00}\x00")
    _install_plugin(claude)

    rows = {check.name: check for check in diagnostics.doctor()}

    assert rows["claude-code"].status is CheckStatus.ok, rows["claude-code"]
    (claude / "settings.json").write_bytes(b"\xff\xfe{\x00}\x00")
    assert diagnostics._check_claude_code().status is CheckStatus.warn


def test_a_hook_program_this_user_cannot_reach_costs_the_doctor_nothing(
    runner: CliRunner, claude: Path, tmp_path: Path
) -> None:
    """Path.exists raised PermissionError on 3.11 to 3.13 for a hook's program in a directory
    this user cannot enter, and `aisquare --json doctor` ended in a traceback with no
    report (review of #257). The program reads as gone, as it is to this user."""
    if sys.platform == "win32" or not can_deny_reads():
        pytest.skip("needs a directory this user cannot enter")
    locked = tmp_path / "someone-else"
    program = locked / "bin" / "aisquare"
    program.parent.mkdir(parents=True)
    program.write_text("#!/bin/sh\n", encoding="utf-8")
    _connect(runner)
    _hooks_name(claude, str(program))
    locked.chmod(0)
    try:
        result = runner.invoke(app, ["--json", "doctor"])
    finally:
        locked.chmod(0o700)

    rows = {row["name"]: row for row in json.loads(result.stdout)}
    assert rows["claude-code"]["status"] == "warn", rows["claude-code"]
    assert f"{program}, which does not exist" in rows["claude-code"]["detail"], rows


@posix_route
def test_a_plugin_only_install_reads_as_connected_everywhere(
    runner: CliRunner, claude: Path
) -> None:
    """For Claude Code, `agents list`/`status` and `aisquare status` read only agents.json,
    which the plugin route never writes: "connected: none" beside a doctor that said
    connected, and a user told so ran `agents connect` and got both routes
    (review of #257)."""
    before = json.loads(runner.invoke(app, ["--json", "agents", "status", "claude-code"]).stdout)
    _install_plugin(claude)
    listed = json.loads(runner.invoke(app, ["--json", "agents", "status", "claude-code"]).stdout)
    summary = json.loads(runner.invoke(app, ["--json", "status"]).stdout)
    row = diagnostics._check_claude_code()

    assert before[0]["connected"] is False, "control: nothing runs aisquare yet"
    assert listed[0]["connected"] is True, listed
    assert listed[0]["sites"] == [
        {"config_dir": str(claude), "hooks_installed": True, "hooks_off": None, "refused": None}
    ], listed
    assert "claude-code" in summary["agents_connected"], summary
    assert row.status is CheckStatus.ok, "doctor says the same"


@posix_route
def test_the_plugins_runner_linked_to_this_install_is_never_probed(
    claude: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On the one-liner's layout the plugin runs ~/.local/bin/aisquare, a link to this
    install's script. Compared unresolved, it was run for its version on every doctor
    run, only to conclude "which runs this install" (review of #257)."""
    if not can_symlink():
        pytest.skip("this machine cannot create symlinks")
    link = tmp_path / "local-bin" / "aisquare"
    link.parent.mkdir()
    link.symlink_to(agent_core.current_install())
    probed: list[list[str]] = []

    def probe(argv: Sequence[str], *, timeout: float = 10.0) -> str | None:
        probed.append(list(argv))
        return None

    monkeypatch.setattr(agent_core, "plugin_runner", lambda: link)
    monkeypatch.setattr(agent_core, "hook_binary_version", probe)
    _install_plugin(claude)

    check = diagnostics._check_claude_code()

    assert check.status is CheckStatus.ok and "which runs this install" in check.detail, check
    assert probed == [], "no process was started to learn it is this install"


def test_native_windows_reads_only_the_settings_json_route(
    claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On win32 the plugin's `sh` hooks are not a route doctor counts: Connect is offered.

    The supported route there is the settings.json hooks, and the plugin's launcher
    stands down beside them if Git Bash ever runs it, so the Connect button is safe.
    """
    monkeypatch.setattr(agent_core, "plugin_route_supported", lambda: False)
    _install_plugin(claude)

    check = diagnostics._check_claude_code()

    assert agent_core.claude_plugin(claude) is not None, "the plugin is still installed"
    assert agents_service.claude_code_connected() is False
    assert check.status is CheckStatus.warn
    assert _buttons(check) == [_CONNECT]


def test_on_native_windows_connect_and_disconnect_say_nothing_about_the_plugin(
    runner: CliRunner, claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """They read the plugin past the platform rule doctor and uninstall keep: on win32
    they said it "keeps running aisquare", which doctor there says it does not, and
    offered an `env -u` command cmd and PowerShell cannot run (review of #257)."""
    _install_plugin(claude)
    monkeypatch.setattr(agent_core, "plugin_route_supported", lambda: False)
    disconnected = runner.invoke(app, ["agents", "disconnect", "claude-code"])
    connected = runner.invoke(app, ["agents", "connect", "claude-code"])
    monkeypatch.setattr(agent_core, "plugin_route_supported", lambda: True)
    posix = runner.invoke(app, ["agents", "connect", "claude-code"])

    assert disconnected.exit_code == 0 and connected.exit_code == 0, connected.output
    assert "no aisquare hooks found" in disconnected.stderr, disconnected.stderr
    assert "plugin" not in disconnected.stderr + connected.stderr
    assert "the aisquare plugin is enabled" in posix.stderr, "control: where the route runs"
