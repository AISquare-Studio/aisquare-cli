"""Work briefs: one user request as numbered requirements, linked to board tasks.

A brief is a contract on the board, never a second task queue: its requirements
(``R1``, ``R2``, …) link to ordinary ``team_task`` rows, and a recorded check names
the requirement, the revision of it that was checked, and the task whose work it
proves (docs/work-briefs.md). Requirement ids are stable for the brief's life; a
correction bumps the revision of what it changed, which makes the checks recorded
against the old revision stale, and keeps the old text in the requirement's history.

Every write is one store transaction (:meth:`~aisquare.core.store.SqliteStore.save_brief`):
the brief compared on its revision and replaced, the tasks it sends back moved, and
its board events written, each attributed to the ``--as`` session, so the session
that wrote it is not woken by it.

A check is backed by an artifact (``manual``: only the validator can judge it, and its
checkout's whole-tree fingerprint is kept) or by a saved command report (``report``:
``asq exec --project``, whose per-file source identity before and after the run is
compared with the checkout now, file by file). :func:`gate` is what ``task done`` asks
of a linked task, and :func:`check` what ``brief check`` asks of a whole brief.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from aisquare.core import atomic, insights, orchestrator, paths
from aisquare.core.ids import new_brief_id, new_event_id, new_evidence_id
from aisquare.core.source_revision import (
    _toplevel,
    fingerprint_of,
    source_file_hashes,
    source_root_for,
    source_unchanged,
)
from aisquare.core.store import BriefRow, ContextStore, TaskTransition, store_session
from aisquare.models import (
    BriefCheck,
    BriefCorrection,
    BriefEvidence,
    BriefRequirement,
    BriefView,
    EvidenceVerdict,
    RequirementState,
    RequirementStatus,
    RequirementVersion,
    TaskProof,
    TeamEvent,
    TeamSession,
    TeamTask,
    WorkBrief,
)
from aisquare.services import command_reports, team

#: Distinct failures by one task against one revision of a requirement that block
#: the task, so the manager re-plans instead of the task looping through review.
FAILURES_TO_BLOCK = 3

#: The largest artifact a check may name: it is copied into the store's home, and the
#: copy's digest is read on every ``brief show``.
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024

#: Worst first: a requirement reads as the worst of its live linked tasks' proofs.
_SEVERITY: tuple[RequirementState, ...] = ("blocked", "fail", "stale", "missing", "pass")


class BriefNotFound(KeyError):
    """``ref`` names no brief on the caller's board."""

    def __init__(self, ref: str, board: str) -> None:
        super().__init__(ref)
        self.ref = ref
        self.board = board

    def __str__(self) -> str:
        return f"no brief {self.ref!r} on this board ({self.board})"


@dataclass(frozen=True)
class _Write:
    """What one brief write leaves for the board: its event, the tasks it moves."""

    kind: str
    text: str
    task_id: str | None = None
    transitions: Sequence[TaskTransition] = ()


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _parse(row: BriefRow) -> WorkBrief:
    """The stored document as a brief, its number from the row."""
    brief = WorkBrief.model_validate_json(row.data)
    brief.number = row.number
    return brief


def _document(brief: WorkBrief) -> str:
    return brief.model_dump_json(exclude={"number"})


def _clean(values: Sequence[str] | None) -> list[str]:
    """Stripped, empty entries left out, order kept."""
    return [value.strip() for value in values or () if value.strip()]


def _unique_texts(requirements: Sequence[BriefRequirement]) -> None:
    seen: dict[str, str] = {}
    for requirement in requirements:
        key = requirement.text.casefold()
        if key in seen:
            raise ValueError(
                f"{requirement.id} repeats {seen[key]}: keep one requirement and link its tasks"
            )
        seen[key] = requirement.id


def _requirement(brief: WorkBrief, ref: str) -> BriefRequirement:
    wanted = ref.strip().upper()
    for requirement in brief.requirements:
        if requirement.id == wanted:
            return requirement
    known = ", ".join(r.id for r in brief.requirements)
    raise ValueError(f"{brief.label} has no requirement {ref!r} (it has {known})")


def _session(store: ContextStore, ref: str | None) -> TeamSession | None:
    """The ``--as`` session, which must exist (``KeyError`` otherwise, as for tasks)."""
    return team._resolve_session(store, ref)


def _load(store: ContextStore, board_id: str, board_name: str, ref: str) -> WorkBrief:
    if not ref.strip():
        raise ValueError("name a brief: its number (B1) or its id")
    row = store.get_brief(board_id, ref)
    if row is None:
        raise BriefNotFound(ref, board_name)
    return _parse(row)


def _linked_task(store: ContextStore, board_id: str, ref: str) -> TeamTask:
    task = team.find_task(store, ref, board_id)
    if task is None:
        raise KeyError(ref)
    if task.project_id != board_id:
        raise ValueError(f"task {team.task_label(task)} is on another board than this brief")
    return task


@dataclass(frozen=True)
class _Artifact:
    """An artifact as read for recording: where it was, its bytes, their SHA-256."""

    source: Path
    body: bytes
    sha256: str


