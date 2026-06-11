"""``aisquare enforce`` — local policy enforcement."""

from __future__ import annotations

import typer

from aisquare.services import policy as policy_service

app = typer.Typer(help="Control local policy enforcement.", no_args_is_help=True)


@app.command("status")
def status() -> None:
    """Show whether enforcement is active."""
    policy_service.enforcement_status()


@app.command("enable")
def enable() -> None:
    """Enable policy enforcement on this machine."""
    policy_service.enable_enforcement()


@app.command("disable")
def disable() -> None:
    """Disable policy enforcement on this machine."""
    policy_service.disable_enforcement()
