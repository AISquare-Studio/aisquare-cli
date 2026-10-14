"""Per-project task numbers (``#12``): what a person reads and types instead of ``tsk_…``.

The human: "is there any reason for the board to have id to be such random values? can it
not be sequential numbers … since its unique to each project? … does not mean order just an
id". So every task gets 1, 2, … on its own board, given by the store at insert; every task
command takes ``12`` or ``#12``; the board, the lists and the receipts say ``#12`` beside the
id agents keep working with.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import paths
from aisquare.core.ids import new_task_id
from aisquare.core.orchestrator import team_project
from aisquare.core.store import open_store, store_session
from aisquare.models import ProjectInfo, TeamTask
from aisquare.services import mcp_server
from aisquare.services import team as team_service

CODER = "bbbb2222-0000-0000-0000-000000000000"


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


@pytest.fixture
def other_dir(tmp_path: Path) -> Path:
    other = tmp_path / "other"
    other.mkdir()
    return other


def _task(project_id: str, key: str, *, created_at: datetime | None = None) -> TeamTask:
    now = created_at or datetime.now(tz=UTC)
    return TeamTask(
        id=new_task_id(), project_id=project_id, key=key, title=key, created_at=now, updated_at=now
    )


def _board(root: Path) -> ProjectInfo:
    project = team_project(root)
    with store_session() as store:
        store.ensure_project(project)
    return project


def _add(project: ProjectInfo, key: str) -> TeamTask:
    with store_session() as store:
        task, _ = store.upsert_task(_task(project.id, key))
    return task


def _number(task: TeamTask | None) -> int | None:
    assert task is not None
    return task.number


def _start(runner: CliRunner, session_id: str, work: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A coder session on ``work``'s board (a role joins it: test_team.py's ``_start``)."""
    monkeypatch.setenv("AISQUARE_ROLE", "coder")
    payload = json.dumps({"cwd": str(work), "session_id": session_id, "source": "startup"})
    assert runner.invoke(app, ["hook", "session-start"], input=payload).exit_code == 0
    monkeypatch.delenv("AISQUARE_ROLE")


# --- allocation ---------------------------------------------------------------------------


def test_each_board_numbers_its_tasks_from_one_and_a_re_add_takes_no_number(
    work_dir: Path, other_dir: Path
) -> None:
    here, there = _board(work_dir), _board(other_dir)
    numbers = [
        (_add(here, "a").number, _add(there, "x").number),
        (_add(here, "b").number, _add(there, "y").number),
    ]
    assert numbers == [(1, 1), (2, 2)]  # per board, not one counter for the machine
    with store_session() as store:
        again, created = store.upsert_task(_task(here.id, "a"))  # idempotent: the original
    assert not created and again.number == 1
    assert _add(here, "c").number == 3  # the re-add consumed nothing
    with store_session() as store:
        assert [t.number for t in store.team_tasks(here.id)] == [1, 2, 3]
        assert store.task_numbers(there.id) == {t.id: t.number for t in store.team_tasks(there.id)}


def test_adds_racing_on_two_boards_never_share_or_skip_a_number(
    work_dir: Path, other_dir: Path
) -> None:
    here, there = _board(work_dir), _board(other_dir)
    barrier = threading.Barrier(12)
    errors: list[BaseException] = []

    def add(project: ProjectInfo, index: int) -> None:
        try:
            barrier.wait()
            with store_session() as store:
                store.upsert_task(_task(project.id, f"t{index}"))
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=add, args=(here if i % 2 else there, i)) for i in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    with store_session() as store:
        for project in (here, there):
            assert sorted(t.number or 0 for t in store.team_tasks(project.id)) == list(range(1, 7))


def test_a_number_is_unique_on_its_board_by_the_database_itself(work_dir: Path) -> None:
    here = _board(work_dir)
    first = _add(here, "a")
    _add(here, "b")
    raw = sqlite3.connect(str(paths.db_path()))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute(
                "UPDATE team_task SET number = ? WHERE key = 'b' AND project_id = ?",
                (first.number, here.id),
            )
    finally:
        raw.close()


