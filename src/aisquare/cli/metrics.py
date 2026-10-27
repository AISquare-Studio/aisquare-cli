"""``aisquare metrics`` — what the CI test bed recorded, per turn, what each agent
session spent in tokens as the hooks recorded it (``metrics tokens``), and the same
counted live from each transcript with its wakes, deltas and compactions (``metrics
ledger``); and every Claude Code session on this machine summed by day, repository, account
and model (``metrics stats``), with the sweep that reads the transcripts no hook saw
(``metrics backfill``)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from aisquare.cli.common import (
    emit_metrics_summary,
    emit_token_ledger,
    emit_token_summary,
    emit_turn_metrics,
    fail,
    parse_since,
    refuse_conflicting_scope,
)
from aisquare.cli.usage_render import (
    emit_sharing_status,
    emit_ship_report,
    emit_usage_stats,
)
from aisquare.core import paths, sharing
from aisquare.core.state import get_state
from aisquare.models import USAGE_RANGES
from aisquare.services import metrics as metrics_service
from aisquare.services import token_ledger, usage_share, usage_stats

app = typer.Typer(
    help="Inspect per-turn metrics recorded by the CI test bed.", no_args_is_help=True
)

share_app = typer.Typer(
    help="Opt-in anonymous usage sharing: on, off, status, and what would be sent.",
    no_args_is_help=True,
)
app.add_typer(share_app, name="share")

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
        # The first sweep on a machine reads every transcript Claude Code kept (about thirty
        # seconds for a few hundred): say so on the terminal, as `backfill` does.
        showing = _shows_progress()
        report = usage_stats.maybe_sweep(progress=_progress_line if showing else None)
        if showing and report.transcripts:
            typer.echo("", err=True)  # the progress line's end
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


def _shows_progress() -> bool:
    """Whether a sweep's progress line is shown: on a terminal, never under ``--json``."""
    return not get_state().json_output and sys.stderr.isatty()


def _progress_line(done: int, total: int) -> None:
    if total:
        typer.echo(f"\rreading Claude Code's transcripts {done}/{total}", nl=False, err=True)


@app.command("backfill")
def backfill() -> None:
    """Read Claude Code's transcripts for every session on this machine — every repository,
    every account directory, whether or not aisquare launched it — and record what they spent.

    Idempotent and incremental: a session already counted (by its Stop hook or an earlier
    backfill) is read only from where it was left. Claude Code deletes transcripts after
    30 days by default, so this keeps history that would otherwise go.
    """
    quiet = not _shows_progress()
    report = usage_stats.sweep(progress=None if quiet else _progress_line)
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


# --- opt-in anonymous usage sharing (docs/usage-telemetry.md) ----------------------------------


def _json(payload: object) -> None:
    typer.echo(json.dumps(payload, default=str))


def _state_file() -> str:
    """Where the sharing's state lives, as a person would type it (``~/.aisquare/state.json``)."""
    path = paths.state_path()
    try:
        return "~/" + path.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(path)


def _state_unwritable(what: str, exc: Exception) -> NoReturn:
    """Fail ``what`` because the sharing's state cannot be updated, naming the file and the way
    out in the message itself (the human surface prints the message and nothing else)."""
    detail = f"{type(exc).__name__}: {exc}"[:200]
    fail(
        f"{what}: {_state_file()} cannot be updated ({detail}); repair or remove it and try again",
        error="sharing_state_unwritable",
        detail=detail,
        hint=f"repair or remove {_state_file()} and try again",
    )


@share_app.command("on")
def share_on() -> None:
    """Share anonymous daily usage counts with AISquare (off by default).

    What is sent is counts: tokens, sessions, commands used and a salted hash per repository,
    never names, paths, remotes, prompts or arguments (`aisquare metrics share preview` prints
    the exact body). Usage is counted for every repo you run Claude Code in, including ones you
    haven't added to aisquare; capture settings don't change this. Not available with
    DO_NOT_TRACK=1, AISQUARE_NO_TELEMETRY=1 or CI set.
    """
    try:
        status = usage_share.enable()
    except usage_share.SharingForcedOff as forced:
        fail(
            f"sharing cannot be turned on: {forced.reason}; unset it to share, "
            "or leave sharing off",
            error="sharing_forced_off",
            hint="unset it to share, or leave sharing off",
        )
    except Exception as exc:  # state.json that cannot be updated (damaged, unreadable)
        _state_unwritable("sharing could not be turned on", exc)
    sharing.start_background_ship()
    if get_state().json_output:
        _json(status.model_dump(mode="json"))
        return
    emit_sharing_status(status)
    typer.echo(usage_share.COVERAGE)


@share_app.command("off")
def share_off() -> None:
    """Stop sharing. What was kept of the shipments is purged, and one opt-out notice is sent
    (best effort, only if anything was ever sent); nothing is sent after it."""
    report = usage_share.disable()
    if get_state().json_output:
        _json({"enabled": False, **report.model_dump(mode="json")})
        return
    typer.echo("Sharing is off.")
    if report.opt_out:
        emit_ship_report(report)


@share_app.command("status")
def share_status() -> None:
    """Whether usage is shared, what forces it off, when it was last sent, and whether it is linked
    to your AISquare account."""
    status = usage_share.status()
    if get_state().json_output:
        _json(status.model_dump(mode="json"))
        return
    emit_sharing_status(status)


@share_app.command("preview")
def share_preview() -> None:
    """Print the body the next shipment would POST (JSON), built as a shipment builds it.

    With sharing on, exactly that body. Otherwise it is a SAMPLE of your last 31 days with
    placeholder ids, to show what would be sent: nothing is sent by looking, and once sharing is
    on only the days from then on are sent. With --json the body is wrapped:
    ``{"sample": bool, "sample_reason": str|null, "body": {...}}``."""
    try:
        reason = usage_share.sample_reason()
        body = usage_share.preview()
    except Exception as exc:  # the state file is damaged, or the store would not open
        detail = f"{type(exc).__name__}: {exc}"[:200]
        fail(
            f"the preview could not be built ({detail}); nothing was sent",
            error="preview_unavailable",
            detail=detail,
        )
    if get_state().json_output:
        _json({"sample": reason is not None, "sample_reason": reason, "body": json.loads(body)})
        return
    if reason is not None:
        typer.echo(
            f"this is a SAMPLE of your last 31 days with placeholder ids, not what a shipment "
            f"would send ({reason}); once sharing is on only days from then on are sent",
            err=True,
        )
    typer.echo(body)


@share_app.command("reset-id")
def share_reset_id() -> None:
    """Forget this installation's random id and salt for a new pair: what was sent before can no
    longer be tied to what is sent next."""
    try:
        install_id, _salt = usage_share.reset_identity()
    except Exception as exc:  # state.json that cannot be updated
        _state_unwritable("the installation id could not be changed", exc)
    if get_state().json_output:
        _json({"install_id": install_id})
        return
    typer.echo(f"New installation id {install_id[:8]}… (the repository hashes change with it).")


@app.command("ship", hidden=True)
def ship(
    force: Annotated[
        bool, typer.Option("--force", help="Send now even if a shipment is not due yet.")
    ] = False,
) -> None:
    """Send the next batch of usage counts if sharing is on and one is due: the detached process
    a command starts (never the command itself), after reading any transcripts not yet read."""
    report = usage_share.ship(force=force)  # reads the transcripts first, as the preview does
    if get_state().json_output:
        _json(report.model_dump(mode="json"))
        return
    emit_ship_report(report)
