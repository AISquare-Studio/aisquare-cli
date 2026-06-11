"""``aisquare policy`` — organisation policies."""

from __future__ import annotations

import typer

from aisquare.services import policy as policy_service

app = typer.Typer(help="View organisation policies.", no_args_is_help=True)


@app.command("list")
def list_() -> None:
    """List policies that apply to this machine."""
    policy_service.list_policies()
