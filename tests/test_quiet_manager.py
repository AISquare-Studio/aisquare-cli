"""RLX R12 (#145): a manager waiting on its person is not woken by noise.

The owner (2026-11-16): the "board update" wake comes often, which is right when the manager
must act, but it kept coming while the manager only waited on them. R12's replay of
cli-next-release's real board (its manager's turns by origin, from the store's ``metric`` rows)
found 59 of 589 wake turns since 2026-11-03 to be noise, of four kinds, and every other one
needed:

- watch's line about the manager's OWN result or question (``board_result``, ``asked``): the
  person's, in asq and on the phone, never the manager's line;
- ``stale`` for an agent waiting by design (its last act was to the manager, or handed its task
  on to review or done);
- a task's review or done right after its author's note to the manager, already read (the
  biggest: 33, every one a Stop-hook continuation);

and standby, the owner's case: a manager waiting on its person, nothing in flight, is not woken
by watch's routine lines until the person's next prompt. What is urgent still wakes it, so a
working fleet always reaches it.

``FIXTURE`` is that replay's day, 2026-11-16, as event shapes (no text, no ids): each of the
manager's 72 wake turns is rebuilt on a fresh board, through the real board writes and watch's
own filters, and wakes the manager exactly when the replay found it needed.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import harness
from aisquare.core.config import WatchSettings
from aisquare.core.orchestrator import team_project
from aisquare.core.store import store_session
from aisquare.models import FleetAgent, TeamSession
from aisquare.services import fleet as fleet_service
from aisquare.services import team as team_service
from aisquare.services import watch as watch_service
from aisquare.services.watch import WatchItem
from tests.projects import add_project

FIXTURE = Path(__file__).parent / "fixtures" / "r12_replay_2026-11-16.json"
MANAGER = "mgr-r12-0000-0000-0000-000000000001"
SESSIONS = {
    "coder": "cdr-r12-0000-0000-0000-000000000002",
    "tester": "tst-r12-0000-0000-0000-000000000003",
    "reviewer": "rvw-r12-0000-0000-0000-000000000004",
    "validator": "vld-r12-0000-0000-0000-000000000005",
}


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    add_project(work)
    return work


@pytest.fixture(autouse=True)
def no_nudge(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """No tmux here: the doors under test are the store's, which every nudge also reads."""
    calls: list[str] = []

    def record(project_id: str, *, reason: str, board: str | None = None) -> bool:
        calls.append(reason)
        return False

    monkeypatch.setattr(fleet_service, "nudge_manager", record)
    return calls


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _start(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, sid: str, work: Path, role: str
) -> None:
    monkeypatch.setenv("AISQUARE_ROLE", role)
    payload = json.dumps({"cwd": str(work), "session_id": sid, "source": "startup"})
    result = runner.invoke(app, ["hook", "session-start"], input=payload)
    monkeypatch.delenv("AISQUARE_ROLE")
    assert result.exit_code == 0, result.output


@pytest.fixture
def board(runner: CliRunner, monkeypatch: pytest.MonkeyPatch, work_dir: Path) -> str:
    """A manager and one session of each worker role on ``work_dir``'s board; its id."""
    _start(runner, monkeypatch, MANAGER, work_dir, "manager")
    for role, sid in SESSIONS.items():
        _start(runner, monkeypatch, sid, work_dir, role)
    _read_all(team_project(work_dir).id)
    return team_project(work_dir).id


def _manager() -> TeamSession:
    with store_session() as store:
        me = store.get_session(MANAGER)
    assert me is not None
    return me


def _read_all(board_id: str) -> None:
    """The manager has read the board so far: its cursor at the newest event."""
    with store_session() as store:
        store.touch_session(MANAGER, cursor=store.latest_seq(board_id))


def _write(
    board_id: str,
    kind: str,
    *,
    author: str | None,
    to: str | None = None,
    meta: dict[str, object] | None = None,
) -> None:
    with store_session() as store:
        team_service._emit(
            store,
            board_id,
            kind,
            f"{kind} (R12 fixture)",
            session_id=None if author is None else SESSIONS.get(author, author),
            to_role={"manager": "manager", "other": "coder2"}.get(to or ""),
            meta=meta,
        )


def _wakes() -> bool:
    with store_session() as store:
        return team_service.wake_pending(store, _manager())


def _agent(board_id: str, label: str, session: str) -> FleetAgent:
    with store_session() as store:
        return store.upsert_fleet_agent(
            FleetAgent(
                id=f"agt_{label}",
                project_id=board_id,
                label=label,
                role=label.split("-")[0],
                pane_id="%9",
                tmux_socket="r12-nobody",
                session_id=session,
                cwd=Path("/w"),
                created_at=datetime.now(tz=UTC) - timedelta(hours=1),
            )
        )


