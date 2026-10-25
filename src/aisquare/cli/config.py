"""``aisquare config`` — read and write configuration."""

from __future__ import annotations

import sys
from typing import Annotated

import typer

from aisquare.cli.common import (
    emit_config,
    emit_config_overrides,
    emit_config_reset,
    emit_config_value,
    expected_config_write_errors,
    fail,
)
from aisquare.core.console import stdout_console
from aisquare.core.state import get_state
from aisquare.models import RedactionLevel
from aisquare.services import settings as settings_service

app = typer.Typer(help="Read and write aisquare configuration.", no_args_is_help=True)


@app.command("list")
def list_(
    overrides: Annotated[
        bool,
        typer.Option("--overrides", help="Only the settings you changed from the system defaults."),
    ] = False,
) -> None:
    """Print the fully-resolved configuration: the system defaults with your changes on top."""
    if overrides:
        emit_config_overrides(settings_service.overrides())
        return
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
    value: Annotated[str, typer.Argument(help="New value; comma-separated items for a list key.")],
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
    note = settings_service.what_it_changes(key, stored)
    if note and not get_state().json_output:
        stdout_console().print(note, style="dim")


@app.command("redaction")
def redaction(
    level: Annotated[RedactionLevel, typer.Argument(help="Redaction level.")],
) -> None:
    """Set how aggressively captured data is scrubbed."""
    with expected_config_write_errors():
        stored = settings_service.set_redaction(level)
    emit_config_value("redaction.level", stored)


def _stdin_is_a_terminal() -> bool:
    """Whether there is somebody to ask: behind a name, as ``fleet down``'s is, because
    ``CliRunner`` replaces ``sys.stdin`` and a test could not reach the question."""
    return sys.stdin.isatty()


@app.command("reset")
def reset(
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Reset without asking; required off a terminal.")
    ] = False,
) -> None:
    """Put every setting back to its system default.

    Your config file moves aside to config.toml.bak-<stamp> beside it, so moving it
    back undoes the reset. Asks first at a terminal; off a terminal it is a dry run
    unless --yes, and under --json without --yes it changes nothing.
    """
    config = settings_service.config_path()
    if not settings_service.anything_to_reset():
        emit_config_reset(config, None)  # nothing to ask about, and nothing to back up
        return
    if not yes:
        if get_state().json_output:
            emit_config_reset(config, None)
            return
        if not _stdin_is_a_terminal():
            stdout_console().print(
                f"dry run: nothing reset — re-run with --yes to move {config} aside "
                "and put every setting back to its system default"
            )
            return
        question = f"Move {config} aside and put every setting back to its system default?"
        if not typer.confirm(question, default=False):
            stdout_console().print("nothing reset")
            return
    with expected_config_write_errors():
        backup = settings_service.reset_settings()
    emit_config_reset(config, backup)
