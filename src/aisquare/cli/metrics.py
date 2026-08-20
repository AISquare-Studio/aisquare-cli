"""``aisquare metrics`` — what the CI test bed recorded, per turn."""

from __future__ import annotations

from typing import Annotated

import typer

from aisquare.cli.common import emit_metrics_summary, emit_turn_metrics
from aisquare.services import metrics as metrics_service

app = typer.Typer(
    help="Inspect per-turn metrics recorded by the CI test bed.", no_args_is_help=True
)


@app.command("show")
def show(
    limit: Annotated[
        int, typer.Option("--limit", "-n", help="How many recent turns to summarise.")
    ] = 500,
    session: Annotated[
        str | None, typer.Option("--session", help="Restrict to one agent session.")
    ] = None,
) -> None:
    """Summarise recorded turns."""
    turns = metrics_service.recent(session_id=session, limit=limit)
    emit_metrics_summary(metrics_service.summarize(turns))


@app.command("list")
def list_(
    limit: Annotated[int, typer.Option("--limit", "-n", help="How many turns to list.")] = 20,
    session: Annotated[
        str | None, typer.Option("--session", help="Restrict to one agent session.")
    ] = None,
) -> None:
    """List recent turns, newest first."""
    emit_turn_metrics(metrics_service.recent(session_id=session, limit=limit))
