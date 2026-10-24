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

#: The real block builder, for a test that stubs it and puts it back.
_REAL_SESSION_REQUIREMENTS = briefs.session_requirements


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
    with pytest.raises(ValueError, match=r"has changed since it ran: app.py\. If that is"):
        _record(brief, task, after)


def test_a_new_file_the_check_wrote_does_not_stop_its_pass(repo: Path) -> None:
    brief, task = _linked()
    report = _run(_py("open('junit.xml', 'w').write('<testsuite/>')"), task=task)
    _record(brief, task, report)
    assert (repo / "junit.xml").exists()
    assert _check(brief) == {"R1": "pass"}


def test_a_file_added_since_the_check_stales_its_pass(repo: Path) -> None:
    """Validator round on #136, M2 (manager's decision): a pass ignores only the paths its
    check created. A conftest.py added after a PROVEN brief changes what the check means:
    the pass goes stale, though no file the check saw changed."""
    brief, task = _linked()
    _record(brief, task, _run(_py("print('ok')"), task=task))
    assert _check(brief) == {"R1": "pass"}
    (repo / "conftest.py").write_text("raise RuntimeError('collection fails now')\n")
    result = briefs.check(brief.id).requirements[0]
    assert result.state == "stale" and "conftest.py" in result.reason
    with pytest.raises(briefs.TaskNotProven):
        team.finish_task(task.id)


def test_the_checks_own_output_never_stales_its_pass(repo: Path) -> None:
    """A file the check itself wrote (a --junitxml report) is its output, and stays so
    when a later `asq exec` run rewrites it: untracked, and holding what a saved run
    wrote there."""
    brief, task = _linked()
    report = _run(_py("open('junit.xml', 'w').write('<testsuite/>')"), task=task)
    _record(brief, task, report)
    _run(_py("open('junit.xml', 'w').write('<testsuite tests=\\'2\\'/>')"))  # the next run
    assert _check(brief) == {"R1": "pass"}


def test_a_check_output_rewritten_outside_exec_reads_stale_with_the_remedies(
    repo: Path,
) -> None:
    """Manager's decision on #12, final: output is only what a saved run wrote. The same
    junit file rewritten by hand (or by a plain pytest outside asq exec) is not, and the
    reason says what to do about it."""
    brief, task = _linked()
    _record(brief, task, _run(_py("open('junit.xml', 'w').write('<testsuite/>')"), task=task))
    (repo / "junit.xml").write_text("<testsuite tests='2'/>")
    status = briefs.check(brief.id).requirements[0]
    assert status.state == "stale" and "junit.xml" in status.reason
    for remedy in ("delete it", "gitignore it", "write it outside the checkout"):
        assert remedy in status.reason, status.reason


def _pytest_into(report_file: str) -> list[str]:
    """A real pytest check that writes its junit report into the checkout."""
    return [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
            f"--junitxml={report_file}", "test_app.py"]  # fmt: skip


def test_two_checks_writing_reports_into_the_checkout_can_both_be_done(repo: Path) -> None:
    """The tester's finding on #12: R1's check writes a.xml and R2's writes b.xml. At
    dac09e77 recording R2 staled R1 (b.xml is new and R1's run did not create it), and
    re-running R1 was refused "changed while it ran (a.xml)" forever. A path some saved
    run created is check output, for every check in that checkout."""
    brief, task = _linked("R one", "R two")
    _record(brief, task, _run(_pytest_into("a.xml"), task=task), req="R1")
    _record(brief, task, _run(_pytest_into("b.xml"), task=task), req="R2")
    assert (repo / "a.xml").exists() and (repo / "b.xml").exists()
    assert _check(brief) == {"R1": "pass", "R2": "pass"}

    again = _run(_pytest_into("a.xml"), task=task)  # a.xml is there now, and rewritten
    _record(brief, task, again, req="R1")
    assert _check(brief) == {"R1": "pass", "R2": "pass"}
    assert team.finish_task(task.id).status == "done"

    (repo / "conftest.py").write_text("raise RuntimeError('collection fails now')\n")
    assert _check(brief) == {"R1": "stale", "R2": "stale"}, "an added conftest.py still counts"


