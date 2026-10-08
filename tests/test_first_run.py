"""``services.first_run``: what the Welcome page asks of the machine, and the fleet it starts.

Every probe takes its seams as parameters, so these tests hand in fakes and
never read the real ``PATH`` (#240's conftest puts a stand-in ``claude`` on it,
which would flip any test that did). Each claim has its control beside it: the
route that must NOT come first, the folder that must NOT be offered, the agent
that must NOT be spawned twice, the ``prompt`` that must NOT reach a spawn.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from aisquare.core import agents as agent_core
from aisquare.core import claude_accounts as accounts_core
from aisquare.core import tmux as tmux_core
from aisquare.core.config import FleetSettings
from aisquare.core.orchestrator import team_project
from aisquare.core.selfcli import CliResult
from aisquare.core.store import store_session
from aisquare.core.tmux import Completed, TmuxServer
from aisquare.models import FleetAgent, ProjectInfo
from aisquare.services import agents as agents_service
from aisquare.services import first_run, onboarding
from aisquare.services import fleet as fleet_service
from aisquare.services.first_run import FleetStep
from tests import fakebin
from tests.fsperms import can_deny_reads, can_symlink
from tests.test_fleet_service import FakeTmux

T0 = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- fakes


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.email=welcome@test", "-c", "user.name=welcome", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=path)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=path)
    return path


def _agent(label: str, role: str, project_id: str = "prj_demo") -> FleetAgent:
    return FleetAgent(
        id=f"agt_{label}",
        project_id=project_id,
        label=label,
        role=role,
        pane_id="%9",
        tmux_socket="asq-test-first-run",
        cwd=Path("/w"),
        created_at=T0,
    )


class Spawns:
    """A ``fleet.spawn`` stand-in that records every call and answers with a receipt."""

    def __init__(self, *, refuse: dict[str, str] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.refuse = refuse or {}

    def __call__(
        self, project: ProjectInfo, role: str, **kwargs: Any
    ) -> fleet_service.SpawnReceipt:
        self.calls.append((role, dict(kwargs)))
        label = kwargs.get("label") or role
        if label in self.refuse:
            raise fleet_service.FleetError(self.refuse[label])
        return fleet_service.SpawnReceipt(
            agent=_agent(label, role, project.id), asked_label=kwargs.get("label"), tmux_session="s"
        )


def _prompted(calls: Sequence[tuple[str, dict[str, Any]]]) -> list[str]:
    """The roles of the spawns that were handed a first prompt — what Welcome must never do."""
    return [role for role, kwargs in calls if kwargs.get("prompt") is not None]


class Server(TmuxServer):
    """A tmux whose presence and version a test decides."""

    def __init__(self, *, installed: bool = True, version: tuple[int, int] | None = (3, 4)) -> None:
        super().__init__("asq-test-first-run")
        self.installed = installed
        self._version = version

    def available(self) -> bool:
        return self.installed

    def version(self) -> tuple[int, int] | None:
        return self._version


# --------------------------------------------------------------------------- install routes


def test_the_native_installer_comes_first_and_npm_never_does() -> None:
    every = {where: first_run.install_routes(where) for where in ("linux", "darwin", "win32")}
    firsts = {where: routes[0].command for where, routes in every.items()}
    assert firsts == {where: accounts_core.INSTALL_COMMAND for where in every}
    npm_first = [where for where, routes in every.items() if "npm" in routes[0].command]
    assert npm_first == []
    # Control: the same check does catch npm in first place.
    backwards = tuple(reversed(every["linux"]))
    assert "npm" in backwards[0].command


def test_each_platform_gets_its_own_routes() -> None:
    mac = [route.command for route in first_run.install_routes("darwin")]
    linux = [route.command for route in first_run.install_routes("linux")]
    windows = first_run.install_routes("win32")
    assert first_run.BREW_COMMAND in mac and first_run.BREW_COMMAND not in linux
    assert accounts_core.INSTALL_ALTERNATIVE in linux  # npm stays, last
    assert [route.how for route in windows] == ["inside WSL2"]


# --------------------------------------------------------------------------- Claude Code


def test_claude_missing_is_not_found_and_reads_no_login() -> None:
    asked: list[str] = []

    def nowhere(name: str) -> str | None:
        asked.append(name)
        return None

    def login() -> bool:
        raise AssertionError("the login of a Claude Code that is not installed was read")

    state = first_run.probe_claude(which=nowhere, connected=lambda: False, signed_in=login)
    assert (state.found, state.ready, state.signed_in) == (False, False, None)
    assert asked == ["claude"] and state.is_claude


def test_claude_found_connected_and_signed_in_is_ready(tmp_path: Path) -> None:
    binary = str(tmp_path / "claude")
    state = first_run.probe_claude(
        which=lambda name: binary, connected=lambda: True, signed_in=lambda: True
    )
    assert (state.found, state.connected, state.signed_in, state.ready) == (True, True, True, True)
    # Control: found but not connected is not ready — step 3 waits for the hooks.
    unhooked = first_run.probe_claude(
        which=lambda name: binary, connected=lambda: False, signed_in=lambda: True
    )
    assert unhooked.found and not unhooked.ready


def test_the_agent_override_is_what_the_probe_looks_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stand_in = str(tmp_path / "bin" / "demo-agent")
    monkeypatch.setenv("AISQUARE_AGENT_BIN", stand_in)
    asked: list[str] = []

    def which(name: str) -> str | None:
        asked.append(name)
        return name if name == stand_in else None

    state = first_run.probe_claude(which=which, connected=lambda: True, signed_in=lambda: True)
    assert asked == [stand_in] and state.found
    assert (state.wanted, state.source, state.is_claude) == (stand_in, "env:global", False)


def test_connected_is_the_shared_check(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    answers = iter([True, False])
    monkeypatch.setattr(
        agents_service, "claude_code_connected", lambda config_dir=None: next(answers)
    )
    binary = str(tmp_path / "claude")
    first = first_run.probe_claude(which=lambda name: binary, signed_in=lambda: True)
    second = first_run.probe_claude(which=lambda name: binary, signed_in=lambda: True)
    assert (first.connected, second.connected) == (True, False)


def test_the_periodic_look_skips_the_login(tmp_path: Path) -> None:
    reads: list[str] = []

    def login() -> bool:
        reads.append("login")
        return True

    binary = str(tmp_path / "claude")
    quick = first_run.probe_claude(
        sign_in=False, which=lambda name: binary, connected=lambda: True, signed_in=login
    )
    full = first_run.probe_claude(
        which=lambda name: binary, connected=lambda: True, signed_in=login
    )
    assert (quick.signed_in, full.signed_in, reads) == (None, True, ["login"])


def test_a_probe_that_fails_is_said_not_raised(tmp_path: Path) -> None:
    def broken() -> bool:
        raise OSError("settings.json is a directory")

    binary = str(tmp_path / "claude")
    state = first_run.probe_claude(which=lambda name: binary, connected=broken, signed_in=broken)
    assert state.found and not state.connected and state.signed_in is None
    assert state.problem is not None and "settings.json is a directory" in state.problem


def test_hooks_switched_off_are_named_and_connect_is_not_their_answer(tmp_path: Path) -> None:
    """``"disableAllHooks": true`` reads as not connected, and Connect cannot change it."""
    where = agent_core.ambient_hook_dir("claude-code")
    assert where is not None
    where.mkdir(parents=True)
    settings = where / "settings.json"
    binary = str(tmp_path / "claude")

    def probe() -> first_run.ClaudeState:
        return first_run.probe_claude(which=lambda name: binary, signed_in=lambda: True)

    settings.write_text(json.dumps({"disableAllHooks": True}), encoding="utf-8")
    off = probe()
    assert (off.connected, off.hooks_off) == (False, settings)
    settings.write_text(json.dumps({}), encoding="utf-8")  # control: hooks merely missing
    assert (probe().connected, probe().hooks_off) == (False, None)

    def never() -> Path | None:
        raise AssertionError("asked whether hooks are off for a connected Claude Code")

    connected = first_run.probe_claude(
        which=lambda name: binary, connected=lambda: True, signed_in=lambda: True, hooks_off=never
    )
    assert connected.connected and connected.hooks_off is None


def test_connect_is_the_doctors_own_fix() -> None:
    ran: list[tuple[list[str], Path | None]] = []

    def run(args: Sequence[str], *, cwd: Path | None = None) -> CliResult:
        ran.append((list(args), cwd))
        return CliResult(argv=list(args), returncode=0, stdout="{}", stderr="")

    result = first_run.connect(run=run)
    assert result.ok and ran == [(["--json", "agents", "connect", "claude-code"], None)]
    known = [fix.argv for fix in onboarding.KNOWN_FIXES]
    assert first_run.connect_fix().argv in known


# --------------------------------------------------------------------------- tmux and gh


def test_tmux_missing_old_and_fine() -> None:
    missing = first_run.probe_tmux(Server(installed=False), platform="darwin")
    old = first_run.probe_tmux(Server(version=(3, 1)), platform="darwin")
    fine = first_run.probe_tmux(Server(version=(3, 4)))
    unreadable = first_run.probe_tmux(Server(version=None))
    assert (missing.ok, missing.hint) == (False, "brew install tmux")
    assert not old.ok and old.problem is not None and "3.1" in old.problem
    assert fine.ok and unreadable.ok  # an unreadable version passes, as require() lets it


def test_gh_presence_never_raises() -> None:
    def broken(name: str) -> str | None:
        raise OSError("PATH is unreadable")

    assert first_run.gh_found(which=lambda name: f"/usr/bin/{name}") is True
    assert first_run.gh_found(which=broken) is False


# --------------------------------------------------------------------------- candidates


def test_the_folder_asq_started_in_is_offered_first(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "home" / "Code" / "demo-app")
    found = first_run.candidates(repo, home=tmp_path / "home", projects=lambda: [])
    assert [(c.root.resolve(), c.here, c.is_git, c.project) for c in found.items] == [
        (repo.resolve(), True, True, None)
    ]


def test_home_itself_is_never_offered(tmp_path: Path) -> None:
    home = _repo(tmp_path / "home")  # a home that is a git repository (dotfiles)
    found = first_run.candidates(home, home=home, projects=lambda: [])
    assert found.items == ()
    # Control: the same folder, when it is not the home directory, is offered.
    elsewhere = first_run.candidates(home, home=tmp_path / "someone-else", projects=lambda: [])
    assert [c.root.resolve() for c in elsewhere.items] == [home.resolve()]


def test_registered_projects_follow_and_nothing_is_scanned(tmp_path: Path) -> None:
    home = tmp_path / "home"
    here = _repo(home / "Code" / "here")
    listed = _repo(home / "Code" / "listed")
    gone = home / "Code" / "gone"
    planted = _repo(home / "src" / "never-registered")  # a scan would find this
    projects = [
        ProjectInfo(id="prj_listed", root=listed, onboarded_at=T0),
        ProjectInfo(id="prj_gone", root=gone, onboarded_at=T0),
    ]
    found = first_run.candidates(here, home=home, projects=lambda: projects)
    roots = [c.root.resolve() for c in found.items]
    assert roots == [here.resolve(), listed.resolve()]
    assert planted.resolve() not in roots and gone not in roots
    assert found.items[1].project == projects[0]


def test_a_captured_row_is_offered_but_not_as_listed(tmp_path: Path) -> None:
    """With ``a`` on, the shell's frame holds folders a hooked session merely ran in."""
    home = tmp_path / "home"
    captured = ProjectInfo(id="prj_dl", root=_repo(home / "Downloads"))  # never added
    listed = ProjectInfo(id="prj_api", root=_repo(home / "api"), onboarded_at=T0)
    found = first_run.candidates(
        tmp_path / "nowhere", home=home, projects=lambda: [captured, listed]
    )
    assert [(c.root.name, c.project) for c in found.items] == [
        ("Downloads", None),  # choosing it onboards it
        ("api", listed),
    ]


