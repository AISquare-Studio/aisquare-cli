"""An agent registers an MCP server through its own CLI, and its adapter says how.

``install.sh --with chrome-devtools`` registers the Chrome DevTools MCP with every agent the
run sets up (r23 of #113, M1). What each agent's CLI is asked (``mcp_registration.add_args``)
and where its config says the server is (``registered``) are the adapter's, so a third agent
gets the extra by declaring them; ``tests/test_install_script_functions.py`` holds install.sh's
shell copy of the commands and of the survey to these. Measured with Claude Code 2.1.310 and
Codex 0.164.0 (``codex mcp add chrome-devtools -- npx chrome-devtools-mcp@latest`` writes
``[mcp_servers.chrome-devtools]`` with ``command`` and ``args`` into ``CODEX_HOME/config.toml``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aisquare.core import agents
from aisquare.core.agent_adapters import adapters, get_adapter
from aisquare.core.agent_adapters.mcp_servers import (
    DEVTOOLS_COMMAND,
    DEVTOOLS_NAME,
    DEVTOOLS_PACKAGE,
    runs_package,
)
from aisquare.core.agent_adapters.types import AgentAdapter
from aisquare.services import diagnostics
from tests.test_adapter_contract import SYNTHETIC

CODEX = get_adapter("codex")
CLAUDE = get_adapter("claude-code")


def _declaring() -> list[AgentAdapter]:
    return [
        adapter
        for adapter in (*adapters(), *(cls() for cls in SYNTHETIC))
        if adapter.mcp_registration is not None
    ]


def test_the_agents_that_register_an_mcp_are_the_ones_that_declare_it() -> None:
    declaring = {adapter.id for adapter in _declaring()}
    synthetic = [cls() for cls in SYNTHETIC]

    assert {"claude-code", "codex"} <= declaring, "both real agents take the extra"
    assert any(agent.mcp_registration is None for agent in synthetic), "an agent without the slot"
    assert any(agent.id in declaring for agent in synthetic), "a third agent takes it by declaring"


@pytest.mark.parametrize("adapter", _declaring(), ids=lambda adapter: adapter.id)
def test_every_declared_registration_carries_the_name_and_the_command(
    adapter: AgentAdapter, tmp_path: Path
) -> None:
    registration = adapter.mcp_registration
    assert registration is not None

    add = registration.add_args(DEVTOOLS_NAME, DEVTOOLS_COMMAND)
    remove = registration.remove_args(DEVTOOLS_NAME)

    assert add[0] == "mcp" and DEVTOOLS_NAME in add
    assert add[-len(DEVTOOLS_COMMAND) :] == DEVTOOLS_COMMAND, "the command is last, as typed"
    assert remove[0] == "mcp" and DEVTOOLS_NAME in remove
    # A home with nothing in it, or a config that is not the agent's format, has no server.
    home = tmp_path / adapter.home_name
    assert not registration.registered(home, DEVTOOLS_NAME, DEVTOOLS_PACKAGE)
    home.mkdir()
    for name in ("config.toml", "settings.json", ".claude.json"):
        (home / name).write_text("{ not: [valid", encoding="utf-8")
    assert not registration.registered(home, DEVTOOLS_NAME, DEVTOOLS_PACKAGE)


def test_the_commands_are_the_agents_own_spelling() -> None:
    """Claude Code keeps it at user scope and Codex has one global config; its add takes the
    command after ``--``, and each agent's remove is its own."""
    assert CLAUDE.mcp_registration is not None and CODEX.mcp_registration is not None

    assert CLAUDE.mcp_registration.add_args("n", ("npx", "p")) == (
        "mcp",
        "add",
        "n",
        "--scope",
        "user",
        "npx",
        "p",
    )
    assert CODEX.mcp_registration.add_args("n", ("npx", "p")) == (
        "mcp",
        "add",
        "n",
        "--",
        "npx",
        "p",
    )
    assert CLAUDE.mcp_registration.remove_args("n") == ("mcp", "remove", "-s", "user", "n")
    assert CODEX.mcp_registration.remove_args("n") == ("mcp", "remove", "n")


