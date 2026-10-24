"""``aisquare brief`` — work briefs: numbered requirements linked to board tasks."""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Annotated, TypeVar

import typer

from aisquare.cli.common import fail
from aisquare.cli.team import (
    STORE_ERRORS,
    SessionRef,
    _fail_team,
    delivery_fields,
    emit_write_warning,
    receipt_suffix,
)
from aisquare.core.console import stdout_console
from aisquare.core.state import get_state
from aisquare.core.store import BriefChangedError
from aisquare.models import BriefEvidence, BriefView, EvidenceVerdict, TeamTask, WorkBrief
from aisquare.services import briefs as service
from aisquare.services import team as team_service

app = typer.Typer(
    help="Work briefs: numbered requirements, linked to board tasks, with recorded checks.",
    no_args_is_help=True,
)

BriefRef = Annotated[
    str, typer.Argument(help="The brief's number on this board (B1, 1) or its id (prefix ok).")
]
RequirementRef = Annotated[str, typer.Argument(help="The requirement's stable id: R1, R2, …")]
TaskOption = Annotated[
    str, typer.Option("--task", help="The linked task: its number (12, #12) or id.")
]
Summary = Annotated[str, typer.Option("--summary", help="What the check found, in a sentence.")]

T = TypeVar("T")


class Verdict(StrEnum):
    """What a check found (``brief evidence --verdict``)."""

    PASS = "pass"
    FAIL = "fail"


def _plain(text: str) -> str:
    """Brief text is written by agents: no control character reaches the terminal."""
    return "".join(ch if ch in "\n\t" or unicodedata.category(ch)[0] != "C" else "�" for ch in text)


def _out(text: str) -> None:
    stdout_console().print(_plain(text), markup=False, highlight=False, soft_wrap=True)


@contextmanager
def _errors(as_session: str | None) -> Iterator[None]:
    try:
        yield
    except service.SourceRootError as exc:
        fail(str(exc), error="invalid_source_root", exit_code=2)
    except service.BriefNotFound as exc:
        fail(str(exc), error="not_found", ref=exc.ref)
    except BriefChangedError as exc:
        fail(str(exc), error="brief_changed", ref=exc.brief_id, hint="read the brief and retry")
    except ValueError as exc:  # pydantic's ValidationError is one
        fail(_plain(str(exc)), error="invalid_brief", detail=str(exc))
    except STORE_ERRORS as exc:
        # A KeyError names what was missing (the session, or a task); blame that.
        missing = str(exc.args[0]) if isinstance(exc, KeyError) and exc.args else as_session
        _fail_team(exc, missing)


def _call(as_session: str | None, work: Callable[[], T]) -> T:
    with _errors(as_session):
        return work()
    raise AssertionError("unreachable")  # pragma: no cover - fail() exits


def _pairs(values: list[str] | None, option: str) -> dict[str, str]:
    """``R1=text`` pairs, each requirement once."""
    result: dict[str, str] = {}
    for value in values or []:
        req_id, separator, text = value.partition("=")
        if not separator or not req_id.strip():
            fail(f"{option} takes R1=text, got {value!r}", error="usage", exit_code=2)
        key = req_id.strip().upper()
        if key in result:
            fail(f"{option} names {key} twice", error="usage", exit_code=2)
        result[key] = text
    return result


def _written(verb: str, brief: WorkBrief, changed: bool, extra: dict[str, object]) -> None:
    """A write's line: its receipt, or that nothing changed (an identical write)."""
    delivery = team_service.last_delivery() if changed else None
    emit_write_warning(delivery)
    if get_state().json_output:
        payload: dict[str, object] = {
            "changed": changed,
            **extra,
            "brief": brief.model_dump(mode="json"),
        }
        payload.update(delivery_fields(delivery))
        typer.echo(json.dumps(payload))
        return
    if not changed:
        _out(f"= unchanged: {brief.label} r{brief.revision} {brief.title} (nothing new)")
        return
    _out(f"✓ {verb}: {brief.label} r{brief.revision} {brief.title}{receipt_suffix(delivery)}")


