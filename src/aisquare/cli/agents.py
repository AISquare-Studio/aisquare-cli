"""``aisquare agents`` — detect and connect coding agents."""

from __future__ import annotations

from typing import Annotated

import typer

from aisquare.cli.common import emit_agents, emit_connected, emit_disconnected, fail
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


@app.command("connect")
def connect(name: AgentName) -> None:
    """Connect an agent: install aisquare's hooks and ingest its existing context."""
    try:
        connection = agents_service.connect(name)
    except KeyError:
        fail(f"unknown agent: {name}", error="unknown_agent", ref=name)
    except ValueError as exc:
        fail(str(exc), error="not_installed", ref=name)
    emit_connected(connection)


@app.command("disconnect")
def disconnect(name: AgentName) -> None:
    """Disconnect an agent (its already-imported context is kept)."""
    try:
        agents_service.disconnect(name)
    except KeyError:
        fail(f"unknown agent: {name}", error="unknown_agent", ref=name)
    emit_disconnected(name)
