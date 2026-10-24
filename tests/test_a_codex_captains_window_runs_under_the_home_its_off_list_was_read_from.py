"""A Codex captain's window runs under the home its MCP off-list was read from.

Review r17 of #113, finding 8 (1a70ba4f). With no account chosen, the Codex captain's
off-list (``-c mcp_servers.<name>.enabled=false`` for each server its config layers define)
was read from the SPAWNER's ``CODEX_HOME``, while the window ran under the tmux server's
environment, which carries no spawner's (a Codex tool shell's is stripped, 262f216f; a
server started from another shell never had it). A server named only in the spawner's home
was switched off where the window's config does not define it, and Codex refused that config
("invalid transport in `mcp_servers.<name>`", measured on 0.162.1 and 0.164.0): the captain
never started. The window is now given the home its tools were confined against.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from aisquare.core.agent_adapters import codex_config
from aisquare.core.config import RoleLaunchProfile, load_config, save_config
from aisquare.services.captain import brain
from tests import test_fleet_service as fleet_suite
from tests.test_codex_account_switch import codex_on_path  # noqa: F401 - fixture, by name
from tests.test_fleet_service import FakeTmux

tmux = fleet_suite.tmux
clock = fleet_suite.clock
claude_on_path = fleet_suite.claude_on_path


def _switched_off(native: list[str]) -> set[str]:
    return {
        word.removeprefix("mcp_servers.").partition(".")[0]
        for word in native
        if word.startswith("mcp_servers.") and word.endswith(".enabled=false")
    }


def _given_a_command(native: list[str]) -> set[str]:
    return {
        word.removeprefix("mcp_servers.").partition(".")[0]
        for word in native
        if word.startswith("mcp_servers.") and ".command=" in word
    }


@pytest.mark.needs_captain
def test_a_server_only_the_spawners_home_names_is_switched_off_where_the_window_has_it(
    tmux: FakeTmux,
    codex_on_path: Path,  # noqa: F811 - the fixture, by name
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawners = tmp_path / "codex-accounts" / "5"
    spawners.mkdir(parents=True)
    (spawners / "config.toml").write_text(
        '[mcp_servers.linear]\ncommand = "/opt/linear"\n', encoding="utf-8"
    )
    monkeypatch.setenv("CODEX_HOME", str(spawners))
    monkeypatch.setattr(codex_config, "SYSTEM_CONFIG", tmp_path / "etc" / "config.toml")
    monkeypatch.setattr(codex_config, "MANAGED_CONFIG", tmp_path / "etc" / "managed.toml")
    config = load_config()
    config.team.profiles["captain"] = RoleLaunchProfile(agent="codex")
    save_config(config)

    brain.start()

    command, env = tmux.spawned[-1]["command"], tmux.spawned[-1]["env"]
    assert isinstance(command, list) and isinstance(env, dict)
    native = command[command.index("--") + 1 :]
    assert _switched_off(native) >= {"linear"}, native
    # The window's home: the one it is given, else the tmux server's, which holds none here.
    window_home = Path(env.get("CODEX_HOME") or Path.home() / ".codex")
    layer = window_home / "config.toml"
    defined = (
        set(tomllib.loads(layer.read_text("utf-8")).get("mcp_servers", {}))
        if layer.is_file()
        else set()
    )
    unknown = _switched_off(native) - defined - _given_a_command(native)
    assert not unknown, f"switched off where {window_home} defines none: {sorted(unknown)}"


@pytest.mark.needs_captain
def test_a_claude_code_captains_window_is_not_given_a_home(
    tmux: FakeTmux, claude_on_path: Path
) -> None:
    """The control: Claude Code's ``CLAUDE_CONFIG_DIR`` stays as the shell wrote it
    (``exports_home_env``), and its confinement reads no home."""
    brain.start()
    env = tmux.spawned[-1]["env"]
    assert isinstance(env, dict) and "CLAUDE_CONFIG_DIR" not in env
