"""Work briefs (docs/work-briefs.md; docs/plans/evidence-gate.md, part 2).

Requirement ids are stable, links are many-to-many, evidence is manual and says so,
findings and corrections move linked tasks the way the spec says, and a session's own
writes do not wake it. The part-2 traps #136's review found that live here each have a
test below: an identical update changes nothing, a dropped or vanished duplicate is
ignored while a live linked task remains, and an ``--as`` write does not wake its writer.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import paths
from aisquare.core.store import BriefChangedError, store_session
from aisquare.models import TeamTask, WorkBrief
from aisquare.services import briefs, team
from aisquare.services import fleet as fleet_service
from tests.projects import add_project

MANAGER = "aaaa1111-0000-0000-0000-000000000000"
CODER = "bbbb2222-0000-0000-0000-000000000000"


@pytest.fixture(autouse=True)
def work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.chdir(root)
    add_project(root)
    return root


@pytest.fixture(autouse=True)
def nudges(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """The fleet's nudge, recorded instead of typed into a pane."""
    calls: list[tuple[str, str]] = []

    def record(project_id: str, *, reason: str, board: str | None = None) -> bool:
        calls.append((project_id, reason))
        return False

    monkeypatch.setattr(fleet_service, "nudge_manager", record)
    return calls


@pytest.fixture
def shot(work: Path) -> Path:
    path = work / "shot.png"
    path.write_bytes(b"a screenshot")
    return path


def _task(title: str = "Build login") -> TeamTask:
    task, _ = team.add_task(title, role="coder")
    return task


def _status(task: TeamTask) -> TeamTask:
    return team.show_task(task.id)


def _set_status(task: TeamTask, status: Any, owner: str | None = None) -> None:
    with store_session() as store:
        store.set_task_status(task.id, status)
        if owner is not None:
            store._conn.execute(  # type: ignore[attr-defined]
                "UPDATE team_task SET claimed_by = ? WHERE id = ?", (owner, task.id)
            )
            store._conn.commit()  # type: ignore[attr-defined]


def _brief(*requirements: str) -> WorkBrief:
    return briefs.create("Login", list(requirements or ("Valid login opens dashboard",)))


def _state(brief: WorkBrief, req_id: str) -> str:
    view = briefs.show(brief.id)
    return next(r.state for r in view.requirements if r.id == req_id)


def _events(kind: str) -> list[Any]:
    with store_session() as store:
        project = team.resolve_project()
        return store.filtered_events(project.id, kind=kind, limit=100)


def _pass(brief: WorkBrief, task: TeamTask, req_id: str, shot: Path, summary: str = "ok") -> None:
    briefs.record_evidence(
        brief.id, req_id, task_ref=task.id, verdict="pass", summary=summary, artifact=shot
    )


def _finding(brief: WorkBrief, task: TeamTask, req_id: str, summary: str) -> None:
    briefs.finding(brief.id, req_id, task_ref=task.id, summary=summary)


# --- ids, numbers and links ---------------------------------------------------------------


def test_requirement_ids_are_stable_through_corrections_and_additions() -> None:
    brief = _brief("Valid login opens dashboard", "Phone error is visible")
    assert [r.id for r in brief.requirements] == ["R1", "R2"]
    assert brief.number == 1 and brief.label == "B1"

    corrected, changed = briefs.update(
        brief.id, changes={"R1": "Valid login opens the dashboard"}, add=["Logout works"]
    )

    assert changed
    assert [(r.id, r.text) for r in corrected.requirements] == [
        ("R1", "Valid login opens the dashboard"),
        ("R2", "Phone error is visible"),
        ("R3", "Logout works"),
    ]
    assert briefs.show("B1").brief.id == brief.id == briefs.show("1").brief.id
    assert briefs.create("Second", ["Other"]).label == "B2"