def _broken_after(repo: Path, made_by_a_run: str, *, then: Any) -> dict[str, str]:
    """A path a saved run made, then ``then`` (commit it, say), a pass recorded after,
    and the path broken by hand: the brief's standing."""
    brief, task = _linked()
    then()
    _record(brief, task, _run(_py("print('the check')"), task=task))
    assert _check(brief) == {"R1": "pass"}
    (repo / made_by_a_run).write_text("raise RuntimeError('broken')\n")
    return _check(brief)


def test_a_scaffold_a_run_wrote_and_that_was_committed_is_source(repo: Path) -> None:
    """Tester's H1 on #12: a scaffold written through `asq exec`, then committed and
    broken, read PROVEN at e7490c0c. Tracked now, it is source."""
    _run(_py("open('helper.py', 'w').write('def help():\\n    return 1\\n')"))

    def commit() -> None:
        _git(repo, "add", "helper.py")
        _git(repo, "commit", "-q", "-m", "the scaffold")

    assert _broken_after(repo, "helper.py", then=commit) == {"R1": "stale"}


def test_a_tracked_file_put_back_to_what_a_run_once_wrote_is_still_a_change(
    repo: Path,
) -> None:
    """Why "untracked now" is half the rule: a scaffold a run wrote is committed and then
    edited, and a pass is recorded on the edit. Putting the file back to the scaffold, the
    exact bytes a saved run wrote, changes the source; tracked, it is no check's output."""
    scaffold = "def help():\n    return 1\n"
    _run(_py(f"open('helper.py', 'w').write({scaffold!r})"))
    _git(repo, "add", "helper.py")
    _git(repo, "commit", "-q", "-m", "the scaffold")
    (repo / "helper.py").write_text("def help():\n    return 2\n")
    _git(repo, "commit", "-q", "-am", "the edit")
    brief, task = _linked()
    _record(brief, task, _run(_py("print('the check')"), task=task))
    (repo / "helper.py").write_text(scaffold)
    assert _check(brief) == {"R1": "stale"}


def test_a_tracked_file_a_run_brought_in_is_source(repo: Path) -> None:
    """Tester's H2 on #12: `asq exec -- git merge feature` brought tracked feature.py,
    which then read PROVEN when broken. Tracked now, it is source."""
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "feature.py").write_text("X = 1\n")
    _git(repo, "add", "feature.py")
    _git(repo, "commit", "-q", "-m", "feature")
    _git(repo, "checkout", "-q", "-")
    assert not (repo / "feature.py").exists()
    _run(["git", "-c", "user.email=t@t.invalid", "-c", "user.name=t", "merge", "-q",
          "--no-edit", "feature"])  # fmt: skip
    assert (repo / "feature.py").exists()
    assert _broken_after(repo, "feature.py", then=lambda: None) == {"R1": "stale"}


def test_a_file_a_run_created_and_someone_then_edited_is_source(repo: Path) -> None:
    """Tester's H3 on #12: `asq exec -- touch conftest.py`, then a raising conftest.py,
    read PROVEN. Its content is no longer what a run wrote: it is source."""
    _run(_py("open('conftest.py', 'w').close()"))
    assert _broken_after(repo, "conftest.py", then=lambda: None) == {"R1": "stale"}


def test_a_read_only_run_does_not_launder_an_added_conftest(repo: Path) -> None:
    """Tester's H4 on #12: a raising conftest.py added by hand, then `asq exec --project
    P -- git status --short`, read PASS and `task done` succeeded at d7c9a974, because the
    run recorded every untracked file it found. A run's digest counts only where it
    changed the path."""
    brief, task = _linked()
    _record(brief, task, _run(_py("print('the check')"), task=task))
    (repo / "conftest.py").write_text("raise RuntimeError('collection fails now')\n")
    assert _check(brief) == {"R1": "stale"}
    _run(["git", "status", "--short"])
    assert _check(brief) == {"R1": "stale"}
    with pytest.raises(briefs.TaskNotProven):
        team.finish_task(task.id)