def test_the_list_is_bounded(tmp_path: Path) -> None:
    home = tmp_path / "home"
    projects = [
        ProjectInfo(id=f"prj_{n}", root=_repo(home / f"p{n}"), onboarded_at=T0) for n in range(6)
    ]
    found = first_run.candidates(
        tmp_path / "nowhere", home=home, projects=lambda: projects, limit=4
    )
    assert len(found.items) == 4


def test_a_store_that_will_not_open_costs_the_list_only(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "home" / "demo")

    def locked() -> list[ProjectInfo]:
        raise RuntimeError("database is locked")

    found = first_run.candidates(repo, home=tmp_path / "home", projects=locked)
    assert found.store_error == "database is locked"
    assert [c.root.resolve() for c in found.items] == [repo.resolve()]


def test_a_registered_folder_this_user_cannot_enter_is_left_out(tmp_path: Path) -> None:
    """The `.git` of a registered folder this user can see but not enter raised
    PermissionError on 3.11 to 3.13, and step 1 lost every folder with it, the one asq
    started in too; 3.14 offered it as "not a git repository" (review of #257)."""
    if sys.platform == "win32" or not can_deny_reads():
        pytest.skip("needs a directory this user cannot enter")
    home = tmp_path / "home"
    here = _repo(home / "good-app")
    locked = _repo(home / "locked-app")
    other = _repo(home / "other-app")
    projects = [
        ProjectInfo(id="prj_locked", root=locked, onboarded_at=T0),
        ProjectInfo(id="prj_other", root=other, onboarded_at=T0),
    ]
    locked.chmod(0o600)
    try:
        found = first_run.candidates(here, home=home, projects=lambda: projects)
    finally:
        locked.chmod(0o755)
    assert [(c.root.name, c.here) for c in found.items] == [
        ("good-app", True),
        ("other-app", False),
    ]
    assert found.store_error is None, "the store read fine: nothing to blame it for"
    # Control: the same folder, enterable again, is offered as the repository it is.
    again = first_run.candidates(here, home=home, projects=lambda: projects)
    assert ("locked-app", True) in [(c.root.name, c.is_git) for c in again.items]