def _read_artifact(path: Path) -> _Artifact:
    """The artifact named on the command line. A regular file, and at most
    :data:`MAX_ARTIFACT_BYTES`: a FIFO or a device would block or never end the read."""
    try:
        resolved = path.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"artifact {path} cannot be read ({exc})") from None
    if not resolved.is_file():
        raise ValueError(f"artifact {path} is not a regular file")
    limit = MAX_ARTIFACT_BYTES // (1024 * 1024)
    if resolved.stat().st_size > MAX_ARTIFACT_BYTES:
        raise ValueError(f"artifact {path} is over the {limit} MiB limit; record a smaller one")
    with resolved.open("rb") as handle:
        body = handle.read(MAX_ARTIFACT_BYTES + 1)
    if len(body) > MAX_ARTIFACT_BYTES:  # it grew between the stat and the read
        raise ValueError(f"artifact {path} is over the {limit} MiB limit; record a smaller one")
    return _Artifact(source=resolved, body=body, sha256=hashlib.sha256(body).hexdigest())


def _private_dirs(directory: Path) -> None:
    """Make ``directory`` and each level of it under the home's ``briefs/``, every one
    owner-only: made by ``mkdir(parents=True)``, only the leaf would take the mode, and
    ``briefs/`` and ``briefs/<project>/`` would list brief ids to every local user. A
    level an earlier build made wider is narrowed. POSIX bits; on Windows the copies
    themselves are restricted (:func:`_keep_copy`)."""
    root = paths.aisquare_home() / "briefs"
    root.parent.mkdir(parents=True, exist_ok=True)
    level = root
    for part in (None, *directory.relative_to(root).parts):
        if part is not None:
            level = level / part
        level.mkdir(exist_ok=True, mode=0o700)
        if sys.platform != "win32":
            os.chmod(level, 0o700)


def _keep_copy(brief: WorkBrief, evidence_id: str, artifact: _Artifact) -> Path:
    """Copy the artifact under the store's home, owner-only, and return the copy's path.

    The record cites the copy, so a check stays what it was when the original goes:
    a temporary directory cleaned, a worktree reaped. The original's path is kept as
    provenance only.
    """
    directory = paths.brief_evidence_dir(brief.project_id, brief.id)
    _private_dirs(directory)
    suffix = artifact.source.suffix
    if not (len(suffix) <= 16 and suffix[1:].isalnum()):
        suffix = ""
    copy = directory / f"{evidence_id}{suffix}"
    atomic.write_replacing(copy, artifact.body, keep_mode=False, owner_only=True)
    return copy


def _copy_unchanged(evidence: BriefEvidence) -> bool:
    """Whether the kept copy of a check's artifact is still the bytes recorded."""
    if evidence.artifact is None:
        return True
    try:
        with Path(evidence.artifact).open("rb") as handle:
            body = handle.read(MAX_ARTIFACT_BYTES + 1)
    except OSError:
        return False
    return hashlib.sha256(body).hexdigest() == evidence.artifact_sha256


class _Sources:
    """The source of each checkout a check names, as it is now: read once per root.

    ``override`` is ``brief check --source-root``: every pass is then compared with that
    one assembled checkout, not the one it was recorded in.
    """

    def __init__(self, override: Path | None = None) -> None:
        self.override = override
        self._files: dict[str, dict[str, str] | str] = {}

    def root(self, recorded: str | None) -> str | None:
        return str(self.override) if self.override is not None else recorded

    def files(self, root: str) -> dict[str, str]:
        """``root``'s per-file identity now; ``ValueError`` (with why) when unreadable."""
        cached = self._files.get(root)
        if cached is None:
            try:
                cached = source_file_hashes(Path(root))
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                cached = f"the source in {root} cannot be read ({exc})"
            self._files[root] = cached
        if isinstance(cached, str):
            raise ValueError(cached)
        return cached


def _report_digest(report_id: str) -> str:
    """One digest of a saved report: its metadata and both streams' originals. A report
    changed after its evidence was recorded no longer backs it."""
    report = command_reports.load_report(report_id)
    digest = hashlib.sha256(report.model_dump_json().encode())
    streams: tuple[Literal["stdout", "stderr"], ...] = ("stdout", "stderr")
    for stream in streams:
        digest.update(b"\0" + stream.encode() + b"\0")
        digest.update(command_reports.read_stream(report_id, stream, raw=True))
    return digest.hexdigest()


def _source_stale(evidence: BriefEvidence, sources: _Sources) -> str | None:
    """Why a recorded pass no longer describes the source in its checkout; None when it
    still does.

    Report evidence is judged against the REPORT's own source root, file by file: a file
    the check wrote that did not exist before it (``--junitxml``) is new output and is
    ignored, while a file that existed and changed or went makes the pass stale. Manual
    evidence keeps the whole-tree fingerprint of the checkout it was recorded in, so any
    change there does. ``sources.override`` replaces both roots.
    """
    if evidence.kind == "report":
        report_id = evidence.report_id or ""
        try:
            report = command_reports.load_report(report_id)
            if _report_digest(report_id) != evidence.report_sha256:
                return f"its command report {report_id[:12]} changed since it was recorded"
            saved = command_reports.load_source_files(report_id)
        except (OSError, ValueError):
            return f"its command report {report_id[:12]} is gone or unreadable"
        root = sources.root(report.source_root)
        if root is None:
            return "its command report holds no source identity"
        try:
            current = sources.files(root)
        except ValueError as exc:
            return str(exc)
        if saved is not None:
            if source_unchanged(saved.before, current):
                return None
        elif fingerprint_of(current) == report.source_fingerprint_before:
            return None
        return f"the source in {root} changed since the check ran"
    root = sources.root(evidence.source_root)
    if root is None or evidence.source_fingerprint is None:
        return "recorded without a source identity; check it again"
    try:
        current = sources.files(root)
    except ValueError as exc:
        return str(exc)
    if fingerprint_of(current) == evidence.source_fingerprint:
        return None
    return f"the source in {root} changed since the check"


