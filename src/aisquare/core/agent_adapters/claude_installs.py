"""What a Claude Code config dir keeps of aisquare's in Claude Code's own files.

:meth:`ClaudeCodeAdapter.lasting_installs`, read for ``aisquare uninstall``
(``services.lifecycle``): the MCP servers that run aisquare in the ``.claude.json`` the
dir uses (user scope, and each project's local scope), in the ``.mcp.json`` of every
project that file lists (project scope), and the aisquare plugin enabled there or in a
repository. Listed, never edited: ``.claude.json`` is Claude Code's own file, rewritten
by every running session, and a second writer racing them is how a login gets lost.
``claude mcp remove`` and ``claude plugin uninstall`` are the tools that own them.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from aisquare.core import agents
from aisquare.core import claude_accounts as accounts_core
from aisquare.core.agent_adapters.lasting import fetches_aisquare, runs_aisquare
from aisquare.core.agent_adapters.types import LastingInstall
from aisquare.core.shell_line import command_line

#: The variable ``claude`` reads its ``.claude.json`` by (``CLAUDE_CONFIG_DIR``).
_CONFIG_DIR_VAR = accounts_core.CONFIG_DIR_VAR


def lasting_installs(home: Path) -> tuple[LastingInstall, ...]:
    """aisquare's MCP servers and plugin as config dir ``home`` keeps them. Read-only;
    a file that cannot be read keeps nothing (``agents.read_json``)."""
    return (*_mcp_servers(home), *_plugins(home))


def _mcp_servers(home: Path) -> list[LastingInstall]:
    found: list[LastingInstall] = []
    default = agents.dir_identity(accounts_core.home_config_dir().parent / ".claude.json")
    read: set[Path] = set()
    for spelled in agents.claude_json_paths(home):
        # The file itself: a slot linked to ~/.claude, or a ~/.claude* sibling that is a
        # link, reaches it twice.
        path = agents.dir_identity(spelled)
        if path in read:
            continue
        read.add(path)
        # Where `claude` reads it: ~/.claude.json with CLAUDE_CONFIG_DIR unset, any other
        # .claude.json with the variable naming its own directory (which outlives a purge
        # as the file does), and one linked to a file of another name through the
        # directory it was found in.
        if path == default:
            reach: Path | None = None
        elif path.name == ".claude.json":
            reach = path.parent
        else:
            reach = home
        data = agents.read_json(path)
        found.extend(_entries(data.get("mcpServers"), path, home, reach, None))
        projects = data.get("projects")
        if not isinstance(projects, dict):
            continue
        for project, block in projects.items():
            if isinstance(block, dict):
                found.extend(_entries(block.get("mcpServers"), path, home, reach, str(project)))
                found.extend(_project_servers(str(project), block, path, home))
    return found


def _entries(
    servers: object, path: Path, home: Path, reach: Path | None, project: str | None
) -> list[LastingInstall]:
    """The servers of one ``mcpServers`` table of ``path`` that run aisquare: user scope,
    or ``project``'s local scope."""
    if not isinstance(servers, dict):
        return []
    return [
        LastingInstall(
            accounts_core.AGENT,
            "mcp",
            str(name),
            path,
            home,
            mcp_removal(str(name), project=project, config_dir=reach),
            fetches=fetches_aisquare(spec),
            project=project,
            scope="user" if project is None else "local",
        )
        for name, spec in servers.items()
        if runs_aisquare(spec)
    ]


def _project_servers(
    project: str, block: dict[str, Any], listed_in: Path, home: Path
) -> list[LastingInstall]:
    """aisquare's MCP servers in the ``.mcp.json`` of a project ``listed_in`` lists, which
    its ``block`` there has not declined — project scope.

    A repository's committed ``.mcp.json`` starts its servers in every session there: one
    running aisquare through ``uvx`` made the purged home again at the next session in that
    repository (review of #257). Each block answers for the sessions Claude Code starts
    under its config dir and its spelling of the project, and one that has not answered
    asks at its next session there, where an approval starts the server; so a server one
    file declined is still another's to start (round 26), and the uninstall plan merges a
    project's entries across files, each keeping the files that start it (``asked_in``).
    A block declines a name in its ``disabledMcpjsonServers`` and not in its
    ``enabledMcpjsonServers``, as the doctor reads one
    (``diagnostics._declined_project_servers``)."""
    if not Path(project).is_absolute():
        return []  # another platform's path, or none: read here, it would name the cwd's
    path = agents.dir_identity(Path(project) / ".mcp.json")
    servers = agents.read_json(path).get("mcpServers")
    if not isinstance(servers, dict):
        return []
    return [
        LastingInstall(
            accounts_core.AGENT,
            "mcp",
            str(name),
            path,
            home,
            mcp_removal(str(name), project=project, config_dir=None, shared=True),
            fetches=fetches_aisquare(spec),
            project=project,
            scope="project",
            shared=True,
            asked_in=(listed_in,),
        )
        for name, spec in servers.items()
        if runs_aisquare(spec) and not _declines(block, str(name))
    ]