def _watch_line(board_id: str, items: list[WatchItem]) -> None:
    """What watch's tick does with a project's new items: its filters, then one line."""
    kept = [item for item in items if watch_service.for_the_manager(item)]
    if kept:
        urgent = any(watch_service._urgent(item) for item in kept)
        team_service.watch_event(board_id, f"watch: {len(kept)} new", urgent=urgent)


def _item(kind: str, **detail: Any) -> WatchItem:
    """A watch item with its look's id, as watch makes them: a need's, a stale agent's, freed
    capacity's, or a pull request's (``<pr>@<project>``)."""
    if kind.startswith("pr_"):
        return WatchItem(f"{kind}:org/repo#7@abc1234:1@p", "p", "p", kind, kind, detail)
    prefix = {"stale": "stale:", "capacity_freed": "capacity:"}.get(kind, "needs:")
    return WatchItem(f"{prefix}{kind}:{len(detail)}", "p", "p", kind, f"{kind} item", detail)


# --- the replay: every turn of 2026-11-16 ---------------------------------------------------

REPLAY = json.loads(FIXTURE.read_text())["turns"]


def test_the_replay_is_the_days_72_wake_turns_57_needed() -> None:
    assert len(REPLAY) == 72
    assert sum(turn["verdict"] == "NEEDED" for turn in REPLAY) == 57
    assert not re.search(r"[0-9a-f]{8}-|prj_|agt_", FIXTURE.read_text()), "shapes, no ids"


@pytest.mark.parametrize(
    "turn", REPLAY, ids=[f"{n:02d}-{t['origin']}-{t['verdict']}" for n, t in enumerate(REPLAY)]
)
def test_each_replayed_turn_wakes_the_manager_exactly_when_it_was_needed(
    turn: dict[str, Any], board: str
) -> None:
    turn = copy.deepcopy(turn)
    history: list[tuple[str, dict[str, Any]]] = []
    for n, event in enumerate(turn["events"]):
        if event.get("echo"):
            history.append(("told", event))
        for item in event.get("watch", []):
            if item["kind"] == "stale" and item.get("last_act"):
                label = f"coder-quiet{n}"
                sid = f"q{n}-r12-0000-0000-0000-000000000009"
                _agent(board, label, sid)
                _write(board, item["last_act"]["kind"], author=sid, to=item["last_act"]["to"])
                item["agent_id"] = f"agt_{label}"
    for _, event in history:
        _write(board, "note", author=event["author"], to="manager")
    _read_all(board)  # what came before the turn's window, the manager has read

    for event in turn["events"]:
        if event["kind"] == "watch":
            items = []
            for item in event["watch"]:
                if item["kind"] == "stale":
                    if item.get("quiet") and watch_service._idle_by_design(item.get("agent_id")):
                        continue
                    items.append(_item("stale", agent="coder-x", quiet=item.get("quiet", False)))
                elif item["kind"] in ("asked", "board_result", "board_question"):
                    who = "manager" if item.get("about_manager") else "coder-x"
                    items.append(_item(item["kind"], agent=who))
                else:
                    items.append(_item(item["kind"], agent="tester-x"))
            _watch_line(board, items)
        else:
            _write(
                board, event["kind"], author=event["author"], to=event["to"], meta=event.get("meta")
            )

    assert _wakes() is (turn["verdict"] == "NEEDED"), turn


# --- what wakes a manager, what does not ------------------------------------------------------

NEEDED = [
    pytest.param("result", "coder", None, None, id="a teammate's result"),
    pytest.param("question", "coder", None, None, id="a teammate's question"),
    pytest.param("task_done", "tester", None, None, id="a task done"),
    pytest.param("task_review", "coder", None, None, id="a task to review"),
    pytest.param("task_reopened", "tester", None, None, id="a task reopened"),
    pytest.param("task_blocked", "coder", None, None, id="a task blocked"),
    pytest.param("limited", "coder", None, None, id="a limit"),
    pytest.param("handover_failed", "coder", None, None, id="a hand-over that failed"),
    pytest.param("prompt_unsent", "coder", None, None, id="a first prompt not taken"),
    pytest.param("note", "coder", "manager", None, id="a teammate's note to the manager"),
    pytest.param("note", None, "manager", None, id="a note with no session (a board, the person)"),
    pytest.param("agent_exited", "coder", None, {"status": 1, "released": 0}, id="a failed exit"),
    pytest.param(
        "agent_exited", "coder", None, {"status": 0, "released": 1}, id="an orphaned task"
    ),
]


