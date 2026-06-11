"""``aisquare config`` — read and write configuration."""

from __future__ import annotations

from typing import Annotated

import typer

from aisquare.models import RedactionLevel
from aisquare.services import settings as settings_service

app = typer.Typer(help="Read and write aisquare configuration.", no_args_is_help=True)


@app.command("list")
def list_() -> None:
    """Print the fully-resolved configuration."""
    settings_service.list_values()


@app.command("get")
def get(
    key: Annotated[str, typer.Argument(help="Dotted key, e.g. 'redaction.level'.")],
) -> None:
    """Print a single configuration value."""
    settings_service.get_value(key)


@app.command("set")
def set_(
    key: Annotated[str, typer.Argument(help="Dotted key, e.g. 'capture.enabled'.")],
    value: Annotated[str, typer.Argument(help="New value.")],
) -> None:
    """Set a configuration value and save it."""
    settings_service.set_value(key, value)


@app.command("redaction")
def redaction(
    level: Annotated[RedactionLevel, typer.Argument(help="Redaction level.")],
) -> None:
    """Set how aggressively captured data is scrubbed."""
    settings_service.set_redaction(level)
