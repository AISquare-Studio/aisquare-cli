"""The captain as a fleet agent: one per home, on the home board, briefed as the captain (T2).

The captain is an agent session the fleet starts like any agent (Claude Code's, or
Codex's with the experimental switch), with four differences the card's contract (seq
13121) pins: it lives on the HOME board, which stays captured and never joins the
projects; it runs from a brain folder under the home, so no project briefs it as a
worker; it has no tool but the captain's own MCP server, as its agent confines it at
every launch (``tools_only_args``); and there is exactly one per home.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import paths
from aisquare.core.agent_adapters import codex_config, register, unregister
from aisquare.core.agent_adapters.codex import TOOL_FEATURES
from aisquare.core.config import RoleLaunchProfile, load_config, save_config
from aisquare.core.harness import role_cycle
from aisquare.core.store import store_session
from aisquare.core.workspace import project_id_for
from aisquare.models import FleetAgent, FleetAgentStatus, ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services import project as project_service
from aisquare.services import team as team_service
from aisquare.services.captain import brain
from aisquare.services.captain import state as captain_state
from tests import fakebin
from tests import test_fleet_service as fleet_suite
from tests.fake_agent import FakeAgentAdapter
from tests.test_codex_account_switch import codex_on_path  # noqa: F401 - fixture, by name
from tests.test_fleet_service import FakeTmux

# The fleet suite's fixtures, bound here so pytest finds them for this module's tests.
tmux = fleet_suite.tmux
clock = fleet_suite.clock
claude_on_path = fleet_suite.claude_on_path


def _command(fake: FakeTmux, index: int = -1) -> list[str]:
    command = fake.spawned[index]["command"]
    assert isinstance(command, list)
    return command


def _after(command: list[str], flag: str) -> str:
    return command[command.index(flag) + 1]


def _mounted(command: list[str]) -> dict[str, object]:
    """The MCP servers the window's Claude Code mounts (its ``--mcp-config``)."""
    servers = json.loads(_after(command, "--mcp-config"))["mcpServers"]
    assert isinstance(servers, dict)
    return servers


#: The captain's one server, as every launch of it mounts it (``brain.server``).
def _the_server() -> dict[str, object]:
    return {
        "command": sys.executable,
        "args": ["-m", "aisquare.services.captain", "--stdio", "--close-after", "0"],
        "env": {"AISQUARE_HOME": str(paths.aisquare_home())},
    }


@pytest.fixture
def project(tmp_path: Path) -> ProjectInfo:
    root = (tmp_path / "repo").resolve()
    root.mkdir()
    return ProjectInfo(id=project_id_for(root), root=root)


# --- the home stays captured ------------------------------------------------------------


def test_the_home_is_captured_never_onboarded(project: ProjectInfo) -> None:
    home = captain_state.home_project()
    with store_session() as store:
        store.onboard_project(home)  # what spawn, ensure_codename and activate() all call
        store.onboard_project(project)
        listed = [p.id for p in store.list_projects()]
        row = store.get_project(home.id)
    assert row is not None and row.onboarded_at is None
    assert home.id not in listed, "the home never joins the sidebar or project list"
    assert project.id in listed, "every other project still onboards"