def test_a_registered_folder_that_is_a_symlink_loop_is_left_out(tmp_path: Path) -> None:
    """``Path.resolve`` raises RuntimeError, not OSError, for a link loop on 3.11/3.12."""
    if not can_symlink():
        pytest.skip("this machine cannot create symlinks")
    home = tmp_path / "home"
    here = _repo(home / "good-app")
    loop = home / "loop-app"
    loop.symlink_to(loop)
    listed = ProjectInfo(id="prj_other", root=_repo(home / "other-app"), onboarded_at=T0)
    projects = [ProjectInfo(id="prj_loop", root=loop, onboarded_at=T0), listed]
    found = first_run.candidates(here, home=home, projects=lambda: projects)
    assert [c.root.name for c in found.items] == ["good-app", "other-app"]


def test_a_listed_folder_needs_no_onboarding_but_a_captured_one_does(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "home" / "demo")
    root = repo.resolve()
    listed = ProjectInfo(id="prj_demo", root=root, onboarded_at=T0)
    captured = ProjectInfo(id="prj_demo", root=root)  # a hooked session ran there; not added

    def judged(project: ProjectInfo) -> list[ProjectInfo | None]:
        found = first_run.candidates(
            repo,
            home=tmp_path / "home",
            projects=lambda: [],
            validate=lambda text: onboarding.validate_path(text, lookup=lambda _id: project),
        )
        return [c.project for c in found.items]

    assert judged(listed) == [listed]
    assert judged(captured) == [None]