def _task_ref(task_id: str, number: int | None) -> str:
    return f"#{number}" if number is not None else task_id


def _render(view: BriefView) -> None:
    brief = view.brief
    statuses = {status.id: status for status in view.requirements}
    _out(f"{brief.label} {brief.id} r{brief.revision}: {brief.title}")
    for requirement in brief.requirements:
        status = statuses[requirement.id]
        label = status.state + (" (manual)" if status.manual and status.state == "pass" else "")
        _out(f"  {requirement.id} r{requirement.revision} [{label}] {requirement.text}")
        if requirement.check:
            _out(f"      check: {requirement.check}")
        for proof in status.tasks:
            kind = f" [{proof.kind}]" if proof.kind else ""
            _out(
                f"      {_task_ref(proof.task_id, proof.task_number)} ({proof.task_status}) "
                f"{proof.state}{kind}: {proof.reason}"
            )
        if not status.tasks:
            _out(f"      {status.reason}")
    for heading, values in (("Assumptions", brief.assumptions), ("Boundaries", brief.boundaries)):
        if values:
            _out(f"{heading}:")
            for value in values:
                _out(f"  - {value}")
    if brief.evidence:
        _out("Evidence (manual evidence is for the validator to judge):")
        for evidence in brief.evidence:
            what = "finding" if evidence.finding else evidence.verdict
            artifact = f" ({evidence.artifact})" if evidence.artifact else ""
            _out(
                f"  {evidence.id} {evidence.requirement_id} r{evidence.requirement_revision} "
                f"{what}{_backing(evidence)} by {evidence.task_id}: {evidence.summary}{artifact}"
            )
    if brief.corrections:
        _out("Corrections:")
        for correction in brief.corrections:
            stale = f" (stale: {', '.join(correction.stale)})" if correction.stale else ""
            _out(f"  r{correction.revision}: {'; '.join(correction.changes)}{stale}")


@app.command("create")
def create(
    title: Annotated[str, typer.Argument(help="The user's request, in a line.")],
    requirement: Annotated[
        list[str],
        typer.Option("--requirement", "-r", help="One requirement; repeat. Numbered R1, R2, …"),
    ],
    check: Annotated[
        list[str] | None,
        typer.Option("--check", help="R1=the expected check for R1 (repeat)."),
    ] = None,
    assumption: Annotated[
        list[str] | None, typer.Option("--assumption", help="An assumption (repeat).")
    ] = None,
    boundary: Annotated[
        list[str] | None, typer.Option("--boundary", help="Out of scope (repeat).")
    ] = None,
    as_session: SessionRef = None,
) -> None:
    """Create a brief. Its requirements get stable ids, R1, R2, …, for its whole life."""
    checks = _pairs(check, "--check")
    brief = _call(
        as_session,
        lambda: service.create(
            title,
            requirement,
            checks=checks,
            assumptions=assumption,
            boundaries=boundary,
            session_ref=as_session,
        ),
    )
    _written("created", brief, True, {})


@app.command("list")
def list_(as_session: SessionRef = None) -> None:
    """List this board's briefs."""
    briefs, unreadable = _call(as_session, lambda: service.list_briefs(session_ref=as_session))
    if get_state().json_output:
        typer.echo(
            json.dumps(
                {
                    "briefs": [brief.model_dump(mode="json") for brief in briefs],
                    "unreadable": unreadable,
                }
            )
        )
        return
    if not briefs and not unreadable:
        _out('No briefs on this board. Create one: asq brief create "request" -r "requirement"')
    for brief in briefs:
        _out(
            f"{brief.label} {brief.id} r{brief.revision} "
            f"({len(brief.requirements)} requirements): {brief.title}"
        )
    if unreadable:
        _out(f"{unreadable} brief(s) on this board could not be read by this build.")


@app.command("show")
def show(ref: BriefRef, as_session: SessionRef = None) -> None:
    """Show a brief: each requirement's standing, its evidence and its corrections."""
    view = _call(as_session, lambda: service.show(ref, session_ref=as_session))
    if get_state().json_output:
        typer.echo(view.model_dump_json())
        return
    _render(view)