def test_onboarding_says_a_row_that_vanished_after_its_write(
    project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``onboard_project`` promises the stored row. A row deleted between the write and the
    read (another process's purge) is a ``KeyError``, as the store's other lookups say it
    — never ``None`` returned against the type, which an ``assert`` gave under ``-O``."""
    home = captain_state.home_project()
    with store_session() as store:
        monkeypatch.setattr(type(store), "get_project", lambda self, project_id: None)
        for row in (home, project):
            with pytest.raises(KeyError):
                store.onboard_project(row)


# --- the spawn --------------------------------------------------------------------------


@pytest.mark.needs_captain
def test_the_captain_starts_on_the_home_board_from_its_brain_folder(
    tmux: FakeTmux,
    claude_on_path: Path,
) -> None:
    receipt = brain.start()
    agent = receipt.agent
    home = captain_state.home_project()
    assert (agent.role, agent.label, agent.project_id) == ("captain", "captain", home.id)
    assert agent.cwd == brain.brain_dir() and brain.brain_dir().is_dir()
    assert agent.persona == "captain"
    command = _command(tmux)
    assert _mounted(command) == {"captain": _the_server()}
    assert "--strict-mcp-config" in command
    assert _after(command, "--tools") == ""
    assert _after(command, "--allowedTools") == "mcp__captain__*"
    envs = [command[i + 1] for i, word in enumerate(command) if word == "-e"]
    assert f"AISQUARE_HOME={paths.aisquare_home()}" in envs
    assert f"AISQUARE_TEAM_HUB={home.root}" in envs
    with store_session() as store:
        assert home.id not in [p.id for p in store.list_projects()]


@pytest.mark.needs_captain
def test_the_mcp_config_mounts_only_the_captain_server_through_the_module_entry(
    tmux: FakeTmux,
    claude_on_path: Path,
) -> None:
    brain.start()
    servers = _mounted(_command(tmux))
    assert list(servers) == ["captain"]
    assert servers["captain"] == _the_server()


@pytest.mark.needs_captain
def test_there_is_one_captain_per_home(tmux: FakeTmux, claude_on_path: Path) -> None:
    brain.start()
    home = captain_state.home_project()
    with pytest.raises(fleet_service.FleetError, match="already has a captain"):
        fleet_service.spawn(
            home,
            "captain",
            label="captain",
            persona="captain",
            cwd=brain.brain_dir(),
        )
    assert len(tmux.spawned) == 1, "refused before a second window was ever started"
    with store_session() as store:
        rows = store.fleet_agents(home.id, live_only=True)
    assert [row.label for row in rows] == ["captain"]


@pytest.mark.needs_captain
def test_the_captain_cannot_be_spawned_into_a_project(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
) -> None:
    with pytest.raises(fleet_service.FleetError, match="the captain lives on the home board"):
        fleet_service.spawn(project, "captain")
    assert tmux.spawned == []


@pytest.mark.needs_captain
def test_the_fleet_refuses_a_captain_without_the_captains_launch(
    tmux: FakeTmux,
    claude_on_path: Path,
) -> None:
    """``fleet spawn captain`` on the home would be a captain with no brain folder, and
    ``aisquare captain`` would then attach to it and type into it. Its tools are never the
    caller's: whatever arguments a spawn gives, the fleet confines them itself."""
    home = captain_state.home_project()
    with pytest.raises(fleet_service.FleetError, match="started by `aisquare captain`"):
        fleet_service.spawn(home, "captain")
    assert tmux.spawned == []
    fleet_service.spawn(home, "captain", cwd=brain.brain_dir(), agent_args=["--tools", "Bash"])
    command = _command(tmux)
    assert command.count("--strict-mcp-config") == 1 and _after(command, "--tools") == ""
    assert _mounted(command) == {"captain": _the_server()}


@pytest.mark.needs_captain
def test_the_captains_window_carries_the_home_and_the_hub(
    tmux: FakeTmux,
    claude_on_path: Path,
) -> None:
    """The window's own ``aisquare launch`` activates a board BEFORE it hands the agent its
    ``-e`` pairs: without the hub in the window's environment it onboarded the brain folder
    (or ``$HOME``, under ``~/.aisquare``) as a project."""
    brain.start()
    home = captain_state.home_project()
    env = tmux.spawned[-1]["env"]
    assert isinstance(env, dict)
    assert env["AISQUARE_TEAM_HUB"] == str(home.root)
    assert env["AISQUARE_HOME"] == str(paths.aisquare_home().resolve())


@pytest.mark.needs_captain
def test_the_captains_window_turns_claude_codes_rating_survey_off(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
) -> None:
    """T1e: runner2's docker pass saw Claude Code 2.1.283 draw its session-rating survey in
    the captain's pane mid-conversation; send rightly refused to type into it, so every voice
    line was refused, and from the phone the owner cannot answer it. Off at spawn, for the
    captain alone (screen.py's refusal stays the backstop)."""
    brain.start()
    env = tmux.spawned[-1]["env"]
    assert isinstance(env, dict)
    assert env["CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY"] == "1"
    fleet_service.spawn(project, "coder", worktree=False)
    coder_env = tmux.spawned[-1]["env"]
    assert isinstance(coder_env, dict)
    assert "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY" not in coder_env, "other roles unchanged"


@pytest.mark.needs_captain
def test_the_captains_launcher_joins_the_home_board_never_a_project(
    tmux: FakeTmux,
    claude_on_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the window's launcher does first, run with the window's environment and cwd."""
    brain.start()
    env = tmux.spawned[-1]["env"]
    assert isinstance(env, dict)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(brain.brain_dir())
    board = team_service.activate()
    assert board.id == captain_state.home_project().id
    with store_session() as store:
        assert store.list_projects() == [], "neither the brain folder nor the home onboarded"


@pytest.mark.needs_captain
def test_a_relative_home_gives_the_captain_absolute_paths(
    tmux: FakeTmux,
    claude_on_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The window starts in the brain folder, so a relative path would resolve from there."""
    home = paths.aisquare_home()
    monkeypatch.chdir(home.parent)
    monkeypatch.setenv("AISQUARE_HOME", home.name)
    agent = brain.start().agent
    command = _command(tmux)
    envs = [command[i + 1] for i, word in enumerate(command) if word == "-e"]
    assert agent.cwd.is_absolute()
    assert all(Path(pair.split("=", 1)[1]).is_absolute() for pair in envs)
    server = _mounted(command)["captain"]
    assert isinstance(server, dict)
    assert Path(server["env"]["AISQUARE_HOME"]).is_absolute()


# --- the captain on another agent: confined as that agent spells it -----------------------


def _captain_runs(agent: str) -> None:
    config = load_config()
    config.team.profiles["captain"] = RoleLaunchProfile(agent=agent)
    save_config(config)


def _within(inner: list[str], outer: list[str]) -> bool:
    return any(outer[at : at + len(inner)] == inner for at in range(len(outer) - len(inner) + 1))


@pytest.mark.needs_captain
def test_a_codex_captain_launches_confined_as_codex_spells_it(
    tmux: FakeTmux,
    codex_on_path: Path,  # noqa: F811 - the fixture, by name
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Experimental: the captain on Codex (``[team.profiles.captain] agent = "codex"``)
    launches with Codex's ``tools_only_args``, asked of the home its window runs under:
    every tool feature off, the server's tools approved, its variables inherited by name,
    and the operator's own MCP server in that home's ``config.toml`` switched off. No
    Claude Code flag, and none of Claude Code's unattended environment."""
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        '[mcp_servers.mine]\ncommand = "/opt/mine"\n', encoding="utf-8"
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(codex_config, "SYSTEM_CONFIG", tmp_path / "etc" / "config.toml")
    monkeypatch.setattr(codex_config, "MANAGED_CONFIG", tmp_path / "etc" / "managed.toml")
    _captain_runs("codex")

    receipt = brain.start()

    assert receipt.agent.agent == "codex" and receipt.agent.cwd == brain.brain_dir()
    command = _command(tmux)
    native = command[command.index("--") + 1 :]
    for feature in TOOL_FEATURES:
        assert _within(["--disable", feature], native), feature
    assert _within(["-c", "mcp_servers.mine.enabled=false"], native), native
    assert f"mcp_servers.captain.command={json.dumps(sys.executable)}" in native
    assert 'mcp_servers.captain.default_tools_approval_mode="approve"' in native
    kept = next(arg for arg in native if arg.startswith("mcp_servers.captain.env_vars="))
    assert all(json.dumps(name) in kept for name in brain.SERVER_INHERITS), kept
    assert not {"--strict-mcp-config", "--mcp-config", "--tools", "--allowedTools"} & set(native)
    env = tmux.spawned[-1]["env"]
    assert isinstance(env, dict) and "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY" not in env


@pytest.mark.needs_captain
def test_an_agent_that_cannot_be_confined_is_refused_as_the_captain_in_one_line(
    tmux: FakeTmux,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``tools_only_args`` answers ``None`` (the fake agent's built-in tools cannot be
    switched off): the spawn is refused, in one line naming the agent, before any window."""
    bin_dir = tmp_path / "bin"
    fakebin.executable_fake(bin_dir, "fakeagent", windows="set /p line=", posix="read line")
    fakebin.prepend_to_path(bin_dir, monkeypatch)
    fake = FakeAgentAdapter()
    register(fake)
    try:
        _captain_runs(fake.id)
        with pytest.raises(fleet_service.FleetError) as refused:
            brain.start()
    finally:
        unregister(fake.id)
    said = str(refused.value)
    assert said == "Fake Agent cannot be the captain: its built-in tools cannot be switched off"
    assert tmux.spawned == []


# --- a captain that died --------------------------------------------------------------


@pytest.mark.needs_captain
def test_an_exited_captain_is_not_found_and_a_bare_start_replaces_it(
    tmux: FakeTmux,
    claude_on_path: Path,
) -> None:
    """``say`` and the bare command both ask :func:`brain.find`; a dead pane under a row
    nothing has ended yet (no UI open, no ``fleet ls``) answered "already running"."""
    first = brain.start().agent
    tmux.die(first.pane_id, 0)
    assert brain.find() is None
    second = brain.start().agent
    assert second.id != first.id
    with store_session() as store:
        ended = store.get_fleet_agent(first.id)
    assert ended is not None and ended.ended_at is not None


TMUX_SOCKETS = pytest.mark.skipif(
    sys.platform == "win32", reason="tmux sockets are POSIX: socket_path() refuses on Windows"
)
"""The reboot rule reads a socket FILE where the fleet resolves it — there is none on
Windows, where there is no tmux (#223's Windows leg: TmuxUnavailable in the helper)."""


def _captain_at_its_prompt(
    tmux: FakeTmux, monkeypatch: pytest.MonkeyPatch
) -> tuple[FleetAgent, Path]:
    """A captain started bare, its session registered and waiting — the state a reboot
    leaves in the store — and where the fleet resolves its socket file: the one its row
    recorded at spawn (0.8.0), in a folder of the test's own (the suite's fake server
    says ``/run/fake``, which no test may write)."""
    tmp = Path(str(brain.brain_dir().parent / "tmux-tmp"))
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp))
    monkeypatch.setattr(tmux, "socket_path", lambda: tmp / "tmux-1000" / tmux.socket)
    agent = brain.start().agent
    assert agent.session_id is not None
    _captain_window_env(monkeypatch, agent.id)
    team_service.hook_session_start(agent.session_id, brain.brain_dir(), "startup")
    socket = fleet_service.server_of(agent).socket_path()
    return agent, socket


@TMUX_SOCKETS
@pytest.mark.needs_captain
def test_after_a_reboot_swept_the_socket_a_bare_captain_starts_fresh(
    tmux: FakeTmux,
    claude_on_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    """13189: the server is provably gone — no socket file where the fleet resolves it,
    which is what a reboot leaves — so the stale row is ended and the owner's first
    `aisquare captain` starts a fresh captain. Never "already running"."""
    agent, socket = _captain_at_its_prompt(tmux, monkeypatch)
    tmux.running = False  # the server died with the machine
    assert not socket.exists(), "the premise: /tmp was swept"
    assert brain.find() is None
    with store_session() as store:
        old = store.get_fleet_agent(agent.id)
    assert old is not None and old.ended_at is not None, "the stale row is ended, as lost"
    result = runner.invoke(app, ["captain"])
    assert result.exit_code == 0, result.output
    assert "started the captain" in result.output and "already running" not in result.output
    with store_session() as store:
        live = store.fleet_agents(captain_state.home_project().id, live_only=True)
    assert [row.role for row in live] == ["captain"] and live[0].id != agent.id


@TMUX_SOCKETS
@pytest.mark.needs_captain
def test_a_silent_server_with_its_socket_present_is_refused_fast_naming_reap(
    tmux: FakeTmux,
    claude_on_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    """13189: the socket file is there but nothing answers (a `kill-server` leaves exactly
    this; so does a server alive under another TMUX_TMPDIR). That is the fleet's call to
    make with `reap --server-down`, not the captain's: bare `aisquare captain` and `say`
    both fail at once, naming the command. Never a silent wait."""
    agent, socket = _captain_at_its_prompt(tmux, monkeypatch)
    tmux.running = False
    socket.parent.mkdir(parents=True, exist_ok=True)
    socket.touch()
    home = captain_state.home_project()
    reap = project_service.fleet_command("reap", home, "--server-down")
    with pytest.raises(brain.Unreachable, match=re.escape(reap)):
        brain.find()
    spawned_before = len(tmux.spawned)
    result = runner.invoke(app, ["captain"])
    assert result.exit_code == 1, result.output
    assert reap in result.output and "already running" not in result.output
    assert len(tmux.spawned) == spawned_before, "nothing was started over a row that may be alive"
    slept: list[float] = []
    monkeypatch.setattr(brain, "_sleep", slept.append)
    with pytest.raises(brain.NoReply, match=re.escape(reap)) as caught:
        brain.say("what is up", timeout=60)
    assert caught.value.timed_out is False and slept == [], "said at once, not waited out"
    with store_session() as store:
        row = store.get_fleet_agent(agent.id)
    assert row is not None and row.ended_at is None, "no evidence, so the row stands"


@TMUX_SOCKETS
@pytest.mark.parametrize("shape", ["no client", "denied socket"])
@pytest.mark.needs_captain
def test_a_tmux_that_cannot_be_asked_is_refused_not_recovered(
    tmux: FakeTmux,
    claude_on_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
) -> None:
    """A question that could not be put is no evidence of absence (the fleet's rule):
    the row stands and the owner is told what to run, not handed a second captain."""
    agent, _ = _captain_at_its_prompt(tmux, monkeypatch)
    if shape == "no client":
        tmux.installed = False
    else:
        tmux.socket_denied = True
    with pytest.raises(
        brain.Unreachable, match=r"tmux could not be (run|asked) .*fleet reap"
    ) as got:
        brain.find()  # said as a question that could not be put, not as a missing socket
    assert "sweep" not in str(got.value), (
        "refused before any sweep ran (coderp's delta gate, 13233)"
    )
    with store_session() as store:
        row = store.get_fleet_agent(agent.id)
    assert row is not None and row.ended_at is None


def test_a_dead_captain_the_listing_could_not_end_is_still_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The listing ends a dead row best-effort; a store locked past its busy timeout leaves
    the row live while its pane reads ``exited`` — and that is still no captain."""
    home = captain_state.home_project()
    row = FleetAgent(
        id="agt_dead", project_id=home.id, label="captain", role="captain",
        pane_id="%9", cwd=brain.brain_dir(), created_at=datetime.now(tz=UTC),
    )  # fmt: skip
    monkeypatch.setattr(
        fleet_service,
        "list_agents",
        lambda project, **kw: [FleetAgentStatus(agent=row, state="exited")],
    )
    assert brain.find() is None


@pytest.mark.needs_captain
def test_a_vanished_captain_is_reaped_so_it_can_be_started_again(
    tmux: FakeTmux,
    claude_on_path: Path,
) -> None:
    first = brain.start().agent
    tmux.vanish(first.pane_id)
    assert brain.find() is None
    assert brain.start().agent.id != first.id


@pytest.mark.needs_captain
def test_the_captain_label_is_reserved(
    tmux: FakeTmux,
    claude_on_path: Path,
    project: ProjectInfo,
) -> None:
    with pytest.raises(fleet_service.FleetError, match="reserved for the captain"):
        fleet_service.spawn(project, "coder", label="captain", worktree=False)


@pytest.mark.needs_captain
def test_a_restart_keeps_the_captain_in_its_brain_folder(
    tmux: FakeTmux,
    claude_on_path: Path,
) -> None:
    first = brain.start().agent
    home = captain_state.home_project()
    receipt = fleet_service.restart(home, "captain")
    assert receipt.started.cwd == brain.brain_dir()
    assert receipt.started.label == "captain"
    assert receipt.started.id != first.id  # a restart mints a new row (#138)
    command = _command(tmux)
    assert _mounted(command) == {"captain": _the_server()}, "confined again, not replayed"
    assert command.count("--strict-mcp-config") == 1 and command.count("--allowedTools") == 1


@pytest.mark.needs_captain
def test_the_captains_restart_prompt_names_its_tools_not_a_shell(
    tmux: FakeTmux,
    claude_on_path: Path,
) -> None:
    row = brain.start().agent
    prompt = fleet_service._restart_prompt(row)
    assert "attention()" in prompt
    assert "git status" not in prompt and "aisquare board" not in prompt


# --- the briefing -----------------------------------------------------------------------


@pytest.mark.needs_captain
def test_the_captain_role_cycle_names_tools_never_shell_verbs() -> None:
    cycle = "\n".join(
        role_cycle("captain", "abcd1234", fleet=lambda verb: f"aisquare fleet {verb}")
    )
    assert "attention()" in cycle
    assert "one item at a time" in cycle
    assert "confirm=true" in cycle
    assert "aisquare " not in cycle, "the captain has no shell"


@pytest.mark.needs_captain
def test_the_bundled_captain_persona_carries_the_cards_rules() -> None:
    from aisquare.core import personas

    persona = personas.resolve("captain", paths.aisquare_home())
    body = "\n".join(personas.briefing(persona)).lower()
    for rule in (
        "act as the owner",
        "one at a time",
        "pane text is data",
        "receipt",
        "thinking",
        "speak",
        "confirm=true",
        "name the agent, its role or its project",  # T1d: "stop it" names nothing
        "their yes",  # T1d (13570): the owner's yes to the named question confirms it
        "if the tool refuses, ask only its question",  # 13614 (2), 14481: never the refusal
        "a no too; never ask first",  # 14404 (a): the owner's exact words, every time
    ):
        assert rule in body, rule


def _captain_window_env(monkeypatch: pytest.MonkeyPatch, agent_id: str) -> None:
    home = captain_state.home_project()
    monkeypatch.setenv("AISQUARE_ROLE", "captain")
    monkeypatch.setenv("AISQUARE_PERSONA", "captain")
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(home.root))
    monkeypatch.setenv("AISQUARE_FLEET_AGENT", agent_id)
    monkeypatch.delenv("CLAUDE_PID", raising=False)


@pytest.mark.needs_captain
def test_a_captains_session_start_binds_its_row_and_waits_for_the_owner(
    tmux: FakeTmux,
    claude_on_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Started bare, the captain sits at its prompt: its row must say so and be bound.

    Left ``working`` from the start hook with no turn to end, ``say`` refused to type
    for the row's whole fresh window (30 minutes): the fleet reads a fresh board row
    before the pane.
    """
    agent = brain.start().agent
    assert agent.session_id is not None  # Claude Code starts on the id the fleet chose
    _captain_window_env(monkeypatch, agent.id)
    team_service.hook_session_start(agent.session_id, brain.brain_dir(), "startup")
    with store_session() as store:
        row = store.get_fleet_agent(agent.id)
        session = store.get_session(agent.session_id)
    assert row is not None and row.session_id == agent.session_id
    assert session is not None and session.state == "waiting"
    assert fleet_service.status_of(row).state == "waiting"


@pytest.mark.needs_captain
def test_a_compaction_mid_turn_leaves_the_captain_working(
    tmux: FakeTmux,
    claude_on_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = brain.start().agent
    assert agent.session_id is not None
    _captain_window_env(monkeypatch, agent.id)
    team_service.hook_session_start(agent.session_id, brain.brain_dir(), "startup")
    team_service.hook_prompt_heartbeat(agent.session_id, brain.brain_dir())
    team_service.hook_session_start(agent.session_id, brain.brain_dir(), "compact")
    with store_session() as store:
        session = store.get_session(agent.session_id)
    assert session is not None and session.state == "working"


@pytest.mark.needs_captain
def test_after_a_clear_the_captains_row_follows_the_new_session(
    tmux: FakeTmux,
    claude_on_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = brain.start().agent
    _captain_window_env(monkeypatch, agent.id)
    monkeypatch.setenv("CLAUDE_PID", "4242")
    tmux.pids[agent.pane_id] = 4242  # the pane's own process clears (rule 1 of team's section)
    assert agent.session_id is not None
    team_service.hook_session_start(agent.session_id, brain.brain_dir(), "startup")
    team_service.hook_session_end(agent.session_id, brain.brain_dir(), reason="clear")
    team_service.hook_session_start("captain-claude-2", brain.brain_dir(), "clear")
    with store_session() as store:
        row = store.get_fleet_agent(agent.id)
        session = store.get_session("captain-claude-2")
    assert row is not None and row.session_id == "captain-claude-2"
    assert session is not None and session.state == "waiting"


@pytest.mark.needs_captain
def test_the_captains_session_start_is_the_captains_briefing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = captain_state.home_project()
    brain.brain_dir().mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AISQUARE_ROLE", "captain")
    monkeypatch.setenv("AISQUARE_PERSONA", "captain")
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(home.root))
    captain_state.ensure_session(home)
    team_service.add_note(
        '{"v": 1, "tool": "projects"}',
        session_ref=captain_state.session_id_for(home.id),
        kind="captain_action",
    )
    text = team_service.hook_session_start("captain-claude-1", brain.brain_dir(), "startup")
    assert "attention()" in text, "its role cycle"
    assert "act as the owner" in text.lower(), "its persona"
    assert "aisquare task claim" not in text, "no shell protocol: the captain has no shell"
    assert '"tool": "projects"' not in text, "the owner's audit is not the captain's briefing"
    with store_session() as store:
        session = store.get_session("captain-claude-1")
    assert session is not None and session.project_id == home.id, "on the home board"