@pytest.mark.parametrize(("kind", "author", "to", "meta"), NEEDED)
@pytest.mark.parametrize("standby", [False, True], ids=["awake", "in standby"])
def test_what_a_manager_must_act_on_wakes_it_in_standby_too(
    kind: str, author: str | None, to: str | None, meta: Any, standby: bool, board: str
) -> None:
    """The break-through list: standby holds watch's routine lines and nothing on it."""
    if standby:
        with store_session() as store:
            team_service.begin_standby(store, _manager(), why="explicit")
    _write(board, kind, author=author, to=to, meta=meta)
    assert _wakes()


@pytest.mark.parametrize(
    "items",
    [
        pytest.param([_item("permission", agent="tester-x")], id="a permission dialog"),
        pytest.param([_item("asked", agent="coder-x")], id="a coder that asked in its pane"),
        pytest.param([_item("stale", agent="coder-x", quiet=False)], id="a lost agent"),
    ],
)
def test_watchs_urgent_lines_wake_a_manager_in_standby(items: list[WatchItem], board: str) -> None:
    with store_session() as store:
        team_service.begin_standby(store, _manager(), why="explicit")
    _watch_line(board, items)
    assert _wakes()


def test_watchs_routine_lines_wake_an_awake_manager_and_not_one_in_standby(board: str) -> None:
    quiet = [_item("stale", agent="coder-x", quiet=True)]
    _watch_line(board, quiet)
    assert _wakes(), "awake, an agent gone quiet is news"
    _read_all(board)
    with store_session() as store:
        team_service.begin_standby(store, _manager(), why="explicit")
    for routine in (quiet, [_item("capacity_freed")], [_item("pr_approved")]):
        _watch_line(board, routine)
    assert not _wakes(), "in standby, the routine lines wait for its person's next prompt"


def test_the_managers_own_result_and_question_do_not_wake_it(board: str) -> None:
    for kind in ("result", "question"):
        _write(board, kind, author=MANAGER)
    assert not _wakes()


@pytest.mark.parametrize("kind", ["task_review", "task_done"])
def test_a_task_move_after_its_authors_note_already_read_is_no_second_wake(
    kind: str, board: str
) -> None:
    _write(board, "note", author="coder", to="manager")
    assert _wakes(), "the note wakes it"
    _read_all(board)
    _write(board, kind, author="coder")
    assert not _wakes(), "the review repeats the note it read"


def test_a_task_move_and_its_note_in_one_delta_are_one_wake(board: str) -> None:
    _write(board, "note", author="coder", to="manager")
    _write(board, "task_review", author="coder")
    assert _wakes()


def test_a_task_move_long_after_a_note_or_from_another_author_still_wakes(board: str) -> None:
    _write(board, "note", author="coder", to="manager")
    _read_all(board)
    _write(board, "task_review", author="tester")  # another author: not an echo
    assert _wakes()


@pytest.mark.parametrize(
    ("kind", "about_manager", "kept"),
    [
        ("board_result", True, False),
        ("board_result", False, False),
        ("board_question", True, False),
        ("board_question", False, False),
        ("asked", True, False),
        ("asked", False, True),
        ("permission", False, True),
    ],
)
def test_watch_leaves_the_persons_items_off_the_managers_line(
    kind: str, about_manager: bool, kept: bool
) -> None:
    agent = "manager" if about_manager else "coder-x"
    assert watch_service.for_the_manager(_item(kind, agent=agent)) is kept


@pytest.mark.parametrize(
    ("kind", "to", "by_design"),
    [
        ("note", "manager", True),
        ("result", None, True),
        ("question", None, True),
        ("task_review", None, True),
        ("task_done", None, True),
        ("note", None, False),
        ("task_claimed", None, False),
    ],
)
def test_an_agent_quiet_after_handing_its_work_on_is_idle_by_design(
    kind: str, to: str | None, by_design: bool, board: str
) -> None:
    agent = _agent(board, "coder-quiet", "qz-r12-0000-0000-0000-000000000009")
    _write(board, kind, author=agent.session_id, to=to)
    assert watch_service._idle_by_design(agent.id) is by_design


def test_an_agent_with_no_board_act_is_stale_news(board: str) -> None:
    agent = _agent(board, "coder-quiet", "qz-r12-0000-0000-0000-000000000009")
    assert watch_service._idle_by_design(agent.id) is False
    assert watch_service._idle_by_design(None) is False


# --- standby ------------------------------------------------------------------------------------


def _standby() -> team_service.Standby | None:
    with store_session() as store:
        return team_service.standby_of(store, _manager())


