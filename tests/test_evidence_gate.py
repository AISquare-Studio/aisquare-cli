"""The evidence gate (docs/work-briefs.md; docs/plans/evidence-gate.md, parts 2 and 3).

Report-backed evidence and its pass rules, the gate on ``task done``, ``brief check``,
the requirements block at session start, and report retention that never deletes a
report recorded evidence cites. The traps #136's review found that live here each have
a test: a pass is judged against the REPORT's own source root; an interrupted run that
exited 0 is no pass; a task is gated only by the briefs it is linked to and the refusal
carries its reason; the session-start block fails open on its own; and it is clipped,
with the whole session start tested against the 10,000-character hook cap.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core.store import BriefChangedError, store_session
from aisquare.models import TeamTask, WorkBrief
from aisquare.services import briefs, team
from aisquare.services import command_reports as reports
from aisquare.services import fleet as fleet_service
from tests.projects import add_project

CODER = "cccc3333-0000-0000-0000-000000000000"


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t.invalid", "-c", "user.name=t",
         "-c", "commit.gpgsign=false", "-C", str(root), *args],
        check=True, capture_output=True,
    )  # fmt: skip


@pytest.fixture(autouse=True)
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real git checkout, added as a board, with a module and its test committed."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "app.py").write_text("def answer():\n    return 42\n")
    (root / "test_app.py").write_text(
        "from app import answer\n\n\ndef test_answer():\n    assert answer() == 42\n"
    )
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "one")
    monkeypatch.chdir(root)
    add_project(root)
    return root


@pytest.fixture(autouse=True)
def no_nudge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fleet_service, "nudge_manager", lambda *a, **k: False)


def _project_id() -> str:
    return team.resolve_project().id


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def _run(argv: list[str], *, task: TeamTask | None = None, cwd: Path | None = None,
         project: str | None = "here", **kw: Any) -> reports.CommandReport:  # fmt: skip
    return reports.run_command(
        argv,
        cwd=cwd,
        task_id=task.id if task else None,
        project_id=_project_id() if project == "here" else project,
        **kw,
    )


def _linked(*requirements: str) -> tuple[WorkBrief, TeamTask]:
    brief = briefs.create("Answer", list(requirements or ("answer() is 42",)))
    task, _ = team.add_task("Build the answer", role="coder")
    briefs.link(brief.id, task.id, [r.id for r in brief.requirements])
    return brief, task


def _record(brief: WorkBrief, task: TeamTask, report: reports.CommandReport,
            req: str = "R1", verdict: Any = "pass") -> None:  # fmt: skip
    briefs.record_evidence(
        brief.id, req, task_ref=task.id, verdict=verdict, summary="ran the check",
        report_id=report.id,
    )  # fmt: skip


def _status(task: TeamTask) -> str:
    return team.show_task(task.id).status


def _check(brief: WorkBrief, **kw: Any) -> dict[str, str]:
    return {r.id: r.state for r in briefs.check(brief.id, **kw).requirements}


def _rewrite_report(report: reports.CommandReport, **fields: Any) -> None:
    """A saved report with some facts changed, as a run that ended that way saves it."""
    path = reports.reports_dir() / report.id / "report.json"
    path.write_text(report.model_copy(update=fields).model_dump_json())


# --- a report-backed pass and its rules --------------------------------------------------


def test_a_clean_report_backs_a_pass_and_is_labelled_report() -> None:
    brief, task = _linked()
    report = _run(_py("print('ok')"), task=task)
    _record(brief, task, report)
    recorded = briefs.show(brief.id).brief.evidence[-1]
    assert (recorded.kind, recorded.report_id) == ("report", report.id)
    assert recorded.source_root == report.source_root
    assert recorded.source_fingerprint == report.source_fingerprint_before
    assert _check(brief) == {"R1": "pass"}


@pytest.mark.parametrize(
    ("argv", "kw", "expected"),
    [
        (_py("raise SystemExit(3)"), {}, "it exited 3"),
        (["/nonexistent/check-binary"], {}, "it could not start"),
        (_py("print('x' * 5000)"), {"max_output_bytes": 1024}, "its output was not kept whole"),
    ],
    ids=["nonzero-exit", "launch-error", "truncated"],
)
def test_a_run_that_did_not_end_cleanly_cannot_back_a_pass(
    argv: list[str], kw: dict[str, Any], expected: str
) -> None:
    brief, task = _linked()
    report = _run(argv, task=task, **kw)
    with pytest.raises(ValueError, match=expected):
        _record(brief, task, report)
    _record(brief, task, report, verdict="fail")  # a failure it can back
    assert _check(brief) == {"R1": "fail"}


def test_an_interrupted_run_that_exited_zero_is_no_pass() -> None:
    """Trap: an interrupted run that still exited 0 was recorded as a pass."""
    brief, task = _linked()
    report = _run(_py("print('ok')"), task=task)
    assert report.exit_code == 0
    _rewrite_report(report, interrupted_by=2)
    with pytest.raises(ValueError, match=r"it was interrupted \(signal 2\)") as refused:
        _record(brief, task, report)
    assert "exited" not in str(refused.value), "exit 0 is not the reason"


def test_a_run_without_a_source_identity_cannot_back_a_pass() -> None:
    brief, task = _linked()
    report = _run(_py("print('ok')"), task=task, project=None)
    with pytest.raises(ValueError, match="not captured for this board"):
        _record(brief, task, report)
    report = _run(_py("print('ok')"), task=task)
    _rewrite_report(report, source_capture_error="git failed", source_fingerprint_after=None)
    with pytest.raises(ValueError, match=r"source identity is unknown \(git failed\)"):
        _record(brief, task, report)


def test_the_source_changing_during_or_after_the_run_refuses_a_pass(repo: Path) -> None:
    brief, task = _linked()
    during = _run(_py("open('app.py', 'a').write('# edited\\n')"), task=task)
    with pytest.raises(ValueError, match="the source changed while it ran"):
        _record(brief, task, during)
    _git(repo, "checkout", "--", "app.py")
    after = _run(_py("print('ok')"), task=task)
    (repo / "app.py").write_text("def answer():\n    return 41\n")
    with pytest.raises(ValueError, match="has changed since it ran; run it again"):
        _record(brief, task, after)


def test_a_new_file_the_check_wrote_does_not_stop_its_pass(repo: Path) -> None:
    brief, task = _linked()
    report = _run(_py("open('junit.xml', 'w').write('<testsuite/>')"), task=task)
    _record(brief, task, report)
    assert (repo / "junit.xml").exists()
    assert _check(brief) == {"R1": "pass"}


def test_a_report_captured_for_a_task_backs_only_that_task() -> None:
    brief, task = _linked()
    other, _ = team.add_task("Another part", role="coder")
    briefs.link(brief.id, other.id, ["R1"])
    report = _run(_py("print('ok')"), task=task)
    with pytest.raises(ValueError, match=f"captured for task {task.id}: it backs only that"):
        _record(brief, other, report)
    _record(brief, task, report)  # its own task, fine
    untagged = _run(_py("print('ok')"))
    _record(brief, other, untagged)  # a report with no --task backs whoever records it
    assert _check(brief) == {"R1": "pass"}


def test_a_report_from_another_board_cannot_back_a_check(tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    elsewhere = add_project(other)
    brief, task = _linked()
    report = _run(_py("print('ok')"), cwd=other, project=elsewhere.id)
    with pytest.raises(ValueError, match="not captured for this board"):
        _record(brief, task, report)


def test_a_pass_is_judged_against_the_reports_own_source_root(repo: Path) -> None:
    """Trap: a pass was judged against the recorder's checkout, not the report's. A check
    run in a worktree and recorded from the principal checkout is about the worktree."""
    worktree = repo.parent / "wt-answer"
    _git(repo, "worktree", "add", "-q", str(worktree), "-b", "answer")
    brief, task = _linked()
    report = _run(_py("print('ok')"), task=task, cwd=worktree)
    assert report.source_root is not None and Path(report.source_root).samefile(worktree)
    _record(brief, task, report)  # recorded from the principal checkout (the cwd)

    (repo / "app.py").write_text("def answer():\n    return 0\n")  # the principal moves on
    assert _check(brief) == {"R1": "pass"}, "the principal's edit is not the worktree's"
    (worktree / "app.py").write_text("def answer():\n    return 0\n")
    assert _check(brief) == {"R1": "stale"}


def test_a_report_changed_or_gone_since_it_was_recorded_is_stale() -> None:
    brief, task = _linked()
    report = _run(_py("print('ok')"), task=task)
    _record(brief, task, report)
    _rewrite_report(report, argv=["something", "else"])
    assert _check(brief) == {"R1": "stale"}
    import shutil

    shutil.rmtree(reports.reports_dir() / report.id)
    assert _check(brief) == {"R1": "stale"}


# --- manual evidence keeps the whole-tree fingerprint --------------------------------------


def test_a_manual_pass_goes_stale_on_any_change_in_its_checkout(repo: Path, tmp_path: Path) -> None:
    brief, task = _linked()
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"screenshot")
    briefs.record_evidence(
        brief.id, "R1", task_ref=task.id, verdict="pass", summary="looked", artifact=shot
    )
    recorded = briefs.show(brief.id).brief.evidence[-1]
    assert recorded.source_root is not None and Path(recorded.source_root).samefile(repo)
    assert recorded.source_fingerprint
    assert _check(brief) == {"R1": "pass"}
    (repo / "notes.txt").write_text("a new untracked file")
    assert _check(brief) == {"R1": "stale"}


def test_a_manual_pass_needs_a_source_identity_but_a_failure_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    brief, task = _linked()
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"screenshot")

    def unreadable(root: Path) -> dict[str, str]:
        raise ValueError("cannot read source file x")

    monkeypatch.setattr(briefs, "source_file_hashes", unreadable)
    with pytest.raises(ValueError, match="a pass needs the source identity"):
        briefs.record_evidence(
            brief.id, "R1", task_ref=task.id, verdict="pass", summary="x", artifact=shot
        )
    briefs.record_evidence(
        brief.id, "R1", task_ref=task.id, verdict="fail", summary="broken", artifact=shot
    )
    assert briefs.show(brief.id).brief.evidence[-1].source_fingerprint is None


# --- the gate on task done ---------------------------------------------------------------


def test_an_unlinked_task_is_done_exactly_as_before(runner: CliRunner) -> None:
    briefs.create("Unrelated", ["Something else"])
    task, _ = team.add_task("Not in any brief", role="coder")
    result = runner.invoke(app, ["task", "done", task.id])
    assert result.exit_code == 0, result.output
    assert _status(task) == "done"


def test_task_done_names_each_requirement_and_its_state(runner: CliRunner) -> None:
    brief, task = _linked("R one", "R two", "R three")
    _record(brief, task, _run(_py("print('ok')"), task=task), req="R1")
    _record(brief, task, _run(_py("raise SystemExit(1)"), task=task), req="R2", verdict="fail")
    team.claim_task(task.id)

    text = runner.invoke(app, ["task", "done", task.id])
    as_json = runner.invoke(app, ["--json", "task", "done", task.id])

    assert text.exit_code == 1 and as_json.exit_code == 1
    assert "B1 R2 [failed]" in text.output and "B1 R3 [missing]" in text.output
    assert "R1" not in text.output.split("cannot be done")[1].split("Record one")[0]
    payload = json.loads(as_json.stdout)
    assert payload["error"] == "task_not_proven" and payload["ref"] == task.id
    assert [(r["requirement"], r["state"]) for r in payload["requirements"]] == [
        ("R2", "failed"), ("R3", "missing")
    ]  # fmt: skip
    assert all(r["reason"] for r in payload["requirements"])
    assert _status(task) == "doing", "a refused task stays as it was"


def test_a_stale_or_blocked_requirement_refuses_done(repo: Path) -> None:
    brief, task = _linked()
    _record(brief, task, _run(_py("print('ok')"), task=task))
    (repo / "app.py").write_text("def answer():\n    return 41\n")
    with pytest.raises(briefs.TaskNotProven) as stale:
        team.finish_task(task.id)
    assert [(i.requirement, i.state) for i in stale.value.items] == [("R1", "stale")]
    assert "app.py" not in stale.value.items[0].reason or "changed" in stale.value.items[0].reason

    _git(repo, "checkout", "--", "app.py")
    assert team.finish_task(task.id).status == "done", "the source is back: proven again"
    team.reopen_task(task.id, reason="again")
    team.block_task(task.id, reason="waiting")
    with pytest.raises(briefs.TaskNotProven) as blocked:
        team.finish_task(task.id)
    assert [i.state for i in blocked.value.items] == ["blocked"]


def test_a_correction_between_the_check_and_the_write_fails_the_completion(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    brief, task = _linked()
    _record(brief, task, _run(_py("print('ok')"), task=task))
    real_gate = briefs.gate

    corrections = iter(range(100))

    def gate_then_correct(store: Any, gated: TeamTask) -> dict[str, int]:
        read = real_gate(store, gated)  # passes: the evidence is fresh
        briefs.update(brief.id, checks={"R1": f"pytest -q #{next(corrections)}"})  # lands now
        return read

    monkeypatch.setattr(briefs, "gate", gate_then_correct)
    with pytest.raises(BriefChangedError):
        team.finish_task(task.id)
    assert _status(task) == "todo"
    _record(brief, task, _run(_py("print('ok')"), task=task))  # fresh again for the CLI
    result = runner.invoke(app, ["--json", "task", "done", task.id])
    assert result.exit_code == 1 and json.loads(result.stdout)["error"] == "brief_changed"
    assert _status(task) == "todo"


def test_a_task_is_gated_only_by_the_briefs_it_is_linked_to() -> None:
    """Trap: one damaged brief anywhere on the board blocked every `task done`, and the
    refusal said only `unverified_requirements`."""
    brief, task = _linked()
    _record(brief, task, _run(_py("print('ok')"), task=task))
    unproven = briefs.create("Someone else's", ["Unproven"])
    other, _ = team.add_task("Theirs", role="coder")
    briefs.link(unproven.id, other.id, ["R1"])
    damaged = briefs.create("Damaged", ["Whatever"])
    with store_session() as store:
        store._conn.execute("UPDATE brief SET data = '{oops' WHERE id = ?", (damaged.id,))  # type: ignore[attr-defined]
        store._conn.commit()  # type: ignore[attr-defined]

    assert team.finish_task(task.id).status == "done"


def test_a_damaged_brief_that_names_the_task_refuses_with_its_reason() -> None:
    brief, task = _linked()
    with store_session() as store:
        store._conn.execute(  # type: ignore[attr-defined]
            "UPDATE brief SET data = ? WHERE id = ?",
            (json.dumps({"id": brief.id, "requirements": [{"task_ids": [task.id]}]}), brief.id),
        )
        store._conn.commit()  # type: ignore[attr-defined]
    with pytest.raises(briefs.TaskNotProven) as refused:
        team.finish_task(task.id)
    [item] = refused.value.items
    assert item.state == "unreadable" and "cannot be read" in item.reason


def test_each_linked_task_proves_its_own_part() -> None:
    brief, task = _linked()
    helper, _ = team.add_task("Helps", role="coder")
    briefs.link(brief.id, helper.id, ["R1"])
    _record(brief, task, _run(_py("print('ok')"), task=task))
    assert team.finish_task(task.id).status == "done", "the helper's part is the helper's"
    with pytest.raises(briefs.TaskNotProven):
        team.finish_task(helper.id)


# --- brief check --------------------------------------------------------------------------


def test_brief_check_exits_1_until_everything_is_proven(runner: CliRunner) -> None:
    brief, task = _linked("R one", "R two")
    missing = runner.invoke(app, ["brief", "check", "B1"])
    assert missing.exit_code == 1 and "NOT PROVEN" in missing.stdout
    _record(brief, task, _run(_py("print('ok')"), task=task), req="R1")
    _record(brief, task, _run(_py("print('ok')"), task=task), req="R2")
    proven = runner.invoke(app, ["--json", "brief", "check", "B1"])
    assert proven.exit_code == 0, proven.output
    assert json.loads(proven.stdout)["complete"] is True


def test_brief_check_against_one_assembled_checkout(repo: Path, runner: CliRunner) -> None:
    worktree = repo.parent / "wt-assembled"
    _git(repo, "worktree", "add", "-q", str(worktree), "-b", "assembled")
    brief, task = _linked()
    _record(brief, task, _run(_py("print('ok')"), task=task))
    assert _check(brief, source_root=worktree) == {"R1": "pass"}, "same content, same proof"
    (worktree / "app.py").write_text("def answer():\n    return 7\n")
    assert _check(brief) == {"R1": "pass"}, "by default, its own checkout"
    assert _check(brief, source_root=worktree) == {"R1": "stale"}
    result = runner.invoke(app, ["brief", "check", "B1", "--source-root", str(worktree)])
    assert result.exit_code == 1 and "NOT PROVEN against" in result.stdout
    checked = briefs.check(brief.id, source_root=worktree).source_root
    assert checked is not None and Path(checked).samefile(worktree)


def test_a_source_root_outside_the_projects_checkouts_is_an_error(
    tmp_path: Path, repo: Path, runner: CliRunner
) -> None:
    _linked()
    stranger = tmp_path / "stranger"
    stranger.mkdir()
    for path, why in (
        (stranger, "not a checkout of this brief's project"),
        (repo / "sub", "not a directory"),
    ):
        result = runner.invoke(app, ["brief", "check", "B1", "--source-root", str(path)])
        assert result.exit_code == 1 and why in result.output, result.output
        assert "stale" not in result.output
    (repo / "sub").mkdir()
    inside = runner.invoke(app, ["brief", "check", "B1", "--source-root", str(repo / "sub")])
    assert "not a checkout" in inside.output, "a sub-directory is not a checkout either"


# --- the session start --------------------------------------------------------------------


def _start(monkeypatch: pytest.MonkeyPatch, repo: Path) -> str:
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    try:
        return team.hook_session_start(CODER, repo, "startup")
    finally:
        monkeypatch.delenv("AISQUARE_ROLE")


def test_the_session_start_shows_the_sessions_linked_requirements(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    brief, task = _linked("R one", "R two")
    briefs.update(brief.id, checks={"R1": "pytest -q test_app.py"})
    _start(monkeypatch, repo)
    team.claim_task(task.id, session_ref=CODER)
    _record(brief, task, _run(_py("print('ok')"), task=task), req="R1")

    started = _start(monkeypatch, repo)

    block = started.split("<aisquare-requirements>")[1].split("</aisquare-requirements>")[0]
    assert f"#{task.number}" in block and "B1" in block
    assert "R1 [pass, as recorded] R one — check: pytest -q test_app.py" in block
    assert "R2 [missing] R two" in block
    assert "<aisquare-team>" in started, "the board is still there"


def test_a_session_with_no_linked_task_gets_no_block(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    _linked()
    assert "<aisquare-requirements>" not in _start(monkeypatch, repo)


def test_the_block_fails_open_on_its_own(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    """Trap: an exception in the new session-start block discarded the whole output."""
    _, task = _linked()
    _start(monkeypatch, repo)
    team.claim_task(task.id, session_ref=CODER)

    def broken(*args: Any) -> str:
        raise RuntimeError("the brief store is damaged")

    monkeypatch.setattr(briefs, "session_requirements", broken)
    started = _start(monkeypatch, repo)
    assert "<aisquare-team>" in started and "<aisquare-requirements>" not in started


def test_the_block_is_clipped_and_the_session_start_fits_the_hook_cap(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    """Trap: session-start text had no overall budget, so long checks or task detail went
    over Claude Code's 10,000-character hook cap. A brief at its limits: 64 requirements
    of 2,000 characters, each with a 2,000-character check, all linked to this task."""
    texts = [f"Requirement {n} " + "x" * 1_980 for n in range(64)]
    brief = briefs.create("Everything " + "t" * 400, texts)
    briefs.update(brief.id, checks={f"R{n}": "c" * 2_000 for n in range(1, 65)})
    task, _ = team.add_task("All of it", detail="d" * 4_000, role="coder")
    briefs.link(brief.id, task.id, [f"R{n}" for n in range(1, 65)])
    _start(monkeypatch, repo)
    team.claim_task(task.id, session_ref=CODER)

    started = _start(monkeypatch, repo)

    block = started[started.index("<aisquare-requirements>") :]
    assert "More are linked" in block
    assert len(block) <= briefs.REQUIREMENTS_BUDGET + 600, len(block)
    assert len(started) <= 10_000, len(started)


# --- report retention ----------------------------------------------------------------------


def _age(report: reports.CommandReport, days: int) -> None:
    old = datetime.now(UTC) - timedelta(days=days)
    _rewrite_report(report, finished_at=old, started_at=old)


def test_a_report_recorded_evidence_cites_is_never_pruned() -> None:
    brief, task = _linked()
    cited = _run(_py("print('ok')"), task=task)
    _record(brief, task, cited)
    loose = _run(_py("print('loose')"))
    for report in (cited, loose):
        _age(report, days=30)
    assert reports.protected_report_ids() == {cited.id}
    removed = reports.prune_reports(keep=0, days=0, protect=reports.protected_report_ids())
    assert loose.id in removed and cited.id not in removed
    assert reports.load_report(cited.id).id == cited.id


def test_a_damaged_brief_still_protects_its_reports() -> None:
    brief, task = _linked()
    cited = _run(_py("print('ok')"), task=task)
    _record(brief, task, cited)
    with store_session() as store:
        store._conn.execute(  # type: ignore[attr-defined]
            "UPDATE brief SET data = substr(data, 1, length(data) - 20) WHERE id = ?",
            (brief.id,),
        )
        store._conn.commit()  # type: ignore[attr-defined]
    assert reports.protected_report_ids() == {cited.id}


def test_an_unreadable_brief_store_means_unknown_and_nothing_is_pruned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = _run(_py("print('old')"), project=None)
    _age(old, days=30)
    from aisquare.core import store as store_module

    def locked(self: Any) -> list[str]:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store_module.SqliteStore, "all_brief_documents", locked)
    assert reports.protected_report_ids() is None
    assert reports.prune_reports(keep=0, days=0, protect=reports.protected_report_ids()) == []
    assert reports.load_report(old.id).id == old.id


# --- end to end ------------------------------------------------------------------------------


def test_end_to_end_with_real_git_and_a_real_pytest_run(repo: Path, runner: CliRunner) -> None:
    """Create a brief, link a task, run the check through `asq exec --project`, record it
    with --report, edit a source file: `task done` is refused as stale. Run and record
    again: `task done` succeeds, and `brief check` exits 0."""

    def asq(*args: str) -> Any:
        result = runner.invoke(app, list(args))
        return result

    def exec_check(task_id: str) -> str:
        result = asq(
            "--json", "exec", "--project", _project_id(), "--task", task_id, "--",
            sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_app.py",
        )  # fmt: skip
        assert result.exit_code == 0, result.output
        report_id = json.loads(result.stdout)["report"]["id"]
        assert isinstance(report_id, str)
        return report_id

    created = asq("--json", "brief", "create", "The answer", "-r", "answer() returns 42",
                  "--check", "R1=pytest -q test_app.py")  # fmt: skip
    assert created.exit_code == 0, created.output
    task, _ = team.add_task("Return the answer", role="coder")
    assert asq("brief", "link", "B1", f"#{task.number}", "-r", "R1").exit_code == 0

    first = exec_check(task.id)
    recorded = asq("brief", "evidence", "B1", "R1", "--task", task.id, "--verdict", "pass",
                   "--summary", "pytest passed", "--report", first)  # fmt: skip
    assert recorded.exit_code == 0, recorded.output
    assert "[report]" in recorded.stdout

    (repo / "app.py").write_text("def answer():\n    return 6 * 7\n")
    refused = asq("--json", "task", "done", task.id)
    assert refused.exit_code == 1
    assert [r["state"] for r in json.loads(refused.stdout)["requirements"]] == ["stale"]
    assert _status(task) != "done"

    second = exec_check(task.id)
    assert second != first
    again = asq("brief", "evidence", "B1", "R1", "--task", task.id, "--verdict", "pass",
                "--summary", "pytest passed on the edit", "--report", second)  # fmt: skip
    assert again.exit_code == 0, again.output
    done = asq("task", "done", task.id)
    assert done.exit_code == 0, done.output
    assert _status(task) == "done"
    check = asq("brief", "check", "B1")
    assert check.exit_code == 0, check.output
    assert "PROVEN" in check.stdout