def test_a_store_from_before_numbers_gets_them_per_board_in_the_order_tasks_were_added(
    work_dir: Path, other_dir: Path
) -> None:
    """v29's backfill. The ids sort one way and the creation times another, so the test
    sees which one numbers: the order the tasks were ADDED, per project."""
    here, there = _board(work_dir), _board(other_dir)
    start = datetime(2026, 10, 1, tzinfo=UTC)
    added = {}
    with store_session() as store:
        for key, project, minutes in (
            ("late", here, 30),
            ("early", here, 10),
            ("middle", here, 20),
            ("elsewhere", there, 5),
        ):
            task, _ = store.upsert_task(
                _task(project.id, key, created_at=start + timedelta(minutes=minutes))
            )
            added[key] = task.id
    raw = sqlite3.connect(str(paths.db_path()))
    try:
        raw.executescript(  # the store as v28 left it: no number, no index
            "DROP INDEX team_task_number; ALTER TABLE team_task DROP COLUMN number; "
            "PRAGMA user_version = 28;"
        )
    finally:
        raw.close()

    open_store().close()  # the ladder: v29 adds and backfills, v30 indexes

    with store_session() as store:
        numbered = {key: _number(store.get_task(task_id)) for key, task_id in added.items()}
        fresh, _ = store.upsert_task(_task(here.id, "next"))
    assert numbered == {"early": 1, "middle": 2, "late": 3, "elsewhere": 1}
    assert fresh.number == 4
    open_store().close()  # opened again: nothing renumbered
    with store_session() as store:
        assert _number(store.get_task(added["late"])) == 3


# --- resolution -------------------------------------------------------------------------


def test_a_number_names_the_task_on_the_callers_board_first(
    work_dir: Path, other_dir: Path, runner: CliRunner
) -> None:
    here, there = _board(work_dir), _board(other_dir)
    mine = [_add(here, key) for key in ("a", "b", "c")]
    theirs = [_add(there, key) for key in ("x", "y")]

    with store_session() as store:
        assert team_service.find_task(store, "2", here.id) == mine[1]
        assert team_service.find_task(store, "#2", here.id) == mine[1]
        assert team_service.find_task(store, " #3 ", here.id) == mine[2]
        assert team_service.find_task(store, "2", there.id) == theirs[1]  # per board
        # `#9` can only be a number, and this board has none: said, never looked for.
        with pytest.raises(team_service.NoSuchTaskNumber, match=r"no task #9 on this board"):
            team_service.find_task(store, "#9", here.id)
        # A full id, a prefix and an id tail still resolve as they always did.
        assert team_service.find_task(store, mine[0].id, here.id) == mine[0]
        assert (
            team_service.find_task(store, mine[0].id[:22], here.id) == mine[0]
        )  # into the random part
        assert team_service.find_task(store, theirs[0].id[-8:], here.id) == theirs[0]

    shown = runner.invoke(app, ["--json", "task", "show", "2"])  # cwd is `here`
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.stdout)["id"] == mine[1].id
    assert json.loads(shown.stdout)["number"] == 2
    missing = runner.invoke(app, ["--json", "task", "show", "#7"])
    assert missing.exit_code == 1
    assert json.loads(missing.stdout) == {"error": "not_found", "ref": "#7"}
    said = runner.invoke(app, ["task", "show", "#7"])
    assert said.exit_code == 1
    assert f"no task #7 on this board ({work_dir.name})" in said.stderr


def test_a_bare_number_the_board_lacks_falls_back_to_an_id_tail(work_dir: Path) -> None:
    """Today's behaviour, kept for a number the board does not have: board lines showed id
    tails, and a tail of digits typed from one must still find its task."""
    here = _board(work_dir)
    now = datetime.now(tz=UTC)
    tailed = TeamTask(
        id="tsk_0000000000aaaaaaaa77777777",
        project_id=here.id,
        key="tailed",
        title="tailed",
        created_at=now,
        updated_at=now,
    )
    with store_session() as store:
        store.upsert_task(tailed)  # #1
        assert team_service.find_task(store, "1", here.id) == store.get_task(tailed.id)
        assert team_service.find_task(store, "77777777", here.id) == store.get_task(tailed.id)
        with pytest.raises(team_service.NoSuchTaskNumber):
            team_service.find_task(store, "#77", here.id)  # `#` is a number, or nothing


