"""The shared "not implemented yet" stub used by every unimplemented service."""

from __future__ import annotations

import json
from typing import NoReturn

import typer

from aisquare.core.console import stderr_console
from aisquare.core.state import get_state

EXIT_NOT_IMPLEMENTED = 70
"""Exit code for stubbed commands (sysexits EX_SOFTWARE)."""


def stub(command: str, tier: str = "v0") -> NoReturn:
    """Report that ``aisquare <command>`` is not implemented yet and exit.

    Prints a warning to stderr — or, when ``--json`` is active, a
    machine-readable error object to stdout — then exits with
    ``EXIT_NOT_IMPLEMENTED`` (70).

    Args:
        command: Command path without the program name, e.g. ``"context add"``.
        tier: Release tier in which the command is planned.
    """
    if get_state().json_output:
        typer.echo(
            json.dumps({"error": "not_implemented", "command": command}, separators=(",", ":"))
        )
    else:
        stderr_console().print(f"⚠ aisquare {command} is not implemented yet (planned: {tier})")
    raise typer.Exit(code=EXIT_NOT_IMPLEMENTED)