def _pass_refusals(
    report: command_reports.CommandReport, saved: command_reports.SourceFiles | None
) -> list[str]:
    """Why a saved command run cannot back a pass: every rule, each said."""
    why: list[str] = []
    if report.launch_error is not None:
        why.append(f"it could not start ({report.launch_error})")
    elif report.exit_code != 0:
        why.append(f"it exited {report.exit_code}")
    if report.interrupted_by is not None:
        why.append(f"it was interrupted (signal {report.interrupted_by})")
    if not report.output_complete:
        why.append("its output was not kept whole")
    if not report.source_known or report.source_root is None:
        cause = report.source_capture_error or "run it with `asq exec --project`"
        why.append(f"its source identity is unknown ({cause})")
        return why
    if saved is not None and saved.after is not None:
        changed_while = not source_unchanged(saved.before, saved.after)
    else:
        changed_while = report.source_fingerprint_before != report.source_fingerprint_after
    if changed_while:
        why.append("the source changed while it ran")
    try:
        current = source_file_hashes(Path(report.source_root))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        why.append(f"the source in {report.source_root} cannot be read now ({exc})")
        return why
    unchanged = (
        source_unchanged(saved.before, current)
        if saved is not None
        else fingerprint_of(current) == report.source_fingerprint_before
    )
    if not unchanged:
        why.append(f"the source in {report.source_root} has changed since it ran; run it again")
    return why


def _event(brief: WorkBrief, write: _Write, session: TeamSession | None) -> TeamEvent:
    return TeamEvent(
        id=new_event_id(),
        project_id=brief.project_id,
        session_id=session.id if session else None,
        kind=write.kind,
        text=f"{brief.label} r{brief.revision}: {write.text}",
        task_id=write.task_id,
        created_at=_now(),
    )


def _publish(
    stored: Sequence[TeamEvent], board: team._Board, writer: str | None, cwd: Path | None
) -> None:
    """After the commit: spool the events, publish the receipt, wake a manager when a task
    moved. None of it raises past the write but the read-back, as for every board write."""
    for event in stored:
        insights.record_team_event(
            event_kind=event.kind,
            text=event.text,
            event_id=event.id,
            session_id=event.session_id,
            project_id=event.project_id,
            task_id=event.task_id,
            seq=event.seq,
        )
    team._record_delivery(stored[0], board)
    moved = stored[1:]
    if moved:
        with store_session() as store:
            target = team._nudge_target(store, writer, board.id, cwd)
        team._nudge_manager(target, reason=moved[0].kind, board=board.id)


def _write_brief(
    store: ContextStore,
    brief: WorkBrief,
    write: _Write,
    session: TeamSession | None,
) -> list[TeamEvent]:
    expected = brief.revision
    brief.revision += 1
    brief.updated_at = _now()
    return store.save_brief(
        brief.id,
        brief.project_id,
        expected=expected,
        revision=brief.revision,
        data=_document(brief),
        updated_at=brief.updated_at,
        event=_event(brief, write, session),
        transitions=write.transitions,
    )


def _reopen_text(task: TeamTask, brief: WorkBrief, why: str) -> str:
    """A moved task's event text, naming the revision the write is about to make.
    ``<title> — <why>`` is the shape ``team.stopped_because`` reads back."""
    return f"{task.title} — {brief.label} r{brief.revision + 1}: {why}"


# --- reads -----------------------------------------------------------------------


def _read_board(store: ContextStore, session_ref: str | None, cwd: Path | None) -> team._Board:
    team._require_enabled()
    session = _session(store, session_ref)
    return team._board_of(store, team._caller_board_id(store, session, cwd))


def status_of(
    brief: WorkBrief, tasks: Sequence[TeamTask], *, sources: _Sources | None = None
) -> list[RequirementStatus]:
    """Each requirement's standing, from its live linked tasks' latest checks.

    A linked task that was dropped, or is gone from the board, is left out, so a dropped
    duplicate never makes its requirement unprovable while a live linked task remains;
    with none left, the requirement is ``unlinked``. For each live one, its latest check
    of the requirement: none is ``missing``; one against an older revision, or whose
    artifact changed or went since, is ``stale``; else its verdict. A blocked task is
    ``blocked`` whatever it recorded. The requirement reads as the worst of them, and a
    pass that rests on manual evidence says so.

    With ``sources``, a pass is also checked against the source in its checkout now
    (:func:`_source_stale`); without, it is the pass as recorded, which is what the
    session start shows (reading a whole checkout costs a hook too much).
    """
    indexed = {task.id: task for task in tasks}
    statuses: list[RequirementStatus] = []
    for requirement in brief.requirements:
        live = [
            indexed[task_id]
            for task_id in requirement.task_ids
            if task_id in indexed and indexed[task_id].status != "dropped"
        ]
        if not live:
            statuses.append(
                RequirementStatus(id=requirement.id, state="unlinked", reason="no live linked task")
            )
            continue
        proofs: list[TaskProof] = []
        for task in live:
            latest = next(
                (
                    e
                    for e in reversed(brief.evidence)
                    if e.requirement_id == requirement.id and e.task_id == task.id
                ),
                None,
            )
            proof = TaskProof(
                task_id=task.id,
                task_number=task.number,
                task_status=task.status,
                state="missing",
                reason="no check recorded",
            )
            if latest is not None:
                proof.evidence_id, proof.kind = latest.id, latest.kind
                if latest.requirement_revision != requirement.revision:
                    proof.state = "stale"
                    proof.reason = (
                        f"checked at r{latest.requirement_revision}, "
                        f"the requirement is now r{requirement.revision}"
                    )
                elif latest.verdict == "pass" and not _copy_unchanged(latest):
                    # Only a pass rests on its artifact. A failure stays a failure
                    # whatever becomes of the file that showed it.
                    proof.state, proof.reason = "stale", "its kept artifact changed or is gone"
                elif (
                    latest.verdict == "pass"
                    and sources is not None
                    and (why := _source_stale(latest, sources)) is not None
                ):
                    proof.state, proof.reason = "stale", why
                else:
                    proof.state, proof.reason = latest.verdict, latest.summary
            if task.status == "blocked":
                proof.state, proof.reason = "blocked", f"task {team.task_label(task)} is blocked"
            proofs.append(proof)
        worst = min(proofs, key=lambda p: _SEVERITY.index(p.state))
        statuses.append(
            RequirementStatus(
                id=requirement.id,
                state=worst.state,
                reason=worst.reason,
                manual=any(p.state == "pass" and p.kind == "manual" for p in proofs),
                tasks=proofs,
            )
        )
    return statuses


