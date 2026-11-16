"""An MCP server an agent keeps in its own config, put there by its own CLI.

``install.sh --with chrome-devtools`` registers the Chrome DevTools MCP with every agent the
run sets up, at user scope, by the agent's own command (``claude mcp add``, ``codex mcp
add``): the file's layout stays the agent's, and a second writer never races a running
session over it. This is the agent's side of that, :attr:`AgentAdapter.mcp_registration`:
the arguments of its add and remove commands, and whether a config dir has the server
already, read from the file the agent writes. ``install.sh`` mirrors the commands and the
survey in shell, which runs before aisquare is installed; ``tests/test_mcp_registration.py``
holds the two to one another.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from aisquare.core.agent_adapters import codex_config

#: The Chrome DevTools MCP (ChromeDevTools/chrome-devtools-mcp), by the name its guide gives
#: it, the package ``npx`` starts and the command that starts it.
DEVTOOLS_NAME = "chrome-devtools"
DEVTOOLS_PACKAGE = "chrome-devtools-mcp"
DEVTOOLS_COMMAND = ("npx", f"{DEVTOOLS_PACKAGE}@latest")


def runs_package(entry: object, package: str) -> bool:
    """Whether the server ``entry`` (a ``command`` and its ``args``, as both agents keep one)
    starts ``package``: from npx, as a scoped fork (``@acme/chrome-devtools-mcp@1.2.0``), or
    as a checkout run by node (``/opt/chrome-devtools-mcp/build/src/index.js``). The name is
    the operator's to choose, so the package counts as well as the name; ``-docs`` after it
    does not."""
    if not isinstance(entry, dict):
        return False
    args = entry.get("args")
    words = [entry.get("command"), *(args if isinstance(args, list) else ())]
    seen = re.compile(rf"(?:^|/){re.escape(package)}(?:[@/]|$)")
    return any(isinstance(word, str) and seen.search(word) for word in words)


def _has(servers: object, name: str, package: str) -> bool:
    return isinstance(servers, dict) and (
        name in servers or any(runs_package(entry, package) for entry in servers.values())
    )


class ClaudeCodeMcp:
    """``claude mcp add NAME --scope user COMMAND``: a server of ``.claude.json``'s top-level
    ``mcpServers``, or the package's plugin enabled in ``settings.json`` (which brings its own,
    and whose guide says not to have both)."""

    def add_args(self, name: str, command: Sequence[str]) -> tuple[str, ...]:
        return ("mcp", "add", name, "--scope", "user", *command)

    def remove_args(self, name: str) -> tuple[str, ...]:
        return ("mcp", "remove", "-s", "user", name)

    def registered(self, home: Path, name: str, package: str) -> bool:
        from aisquare.core import agents

        if any(
            _has(agents.read_json(path).get("mcpServers"), name, package)
            for path in agents.claude_json_paths(home)
        ):
            return True
        plugins = agents.read_json(home / "settings.json").get("enabledPlugins")
        return isinstance(plugins, dict) and any(
            str(plugin).startswith(f"{package}@") and enabled is True
            for plugin, enabled in plugins.items()
        )


class CodexMcp:
    """``codex mcp add NAME -- COMMAND``: a ``[mcp_servers.NAME]`` table of ``config.toml`` in
    ``CODEX_HOME``. Measured with Codex 0.164.0: adding a name it has already REPLACES the
    table and says so no differently (no ``already exists``), so the survey is what keeps an
    operator's own entry (its ``env``, its ``cwd``) from being overwritten."""

    def add_args(self, name: str, command: Sequence[str]) -> tuple[str, ...]:
        return ("mcp", "add", name, "--", *command)

    def remove_args(self, name: str) -> tuple[str, ...]:
        return ("mcp", "remove", name)

    def registered(self, home: Path, name: str, package: str) -> bool:
        return _has(codex_config.read_layer(home / "config.toml").get("mcp_servers"), name, package)