# --------------------------------------------------------------------------- start_fleet


def test_the_fleet_starts_manager_then_two_coders_and_types_nothing(tmp_path: Path) -> None:
    project = ProjectInfo(id="prj_demo", root=_repo(tmp_path / "demo"))
    spawns = Spawns()
    heard: list[str] = []
    started = first_run.start_fleet(
        project, spawn=spawns, live=lambda p: [], on_step=lambda step: heard.append(step.label)
    )
    assert [(role, kwargs.get("label")) for role, kwargs in spawns.calls] == [
        ("manager", None),
        ("coder", "coder-1"),
        ("coder", "coder-2"),
    ]
    assert _prompted(spawns.calls) == []
    assert heard == ["manager", "coder-1", "coder-2"]
    assert [step.outcome for step in started.steps] == ["started"] * 3
    assert started.manager is not None and started.refused is None
    # Control: a spawn that WAS handed a prompt is what the check above would name.
    spawns(project, "coder", label="coder-9", prompt="hello")
    assert _prompted(spawns.calls) == ["coder"]


def test_a_live_manager_is_never_started_twice(tmp_path: Path) -> None:
    project = ProjectInfo(id="prj_demo", root=_repo(tmp_path / "demo"))
    spawns = Spawns()
    running = [_agent("manager", "manager")]
    started = first_run.start_fleet(project, coders=0, spawn=spawns, live=lambda p: running)
    assert spawns.calls == []
    assert [(s.label, s.outcome) for s in started.steps] == [("manager", "running")]
    # Control: with no manager live, the same call starts one.
    first_run.start_fleet(project, coders=0, spawn=spawns, live=lambda p: [])
    assert [role for role, _ in spawns.calls] == ["manager"]