def show(ref: str, *, session_ref: str | None = None, cwd: Path | None = None) -> BriefView:
    """One brief in full, with each requirement's standing (:func:`status_of`)."""
    with store_session() as store:
        board = _read_board(store, session_ref, cwd)
        brief = _load(store, board.id, board.name, ref)
        tasks = store.team_tasks(board.id)
    return BriefView(brief=brief, requirements=status_of(brief, tasks, sources=_Sources()))


def check(
    ref: str,
    *,
    source_root: Path | None = None,
    session_ref: str | None = None,
    cwd: Path | None = None,
) -> BriefCheck:
    """Every requirement's standing with each pass checked against the source now; the
    brief is ``complete`` only when every one is a pass.

    By default a pass is compared with the checkout it was recorded in. ``source_root``
    compares every pass with that one checkout, the assembled one a validator checks; it
    must be this project's checkout or a worktree of it, since "everything is stale" is
    the wrong answer to a typo.
    """
    with store_session() as store:
        board = _read_board(store, session_ref, cwd)
        brief = _load(store, board.id, board.name, ref)
        tasks = store.team_tasks(board.id)
        project = store.get_project(board.id, include_forgotten=True)
    override = None
    if source_root is not None:
        override = _checkout_of(source_root, brief, project.root if project else None)
    statuses = status_of(brief, tasks, sources=_Sources(override))
    return BriefCheck(
        brief=brief,
        requirements=statuses,
        complete=all(status.state == "pass" for status in statuses),
        source_root=str(override) if override is not None else None,
    )


def _checkout_of(path: Path, brief: WorkBrief, project_root: Path | None) -> Path:
    """``--source-root`` resolved, when it is a checkout of the brief's project."""
    resolved = path.expanduser().resolve()
    if not resolved.is_dir():
        raise ValueError(f"--source-root {path} is not a directory")
    same_board = orchestrator.team_project(resolved).id == brief.project_id
    whole = resolved == (project_root.resolve() if project_root else None) or (
        _toplevel(resolved) == resolved
    )
    if not (same_board and whole):
        raise ValueError(
            f"--source-root {resolved} is not a checkout of this brief's project"
            + (f" ({project_root})" if project_root else "")
            + ": name the project's directory or one of its worktrees"
        )
    return resolved


# --- the gate ----------------------------------------------------------------------

#: How ``task done`` names each requirement's state (docs/plans/evidence-gate.md, part 3).
_GATE_WORDS: dict[RequirementState, str] = {
    "missing": "missing",
    "unlinked": "missing",
    "fail": "failed",
    "stale": "stale",
    "blocked": "blocked",
    "pass": "pass",
}


@dataclass(frozen=True)
class GateItem:
    """One requirement a task cannot be done without: where, what state, and why."""

    brief: str
    requirement: str
    state: str
    reason: str


class TaskNotProven(ValueError):
    """``task done`` on a linked task without a fresh pass from it for every requirement
    it is linked to. Carries each requirement's state and reason."""

    def __init__(self, task: TeamTask, items: Sequence[GateItem]) -> None:
        self.task = task
        self.items = list(items)
        listed = "; ".join(f"{i.brief} {i.requirement} {i.state}: {i.reason}" for i in items)
        super().__init__(f"{team.task_label(task)} is not proven: {listed}")


def _own_part(brief: WorkBrief, linked: Sequence[BriefRequirement], task_id: str) -> WorkBrief:
    """``brief`` as one task's part of it: only the requirements it is linked to, each as
    if it were their only task. A task proves its own contribution; other tasks linked
    to the same requirement are theirs to prove."""
    return brief.model_copy(
        update={"requirements": [r.model_copy(update={"task_ids": [task_id]}) for r in linked]}
    )


