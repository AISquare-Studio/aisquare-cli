"""Native work contracts, linked board tasks and evidence checks."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import typer
from pydantic import BaseModel

from aisquare.cli.common import fail
from aisquare.core.state import get_state
from aisquare.services import work_briefs as service

app = typer.Typer(
    help="Native requirements, linked tasks and fresh evidence.", no_args_is_help=True
)
Ref = Annotated[str, typer.Argument(help="Brief id (unambiguous prefix accepted).")]
Requirements = Annotated[
    list[str], typer.Option("--requirement", "-r", help="Repeat for each outcome.")
]


@contextmanager
def _errors() -> Iterator[None]:
    try:
        yield
    except (ValueError, KeyError, OSError, sqlite3.Error) as exc:
        fail(str(exc), error="brief_error")


def _emit(value: BaseModel) -> None:
    if get_state().json_output:
        typer.echo(value.model_dump_json())
    elif isinstance(value, service.WorkBrief):
        typer.echo(service.export_markdown(value))
    elif isinstance(value, service.BriefCheck):
        verdict = "VERIFIED" if value.complete else "NOT VERIFIED"
        typer.echo(f"{value.brief_id} r{value.revision}: {verdict}")
        for requirement in value.requirements:
            typer.echo(f"{requirement.requirement_id} [{requirement.status}]: {requirement.reason}")
        for warning in value.warnings:
            typer.echo(f"Warning: {warning}")
        if value.manual_evidence:
            typer.echo(
                "Manual evidence (requires independent validator review): "
                + ", ".join(value.manual_evidence)
            )


def _pairs(values: list[str] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values or []:
        req_id, separator, text = value.partition("=")
        if not separator or not req_id or not text.strip():
            raise ValueError("use R1=corrected text (repeat for other requirements)")
        if req_id in result:
            raise ValueError(f"{req_id} was supplied more than once")
        result[req_id] = text
    return result


@app.command("create")
def create(
    title: Annotated[str, typer.Argument(help="The requested change.")],
    requirement: Requirements,
    assumption: Annotated[list[str] | None, typer.Option("--assumption")] = None,
    boundary: Annotated[list[str] | None, typer.Option("--boundary")] = None,
) -> None:
    """Create a brief; its requirements receive stable R1, R2… ids."""
    with _errors():
        _emit(service.create(title, requirement, assumptions=assumption, boundaries=boundary))


@app.command("list")
def list_() -> None:
    """List briefs on this project's existing board."""
    with _errors():
        briefs = service.list_briefs()
        if get_state().json_output:
            typer.echo(json.dumps([brief.model_dump(mode="json") for brief in briefs]))
        else:
            for brief in briefs:
                typer.echo(f"{brief.id} r{brief.revision}: {brief.title}")
            if not briefs:
                typer.echo('No briefs yet. Use: asq brief create "change" -r "outcome"')


@app.command("show")
def show(ref: Ref) -> None:
    """Read the authoritative stored contract and evidence history."""
    with _errors():
        _emit(service.show(ref))


@app.command("update")
def update(
    ref: Ref,
    requirement: Annotated[
        list[str] | None, typer.Option("--requirement", "-r", help="R1=new text.")
    ] = None,
    add: Annotated[list[str] | None, typer.Option("--add", help="Add a new outcome.")] = None,
    expected_check: Annotated[
        list[str] | None, typer.Option("--check", help="R1=expected check.")
    ] = None,
    source_revision: Annotated[str | None, typer.Option("--source-revision")] = None,
    affected: Annotated[
        list[str] | None, typer.Option("--affected", help="Affected R-number; default all.")
    ] = None,
    assumption: Annotated[list[str] | None, typer.Option("--assumption")] = None,
    boundary: Annotated[list[str] | None, typer.Option("--boundary")] = None,
) -> None:
    """Apply corrections. Changed requirements/source invalidate affected evidence."""
    with _errors():
        if not any((requirement, add, expected_check, source_revision, assumption, boundary)):
            raise ValueError("provide a correction, --add, --check or --source-revision")
        _emit(
            service.update(
                ref,
                changes=_pairs(requirement),
                add=add,
                checks=_pairs(expected_check),
                source_revision=source_revision,
                affected=affected,
                assumptions=assumption,
                boundaries=boundary,
            )
        )


@app.command("link")
def link(ref: Ref, task: Annotated[str, typer.Argument()], requirement: Requirements) -> None:
    """Link an existing task to one or more requirements; does not duplicate tasks."""
    with _errors():
        _emit(service.link(ref, task, requirement))


@app.command("evidence")
def evidence(
    ref: Ref,
    requirement: Annotated[str, typer.Argument(help="Stable requirement id, e.g. R1.")],
    task: Annotated[str, typer.Option("--task")],
    verdict: Annotated[str, typer.Option("--verdict", help="pass, fail or blocked.")],
    summary: Annotated[str, typer.Option("--summary")],
    artifact: Annotated[
        Path | None,
        typer.Option("--artifact", help="Manual screenshot/report; not machine verified."),
    ] = None,
    report: Annotated[
        str | None, typer.Option("--report", help="Source-bound saved command report ID.")
    ] = None,
    as_session: Annotated[str | None, typer.Option("--as")] = None,
) -> None:
    """Record an actual check. A failed check reopens its task; repeated failures block."""
    with _errors():
        if verdict not in ("pass", "fail", "blocked"):
            raise ValueError("verdict must be pass, fail or blocked")
        narrowed: service.Verdict = verdict  # type: ignore[assignment]
        _emit(
            service.record_evidence(
                ref,
                requirement,
                task_ref=task,
                verdict=narrowed,
                summary=summary,
                artifact=artifact,
                report_id=report,
                session_id=as_session,
            )
        )


@app.command("finding")
def finding(
    ref: Ref,
    requirement: Annotated[str, typer.Argument()],
    summary: Annotated[str, typer.Option("--summary")],
    artifact: Annotated[Path, typer.Option("--artifact")],
    task: Annotated[str | None, typer.Option("--task")] = None,
) -> None:
    """Report a failure; reuse its linked task or create one deduplicated correction."""
    with _errors():
        _emit(service.finding(ref, requirement, summary=summary, artifact=artifact, task_ref=task))


@app.command("check")
def check(
    ref: Ref,
    source_root: Annotated[
        Path | None,
        typer.Option("--source-root", help="Require proof for this assembled checkout."),
    ] = None,
) -> None:
    """Check current coverage; exit 1 for missing, failed, blocked or stale proof."""
    with _errors():
        result = service.check(ref, source_root=source_root)
        _emit(result)
        if not result.complete:
            raise typer.Exit(1)


@app.command("export")
def export(
    ref: Ref,
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
) -> None:
    """Export a readable snapshot (JSON with --json); never a second editable source."""
    with _errors():
        brief = service.show(ref)
        text = (
            brief.model_dump_json(indent=2)
            if get_state().json_output
            else service.export_markdown(brief)
        )
        if output is None:
            typer.echo(text)
        else:
            # Exclusive creation prevents silently replacing an existing user file.
            with output.open("x", encoding="utf-8") as handle:
                handle.write(text + "\n")
            typer.echo(str(output.resolve()))


@app.command("mode")
def mode(
    value: Annotated[str, typer.Argument(help="native or off; affects new sessions.")],
) -> None:
    """Select working rules independently of personality. Existing sessions retain theirs."""
    with _errors():
        selected = service.set_mode(value)
        typer.echo(
            json.dumps({"working_mode": selected})
            if get_state().json_output
            else f"Working mode: {selected}. Applies to new sessions; personas are unchanged."
        )