def test_coders_are_topped_up_not_added(tmp_path: Path) -> None:
    project = ProjectInfo(id="prj_demo", root=_repo(tmp_path / "demo"))
    spawns = Spawns()
    running = [_agent("manager", "manager"), _agent("coder-1", "coder")]
    started = first_run.start_fleet(project, manager=False, spawn=spawns, live=lambda p: running)
    assert [kwargs["label"] for _, kwargs in spawns.calls] == ["coder-2"]
    assert [(s.label, s.outcome) for s in started.steps] == [
        ("coder-1", "running"),
        ("coder-2", "started"),
    ]
    both = [*running, _agent("coder-2", "coder")]
    again = Spawns()
    first_run.start_fleet(project, manager=False, spawn=again, live=lambda p: both)
    assert again.calls == []


def test_a_coder_label_held_by_a_live_agent_is_skipped(tmp_path: Path) -> None:
    project = ProjectInfo(id="prj_demo", root=_repo(tmp_path / "demo"))
    spawns = Spawns()
    running = [_agent("coder-1", "reviewer")]  # someone else holds the label
    first_run.start_fleet(project, manager=False, spawn=spawns, live=lambda p: running)
    assert [kwargs["label"] for _, kwargs in spawns.calls] == ["coder-2", "coder-3"]