def _task(runner: CliRunner, status: str) -> None:
    """A task on the board, moved to ``status`` by the coder."""
    added = runner.invoke(app, ["--json", "task", "add", "the work", "--as", MANAGER[:8]])
    assert added.exit_code == 0, added.output
    task_id = json.loads(added.output)["id"]
    if status in ("doing", "review"):
        claimed = runner.invoke(app, ["task", "claim", task_id, "--as", SESSIONS["coder"][:8]])
        assert claimed.exit_code == 0, claimed.output
    if status == "review":
        moved = runner.invoke(app, ["task", "review", task_id, "--as", SESSIONS["coder"][:8]])
        assert moved.exit_code == 0, moved.output


def test_team_standby_holds_until_the_person_speaks(
    runner: CliRunner, board: str, work_dir: Path
) -> None:
    said = runner.invoke(app, ["team", "standby", "--as", MANAGER[:8]])
    assert said.exit_code == 0, said.output
    assert "on standby" in said.output and "still do" in said.output
    assert _standby() is not None and _standby().why == "explicit"  # type: ignore[union-attr]
    payload = json.dumps({"cwd": str(work_dir), "session_id": MANAGER, "prompt": "go on"})
    assert runner.invoke(app, ["hook", "user-prompt-submit"], input=payload).exit_code == 0
    assert _standby() is None, "its person's prompt ends it"


def test_team_standby_off_ends_it(runner: CliRunner, board: str) -> None:
    assert runner.invoke(app, ["team", "standby", "--as", MANAGER[:8]]).exit_code == 0
    off = runner.invoke(app, ["team", "standby", "--off", "--as", MANAGER[:8]])
    assert off.exit_code == 0 and "standby ended" in off.output
    assert _standby() is None


def test_standby_is_a_managers_alone(runner: CliRunner, board: str) -> None:
    said = runner.invoke(app, ["--json", "team", "standby", "--as", SESSIONS["coder"][:8]])
    assert said.exit_code != 0 and "standby_refused" in said.output


@pytest.mark.parametrize("status", ["doing", "review"])
def test_standby_is_refused_while_work_is_in_flight(
    status: str, runner: CliRunner, board: str
) -> None:
    _task(runner, status)
    said = runner.invoke(app, ["--json", "team", "standby", "--as", MANAGER[:8]])
    assert said.exit_code != 0 and "standby_refused" in said.output


def test_standby_never_strands_a_working_fleet(runner: CliRunner, board: str) -> None:
    """Work coming back ends standby for good, and while a task is in flight nothing is held:
    a fleet at work always reaches its manager."""
    _task(runner, "todo")
    assert runner.invoke(app, ["team", "standby", "--as", MANAGER[:8]]).exit_code == 0
    _read_all(board)
    _watch_line(board, [_item("stale", agent="coder-x", quiet=True)])
    assert not _wakes(), "held"
    with store_session() as store:
        task = store.team_tasks(board)[0]
    claimed = runner.invoke(app, ["task", "claim", task.id, "--as", SESSIONS["coder"][:8]])
    assert claimed.exit_code == 0, claimed.output
    assert _standby() is None, "work came back: standby is over"
    with store_session() as store:
        assert store.get_meta(team_service.STANDBY_META + MANAGER) is None
    assert _wakes(), "the line it held wakes it now"


def _asked(board: str, agent: str) -> None:
    """One watch tick whose look found ``agent``'s turn ended on a question (``asked``)."""
    watcher = watch_service.Watcher(
        WatchSettings(auto_stop=False), needs_sources=lambda: [], stale=False
    )
    item = WatchItem(f"needs:asked:{agent}", board, "p", "asked", "asked", {"agent": agent})
    watcher.needs = lambda now: [item]  # type: ignore[method-assign]
    watcher.tick(datetime.now(tz=UTC))


def test_a_manager_that_asks_its_person_with_nothing_in_flight_goes_into_standby(
    runner: CliRunner, board: str
) -> None:
    """Watch's look decides it: the manager's turn ended on a question to its person (the
    phone's ``asked``), and nothing is in flight. The Stop hook stays off the remote code."""
    _agent(board, "manager", MANAGER)
    _asked(board, "manager")
    standby = _standby()
    assert standby is not None and standby.why == "asked"


def test_a_coders_question_or_work_in_flight_puts_no_manager_in_standby(
    runner: CliRunner, board: str
) -> None:
    _agent(board, "manager", MANAGER)
    _asked(board, "coder-x")
    assert _standby() is None, "a coder's question is the manager's to answer"
    _task(runner, "doing")
    _asked(board, "manager")
    assert _standby() is None, "a task in flight: the fleet is at work"


def test_the_manager_cycle_names_standby() -> None:
    cycle = "\n".join(
        harness.role_cycle("manager", "abcd1234", fleet=lambda verb: f"aisquare fleet {verb}")
    )
    assert "aisquare team standby --as abcd1234" in cycle
    assert "urgent news still does" in cycle