def _declines(block: dict[str, Any], name: str) -> bool:
    """Whether one ``.claude.json`` project block declined the ``.mcp.json`` server
    ``name``: listed in its ``disabledMcpjsonServers`` and not in its
    ``enabledMcpjsonServers`` (a name in both is approved, as the doctor reads it)."""

    def named(field: str) -> bool:
        names = block.get(field)
        return isinstance(names, list) and name in (str(item) for item in names)

    return named("disabledMcpjsonServers") and not named("enabledMcpjsonServers")


def mcp_removal(
    name: str, *, project: str | None, config_dir: Path | None, shared: bool = False
) -> str:
    """The command that removes the MCP server ``name``: ``claude mcp remove`` at its scope
    (``project``, local or user), aimed at the ``.claude.json`` that holds it, and for a
    project's entry run from inside the project. ``config_dir`` is the
    ``CLAUDE_CONFIG_DIR`` under which ``claude`` reads that file, ``None`` for
    ``~/.claude.json``, which it reads with that variable unset.

    ``claude mcp remove`` acts on the file ``CLAUDE_CONFIG_DIR`` names (``~/.claude.json``
    when it is unset), and its local scope is the project it runs in; its project scope
    is that project's ``.mcp.json``, whatever the variable names. Bare, it answered
    "No MCP server named …" for a project's entry run from elsewhere and for one in
    another config dir, and left both (measured on Claude Code 2.1.296, review of #257).
    Built as ``agents.claude_plugin_command`` builds the plugin's: nothing for the file
    this shell's ``claude`` already reads, ``env -u`` for ``~/.claude.json`` from a shell
    that names another, and the directory otherwise, every part quoted. Native Windows has
    no ``env -u``, ``VAR=value command`` or (in PowerShell 5.1) ``&&``, so there the folder
    and the variable the command needs are said after it.
    """
    scope = "project" if shared else "user" if project is None else "local"
    command = command_line(["claude", "mcp", "remove", "--scope", scope, name])
    ambient = os.environ.get(_CONFIG_DIR_VAR, "").strip()
    here = agents.dir_identity(Path(ambient)) if ambient else None
    if shared:
        # Project scope is the `.mcp.json` of the directory it runs in, whatever config dir
        # the shell names: only the project is needed.
        there = here
    elif config_dir is None:
        there = None
    else:
        there = agents.dir_identity(config_dir)
    if sys.platform == "win32":
        needs = [] if project is None else [f"in {project}"]
        if there != here:
            needs.append(
                f"with {_CONFIG_DIR_VAR} unset"
                if config_dir is None
                else f"with {_CONFIG_DIR_VAR} set to {config_dir}"
            )
        return f"{command} (run it {', '.join(needs)})" if needs else command
    if there == here:
        line = command
    elif config_dir is None:
        line = f"env -u {_CONFIG_DIR_VAR} {command}"
    else:
        line = f"{_CONFIG_DIR_VAR}={command_line([str(config_dir)])} {command}"
    if project is None:
        return line
    return f"cd {command_line([project])} && {line}"


def _plugins(home: Path) -> list[LastingInstall]:
    """The aisquare plugin wherever ``home`` enables it: in the dir itself (user scope), and
    in each repository a project- or local-scope install of its enables (sweep of #257:
    those were never read, so --purge went ahead and the next session in the repository
    made the home again). Its launcher fetches aisquare through uvx once the package is
    gone. Empty where the plugin route does not run (``agents.plugin_route_supported``)."""
    if not agents.plugin_route_supported():
        return []
    user = agents.claude_plugin(home)
    installs = [*([user] if user is not None else []), *agents.claude_repo_plugins(home)]
    records = agents.dir_identity(home) / "plugins" / "installed_plugins.json"
    return [
        LastingInstall(
            accounts_core.AGENT,
            "plugin",
            agents.CLAUDE_PLUGIN_ID,
            records,
            install.config_dir,
            agents.claude_plugin_command(
                "uninstall", install.config_dir, scope=install.scope, project=install.project
            ),
            fetches=True,
            project=None if install.project is None else str(install.project),
            scope=install.scope,
            version=install.version,
        )
        for install in installs
    ]
