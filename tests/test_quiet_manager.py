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
from aisquare.models import FleetAgent, TeamEvent, TeamSession, TeamTask
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
    history: list[tuple[int, dict[str, Any]]] = []
    echoed: dict[int, TeamTask] = {}
    for n, event in enumerate(turn["events"]):
        if event.get("echo"):
            echoed[n] = _real_task(board, f"echoed {n}")
            history.append((n, event))
        for item in event.get("watch", []):
            if item["kind"] == "stale" and item.get("last_act"):
                label = f"coder-quiet{n}"
                sid = f"q{n}-r12-0000-0000-0000-000000000009"
                _agent(board, label, sid)
                _write(board, item["last_act"]["kind"], author=sid, to=item["last_act"]["to"])
                item["agent_id"] = f"agt_{label}"
    for n, event in history:
        # As the day's notes were written: "DONE #N …", the task named in the text and filed
        # with no --task (49 of the 70 such pairs on that board; 20 more carried --task).
        text = f"DONE #{echoed[n].number}, moving to review"
        _say(board, "note", by=event["author"], to="manager", text=text)
    _read_all(board)  # what came before the turn's window, the manager has read

    for n, event in enumerate(turn["events"]):
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
        elif n in echoed:
            _say(board, event["kind"], by=event["author"], task=echoed[n].id)
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
    task = _real_task(board)
    _say(board, "note", by="coder", to="manager", text=f"DONE #{task.number}, to review")
    assert _wakes(), "the note wakes it"
    _read_all(board)
    _say(board, kind, by="coder", task=task.id)
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


# --- #145's reopen (tester 12612): the echo and idle by design, narrowed ----------------------


def _say(
    board_id: str,
    kind: str,
    *,
    by: str | None,
    to: str | None = None,
    task: str | None = None,
    text: str | None = None,
) -> TeamEvent:
    """One board write by session ``by`` (a role of :data:`SESSIONS`, or a session id), addressed
    to ``to`` as typed and about ``task``: the event."""
    with store_session() as store:
        return team_service._emit(
            store,
            board_id,
            kind,
            text or f"{kind} (#145 reopen)",
            session_id=None if by is None else SESSIONS.get(by, by),
            to_role=to,
            task_id=task,
        )


def _real_task(board_id: str, title: str = "the work") -> TeamTask:
    """A task on the board, with the number the store gives it."""
    now = datetime.now(tz=UTC)
    with store_session() as store:
        task, _ = store.upsert_task(
            TeamTask(
                id=f"tsk_{len(store.team_tasks(board_id)) + 1:026d}",
                project_id=board_id,
                key="-".join(title.split()),
                title=title,
                created_at=now,
                updated_at=now,
            )
        )
    assert task.number is not None
    return task


def _echo(event: TeamEvent) -> bool:
    with store_session() as store:
        return team_service.is_echo(store, event, _manager())


def test_the_echo_still_holds_for_a_done_note_then_its_review(board: str) -> None:
    """The replay's 33: the coder's note to the manager, read, then its review of that task."""
    _say(board, "note", by="coder", to="manager", task="tsk_a")
    _read_all(board)
    assert _echo(_say(board, "task_review", by="coder", task="tsk_a"))
    assert not _wakes()


@pytest.mark.parametrize("kind", ["note", "decision", "result"])
def test_each_word_to_the_manager_makes_its_authors_next_move_an_echo(
    kind: str, board: str
) -> None:
    _say(board, kind, by="tester", to="manager" if kind != "result" else None, task="tsk_a")
    _read_all(board)
    assert _echo(_say(board, "task_done", by="tester", task="tsk_a"))


def test_a_second_rounds_review_after_a_reopen_wakes(board: str) -> None:
    """CE1.4: DONE note, review, reopened, fixed, reviewed again with no new note. The last
    act before round 2's review is round 1's review, and a reopen came between: news."""
    _say(board, "note", by="coder", to="manager")
    _say(board, "task_review", by="coder", task="tsk_a")
    _read_all(board)
    _say(board, "task_reopened", by="tester", task="tsk_a")
    _read_all(board)
    assert not _echo(_say(board, "task_review", by="coder", task="tsk_a"))
    assert _wakes(), "the manager must brief a tester for round 2"


def test_a_reopen_between_a_note_and_the_review_makes_the_review_news(board: str) -> None:
    _say(board, "note", by="coder", to="manager", task="tsk_a")
    _say(board, "task_reopened", by="tester", task="tsk_a")
    _read_all(board)
    assert not _echo(_say(board, "task_review", by="coder", task="tsk_a"))


