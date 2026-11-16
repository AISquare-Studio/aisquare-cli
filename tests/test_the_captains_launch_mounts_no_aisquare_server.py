"""The captain's window's ``aisquare launch`` gives its agent no server but the captain's own.

Review r17 of #113, finding 3 (merge 0557415d). Every fleet window's launch appended
``agent_launch.mcp_args``, the captain's included, so with ``[agents] mcp = true`` (opt-in)
the captain's session got the aisquare server beside its own: on Claude Code a second
``--mcp-config``, which ``--strict-mcp-config`` loads since it came from a flag; on Codex
``-c mcp_servers.aisquare.*``, which its off-list never names. Here the captain's real start
command, as the fleet hands it to its window, is run through ``aisquare launch`` with the
window's environment, for each agent that can be the captain.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli import launch as launch_cli
from aisquare.cli.app import app
from aisquare.core.config import load_config, save_config
from aisquare.services.captain import brain
from tests import test_the_captains_start_command_parses_as_launch_reads_it as start_suite
from tests.test_fleet_service import FakeTmux

tmux = start_suite.tmux
clock = start_suite.clock
captain_on_its_agent = start_suite.captain_on_its_agent


def _servers(argv: list[str]) -> set[str]:
    """The MCP servers ``argv`` mounts, by name: every ``--mcp-config``'s, and every
    ``mcp_servers.<name>.command`` a ``-c`` sets and none switches off (Codex's own TUI
    server, named only to be off)."""
    named: set[str] = set()
    off: set[str] = set()
    for at, word in enumerate(argv[:-1]):
        if word == "--mcp-config":
            named |= set(json.loads(argv[at + 1])["mcpServers"])
        elif word == "-c" and argv[at + 1].startswith("mcp_servers."):
            name, _, rest = argv[at + 1].removeprefix("mcp_servers.").partition(".")
            if rest.startswith("command="):
                named.add(name)
            elif rest == "enabled=false":
                off.add(name)
    return named - off


@pytest.mark.needs_captain
@pytest.mark.parametrize("captain_on_its_agent", start_suite._confinable(), indirect=True)
def test_with_agents_mcp_on_the_captains_launch_mounts_its_own_server_alone(
    tmux: FakeTmux,
    captain_on_its_agent: str,
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_config()
    config.agents.mcp = True
    save_config(config)
    brain.start()
    spawned = tmux.spawned[-1]
    command, env = spawned["command"], spawned["env"]
    assert isinstance(command, list) and isinstance(env, dict)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(str(spawned["cwd"]))
    captured: dict[str, Any] = {}

    def fake_exec(binary: str, argv: list[str], env: dict[str, str]) -> None:
        captured.update(binary=binary, argv=argv, env=env)

    monkeypatch.setattr(launch_cli, "_exec", fake_exec)

    result = runner.invoke(app, command[command.index("launch") :])

    assert result.exit_code == 0, result.output
    argv = captured["argv"]
    assert _servers(argv) == {brain.SERVER}, argv
    assert argv.count("--mcp-config") <= 1, argv
    assert not any(word.startswith("mcp_servers.aisquare.") for word in argv), argv
