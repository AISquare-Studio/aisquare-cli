"""``aisquare metrics`` — what the CI test bed recorded, per turn, what each agent
session spent in tokens as the hooks recorded it (``metrics tokens``), and the same
counted live from each transcript with its wakes, deltas and compactions (``metrics
ledger``); and every Claude Code session on this machine summed by day, repository, account
and model (``metrics stats``), with the sweep that reads the transcripts no hook saw
(``metrics backfill``)."""

from __future__ import annotations

import json
import sys
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
from aisquare.cli.usage_render import emit_usage_stats
from aisquare.core.state import get_state
from aisquare.models import USAGE_RANGES
from aisquare.services import metrics as metrics_service
from aisquare.services import token_ledger, usage_stats

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


_RANGE_HELP = "The window: today, the last 7d or 30d (local days, today included), or lifetime."


@app.command("stats")
def stats(
    range_: Annotated[
        str, typer.Option("--range", "-r", help=_RANGE_HELP, show_default=True)
    ] = "7d",
    sweep: Annotated[
        bool,
        typer.Option(
            "--sweep/--no-sweep",
            help="Read the transcripts nothing has read for 10 minutes first (the default).",
        ),
    ] = True,
) -> None:
    """Usage across every Claude Code session on this machine: tokens, cost at list prices,
    sessions, repositories, accounts, models and the features you use, for today, 7 or 30 days
    or all time. The same figures as Settings ▸ Usage & stats."""
    if range_ not in USAGE_RANGES:
        raise typer.BadParameter(
            f"{range_!r} is not one of {', '.join(USAGE_RANGES)}", param_hint="--range"
        )
    if sweep:
        report = usage_stats.maybe_sweep()
        if report.sessions_new and not get_state().json_output:
            typer.echo(
                f"read {report.sessions_new} new session(s) from Claude Code's transcripts",
                err=True,
            )
    result = usage_stats.stats(range_)
    if get_state().json_output:
        typer.echo(json.dumps(result.model_dump(mode="json")))
        return
    emit_usage_stats(result)


@app.command("backfill")
def backfill() -> None:
    """Read Claude Code's transcripts for every session on this machine — every repository,
    every account directory, whether or not aisquare launched it — and record what they spent.

    Idempotent and incremental: a session already counted (by its Stop hook or an earlier
    backfill) is read only from where it was left. Claude Code deletes transcripts after
    30 days by default, so this keeps history that would otherwise go.
    """
    quiet = get_state().json_output or not sys.stderr.isatty()

    def progress(done: int, total: int) -> None:
        if not quiet and total:
            typer.echo(f"\rreading transcripts {done}/{total}", nl=False, err=True)

    report = usage_stats.sweep(progress=progress)
    if not quiet:
        typer.echo("", err=True)
    if get_state().json_output:
        typer.echo(json.dumps(report.as_dict()))
        return
    if report.skipped:
        typer.echo(f"nothing read: {report.skipped}")
        return
    typer.echo(
        f"{report.transcripts} transcripts: {report.sessions_new} new sessions, "
        f"{report.sessions_updated} grown, {report.unchanged} unchanged"
        + (f", {report.failed} unreadable" if report.failed else "")
        + f" — {report.turns} turns recorded in {report.seconds:.1f}s"
    )
