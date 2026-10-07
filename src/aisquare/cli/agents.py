"""``aisquare agents`` — detect and connect coding agents."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from aisquare.cli.common import emit_agents, emit_connected, emit_disconnected, fail
from aisquare.core.console import stderr_console
from aisquare.services import agents as agents_service

app = typer.Typer(help="Detect and connect coding agents.", no_args_is_help=True)

AgentName = Annotated[str, typer.Argument(help="Agent name, e.g. 'claude-code'.")]


@app.command("list")
def list_() -> None:
    """List supported agents and whether they are connected."""
    emit_agents(agents_service.list_agents())


@app.command("scan")
def scan() -> None:
    """Scan this machine for installed agents."""
    emit_agents(agents_service.scan())


@app.command("status")
def status(
    name: Annotated[str | None, typer.Argument(help="Agent to inspect (default: all).")] = None,
) -> None:
    """Show integration health for one agent, or all of them."""
    try:
        agents = agents_service.status(name)
    except KeyError:
        fail(f"unknown agent: {name}", error="unknown_agent", ref=name)
    emit_agents(agents)


ConfigDir = Annotated[
    Path | None,
    typer.Option(
        "--config-dir",
        help="Claude Code config directory to target (for CLAUDE_CONFIG_DIR "
        "installs, e.g. ~/.claude4). Default: $CLAUDE_CONFIG_DIR or ~/.claude.",
    ),
]


@app.command("connect")
def connect(name: AgentName, config_dir: ConfigDir = None) -> None:
    """Connect an agent: install aisquare's hooks and ingest its existing context."""
    try:
        connection = agents_service.connect(name, config_dir)
    except KeyError:
        fail(f"unknown agent: {name}", error="unknown_agent", ref=name)
    except agents_service.UnsupportedAgentError as exc:
        fail(str(exc), error="unsupported_agent", ref=name)
    except agents_service.AgentFileUnreadableError as exc:
        # `detail` too: under --json `fail` drops the message, and asq's Connect
        # button shows the error with its detail, so the file is named there as well.
        fail(str(exc), error="agent_file_unreadable", ref=name, detail=str(exc))
    except agents_service.AgentNotInstalledError as exc:
        fail(str(exc), error="not_installed", ref=name)
    emit_connected(connection)
    plugin = agents_service.claude_plugin(config_dir) if name == "claude-code" else None
    if plugin is not None:
        stderr_console().print(
            f"note: the aisquare plugin is enabled in {plugin.config_dir} too — its hooks "
            "stand down while these run; keep one route (aisquare doctor says how)"
        )


@app.command("disconnect")
def disconnect(name: AgentName, config_dir: ConfigDir = None) -> None:
    """Disconnect an agent (its already-imported context is kept)."""
    try:
        removed = agents_service.disconnect(name, config_dir)
    except KeyError:
        fail(f"unknown agent: {name}", error="unknown_agent", ref=name)
    plugin = agents_service.claude_plugin(config_dir) if name == "claude-code" else None
    if not removed and plugin is None:
        stderr_console().print(
            "note: no aisquare hooks found in that config dir — if you connected "
            "with --config-dir, disconnect with the same one"
        )
    if plugin is not None:
        # The plugin's hooks stand down only while settings.json runs them, so
        # removing these hands every event to the plugin rather than stopping it.
        stderr_console().print(
            f"note: the aisquare plugin is still enabled in {plugin.config_dir}, so aisquare "
            f"keeps running there — to stop it: "
            f"{agents_service.claude_plugin_command('disable', plugin.config_dir)}"
        )
    emit_disconnected(name)