def test_a_second_tasks_review_wakes(board: str) -> None:
    """CE1.5: the same coder's review of another task, after its first one's note and review."""
    _say(board, "note", by="coder", to="manager", task="tsk_a")
    _say(board, "task_review", by="coder", task="tsk_a")
    _read_all(board)
    assert not _echo(_say(board, "task_review", by="coder", task="tsk_b"))
    assert _wakes()


def test_a_note_about_another_task_is_no_echo(board: str) -> None:
    _say(board, "note", by="coder", to="manager", task="tsk_a")
    _read_all(board)
    assert not _echo(_say(board, "task_review", by="coder", task="tsk_b"))


@pytest.mark.parametrize("answered", [False, True], ids=["unanswered", "answered"])
def test_a_verdict_after_its_testers_question_wakes(answered: bool, board: str) -> None:
    """CE1.7: a question asks, about that very task; the answer to it is no news of the
    verdict that follows."""
    task = _real_task(board)
    text = f"#{task.number}: is the error page in scope?"
    _say(board, "question", by="tester", to="manager", task=task.id, text=text)
    if answered:
        _say(board, "note", by=MANAGER, to="tester")
    _read_all(board)
    assert not _echo(_say(board, "task_done", by="tester", task=task.id))
    assert _wakes()


@pytest.mark.parametrize("to", ["coder", "CODER", "coder-auth"])
def test_a_word_to_the_author_between_its_note_and_its_move_makes_the_move_news(
    to: str, board: str
) -> None:
    """Told "also do X" after its DONE note, the coder's review says X is done: news. Its role,
    any case, or its fleet row's label, as turn-end delivery reads an address."""
    _agent(board, "coder-auth", SESSIONS["coder"])
    _say(board, "note", by="coder", to="manager", task="tsk_a")
    _read_all(board)
    _say(board, "note", by=MANAGER, to=to)
    _read_all(board)
    assert not _echo(_say(board, "task_review", by="coder", task="tsk_a"))


def test_a_word_to_someone_else_between_them_keeps_the_echo(board: str) -> None:
    _say(board, "note", by="coder", to="manager", task="tsk_a")
    _say(board, "note", by=MANAGER, to="tester")
    _say(board, "task_reopened", by="tester", task="tsk_other")
    _read_all(board)
    assert _echo(_say(board, "task_review", by="coder", task="tsk_a"))


def test_a_note_to_another_role_is_no_echo(board: str) -> None:
    """Told the tester, not the manager: the review is the manager's first word of it."""
    _say(board, "note", by="coder", to="tester", task="tsk_a")
    _read_all(board)
    assert not _echo(_say(board, "task_review", by="coder", task="tsk_a"))
    assert _wakes()


def test_an_unread_note_makes_no_echo(board: str) -> None:
    _say(board, "note", by="coder", to="manager", task="tsk_a")
    assert not _echo(_say(board, "task_review", by="coder", task="tsk_a"))


def test_what_hooks_file_under_the_author_does_not_break_the_echo(board: str) -> None:
    """Its last BOARD act counts: an attention line or an exit filed under its session is not
    something it said."""
    _say(board, "note", by="coder", to="manager", task="tsk_a")
    _say(board, "attention", by="coder")
    _read_all(board)
    assert _echo(_say(board, "task_review", by="coder", task="tsk_a"))


