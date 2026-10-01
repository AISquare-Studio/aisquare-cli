"""The captain's window is started with the experimental switch its starter has.

Review of #240, finding 13. The bundled ``captain`` persona is hidden while the switch is
off (``core.personas.hidden``), and the captain's window looks it up again: it runs
``aisquare launch captain --persona captain``. A window's environment is the tmux SERVER's
plus the pairs its spawn passes, so ``AISQUARE_EXPERIMENTAL_CAPTAIN=1 aisquare captain``
against a fleet server started without the variable passed the spawner's check, the window
printed "no persona named 'captain'" and exited, and ``aisquare captain`` still said
"started the captain" over a dead pane.

The variable now travels with the captain's window, at every start and restart, with the
value the starting process has. Unset there, nothing travels; no other agent's window gets
it; and it is never one of the arguments the row records, so no restart replays a stale one.
While the switch is off, ``fleet restart`` and ``fleet switch`` refuse the captain's row
with the one line every captain refusal says, before anything is stopped.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli import launch as launch_cli
from aisquare.cli.app import app
from aisquare.core import experimental
from aisquare.core import tmux as tmux_core
from aisquare.core.config import load_config, save_config
from aisquare.core.store import store_session
from aisquare.core.tmux import Completed, TmuxServer
from aisquare.core.workspace import project_id_for
from aisquare.models import ProjectInfo
from aisquare.services import fleet as fleet_service
from aisquare.services.captain import brain
from aisquare.services.captain import state as captain_state
from tests import test_fleet_service as fleet_suite
from tests.test_fleet_service import FakeTmux

# The fleet suite's fixtures, bound here so pytest finds them for this module's tests.
tmux = fleet_suite.tmux
clock = fleet_suite.clock
claude_on_path = fleet_suite.claude_on_path

ENV = "AISQUARE_EXPERIMENTAL_CAPTAIN"


def _window_env(fake: FakeTmux, index: int = -1) -> dict[str, str]:
    env = fake.spawned[index]["env"]
    assert isinstance(env, dict)
    return env


def _window_command(fake: FakeTmux, index: int = -1) -> list[str]:
    command = fake.spawned[index]["command"]
    assert isinstance(command, list)
    return command


def _on_in_the_config() -> None:
    config = load_config()
    config.experimental.captain = True
    save_config(config)


@pytest.fixture
def project(tmp_path: Path) -> ProjectInfo:
    root = (tmp_path / "repo").resolve()
    root.mkdir()
    return ProjectInfo(id=project_id_for(root), root=root)


@pytest.fixture
def launched(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Intercept the launcher's exec: what the window would have handed to Claude Code."""
    captured: dict[str, Any] = {}

    def fake_exec(binary: str, argv: list[str], env: dict[str, str]) -> None:
        captured.update(binary=binary, argv=argv, env=env)

    monkeypatch.setattr(launch_cli, "_exec", fake_exec)
    return captured