def gate(store: ContextStore, task: TeamTask) -> dict[str, int]:
    """What ``task done`` asks first: every requirement ``task`` is linked to needs a fresh
    pass recorded by ``task`` itself. Raises :class:`TaskNotProven` naming each one that
    has none; returns the revision of each brief it read, which the status write compares
    in its own transaction, so a correction between the two fails the completion.

    Only the briefs that name the task are read, so a damaged brief elsewhere on the board
    never blocks it; one that names it and cannot be read refuses, saying so. A task no
    brief links is not gated at all.
    """
    snapshot: dict[str, int] = {}
    items: list[GateItem] = []
    tasks: list[TeamTask] | None = None
    sources = _Sources()
    for row in store.briefs(task.project_id):
        if task.id not in row.data:
            continue
        try:
            brief = _parse(row)
        except ValidationError as exc:
            snapshot[row.id] = row.revision
            first = exc.errors()[0]["msg"] if exc.errors() else str(exc)
            items.append(
                GateItem(
                    f"B{row.number}",
                    "-",
                    "unreadable",
                    f"it names this task but cannot be read ({first})",
                )
            )
            continue
        linked = [r for r in brief.requirements if task.id in r.task_ids]
        if not linked:
            continue
        snapshot[row.id] = row.revision
        if tasks is None:
            tasks = store.team_tasks(task.project_id)
        for status in status_of(_own_part(brief, linked, task.id), tasks, sources=sources):
            if status.state != "pass":
                items.append(
                    GateItem(brief.label, status.id, _GATE_WORDS[status.state], status.reason)
                )
    if items:
        raise TaskNotProven(task, items)
    return snapshot


# --- the session start ---------------------------------------------------------------

#: The most the requirements block may add to a session start. Claude Code cuts hook
#: output at 10,000 characters, and the board and the role cycle come first.
REQUIREMENTS_BUDGET = 2_500

#: Each requirement's text and check is clipped to this in the block; `brief show` has
#: them whole.
_CLIP = 160


def _clip(text: str, limit: int = _CLIP) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def session_requirements(store: ContextStore, session: TeamSession, task: TeamTask | None) -> str:
    """The session start's block: the requirements linked to this session's task (its
    assignment, and any task it holds), each with its state as recorded. Empty when
    none is linked. Bounded by :data:`REQUIREMENTS_BUDGET`; the caller fails open.
    """
    tasks = store.team_tasks(session.project_id)
    mine = [t for t in tasks if t.claimed_by == session.id and t.status == "doing"]
    wanted = list({t.id: t for t in ([task] if task else []) + mine}.values())
    if not wanted:
        return ""
    rows = store.briefs(session.project_id)
    lines: list[str] = []
    omitted = False

    def size() -> int:
        return sum(len(line) + 1 for line in lines)

    for own_task in wanted:
        for row in rows:
            if own_task.id not in row.data:
                continue
            try:
                brief = _parse(row)
            except ValidationError:
                lines.append(f"B{row.number} names #{own_task.number} but cannot be read here.")
                continue
            linked = [r for r in brief.requirements if own_task.id in r.task_ids]
            if not linked:
                continue
            states = {s.id: s for s in status_of(_own_part(brief, linked, own_task.id), tasks)}
            lines.append(
                f"{team.task_label(own_task)} is linked to {brief.label} r{brief.revision} "
                f'"{_clip(brief.title, 80)}": `task done` needs a fresh pass from it for each.'
            )
            for requirement in linked:
                status = states[requirement.id]
                state = "pass, as recorded" if status.state == "pass" else status.state
                line = f"  {requirement.id} [{state}] {_clip(requirement.text)}"
                if requirement.check:
                    line += f" — check: {_clip(requirement.check)}"
                lines.append(line)
                if size() > REQUIREMENTS_BUDGET:
                    lines.pop()
                    omitted = True
                    break
            if omitted:
                break
        if omitted:
            break
    if not lines:
        return ""
    if omitted:
        lines.append("More are linked: `asq brief show` has them all.")
    lines.append(
        "Record a check: `asq exec --project PROJECT --task TASK -- CMD`, then "
        "`asq brief evidence BRIEF R1 --task TASK --verdict pass --summary … --report ID`. "
        "`asq brief check BRIEF` re-checks each pass against the source."
    )
    return "\n<aisquare-requirements>\n" + "\n".join(lines) + "\n</aisquare-requirements>"


# --- report retention ----------------------------------------------------------------

_REPORT_ID_FIELD = re.compile(r'"report_id"\s*:\s*"([0-9a-f]{32})"')


def referenced_report_ids() -> set[str]:
    """Every command report recorded evidence cites, on every board, a forgotten project's
    included: report retention never deletes one (``command_reports``). Read from the
    stored documents' text, so a brief this build cannot parse still protects its
    reports. Raises when the store cannot be read, which retention takes as unknown.
    """
    with store_session() as store:
        return {
            report_id
            for data in store.all_brief_documents()
            for report_id in _REPORT_ID_FIELD.findall(data)
        }


def list_briefs(
    *, session_ref: str | None = None, cwd: Path | None = None
) -> tuple[list[WorkBrief], int]:
    """The board's briefs by number, and how many rows could not be read (a newer build
    wrote them, or they are damaged): those cost their own line, never the list."""
    with store_session() as store:
        board = _read_board(store, session_ref, cwd)
        rows = store.briefs(board.id)
    briefs: list[WorkBrief] = []
    unreadable = 0
    for row in rows:
        try:
            briefs.append(_parse(row))
        except ValidationError:
            unreadable += 1
    return briefs, unreadable


# --- writes ----------------------------------------------------------------------