def test_every_task_command_takes_a_number_and_says_it(
    work_dir: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _start(runner, CODER, work_dir, monkeypatch)
    first = runner.invoke(app, ["task", "add", "wire auth", "--as", CODER[:8]])
    assert first.exit_code == 0, first.output
    assert "✓ added: #1 tsk_" in first.stdout
    second = runner.invoke(app, ["task", "add", "ship it", "--needs", "#1", "--as", CODER[:8]])
    assert second.exit_code == 0, second.output
    with store_session() as store:
        here = team_project(work_dir).id
        one, two = store.task_by_number(here, 1), store.task_by_number(here, 2)
    assert one is not None and two is not None and two.needs == [one.id]

    claimed = runner.invoke(app, ["task", "claim", "1", "--as", CODER[:8]])
    assert claimed.exit_code == 0, claimed.output
    assert f"✓ claimed: #1 {one.id} [doing] wire auth" in claimed.stdout
    for verb, extra in (("review", ["--note", "see diff"]), ("done", [])):
        moved = runner.invoke(app, ["task", verb, "#1", *extra, "--as", CODER[:8]])
        assert moved.exit_code == 0, moved.output
        assert f"#1 {one.id}" in moved.stdout
    noted = runner.invoke(app, ["note", "picked up", "--task", "2", "--as", CODER[:8]])
    assert noted.exit_code == 0, noted.output
    for verb, extra in (("block", ["--reason", "waiting"]), ("reopen", ["--reason", "again"])):
        moved = runner.invoke(app, ["task", verb, "2", *extra, "--as", CODER[:8]])
        assert moved.exit_code == 0, moved.output
    dropped = runner.invoke(app, ["--json", "task", "drop", "#2", "--as", CODER[:8]])
    assert dropped.exit_code == 0 and json.loads(dropped.stdout)["number"] == 2
    refused = runner.invoke(app, ["task", "claim", "#3", "--as", CODER[:8]])
    assert refused.exit_code == 1 and "✗ no task #3 on this board" in refused.stderr

    listed = runner.invoke(app, ["task", "list"], env={"COLUMNS": "200"})
    assert listed.exit_code == 0
    rows = [line for line in listed.stdout.splitlines() if "tsk_" in line]
    assert [row.index(f"#{n}") < row.index("tsk_") for n, row in enumerate(rows, 1)] == [
        True,
        True,
    ]  # the number, then the id, on each row
    shown = runner.invoke(app, ["task", "show", "#1"])
    assert "number" in shown.stdout and "#1" in shown.stdout


def test_the_board_and_its_event_lines_say_the_number_beside_the_id(
    work_dir: Path, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _start(runner, CODER, work_dir, monkeypatch)
    assert runner.invoke(app, ["task", "add", "first", "--as", CODER[:8]]).exit_code == 0
    assert (
        runner.invoke(app, ["task", "add", "second", "--needs", "1", "--as", CODER[:8]]).exit_code
        == 0
    )
    with store_session() as store:
        here = team_project(work_dir).id
        one, two = store.task_by_number(here, 1), store.task_by_number(here, 2)
    assert one is not None and two is not None
    board = runner.invoke(app, ["board"])
    assert board.exit_code == 0, board.output
    assert f"  - #1 {one.id} [todo] first" in board.stdout
    assert f"  - #2 {two.id} [todo] second ⧗ waits on #1" in board.stdout
    assert f"[#1 {one.id}] first" in board.stdout  # the task_added event line

    _, sessions, _, events = team_service.board_data(work_dir)
    roles = {session.id: session.role for session in sessions}
    added = next(event for event in events if event.task_id == one.id)
    assert team_service.event_line(added, roles).endswith(f"[{one.id}] first")  # no numbers given
    assert team_service.event_line(added, roles, {one.id: 1}).endswith(f"[#1 {one.id}] first")


def test_the_mcp_task_tools_take_and_say_a_number(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    _start(runner, CODER, work_dir, monkeypatch)
    monkeypatch.setattr(mcp_server, "_ensure_virtual_session", lambda: CODER)
    added = mcp_server.task_add("wire auth")
    assert added.startswith("created: #1 tsk_")
    moved = mcp_server.task_update("#1", "review", note="see diff")
    assert moved.startswith("#1 tsk_") and moved.endswith("is now review")
    with pytest.raises(Exception, match=r"no task #4 on this board"):
        mcp_server.task_update("#4", "done")


def test_fleet_spawn_for_a_task_takes_its_number_on_that_board(
    work_dir: Path, other_dir: Path
) -> None:
    from aisquare.services import fleet as fleet_service

    here, there = _board(work_dir), _board(other_dir)
    mine = _add(here, "a")
    _add(there, "x")
    with store_session() as store:
        assert fleet_service._task_for(store, here, "1") == mine
        assert fleet_service._task_for(store, here, "#1") == mine
        with pytest.raises(fleet_service.FleetError, match=r"no task #2 on this board"):
            fleet_service._task_for(store, here, "#2")