# --- the window's pairs -----------------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "ON"])
def test_the_captains_window_is_started_with_the_switch_its_starter_has(
    tmux: FakeTmux, claude_on_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """The same value, not a rewritten one: the window decides exactly as its starter did."""
    monkeypatch.setenv(ENV, value)
    brain.start()
    assert _window_env(tmux)[ENV] == value


def test_nothing_is_passed_when_the_starter_has_no_variable(
    tmux: FakeTmux, claude_on_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On through the config alone: the window reads the config too, and is handed nothing."""
    _on_in_the_config()
    monkeypatch.delenv(ENV)
    brain.start()
    assert ENV not in _window_env(tmux)
    assert not any(ENV in word for word in _window_command(tmux))


def test_no_other_agents_window_is_handed_the_switch(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    fleet_service.spawn(project, "coder", worktree=False)
    assert ENV not in _window_env(tmux), "the suite runs with the variable set"
    assert not any(ENV in word for word in _window_command(tmux))


def test_tmux_is_asked_for_the_captains_window_with_the_switch_among_its_e_pairs(
    claude_on_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The argv itself, through the real ``TmuxServer`` over a runner that runs nothing.

    The pair is the WINDOW's — tmux's ``-e``, before ``--`` — where the launcher's persona
    check reads it. It is not one of ``launch``'s own ``-e`` pairs after ``--``: those reach
    the agent only after that check, and the row records them, so every restart would
    replay the value of the day the captain was first started.
    """
    ran: list[list[str]] = []

    def recording(argv: Sequence[str], stdin: bytes | None) -> Completed:
        ran.append(list(argv))
        if "has-session" in argv:
            return Completed(1, "", "no such session")
        if "new-session" in argv:
            return Completed(0, f"@1{tmux_core._SEP}%1\n", "")
        return Completed(0, "", "")

    server = TmuxServer(
        "asq-test", binary=sys.executable, conf=tmp_path / "fleet-tmux.conf", runner=recording
    )
    monkeypatch.setattr(fleet_service, "server", lambda config=None: server)
    monkeypatch.setattr(tmux_core, "desktop_environment", lambda environ=None: {})
    brain.start()
    started = next(argv for argv in ran if "new-session" in argv)
    split = started.index("--")
    window_pairs = [started[i + 1] for i, word in enumerate(started[:split]) if word == "-e"]
    assert f"{ENV}=1" in window_pairs
    launcher = started[split + 1 :]
    assert launcher[:6] == [sys.executable, "-P", "-m", "aisquare", "launch", "captain"]
    assert not any(ENV in word for word in launcher)


# --- the finding's repro ----------------------------------------------------------------


def test_the_window_finds_the_captain_persona_on_a_server_started_without_the_switch(
    tmux: FakeTmux,
    claude_on_path: Path,
    launched: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    """The config flag off, the fleet's tmux server already running and started without the
    variable, and the owner opting in for one shell, the way ``docs/captain.md``,
    ``CAPTAIN_OFF`` and doctor all suggest. What the window runs — the command it was
    started with, in the server's environment plus the window's own pairs — resolves the
    bundled persona and hands the agent the switch, for the hooks that brief it."""
    assert load_config().experimental.captain is False, "the premise: off in the config"
    brain.start()  # the owner's shell has AISQUARE_EXPERIMENTAL_CAPTAIN=1 (conftest, captain_on)
    command = _window_command(tmux)
    cwd = tmux.spawned[-1]["cwd"]
    assert isinstance(cwd, Path)
    monkeypatch.delenv(ENV)  # the server was started without it …
    for key, value in _window_env(tmux).items():  # … and the window adds its own pairs
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(cwd)
    assert command[:4] == [sys.executable, "-P", "-m", "aisquare"]
    result = runner.invoke(app, command[4:])
    assert result.exit_code == 0, result.output
    assert "no persona named" not in result.output
    assert launched["env"]["AISQUARE_PERSONA"] == "captain"
    assert launch_cli.SYSTEM_PROMPT_FLAG in launched["argv"], "briefed as the captain"
    assert launched["env"][ENV] == "1"


# --- a restart --------------------------------------------------------------------------


def test_a_restart_hands_the_window_the_switch_the_restarting_process_has(
    tmux: FakeTmux, claude_on_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restart replays the row's launch spec, and the pair is not in it: each start reads
    the variable of the process that makes it."""
    monkeypatch.setenv(ENV, "true")
    brain.start()
    monkeypatch.setenv(ENV, "1")  # another shell restarts it
    receipt = fleet_service.restart(captain_state.home_project(), "captain")
    assert _window_env(tmux)[ENV] == "1"
    assert receipt.started.persona == "captain"


def test_a_restart_from_a_process_without_the_variable_passes_none(
    tmux: FakeTmux, claude_on_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Started under the variable, restarted where the config alone says on: nothing of the
    first start's value was recorded on the row to come back."""
    first = brain.start().agent
    assert first.launch_spec is not None
    assert not any(ENV in word for word in first.launch_spec.extra_args)
    _on_in_the_config()
    monkeypatch.delenv(ENV)
    receipt = fleet_service.restart(captain_state.home_project(), "captain")
    assert ENV not in _window_env(tmux)
    assert not any(ENV in word for word in _window_command(tmux))
    assert receipt.started.persona == "captain"


# --- off: the captain's row is not started again ----------------------------------------


@pytest.mark.parametrize("pane", ["running", "exited"])
def test_a_restart_of_the_captains_row_is_refused_while_the_switch_is_off(
    tmux: FakeTmux, claude_on_path: Path, monkeypatch: pytest.MonkeyPatch, pane: str
) -> None:
    """Off, the bundled persona does not resolve, and a replay drops a persona that no longer
    resolves rather than refuse (``fleet._persona_for_replay``): the captain came back with
    its tools and without its rules. Refused with the one line, before anything is stopped."""
    first = brain.start().agent
    if pane == "exited":
        tmux.die(first.pane_id, 0)
    monkeypatch.delenv(ENV)  # off: no variable, and the config's default
    with pytest.raises(fleet_service.FleetError) as refused:
        fleet_service.restart(captain_state.home_project(), "captain")
    assert str(refused.value) == experimental.CAPTAIN_OFF
    assert tmux.typed == [], "nothing was stopped"
    assert len(tmux.spawned) == 1, "and nothing was started"
    with store_session() as store:
        row = store.get_fleet_agent(first.id)
    assert row is not None and row.ended_at is None, "the row stands as it was"


def test_a_switch_of_the_captains_row_is_refused_while_the_switch_is_off(
    tmux: FakeTmux, claude_on_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``fleet switch`` replays the row as a restart does: with another account to move to,
    it stopped the captain and started it again without its persona."""
    fleet_suite._two_slots_with_usage(monkeypatch, work=10, personal=10)
    first = brain.start().agent
    monkeypatch.delenv(ENV)
    with pytest.raises(fleet_service.FleetError) as refused:
        fleet_service.switch(captain_state.home_project(), "captain", to="2")
    assert str(refused.value) == experimental.CAPTAIN_OFF
    assert tmux.typed == [] and len(tmux.spawned) == 1
    with store_session() as store:
        row = store.get_fleet_agent(first.id)
    assert row is not None and row.ended_at is None


def test_the_fleet_cli_says_the_one_line_for_a_refused_captain_restart(
    tmux: FakeTmux, claude_on_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    brain.start()
    monkeypatch.delenv(ENV)
    home = captain_state.home_project()
    result = runner.invoke(app, ["fleet", "restart", "captain", "-P", home.id])
    assert result.exit_code == 1, result.output
    assert experimental.CAPTAIN_OFF in " ".join(result.output.split())


def test_another_agents_restart_is_not_refused_while_the_switch_is_off(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    coder = fleet_service.spawn(project, "coder", worktree=False).agent
    monkeypatch.delenv(ENV)
    receipt = fleet_service.restart(project, coder.label)
    assert receipt.started.role == "coder" and receipt.started.id != coder.id