def test_one_task_covers_several_requirements_and_one_requirement_needs_several_tasks(
    shot: Path,
) -> None:
    brief = _brief("R one", "R two")
    ui, api = _task("UI"), _task("API")
    briefs.link(brief.id, ui.id, ["R1", "R2"])
    briefs.link(brief.id, str(api.number), ["R1"])

    view = briefs.show(brief.id)
    assert [len(r.tasks) for r in view.requirements] == [2, 1]
    _pass(brief, ui, "R1", shot)
    # Each linked task proves its own part: the API's is still missing.
    assert _state(brief, "R1") == "missing"
    _pass(brief, api, "R1", shot)
    assert _state(brief, "R1") == "pass"
    assert _state(brief, "R2") == "missing"


def test_linking_again_what_is_linked_writes_nothing() -> None:
    brief = _brief()
    task = _task()
    briefs.link(brief.id, task.id, ["R1"])
    before = briefs.show(brief.id).brief.revision

    _, _, changed = briefs.link(brief.id, task.id, ["r1"])

    assert not changed
    assert briefs.show(brief.id).brief.revision == before


def test_a_task_on_another_board_cannot_be_linked(tmp_path: Path, work: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    add_project(other)
    foreign, _ = team.add_task("Elsewhere", cwd=other)
    brief = _brief()
    with pytest.raises(ValueError, match="another board"):
        briefs.link(brief.id, foreign.id, ["R1"])


# --- evidence -------------------------------------------------------------------------


def test_artifact_evidence_is_manual_everywhere(runner: CliRunner, shot: Path) -> None:
    brief = _brief()
    task = _task()
    briefs.link(brief.id, task.id, ["R1"])

    recorded = runner.invoke(
        app,
        ["--json", "brief", "evidence", "B1", "R1", "--task", str(task.number),
         "--verdict", "pass", "--summary", "saw the dashboard", "--artifact", str(shot)],
    )  # fmt: skip
    shown = runner.invoke(app, ["brief", "show", "B1"])
    as_json = runner.invoke(app, ["--json", "brief", "show", "B1"])

    assert recorded.exit_code == 0, recorded.output
    assert json.loads(recorded.stdout)["evidence"]["kind"] == "manual"
    assert "R1 r1 [pass (manual)]" in shown.stdout
    assert "pass [manual]" in shown.stdout
    view = json.loads(as_json.stdout)
    assert view["requirements"][0]["manual"] is True
    assert view["brief"]["evidence"][0]["kind"] == "manual"
    assert view["brief"]["evidence"][0]["report_id"] is None
    assert "[manual]" in _events("brief_evidence")[-1].text


def test_evidence_records_room_for_a_report_and_its_source() -> None:
    """Task C adds report-backed evidence without migrating: the fields are there."""
    from aisquare.models import BriefEvidence

    fields = set(BriefEvidence.model_fields)
    assert {"kind", "report_id", "source_root", "source_fingerprint"} <= fields
    assert BriefEvidence.model_fields["kind"].default == "manual"


def test_evidence_needs_a_linked_task_and_a_known_requirement(shot: Path) -> None:
    brief = _brief()
    task = _task()
    with pytest.raises(ValueError, match="not linked"):
        _pass(brief, task, "R1", shot)
    briefs.link(brief.id, task.id, ["R1"])
    with pytest.raises(ValueError, match="no requirement 'R9'"):
        _pass(brief, task, "R9", shot)
    assert briefs.show(brief.id).brief.evidence == []


def test_the_same_check_sent_again_writes_nothing(shot: Path) -> None:
    brief = _brief()
    task = _task()
    briefs.link(brief.id, task.id, ["R1"])
    _pass(brief, task, "R1", shot)
    before = briefs.show(brief.id).brief

    _, _, changed = briefs.record_evidence(
        brief.id, "R1", task_ref=task.id, verdict="pass", summary="ok", artifact=shot
    )

    assert not changed
    assert briefs.show(brief.id).brief == before


def test_an_artifact_that_changed_or_went_makes_its_pass_stale(shot: Path) -> None:
    brief = _brief()
    task = _task()
    briefs.link(brief.id, task.id, ["R1"])
    _pass(brief, task, "R1", shot)
    assert _state(brief, "R1") == "pass"
    shot.write_bytes(b"another screenshot")
    assert _state(brief, "R1") == "stale"
    shot.unlink()
    assert _state(brief, "R1") == "stale"


def test_an_artifact_must_be_a_regular_file(work: Path) -> None:
    brief = _brief()
    task = _task()
    briefs.link(brief.id, task.id, ["R1"])
    with pytest.raises(ValueError, match="not a regular file"):
        briefs.record_evidence(
            brief.id, "R1", task_ref=task.id, verdict="pass", summary="x", artifact=work
        )
    with pytest.raises(ValueError, match="cannot be read"):
        briefs.record_evidence(
            brief.id, "R1", task_ref=task.id, verdict="pass", summary="x",
            artifact=work / "missing.png",
        )  # fmt: skip


# --- findings -----------------------------------------------------------------------


@pytest.mark.parametrize("status", ["review", "done"])
def test_a_finding_sends_a_task_in_review_or_done_back_to_todo_unowned(status: str) -> None:
    brief = _brief()
    task = _task()
    briefs.link(brief.id, task.id, ["R1"])
    team.claim_task(task.id)
    _set_status(task, status)

    _finding(brief, task, "R1", "clipped on a phone")

    after = _status(task)
    assert (after.status, after.claimed_by) == ("todo", None)
    reopened = _events("task_reopened")[-1]
    assert reopened.task_id == task.id and "clipped on a phone" in reopened.text
    assert team.stopped_because(after) is not None


def test_a_finding_on_a_task_in_progress_keeps_its_owner_and_status() -> None:
    brief = _brief()
    task = _task()
    briefs.link(brief.id, task.id, ["R1"])
    claimed = team.claim_task(task.id)

    _finding(brief, task, "R1", "clipped on a phone")

    after = _status(task)
    assert (after.status, after.claimed_by) == ("doing", claimed.claimed_by)
    assert _state(brief, "R1") == "fail"


def test_a_finding_leaves_a_dropped_task_dropped() -> None:
    brief = _brief()
    task = _task()
    briefs.link(brief.id, task.id, ["R1"])
    team.drop_task(task.id)
    for n in range(3):
        _finding(brief, task, "R1", f"failure {n}")
    assert _status(task).status == "dropped"


def test_a_finding_links_a_task_not_linked_yet() -> None:
    brief = _brief()
    task = _task()
    _finding(brief, task, "R1", "broken")
    assert briefs.show(brief.id).brief.requirements[0].task_ids == [task.id]
    assert _state(brief, "R1") == "fail"


def test_three_distinct_failures_on_one_revision_block_the_task() -> None:
    brief = _brief()
    task = _task()
    briefs.link(brief.id, task.id, ["R1"])
    claimed = team.claim_task(task.id)

    _finding(brief, task, "R1", "first")
    _finding(brief, task, "R1", "second")
    _finding(brief, task, "R1", "second")  # identical: not new
    assert _status(task).status == "doing", "two distinct failures do not block"
    _finding(brief, task, "R1", "first")  # seen before on this revision: not distinct
    assert _status(task).status == "doing"
    _finding(brief, task, "R1", "third")

    after = _status(task)
    assert (after.status, after.claimed_by) == ("blocked", claimed.claimed_by)
    assert _events("task_blocked")[-1].task_id == task.id
    assert _state(brief, "R1") == "blocked"


def test_a_correction_starts_the_failure_count_again() -> None:
    brief = _brief()
    task = _task()
    briefs.link(brief.id, task.id, ["R1"])
    team.claim_task(task.id)
    _finding(brief, task, "R1", "first")
    _finding(brief, task, "R1", "second")
    briefs.update(brief.id, changes={"R1": "Valid login opens the dashboard"})
    _finding(brief, task, "R1", "third")
    assert _status(task).status == "doing"


def test_one_tasks_failures_never_block_another() -> None:
    brief = _brief()
    first, second = _task("one"), _task("two")
    briefs.link(brief.id, first.id, ["R1"])
    briefs.link(brief.id, second.id, ["R1"])
    for n in range(2):
        _finding(brief, first, "R1", f"first's {n}")
        _finding(brief, second, "R1", f"second's {n}")
    assert {_status(first).status, _status(second).status} == {"todo"}


# --- corrections ----------------------------------------------------------------------


def _proven(shot: Path, *requirements: str) -> tuple[WorkBrief, TeamTask]:
    brief = _brief(*requirements)
    task = _task()
    briefs.link(brief.id, task.id, [r.id for r in brief.requirements])
    for requirement in brief.requirements:
        _pass(brief, task, requirement.id, shot)
    _set_status(task, "done")
    return brief, task


def test_a_changed_requirement_makes_its_evidence_stale_and_reopens_its_done_task(
    shot: Path,
) -> None:
    brief, task = _proven(shot, "R one", "R two")

    corrected, _ = briefs.update(brief.id, changes={"R2": "R two, at 320px"})

    assert [_state(brief, "R1"), _state(brief, "R2")] == ["pass", "stale"]
    assert [r.revision for r in corrected.requirements] == [1, 2]
    assert _status(task).status == "todo"
    history = corrected.requirements[1].history
    assert [(v.revision, v.text) for v in history] == [(1, "R two")]
    assert corrected.corrections[-1].stale == ["R2"]
    assert len(corrected.evidence) == 2, "the old checks are kept"


def test_a_changed_check_makes_that_requirements_evidence_stale(shot: Path) -> None:
    brief, _ = _proven(shot, "R one", "R two")
    briefs.update(brief.id, checks={"R1": "pytest tests/test_login.py"})
    assert [_state(brief, "R1"), _state(brief, "R2")] == ["stale", "pass"]


@pytest.mark.parametrize(
    "correction",
    [{"assumptions": ["Users have an email"]}, {"boundaries": ["No SSO"]}],
    ids=["assumption", "boundary"],
)
def test_a_changed_assumption_or_boundary_makes_all_evidence_stale(
    shot: Path, correction: dict[str, Any]
) -> None:
    brief, task = _proven(shot, "R one", "R two")
    briefs.update(brief.id, **correction)
    assert [_state(brief, "R1"), _state(brief, "R2")] == ["stale", "stale"]
    assert _status(task).status == "todo"


def test_a_correction_keeps_a_working_owner_and_a_dropped_task(shot: Path) -> None:
    brief = _brief()
    working, dropped, reviewed = _task("working"), _task("dropped"), _task("reviewed")
    for task in (working, dropped, reviewed):
        briefs.link(brief.id, task.id, ["R1"])
    claimed = team.claim_task(working.id)
    team.drop_task(dropped.id)
    _set_status(reviewed, "review")

    briefs.update(brief.id, changes={"R1": "Valid login opens the dashboard"})

    assert (_status(working).status, _status(working).claimed_by) == ("doing", claimed.claimed_by)
    assert _status(dropped).status == "dropped"
    assert _status(reviewed).status == "todo"


def test_an_identical_update_changes_nothing(shot: Path, nudges: list[tuple[str, str]]) -> None:
    """Trap: re-sending an identical `brief update` bumped revisions, made evidence stale
    and reopened tasks."""
    brief = briefs.create(
        "Login", ["R one"], checks={"R1": "pytest"}, assumptions=["a"], boundaries=["b"]
    )
    task = _task()
    briefs.link(brief.id, task.id, ["R1"])
    _pass(brief, task, "R1", shot)
    _set_status(task, "done")
    before = briefs.show(brief.id).brief
    events = len(_events("brief_updated"))

    after, changed = briefs.update(
        brief.id,
        title="Login",
        changes={"R1": "R one"},
        checks={"R1": "pytest"},
        assumptions=["a"],
        boundaries=["b"],
    )

    assert not changed
    assert briefs.show(brief.id).brief == before == after
    assert _state(brief, "R1") == "pass"
    assert _status(task).status == "done"
    assert len(_events("brief_updated")) == events
    assert nudges == []


def test_a_correction_that_duplicates_another_requirement_is_refused() -> None:
    brief = _brief("R one", "R two")
    with pytest.raises(ValueError, match="repeats R1"):
        briefs.update(brief.id, changes={"R2": "r ONE"})
    assert briefs.show(brief.id).brief.requirements[1].text == "R two"


def test_a_write_against_a_revision_another_writer_replaced_changes_nothing() -> None:
    brief = _brief()
    with store_session() as store:
        row = store.get_brief(brief.project_id, brief.id)
        assert row is not None
        briefs.update(brief.id, changes={"R1": "changed meanwhile"})
        with pytest.raises(BriefChangedError):
            store.save_brief(
                brief.id, brief.project_id, expected=row.revision, revision=row.revision + 1,
                data=row.data, updated_at=datetime.now(tz=UTC),
                event=briefs._event(brief, briefs._Write("brief_updated", "stale write"), None),
            )  # fmt: skip
    assert briefs.show(brief.id).brief.requirements[0].text == "changed meanwhile"


# --- dropped and vanished duplicates ----------------------------------------------------


def test_a_dropped_or_vanished_duplicate_is_ignored_while_a_live_linked_task_remains(
    shot: Path,
) -> None:
    """Trap: nothing unlinks a task, so one dropped duplicate made its requirement
    unprovable forever."""
    brief = _brief()
    real, duplicate, vanished = _task("real"), _task("duplicate"), _task("vanished")
    for task in (real, duplicate, vanished):
        briefs.link(brief.id, task.id, ["R1"])
    team.drop_task(duplicate.id)
    with store_session() as store:
        store._conn.execute("DELETE FROM team_task WHERE id = ?", (vanished.id,))  # type: ignore[attr-defined]
        store._conn.commit()  # type: ignore[attr-defined]

    _pass(brief, real, "R1", shot)

    view = briefs.show(brief.id)
    assert view.requirements[0].state == "pass"
    assert [proof.task_id for proof in view.requirements[0].tasks] == [real.id]
    team.drop_task(real.id)
    assert _state(brief, "R1") == "unlinked"


# --- --as and the wake-up ---------------------------------------------------------------


def _start(runner: CliRunner, monkeypatch: pytest.MonkeyPatch, session: str, work: Path,
           role: str) -> None:  # fmt: skip
    monkeypatch.setenv("AISQUARE_ROLE", role)
    payload = json.dumps({"cwd": str(work), "session_id": session, "source": "startup"})
    result = runner.invoke(app, ["hook", "session-start"], input=payload)
    monkeypatch.delenv("AISQUARE_ROLE")
    assert result.exit_code == 0, result.output


def test_an_as_write_does_not_wake_its_writer(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work: Path, shot: Path
) -> None:
    """Trap: a manager's own `brief update` reopened its done task, and the event named
    no session, so the manager's next Stop woke it on news it wrote itself. Control: the
    same correction by a coder wakes it."""
    _start(runner, monkeypatch, MANAGER, work, "manager")
    _start(runner, monkeypatch, CODER, work, "coder")
    brief = briefs.create("Login", ["R one"], session_ref=MANAGER)
    task = _task()
    briefs.link(brief.id, task.id, ["R1"], session_ref=MANAGER)

    def settle() -> None:
        _set_status(task, "done")
        with store_session() as store:
            store.touch_session(MANAGER, cursor=store.latest_seq(brief.project_id), state="working")

    settle()
    briefs.update(brief.id, changes={"R1": "R one, fast"}, session_ref=MANAGER)
    assert _status(task).status == "todo", "the correction reopened the done task"
    assert _events("task_reopened")[-1].session_id == MANAGER
    assert team.hook_stop(MANAGER, work) is None, "its own correction must not wake it"

    settle()
    briefs.finding(brief.id, "R1", task_ref=task.id, summary="slow", session_ref=MANAGER)
    assert _status(task).status == "todo"
    assert team.hook_stop(MANAGER, work) is None, "nor its own finding"

    settle()
    briefs.update(brief.id, changes={"R1": "R one, faster"}, session_ref=CODER)
    assert team.hook_stop(MANAGER, work) is not None, "a coder's correction is news"


def test_every_write_is_attributed_to_its_as_session(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work: Path, shot: Path
) -> None:
    _start(runner, monkeypatch, CODER, work, "coder")
    brief = briefs.create("Login", ["R one"], session_ref=CODER)
    task = _task()
    briefs.link(brief.id, task.id, ["R1"], session_ref=CODER)
    briefs.record_evidence(
        brief.id, "R1", task_ref=task.id, verdict="pass", summary="ok", artifact=shot,
        session_ref=CODER,
    )  # fmt: skip
    briefs.finding(brief.id, "R1", task_ref=task.id, summary="no", session_ref=CODER)
    briefs.update(brief.id, add=["R two"], session_ref=CODER)
    kinds = ("brief_created", "brief_linked", "brief_evidence", "brief_finding", "brief_updated")
    for kind in kinds:
        assert [e.session_id for e in _events(kind)] == [CODER], kind
    stored = briefs.show(brief.id).brief
    assert stored.created_by == CODER
    assert {e.session_id for e in stored.evidence} == {CODER}
    assert stored.corrections[-1].session_id == CODER


def test_a_write_as_an_unknown_session_is_refused() -> None:
    brief = _brief()
    with pytest.raises(KeyError):
        briefs.update(brief.id, changes={"R1": "changed"}, session_ref="nobody")
    assert briefs.show(brief.id).brief.requirements[0].text == "Valid login opens dashboard"


# --- the store -------------------------------------------------------------------------

#: #136's table as its builds stamped it at their own v15, with a row in its own shape.
PR136_WORK_BRIEF = """
CREATE TABLE IF NOT EXISTS work_brief (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS work_brief_project ON work_brief(project_id);
INSERT INTO work_brief VALUES ('brief_0123456789ab', 'prj_x', 3, '{"id": "brief_0123456789ab"}');
"""


def test_a_store_a_136_build_stamped_opens_and_works(work: Path) -> None:
    """A #136 build left ``work_brief`` and stamped 15. This build's table has another
    name, so ``IF NOT EXISTS`` cannot keep #136's shape in its place: the store converges
    to the current version, gains ``brief``, keeps #136's row untouched, and works."""
    from aisquare.core.store import SCHEMA_VERSION, _run_step

    db = paths.db_path()
    db.unlink(missing_ok=True)
    raw = sqlite3.connect(str(db))
    try:
        for step in range(14):
            _run_step(raw, step)
        raw.executescript(PR136_WORK_BRIEF)
        raw.execute("PRAGMA user_version = 15")
        raw.commit()
    finally:
        raw.close()
    add_project(work)

    brief = briefs.create("Login", ["R one"])
    task = _task()
    briefs.link(brief.id, task.id, ["R1"])

    raw = sqlite3.connect(str(db))
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert raw.execute("SELECT id, revision FROM work_brief").fetchall() == [
            ("brief_0123456789ab", 3)
        ]
        assert raw.execute("SELECT number, revision FROM brief").fetchall() == [(1, 2)]
    finally:
        raw.close()
    assert briefs.show("B1").requirements[0].tasks[0].task_id == task.id


def test_a_store_that_skipped_the_brief_step_gets_it_back() -> None:
    """Stamped current without the table, as a store another line carried past v34
    would be: the presence pass makes it."""
    from aisquare.core.store import SCHEMA_VERSION

    with store_session():
        pass
    raw = sqlite3.connect(str(paths.db_path()))
    try:
        raw.execute("DROP TABLE brief")
        raw.commit()
    finally:
        raw.close()

    brief = _brief()

    raw = sqlite3.connect(str(paths.db_path()))
    try:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        raw.close()
    assert briefs.show(brief.id).brief.title == "Login"


def test_an_unreadable_brief_costs_its_own_line_not_the_list() -> None:
    good = _brief()
    bad = briefs.create("Damaged", ["x"])
    with store_session() as store:
        store._conn.execute("UPDATE brief SET data = '{}' WHERE id = ?", (bad.id,))  # type: ignore[attr-defined]
        store._conn.commit()  # type: ignore[attr-defined]
    listed, unreadable = briefs.list_briefs()
    assert [b.id for b in listed] == [good.id]
    assert unreadable == 1


# --- the CLI ---------------------------------------------------------------------------


def _cli(runner: CliRunner, *args: str) -> Any:
    return runner.invoke(app, list(args))


def test_the_cli_round_trip(runner: CliRunner, shot: Path) -> None:
    task = _task()
    created = _cli(
        runner, "--json", "brief", "create", "Login", "-r", "Valid login", "-r", "Phone error",
        "--check", "r1=pytest tests/test_login.py", "--boundary", "No SSO",
    )  # fmt: skip
    assert created.exit_code == 0, created.output
    payload = json.loads(created.stdout)
    assert payload["delivered"] is True and payload["brief"]["number"] == 1
    assert payload["brief"]["requirements"][0]["check"] == "pytest tests/test_login.py"

    linked = _cli(runner, "brief", "link", "B1", f"#{task.number}", "-r", "R1", "-r", "R2")
    assert linked.exit_code == 0, linked.output
    assert "linked" in linked.stdout and "R1, R2" in linked.stdout

    again = _cli(runner, "brief", "link", "B1", task.id, "-r", "R1")
    assert again.exit_code == 0 and "unchanged" in again.stdout

    found = _cli(runner, "brief", "finding", "B1", "R2", "--task", task.id, "--summary", "clipped")
    assert found.exit_code == 0, found.output
    assert "finding [manual]" in found.stdout

    updated = _cli(runner, "--json", "brief", "update", "B1", "-r", "R1=Valid login, fast")
    assert json.loads(updated.stdout)["stale"] == ["R1"]

    listed = _cli(runner, "brief", "list")
    assert "B1" in listed.stdout and "Login" in listed.stdout

    shown = _cli(runner, "brief", "show", "B1")
    assert "R1 r2 [missing] Valid login, fast" in shown.stdout
    assert "R2 r1 [fail] Phone error" in shown.stdout
    assert "No SSO" in shown.stdout


def test_the_cli_refuses_bad_input_without_a_traceback(runner: CliRunner, shot: Path) -> None:
    _brief()
    task = _task()
    cases = [
        (["brief", "show", "B9"], 1, "no brief 'B9'"),
        (["brief", "update", "B1"], 1, "nothing to correct"),
        (["brief", "update", "B1", "-r", "R1"], 2, "takes R1=text"),
        (["brief", "update", "B1", "-r", "R7=x"], 1, "no requirement 'R7'"),
        (["brief", "evidence", "B1", "R1", "--task", task.id, "--verdict", "maybe",
          "--summary", "x", "--artifact", str(shot)], 2, "'maybe' is not"),
        (["brief", "evidence", "B1", "R1", "--task", task.id, "--verdict", "pass",
          "--summary", "x", "--artifact", str(shot)], 1, "not linked"),
        (["brief", "link", "B1", "#99", "-r", "R1"], 1, "no task #99"),
        (["brief", "update", "B1", "-r", "R1=x", "--as", "nobody"], 1, "nobody"),
        (["brief", "link", "B1", "tsk_nothing", "-r", "R1", "--as", "nobody"], 1, "'nobody'"),
    ]  # fmt: skip
    for args, code, message in cases:
        result = runner.invoke(app, args)
        assert result.exit_code == code, (args, result.output)
        assert message in result.output, (args, result.output)
        assert result.exception is None or isinstance(result.exception, SystemExit), args


def test_agent_written_text_reaches_the_terminal_without_control_characters(
    runner: CliRunner,
) -> None:
    briefs.create("Login\x1b[2J", ["clear\x1b]0;owned\x07"])
    shown = _cli(runner, "brief", "show", "B1")
    assert shown.exit_code == 0, shown.output
    assert "\x1b" not in shown.stdout and "\x07" not in shown.stdout
    assert "Login" in shown.stdout


def test_a_missing_task_is_named_even_when_the_write_names_a_session(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work: Path
) -> None:
    """The session resolves; the task does not. The refusal names the task, not the
    session that was found."""
    _start(runner, monkeypatch, CODER, work, "coder")
    _brief()
    result = runner.invoke(
        app, ["brief", "link", "B1", "tsk_nothing", "-r", "R1", "--as", CODER[:8]]
    )
    assert result.exit_code == 1, result.output
    assert "'tsk_nothing'" in result.output and CODER[:8] not in result.output
