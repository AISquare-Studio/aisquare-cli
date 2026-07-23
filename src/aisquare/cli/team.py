"""``aisquare team`` — the agent orchestrator for parallel sessions.

Also home of the top-level ``note`` and ``board`` shortcuts (registered by
``cli/app.py``) so agents can type the two most frequent verbs directly.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Annotated, NoReturn

import typer

from aisquare.cli.common import fail, local_time
from aisquare.core.console import stdout_console
from aisquare.core.state import get_state
from aisquare.core.store import AmbiguousIdError
from aisquare.services import team as team_service
from aisquare.services.team import DeliveryUnconfirmedError, TeamDisabledError

app = typer.Typer(help="Coordinate parallel agent sessions on this project.", no_args_is_help=True)

SessionRef = Annotated[
    str | None,
    typer.Option("--as", help="Act as this team session (id prefix, from your board)."),
]

# Every failure a store-backed team command can hit, routed through _fail_team.
STORE_ERRORS = (
    TeamDisabledError,
    KeyError,
    AmbiguousIdError,
    DeliveryUnconfirmedError,
    sqlite3.OperationalError,
)


def _fail_team(exc: Exception, ref: str | None = None) -> NoReturn:
    """Translate service errors into the shared CLI error contract."""
    if isinstance(exc, TeamDisabledError):
        fail(str(exc), error="team_disabled")
    if isinstance(exc, AmbiguousIdError):
        fail(f"'{exc.ref}' is ambiguous — use more characters", error="ambiguous_id", ref=exc.ref)
    if isinstance(exc, DeliveryUnconfirmedError):
        # ref = the unconfirmed write's id, so an agent knows exactly which
        # event/task to look for in `aisquare log` before retrying.
        fail(str(exc), error="delivery_unconfirmed", ref=exc.ref)
    if isinstance(exc, sqlite3.OperationalError):
        # A wedged or contended store must fail loudly, never traceback —
        # and never print anything a caller could mistake for success.
        fail(f"context store unavailable ({exc}) — retry shortly", error="store_locked")
    if isinstance(exc, KeyError):
        missing = ref if ref is not None else str(exc)
        fail(f"nothing matches '{missing}'", error="not_found", ref=missing)
    raise exc


def emit_write_warning(delivery: team_service.Delivery | None) -> None:
    """Surface a board-mismatch warning on stderr (human and ``--json`` runs).

    Plain ``echo``, not the rich console: the warning must never wrap or be
    reflowed — agents grep their own stderr for it.
    """
    if delivery is not None and delivery.warning:
        typer.echo(f"⚠ {delivery.warning}", err=True)


def delivery_fields(delivery: team_service.Delivery | None) -> dict[str, object]:
    """The top-level JSON fields a confirmed write adds to its payload."""
    if delivery is None:
        return {}
    fields: dict[str, object] = {"delivered": True}
    if delivery.warning:
        fields["warning"] = delivery.warning
    return fields


def receipt_suffix(delivery: team_service.Delivery | None) -> str:
    """The `` · seq N on <board>`` tail of a human ✓ line (empty for reads)."""
    return f" · {delivery.receipt}" if delivery is not None else ""


@app.command("on")
def on() -> None:
    """Activate the orchestrator for this project."""
    try:
        project = team_service.activate()
    except STORE_ERRORS as exc:
        _fail_team(exc)
    delivery = team_service.last_delivery()
    if get_state().json_output:
        payload: dict[str, object] = {"activated": project.id, "root": str(project.root)}
        payload.update(delivery_fields(delivery))
        typer.echo(json.dumps(payload))
    else:
        stdout_console().print(
            f"✓ agent orchestrator active for {project.root.name or project.id} — "
            f"sessions launched here now share tasks and notes{receipt_suffix(delivery)}"
        )


@app.command("status")
def status() -> None:
    """Show the live team board (sessions, tasks, recent updates)."""
    board()


@app.command("focus")
def focus(
    text: Annotated[str, typer.Argument(help="What this session is working on right now.")],
    as_session: SessionRef = None,
) -> None:
    """Announce this session's current focus to the team."""
    if as_session is None:
        fail("--as <session> is required (your id is on the board)", error="missing_session")
    try:
        session = team_service.set_focus(text, as_session)
    except STORE_ERRORS as exc:
        _fail_team(exc, as_session)
    delivery = team_service.last_delivery()
    if get_state().json_output:
        payload = session.model_dump(mode="json")
        payload.update(delivery_fields(delivery))
        typer.echo(json.dumps(payload))
    else:
        stdout_console().print(
            f"✓ focus of {team_service.short_id(session.id)}: {text}{receipt_suffix(delivery)}",
            markup=False,
        )


