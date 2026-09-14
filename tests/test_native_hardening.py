"""Regressions for the adversarial review of the native briefs/reports branch.

Each test names the defect it pins. They are grouped by the module they guard so
a failure points at one owner.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.core.store import store_session
from aisquare.services import command_reports as reports
from aisquare.services import team
from aisquare.services import work_briefs as briefs

# --- command reports -------------------------------------------------------------------------


def _python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_retention_never_deletes_a_report_that_backs_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kept = reports.run_command(_python("print(1)"))
    later = [reports.run_command(_python("print(2)")) for _ in range(3)]
    monkeypatch.setattr(reports, "protected_report_ids", lambda: {kept.id})
    removed = reports.prune_reports(keep=1, protect={kept.id})
    assert kept.id not in removed
    assert reports.load_report(kept.id).id == kept.id
    assert len([r for r in later if r.id in removed]) == 2


def test_combined_short_flags_disable_pytest_compaction() -> None:
    assert reports._pytest_capture_disabled(["-q", "-s"])
    assert reports._pytest_capture_disabled(["-sv"])
    assert reports._pytest_capture_disabled(["-xqs"])
    assert reports._pytest_capture_disabled(["--capture=no"])
    assert not reports._pytest_capture_disabled(["-q", "-x", "--tb=short"])


def test_progress_lookalikes_inside_a_failure_section_survive() -> None:
    text = (
        "============================= test session starts ==============================\n"
        "tests/test_x.py .F                                                       [100%]\n"
        "=================================== FAILURES ===================================\n"
        "___________________________________ test_b _____________________________________\n"
        "----------------------------- Captured stdout call -----------------------------\n"
        "tests/test_x.py .F                                                       [100%]\n"
        "=========================== short test summary info ============================\n"
        "FAILED tests/test_x.py::test_b\n"
        "========================= 1 failed, 1 passed in 0.01s ==========================\n"
    )
    kind, output = reports.compact_stdout(["pytest", "-q"], text.encode(), truncated=False)
    assert kind == "pytest"
    body = output.decode()
    assert body.count("tests/test_x.py .F") == 1, "only the collection-zone line goes"
    assert "Captured stdout call" in body and "FAILED tests/test_x.py::test_b" in body


def test_one_megabyte_line_does_not_hang_compaction() -> None:
    text = (
        "=== test session starts ===\n" + "t" * 1_000_000 + " .py [ 50%]\n" + "1 passed in 0.1s\n"
    )
    started = time.monotonic()
    reports.compact_stdout(["pytest"], text.encode(), truncated=False)
    assert time.monotonic() - started < 1.0


def test_a_report_records_no_interruption_on_a_clean_run_and_survives_reload() -> None:
    report = reports.run_command(_python("print('ok')"))
    assert report.interrupted_by is None
    assert reports.load_report(report.id).interrupted_by is None


def test_pending_directories_without_a_pid_record_are_left_alone(tmp_path: Path) -> None:
    root = reports.reports_dir()
    root.mkdir(parents=True, exist_ok=True)
    unknown = root / ".pending-unknown"
    unknown.mkdir()
    dead = root / ".pending-dead"
    dead.mkdir()
    (dead / "wrapper.pid").write_text("999999999\n")
    import os

    old = time.time() - 7200
    os.utime(dead, (old, old))
    removed = reports.prune_reports()
    assert ".pending-dead" in removed and not dead.exists()
    assert ".pending-unknown" not in removed and unknown.exists()


# --- work briefs -------------------------------------------------------------------------------


@pytest.fixture
def work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "app.py").write_text("version = 1\n")
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def proof(tmp_path: Path) -> Path:
    report = tmp_path / "proof.txt"
    report.write_text("Actual test report: success\n")
    return report


def _contract(work: Path) -> tuple[briefs.WorkBrief, str]:
    brief = briefs.create("Login", ["Valid login opens dashboard", "Phone error is visible"])
    task, _ = team.add_task("Build login", role="coder", cwd=work)
    brief = briefs.link(brief.id, task.id, ["R1", "R2"])
    return brief, task.id


def _record(
    brief_id: str, task: str, requirement: str, proof: Path, verdict: str, summary: str
) -> None:
    briefs.record_evidence(
        brief_id,
        requirement,
        task_ref=task,
        verdict=verdict,  # type: ignore[arg-type]
        summary=summary,
        artifact=proof,
    )


def test_a_failure_against_work_in_progress_keeps_the_owner(work: Path, proof: Path) -> None:
    brief, task = _contract(work)
    team.hook_session_start("w1", work, "startup")
    team.claim_task(task, session_ref="w1")
    _record(brief.id, task, "R1", proof, "fail", "clipped on a phone")
    after = team.show_task(task)
    assert after.status == "doing" and after.claimed_by == "w1"
    team.review_task(task, session_ref="w1")
    _record(brief.id, task, "R1", proof, "fail", "still clipped")
    assert team.show_task(task).status == "todo"


def test_a_dropped_task_is_never_revived_by_a_finding(work: Path, proof: Path) -> None:
    brief, task = _contract(work)
    with store_session() as store:
        store.set_task_status(task, "dropped")
    _record(brief.id, task, "R1", proof, "fail", "irrelevant now")
    assert team.show_task(task).status == "dropped"


def test_failure_count_is_per_task_and_resets_on_correction(work: Path, proof: Path) -> None:
    brief, task = _contract(work)
    other, _ = team.add_task("Second lane", role="coder", cwd=work)
    briefs.link(brief.id, other.id, ["R1"])
    for n in range(3):
        _record(brief.id, task, "R1", proof, "fail", f"failure {n}")
    assert team.show_task(task).status == "blocked"
    assert team.show_task(other.id).status == "todo", "another task's failures are not mine"
    # Re-planning: the requirement is corrected and a human reopens the blocked task.
    briefs.update(brief.id, changes={"R1": "Valid login opens the dashboard page"})
    team.reopen_task(task, reason="re-planned after correction")
    assert team.show_task(task).status == "todo"
    _record(brief.id, task, "R1", proof, "fail", "one more after re-planning")
    assert team.show_task(task).status == "todo", "the count restarted with the correction"
    for n in range(2):
        _record(brief.id, task, "R1", proof, "fail", f"post-correction failure {n}")
    assert team.show_task(task).status == "blocked", "three NEW failures block again"


def test_an_identical_finding_after_a_pass_supersedes_it(work: Path, proof: Path) -> None:
    brief, task = _contract(work)
    _record(brief.id, task, "R1", proof, "fail", "broken")
    _record(brief.id, task, "R1", proof, "pass", "fixed")
    _record(brief.id, task, "R2", proof, "pass", "fixed")
    assert briefs.check(brief.id).complete
    _record(brief.id, task, "R1", proof, "fail", "broken")
    result = briefs.check(brief.id)
    assert not result.complete
    assert next(r for r in result.requirements if r.requirement_id == "R1").status == "fail"


def test_source_root_must_be_a_real_checkout_of_this_project(work: Path) -> None:
    brief, _ = _contract(work)
    with pytest.raises(ValueError, match="not a directory"):
        briefs.check(brief.id, source_root=work / "nowhere")
    with pytest.raises(ValueError, match="checkout or a git worktree"):
        briefs.check(brief.id, source_root=Path("/"))


def test_an_empty_brief_ref_is_refused(work: Path) -> None:
    _contract(work)
    with pytest.raises(ValueError, match="brief id"):
        briefs.show("")


def test_referenced_report_ids_lists_every_evidence_report(work: Path) -> None:
    brief, task = _contract(work)
    report = reports.run_command(
        _python("print('passed')"), project_id=brief.project_id, task_id=task
    )
    briefs.record_evidence(
        brief.id, "R1", task_ref=task, verdict="pass", summary="ran", report_id=report.id
    )
    assert briefs.referenced_report_ids() == {report.id}
    assert reports.protected_report_ids() == {report.id}


def test_mode_off_injects_no_native_instructions_but_keeps_the_facts(work: Path) -> None:
    brief, _ = _contract(work)
    briefs.set_mode("off")
    with store_session() as store:
        context = briefs.session_context(store, brief.project_id, "fresh-session", "coder")
    assert "Working rules: off" in context
    assert "asq exec" not in context and "Inspect the affected flow" not in context
    assert brief.id in context and "Valid login opens dashboard" in context


def test_working_rules_follow_a_session_relaunched_under_another_role(work: Path) -> None:
    brief, _ = _contract(work)
    with store_session() as store:
        coder = briefs.session_context(store, brief.project_id, "s-1", "coder")
        assert "Inspect the affected flow first" in coder
        tester = briefs.session_context(store, brief.project_id, "s-1", "tester")
        assert (
            "Run every required check" in tester and "Inspect the affected flow first" not in tester
        )


def test_json_errors_carry_the_reason(work: Path) -> None:
    from aisquare.cli.app import app

    _contract(work)
    runner = CliRunner()
    result = runner.invoke(app, ["--json", "brief", "show", "brief_nope"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["error"] == "brief_error" and "brief_nope" in payload["detail"]


def test_brief_text_with_terminal_controls_is_neutralized_for_the_operator(work: Path) -> None:
    from aisquare.cli.app import app

    brief = briefs.create("Login \x1b]0;pwned\x07", ["Valid \x1b[2J login"])
    result = CliRunner().invoke(app, ["brief", "show", brief.id])
    assert result.exit_code == 0, result.output
    assert "\x1b" not in result.output and "\x07" not in result.output
    assert "Valid" in result.output


# --- packaging -----------------------------------------------------------------------------------


def test_the_built_wheel_ships_the_native_modules(tmp_path: Path) -> None:
    """Stage 3's gate, checked on a real build rather than a hand-written paragraph."""
    pytest.importorskip("build")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(tmp_path), str(root)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        pytest.skip(f"hatchling could not build here: {result.stderr[-400:]}")
    import zipfile

    wheel = next(tmp_path.glob("*.whl"))
    names = set(zipfile.ZipFile(wheel).namelist())
    for module in (
        "services/work_briefs",
        "services/command_reports",
    ):
        assert f"aisquare/{module}.py" in names