@pytest.mark.parametrize(
    ("entry", "runs"),
    [
        ({"command": "npx", "args": ["chrome-devtools-mcp@latest"]}, True),
        ({"command": "npx", "args": ["-y", "chrome-devtools-mcp@latest"]}, True),
        ({"command": "npx", "args": ["@acme/chrome-devtools-mcp@1.2.0"]}, True),
        ({"command": "node", "args": ["/opt/chrome-devtools-mcp/build/src/index.js"]}, True),
        ({"command": "chrome-devtools-mcp"}, True),
        ({"command": "npx", "args": ["chrome-devtools-mcp-docs"]}, False),
        ({"command": "node", "args": ["/opt/chrome-devtools-mcp-docs/index.js"]}, False),
        ({"command": "npx", "args": ["@acme/my-chrome-devtools-mcp@1.2.0"]}, False),
        ({"url": "https://example.invalid/mcp"}, False),
        ({"command": "npx", "args": "chrome-devtools-mcp"}, False),
        ("chrome-devtools-mcp", False),
        (None, False),
    ],
)
def test_a_server_that_runs_the_package_is_the_package_under_any_name(
    entry: object, runs: bool
) -> None:
    assert runs_package(entry, DEVTOOLS_PACKAGE) is runs


def test_codex_has_the_server_by_its_name_or_by_what_it_runs(tmp_path: Path) -> None:
    registration = CODEX.mcp_registration
    assert registration is not None
    config = tmp_path / "config.toml"

    config.write_text("[mcp_servers.chrome-devtools]\ncommand = 'x'\n", encoding="utf-8")
    assert registration.registered(tmp_path, DEVTOOLS_NAME, DEVTOOLS_PACKAGE)

    config.write_text(
        '[mcp_servers.mine]\ncommand = "npx"\nargs = ["chrome-devtools-mcp@latest"]\n',
        encoding="utf-8",
    )
    assert registration.registered(tmp_path, DEVTOOLS_NAME, DEVTOOLS_PACKAGE)

    config.write_text('[mcp_servers.github]\nurl = "https://example.invalid/mcp"\n', "utf-8")
    assert not registration.registered(tmp_path, DEVTOOLS_NAME, DEVTOOLS_PACKAGE)


def test_claude_code_has_the_server_in_its_json_or_through_the_plugin(tmp_path: Path) -> None:
    """``~/.claude.json`` for the default dir (where ``claude mcp add`` writes), and the
    package's plugin enabled in ``settings.json`` (which brings its own server)."""
    registration = CLAUDE.mcp_registration
    assert registration is not None
    user = agents.home()  # the suite's autouse fixture puts it in the test's folder
    home = user / ".claude"
    home.mkdir(parents=True)
    assert not registration.registered(home, DEVTOOLS_NAME, DEVTOOLS_PACKAGE)

    (user / ".claude.json").write_text(
        json.dumps({"mcpServers": {DEVTOOLS_NAME: {"command": "npx"}}}), encoding="utf-8"
    )
    assert registration.registered(home, DEVTOOLS_NAME, DEVTOOLS_PACKAGE)

    # A project's own server is not the user's.
    (user / ".claude.json").write_text(
        json.dumps({"projects": {"/p": {"mcpServers": {DEVTOOLS_NAME: {"command": "npx"}}}}}),
        encoding="utf-8",
    )
    assert not registration.registered(home, DEVTOOLS_NAME, DEVTOOLS_PACKAGE)

    (home / "settings.json").write_text(
        json.dumps({"enabledPlugins": {f"{DEVTOOLS_PACKAGE}@plugins": False}}), encoding="utf-8"
    )
    assert not registration.registered(home, DEVTOOLS_NAME, DEVTOOLS_PACKAGE)
    (home / "settings.json").write_text(
        json.dumps({"enabledPlugins": {f"{DEVTOOLS_PACKAGE}@plugins": True}}), encoding="utf-8"
    )
    assert registration.registered(home, DEVTOOLS_NAME, DEVTOOLS_PACKAGE)


def test_codexs_browser_tools_name_the_registered_server(tmp_path: Path) -> None:
    """The doctor's browser row reads a Codex ui-tester's own tools: with ``browser_use`` off
    and the MCP registered, it has the MCP, not "no browser of its own"."""
    (tmp_path / "config.toml").write_text(
        "[features]\nbrowser_use = false\n\n[mcp_servers.chrome-devtools]\ncommand = 'npx'\n",
        encoding="utf-8",
    )

    assert CODEX.browser_tools(tmp_path) == [f"the {DEVTOOLS_NAME} MCP server"]

    (tmp_path / "config.toml").write_text("[features]\nbrowser_use = false\n", encoding="utf-8")
    assert CODEX.browser_tools(tmp_path) == []


def test_the_doctor_tells_a_browserless_codex_ui_tester_how_to_register_the_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aisquare.core.config import RoleLaunchProfile, load_config, save_config

    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text("[features]\nbrowser_use = false\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(home))
    settings = load_config()
    settings.team.profiles["ui-tester"] = RoleLaunchProfile(agent="codex")
    save_config(settings)

    row = diagnostics._check_browser_tools(tmp_path)

    assert row.status.value == "ok" and "no browser of its own" in row.detail
    assert "codex mcp add chrome-devtools -- npx chrome-devtools-mcp@latest" in row.detail