def create(
    title: str,
    requirements: Sequence[str],
    *,
    checks: dict[str, str] | None = None,
    assumptions: Sequence[str] | None = None,
    boundaries: Sequence[str] | None = None,
    session_ref: str | None = None,
    cwd: Path | None = None,
) -> WorkBrief:
    """A new brief on the board; its requirements numbered ``R1``, ``R2``, … in order.

    ``checks`` maps a requirement id to its expected check.
    """
    team._require_enabled()
    texts = _clean(requirements)
    if not texts:
        raise ValueError("a brief needs at least one requirement (-r)")
    now = _now()
    built = [BriefRequirement(id=f"R{n}", text=text) for n, text in enumerate(texts, 1)]
    brief = WorkBrief(
        id=new_brief_id(),
        project_id="",
        title=title.strip(),
        requirements=built,
        assumptions=_clean(assumptions),
        boundaries=_clean(boundaries),
        created_at=now,
        updated_at=now,
    )
    for req_id, check in (checks or {}).items():
        _requirement(brief, req_id).check = check.strip() or None
    _unique_texts(brief.requirements)
    team._DELIVERY.set(None)
    with store_session() as store:
        session = _session(store, session_ref)
        board = team._board(store, session, cwd, write=True)
        brief.project_id = board.id
        brief.created_by = session.id if session else None
        brief = WorkBrief.model_validate(brief.model_dump())  # the limits, on the whole
        event = TeamEvent(
            id=new_event_id(),
            project_id=board.id,
            session_id=brief.created_by,
            kind="brief_created",
            text=f"{brief.title} ({len(brief.requirements)} requirements)",
            created_at=now,
        )
        number, stored = store.add_brief(brief.id, board.id, _document(brief), now, event)
    brief.number = number
    _publish([stored], board, brief.created_by, cwd)
    return brief


def update(
    ref: str,
    *,
    title: str | None = None,
    changes: dict[str, str] | None = None,
    checks: dict[str, str] | None = None,
    add: Sequence[str] | None = None,
    assumptions: Sequence[str] | None = None,
    boundaries: Sequence[str] | None = None,
    clear_assumptions: bool = False,
    clear_boundaries: bool = False,
    session_ref: str | None = None,
    cwd: Path | None = None,
) -> tuple[WorkBrief, bool]:
    """Correct a brief; the brief, and whether anything changed.

    Only a value that differs from the stored one is a change, so re-sending an
    identical correction writes nothing: no revision, nothing stale, no task moved.
    A changed requirement text or check bumps that requirement's revision, which makes
    its recorded checks stale, and keeps the old version in its history. Changed
    assumptions or boundaries bump every requirement, since what they change cannot be
    told apart. A linked task in review or done whose requirement went stale goes back
    to todo; a task someone is working on keeps its owner, and a dropped one stays
    dropped. ``assumptions`` and ``boundaries`` replace the lists whole.
    """
    team._require_enabled()
    team._DELIVERY.set(None)
    with store_session() as store:
        session = _session(store, session_ref)
        board = team._board(store, session, cwd, write=True)
        brief = _load(store, board.id, board.name, ref)
        now = _now()
        lines: list[str] = []
        stale: set[str] = set()

        def supersede(requirement: BriefRequirement) -> None:
            if requirement.id in stale:
                return
            requirement.history.append(
                RequirementVersion(
                    revision=requirement.revision,
                    text=requirement.text,
                    check=requirement.check,
                    superseded_at=now,
                    superseded_by=session.id if session else None,
                )
            )
            stale.add(requirement.id)

        if title is not None and title.strip() and title.strip() != brief.title:
            lines.append(f"title: {brief.title!r} → {title.strip()!r}")
            brief.title = title.strip()
        for req_id, text in (changes or {}).items():
            requirement = _requirement(brief, req_id)
            if not text.strip():
                raise ValueError(f"{requirement.id}: a requirement cannot be empty")
            if text.strip() != requirement.text:
                supersede(requirement)
                lines.append(f"{requirement.id} text: {requirement.text!r} → {text.strip()!r}")
                requirement.text = text.strip()
        for req_id, check in (checks or {}).items():
            requirement = _requirement(brief, req_id)
            if (check.strip() or None) != requirement.check:
                supersede(requirement)
                lines.append(f"{requirement.id} check: {requirement.check!r} → {check.strip()!r}")
                requirement.check = check.strip() or None
        for field, values, clear in (
            ("assumptions", assumptions, clear_assumptions),
            ("boundaries", boundaries, clear_boundaries),
        ):
            if values is None and not clear:
                continue
            new = [] if clear and values is None else _clean(values)
            if new != getattr(brief, field):
                lines.append(f"{field}: {getattr(brief, field)} → {new}")
                setattr(brief, field, new)
                for requirement in brief.requirements:
                    supersede(requirement)
        existing = {r.text.casefold() for r in brief.requirements}
        # A requirement already there is not added again: re-sending an identical
        # `--add` is a no-op, like every other identical correction.
        added = [text for text in dict.fromkeys(_clean(add)) if text.casefold() not in existing]
        for text in added:
            new_id = f"R{max(int(r.id[1:]) for r in brief.requirements) + 1}"
            brief.requirements.append(BriefRequirement(id=new_id, text=text))
            lines.append(f"added {new_id}: {text!r}")
        if not lines:
            return brief, False
        for requirement in brief.requirements:
            if requirement.id in stale:
                requirement.revision += 1
        _unique_texts(brief.requirements)
        brief = WorkBrief.model_validate(brief.model_dump())
        transitions: list[TaskTransition] = []
        corrected = ", ".join(sorted(stale, key=_rank))
        why = f"corrected {corrected}; read `asq brief show {brief.label}`"
        for task in _linked(store, brief, stale):
            if task.status in ("review", "done"):
                transitions.append((task.id, "reopen", _reopen_text(task, brief, why)))
        brief.corrections.append(
            BriefCorrection(
                revision=brief.revision + 1,
                changes=lines,
                stale=sorted(stale, key=_rank),
                at=now,
                session_id=session.id if session else None,
            )
        )
        summary = "; ".join(lines)
        stored = _write_brief(
            store,
            brief,
            _Write("brief_updated", summary if len(summary) <= 500 else summary[:497] + "…",
                   transitions=transitions),
            session,
        )  # fmt: skip
    _publish(stored, board, session.id if session else None, cwd)
    return brief, True


