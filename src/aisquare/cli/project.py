"""``aisquare project`` (alias ``workspace``) — manage projects."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from aisquare.services import project as project_service

app = typer.Typer(
    help="Inspect, switch and onboard projects (alias: workspace).", no_args_is_help=True
)


@app.command("info")
def info() -> None:
    """Show the active project: id, root and linked repos."""
    project_service.info()


@app.command("list")
def list_() -> None:
    """List known projects."""
    project_service.list_projects()


@app.command("switch")
def switch(name: Annotated[str, typer.Argument(help="Project to make active.")]) -> None:
    """Switch the active project."""
    project_service.switch(name)


@app.command("link")
def link(repo: Annotated[str, typer.Argument(help="Repository path or URL to link.")]) -> None:
    """Link another repository into the active project."""
    project_service.link(repo)


@app.command("onboard")
def onboard(
    path: Annotated[
        Path | None, typer.Argument(help="Project root (default: current directory).")
    ] = None,
    refresh: Annotated[
        bool, typer.Option("--refresh", help="Re-run onboarding from scratch.")
    ] = False,
) -> None:
    """Analyse a project and seed its context pool."""
    project_service.onboard(path, refresh=refresh)
