"""``aisquare task`` — the shared, idempotent team task list."""

from __future__ import annotations

import json
from typing import Annotated, get_args

import typer
from rich.table import Table

from aisquare.cli.common import fail, local_time
from aisquare.cli.team import (
    STORE_ERRORS,
    SessionRef,
    _fail_team,
    delivery_fields,
    emit_write_warning,
    receipt_suffix,
    warn_board_scope,
)
from aisquare.core.console import stdout_console
from aisquare.core.state import get_state
from aisquare.models import TaskStatus, TeamTask
from aisquare.services import team as team_service
from aisquare.services.team import ClaimLostError

_STATUSES: tuple[str, ...] = get_args(TaskStatus)
_STATUS_HELP = ", ".join(_STATUSES)

app = typer.Typer(
    help="Shared team tasks: add is idempotent, claim is atomic.", no_args_is_help=True
)

TaskRef = Annotated[str, typer.Argument(help="Task id (prefix ok).")]


def _emit_task(task: TeamTask, *, verb: str) -> None:
    """Render a task verb line, carrying the write receipt when one exists.

    Reads (``task next`` without ``--claim``) leave no delivery behind, so
    their output stays receipt-free and, under ``--json``, unchanged.
    """
    delivery = team_service.last_delivery()
    emit_write_warning(delivery)
    if get_state().json_output:
        payload = task.model_dump(mode="json")
        payload.update(delivery_fields(delivery))
        typer.echo(json.dumps(payload))
    else:
        stdout_console().print(
            f"✓ {verb}: {task.id} [{task.status}] {task.title}{receipt_suffix(delivery)}",
            markup=False,
        )


@app.command("add")
def add(
    title: Annotated[str, typer.Argument(help="What needs doing.")],
    key: Annotated[
        str | None,
        typer.Option("--key", help="Idempotency key (defaults to a slug of the title)."),
    ] = None,
    detail: Annotated[str | None, typer.Option("--detail", help="Longer description.")] = None,
    role: Annotated[
        str | None, typer.Option("--role", help="Suggested owner role (advisory).")
    ] = None,
    needs: Annotated[
        list[str] | None,
        typer.Option("--needs", help="Task this one depends on (id prefix; repeat for several)."),
    ] = None,
    as_session: SessionRef = None,
) -> None:
    """Add a shared task. Re-adding the same task is safe — you get the original."""
    try:
        task, created = team_service.add_task(
            title, key=key, detail=detail, role=role, needs=needs, session_ref=as_session
        )
    except ValueError as exc:
        fail(str(exc), error="invalid_needs")
    except KeyError as exc:
        # The session resolves first in add_task, so a KeyError here names a
        # --needs ref — blame it, not the (valid) --as session.
        _fail_team(exc, str(exc.args[0]) if exc.args else None)
    except STORE_ERRORS as exc:
        _fail_team(exc, as_session)
    delivery = team_service.last_delivery()
    emit_write_warning(delivery)
    if get_state().json_output:
        payload = {"created": created, **task.model_dump(mode="json")}
        payload.update(delivery_fields(delivery))
        typer.echo(json.dumps(payload))
    else:
        verb = "added" if created else "already tracked (idempotent)"
        stdout_console().print(f"✓ {verb}: {task.id} {task.title}{receipt_suffix(delivery)}")


@app.command("list")
def list_(
    status: Annotated[
        str | None,
        typer.Option("--status", help=f"Filter: {_STATUS_HELP}."),
    ] = None,
) -> None:
    """List the team's shared tasks."""
    if status is not None and status not in _STATUSES:
        raise typer.BadParameter(f"status must be one of: {_STATUS_HELP}")
    narrowed: TaskStatus | None = status  # type: ignore[assignment]
    try:
        tasks = team_service.list_tasks(narrowed)
    except STORE_ERRORS as exc:
        _fail_team(exc)
    warn_board_scope(None)
    if get_state().json_output:
        typer.echo(json.dumps([task.model_dump(mode="json") for task in tasks]))
        return
    if not tasks:
        stdout_console().print('No shared tasks yet. Add one with: aisquare task add "…"')
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("ID", no_wrap=True)
    table.add_column("STATUS", no_wrap=True)
    table.add_column("CLAIMED BY", no_wrap=True)
    table.add_column("ROLE", no_wrap=True)
    table.add_column("TITLE")
    for task in tasks:
        table.add_row(
            task.id,
            task.status,
            team_service.short_id(task.claimed_by) if task.claimed_by else "",
            task.role or "",
            task.title,
        )
    stdout_console().print(table)


