"""A restart's and a switch's window run what the fleet decided, and a restart's own model runs.

tester-43c on #43, reopen 3. (A) Every fleet window passes its ``aisquare launch``
``--fleet-decided``: the model and effort on its line are the fleet's one decision, and the
tmux server's ``AISQUARE_MODEL_<ROLE>`` / ``AISQUARE_EFFORT_<ROLE>`` reach no argv. Only a new
spawn's window was run through to its argv; with the flag on new spawns alone (mutant M3),
``fleet restart coder-1`` of a Codex coder ran the server's env pins. (B) ``fleet restart
<label> --model X --effort Y`` of a Codex agent printed a check mark and ran neither, and the
replacement's ``launch_spec`` recorded neither, while Claude Code's ran both. Each runs here
through the real window launch to the agent's argv, for both agents.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core.agent_adapters import get_adapter
from aisquare.core.config import (
    AgentModelSettings,
    FleetRoleSettings,
    RoleLaunchProfile,
    load_config,
    save_config,
)
from aisquare.models import ClaudeAccount, FleetAgent, ProjectInfo
from aisquare.services import agent_launch, model_memory
from aisquare.services import fleet as fleet_service
from tests.test_codex_account_switch import codex_on_path  # noqa: F401 - fixture, by name
from tests.test_codex_fleet_parity import _window_launched
from tests.test_fleet_service import (  # noqa: F401 - fixtures, by name
    FakeTmux,
    claude_on_path,
    project,
    repo,
    tmux,
)
from tests.test_hand_off_across_agents import _claude_slots, _codex_slot

#: What the fleet's tmux server exports: pins a fleet window never runs (#441).
ENV_MODEL = {"claude-code": "haiku", "codex": "gpt-env"}
ENV_EFFORT = "low"
#: A model each agent's own config names for the coder (``[agents.models.<agent>]``).
CONFIGURED = {"claude-code": "opus", "codex": "gpt-test"}
#: What the agent ran before its restart, and what the restart is given.
BEFORE = {"claude-code": "sonnet", "codex": "gpt-old"}
GIVEN = {"claude-code": "opus", "codex": "gpt-5.4"}


def _two_slots(agent: str, monkeypatch: pytest.MonkeyPatch) -> list[ClaudeAccount]:
    """Two signed-in slots of ``agent``'s, each with room."""
    if agent == "claude-code":
        return _claude_slots(monkeypatch, 10, 10)
    return [_codex_slot(10, "a@example.com"), _codex_slot(10, "b@example.com")]