@app.command("role")
def role(
    name: Annotated[str, typer.Argument(help="Role for this session (planner/coder/runner/…).")],
    as_session: SessionRef = None,
) -> None:
    """Set a session's role on the board."""
    if as_session is None:
        fail("--as <session> is required (your id is on the board)", error="missing_session")
    try:
        session = team_service.set_role(name, as_session)
    except STORE_ERRORS as exc:
        _fail_team(exc, as_session)
    if get_state().json_output:
        typer.echo(session.model_dump_json())
    else:
        stdout_console().print(f"✓ {team_service.short_id(session.id)} is now {session.role}")


def _parse_since(value: str) -> datetime:
    """``--since`` accepts a relative span (15m, 2h, 3d) or an ISO timestamp."""
    span = re.fullmatch(r"(\d+)([smhd])", value.strip())
    if span:
        unit = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[span.group(2)]
        return datetime.now(tz=UTC) - timedelta(**{unit: int(span.group(1))})
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise typer.BadParameter(
            f"{value!r} is neither a span (15m, 2h) nor an ISO timestamp"
        ) from None
    # A naive timestamp means the user's local clock.
    return moment if moment.tzinfo is not None else moment.astimezone()


@app.command("log")
def log(
    limit: Annotated[int, typer.Option("--limit", "-n", help="How many events to show.")] = 30,
    by: Annotated[
        str | None, typer.Option("--by", help="Only events by this session (id prefix).")
    ] = None,
    mine: Annotated[
        bool, typer.Option("--mine", help="Only events by the --as session (self-check).")
    ] = False,
    since: Annotated[
        str | None, typer.Option("--since", help="Only events after: 15m, 2h, or an ISO time.")
    ] = None,
    since_seq: Annotated[
        int | None, typer.Option("--since-seq", help="Only events past this seq (cursor).")
    ] = None,
    kind: Annotated[
        str | None, typer.Option("--kind", help="Only this kind (note, decision, task_done, …).")
    ] = None,
    task: Annotated[
        str | None, typer.Option("--task", help="Only events about this task (id prefix).")
    ] = None,
    as_session: SessionRef = None,
) -> None:
    """Show the team pipe: recent events from every session, filterable."""
    if mine and by is not None:
        raise typer.BadParameter("--mine and --by are mutually exclusive")
    if mine:
        if as_session is None:
            fail("--mine needs --as <session> (your id is on the board)", error="missing_session")
        by = as_session
    moment = _parse_since(since) if since is not None else None
    try:
        events = team_service.log_events(
            limit=limit,
            by=by,
            since=moment,
            since_seq=since_seq,
            kind=kind,
            task_ref=task,
            session_ref=as_session,
        )
    except STORE_ERRORS as exc:
        _fail_team(exc, task or by or as_session)
    if get_state().json_output:
        typer.echo(json.dumps([event.as_envelope().model_dump(mode="json") for event in events]))
        return
    if not events:
        stdout_console().print("No team events match.")
        return
    console = stdout_console()
    for event in events:
        who = team_service.short_id(event.session_id) if event.session_id else "cli"
        console.print(
            f"{local_time(event.created_at):%H:%M} {who} {event.kind}: {event.text}",
            markup=False,
        )


@app.command("verify")
def verify(
    receipt: Annotated[
        str, typer.Argument(help="A write receipt: event seq number, or event id (prefix ok).")
    ],
    as_session: SessionRef = None,
) -> None:
    """Re-check a receipt: prove the write is really on this board.

    The pull side of delivery trust — every ✓ prints ``seq N on <board>``
    (#20's push side); this re-proves it any time, from any process.
    """
    try:
        result = team_service.verify_receipt(receipt, session_ref=as_session)
    except STORE_ERRORS as exc:
        _fail_team(exc, receipt)
    if result.event is None:
        aside = f" — it exists on board {result.elsewhere}" if result.elsewhere else ""
        fail(
            f"no event matches receipt '{receipt}' on board {result.board_name}{aside}",
            error="not_found",
            ref=receipt,
            hint=result.elsewhere,
        )
    if get_state().json_output:
        payload = result.event.as_envelope().model_dump(mode="json")
        payload["delivered"] = True
        typer.echo(json.dumps(payload))
    else:
        stdout_console().print(
            f"✓ delivered · seq {result.event.seq} on {result.board_name}: {result.line}",
            markup=False,
        )


def _signal_json(state: team_service.SignalState) -> dict[str, object]:
    return {
        "name": state.name,
        "value": state.value,
        "set_by": state.set_by,
        "seq": state.seq,
        "updated_at": state.updated_at.isoformat() if state.updated_at else None,
    }


def _signal_line(state: team_service.SignalState) -> str:
    who = team_service.short_id(state.set_by) if state.set_by else "cli"
    when = f" at {local_time(state.updated_at):%H:%M}" if state.updated_at else ""
    return f"{state.name} = {state.value} · set by {who}{when} · seq {state.seq}"