@pytest.mark.parametrize(("minutes", "echo"), [(29, True), (31, False)])
def test_the_echo_looks_back_thirty_minutes(
    minutes: int, echo: bool, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert timedelta(minutes=30) == team_service.ECHO_WINDOW
    now = datetime.now(tz=UTC)
    monkeypatch.setattr(team_service, "_now", lambda: now - timedelta(minutes=minutes))
    _say(board, "note", by="coder", to="manager", task="tsk_a")
    _read_all(board)
    monkeypatch.setattr(team_service, "_now", lambda: now)
    assert _echo(_say(board, "task_review", by="coder", task="tsk_a")) is echo


def test_a_re_review_after_a_reopen_wakes_through_the_cli(runner: CliRunner, board: str) -> None:
    """CE1.4 through the real writes: the board's commands, not ``_emit``."""

    def run(*args: str) -> str:
        done = runner.invoke(app, ["--json", *args])
        assert done.exit_code == 0, done.output
        return done.output

    added = json.loads(run("task", "add", "the work", "--as", MANAGER[:8]))
    task_id, number = added["id"], added["number"]
    coder, tester = SESSIONS["coder"][:8], SESSIONS["tester"][:8]
    run("task", "claim", task_id, "--as", coder)
    run("note", f"DONE #{number}, moving to review", "--to", "manager", "--as", coder)
    _read_all(board)
    run("task", "review", task_id, "--as", coder)
    assert not _wakes(), "round 1's review repeats the DONE note it read"
    _read_all(board)
    run("task", "reopen", task_id, "--reason", "the state check is missing", "--as", tester)
    _read_all(board)
    with store_session() as store:
        if store.get_task(task_id).claimed_by is None:  # type: ignore[union-attr]
            run("task", "claim", task_id, "--as", coder)
    _read_all(board)
    run("task", "review", task_id, "--as", coder)
    assert _wakes(), "round 2 is ready: the manager must hear it"


# idle by design, answered


@pytest.mark.parametrize("act", ["note", "result", "question", "task_review"])
@pytest.mark.parametrize("to", ["coder-quiet", "coder", "Coder-Quiet"])
def test_an_agent_answered_after_its_act_is_stale_news_again(act: str, to: str, board: str) -> None:
    """CE2: the manager answered (``note --to coder-20 GO``), and a note to a waiting agent waits
    for a turn end that never comes. Its quiet is no longer by design."""
    agent = _agent(board, "coder-quiet", "qz-r12-0000-0000-0000-000000000009")
    _say(board, act, by=agent.session_id, to="manager" if act == "note" else None, task="tsk_q")
    assert watch_service._idle_by_design(agent.id) is True
    _say(board, "note", by=MANAGER, to=to)
    assert watch_service._idle_by_design(agent.id) is False


def test_an_agent_whose_reviewed_task_was_reopened_is_stale_news_again(board: str) -> None:
    agent = _agent(board, "coder-quiet", "qz-r12-0000-0000-0000-000000000009")
    _say(board, "task_review", by=agent.session_id, task="tsk_q")
    _say(board, "task_reopened", by="tester", task="tsk_other")
    assert watch_service._idle_by_design(agent.id) is True, "another task's reopen"
    _say(board, "task_reopened", by="tester", task="tsk_q")
    assert watch_service._idle_by_design(agent.id) is False


def test_a_word_to_someone_else_leaves_an_agent_idle_by_design(board: str) -> None:
    agent = _agent(board, "coder-quiet", "qz-r12-0000-0000-0000-000000000009")
    _say(board, "result", by=agent.session_id)
    _say(board, "note", by=MANAGER, to="tester")
    _say(board, "task_claimed", by="coder", task="tsk_q")
    assert watch_service._idle_by_design(agent.id) is True


def _stale_look(board: str, texts: list[str], agent_id: str, monkeypatch: Any) -> list[str]:
    """Watch's stale look over ``texts`` as the captain queue's observations of one agent."""
    from aisquare.services.captain import queue

    now = datetime.now(tz=UTC)
    seen = queue.Observed(
        observations=[
            queue.Observation(
                key=f"k{n}",
                project=board,
                project_name="repo",
                agent="coder-quiet",
                kind="stale",
                text=text,
                seen_at=now - timedelta(minutes=41),
                source_ref=agent_id,
            )
            for n, text in enumerate(texts)
        ]
    )
    monkeypatch.setattr(queue, "observe", lambda *_, **__: seen)
    watcher = watch_service.Watcher(
        WatchSettings(auto_stop=False), needs_sources=lambda: [], stale=True
    )
    return [item.summary for item in watcher.stale(now)]


def test_a_lost_or_unknown_agent_is_never_idle_by_design(
    board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only an agent that WAITS quietly can be waiting by design: lost and unknown are news."""
    from aisquare.services.captain import queue

    agent = _agent(board, "coder-quiet", "qz-r12-0000-0000-0000-000000000009")
    _say(board, "result", by=agent.session_id)
    quiet = f"coder-quiet {queue.QUIET} for 41 min"
    lost, unknown = "coder-quiet is lost: pane gone", "coder-quiet is unknown"
    assert _stale_look(board, [quiet, lost, unknown], agent.id, monkeypatch) == [lost, unknown]
    _say(board, "note", by=MANAGER, to="coder-quiet")
    assert _stale_look(board, [quiet], agent.id, monkeypatch) == [quiet], "answered: news"


# standby's ends and holds


@pytest.mark.parametrize("prompt", ["wake", "tell"])
def test_a_fleet_prompt_does_not_end_standby(
    prompt: str, runner: CliRunner, board: str, work_dir: Path
) -> None:
    """The brief: "A fleet wake must NOT end it". Only its person's prompt does."""
    from aisquare.core import prompt_origin

    assert runner.invoke(app, ["team", "standby", "--as", MANAGER[:8]]).exit_code == 0
    text = prompt_origin.WAKE_TEXT
    if prompt == "tell":
        text = "coder-auth: PR #42 is up"
        prompt_origin.mark_origin(text, prompt_origin.TELL, by="agent")
    payload = json.dumps({"cwd": str(work_dir), "session_id": MANAGER, "prompt": text})
    assert runner.invoke(app, ["hook", "user-prompt-submit"], input=payload).exit_code == 0
    assert _standby() is not None, f"a fleet {prompt} keeps it"


def test_standby_holds_nothing_while_work_is_in_flight(runner: CliRunner, board: str) -> None:
    """standby_of itself: a standby begun before a task was in flight holds nothing while one
    is, even with no work-returns event to end it (a task added already in doing)."""
    with store_session() as store:
        team_service.begin_standby(store, _manager(), why="explicit")
        assert team_service.standby_of(store, _manager()) is not None
    _task(runner, "doing")
    with store_session() as store:
        store.delete_meta(team_service.STANDBY_META + MANAGER)
        team_service.begin_standby(store, _manager(), why="explicit")  # after the claim
        assert team_service.work_in_flight(store, board)
        assert team_service.standby_of(store, _manager()) is None
        assert team_service.standby_on_its_question(store, _manager()) is None


# the manager's rulings on the reopen (seq 12616)


def test_a_note_filed_with_no_task_is_about_the_task_it_names(board: str) -> None:
    """How agents write it: "DONE #134 …", no ``--task``. Its number makes it the same task."""
    task, other = _real_task(board, "the work"), _real_task(board, "other work")
    _say(board, "note", by="coder", to="manager", text=f"DONE #{task.number}, PR #507 is up")
    _read_all(board)
    assert _echo(_say(board, "task_review", by="coder", task=task.id))
    _say(board, "note", by="coder", to="manager", text=f"DONE #{other.number}")
    _read_all(board)
    assert not _echo(_say(board, "task_review", by="coder", task=task.id)), "another number"


@pytest.mark.parametrize(
    ("filed", "names"),
    [
        pytest.param(None, None, id="no task, no number"),
        pytest.param("other", "own", id="filed with another task, naming this one"),
    ],
)
def test_a_note_not_about_the_moved_task_is_no_echo(
    filed: str | None, names: str | None, board: str
) -> None:
    task, other = _real_task(board, "the work"), _real_task(board, "other work")
    text = "DONE, moving to review" if names is None else f"DONE #{task.number}"
    _say(board, "note", by="coder", to="manager", task=other.id if filed else None, text=text)
    _read_all(board)
    assert not _echo(_say(board, "task_review", by="coder", task=task.id))
    assert _wakes()


def test_a_move_with_no_task_is_no_echo(board: str) -> None:
    _say(board, "note", by="coder", to="manager", task="tsk_a")
    _read_all(board)
    assert not _echo(_say(board, "task_review", by="coder"))


def test_the_stop_door_blocks_for_a_review_after_a_result_about_something_else(
    runner: CliRunner, board: str, work_dir: Path
) -> None:
    """The tester's Stop-door case: a coder's result one step earlier, read mid-turn, made its
    review no block. Not about that task, the review keeps the manager going."""
    task = _real_task(board)
    _say(board, "result", by="coder", text="PUSHED the fix for the flaky test")
    _read_all(board)
    _say(board, "task_review", by="coder", task=task.id)
    payload = {"cwd": str(work_dir), "session_id": MANAGER, "stop_hook_active": False}
    done = runner.invoke(app, ["hook", "stop"], input=json.dumps(payload))
    assert done.exit_code == 0, done.output
    assert json.loads(done.stdout)["decision"] == "block"


def test_an_agent_holding_a_task_in_doing_is_never_idle_by_design(
    runner: CliRunner, board: str
) -> None:
    """Mid-task, its quiet is news whatever it said last, answered or not."""
    agent = _agent(board, "coder-quiet", SESSIONS["coder"])
    _say(board, "note", by="coder", to="manager", text="PUSHED; may I have the suite slot?")
    assert watch_service._idle_by_design(agent.id) is True, "no task in doing: waiting on it"
    _task(runner, "doing")  # the coder claims one
    _say(board, "note", by="coder", to="manager", text="PUSHED; may I have the suite slot?")
    assert watch_service._idle_by_design(agent.id) is False


def test_ce2_a_coder_answered_then_quiet_for_40_min_wakes_the_manager(
    board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CE2: coder-20 asks for the suite slot, the manager answers ``note --to coder-20 GO``
    (a note to a waiting agent waits for a turn end that never comes), and coder-20 stays
    quiet for 40 minutes. Watch's stale line is on the manager's line, and wakes it."""
    from aisquare.services.captain import queue

    agent = _agent(board, "coder-20", "c20-r12-0000-0000-0000-000000000020")
    _say(board, "note", by=agent.session_id, to="manager", text="PUSHED; may I have the slot?")
    _say(board, "note", by=MANAGER, to="coder-20", text="GO")
    _read_all(board)
    quiet = f"coder-20 {queue.QUIET} for 40 min"
    summaries = _stale_look(board, [quiet], agent.id, monkeypatch)
    assert summaries == [quiet]
    _watch_line(board, [_item("stale", agent="coder-20", quiet=True)])
    assert _wakes(), "a working fleet stalled: the manager must hear it"


def test_ce3_a_re_review_and_a_quiet_coder_mid_task_both_reach_the_manager(
    runner: CliRunner, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CE3: F1 and F2 together. Round 2's review wakes the manager; and the coder, back on a
    task in doing and quiet for 40 minutes, is a stale line that wakes it too."""
    from aisquare.services.captain import queue

    agent = _agent(board, "coder-quiet", SESSIONS["coder"])
    task = _real_task(board)
    _say(board, "note", by="coder", to="manager", text=f"DONE #{task.number}")
    _say(board, "task_review", by="coder", task=task.id)
    _say(board, "task_reopened", by="tester", task=task.id)
    _read_all(board)
    _say(board, "task_review", by="coder", task=task.id)
    assert _wakes(), "round 2's review"
    _read_all(board)
    _task(runner, "doing")
    quiet = f"coder-quiet {queue.QUIET} for 40 min"
    assert _stale_look(board, [quiet], agent.id, monkeypatch) == [quiet]
    _watch_line(board, [_item("stale", agent="coder-quiet", quiet=True)])
    assert _wakes()


def test_team_standby_off_holds_against_the_same_question_until_a_new_turn(
    runner: CliRunner, board: str, work_dir: Path
) -> None:
    """The ruling's minor: after ``--off``, the question the manager's turn ended on does not
    put it back in standby at watch's next look; a new turn of its, fleet wake or not, ends
    that, and ``team standby`` turns it on whenever asked."""
    from aisquare.core import prompt_origin

    _agent(board, "manager", MANAGER)
    _asked(board, "manager")
    assert _standby() is not None
    off = runner.invoke(app, ["team", "standby", "--off", "--as", MANAGER[:8]])
    assert off.exit_code == 0, off.output
    _asked(board, "manager")
    assert _standby() is None, "off holds while its last words are that question"
    assert runner.invoke(app, ["team", "standby", "--as", MANAGER[:8]]).exit_code == 0
    assert _standby() is not None, "turned on by hand, it is on"
    assert runner.invoke(app, ["team", "standby", "--off", "--as", MANAGER[:8]]).exit_code == 0
    payload = {"cwd": str(work_dir), "session_id": MANAGER, "prompt": prompt_origin.WAKE_TEXT}
    hooked = runner.invoke(app, ["hook", "user-prompt-submit"], input=json.dumps(payload))
    assert hooked.exit_code == 0, hooked.output
    _asked(board, "manager")
    assert _standby() is not None, "a new turn ended on a question: standby again"


def test_only_the_authors_last_act_can_be_the_word_a_move_echoes(board: str) -> None:
    """The ruling's "last act before the move": after its note about the task and its review,
    the same author's done of that task is news. The note is no longer its last word."""
    task = _real_task(board)
    _say(board, "note", by="coder", to="manager", text=f"DONE #{task.number}")
    _read_all(board)
    assert _echo(_say(board, "task_review", by="coder", task=task.id))
    _read_all(board)
    assert not _echo(_say(board, "task_done", by="coder", task=task.id))
    assert _wakes()


def test_a_word_to_the_author_after_its_move_leaves_the_echo(board: str) -> None:
    """Between means between: the manager's own reply after the review is no news in it."""
    task = _real_task(board)
    _say(board, "note", by="coder", to="manager", text=f"DONE #{task.number}")
    _read_all(board)
    review = _say(board, "task_review", by="coder", task=task.id)
    _say(board, "note", by=MANAGER, to="coder", text="thanks, a tester is on it")
    _say(board, "task_reopened", by="tester", task=task.id)
    assert _echo(review)