def test_a_read_only_run_does_not_launder_an_edited_untracked_file(repo: Path) -> None:
    """Tester's H4b on #12: an untracked helper.py the check relies on, edited by hand,
    then a listing run (`ls`), read PASS. It still reads stale."""
    (repo / "helper.py").write_text("VALUE = 1\n")
    brief, task = _linked()
    _record(brief, task, _run(_py("print('the check')"), task=task))
    (repo / "helper.py").write_text("VALUE = 2\n")
    assert _check(brief) == {"R1": "stale"}
    _run(_py("import os; print(sorted(os.listdir('.')))"))
    assert _check(brief) == {"R1": "stale"}


def test_a_source_file_the_check_changes_is_named_with_the_three_remedies(repo: Path) -> None:
    """Where a path that existed still changes during a run, the refusal names it and says
    what to do: delete it, gitignore it, or write check output outside the checkout."""
    (repo / "snapshot.txt").write_text("one\n")
    _git(repo, "add", "snapshot.txt")
    _git(repo, "commit", "-q", "-m", "a committed file the check rewrites")
    brief, task = _linked()
    report = _run(_py("open('snapshot.txt', 'w').write('two\\n')"), task=task)
    with pytest.raises(ValueError) as refused:
        _record(brief, task, report)
    said = str(refused.value)
    assert "the source changed while it ran: snapshot.txt" in said
    for remedy in ("delete it", "gitignore it", "write it outside the checkout"):
        assert said.count(remedy) == 1, said  # said once, when both reasons fire (n11)
    assert "run it again" not in said


def test_a_stale_pass_on_an_assembled_checkout_names_what_changed(repo: Path) -> None:
    """Validator round on #136, M5: on an assembled checkout every pass read "stale" and
    named no file. The reason names the first changed paths, up to five."""
    worktree = repo.parent / "wt-assembled-m5"
    _git(repo, "worktree", "add", "-q", str(worktree), "-b", "assembled-m5")
    brief, task = _linked()
    _record(brief, task, _run(_py("print('ok')"), task=task))
    (worktree / "app.py").write_text("def answer():\n    return 7\n")
    for n in range(6):
        (worktree / f"extra_{n}.py").write_text("x = 1\n")
    status = briefs.check(brief.id, source_root=worktree).requirements[0]
    assert status.state == "stale"
    named = status.reason.split("changed since the check ran: ")[1].split(". If that is")[0]
    assert named == "app.py, extra_0.py, extra_1.py, extra_2.py, extra_3.py and 2 more"


def test_a_report_captured_for_a_task_backs_only_that_task() -> None:
    brief, task = _linked()
    other, _ = team.add_task("Another part", role="coder")
    briefs.link(brief.id, other.id, ["R1"])
    report = _run(_py("print('ok')"), task=task)
    with pytest.raises(ValueError) as refused:
        _record(brief, other, report)
    said = str(refused.value)
    assert f"captured for task {task.id}, not #{other.number} {other.id}" in said
    assert f"`asq exec --project {brief.project_id} --task {other.id} -- …`" in said
    _record(brief, task, report)  # its own task, fine
    untagged = _run(_py("print('ok')"))
    _record(brief, other, untagged)  # a report with no --task backs whoever records it
    assert _check(brief) == {"R1": "pass"}


def _exec(runner: CliRunner, *args: str) -> Any:
    return runner.invoke(app, ["--json", "exec", "--project", _project_id(), *args])


def test_exec_task_takes_a_board_ref_as_every_other_task_does(runner: CliRunner) -> None:
    """Validator round on #136, M1: `asq exec --task 1` stored "1", and evidence from task
    #1 was refused as "captured for task 1". The natural flow now works."""
    brief, task = _linked()
    ran = _exec(runner, "--task", str(task.number), "--", *_py("print('ok')"))
    assert ran.exit_code == 0, ran.output
    report = json.loads(ran.stdout)["report"]
    assert report["task_id"] == task.id, "the ref is recorded as the task's id"
    recorded = runner.invoke(
        app,
        ["brief", "evidence", "B1", "R1", "--task", str(task.number), "--verdict", "pass",
         "--summary", "ran", "--report", report["id"]],
    )  # fmt: skip
    assert recorded.exit_code == 0, recorded.output
    assert _check(brief) == {"R1": "pass"}


