"""The captain's start command, read by ``aisquare launch``'s own argument parser (SMOKE8 8a).

The window runs ``aisquare launch captain <launch options> -- <the agent's arguments>``,
and everything after the ``--`` is the agent's. The captain's ``-e`` pairs had come to sit
after it: Codex died on ``unexpected argument '-e'`` and Claude Code on ``unknown option
'-e'``, every start, while the suite read the pairs from anywhere in the argv and passed.
Here the command is parsed as the window's ``launch`` parses it, for every agent that can
be the captain (``AgentAdapter.tools_only_args`` answers), at a start and at a restart.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.core import TyperGroup
from typer.main import get_command

from aisquare.cli.app import app
from aisquare.core import agent_adapters, launch_seal, paths
from aisquare.core.agent_adapters import codex as codex_module
from aisquare.core.config import RoleLaunchProfile, load_config, save_config
from aisquare.core.orchestrator import TEAM_HUB_ENV_VAR
from aisquare.services import fleet as fleet_service
from aisquare.services.captain import brain
from aisquare.services.captain import state as captain_state
from tests import fakebin
from tests import test_fleet_service as fleet_suite
from tests.test_fleet_service import FakeTmux

tmux = fleet_suite.tmux
clock = fleet_suite.clock


def _confinable() -> list[str]:
    """The agents that answer ``tools_only_args``: the ones that can be the captain."""
    probe = Path("/nonexistent-aisquare-probe")
    return [
        adapter.id
        for adapter in agent_adapters.adapters()
        if adapter.tools_only_args(
            brain.SERVER, "server", [], {}, inherit=(), home=probe, cwd=probe, launch_args=()
        )
        is not None
    ]


def _as_launch_reads(command: object) -> tuple[list[str], list[str]]:
    """``command``'s ``launch …`` parsed by the window's own ``aisquare launch``: its ``-e``
    pairs, and the arguments it hands the agent."""
    assert isinstance(command, list)
    root = get_command(app)
    assert isinstance(root, TyperGroup)
    with root.commands["launch"].make_context(
        "launch", command[command.index("launch") + 1 :]
    ) as parsed:
        return list(parsed.params["env_pairs"] or []), list(parsed.args)


@pytest.fixture
def captain_on_its_agent(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> str:
    agent: str = request.param
    adapter = agent_adapters.get_adapter(agent)
    bin_dir = tmp_path / "bin"
    fakebin.executable_fake(bin_dir, adapter.binary, windows="set /p line=", posix="read line")
    fakebin.prepend_to_path(bin_dir, monkeypatch)
    home = tmp_path / "agent-home"
    home.mkdir()
    monkeypatch.setenv(adapter.home_env, str(home))
    monkeypatch.setattr(codex_module, "SYSTEM_CONFIG", tmp_path / "etc" / "config.toml")
    monkeypatch.setattr(codex_module, "MANAGED_CONFIG", tmp_path / "etc" / "managed.toml")
    config = load_config()
    config.team.profiles["captain"] = RoleLaunchProfile(agent=agent)
    save_config(config)
    return agent


def _hub() -> str:
    return str(captain_state.home_project().root)


def _pairs() -> list[str]:
    return [f"AISQUARE_HOME={paths.aisquare_home().resolve()}", f"{TEAM_HUB_ENV_VAR}={_hub()}"]


@pytest.mark.needs_captain
@pytest.mark.parametrize("captain_on_its_agent", _confinable(), indirect=True)
def test_the_captains_environment_is_launchs_and_never_its_agents(
    tmux: FakeTmux, captain_on_its_agent: str
) -> None:
    agent = brain.start().agent
    pairs, native = _as_launch_reads(tmux.spawned[-1]["command"])
    assert pairs == _pairs()
    assert "-e" not in native and not set(_pairs()) & set(native), native

    # A row recorded with the pairs among its agent's arguments, as the captain's were
    # before they were launch's: its restart is given them once, as launch's.
    record = launch_seal.read(agent.id)
    assert isinstance(record, launch_seal.LaunchRecord)
    old = record.spec.model_copy(
        update={"extra_args": [*brain.launch_args(Path(_hub())), *record.spec.extra_args]}
    )
    assert launch_seal.write(record.model_copy(update={"spec": old})) is None
    fleet_service.restart(captain_state.home_project(), "captain")
    pairs, native = _as_launch_reads(tmux.spawned[-1]["command"])
    assert pairs == _pairs()
    assert "-e" not in native, native


@pytest.mark.needs_captain
def test_both_agents_can_be_the_captain() -> None:
    """The parametrization above is not vacuous: Claude Code and Codex confine."""
    assert {"claude-code", "codex"} <= set(_confinable())


#: The agent arguments 0.8.2's ``brain.launch_args`` gave the captain, which its row's spec
#: recorded and the census carried over: its environment, and its confinement as it was
#: then, a ``--mcp-config`` naming the ``mcp.json`` that release wrote.
def _recorded_by_0_8_2() -> list[str]:
    return [
        *brain.launch_args(Path(_hub())),
        "--strict-mcp-config",
        "--mcp-config",
        str(paths.aisquare_home() / "captain" / "mcp.json"),
        "--tools",
        "",
        "--allowedTools",
        f"mcp__{brain.SERVER}__*",
    ]


def _per_start(native: list[str]) -> list[str]:
    """``native`` less what each launch gives anew: Claude Code's ``--session-id``, and the
    restart's prompt, which Codex takes after a ``--``."""
    kept = native[: native.index("--")] if "--" in native else list(native)
    if "--session-id" in kept:
        at = kept.index("--session-id")
        del kept[at : at + 2]
    return kept


@pytest.mark.needs_captain
@pytest.mark.parametrize("captain_on_its_agent", _confinable(), indirect=True)
def test_a_captain_row_0_8_2_recorded_restarts_with_todays_arguments_alone(
    tmux: FakeTmux, captain_on_its_agent: str
) -> None:
    """Review r17 of #113, finding 6: a captain's agent arguments are derived at every
    launch, never replayed. A 0.8.2 row replayed its old confinement after today's, two
    ``--mcp-config`` values, one a frozen ``mcp.json`` (no start at all once that file is
    gone)."""
    agent = brain.start().agent
    _, fresh = _as_launch_reads(tmux.spawned[-1]["command"])
    record = launch_seal.read(agent.id)
    assert isinstance(record, launch_seal.LaunchRecord)
    old = record.spec.model_copy(update={"extra_args": _recorded_by_0_8_2(), "recorded_by": None})
    assert launch_seal.write(record.model_copy(update={"spec": old})) is None

    fleet_service.restart(captain_state.home_project(), "captain")

    pairs, native = _as_launch_reads(tmux.spawned[-1]["command"])
    assert pairs == _pairs()
    assert _per_start(native) == _per_start(fresh), native
    assert "mcp.json" not in " ".join(native)