@app.command("update")
def update(
    ref: BriefRef,
    requirement: Annotated[
        list[str] | None,
        typer.Option("--requirement", "-r", help="R1=the corrected text (repeat)."),
    ] = None,
    check: Annotated[
        list[str] | None, typer.Option("--check", help="R1=the corrected check (repeat).")
    ] = None,
    add: Annotated[
        list[str] | None, typer.Option("--add", help="A new requirement, numbered next (repeat).")
    ] = None,
    title: Annotated[str | None, typer.Option("--title", help="A corrected title.")] = None,
    assumption: Annotated[
        list[str] | None,
        typer.Option("--assumption", help="Replaces the assumptions (repeat for several)."),
    ] = None,
    boundary: Annotated[
        list[str] | None,
        typer.Option("--boundary", help="Replaces the boundaries (repeat for several)."),
    ] = None,
    clear_assumptions: Annotated[
        bool, typer.Option("--clear-assumptions", help="Remove every assumption.")
    ] = False,
    clear_boundaries: Annotated[
        bool, typer.Option("--clear-boundaries", help="Remove every boundary.")
    ] = False,
    as_session: SessionRef = None,
) -> None:
    """Correct a brief; its ids stay.

    What changed makes its evidence stale,
    and reopens its tasks in review or done.
    Re-sending what is already there changes nothing."""
    changes = _pairs(requirement, "--requirement")
    checks = _pairs(check, "--check")
    if not (changes or checks or add or title or assumption or boundary) and not (
        clear_assumptions or clear_boundaries
    ):
        fail("nothing to correct: give -r, --check, --add, --title, --assumption or --boundary",
             error="nothing_to_correct")  # fmt: skip
    brief, changed = _call(
        as_session,
        lambda: service.update(
            ref,
            title=title,
            changes=changes,
            checks=checks,
            add=add,
            assumptions=assumption,
            boundaries=boundary,
            clear_assumptions=clear_assumptions,
            clear_boundaries=clear_boundaries,
            session_ref=as_session,
        ),
    )
    extra: dict[str, object] = {}
    if changed and brief.corrections:
        extra["stale"] = brief.corrections[-1].stale
    _written("corrected", brief, changed, extra)
    if changed and brief.corrections and not get_state().json_output:
        stale = brief.corrections[-1].stale
        if stale:
            _out(f"  stale now: {', '.join(stale)}")


@app.command("link")
def link(
    ref: BriefRef,
    task: Annotated[str, typer.Argument(help="The task: its number (12, #12) or id.")],
    requirement: Annotated[
        list[str], typer.Option("--requirement", "-r", help="A requirement it covers (repeat).")
    ],
    as_session: SessionRef = None,
) -> None:
    """Link a board task to requirements.

    A task can cover several requirements,
    and a requirement can need several tasks, each proving its own part."""
    brief, linked, changed = _call(
        as_session, lambda: service.link(ref, task, requirement, session_ref=as_session)
    )
    covered = ", ".join(dict.fromkeys(r.strip().upper() for r in requirement))
    verb = f"linked {team_service.task_label(linked)} to {covered} of"
    _written(verb, brief, changed, _task(linked))


def _task(task: TeamTask) -> dict[str, object]:
    return {"task_id": task.id, "task_number": task.number}


def _backing(evidence: BriefEvidence) -> str:
    """How a record is backed, as its lines label it: ``[report]``, ``[manual]`` for an
    artifact, and nothing for a finding no artifact shows (there is nothing to judge)."""
    if evidence.kind == "manual" and evidence.artifact is None:
        return ""
    return f" [{evidence.kind}]"


def _evidence_written(brief: WorkBrief, evidence: BriefEvidence, changed: bool) -> None:
    what = "finding" if evidence.finding else evidence.verdict
    verb = f"recorded {evidence.requirement_id} {what}{_backing(evidence)}"
    _written(verb, brief, changed, {"evidence": evidence.model_dump(mode="json")})
    if changed and not get_state().json_output and evidence.artifact is not None:
        _out("  manual evidence: the validator judges it; nothing checked it here")


