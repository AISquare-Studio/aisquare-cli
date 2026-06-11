"""``aisquare auth`` — inspect and manage credentials."""

from __future__ import annotations

import typer

from aisquare.services import auth as auth_service

app = typer.Typer(help="Inspect and manage credentials.", no_args_is_help=True)


@app.command("status")
def status() -> None:
    """Show whether this machine is authenticated, and as whom."""
    auth_service.status()


@app.command("rotate")
def rotate() -> None:
    """Rotate the stored API token."""
    auth_service.rotate()


@app.command("token")
def token() -> None:
    """Print the active API token (for scripting)."""
    auth_service.token()
