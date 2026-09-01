"""``aisquare project`` (alias ``workspace``) — manage projects."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from aisquare.cli.common import (
    emit_onboard,
    emit_project_action,
    emit_project_detail,
    emit_projects,
    fail,
)
from aisquare.services import project as project_service

app = typer.Typer(
    help="Inspect, switch and onboard projects (alias: workspace).", no_args_is_help=True
)


@app.command("info")
def info() -> None:
    """Show the active project: id, root and linked repos."""
    emit_project_detail(project_service.info())


@app.command("list")
def list_() -> None:
    """List known projects (the active one is marked with *)."""
    projects = project_service.list_projects()
    emit_projects(projects, active_id=project_service.info().id)


@app.command("switch")
def switch(name: Annotated[str, typer.Argument(help="Project name or id prefix.")]) -> None:
    """Switch the active project (deprecated — prefer streams)."""
    _deprecated(
        "project switch pins ONE project globally and the pin beats your working "
        "directory in every terminal. Prefer streams: `aisquare stream add NAME` "
        "to group projects, AISQUARE_STREAM=NAME to force one per shell."
    )
    try:
        project = project_service.switch(name)
    except KeyError:
        fail(f"no project matches '{name}'", error="not_found", ref=name)
    except ValueError as exc:
        fail(str(exc), error="ambiguous_project", ref=name)
    emit_project_action(f"✓ switched to {project.root.name or project.id} ({project.id})", project)


@app.command("link")
def link(repo: Annotated[str, typer.Argument(help="Repository path or URL to link.")]) -> None:
    """Link another repository into the active project (deprecated — prefer streams)."""
    _deprecated(
        "project link only records the path — nothing reads it back. To share "
        "context between repositories, put them in a stream: "
        "`aisquare stream new NAME && aisquare stream add NAME PATH…`."
    )
    project = project_service.link(repo)
    emit_project_action(f"✓ linked {repo} into {project.root.name or project.id}", project)


def _deprecated(message: str) -> None:
    """Warn on stderr; the command still works for one more release."""
    typer.secho(f"⚠ deprecated: {message}", err=True, fg=typer.colors.YELLOW)


@app.command("onboard")
def onboard(
    path: Annotated[
        Path | None, typer.Argument(help="Project root (default: current directory).")
    ] = None,
    refresh: Annotated[
        bool, typer.Option("--refresh", help="Re-scan even if already onboarded.")
    ] = False,
) -> None:
    """Pack the codebase into a snapshot and seed its context pool."""
    emit_onboard(project_service.onboard(path, refresh=refresh))
