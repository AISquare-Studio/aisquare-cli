"""``aisquare agents`` — detect and connect coding agents."""

from __future__ import annotations

from typing import Annotated

import typer

from aisquare.services import agents as agents_service

app = typer.Typer(help="Detect and connect coding agents.", no_args_is_help=True)

AgentName = Annotated[str, typer.Argument(help="Agent name, e.g. 'claude-code'.")]


@app.command("list")
def list_() -> None:
    """List supported agents and whether they are connected."""
    agents_service.list_agents()


@app.command("connect")
def connect(name: AgentName) -> None:
    """Connect an agent by installing the aisquare hook into it."""
    agents_service.connect(name)


@app.command("disconnect")
def disconnect(name: AgentName) -> None:
    """Disconnect an agent and remove its hook."""
    agents_service.disconnect(name)


@app.command("scan")
def scan() -> None:
    """Scan this machine for installed agents."""
    agents_service.scan()


@app.command("status")
def status(
    name: Annotated[str | None, typer.Argument(help="Agent to inspect (default: all).")] = None,
) -> None:
    """Show integration health for one agent, or all of them."""
    agents_service.status(name)