def _rank(req_id: str) -> int:
    return int(req_id[1:])


def _linked(store: ContextStore, brief: WorkBrief, req_ids: set[str]) -> list[TeamTask]:
    """The board tasks linked to any of ``req_ids``, each once, in link order."""
    seen: dict[str, TeamTask] = {}
    for requirement in brief.requirements:
        if requirement.id not in req_ids:
            continue
        for task_id in requirement.task_ids:
            task = store.get_task(task_id, project_ids=[brief.project_id])
            if task is not None and task.id not in seen:
                seen[task.id] = task
    return list(seen.values())


def link(
    ref: str,
    task_ref: str,
    requirement_ids: Sequence[str],
    *,
    session_ref: str | None = None,
    cwd: Path | None = None,
) -> tuple[WorkBrief, TeamTask, bool]:
    """Link a board task to requirements: one task can cover several, and one requirement
    can need several tasks. The brief, the task, and whether any link was new; linking
    again what is linked writes nothing."""
    if not requirement_ids:
        raise ValueError("name at least one requirement (-r R1)")
    team._require_enabled()
    team._DELIVERY.set(None)
    with store_session() as store:
        session = _session(store, session_ref)
        board = team._board(store, session, cwd, write=True)
        brief = _load(store, board.id, board.name, ref)
        task = _linked_task(store, board.id, task_ref)
        new = [
            requirement.id
            for requirement in (_requirement(brief, r) for r in requirement_ids)
            if task.id not in requirement.task_ids
        ]
        if not new:
            return brief, task, False
        for req_id in dict.fromkeys(new):
            _requirement(brief, req_id).task_ids.append(task.id)
        stored = _write_brief(
            store,
            brief,
            _Write(
                "brief_linked",
                f"linked {team.task_label(task)} to {', '.join(dict.fromkeys(new))}",
                task.id,
            ),
            session,
        )
    _publish(stored, board, session.id if session else None, cwd)
    return brief, task, True


def record_evidence(
    ref: str,
    requirement_id: str,
    *,
    task_ref: str,
    verdict: EvidenceVerdict,
    summary: str,
    artifact: Path | None = None,
    report_id: str | None = None,
    session_ref: str | None = None,
    cwd: Path | None = None,
) -> tuple[WorkBrief, BriefEvidence, bool]:
    """Record a check of one requirement by one linked task. The brief, the record, and
    whether it was new: the same check sent again, identical to the task's latest record
    of the requirement, writes nothing. A failure has :func:`finding`'s effect on the task.

    Backed by exactly one of an ``artifact`` (manual evidence, which only the validator
    can judge, with its checkout's whole-tree fingerprint) or a saved command report
    (``report_id``). A report must have been captured with ``--project`` for this board,
    and one captured with ``--task`` backs only that task. A pass from a report needs
    exit 0, no interruption, both streams kept whole, no launch error, a known source
    identity, and the same identity before the run, after it and now, in the report's
    own checkout (:func:`_pass_refusals`).
    """
    if (artifact is None) == (report_id is None):
        raise ValueError("back the check with exactly one of --artifact or --report")
    return _record(
        ref,
        requirement_id,
        task_ref=task_ref,
        verdict=verdict,
        summary=summary,
        artifact=artifact,
        report_id=report_id,
        finding=False,
        session_ref=session_ref,
        cwd=cwd,
    )


def finding(
    ref: str,
    requirement_id: str,
    *,
    task_ref: str,
    summary: str,
    artifact: Path | None = None,
    session_ref: str | None = None,
    cwd: Path | None = None,
) -> tuple[WorkBrief, BriefEvidence, bool]:
    """Record a failure someone found in a task's work on a requirement, linking the task
    to it when it is not linked yet.

    A task in review or done goes back to todo, unowned. A task someone is working on
    keeps its owner and its status: the finding reaches them through the brief. A dropped
    task stays dropped. The third distinct failure by one task against one revision of
    the requirement blocks the task, keeping its owner, so the manager re-plans instead
    of the task looping; a correction to the requirement starts the count again.
    """
    return _record(
        ref,
        requirement_id,
        task_ref=task_ref,
        verdict="fail",
        summary=summary,
        artifact=artifact,
        report_id=None,
        finding=True,
        session_ref=session_ref,
        cwd=cwd,
    )