@app.command("signal")
def signal(
    name: Annotated[str, typer.Argument(help="Signal name (lowercase token, e.g. fold-ready).")],
    value: Annotated[
        str | None,
        typer.Argument(help="New value (single token). Omit to read the current value."),
    ] = None,
    as_session: SessionRef = None,
) -> None:
    """Set or read a named board state — structured, never substring-matched.

    ``team signal fold-ready on --as <sid>`` sets; ``team signal fold-ready``
    reads. Every set emits a ``signal`` event whose payload carries
    ``name``/``value``/``prev``/``set_by`` — watchers key on fields, so a
    note saying "NOT READY" can never trip a ``ready`` watcher again (#23).
    """
    if value is None:
        try:
            state = team_service.read_signal(name, session_ref=as_session)
        except STORE_ERRORS as exc:
            _fail_team(exc, as_session)
        if state is None:
            fail(f"no signal named '{name}' on this board", error="not_found", ref=name)
        if get_state().json_output:
            typer.echo(json.dumps(_signal_json(state)))
        else:
            stdout_console().print(_signal_line(state), markup=False)
        return
    try:
        state, prev = team_service.set_signal(name, value, session_ref=as_session)
    except ValueError as exc:
        fail(str(exc), error="invalid_signal", ref=name)
    except STORE_ERRORS as exc:
        _fail_team(exc, as_session)
    delivery = team_service.last_delivery()
    emit_write_warning(delivery)
    if get_state().json_output:
        payload = _signal_json(state)
        payload["prev"] = prev
        payload.update(delivery_fields(delivery))
        typer.echo(json.dumps(payload))
    else:
        was = f" (was {prev})" if prev is not None else ""
        stdout_console().print(
            f"✓ signal {state.name}: {state.value}{was}{receipt_suffix(delivery)}",
            markup=False,
        )


@app.command("signals")
def signals(as_session: SessionRef = None) -> None:
    """List every named board state and who set it."""
    try:
        states = team_service.list_signals(session_ref=as_session)
    except STORE_ERRORS as exc:
        _fail_team(exc, as_session)
    if get_state().json_output:
        typer.echo(json.dumps([_signal_json(state) for state in states]))
        return
    if not states:
        stdout_console().print("No signals set. Set one with: aisquare team signal <name> <value>")
        return
    console = stdout_console()
    for state in states:
        console.print(_signal_line(state), markup=False)


def _fmt_idle(minutes: int) -> str:
    """Render an idle span the way the board's ``_age`` does (12m, 3h07m)."""
    return f"{minutes}m" if minutes < 60 else f"{minutes // 60}h{minutes % 60:02d}m"


