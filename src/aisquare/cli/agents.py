"""``aisquare agents`` — detect and connect coding agents."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from aisquare.cli.common import emit_agents, emit_connected, emit_disconnected, fail
from aisquare.core.console import stderr_console, stdout_console
from aisquare.core.state import get_state
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
    beside = agents_service.plugin_beside_note(name, config_dir)
    if beside is not None:
        stderr_console().print(f"note: {beside}")


@app.command("disconnect")
def disconnect(name: AgentName, config_dir: ConfigDir = None) -> None:
    """Disconnect an agent (its already-imported context is kept)."""
    refusal = agents_service.disconnect_refusal(name, config_dir)
    if refusal is not None:
        # Before anything is touched: hooks it cannot take out keep their record too.
        fail(refusal, error="agent_file_unreadable", ref=name, detail=refusal)
    try:
        removed = agents_service.disconnect(name, config_dir)
    except KeyError:
        fail(f"unknown agent: {name}", error="unknown_agent", ref=name)
    for note in agents_service.disconnect_notes(name, config_dir, removed=removed):
        stderr_console().print(f"note: {note}")
    emit_disconnected(name)


@app.command("refresh-hooks", hidden=True)
def refresh_hooks(name: AgentName, config_dir: ConfigDir = None) -> None:
    """Rewrite aisquare's hooks for this version and import nothing.

    Plumbing for ``aisquare upgrade``, which runs it in the NEW install for each
    directory it re-connects. Kept hidden: ``agents connect`` is the command a
    person types. Later releases must keep it, or an upgrade from this one
    cannot refresh hooks.
    """
    try:
        written = agents_service.refresh_hooks(name, config_dir)
    except KeyError:
        fail(f"unknown agent: {name}", error="unknown_agent", ref=name)
    except agents_service.AgentFileUnreadableError as exc:
        fail(str(exc), error="agent_file_unreadable", ref=name, detail=str(exc))
    except ValueError as exc:
        fail(str(exc), error="not_installed", ref=name)
    if not written:
        fail(f"{name} has no hooks for aisquare to write", error="no_hooks", ref=name)
    if get_state().json_output:
        typer.echo(json.dumps({"name": name, "hooks_installed": True}))
    else:
        stdout_console().print(f"✓ hooks rewritten for {name}")
