"""Native requirements and evidence on the existing team board.

A brief is a versioned contract, never a second task queue. Requirement ids
survive corrections; linked tasks remain the ordinary ``team_task`` records.
All mutations use a compare-and-swap write with their board event, so two
workers cannot silently overwrite each other's evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from aisquare.core import harness, insights, orchestrator
from aisquare.core.ids import new_event_id
from aisquare.core.source_revision import source_fingerprint, source_root_for
from aisquare.core.store import ContextStore, store_session
from aisquare.models import TeamEvent, TeamTask
from aisquare.services import command_reports

Verdict = Literal["pass", "fail", "blocked"]


class Requirement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    text: str = Field(min_length=1, max_length=2000)
    revision: int = 1
    source_revision: str = "initial"
    expected_check: str = ""
    task_ids: list[str] = Field(default_factory=list)


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    requirement_id: str
    requirement_revision: int
    source_revision: str
    task_id: str
    verdict: Verdict
    summary: str = Field(min_length=1, max_length=4000)
    artifact: str
    artifact_sha256: str
    source_fingerprint: str
    source_root: str
    provenance: Literal["manual", "command"] = "manual"
    report_id: str | None = None
    report_sha256: str | None = None
    recorded_at: datetime
    session_id: str | None = None


class WorkBrief(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    project_id: str
    title: str = Field(min_length=1, max_length=500)
    revision: int = 1
    requirements: list[Requirement] = Field(min_length=1, max_length=64)
    assumptions: list[str] = Field(default_factory=list)
    boundaries: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list, max_length=512)
    created_at: datetime
    updated_at: datetime


class RequirementCoverage(BaseModel):
    requirement_id: str
    text: str
    status: Literal["pass", "fail", "blocked", "missing-task", "missing-evidence", "stale"]
    reason: str
    task_ids: list[str]
    evidence_id: str | None = None
    provenance: Literal["manual", "command"] | None = None


class BriefCheck(BaseModel):
    brief_id: str
    revision: int
    complete: bool
    requirements: list[RequirementCoverage]
    warnings: list[str] = Field(default_factory=list)
    manual_evidence: list[str] = Field(default_factory=list)


def _scope(store: ContextStore, cwd: Path | None = None) -> str:
    project = orchestrator.team_project(cwd)
    store.ensure_project(project)
    return project.id


def _load(store: ContextStore, ref: str, project_id: str) -> WorkBrief:
    if not ref.strip():
        # An empty prefix matches every brief; with one brief it would "resolve".
        raise ValueError("a brief id (or unambiguous prefix) is required")
    data = store.get_work_brief(ref, project_id)
    if data is None:
        raise KeyError(f"brief {ref!r} is not on this project's board")
    return WorkBrief.model_validate_json(data)


def referenced_report_ids(store: ContextStore | None = None) -> set[str]:
    """Every command report some recorded evidence points at, across all boards.

    Report retention consults this so it never deletes the file that backs a
    recorded pass; a missing report would flip a VERIFIED brief to NOT VERIFIED.
    """
    from aisquare.services import project as projects

    def collect(active: ContextStore) -> set[str]:
        found: set[str] = set()
        for info in projects.list_projects():
            for data in active.work_briefs(info.id):
                brief = WorkBrief.model_validate_json(data)
                found.update(e.report_id for e in brief.evidence if e.report_id is not None)
        return found

    if store is not None:
        return collect(store)
    with store_session() as active:
        return collect(active)


def _requirement(brief: WorkBrief, ref: str) -> Requirement:
    for requirement in brief.requirements:
        if requirement.id == ref:
            return requirement
    raise ValueError(f"unknown requirement {ref!r}; use its stable R-number")


def _event(brief: WorkBrief, kind: str, text: str, task_id: str | None = None) -> TeamEvent:
    return TeamEvent(
        id=new_event_id(),
        project_id=brief.project_id,
        kind=kind,
        text=f"{brief.id} r{brief.revision}: {text}",
        task_id=task_id,
        created_at=datetime.now(UTC),
    )


def _save(
    store: ContextStore,
    brief: WorkBrief,
    *,
    expected: int | None,
    kind: str,
    text: str,
    task_statuses: dict[str, str] | None = None,
) -> WorkBrief:
    brief.updated_at = datetime.now(UTC)
    event = _event(brief, kind, text)
    if kind == "brief_evidence" and brief.evidence:
        event.session_id = brief.evidence[-1].session_id
        event.task_id = brief.evidence[-1].task_id
    store.save_work_brief(
        brief.id,
        brief.project_id,
        brief.revision,
        brief.model_dump_json(),
        expected,
        event,
        task_statuses or {},
    )
    # Read the committed records back; traces carry the same ids/sequence that
    # the Board shows. No persona-rendered text enters this factual path.
    for event_id in [event.id, *[f"{event.id}-{task_id}" for task_id in task_statuses or {}]]:
        stored = store.get_event(event_id)
        if stored is None:
            raise RuntimeError("brief write committed but its event could not be read back")
        insights.record_team_event(
            event_kind=stored.kind,
            text=stored.text,
            event_id=stored.id,
            session_id=stored.session_id,
            project_id=stored.project_id,
            task_id=stored.task_id,
            seq=stored.seq,
        )
    if task_statuses or kind == "brief_updated":
        from aisquare.services.team import _nudge_manager

        _nudge_manager(brief.project_id, reason=kind)
    return brief


def create(
    title: str,
    requirements: list[str],
    *,
    assumptions: list[str] | None = None,
    boundaries: list[str] | None = None,
    cwd: Path | None = None,
) -> WorkBrief:
    """Create a contract with stable sequential requirement ids."""
    cleaned = [text.strip() for text in requirements]
    if not cleaned or any(not text for text in cleaned):
        raise ValueError("at least one non-empty requirement is required")
    if len(set(text.casefold() for text in cleaned)) != len(cleaned):
        raise ValueError("duplicate requirements: keep one requirement and link its tasks")
    with store_session() as store:
        now = datetime.now(UTC)
        brief = WorkBrief(
            id=f"brief_{uuid4().hex[:12]}",
            project_id=_scope(store, cwd),
            title=title.strip(),
            requirements=[Requirement(id=f"R{i}", text=t) for i, t in enumerate(cleaned, 1)],
            assumptions=assumptions or [],
            boundaries=boundaries or [],
            created_at=now,
            updated_at=now,
        )
        return _save(store, brief, expected=None, kind="brief_created", text=brief.title)


def show(ref: str, *, cwd: Path | None = None) -> WorkBrief:
    with store_session() as store:
        return _load(store, ref, _scope(store, cwd))


def list_briefs(*, cwd: Path | None = None) -> list[WorkBrief]:
    with store_session() as store:
        return [
            WorkBrief.model_validate_json(data) for data in store.work_briefs(_scope(store, cwd))
        ]


def update(
    ref: str,
    *,
    changes: dict[str, str] | None = None,
    add: list[str] | None = None,
    checks: dict[str, str] | None = None,
    source_revision: str | None = None,
    affected: list[str] | None = None,
    assumptions: list[str] | None = None,
    boundaries: list[str] | None = None,
    cwd: Path | None = None,
) -> WorkBrief:
    """Corrections supersede old text; source changes stale affected evidence.

    Without --affected, a source change invalidates ALL requirements. Changing
    assumptions/boundaries also invalidates all: their effect cannot be inferred.
    Evidence is retained as history, and done tasks return to todo atomically.
    """
    with store_session() as store:
        brief = _load(store, ref, _scope(store, cwd))
        expected = brief.revision
        touched: set[str] = set()
        for req_id, text in (changes or {}).items():
            requirement = _requirement(brief, req_id)
            requirement.text = Requirement(id=req_id, text=text.strip()).text
            touched.add(req_id)
        for req_id, text in (checks or {}).items():
            _requirement(brief, req_id).expected_check = text.strip()
            touched.add(req_id)
        for req_id in affected or []:
            _requirement(brief, req_id)
        if affected and source_revision is None:
            raise ValueError("--affected requires --source-revision")
        if source_revision is not None:
            if not source_revision.strip() or len(source_revision) > 200:
                raise ValueError("source revision must be non-empty and at most 200 characters")
            for requirement in brief.requirements:
                if (
                    affected is None or requirement.id in affected
                ) and requirement.source_revision != source_revision:
                    requirement.source_revision = source_revision
                    touched.add(requirement.id)
        if assumptions is not None or boundaries is not None:
            touched.update(req.id for req in brief.requirements)
        if assumptions is not None:
            brief.assumptions = assumptions
        if boundaries is not None:
            brief.boundaries = boundaries
        statuses: dict[str, str] = {}
        for requirement in brief.requirements:
            if requirement.id in touched:
                requirement.revision += 1
                for task_id in requirement.task_ids:
                    task = store.get_task(task_id)
                    # Finished work returns to the pool; work in progress keeps its
                    # owner (the correction reaches them through the brief), and a
                    # dropped task stays dropped.
                    if task is not None and task.status in ("done", "review"):
                        statuses[task_id] = "todo"
        for text in add or []:
            new_id = f"R{max(int(r.id[1:]) for r in brief.requirements) + 1}"
            brief.requirements.append(Requirement(id=new_id, text=text.strip()))
        if len(brief.requirements) > 64:
            raise ValueError("a brief supports at most 64 requirements; split larger work")
        texts = [r.text.casefold() for r in brief.requirements]
        if len(set(texts)) != len(texts):
            raise ValueError("duplicate requirements after correction")
        brief.revision += 1
        return _save(
            store,
            brief,
            expected=expected,
            kind="brief_updated",
            text=f"corrected {', '.join(sorted(touched)) or 'requirements'}; "
            f"read `asq brief show {brief.id}` before continuing",
            task_statuses=statuses,
        )


def link(
    ref: str, task_ref: str, requirement_ids: list[str], *, cwd: Path | None = None
) -> WorkBrief:
    """Link an existing task; never create a parallel task list."""
    if not requirement_ids:
        raise ValueError("at least one --requirement is required")
    with store_session() as store:
        brief = _load(store, ref, _scope(store, cwd))
        task = store.get_task(task_ref)
        if task is None or task.project_id != brief.project_id:
            raise ValueError("task is missing or belongs to another board")
        expected = brief.revision
        changed = False
        for req_id in requirement_ids:
            requirement = _requirement(brief, req_id)
            if task.id not in requirement.task_ids:
                requirement.task_ids.append(task.id)
                changed = True
        if not changed:
            return brief
        brief.revision += 1
        return _save(store, brief, expected=expected, kind="brief_linked", text=f"linked {task.id}")


def _artifact(path: Path) -> tuple[str, str]:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except RuntimeError as exc:
        # Python 3.12 reports a symlink loop as RuntimeError; callers handle OSError
        # and ValueError, so it must become one of those rather than a traceback.
        raise ValueError(f"evidence artifact unavailable: {path} ({exc})") from None
    # is_file() follows links and is False for FIFOs, sockets and devices, so an
    # unbounded read of /dev/zero or a blocking pipe never starts.
    if not resolved.is_file():
        raise ValueError(f"evidence artifact must be an existing regular file: {path}")
    if resolved.stat().st_size > 20_000_000:
        raise ValueError("evidence artifact exceeds 20 MB; use a focused report")
    return str(resolved), hashlib.sha256(resolved.read_bytes()).hexdigest()


def _report_digest(report_id: str) -> str:
    report = command_reports.load_report(report_id)
    digest = hashlib.sha256(report.model_dump_json().encode())
    for stream in ("stdout", "stderr"):
        name: Literal["stdout", "stderr"] = stream
        digest.update(stream.encode() + b"\0")
        digest.update(command_reports.read_stream(report_id, name, raw=True))
    return digest.hexdigest()


def record_evidence(
    ref: str,
    requirement_id: str,
    *,
    task_ref: str,
    verdict: Verdict,
    summary: str,
    artifact: Path | None = None,
    report_id: str | None = None,
    session_id: str | None = None,
    cwd: Path | None = None,
) -> WorkBrief:
    """Record a check against the current requirement/source and an existing artifact.

    Repeating identical evidence is idempotent. Distinct failures reopen the same
    linked task; three failures block it for human re-planning instead of looping.
    """
    if (artifact is None) == (report_id is None):
        raise ValueError("provide exactly one --artifact (manual) or --report (command evidence)")
    report = command_reports.load_report(report_id) if report_id is not None else None
    report_digest = _report_digest(report_id) if report_id is not None else None
    artifact_path = (
        command_reports.reports_dir() / report_id / "report.json"
        if report_id is not None
        else artifact
    )
    assert artifact_path is not None
    path, digest = _artifact(artifact_path)
    with store_session() as store:
        brief = _load(store, ref, _scope(store, cwd))
        requirement = _requirement(brief, requirement_id)
        task = store.get_task(task_ref)
        if (
            task is None
            or task.project_id != brief.project_id
            or task.id not in requirement.task_ids
        ):
            raise ValueError("link this task to the requirement before recording evidence")
        if session_id is not None:
            session = store.get_session(session_id)
            if session is None or session.project_id != brief.project_id:
                raise ValueError("evidence session is missing or on another board")
        project = store.get_project(brief.project_id)
        if project is None:
            raise ValueError("brief project is unavailable")
        source_root = source_root_for(project.root, cwd)
        fingerprint = source_fingerprint(source_root)
        if report is not None:
            if report.project_id != brief.project_id:
                raise ValueError("report must be captured with --project for this brief's board")
            if report.task_id is not None and report.task_id != task.id:
                raise ValueError("report belongs to a different task")
            if verdict == "pass":
                if not report.completed or report.exit_code != 0 or report.launch_error:
                    raise ValueError("a failed command cannot be recorded as pass")
                if (
                    report.source_capture_error
                    or not report.source_fingerprint_before
                    or report.source_fingerprint_before != report.source_fingerprint_after
                    or report.source_fingerprint_after != fingerprint
                ):
                    raise ValueError(
                        "command evidence is stale or source changed during the check; rerun"
                    )
                if report.stdout.truncated or report.stderr.truncated:
                    raise ValueError(
                        "truncated command evidence cannot certify a pass; retain the full check"
                    )
            # A failed old check stays attached to its actual source, never
            # relabelled as evidence for whatever source happens to exist now.
            fingerprint = report.source_fingerprint_after or "unknown-command-source"
        same_lane = [
            e for e in brief.evidence if e.requirement_id == requirement_id and e.task_id == task.id
        ]
        # Idempotent only against the LATEST record of this lane: an identical
        # failure re-reported after a later pass is new information and must
        # supersede that pass, not vanish as a duplicate.
        previous = same_lane[-1] if same_lane else None
        if (
            previous is not None
            and previous.requirement_revision == requirement.revision
            and previous.source_revision == requirement.source_revision
            and previous.verdict == verdict
            and previous.summary == summary
            and previous.artifact_sha256 == digest
            and previous.source_fingerprint == fingerprint
            and previous.report_id == report_id
            and previous.report_sha256 == report_digest
        ):
            return brief
        if len(brief.evidence) >= 512:
            raise ValueError("brief has 512 evidence records; export and create a follow-up brief")
        expected = brief.revision
        evidence = Evidence(
            id=f"evidence_{uuid4().hex[:12]}",
            requirement_id=requirement_id,
            requirement_revision=requirement.revision,
            source_revision=requirement.source_revision,
            task_id=task.id,
            verdict=verdict,
            summary=summary,
            artifact=path,
            artifact_sha256=digest,
            source_fingerprint=fingerprint,
            source_root=report.source_root or str(source_root) if report else str(source_root),
            provenance="command" if report is not None else "manual",
            report_id=report_id,
            report_sha256=report_digest,
            recorded_at=datetime.now(UTC),
            session_id=session_id,
        )
        brief.evidence.append(evidence)
        brief.revision += 1
        statuses: dict[str, str] = {}
        if verdict in ("fail", "blocked") and task.status != "dropped":
            # Per task and per requirement REVISION: a correction to the requirement
            # starts the count again, and one task's failures never block another.
            failures = sum(
                e.requirement_id == requirement_id
                and e.task_id == task.id
                and e.requirement_revision == requirement.revision
                and e.verdict == "fail"
                for e in brief.evidence
            )
            if verdict == "blocked" or failures >= 3:
                statuses[task.id] = "blocked"
            elif task.status in ("review", "done"):
                statuses[task.id] = "todo"
            # A task in progress keeps its owner and status: the failure reaches the
            # worker through the brief, and stripping the claim would orphan the work.
        return _save(
            store,
            brief,
            expected=expected,
            kind="brief_evidence",
            text=f"{requirement_id} {verdict}: {summary}",
            task_statuses=statuses,
        )


def finding(
    ref: str,
    requirement_id: str,
    *,
    summary: str,
    artifact: Path,
    task_ref: str | None = None,
    cwd: Path | None = None,
) -> WorkBrief:
    """Attach a finding to an existing task or create one idempotent correction task."""
    from aisquare.services import team

    # Validate before creating/linking a correction task. Invalid evidence must
    # not leave a new board task behind; record_evidence validates again on save.
    _artifact(artifact)
    brief = show(ref, cwd=cwd)
    requirement = _requirement(brief, requirement_id)
    if task_ref is None:
        if requirement.task_ids:
            task_ref = requirement.task_ids[0]
        else:
            task, _ = team.add_task(
                f"Correct {requirement_id}: {requirement.text[:150]}",
                key=f"{brief.id}-{requirement_id}-correction",
                detail=summary,
                role="coder",
                cwd=cwd,
            )
            task_ref = task.id
    link(ref, task_ref, [requirement_id], cwd=cwd)
    return record_evidence(
        ref,
        requirement_id,
        task_ref=task_ref,
        verdict="fail",
        summary=summary,
        artifact=artifact,
        cwd=cwd,
    )


def coverage(
    brief: WorkBrief,
    tasks: list[TeamTask],
    *,
    source_root: Path | None = None,
) -> BriefCheck:
    """Every linked task needs fresh proof; optionally require one assembled source.

    By default evidence is checked against the checkout actually tested. Shared
    boards can legitimately contain worktrees and different repositories. A
    validator can explicitly request a single assembled --source-root instead.
    """
    indexed = {task.id: task for task in tasks}
    fingerprints: dict[str, str] = {}
    rows: list[RequirementCoverage] = []
    for requirement in brief.requirements:
        row = RequirementCoverage(
            requirement_id=requirement.id,
            text=requirement.text,
            status="missing-task",
            reason="No live linked task",
            task_ids=requirement.task_ids,
        )
        linked = [indexed[t] for t in requirement.task_ids if t in indexed]
        if (
            not linked
            or len(linked) != len(requirement.task_ids)
            or any(t.status == "dropped" for t in linked)
        ):
            rows.append(row)
            continue
        outcomes: list[RequirementCoverage] = []
        for task in linked:
            outcome = row.model_copy(deep=True)
            outcome.status, outcome.reason = "missing-evidence", f"No recorded check for {task.id}"
            evidence = [
                e
                for e in brief.evidence
                if e.requirement_id == requirement.id and e.task_id == task.id
            ]
            latest = evidence[-1] if evidence else None
            if latest is not None:
                outcome.evidence_id = latest.id
                outcome.provenance = latest.provenance
                outcome.status, outcome.reason = "stale", "Requirement/source changed; check again"
                root = str(source_root.resolve()) if source_root else latest.source_root
                if root not in fingerprints:
                    try:
                        fingerprints[root] = source_fingerprint(Path(root))
                    except (OSError, ValueError, subprocess.SubprocessError):
                        fingerprints[root] = ""
                if (
                    latest.requirement_revision == requirement.revision
                    and latest.source_revision == requirement.source_revision
                    and latest.source_fingerprint == fingerprints[root]
                ):
                    try:
                        _, digest = _artifact(Path(latest.artifact))
                        if (
                            latest.report_id is not None
                            and _report_digest(latest.report_id) != latest.report_sha256
                        ):
                            digest = ""
                    except (OSError, ValueError):
                        digest = ""
                    if digest == latest.artifact_sha256:
                        outcome.status, outcome.reason = latest.verdict, latest.summary
                    else:
                        outcome.reason = "Evidence artifact changed or unavailable; check again"
            if task.status == "blocked":
                outcome.status, outcome.reason = "blocked", f"Linked task {task.id} is blocked"
            outcomes.append(outcome)
        row = next((outcome for outcome in outcomes if outcome.status != "pass"), outcomes[-1])
        if any(outcome.provenance == "manual" for outcome in outcomes):
            row.provenance = "manual"
        rows.append(row)
    warnings: list[str] = []
    # Semantic contradictions require planner review; no keyword heuristic is
    # presented as a complete contradiction detector.
    overlap = set(t.casefold().strip() for t in brief.boundaries) & {
        r.text.casefold().strip() for r in brief.requirements
    }
    if overlap:
        warnings.append("A boundary exactly repeats a required outcome; clarify the contradiction")
    return BriefCheck(
        brief_id=brief.id,
        revision=brief.revision,
        complete=bool(rows) and all(r.status == "pass" for r in rows) and not warnings,
        requirements=rows,
        warnings=warnings,
        manual_evidence=[r.requirement_id for r in rows if r.provenance == "manual"],
    )


def check(
    ref: str,
    *,
    cwd: Path | None = None,
    source_root: Path | None = None,
) -> BriefCheck:
    with store_session() as store:
        brief = _load(store, ref, _scope(store, cwd))
        project = store.get_project(brief.project_id)
        if project is None:
            raise ValueError("brief project is unavailable")
        if source_root is not None:
            resolved = source_root.expanduser().resolve()
            root = project.root.resolve()
            # A typo here must not read as "everything is stale": the strict check
            # only makes sense against a checkout of THIS project.
            if not resolved.is_dir():
                raise ValueError(f"--source-root is not a directory: {source_root}")
            if not (
                resolved == root or resolved.is_relative_to(root) or (resolved / ".git").exists()
            ):
                raise ValueError(
                    f"--source-root must be this project's checkout or a git worktree of it: "
                    f"{resolved}"
                )
            source_root = resolved
        return coverage(brief, store.team_tasks(brief.project_id), source_root=source_root)


def task_gate(store: ContextStore, task: TeamTask) -> dict[str, int]:
    """A linked task cannot be declared done with missing, failed or stale evidence."""
    snapshot: dict[str, int] = {}
    for data in store.work_briefs(task.project_id):
        brief = WorkBrief.model_validate_json(data)
        snapshot[brief.id] = brief.revision
        linked = {r.id for r in brief.requirements if task.id in r.task_ids}
        if not linked:
            continue
        project = store.get_project(task.project_id)
        if project is None:
            raise ValueError("brief project is unavailable")
        # Task completion proves this task's contribution. The final brief
        # check still requires ALL linked contributions, including later ones.
        task_brief = brief.model_copy(deep=True)
        task_brief.requirements = [r for r in task_brief.requirements if r.id in linked]
        for requirement in task_brief.requirements:
            requirement.task_ids = [task.id]
        result = coverage(task_brief, store.team_tasks(task.project_id), source_root=None)
        failed = [
            r.requirement_id
            for r in result.requirements
            if r.requirement_id in linked and r.status != "pass"
        ]
        if failed or result.warnings:
            raise ValueError(
                f"brief {brief.id} is not verified: {', '.join(failed)}; "
                "record fresh evidence before task done"
            )
    return snapshot


def export_markdown(brief: WorkBrief) -> str:
    lines = [
        f"# {brief.title}",
        "",
        f"Brief: {brief.id} · revision {brief.revision}",
        "",
        "This export is a snapshot. Change the stored brief with `asq brief update`.",
        "",
    ]
    for requirement in brief.requirements:
        lines += [
            f"## {requirement.id}: {requirement.text}",
            f"Requirement revision {requirement.revision}; source {requirement.source_revision}",
            f"Expected check: {requirement.expected_check or 'define before checking'}",
            f"Tasks: {', '.join(requirement.task_ids) or 'not linked'}",
            "",
        ]
    for heading, values in (("Assumptions", brief.assumptions), ("Boundaries", brief.boundaries)):
        lines += [f"## {heading}", *[f"- {v}" for v in values], ""]
    lines += [
        "## Evidence history",
        *[
            f"- {e.id}: {e.requirement_id} {e.verdict} [{e.provenance}] — "
            f"{e.summary} ({e.artifact})"
            for e in brief.evidence
        ],
        "",
    ]
    return "\n".join(lines)


def set_mode(mode: str, *, cwd: Path | None = None) -> str:
    if mode not in ("native", "off"):
        raise ValueError("working mode must be native or off")
    with store_session() as store:
        store.set_meta(f"work_mode/{_scope(store, cwd)}", mode)
    return mode


def session_context(store: ContextStore, project_id: str, session_id: str, role: str) -> str:
    """One shared direct/fleet entry point. Personas are deliberately not imported.

    A session records its selected rules at its first briefing. Resumes keep
    those rules: changing project mode is a new-session choice.
    """
    key = f"work_rules/{session_id}"
    version = store.get_meta(key)
    if version is None:
        version = (
            "off"
            if store.get_meta(f"work_mode/{project_id}") == "off"
            else harness.WORK_RULES_VERSION
        )
        store.set_meta(key, version)
    lines = [f"Working rules: {version}"]
    if version != "off":
        # The text is pinned per session AND per role: a session re-launched under
        # another role gets that role's habits, not the first role's.
        base = harness.base_role(role)
        saved_rules = store.get_meta(f"work_rules_text/{session_id}")
        if saved_rules is None or store.get_meta(f"work_rules_role/{session_id}") != base:
            saved_rules = json.dumps(harness.working_rules(role))
            store.set_meta(f"work_rules_text/{session_id}", saved_rules)
            store.set_meta(f"work_rules_role/{session_id}", base)
        lines.extend(str(line) for line in json.loads(saved_rules))
        lines.append(
            f"For source-bound check evidence use `asq exec --project {project_id} "
            "--task TASK -- pytest ...`, then `asq brief evidence BRIEF R1 --task TASK "
            "--verdict pass --summary 'actual check' --report REPORT_ID`."
        )
    # `mode off` means no native instructions at all; the requirements below are
    # facts about the project and are shown either way.
    # Requirements are factual project state, not a work-mode preference.
    task_id = os.environ.get("AISQUARE_TASK_ID")
    for data in store.work_briefs(project_id):
        brief = WorkBrief.model_validate_json(data)
        relevant = [r for r in brief.requirements if not task_id or task_id in r.task_ids]
        if not relevant:
            continue
        lines.append(f"Work brief {brief.id} r{brief.revision}: {brief.title}")
        for requirement in relevant:
            if sum(len(line) for line in lines) > 5000:
                lines.append(f"More requirements omitted: `asq brief show {brief.id}`.")
                break
            lines.append(
                f"{requirement.id} r{requirement.revision} [{requirement.source_revision}]: "
                f"{requirement.text}; check: {requirement.expected_check or 'define'}"
            )
        lines.append(f"Full contract and corrections: `asq brief show {brief.id}`.")
        if sum(len(line) for line in lines) > 7000:
            lines.append("More requirements available with `asq brief list` and `asq brief show`.")
            break
    return "\n<aisquare-work>\n" + "\n".join(lines) + "\n</aisquare-work>"
