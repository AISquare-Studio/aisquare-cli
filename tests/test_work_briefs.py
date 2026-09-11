"""Requirements survive corrections and only fresh source/evidence can pass the gate."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.brief import app
from aisquare.core import harness, paths
from aisquare.core import store as store_module
from aisquare.core.ids import new_event_id
from aisquare.core.store import store_session
from aisquare.models import TeamEvent
from aisquare.services import team
from aisquare.services import work_briefs as briefs


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


def contract(work: Path) -> tuple[briefs.WorkBrief, str]:
    brief = briefs.create("Login", ["Valid login opens dashboard", "Phone error is visible"])
    task, _ = team.add_task("Build login", role="coder", cwd=work)
    brief = briefs.link(brief.id, task.id, ["R1", "R2"])
    return brief, task.id


def record(brief: briefs.WorkBrief, task: str, requirement: str, proof: Path) -> None:
    briefs.record_evidence(
        brief.id,
        requirement,
        task_ref=task,
        verdict="pass",
        summary="Checked the requested outcome",
        artifact=proof,
    )


def test_story_failure_fix_recheck_and_final_gate(work: Path, proof: Path) -> None:
    brief, task = contract(work)
    assert not briefs.check(brief.id).complete
    with pytest.raises(ValueError, match="not verified"):
        team.finish_task(task)
    record(brief, task, "R1", proof)
    briefs.record_evidence(
        brief.id,
        "R2",
        task_ref=task,
        verdict="fail",
        summary="Phone error is clipped",
        artifact=proof,
    )
    assert briefs.check(brief.id).requirements[1].status == "fail"
    assert team.show_task(task).status == "todo"
    (work / "app.py").write_text("version = 2\n")
    assert all(row.status == "stale" for row in briefs.check(brief.id).requirements)
    briefs.update(brief.id, source_revision="build-2")
    record(brief, task, "R1", proof)
    record(brief, task, "R2", proof)
    assert briefs.check(brief.id).complete
    assert team.finish_task(task).status == "done"
    assert len(briefs.show(brief.id).evidence) == 4


def test_correction_preserves_ids_invalidates_only_affected_and_reopens(
    work: Path, proof: Path
) -> None:
    brief, task = contract(work)
    record(brief, task, "R1", proof)
    record(brief, task, "R2", proof)
    team.finish_task(task)
    corrected = briefs.update(brief.id, changes={"R2": "Phone error is visible at 320px"})
    assert [r.id for r in corrected.requirements] == ["R1", "R2"]
    assert corrected.requirements[0].revision == 1
    assert corrected.requirements[1].revision == 2
    assert [r.status for r in briefs.check(brief.id).requirements] == ["pass", "stale"]
    assert team.show_task(task).status == "todo"
    assert "320px" in briefs.export_markdown(corrected)


def test_artifact_mutation_is_not_valid_proof(work: Path, proof: Path) -> None:
    brief, task = contract(work)
    record(brief, task, "R1", proof)
    record(brief, task, "R2", proof)
    proof.write_text("Different report")
    assert not briefs.check(brief.id).complete
    assert briefs.check(brief.id).requirements[0].status == "stale"
    proof.unlink()
    assert not briefs.check(brief.id).complete


def test_new_source_without_declared_update_invalidates_proof(work: Path, proof: Path) -> None:
    brief, task = contract(work)
    record(brief, task, "R1", proof)
    record(brief, task, "R2", proof)
    (work / "new.py").write_text("print('new implementation')")
    assert not briefs.check(brief.id).complete
    # Same summary/artifact on newly checked source must not dedup away fresh proof.
    record(brief, task, "R1", proof)
    record(brief, task, "R2", proof)
    assert briefs.check(brief.id).complete
    assert len(briefs.show(brief.id).evidence) == 4


def test_duplicate_failures_do_not_spawn_or_loop_but_distinct_failures_block(
    work: Path, proof: Path
) -> None:
    brief = briefs.create("Login", ["Phone layout fits"])
    first = briefs.finding(brief.id, "R1", summary="Clipped", artifact=proof)
    again = briefs.finding(brief.id, "R1", summary="Clipped", artifact=proof)
    assert len(team.list_tasks()) == 1
    assert first.revision == again.revision
    assert len(again.evidence) == 1
    task = again.requirements[0].task_ids[0]
    briefs.finding(brief.id, "R1", summary="Still clipped after fix", artifact=proof)
    briefs.finding(brief.id, "R1", summary="Third attempt still clipped", artifact=proof)
    assert team.show_task(task).status == "blocked"
    assert briefs.check(brief.id).requirements[0].status == "blocked"
    with store_session() as store:
        events = store.filtered_events(brief.project_id, kind="task_blocked")
        assert any(e.task_id == task for e in events)


def test_new_task_coverage_cannot_reuse_unrelated_or_cross_board_evidence(
    work: Path, proof: Path
) -> None:
    brief, task = contract(work)
    other, _ = team.add_task("Unrelated", cwd=work)
    with pytest.raises(ValueError, match="link this task"):
        record(brief, other.id, "R1", proof)
    another = work.parent / "another"
    another.mkdir()
    other, _ = team.add_task("Other project", cwd=another)
    with pytest.raises(ValueError, match="another board"):
        briefs.link(brief.id, other.id, ["R1"])
    record(brief, task, "R1", proof)
    with pytest.raises(KeyError):
        briefs.show(brief.id, cwd=another)


def test_missing_artifact_never_records_a_pass(work: Path) -> None:
    brief, task = contract(work)
    with pytest.raises(FileNotFoundError):
        record(brief, task, "R1", work / "not-real.txt")
    assert not briefs.show(brief.id).evidence


def test_missing_or_dropped_link_cannot_pass(work: Path, proof: Path) -> None:
    brief, task = contract(work)
    record(brief, task, "R1", proof)
    record(brief, task, "R2", proof)
    team.drop_task(task)
    assert not briefs.check(brief.id).complete


def test_boundaries_correction_and_exact_contradiction_block_gate(work: Path, proof: Path) -> None:
    brief, task = contract(work)
    record(brief, task, "R1", proof)
    record(brief, task, "R2", proof)
    briefs.update(brief.id, boundaries=["Valid login opens dashboard"])
    record(brief, task, "R1", proof)
    record(brief, task, "R2", proof)
    result = briefs.check(brief.id)
    assert not result.complete and result.warnings


def test_optimistic_write_does_not_drop_another_workers_evidence(work: Path) -> None:
    brief, _ = contract(work)
    current = briefs.update(brief.id, changes={"R1": "Corrected by first worker"})
    with store_session() as store, pytest.raises(ValueError, match="concurrently"):
        store.save_work_brief(
            brief.id,
            brief.project_id,
            brief.revision + 1,
            brief.model_dump_json(),
            brief.revision,
            TeamEvent(
                id=new_event_id(),
                project_id=brief.project_id,
                text="stale write",
                created_at=datetime.now(UTC),
            ),
            {},
        )
    assert briefs.show(brief.id).revision == current.revision
    assert briefs.show(brief.id).requirements[0].text == "Corrected by first worker"


def test_working_mode_is_session_versioned_and_has_no_persona_input(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    native = team.hook_session_start("native-session", work, "startup")
    assert harness.WORK_RULES_VERSION in native
    assert "Reuse suitable project code" in native
    briefs.set_mode("off")
    resumed = team.hook_session_start("native-session", work, "resume")
    assert harness.WORK_RULES_VERSION in resumed
    legacy = team.hook_session_start("legacy-session", work, "startup")
    assert "Working rules: off" in legacy
    assert "Reuse suitable project code" not in legacy
    assert "Your standing cycle (coder)" in legacy
    assert "persona" not in native.lower()


def test_startup_assignment_receives_intended_contract_and_matching_requirements(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    brief, task = contract(work)
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    monkeypatch.setenv("AISQUARE_TASK_ID", task)
    context = team.hook_session_start("worker", work, "startup")
    assert f"Claim THIS task: `asq task claim {task}" in context
    assert "overrides generic 'task next'" in context
    assert "Work brief " + brief.id in context
    assert "Phone error is visible" in context
    assert team.show_task(task).claimed_by is None


def test_ui_tester_has_real_browser_cycle_and_native_evidence_habits() -> None:
    assert "ui-tester" in harness.ROLE_PROFILES
    assert "real browser" in " ".join(harness.role_cycle("ui-tester2", "sid"))
    assert "stale screenshots" in " ".join(harness.working_rules("ui-tester"))


def test_upgrade_v14_keeps_existing_task_and_adds_briefs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "old.db"
    connection = sqlite3.connect(db)
    connection.row_factory = sqlite3.Row
    for index, migration in enumerate(store_module._MIGRATIONS[:14]):
        preflight = store_module._PREPARE.get(index)
        if preflight:
            preflight(connection)
        for statement in store_module._statements(migration):
            connection.execute(statement)
        connection.execute(f"PRAGMA user_version = {index + 1}")
        connection.commit()
    connection.execute(
        "INSERT INTO team_task (id, project_id, key, title, created_at, updated_at) "
        "VALUES ('old-task', 'p', 'k', 'existing work', '2026-09-11', '2026-09-11')"
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(paths, "db_path", lambda: db)
    with store_module.store_session() as store:
        assert store.get_task("old-task") is not None
        assert store.work_briefs("p") == []
    with sqlite3.connect(db) as connection:
        assert (
            connection.execute("PRAGMA user_version").fetchone()[0] == store_module.SCHEMA_VERSION
        )


def test_cli_create_correction_link_failure_export_and_invalid_input(
    work: Path, proof: Path, runner: CliRunner
) -> None:
    created = runner.invoke(app, ["create", "Login", "-r", "Phone fits"])
    assert created.exit_code == 0, created.output
    brief = briefs.list_briefs()[0]
    task, _ = team.add_task("Login", cwd=work)
    result = runner.invoke(app, ["link", brief.id, task.id, "-r", "R1"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["update", brief.id, "--check", "R1=Open phone browser"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(
        app,
        [
            "evidence",
            brief.id,
            "R1",
            "--task",
            task.id,
            "--verdict",
            "fail",
            "--summary",
            "Phone clipped",
            "--artifact",
            str(proof),
        ],
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["check", brief.id])
    assert result.exit_code == 1 and "NOT VERIFIED" in result.output
    exported = proof.parent / "brief.md"
    result = runner.invoke(app, ["export", brief.id, "-o", str(exported)])
    assert result.exit_code == 0 and "Phone clipped" in exported.read_text()
    result = runner.invoke(app, ["update", brief.id, "-r", "missing-id"])
    assert result.exit_code != 0 and "R1=" in result.output


def test_json_export_is_machine_readable(work: Path, runner: CliRunner) -> None:
    from aisquare.core.state import get_state

    brief = briefs.create("Login", ["Works"])
    get_state().json_output = True
    result = runner.invoke(app, ["export", brief.id])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["requirements"][0]["id"] == "R1"


def test_new_linked_task_needs_its_own_evidence(work: Path, proof: Path) -> None:
    brief, task = contract(work)
    record(brief, task, "R1", proof)
    record(brief, task, "R2", proof)
    extra, _ = team.add_task("Check additional login route", cwd=work)
    briefs.link(brief.id, extra.id, ["R1"])
    assert not briefs.check(brief.id).complete
    with pytest.raises(ValueError, match="not verified"):
        team.finish_task(extra.id)
    # An independent task may finish before a later contribution is checked.
    assert team.finish_task(task).status == "done"
    record(brief, extra.id, "R1", proof)
    assert briefs.check(brief.id).complete


def test_correction_between_gate_and_completion_prevents_done(
    work: Path,
    proof: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aisquare.core.store import ContextStore
    from aisquare.models import TeamTask

    brief, task = contract(work)
    record(brief, task, "R1", proof)
    record(brief, task, "R2", proof)
    original_gate = briefs.task_gate

    def racing_gate(store: ContextStore, value: TeamTask) -> dict[str, int]:
        snapshot = original_gate(store, value)
        briefs.update(brief.id, changes={"R1": "Corrected while finishing"})
        return snapshot

    monkeypatch.setattr(briefs, "task_gate", racing_gate)
    with pytest.raises(ValueError, match="changed during verification"):
        team.finish_task(task)
    assert team.show_task(task).status != "done"
    assert not briefs.check(brief.id).complete


def test_shared_hub_hashes_actual_workers_source(
    work: Path,
    proof: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub = work.parent / "hub"
    hub.mkdir()
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(hub))
    brief, task = contract(work)
    record(brief, task, "R1", proof)
    record(brief, task, "R2", proof)
    assert briefs.show(brief.id).evidence[0].source_root == str(work.resolve())
    assert briefs.check(brief.id, cwd=hub).complete
    assert not briefs.check(brief.id, cwd=hub, source_root=hub).complete
    (work / "app.py").write_text("version = 3")
    assert not briefs.check(brief.id, cwd=hub).complete


def test_command_evidence_rejects_failed_stale_and_changed_raw_report(work: Path) -> None:
    import sys

    from aisquare.services import command_reports

    brief, task = contract(work)
    failed = command_reports.run_command(
        [sys.executable, "-c", "raise SystemExit(1)"], project_id=brief.project_id, task_id=task
    )
    with pytest.raises(ValueError, match="failed command"):
        briefs.record_evidence(
            brief.id,
            "R1",
            task_ref=task,
            verdict="pass",
            summary="must not pass",
            report_id=failed.id,
        )
    passed = command_reports.run_command(
        [sys.executable, "-c", "print('passed')"], project_id=brief.project_id, task_id=task
    )
    (work / "app.py").write_text("version = 4")
    with pytest.raises(ValueError, match="stale"):
        briefs.record_evidence(
            brief.id, "R1", task_ref=task, verdict="pass", summary="old pass", report_id=passed.id
        )
    fresh = command_reports.run_command(
        [sys.executable, "-c", "print('passed')"], project_id=brief.project_id, task_id=task
    )
    for requirement in ("R1", "R2"):
        briefs.record_evidence(
            brief.id,
            requirement,
            task_ref=task,
            verdict="pass",
            summary="actual check",
            report_id=fresh.id,
        )
    assert briefs.check(brief.id).complete
    assert not briefs.check(brief.id).manual_evidence
    # Same byte count: stronger than the report reader's completeness check.
    (command_reports.reports_dir() / fresh.id / "stdout.bin").write_bytes(b"failed\n")
    assert not briefs.check(brief.id).complete


def test_session_resume_keeps_saved_rules_after_package_rules_change(
    work: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    original = team.hook_session_start("stable", work, "startup")
    monkeypatch.setattr(harness, "working_rules", lambda role: ["different new instructions"])
    resumed = team.hook_session_start("stable", work, "resume")
    assert "different new instructions" not in resumed
    assert "Reuse suitable project code" in original and "Reuse suitable project code" in resumed


def test_invalid_finding_does_not_create_a_correction_task(work: Path) -> None:
    brief = briefs.create("Login", ["Phone fits"])
    with pytest.raises(FileNotFoundError):
        briefs.finding(brief.id, "R1", summary="failure", artifact=work / "missing.png")
    assert not team.list_tasks()
    assert not briefs.show(brief.id).requirements[0].task_ids
