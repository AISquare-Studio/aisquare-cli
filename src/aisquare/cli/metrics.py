"""``aisquare metrics`` — what the CI test bed recorded, per turn, what each agent
session spent in tokens as the hooks recorded it (``metrics tokens``), and the same
counted live from each transcript with its wakes, deltas and compactions (``metrics
ledger``)."""

from __future__ import annotations

from typing import Annotated

import typer

from aisquare.cli.common import (
    emit_metrics_summary,
    emit_token_ledger,
    emit_token_summary,
    emit_turn_metrics,
    parse_since,
    refuse_conflicting_scope,
)
from aisquare.services import metrics as metrics_service
from aisquare.services import token_ledger

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
    refuse_conflicting_scope(all_projects, project)
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


@app.command("tokens")
def tokens(
    session: Annotated[
        str | None, typer.Option("--session", help="One session (an id or its prefix).")
    ] = None,
    project: Annotated[str | None, typer.Option("--project", help=_PROJECT_HELP)] = None,
    all_projects: Annotated[bool, typer.Option("--all", help=_ALL_HELP)] = False,
) -> None:
    """Token usage of recorded sessions: raw, cost-weighted and at list price.

    Read off each session's transcript by the Stop hook, deduplicated per API
    request.
    """
    project_id = _scope(project, all_projects, session)
    sessions = metrics_service.session_usages(project_id=project_id, session_id=session)
    emit_token_summary(
        metrics_service.summarize_tokens(sessions, project_id=project_id, session_id=session)
    )


@app.command("ledger")
def ledger(
    session: Annotated[str | None, typer.Option("--session", help=_SESSION_HELP)] = None,
    project: Annotated[str | None, typer.Option("--project", help=_PROJECT_HELP)] = None,
    all_projects: Annotated[bool, typer.Option("--all", help=_ALL_HELP)] = False,
    since: Annotated[
        str | None,
        typer.Option("--since", help="Only what happened after: 15m, 2h, or an ISO time."),
    ] = None,
    role: Annotated[
        str | None,
        typer.Option("--role", help="Only this role's sessions (a seat counts as its role)."),
    ] = None,
) -> None:
    """What each agent session spent in tokens, counted from its transcript: one row per
    session and a total. Counts only, never text (docs/plans/handoff-p1-token-diet.md §1.7)."""
    project_id = _scope(project, all_projects, session)
    rows = token_ledger.ledger(
        token_ledger.sessions(project_id, session=session, role=role),
        since=parse_since(since) if since is not None else None,
    )
    emit_token_ledger(rows, token_ledger.total(rows))