def test_a_folder_that_is_not_git_gets_coders_without_worktrees(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    repo = _repo(tmp_path / "repo")
    spawns = Spawns()
    started = first_run.start_fleet(
        ProjectInfo(id="prj_plain", root=plain), manager=False, spawn=spawns, live=lambda p: []
    )
    assert [kwargs["worktree"] for _, kwargs in spawns.calls] == [False, False]
    assert all(first_run.NOT_GIT_NOTE in step.notes for step in started.steps)
    # Control: in a repository the role's own default stands (None).
    git = Spawns()
    first_run.start_fleet(
        ProjectInfo(id="prj_repo", root=repo), manager=False, spawn=git, live=lambda p: []
    )
    assert [kwargs["worktree"] for _, kwargs in git.calls] == [None, None]


def test_a_refusal_lands_on_its_step_and_stops_the_rest(tmp_path: Path) -> None:
    project = ProjectInfo(id="prj_demo", root=_repo(tmp_path / "demo"))
    no_tmux = Spawns(refuse={"manager": "tmux is not installed"})
    started = first_run.start_fleet(project, spawn=no_tmux, live=lambda p: [])
    assert [role for role, _ in no_tmux.calls] == ["manager"]
    refused = started.refused
    assert refused is not None and (refused.label, refused.detail) == (
        "manager",
        "tmux is not installed",
    )
    capped = Spawns(refuse={"coder-1": "already runs 4 agents"})
    later = first_run.start_fleet(project, manager=False, spawn=capped, live=lambda p: [])
    assert [kwargs["label"] for _, kwargs in capped.calls] == ["coder-1"]  # coder-2 not tried
    assert [(s.label, s.outcome) for s in later.steps] == [("coder-1", "refused")]


def test_a_crash_in_the_fleet_path_is_a_refusal_not_a_raise(tmp_path: Path) -> None:
    project = ProjectInfo(id="prj_demo", root=_repo(tmp_path / "demo"))

    def crash(project: ProjectInfo, role: str, **kwargs: Any) -> fleet_service.SpawnReceipt:
        raise KeyError("boom")

    def unreadable(project: ProjectInfo) -> list[FleetAgent]:
        raise RuntimeError("database is locked")

    crashed = first_run.start_fleet(project, spawn=crash, live=lambda p: [])
    blind = first_run.start_fleet(project, spawn=Spawns(), live=unreadable)
    assert crashed.refused is not None and "KeyError" in crashed.refused.detail
    assert blind.refused is not None and "database is locked" in blind.refused.detail
    assert [step.outcome for step in blind.steps] == ["refused"]


def test_the_real_spawn_starts_three_windows_and_types_into_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same calls through ``fleet.spawn`` itself, on a tmux kept in memory.

    What the fakes above cannot show: that the keyword-only call Welcome makes is
    one ``fleet.spawn`` accepts, that the labels come out as coder-1 and coder-2
    in their own worktrees, and that no pane is typed into.
    """
    tmux = FakeTmux()
    monkeypatch.setattr(fleet_service, "server", lambda config=None: tmux)
    monkeypatch.setattr(fleet_service, "settings", lambda: FleetSettings(tmux_socket="asq-test"))
    monkeypatch.setattr(tmux_core, "desktop_environment", lambda environ=None: {})
    monkeypatch.setattr(fleet_service, "_sleep", lambda seconds: None)
    fakebin.prepend_to_path(tmp_path / "bin", monkeypatch)
    fakebin.executable_fake(
        tmp_path / "bin",
        "claude",
        windows='echo fake claude: "%*"\nset /p line=',
        posix='echo "fake claude: $*"\nread line',
    )
    project = team_project(_repo(tmp_path / "demo"))
    with store_session() as store:
        store.ensure_project(project)
    started = first_run.start_fleet(project)
    assert [(s.label, s.outcome) for s in started.steps] == [
        ("manager", "started"),
        ("coder-1", "started"),
        ("coder-2", "started"),
    ], [s.detail for s in started.steps]
    assert len(tmux.spawned) == 3 and tmux.typed == []
    coders = [s.agent for s in started.steps if s.role == "coder"]
    assert all(agent is not None and agent.worktree for agent in coders)
    # Idempotent through the real service too: nothing new on a second call.
    again = first_run.start_fleet(project)
    assert [s.outcome for s in again.steps] == ["running"] * 3 and len(tmux.spawned) == 3


def test_fleet_step_lines_up_with_what_welcome_reads() -> None:
    step = FleetStep("coder-1", "coder", "started", "agt_1", _agent("coder-1", "coder"))
    assert first_run.FleetStart((step,)).manager is None
    manager = FleetStep("manager", "manager", "running", "agt_m", _agent("manager", "manager"))
    assert first_run.FleetStart((manager, step)).manager == manager.agent


def test_no_tmux_server_is_addressed_by_a_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[list[str]] = []

    def record(argv: Sequence[str], stdin: bytes | None) -> Completed:
        ran.append(list(argv))
        return Completed(0, "tmux 3.4\n", "")

    monkeypatch.setattr(tmux_core, "_tmux", record)
    state = first_run.probe_tmux(TmuxServer("asq-test-first-run", binary=sys.executable))
    assert state.ok and state.version == (3, 4)
    assert [argv[1:] for argv in ran] == [["-V"]]