def _record(
    ref: str,
    requirement_id: str,
    *,
    task_ref: str,
    verdict: EvidenceVerdict,
    summary: str,
    artifact: Path | None,
    report_id: str | None,
    finding: bool,
    session_ref: str | None,
    cwd: Path | None,
) -> tuple[WorkBrief, BriefEvidence, bool]:
    team._require_enabled()
    if not summary.strip():
        raise ValueError("say what the check found (--summary)")
    # Read before the store opens: a large artifact is hashed outside the transaction.
    read = _read_artifact(artifact) if artifact is not None else None
    digest = read.sha256 if read is not None else None
    report: command_reports.CommandReport | None = None
    saved: command_reports.SourceFiles | None = None
    report_sha256: str | None = None
    if report_id is not None:
        try:
            report = command_reports.load_report(report_id.strip())
            saved = command_reports.load_source_files(report.id)
            report_sha256 = _report_digest(report.id)
        except (OSError, ValueError) as exc:
            raise ValueError(f"no readable command report {report_id!r} ({exc})") from None
    team._DELIVERY.set(None)
    with store_session() as store:
        session = _session(store, session_ref)
        board = team._board(store, session, cwd, write=True)
        brief = _load(store, board.id, board.name, ref)
        requirement = _requirement(brief, requirement_id)
        task = _linked_task(store, board.id, task_ref)
        linking = task.id not in requirement.task_ids
        if linking and not finding:
            raise ValueError(
                f"{team.task_label(task)} is not linked to {requirement.id}: "
                f"`asq brief link {brief.label} {task.id} -r {requirement.id}` first"
            )
        source_root: str | None
        fingerprint: str | None
        if report is not None:
            if report.project_id != brief.project_id:
                raise ValueError(
                    f"report {report.id[:12]} was not captured for this board: run the check "
                    f"with `asq exec --project {brief.project_id} --task {task.id} -- …`"
                )
            if report.task_id is not None and report.task_id != task.id:
                raise ValueError(
                    f"report {report.id[:12]} was captured for task {report.task_id}: "
                    "it backs only that task"
                )
            if verdict == "pass":
                refusals = _pass_refusals(report, saved)
                if refusals:
                    raise ValueError(
                        f"report {report.id[:12]} cannot back a pass: " + "; ".join(refusals)
                    )
            source_root, fingerprint = report.source_root, report.source_fingerprint_before
        else:
            project = store.get_project(brief.project_id, include_forgotten=True)
            root = source_root_for(project.root, cwd) if project else None
            try:
                if root is None:
                    raise ValueError("the project's directory is not known")
                fingerprint = fingerprint_of(source_file_hashes(root))
                source_root = str(root)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                if verdict == "pass":
                    raise ValueError(
                        f"a pass needs the source identity of the checkout, which cannot be "
                        f"taken ({exc})"
                    ) from None
                source_root, fingerprint = (str(root) if root else None), None
        lane = [
            e for e in brief.evidence if e.requirement_id == requirement.id and e.task_id == task.id
        ]
        latest = lane[-1] if lane else None
        if (
            latest is not None
            and latest.requirement_revision == requirement.revision
            and latest.verdict == verdict
            and latest.summary == summary.strip()
            and latest.artifact_sha256 == digest
            and latest.report_id == (report.id if report is not None else None)
            and latest.source_fingerprint == fingerprint
            and latest.finding == finding
        ):
            return brief, latest, False
        evidence_id = new_evidence_id()
        copy = _keep_copy(brief, evidence_id, read) if read is not None else None
        evidence = BriefEvidence(
            id=evidence_id,
            requirement_id=requirement.id,
            requirement_revision=requirement.revision,
            task_id=task.id,
            verdict=verdict,
            kind="report" if report is not None else "manual",
            finding=finding,
            summary=summary.strip(),
            artifact=str(copy) if copy is not None else None,
            artifact_source=str(read.source) if read is not None else None,
            artifact_sha256=digest,
            report_id=report.id if report is not None else None,
            report_sha256=report_sha256,
            source_root=source_root,
            source_fingerprint=fingerprint,
            recorded_at=_now(),
            session_id=session.id if session else None,
        )
        if linking:
            requirement.task_ids.append(task.id)
        brief.evidence.append(evidence)
        brief = WorkBrief.model_validate(brief.model_dump())
        transitions: list[TaskTransition] = []
        if verdict == "fail" and task.status != "dropped":
            failures = {
                (e.summary, e.artifact_sha256, e.report_id)
                for e in brief.evidence
                if e.requirement_id == requirement.id
                and e.task_id == task.id
                and e.requirement_revision == requirement.revision
                and e.verdict == "fail"
            }
            label = f"{requirement.id} r{requirement.revision}"
            if len(failures) >= FAILURES_TO_BLOCK and task.status != "blocked":
                why = f"{len(failures)} distinct failures on {label}; re-plan it: {summary.strip()}"
                transitions.append((task.id, "block", _reopen_text(task, brief, why)))
            elif task.status in ("review", "done"):
                why = f"{label} failed: {summary.strip()}"
                transitions.append((task.id, "reopen", _reopen_text(task, brief, why)))
        kind = "brief_finding" if finding else "brief_evidence"
        text = (
            f"{requirement.id} {verdict} [{evidence.kind}] by {team.task_label(task)}: "
            f"{summary.strip()}"
        )
        try:
            stored = _write_brief(
                store,
                brief,
                _Write(kind, text if len(text) <= 500 else text[:497] + "…", task.id, transitions),
                session,
            )
        except BaseException:
            if copy is not None:  # nothing cites it: the write did not happen
                copy.unlink(missing_ok=True)
            raise
    _publish(stored, board, session.id if session else None, cwd)
    return brief, evidence, True
