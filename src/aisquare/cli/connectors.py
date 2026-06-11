"""``aisquare connectors`` — external sources feeding the context pools."""

from __future__ import annotations

from typing import Annotated

import typer

from aisquare.services import connectors as connectors_service

app = typer.Typer(help="Manage connectors to external sources.", no_args_is_help=True)

ConnectorName = Annotated[str, typer.Argument(help="Connector name, e.g. 'notion'.")]


@app.command("list")
def list_() -> None:
    """List available and configured connectors."""
    connectors_service.list_connectors()


@app.command("add")
def add(name: ConnectorName) -> None:
    """Add and configure a connector."""
    connectors_service.add(name)


@app.command("remove")
def remove(name: ConnectorName) -> None:
    """Remove a configured connector."""
    connectors_service.remove(name)


@app.command("status")
def status() -> None:
    """Show connector health and last sync times."""
    connectors_service.status()
