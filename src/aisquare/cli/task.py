"""``aisquare task`` — the shared, idempotent team task list."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, NoReturn, get_args

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
from aisquare.core.console import stderr_console, stdout_console
from aisquare.core.state import get_state
from aisquare.core.store import BriefChangedError
from aisquare.models import TaskStatus, TeamTask
from aisquare.services import briefs as briefs_service
from aisquare.services import team as team_service
from aisquare.services.team import ClaimLostError

_STATUSES: tuple[str, ...] = get_args(TaskStatus)
_STATUS_HELP = ", ".join(_STATUSES)

app = typer.Typer(
    help="Shared team tasks: add is idempotent, claim is atomic.", no_args_is_help=True
)

TaskRef = Annotated[
    str, typer.Argument(help="The task's number on this board (12 or #12), or its id (prefix ok).")
]


def _emit_task(task: TeamTask, *, verb: str, contract: bool = False, peek: bool = False) -> None:
    """Render a task verb line, carrying the write receipt when one exists.

    Reads (``task next`` without ``--claim``) leave no delivery behind, so
    their output stays receipt-free and, under ``--json``, unchanged.

    ``contract`` is a claim (``task claim``, ``task next --claim``): the line is followed by
    the task's contract (``detail``), the newest reopen feedback, and the command for the
    full task. The coder used to see the title alone, and the contract was a ``task show``
    nothing named (#74). ``peek`` (``task next``) says a contract exists. ``--json`` is
    unchanged: it always carried ``detail``.
    """
    delivery = team_service.last_delivery()
    emit_write_warning(delivery)
    if get_state().json_output:
        payload = task.model_dump(mode="json")
        payload.update(delivery_fields(delivery))
        typer.echo(json.dumps(payload))
    else:
        console = stdout_console()
        # A bare number, never `#N`: in a shell `#` starts a comment, so a copied
        # `aisquare task show #2` ran `task show` with no ref (P0-FU, #51).
        ref = str(task.number) if task.number is not None else task.id
        console.print(
            f"✓ {verb}: {team_service.task_label(task)} [{task.status}] {task.title}"
            f"{receipt_suffix(delivery)}",
            markup=False,
        )
        if peek and task.detail:
            console.print(f"  has a contract — `aisquare task show {ref}`", markup=False)
        if not contract:
            return
        feedback = team_service.last_feedback(task)
        if task.detail:
            console.print(f"\n{task.detail}", markup=False)
        if feedback:
            console.print(f"\nreopened because: {feedback}", markup=False)
        if task.detail or feedback:
            console.print(f"\nfull task: `aisquare task show {ref}`", markup=False)


@app.command("add")
def add(
    title: Annotated[str, typer.Argument(help="What needs doing.")],
    key: Annotated[
        str | None,
        typer.Option("--key", help="Idempotency key (defaults to a slug of the title)."),
    ] = None,
    detail: Annotated[
        str | None,
        typer.Option(
            "--detail",
            help="The task's contract: objective · why · known · acceptance · boundaries. "
            "Printed to whoever claims the task.",
        ),
    ] = None,
    detail_file: Annotated[
        Path | None,
        typer.Option(
            "--detail-file",
            help="Read the contract from this file instead of --detail.",
            dir_okay=False,
        ),
    ] = None,
    rules: Annotated[
        list[str] | None,
        typer.Option(
            "--rules",
            help="Rules files the contract cites, by name: testing,git -> "
            ".aisquare/rules/testing.md, … A missing one refuses the add.",
        ),
    ] = None,
    role: Annotated[
        str | None, typer.Option("--role", help="Suggested owner role (advisory).")
    ] = None,
    needs: Annotated[
        list[str] | None,
        typer.Option(
            "--needs",
            help="Task this one depends on: its number (12, #12) or id (repeat for several).",
        ),
    ] = None,
    project: Annotated[
        str | None,
        typer.Option(
            "--project",
            help="A parent manager's delegation: add it on this member's board "
            "(a member of a group this board parents).",
        ),
    ] = None,
    origin: Annotated[
        str | None,
        typer.Option(
            "--origin",
            help="With --project: the task on this board it is delegated from; its status "
            "changes route back here.",
        ),
    ] = None,
    as_session: SessionRef = None,
) -> None:
    """Add a shared task. Re-adding the same task is safe — you get the original.

    The contract is checked for ``\\[team] brief_blocks`` (objective:, acceptance:,
    boundaries:): a missing block warns, or refuses under ``\\[team] brief_strict``."""
    missing: list[str] = []
    if detail is not None and detail_file is not None:
        fail("give --detail or --detail-file, not both", error="invalid_brief", exit_code=2)
    if detail_file is not None:
        try:
            detail = detail_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            fail(f"--detail-file {detail_file}: {exc}", error="invalid_brief", exit_code=2)
    if detail is not None or rules:
        try:
            brief = team_service.build_brief(detail or "", rules=rules or [])
        except team_service.BriefRefused as exc:
            fail(str(exc), error="invalid_brief", exit_code=2)
        detail, missing = brief.detail, brief.missing_blocks
    if missing:
        blocks = ", ".join(f"{block}:" for block in missing)
        typer.echo(
            f"aisquare: the contract has no {blocks} block — the claim prints the contract, "
            "so say it there",
            err=True,
        )
    try:
        task, created = team_service.add_task(
            title,
            key=key,
            detail=detail,
            role=role,
            needs=needs,
            session_ref=as_session,
            project=project,
            origin=origin,
        )
    except ValueError as exc:
        said = str(exc)
        delegation = said.startswith(("--project", "--origin"))
        fail(said, error="invalid_delegation" if delegation else "invalid_needs")
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
        payload["missing_blocks"] = missing
        typer.echo(json.dumps(payload))
    else:
        verb = "added" if created else "already tracked (idempotent)"
        stdout_console().print(
            f"✓ {verb}: {team_service.task_label(task)} {task.title}{receipt_suffix(delivery)}"
        )


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
    table.add_column("#", no_wrap=True, justify="right")
    table.add_column("ID", no_wrap=True)
    table.add_column("STATUS", no_wrap=True)
    table.add_column("CLAIMED BY", no_wrap=True)
    table.add_column("ROLE", no_wrap=True)
    table.add_column("TITLE")
    for task in tasks:
        table.add_row(
            f"#{task.number}" if task.number is not None else "",
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
    if task.number is not None:
        grid.add_row("number", f"#{task.number}")
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
    _emit_task(task, verb="claimed", contract=True)


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
    only: Annotated[
        str | None,
        typer.Option(
            "--task",
            help="Only this task (#n or id), when it is in --status and ready; else nothing.",
            metavar="TASK",
        ),
    ] = None,
    as_session: SessionRef = None,
) -> None:
    """Pick up the next available task — built for looped sessions."""
    if status not in _STATUSES:
        raise typer.BadParameter(f"status must be one of: {_STATUS_HELP}")
    narrowed: TaskStatus = status  # type: ignore[assignment]
    try:
        task = team_service.next_task(
            role=role, status=narrowed, claim=claim, session_ref=as_session, task_ref=only
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
    _emit_task(task, verb="claimed" if claim else "next up", contract=claim, peek=not claim)


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
    """Mark a task done (optionally sharing the outcome).

    A task linked to a brief's requirements needs a fresh pass from it for each
    (docs/work-briefs.md); without one, it stays as it is and each one is named."""
    try:
        task = team_service.finish_task(ref, note=note, session_ref=as_session)
    except briefs_service.TaskNotProven as exc:
        _not_proven(exc)
    except BriefChangedError as exc:
        fail(f"{exc}: run task done again", error="brief_changed", ref=exc.brief_id)
    except STORE_ERRORS as exc:
        _fail_team(exc, ref)
    _emit_task(task, verb="done")


def _not_proven(exc: briefs_service.TaskNotProven) -> NoReturn:
    """``task done`` refused by the evidence gate: each requirement, its state and why, in
    text on stderr and as a list under ``--json``."""
    label = team_service.task_label(exc.task)
    if get_state().json_output:
        typer.echo(
            json.dumps(
                {
                    "error": "task_not_proven",
                    "ref": exc.task.id,
                    "detail": str(exc),
                    "requirements": [
                        {"brief": i.brief, "requirement": i.requirement, "state": i.state,
                         "reason": i.reason}
                        for i in exc.items
                    ],
                },
                separators=(",", ":"),
            )
        )  # fmt: skip
    else:
        console = stderr_console()
        console.print(f"✗ {label} cannot be done: it needs a fresh pass for each of", markup=False)
        for item in exc.items:
            console.print(
                f"  {item.brief} {item.requirement} [{item.state}] {item.reason}", markup=False
            )
        first = next((i for i in exc.items if i.requirement.startswith("R")), None)
        console.print(
            "  "
            + briefs_service.record_hint(
                exc.task.project_id,
                exc.task.id,
                first.brief if first else "BRIEF",
                first.requirement if first else "R1",
            ),
            markup=False,
        )
    raise typer.Exit(1)


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