@app.command("show")
def show(ref: TaskRef) -> None:
    """Show one task in full."""
    try:
        task = team_service.show_task(ref)
    except STORE_ERRORS as exc:
        _fail_team(exc, ref)
    stopped = team_service.stopped_because(task)
    if get_state().json_output:
        payload = task.model_dump(mode="json")
        # Derived, not stored: the note lives in the event stream and this is a
        # join. Always present as a key so a script can read it without probing
        # — null means "no note", which is different from "field absent".
        payload["stopped_because"] = stopped
        typer.echo(json.dumps(payload))
        return
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold")
    grid.add_column()
    grid.add_row("id", task.id)
    grid.add_row("key", task.key)
    grid.add_row("status", task.status)
    if task.needs:
        grid.add_row("needs", ", ".join(task.needs))
    if task.role:
        grid.add_row("role", task.role)
    if task.claimed_by:
        grid.add_row("claimed by", team_service.short_id(task.claimed_by))
    if task.claim_expires_at:
        grid.add_row("lease until", f"{local_time(task.claim_expires_at):%Y-%m-%d %H:%M}")
    grid.add_row("created", f"{local_time(task.created_at):%Y-%m-%d %H:%M}")
    if stopped:
        grid.add_row("stopped because", stopped)
    console = stdout_console()
    console.print(grid)
    console.print()
    console.print(task.title if task.detail is None else f"{task.title}\n\n{task.detail}")


@app.command("claim")
def claim(ref: TaskRef, as_session: SessionRef = None) -> None:
    """Claim a task before working on it — exactly one session wins."""
    try:
        task = team_service.claim_task(ref, session_ref=as_session)
    except ClaimLostError as exc:
        fail(str(exc), error="claim_lost", ref=exc.task.id)
    except STORE_ERRORS as exc:
        _fail_team(exc, ref)
    _emit_task(task, verb="claimed")


@app.command("next")
def next_(
    role: Annotated[
        str | None, typer.Option("--role", help="Only tasks for this role (or unassigned).")
    ] = None,
    status: Annotated[
        str, typer.Option("--status", help="Which pool to draw from (todo or review).")
    ] = "todo",
    claim: Annotated[
        bool, typer.Option("--claim", help="Atomically claim the task (todo only).")
    ] = False,
    as_session: SessionRef = None,
) -> None:
    """Pick up the next available task — built for looped sessions."""
    if status not in _STATUSES:
        raise typer.BadParameter(f"status must be one of: {_STATUS_HELP}")
    narrowed: TaskStatus = status  # type: ignore[assignment]
    try:
        task = team_service.next_task(
            role=role, status=narrowed, claim=claim, session_ref=as_session
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    except STORE_ERRORS as exc:
        _fail_team(exc, as_session)
    if task is None:
        if get_state().json_output:
            typer.echo(json.dumps(None))
        else:
            stdout_console().print(f"Nothing to pick up (status: {status}).")
        return
    _emit_task(task, verb="claimed" if claim else "next up")


@app.command("review")
def review(
    ref: TaskRef,
    note: Annotated[
        str | None, typer.Option("--note", help="What to verify / what changed.")
    ] = None,
    as_session: SessionRef = None,
) -> None:
    """Send a task to review, ready for the runner to verify."""
    try:
        task = team_service.review_task(ref, note=note, session_ref=as_session)
    except STORE_ERRORS as exc:
        _fail_team(exc, ref)
    _emit_task(task, verb="sent to review")


@app.command("reopen")
def reopen(
    ref: TaskRef,
    reason: Annotated[str, typer.Option("--reason", help="The feedback: what failed and how.")],
    as_session: SessionRef = None,
) -> None:
    """Send a task back to the pool with feedback (verification failed)."""
    try:
        task = team_service.reopen_task(ref, reason=reason, session_ref=as_session)
    except ValueError as exc:
        fail(str(exc), error="invalid_state", ref=ref)
    except STORE_ERRORS as exc:
        _fail_team(exc, ref)
    _emit_task(task, verb="reopened")


@app.command("done")
def done(
    ref: TaskRef,
    note: Annotated[str | None, typer.Option("--note", help="Result worth sharing.")] = None,
    as_session: SessionRef = None,
) -> None:
    """Mark a task done (optionally sharing the outcome)."""
    try:
        task = team_service.finish_task(ref, note=note, session_ref=as_session)
    except STORE_ERRORS as exc:
        _fail_team(exc, ref)
    _emit_task(task, verb="done")


@app.command("block")
def block(
    ref: TaskRef,
    reason: Annotated[str, typer.Option("--reason", help="Why this task is stuck.")],
    as_session: SessionRef = None,
) -> None:
    """Mark a task blocked, telling the team why."""
    try:
        task = team_service.block_task(ref, reason=reason, session_ref=as_session)
    except STORE_ERRORS as exc:
        _fail_team(exc, ref)
    _emit_task(task, verb="blocked")


@app.command("drop")
def drop(ref: TaskRef, as_session: SessionRef = None) -> None:
    """Drop a task that is no longer worth doing."""
    try:
        task = team_service.drop_task(ref, session_ref=as_session)
    except STORE_ERRORS as exc:
        _fail_team(exc, ref)
    _emit_task(task, verb="dropped")


@app.command("release")
def release(ref: TaskRef, as_session: SessionRef = None) -> None:
    """Give a claimed (doing) task back to the pool."""
    try:
        task = team_service.release_task(ref, session_ref=as_session)
    except ValueError as exc:
        fail(str(exc), error="invalid_state", ref=ref)
    except STORE_ERRORS as exc:
        _fail_team(exc, ref)
    _emit_task(task, verb="released")