@app.command("evidence")
def evidence(
    ref: BriefRef,
    requirement: RequirementRef,
    task: TaskOption,
    verdict: Annotated[Verdict, typer.Option("--verdict", help="pass or fail.")],
    summary: Summary,
    artifact: Annotated[
        Path | None,
        typer.Option(
            "--artifact", help="The file that shows it, such as a screenshot: manual evidence."
        ),
    ] = None,
    report: Annotated[
        str | None,
        typer.Option("--report", help="The saved command report (`asq exec --project`)."),
    ] = None,
    as_session: SessionRef = None,
) -> None:
    """Record a check by a linked task, backed by --report or --artifact.

    A --report pass needs an uninterrupted run that exited 0.
    Its output must be kept whole.
    Its source must be unchanged before the run, after it and now.
    Artifact-backed evidence is MANUAL, and labelled so everywhere.
    Only the validator can judge it.
    A fail has `brief finding`'s effect."""
    narrowed: EvidenceVerdict = "pass" if verdict is Verdict.PASS else "fail"
    brief, recorded, changed = _call(
        as_session,
        lambda: service.record_evidence(
            ref,
            requirement,
            task_ref=task,
            verdict=narrowed,
            summary=summary,
            artifact=artifact,
            report_id=report,
            session_ref=as_session,
        ),
    )
    _evidence_written(brief, recorded, changed)


@app.command("check")
def check(
    ref: BriefRef,
    source_root: Annotated[
        Path | None,
        typer.Option(
            "--source-root",
            help="Compare every pass with this one checkout (the project's, or a worktree).",
        ),
    ] = None,
    as_session: SessionRef = None,
) -> None:
    """Check every requirement against the source now.

    Exits 1 while any is missing, failed, blocked, stale or unlinked (no live task).
    Exits 2 for a --source-root that is not a checkout of the project."""
    result = _call(
        as_session, lambda: service.check(ref, source_root=source_root, session_ref=as_session)
    )
    if get_state().json_output:
        typer.echo(result.model_dump_json())
    else:
        brief = result.brief
        verdict = "PROVEN" if result.complete else "NOT PROVEN"
        against = f" against {result.source_root}" if result.source_root else ""
        _out(f"{brief.label} r{brief.revision} {brief.title}: {verdict}{against}")
        for status in result.requirements:
            label = status.state + (" (manual)" if status.manual and status.state == "pass" else "")
            _out(f"  {status.id} [{label}] {status.reason}")
            # Every linked task when there are several: a [missing] requirement listed
            # only the task that passed, never the one still missing (re-gate, n3).
            for proof in status.tasks:
                if len(status.tasks) > 1 or proof.state != status.state:
                    _out(
                        f"      {_task_ref(proof.task_id, proof.task_number)} {proof.state}: "
                        f"{proof.reason}"
                    )
        manual = [status.id for status in result.requirements if status.manual]
        if manual:
            _out(f"Manual evidence, for the validator to judge: {', '.join(manual)}")
    if not result.complete:
        raise typer.Exit(1)


@app.command("finding")
def finding(
    ref: BriefRef,
    requirement: RequirementRef,
    task: TaskOption,
    summary: Summary,
    artifact: Annotated[
        Path | None, typer.Option("--artifact", help="A file that shows the failure.")
    ] = None,
    as_session: SessionRef = None,
) -> None:
    """Record a failure found in a task's work on a requirement.

    A task in review or done goes back to todo,
    one being worked on keeps its owner, and a dropped one stays dropped.
    Three distinct failures by one task on one revision of a requirement
    block the task, so the manager re-plans it."""
    brief, recorded, changed = _call(
        as_session,
        lambda: service.finding(
            ref,
            requirement,
            task_ref=task,
            summary=summary,
            artifact=artifact,
            session_ref=as_session,
        ),
    )
    _evidence_written(brief, recorded, changed)
