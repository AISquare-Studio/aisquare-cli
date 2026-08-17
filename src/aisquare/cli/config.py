"""``aisquare config`` — read and write configuration."""

from __future__ import annotations

from typing import Annotated

import typer

from aisquare.cli.common import emit_config, emit_config_value, expected_config_write_errors, fail
from aisquare.models import RedactionLevel
from aisquare.services import settings as settings_service

app = typer.Typer(help="Read and write aisquare configuration.", no_args_is_help=True)


@app.command("list")
def list_() -> None:
    """Print the fully-resolved configuration."""
    emit_config(settings_service.list_values())


@app.command("get")
def get(
    key: Annotated[str, typer.Argument(help="Dotted key, e.g. 'redaction.level'.")],
) -> None:
    """Print a single configuration value."""
    try:
        value = settings_service.get_value(key)
    except KeyError:
        fail(f"unknown config key: {key}", error="unknown_key", ref=key)
    emit_config_value(key, value)


@app.command("set")
def set_(
    key: Annotated[str, typer.Argument(help="Dotted key, e.g. 'capture.enabled'.")],
    value: Annotated[str, typer.Argument(help="New value.")],
) -> None:
    """Set a configuration value and save it."""
    try:
        with expected_config_write_errors():
            stored = settings_service.set_value(key, value)
    except KeyError:
        fail(f"unknown config key: {key}", error="unknown_key", ref=key)
    except ValueError as exc:
        fail(str(exc), error="invalid_value", ref=key)
    emit_config_value(key, stored)


@app.command("redaction")
def redaction(
    level: Annotated[RedactionLevel, typer.Argument(help="Redaction level.")],
) -> None:
    """Set how aggressively captured data is scrubbed."""
    emit_config_value("redaction.level", settings_service.set_redaction(level))