@app.command("prune")
def prune(
    older_than: Annotated[
        int | None,
        typer.Option(
            "--older-than",
            help="Minutes without a heartbeat before a session counts as a ghost "
            "(default: 30, the board's stale mark).",
        ),
    ] = None,
    keep: Annotated[
        str | None,
        typer.Option("--keep", help="Spare this session (id prefix) even if it looks stale."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show who would be retired without touching anything."),
    ] = False,
) -> None:
    """Retire ghost sessions and return their orphaned claims to the pool — a clean roll-call.

    A dead loop or crashed terminal lingers on the board as ``(stale)`` forever.
    This ends those rows so the board shows who is actually here, and frees any
    task stranded under them. Data-safe: only presence + orphaned claims change
    — tasks, notes, events and the project brain are untouched.
    """
    try:
        report = team_service.prune_sessions(older_than, dry_run=dry_run, keep=keep)
    except STORE_ERRORS as exc:
        _fail_team(exc, keep)
    delivery = team_service.last_delivery()
    if get_state().json_output:
        payload: dict[str, object] = {
            "dry_run": report.dry_run,
            "threshold_minutes": report.threshold_minutes,
            "released_total": report.released_total,
            "pruned": [
                {
                    "id": p.id,
                    "role": p.role,
                    "idle_minutes": p.idle_minutes,
                    "released": p.released,
                }
                for p in report.pruned
            ],
        }
        payload.update(delivery_fields(delivery))
        typer.echo(json.dumps(payload))
        return
    console = stdout_console()
    if not report.pruned:
        console.print(
            f"✓ roll-call clean — every live session checked in within "
            f"{report.threshold_minutes}m. No ghosts to retire."
        )
        return
    for entry in report.pruned:
        claims = (
            f", freed {entry.released} claim{'' if entry.released == 1 else 's'}"
            if entry.released
            else ""
        )
        bullet = "·" if report.dry_run else "✓"
        console.print(
            f"  {bullet} {team_service.short_id(entry.id)} ({entry.role}) — "
            f"dark {_fmt_idle(entry.idle_minutes)}{claims}",
            markup=False,
        )
    count = len(report.pruned)
    plural = "" if count == 1 else "s"
    if report.dry_run:
        console.print(
            f"— would retire {count} ghost session{plural} (dry run, nothing changed). "
            "Re-run without --dry-run to clear them."
        )
    else:
        tail = (
            f" and returned {report.released_total} orphaned "
            f"claim{'' if report.released_total == 1 else 's'} to the pool"
            if report.released_total
            else ""
        )
        console.print(
            f"🧹 retired {count} ghost session{plural}{tail} — "
            f"board's aligned.{receipt_suffix(delivery)}"
        )


@app.command("distill")
def distill(
    rescan: Annotated[
        bool,
        typer.Option(
            "--all", help="Backfill: re-distill the whole pipe from the beginning (idempotent)."
        ),
    ] = False,
) -> None:
    """Push undistilled notes/decisions/outcomes into the project brain now."""
    try:
        count = team_service.distill_now(rescan=rescan)
    except STORE_ERRORS as exc:
        _fail_team(exc)
    if get_state().json_output:
        typer.echo(json.dumps({"distilled": count}))
    elif count is None:
        stdout_console().print("… another distill is already running — it has the brain")
    else:
        noun = "page" if count == 1 else "pages"
        stdout_console().print(f"✓ distilled {count} {noun} into the project brain")


def recall(
    query: Annotated[str, typer.Argument(help="What to look up in the project brain.")],
) -> None:
    """Search the team's long-term memory (decisions, results, outcomes)."""
    try:
        output = team_service.recall(query)
    except STORE_ERRORS as exc:
        _fail_team(exc)
    if output is None:
        fail(
            "project brain unavailable — gbrain missing, brain busy, or nothing "
            "distilled yet (try `aisquare team distill`)",
            error="brain_unavailable",
        )
    if get_state().json_output:
        typer.echo(json.dumps({"query": query, "output": output}))
    else:
        stdout_console().print(output.rstrip("\n"), markup=False)


def note(
    text: Annotated[str, typer.Argument(help="The note/decision/question/result to share.")],
    as_session: SessionRef = None,
    task: Annotated[
        str | None, typer.Option("--task", help="Task this note is about (id prefix).")
    ] = None,
    to: Annotated[
        str | None, typer.Option("--to", help="Address a role (planner/coder/runner/…).")
    ] = None,
    kind: Annotated[
        str, typer.Option("--kind", help="note, decision, question or result.")
    ] = "note",
) -> None:
    """Share a note with the team (it reaches every session automatically)."""
    try:
        event = team_service.add_note(
            text, session_ref=as_session, task_ref=task, to_role=to, kind=kind
        )
    except STORE_ERRORS as exc:
        _fail_team(exc, task or as_session)
    delivery = team_service.last_delivery()
    emit_write_warning(delivery)
    if get_state().json_output:
        payload = event.as_envelope().model_dump(mode="json")
        payload.update(delivery_fields(delivery))
        typer.echo(json.dumps(payload))
    else:
        stdout_console().print(
            f"✓ shared ({event.kind}): {event.text}{receipt_suffix(delivery)}", markup=False
        )


def board(
    watch: Annotated[
        bool, typer.Option("--watch", "-w", help="Full-screen live board; Ctrl-C exits.")
    ] = False,
    interval: Annotated[
        float, typer.Option("--interval", "-i", help="Refresh seconds in watch mode.")
    ] = 3.0,
) -> None:
    """Show the live team board (sessions, tasks, recent updates)."""
    if watch:
        if get_state().json_output:
            raise typer.BadParameter("--watch and --json cannot be combined")
        _watch_board(max(interval, 0.5))
        return
    try:
        project, sessions, tasks, events = team_service.board_data()
    except STORE_ERRORS as exc:
        _fail_team(exc)
    if get_state().json_output:
        typer.echo(
            json.dumps(
                {
                    "project": project.model_dump(mode="json"),
                    "sessions": [s.model_dump(mode="json") for s in sessions],
                    "tasks": [t.model_dump(mode="json") for t in tasks],
                    "events": [e.as_envelope().model_dump(mode="json") for e in events],
                }
            )
        )
        return
    if not sessions and not tasks:
        stdout_console().print(
            "The orchestrator is quiet here. Activate with `aisquare team on`, or launch a "
            "session with AISQUARE_ROLE=planner (coder/runner/…) set."
        )
        return
    stdout_console().print(
        team_service.render_board(project, sessions, tasks, events), markup=False
    )


def _watch_board(interval: float) -> None:
    """Run the live board (interactive TUI or Rich fallback — see cli.watch)."""
    from aisquare.cli import watch as watch_ui

    try:
        watch_ui.run_watch(interval)
    except TeamDisabledError as exc:
        _fail_team(exc)
