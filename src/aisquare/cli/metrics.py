"""``aisquare metrics`` — what the CI test bed recorded, per turn."""

from __future__ import annotations

from typing import Annotated

import typer

from aisquare.cli.common import emit_metrics_summary, emit_turn_metrics
from aisquare.services import metrics as metrics_service

app = typer.Typer(
    help="Inspect per-turn metrics recorded by the CI test bed.", no_args_is_help=True
)

_LIMIT_HELP = "How many recent turns to read (at least 1)."
_SESSION_HELP = "Restrict to one agent session."
_PROJECT_HELP = "Report on this project (name or id prefix) instead of the current one."
_ALL_HELP = "Every project on this machine, not just the current one."


def _scope(project: str | None, all_projects: bool, session: str | None) -> str | None:
    """Which project's turns to read. A session already names a scope, so
    ``--session`` alone is not narrowed to the current project."""
    if session is not None and project is None and not all_projects:
        return None
    try:
        return metrics_service.resolve_scope(project, all_projects=all_projects)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--project") from exc


@app.command("show")
def show(
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, help=_LIMIT_HELP)] = 500,
    session: Annotated[str | None, typer.Option("--session", help=_SESSION_HELP)] = None,
    project: Annotated[str | None, typer.Option("--project", help=_PROJECT_HELP)] = None,
    all_projects: Annotated[bool, typer.Option("--all", help=_ALL_HELP)] = False,
) -> None:
    """Summarise recorded turns for the current project."""
    project_id = _scope(project, all_projects, session)
    turns = metrics_service.recent(project_id=project_id, session_id=session, limit=limit)
    emit_metrics_summary(metrics_service.summarize(turns, project_id=project_id))


@app.command("list")
def list_(
    limit: Annotated[int, typer.Option("--limit", "-n", min=1, help=_LIMIT_HELP)] = 20,
    session: Annotated[str | None, typer.Option("--session", help=_SESSION_HELP)] = None,
    project: Annotated[str | None, typer.Option("--project", help=_PROJECT_HELP)] = None,
    all_projects: Annotated[bool, typer.Option("--all", help=_ALL_HELP)] = False,
) -> None:
    """List recent turns, newest first."""
    project_id = _scope(project, all_projects, session)
    emit_turn_metrics(
        metrics_service.recent(project_id=project_id, session_id=session, limit=limit)
    )