def test_exec_refuses_a_task_ref_the_board_does_not_have_and_runs_nothing(
    runner: CliRunner, tmp_path: Path
) -> None:
    _linked()
    marker = tmp_path / "ran"
    before = {r.id for r in reports.list_reports()}
    result = _exec(runner, "--task", "#99", "--", *_py(f"open({str(marker)!r}, 'w')"))
    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["error"] == "unknown_task"
    assert not marker.exists(), "nothing was run"
    assert {r.id for r in reports.list_reports()} == before, "nothing was saved"


def test_a_report_that_stored_the_ref_as_typed_still_backs_its_task() -> None:
    """A report saved by a build that kept `--task 1` as typed names task #1 all the same."""
    brief, task = _linked()
    report = _run(_py("print('ok')"))
    _rewrite_report(report, task_id=str(task.number))
    _record(brief, task, report)
    assert _check(brief) == {"R1": "pass"}


def test_the_hints_name_the_real_project_and_task(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    brief, task = _linked()
    _start(monkeypatch, repo)
    team.claim_task(task.id, session_ref=CODER)
    started = _start(monkeypatch, repo)
    assert f"asq exec --project {brief.project_id} --task {task.id} -- CMD" in started
    assert f"asq brief evidence B1 R1 --task {task.id}" in started
    assert "--task TASK" not in started


def test_an_unknown_option_right_after_exec_is_refused_not_run(runner: CliRunner) -> None:
    """Validator round on #136, n2: `asq exec --json -- true` saved argv ['--json', 'true']
    and a 127. An option exec does not know is a usage error; nothing runs."""
    before = {r.id for r in reports.list_reports()}
    result = runner.invoke(app, ["exec", "--json", "--", *_py("print('x')")])
    assert result.exit_code == 2, result.output
    assert "put global flags such as --json before exec" in result.output
    assert {r.id for r in reports.list_reports()} == before


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_an_interrupt_during_the_drain_is_recorded_and_the_output_still_comes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validator round on #136, m4: the doc said such an interrupt exits 130 with no
    output; the code records it in interrupted_by (no pass) and the run returns its
    report. The doc now says what the code does, and this pins the code."""
    import os
    import signal as signals

    real_capture = reports._capture

    def capture_then_interrupt(*args: Any, **kwargs: Any) -> Any:
        captured = real_capture(*args, **kwargs)  # the command has exited; still draining
        os.kill(os.getpid(), signals.SIGINT)
        return captured

    monkeypatch.setattr(reports, "_capture", capture_then_interrupt)
    report = reports.run_command(_py("print('drained')"))
    assert report.interrupted_by == signals.SIGINT
    assert report.exit_code == 0
    assert reports.read_stream(report.id, "stdout", raw=True).strip() == b"drained"


def test_manual_is_said_only_of_a_pass(runner: CliRunner, tmp_path: Path) -> None:
    """Validator round on #136, n3: a requirement one task passed manually and another
    had not checked read "[missing (manual)]"."""
    brief, task = _linked()
    other, _ = team.add_task("Another part", role="coder")
    briefs.link(brief.id, other.id, ["R1"])
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"x")
    briefs.record_evidence(
        brief.id, "R1", task_ref=task.id, verdict="pass", summary="looked", artifact=shot
    )
    shown = runner.invoke(app, ["brief", "show", "B1"]).stdout
    checked = runner.invoke(app, ["brief", "check", "B1"]).stdout
    assert "R1 r1 [missing] " in shown and "(manual)" not in shown
    assert "R1 [missing]" in checked and "(manual)" not in checked


def test_the_record_hint_names_the_first_requirement_not_proven(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    """Re-gate, m7: the hint always said R1. A task linked to R2 only was told
    `brief evidence B1 R1`, which, copied as given, asked to link the wrong one."""
    brief = briefs.create("Answer", ["R one", "R two"])
    task, _ = team.add_task("The second part", role="coder")
    briefs.link(brief.id, task.id, ["R2"])
    refused = runner.invoke(app, ["task", "done", task.id])
    assert refused.exit_code == 1
    assert f"asq brief evidence B1 R2 --task {task.id}" in refused.output
    assert "B1 R1 " not in refused.output
    _start(monkeypatch, repo)
    team.claim_task(task.id, session_ref=CODER)
    started = _start(monkeypatch, repo)
    assert f"asq brief evidence B1 R2 --task {task.id}" in started


def test_a_finding_without_an_artifact_is_not_called_manual_in_show(
    runner: CliRunner,
) -> None:
    """Re-gate, n7: brief show's per-task line still said "fail [manual]" for a finding
    no artifact shows; nothing backs it, so nothing is manual about it."""
    _, task = _linked()
    briefs.finding("B1", "R1", task_ref=task.id, summary="broken on empty input")
    shown = runner.invoke(app, ["brief", "show", "B1"]).stdout
    line = next(row for row in shown.splitlines() if "broken on empty input" in row and "(" in row)
    assert "fail: broken on empty input" in line and "[manual]" not in line, line


def test_brief_check_lists_every_linked_task_under_a_requirement(
    runner: CliRunner,
) -> None:
    """Re-gate, n3: under a [missing] requirement, brief check listed only the task that
    passed, never the one still missing."""
    brief, task = _linked()
    other, _ = team.add_task("Another part", role="coder")
    briefs.link(brief.id, other.id, ["R1"])
    _record(brief, task, _run(_py("print('ok')"), task=task))
    checked = runner.invoke(app, ["brief", "check", "B1"]).stdout
    assert "R1 [missing]" in checked
    assert f"#{task.number} pass" in checked and f"#{other.number} missing" in checked


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
    run in a worktree and recorded from the principal checkout is about the worktree, both
    when it is recorded and when it is checked. The principal moves on BEFORE the record:
    judged against the recorder's checkout, the record itself is refused (review of #341,
    finding 1: with the two still identical, that half was never exercised)."""
    worktree = repo.parent / "wt-answer"
    _git(repo, "worktree", "add", "-q", str(worktree), "-b", "answer")
    brief, task = _linked()
    report = _run(_py("print('ok')"), task=task, cwd=worktree)
    assert report.source_root is not None and Path(report.source_root).samefile(worktree)
    (repo / "app.py").write_text("def answer():\n    return 0\n")  # the principal moves on

    _record(brief, task, report)  # recorded from the principal checkout (the cwd)

    assert Path.cwd().samefile(repo)
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


def test_a_report_serialized_differently_by_a_later_build_still_backs_its_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #341, finding 3: the digest is of the report's bytes on disk, not of the
    model re-serialized, so a schema or pydantic change does not stale every pass."""
    brief, task = _linked()
    _record(brief, task, _run(_py("print('ok')"), task=task))
    original = reports.CommandReport.model_dump_json

    def reserialized(self: reports.CommandReport, **kw: Any) -> str:
        return original(self, **kw).replace('"schema_version":1', '"schema_version": 1')

    monkeypatch.setattr(reports.CommandReport, "model_dump_json", reserialized)
    assert _check(brief) == {"R1": "pass"}


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
    assert "R1 [" not in text.output, "a passing requirement is not listed"
    assert f"--project {task.project_id} --task {task.id}" in text.output, "the real ids"
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


def test_mcp_task_update_done_carries_the_refusal_and_the_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #341, finding 5: both refusals reach an MCP client as the stable
    ``error: …`` tool error, the race (a RuntimeError) included."""
    pytest.importorskip("mcp", reason="the [serve] extra is not installed")
    from mcp.server.mcpserver.exceptions import ToolError

    from aisquare.services import mcp_server

    brief, task = _linked()
    with pytest.raises(ToolError, match=r"^error: .* is not proven: B1 R1 missing"):
        mcp_server.task_update(task.id, "done")
    _record(brief, task, _run(_py("print('ok')"), task=task))
    real_gate = briefs.gate

    def gate_then_correct(store: Any, gated: TeamTask) -> dict[str, int]:
        read = real_gate(store, gated)
        briefs.update(brief.id, checks={"R1": "pytest -q"})
        return read

    monkeypatch.setattr(briefs, "gate", gate_then_correct)
    with pytest.raises(ToolError, match=r"^error: brief .* changed .*run task_update done"):
        mcp_server.task_update(task.id, "done")
    assert _status(task) == "todo"


def test_remote_task_done_answers_409_for_the_refusal_and_the_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aisquare.services import remote_server

    brief, task = _linked()
    done = remote_server.live_writes().handlers["task/done"]
    with pytest.raises(remote_server.RequestError) as refused:
        done({"ref": task.id})
    assert (refused.value.status, refused.value.error) == (409, "task_not_proven")
    assert "B1 R1 missing" in str(refused.value.message)
    _record(brief, task, _run(_py("print('ok')"), task=task))
    real_gate = briefs.gate

    def gate_then_correct(store: Any, gated: TeamTask) -> dict[str, int]:
        read = real_gate(store, gated)
        briefs.update(brief.id, checks={"R1": "pytest -q"})
        return read

    monkeypatch.setattr(briefs, "gate", gate_then_correct)
    with pytest.raises(remote_server.RequestError) as raced:
        done({"ref": task.id})
    assert (raced.value.status, raced.value.error) == (409, "brief_changed")
    assert _status(task) == "todo"


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
    """A usage error, exit 2, so a validator's script tells a typo from NOT PROVEN (exit 1)
    (review of #341, nit 8)."""
    _linked()
    stranger = tmp_path / "stranger"
    stranger.mkdir()
    for path, why in (
        (stranger, "not a checkout of this brief's project"),
        (repo / "sub", "not a directory"),
    ):
        result = runner.invoke(app, ["brief", "check", "B1", "--source-root", str(path)])
        assert result.exit_code == 2 and why in result.output, result.output
        assert "stale" not in result.output
    (repo / "sub").mkdir()
    inside = runner.invoke(app, ["brief", "check", "B1", "--source-root", str(repo / "sub")])
    assert "not a checkout" in inside.output, "a sub-directory is not a checkout either"
    assert inside.exit_code == 2


def test_under_a_hub_an_unrelated_repository_is_still_no_source_root(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #341, finding 4: with AISQUARE_TEAM_HUB set, every directory maps to the
    hub's board, so any git toplevel passed as --source-root and read as a page of
    "stale". It must share the project's repository; a worktree of it still does."""
    worktree = repo.parent / "wt-hubbed"
    _git(repo, "worktree", "add", "-q", str(worktree), "-b", "hubbed")
    brief, task = _linked()
    _record(brief, task, _run(_py("print('ok')"), task=task))
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    _git(unrelated, "init", "-q")
    (unrelated / "app.py").write_text("something else\n")
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(repo))
    from aisquare.core import orchestrator

    assert orchestrator.team_project(unrelated).id == brief.project_id, "the hub's premise"
    with pytest.raises(briefs.SourceRootError, match="not a checkout of this brief's project"):
        briefs.check(brief.id, source_root=unrelated)
    assert _check(brief, source_root=worktree) == {"R1": "pass"}


def test_under_a_hub_a_repository_a_check_was_recorded_in_is_a_source_root(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of --source-root: a hub's board can cover several repositories,
    and a check run in one of them is about it. That repository is a checkout of the
    brief (its passes are compared with it); one no check names is still refused."""
    sibling = tmp_path / "sibling"
    sibling.mkdir()
    _git(sibling, "init", "-q")
    (sibling / "lib.py").write_text("VALUE = 1\n")
    _git(sibling, "add", ".")
    _git(sibling, "commit", "-q", "-m", "one")
    stranger = tmp_path / "stranger"
    stranger.mkdir()
    _git(stranger, "init", "-q")
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(repo))
    brief, task = _linked()
    report = _run(_py("print('ok')"), task=task, cwd=sibling)
    assert report.source_root is not None and Path(report.source_root).samefile(sibling)
    _record(brief, task, report)

    assert _check(brief, source_root=sibling) == {"R1": "pass"}
    (sibling / "lib.py").write_text("VALUE = 2\n")
    assert _check(brief, source_root=sibling) == {"R1": "stale"}
    with pytest.raises(briefs.SourceRootError):
        briefs.check(brief.id, source_root=stranger)


def test_evidence_needs_exactly_one_backing(runner: CliRunner, tmp_path: Path) -> None:
    _, task = _linked()
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"x")
    report = _run(_py("print('ok')"), task=task)
    base = ["brief", "evidence", "B1", "R1", "--task", task.id, "--verdict", "pass",
            "--summary", "x"]  # fmt: skip
    for extra in ([], ["--artifact", str(shot), "--report", report.id]):
        result = runner.invoke(app, [*base, *extra])
        assert result.exit_code == 1, result.output
        assert "exactly one of --report or --artifact" in result.output
    assert briefs.show("B1").brief.evidence == []


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
    # The clip is what keeps it under the cap WITH requirements in it (review of #341,
    # finding 2): unclipped, no 4,000-character line fits, and the block shows none.
    shown = [line for line in block.splitlines() if line.startswith("  R")]
    assert len(shown) >= 3, block
    assert shown[0].startswith("  R1 [missing] Requirement 0 x")
    text, _, check = shown[0].partition(" — check: ")
    assert text.endswith("…") and check.startswith("c") and check.endswith("…"), shown[0]
    assert len(shown[0]) < 400


def _busy_board(monkeypatch: pytest.MonkeyPatch, repo: Path, runner: CliRunner) -> TeamTask:
    """The tester's board on #341: 7 live coder sessions, 8 open tasks with long titles,
    three 1,500-character results among the latest events, and a 64-requirement brief
    at its limits linked to a task the coder holds."""
    for n in range(6):
        _start_as(monkeypatch, repo, runner, f"dddd{n:04d}-0000-0000-0000-000000000000")
    _start_as(monkeypatch, repo, runner, CODER)
    for n in range(8):
        team.add_task(f"Task {n}: " + "a long title about the work " * 5, role="coder")
    texts = [f"Requirement {n} " + "x" * 1_980 for n in range(64)]
    brief = briefs.create("Everything", texts)
    briefs.update(brief.id, checks={f"R{n}": "c" * 1_970 for n in range(1, 65)})
    task, _ = team.add_task("The coder's own", role="coder")
    briefs.link(brief.id, task.id, [f"R{n}" for n in range(1, 65)])
    team.claim_task(task.id, session_ref=CODER)
    # Last, so they are among the latest events the briefing shows.
    for n in range(3):
        team.add_note(f"result {n}: " + "r" * 1_500, kind="result", session_ref=CODER)
    return task


def _start_as(monkeypatch: pytest.MonkeyPatch, repo: Path, runner: CliRunner, session: str) -> str:
    """``asq hook session-start`` as Claude Code runs it: the stdout it reads."""
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    payload = json.dumps({"cwd": str(repo), "session_id": session, "source": "startup"})
    result = runner.invoke(app, ["hook", "session-start"], input=payload)
    monkeypatch.delenv("AISQUARE_ROLE")
    assert result.exit_code == 0, result.output
    return result.stdout


def test_on_a_busy_board_the_block_takes_only_the_room_left(
    monkeypatch: pytest.MonkeyPatch, repo: Path, runner: CliRunner
) -> None:
    """Tester's finding on #341: a fixed 2,500-character block took a busy board's session
    start to 10,309 characters, and Claude Code cut the tail (its last requirements, the
    'Record a check' line, the closing tag). The block now takes the room left."""
    _busy_board(monkeypatch, repo, runner)
    monkeypatch.setattr(briefs, "session_requirements", lambda *a, **k: "")
    without = len(_start_as(monkeypatch, repo, runner, CODER))
    monkeypatch.setattr(briefs, "session_requirements", _REAL_SESSION_REQUIREMENTS)

    started = _start_as(monkeypatch, repo, runner, CODER)

    assert without < 10_000, "the board alone fits: the premise"
    assert without + briefs.REQUIREMENTS_BUDGET > 10_000, "and a fixed share would not"
    assert len(started) <= 10_000, len(started)
    block = started[started.index("<aisquare-requirements>") :]
    assert block.rstrip("\n").endswith("</aisquare-requirements>"), "the closing tag stays"
    shown = [line for line in block.splitlines() if line.startswith("  R")]
    assert shown, "some requirements still fit"
    assert all(line.endswith("…") for line in shown), "no line is cut, only clipped"


def test_with_no_room_for_a_requirement_the_block_is_a_pointer_or_nothing(
    monkeypatch: pytest.MonkeyPatch, repo: Path, runner: CliRunner
) -> None:
    task = _busy_board(monkeypatch, repo, runner)
    with store_session() as store:
        session = store.get_session(CODER)
        assert session is not None
        pointer = briefs.session_requirements(store, session, task, budget=300)
        nothing = briefs.session_requirements(store, session, task, budget=40)
    assert pointer.startswith("\n<aisquare-requirements>\n")
    assert pointer.endswith("</aisquare-requirements>") and len(pointer) <= 300
    assert "`asq brief show B1`" in pointer and "  R1" not in pointer
    assert nothing == ""


def test_a_session_start_the_ci_parts_take_over_the_cap_drops_the_block(
    monkeypatch: pytest.MonkeyPatch, repo: Path, runner: CliRunner
) -> None:
    """The CI parts come after the board and were not known when the block was sized: if
    the whole goes over the cap, the block goes, and nothing else of main's changes."""
    from aisquare.services import hooks

    _busy_board(monkeypatch, repo, runner)
    monkeypatch.setattr(hooks, "_session_start_ci", lambda *a, **k: ("", "z" * 4_000))
    started = _start_as(monkeypatch, repo, runner, CODER)
    assert "<aisquare-requirements>" not in started
    assert "<aisquare-team>" in started and started.rstrip("\n").endswith("z")


def test_no_header_in_the_block_is_left_without_a_requirement(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    """Review of #341, nit 7: every line is budgeted, a header too, and a brief whose
    first requirement does not fit leaves no header behind. The budget is set where a
    header fits and a requirement under it does not."""
    _start(monkeypatch, repo)
    task, _ = team.add_task("Two briefs", role="coder")
    for n in range(2):
        brief = briefs.create(f"Brief {n}", [f"B{n} requirement " + "y" * 300])
        briefs.link(brief.id, task.id, ["R1"])
    team.claim_task(task.id, session_ref=CODER)
    with store_session() as store:
        session = store.get_session(CODER)
        assert session is not None
        full = briefs.session_requirements(store, session, None).splitlines()
        header, requirement = full[2], full[3]
        assert " is linked to B1" in header and requirement.startswith("  R1")
        # The frame, the first brief whole, and the second's header, not its requirement.
        assert " is linked to B2" in full[4]
        reserved = briefs.record_hint(session.project_id, "t" * len(task.id), "B999999")
        frame = sum(len(part) for part in (briefs._OPEN, briefs._CLOSE, briefs._MORE, reserved))
        budget = frame + 2 + sum(len(line) + 1 for line in full[2:5]) + 10
        block = briefs.session_requirements(store, session, None, budget=budget).splitlines()

    assert header in block and requirement in block
    assert not any(" is linked to B2" in line for line in block), block
    assert "More are linked: `asq brief show` has them all." in block


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


def test_prune_says_how_many_reports_it_kept_for_evidence(runner: CliRunner) -> None:
    """Validator round on #136, M3: a prune of everything printed "Removed 1 saved
    reports." and silently kept the reports evidence cites. It now says so."""
    brief, task = _linked()
    cited = _run(_py("print('ok')"), task=task)
    _record(brief, task, cited)
    _run(_py("print('loose')"))

    text = runner.invoke(app, ["reports", "prune", "--keep", "0", "--days", "0"])
    assert text.exit_code == 0, text.output
    assert "Removed 1 saved reports; kept 1 that recorded evidence cites" in text.stdout
    as_json = runner.invoke(app, ["--json", "reports", "prune", "--keep", "0", "--days", "0"])
    payload = json.loads(as_json.stdout)
    assert payload == {"removed": [], "protection_known": True, "kept_for_evidence": 1}
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