def _the_servers_env_pins(agent: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The env pins in this process's environment, which the window's launch runs under: it
    stands for the tmux server's global one (``_window_launched``)."""
    monkeypatch.setenv("AISQUARE_MODEL_CODER", ENV_MODEL[agent])
    monkeypatch.setenv("AISQUARE_EFFORT_CODER", ENV_EFFORT)


def _ran(agent: str, argv: list[str]) -> tuple[str | None, str | None]:
    choice = get_adapter(agent).native_choice(argv)
    return choice.model, choice.effort


def _no_env_pin(agent: str, argv: list[str]) -> None:
    assert not any(ENV_MODEL[agent] in arg for arg in argv), argv
    assert ENV_EFFORT not in argv and f'"{ENV_EFFORT}"' not in " ".join(argv), argv


def _each_once(agent: str, argv: list[str]) -> None:
    """The model and the effort are each passed once: Codex 0.164.0 refuses a doubled
    ``--model`` (#41), and takes the last ``model_reasoning_effort`` it is given."""
    if agent == "codex":
        models = [arg for arg in argv if arg in {"--model", "-m"} or arg.startswith("model=")]
        efforts = [arg for arg in argv if arg.startswith("model_reasoning_effort=")]
    else:
        models = [arg for arg in argv if arg == "--model"]
        efforts = [arg for arg in argv if arg == "--effort"]
    assert (len(models), len(efforts)) == (1, 1), argv


@pytest.mark.parametrize("pin", ["nothing", "a-configured-model"])
@pytest.mark.parametrize("replay", ["restart", "switch"])
@pytest.mark.parametrize("agent", ["claude-code", "codex"])
def test_a_restarts_and_a_switchs_window_runs_no_env_pin_of_the_servers(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    agent: str,
    replay: str,
    pin: str,
) -> None:
    """(A), mutant M3. A bound coder of either agent, the server exporting
    ``AISQUARE_MODEL_CODER`` / ``AISQUARE_EFFORT_CODER``: its spawn passes nothing of them, and
    neither does its restart nor its switch to another slot of its agent. Each replay's window
    carries ``--fleet-decided`` and is run through its own ``aisquare launch`` to the argv: it
    runs what the spawn ran, with no env pin. Under M3 a Codex restart ran ``--model gpt-env``
    and ``model_reasoning_effort="low"``; with a configured model alone, the env effort."""
    first, second = _two_slots(agent, monkeypatch)
    config = load_config()
    config.fleet.roles["coder"] = FleetRoleSettings(worktree=False)
    config.team.profiles["coder"] = RoleLaunchProfile(agent=agent)
    if pin == "a-configured-model":
        config.agents.models[agent] = AgentModelSettings(model=CONFIGURED[agent])
    save_config(config)
    _the_servers_env_pins(agent, monkeypatch)
    expected = (CONFIGURED[agent] if pin == "a-configured-model" else None, None)

    spawned = fleet_service.spawn(project, "coder", account=str(first.slot)).agent
    assert spawned.agent == agent
    assert _ran(agent, _window_launched(tmux, CliRunner(), monkeypatch)) == expected
    if replay == "restart":
        started = fleet_service.restart(project, spawned.label).started
    else:
        started = fleet_service.switch(project, spawned.label, to=str(second.slot)).started

    assert (started.agent, started.account_slot) == (
        agent,
        first.slot if replay == "restart" else second.slot,
    )
    command = tmux.spawned[-1]["command"]
    assert isinstance(command, list) and agent_launch.FLEET_DECIDED_FLAG in command
    argv = _window_launched(tmux, CliRunner(), monkeypatch)
    assert _ran(agent, argv) == expected, argv
    _no_env_pin(agent, argv)


def _spawn_with(where: ProjectInfo, agent: str, before: str) -> FleetAgent:
    """A coder of ``agent``'s that ran :data:`BEFORE` at ``low``, as ``before`` says it was
    chosen: its spawn's own arguments, the Spawn dialog's fields, or its role's binding."""
    config = load_config()
    config.fleet.roles["coder"] = FleetRoleSettings(worktree=False)
    if before == "bound":
        effort = ["-c", 'model_reasoning_effort="low"'] if agent == "codex" else ["--effort", "low"]
        config.team.profiles["coder"] = RoleLaunchProfile(
            agent=agent, args=["--model", BEFORE[agent], *effort]
        )
    save_config(config)
    if before == "native":
        effort = ["-c", 'model_reasoning_effort="low"'] if agent == "codex" else ["--effort", "low"]
        return fleet_service.spawn(
            where, "coder", agent=agent, agent_args=["--model", BEFORE[agent], *effort]
        ).agent
    if before == "picked":
        return fleet_service.spawn(
            where, "coder", agent=agent, model=BEFORE[agent], effort="low"
        ).agent
    return fleet_service.spawn(where, "coder", agent=agent).agent


@pytest.mark.parametrize("before", ["nothing", "native", "picked", "bound"])
@pytest.mark.parametrize("agent", ["claude-code", "codex"])
def test_fleet_restart_given_a_model_and_an_effort_runs_them_and_so_does_its_next_restart(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    agent: str,
    before: str,
) -> None:
    """(B). ``fleet restart coder-1 --model X --effort medium``, by the CLI, under the server's
    env pins: the replacement's window runs X at medium, each once, over what the agent ran
    (its spawn's arguments, its dialog pick, its binding's), and its ``launch_spec`` records
    them, so a plain restart after it runs them again. For Codex as for Claude Code; never the
    owner's pick (#16). Codex ran neither, and recorded ``model=None, effort=None``."""
    agent_row = _spawn_with(project, agent, before)
    pick = model_memory.remembered(agent=agent)
    _the_servers_env_pins(agent, monkeypatch)
    monkeypatch.chdir(project.root)

    result = CliRunner().invoke(
        app, ["fleet", "restart", agent_row.label, "--model", GIVEN[agent], "--effort", "medium"]
    )

    assert result.exit_code == 0, result.output
    argv = _window_launched(tmux, CliRunner(), monkeypatch)
    assert _ran(agent, argv) == (GIVEN[agent], "medium"), argv
    _each_once(agent, argv)
    _no_env_pin(agent, argv)
    restarted = fleet_service.restart(project, agent_row.label)
    spec = restarted.started.launch_spec
    assert spec is not None
    again = _window_launched(tmux, CliRunner(), monkeypatch)
    assert _ran(agent, again) == (GIVEN[agent], "medium"), "the next restart replays them"
    _each_once(agent, again)
    assert (spec.model, spec.effort) == (GIVEN[agent], "medium"), spec
    assert model_memory.remembered(agent=agent) == pick, "that agent's alone, never the pick"


@pytest.mark.parametrize("given", ["model", "effort"])
@pytest.mark.parametrize("before", ["native", "picked", "bound"])
@pytest.mark.parametrize("agent", ["claude-code", "codex"])
def test_a_restart_given_one_of_the_two_keeps_the_other_as_the_agent_ran_it(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    agent: str,
    before: str,
    given: str,
) -> None:
    """(B), each on its own: a restart given the model alone keeps the effort the agent ran,
    and given the effort alone keeps its model, wherever those came from, a Codex model its
    arguments name included (which takes the effort with it, #129 b: the effort given joins
    them); and the restart after it runs the same."""
    agent_row = _spawn_with(project, agent, before)
    expected = (GIVEN[agent], "low") if given == "model" else (BEFORE[agent], "medium")

    fleet_service.restart(
        project,
        agent_row.label,
        model=GIVEN[agent] if given == "model" else None,
        effort="medium" if given == "effort" else None,
    )

    argv = _window_launched(tmux, CliRunner(), monkeypatch)
    assert _ran(agent, argv) == expected, argv
    _each_once(agent, argv)
    fleet_service.restart(project, agent_row.label)
    again = _window_launched(tmux, CliRunner(), monkeypatch)
    assert _ran(agent, again) == expected, again
    _each_once(agent, again)


@pytest.mark.parametrize("agent", ["claude-code", "codex"])
def test_a_restart_given_an_empty_model_is_refused_for_either_agent(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    agent: str,
) -> None:
    """``fleet restart --model ""`` names nothing, and is refused, for Codex as for Claude
    Code: no window opens for it."""
    agent_row = _spawn_with(project, agent, "nothing")
    spawned = len(tmux.spawned)

    with pytest.raises(fleet_service.FleetError, match="--model needs a value: name one"):
        fleet_service.restart(project, agent_row.label, model="")

    assert len(tmux.spawned) == spawned


def test_a_codex_replay_keeps_its_bindings_effort_where_the_bindings_model_holds_the_record(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Codex replay's recorded model and effort replace its binding's only where its model
    slot passes them. A binding that names the model takes the effort with it (#129 b): the
    effort its spawn recorded from ``[agents.models.codex]``, never passed, does not take the
    binding's own effort away from a restart once the binding names one."""
    config = load_config()
    config.fleet.roles["coder"] = FleetRoleSettings(worktree=False)
    config.team.profiles["coder"] = RoleLaunchProfile(agent="codex", args=["--model", "gpt-bind"])
    config.agents.models["codex"] = AgentModelSettings(effort="high")
    save_config(config)
    agent_row = fleet_service.spawn(project, "coder").agent
    assert _ran("codex", _window_launched(tmux, CliRunner(), monkeypatch)) == ("gpt-bind", None)
    config = load_config()
    config.team.profiles["coder"] = RoleLaunchProfile(
        agent="codex", args=["--model", "gpt-bind", "-c", 'model_reasoning_effort="low"']
    )
    save_config(config)

    fleet_service.restart(project, agent_row.label)

    argv = _window_launched(tmux, CliRunner(), monkeypatch)
    assert _ran("codex", argv) == ("gpt-bind", "low"), argv


#: Efforts Codex's model slot refuses (``CodexAdapter.reasoning_effort``): no level name.
UNTAKEN = ["x y", "extra high", "med.", "hi!"]


#: What a refused restart leaves as it was: the windows opened, the panes killed, the keys typed.
World = tuple[int, list[str], list[tuple[str, str, str]]]


def _the_world(tmux: FakeTmux) -> World:  # noqa: F811
    return len(tmux.spawned), list(tmux.killed), list(tmux.typed)


def _as_it_was(
    tmux: FakeTmux,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    row: FleetAgent,
    before: World,
) -> None:
    """``row``'s agent runs on as it did: no ``/exit`` typed, no window killed or opened, and
    its row is still the live one under its label."""
    assert (len(tmux.spawned), tmux.killed, tmux.typed) == before
    live = fleet_service.list_agents(project, live_only=True)
    assert [(status.agent.id, status.agent.label) for status in live] == [(row.id, row.label)]


@pytest.mark.parametrize("effort", UNTAKEN)
@pytest.mark.parametrize("how", ["cli", "service"])
def test_a_codex_restart_given_an_effort_its_slot_refuses_stops_nothing(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    how: str,
    effort: str,
) -> None:
    """tester-43d on #43 reopen 3; mutant: the check back after the stop. ``fleet restart
    coder-1 --effort "x y"`` of a running Codex coder typed its ``/exit``, ended its row and
    killed its window, and only then refused the effort, which its model slot cannot take
    (``model_args``, in the replay's spawn): the agent was lost, and nothing started. Asked
    before the stop now, as every refusal that does not depend on it is: the agent runs on,
    and the refusal says nothing was stopped."""
    row = _spawn_with(project, "codex", "nothing")
    before = _the_world(tmux)

    if how == "cli":
        monkeypatch.chdir(project.root)
        result = CliRunner().invoke(app, ["fleet", "restart", row.label, "--effort", effort])
        assert result.exit_code == 1, result.output
        said = " ".join(result.output.split())
    else:
        with pytest.raises(fleet_service.FleetError) as refused:
            fleet_service.restart(project, row.label, effort=effort)
        said = str(refused.value)

    assert f"cannot restart {row.label!r}: Codex reasoning effort must be a level name" in said
    assert said.endswith(f"not {effort!r} — nothing was stopped"), said
    _as_it_was(tmux, project, row, before)


def test_a_claude_code_restart_given_any_effort_passes_it_through_as_main_does(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review r30 of #113, n3; tester-43e's mutant E5, the ``model_ladders`` guard dropped.
    The check before the stop is the model slot's, which an agent that keeps its own models
    has (Codex's). Claude Code's ``--effort`` is passed as it is given: ``fleet restart
    coder-1 --effort "x y"`` of a Claude Code coder stops it, starts it and passes ``--effort
    "x y"`` through, once, as main does; it is refused by nothing here."""
    row = _spawn_with(project, "claude-code", "nothing")

    assert fleet_service.restart(project, row.label, effort="x y").was_running

    assert tmux.killed, "it was stopped"
    argv = _window_launched(tmux, CliRunner(), monkeypatch)
    assert _ran("claude-code", argv)[1] == "x y", argv
    assert argv.count("--effort") == 1, argv
    live = fleet_service.list_agents(project, live_only=True)
    assert [status.agent.label for status in live] == [row.label]
    assert [status.agent.id for status in live] != [row.id], "a new row runs under its label"


@pytest.mark.parametrize(("effort", "native"), [("High", "high"), ("ultracode", "max")])
def test_a_codex_restart_given_an_effort_its_slot_takes_runs_it(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    effort: str,
    native: str,
) -> None:
    """The check before the stop refuses what the model slot refuses, and no more: an effort
    in another case, or AISquare's alias for one of Codex's, restarts the agent at that level."""
    row = _spawn_with(project, "codex", "nothing")

    receipt = fleet_service.restart(project, row.label, effort=effort)

    assert receipt.was_running
    argv = _window_launched(tmux, CliRunner(), monkeypatch)
    assert _ran("codex", argv) == (None, native), argv


@pytest.mark.parametrize("replay", ["restart", "switch"])
def test_a_codex_replay_of_an_effort_its_slot_refuses_stops_nothing(
    tmux: FakeTmux,  # noqa: F811
    claude_on_path: Path,  # noqa: F811
    codex_on_path: Path,  # noqa: F811
    project: ProjectInfo,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    replay: str,
) -> None:
    """#43 reopen 4, item 2; mutant: the record's effort left out of the check. A Codex spawn
    whose own arguments name the model passes no effort beside them (that model takes the
    effort with it, #129 b), and records the one it was given unasked; a plain restart, or a
    switch, gives that effort to the model slot, and refused it after the stop. Before it now,
    for both; and a restart given an effort the slot takes replaces it, and runs."""
    first, second = _two_slots("codex", monkeypatch)
    config = load_config()
    config.fleet.roles["coder"] = FleetRoleSettings(worktree=False)
    save_config(config)
    row = fleet_service.spawn(
        project,
        "coder",
        agent="codex",
        account=str(first.slot),
        effort="x y",
        agent_args=["--model", GIVEN["codex"]],
    ).agent
    assert row.launch_spec is not None and row.launch_spec.effort == "x y", row.launch_spec
    _window_launched(tmux, CliRunner(), monkeypatch)
    before = _the_world(tmux)

    with pytest.raises(fleet_service.FleetError, match=r"not 'x y' — nothing was stopped$"):
        if replay == "restart":
            fleet_service.restart(project, row.label)
        else:
            fleet_service.switch(project, row.label, to=str(second.slot))

    _as_it_was(tmux, project, row, before)
    fleet_service.restart(project, row.label, effort="high")
    argv = _window_launched(tmux, CliRunner(), monkeypatch)
    assert _ran("codex", argv) == (GIVEN["codex"], "high"), argv
