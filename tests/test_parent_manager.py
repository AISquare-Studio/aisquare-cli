"""P2-W6 (#199): a parent manager over a project group.

A manager could not hear past its own board: a member's question reached the manager
coordinating the group only when it next read that board. A group can name a parent, an
ordinary project whose manager is the group's, and its members' chosen events route up to it
as one ``routed`` line, in the member's own write, with the parent manager nudged.

The store is real (an isolated home); the nudges are recorded instead of typed.
"""

from __future__ import annotations

import json
import re
import shlex
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import harness, paths
from aisquare.core.store import store_session
from aisquare.models import ProjectInfo, TeamEvent, TeamSession, TeamTask
from aisquare.services import fleet as fleet_service
from aisquare.services import parent as parent_service
from aisquare.services import project_groups as groups
from aisquare.services import team as team_service
from tests import test_fleet_service as fleet_suite
from tests.test_fleet_service import FakeTmux

pytestmark = pytest.mark.usefixtures("project_capture")

HQ_MANAGER = "aaaa1111-0000-0000-0000-000000000000"
API_CODER = "bbbb2222-0000-0000-0000-000000000000"


@pytest.fixture(autouse=True)
def nudges(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    def record(project_id: str, *, reason: str, board: str | None = None) -> bool:
        calls.append((project_id, reason))
        return True

    monkeypatch.setattr(fleet_service, "nudge_manager", record)
    return calls


def _project(root: Path, codename: str) -> ProjectInfo:
    root.mkdir(parents=True, exist_ok=True)
    with store_session() as store:
        return store.onboard_project(
            ProjectInfo(id=f"prj_{codename}", root=root), codename=codename
        )


def _session(sid: str, project: ProjectInfo, role: str) -> None:
    now = datetime.now(tz=UTC)
    with store_session() as store:
        store.upsert_session(
            TeamSession(
                id=sid,
                project_id=project.id,
                role=role,
                started_at=now,
                last_seen_at=now,
                cursor=store.latest_seq(project.id),
            )
        )


@pytest.fixture
def tree(tmp_path: Path) -> dict[str, ProjectInfo]:
    """``hq`` parents the group ``app`` of ``api`` and ``web``."""
    found = {name: _project(tmp_path / name, name) for name in ("hq", "api", "web")}
    with store_session() as store:
        group, _ = groups.create_group(store, "app", [found["api"].id, found["web"].id])
        store.set_group_settings(group.id, parent_project_id=found["hq"].id)
    _session(HQ_MANAGER, found["hq"], "manager")
    _session(API_CODER, found["api"], "coder")
    return found


def _events(project: ProjectInfo, kind: str) -> list[TeamEvent]:
    with store_session() as store:
        return [e for e in store.recent_events(project.id, limit=200) if e.kind == kind]


def _note(text: str, *, to: str | None = None, kind: str = "note") -> TeamEvent:
    return team_service.add_note(text, kind=kind, to_role=to, session_ref=API_CODER)


def _settings(**route: object) -> None:
    with store_session() as store:
        group = store.find_project_group("app")
        assert group is not None
        store.set_group_settings(group.id, route=route)


# --- route up --------------------------------------------------------------------------------


def test_a_members_question_to_the_parent_lands_on_its_board_and_wakes_its_manager(
    tree: dict[str, ProjectInfo], nudges: list[tuple[str, str]]
) -> None:
    """In the member's own write: one ``routed`` line on the parent's board, addressed to its
    manager (a wake under P1 WS-2), one line and a pointer, and the parent's manager nudged.
    Before this, the question stayed on the member's board until the parent read it."""
    event = _note("which base, rc/p1 or rc/p2?", to="parent", kind="question")

    [routed] = _events(tree["hq"], "routed")
    assert routed.to_role == "manager"
    assert routed.text == (
        f"routed from api#{event.seq} (question by coder): which base, rc/p1 or rc/p2?"
        " — aisquare board --project api"
    )
    assert ("prj_hq", "routed") in nudges
    with store_session() as store:
        manager = store.get_session(HQ_MANAGER)
    assert manager is not None and team_service.wake_class(routed, manager) == "wake"


def test_the_routed_lines_pointer_runs_and_shows_the_members_board(
    tree: dict[str, ProjectInfo], monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """TUI-401c check 4: the routed line ends with ' — aisquare board --project <member>',
    and `aisquare board` had no --project (exit 2, 'No such option'), so the one thing the
    line asks of the parent's manager failed. Run exactly as written, from the parent's own
    folder, it shows the member's board with the member's question on it."""
    event = _note("which base, rc/p1 or rc/p2?", to="parent", kind="question")
    [routed] = _events(tree["hq"], "routed")
    pointer = routed.text.rsplit(" — ", 1)[1]
    assert pointer == "aisquare board --project api", "control: the line as written"
    monkeypatch.chdir(tree["hq"].root)

    shown = runner.invoke(app, pointer.split()[1:])
    as_json = runner.invoke(app, ["--json", *pointer.split()[1:]])

    assert shown.exit_code == 0, shown.output
    assert "which base, rc/p1 or rc/p2?" in shown.stdout, shown.stdout
    board = json.loads(as_json.stdout)
    assert board["project"]["id"] == tree["api"].id
    assert event.seq in [e["payload"]["seq"] for e in board["events"]]
    missing = runner.invoke(app, ["--json", "board", "--project", "nope"])
    assert missing.exit_code == 1 and '"not_found"' in missing.stdout, missing.output


def test_board_watch_follows_the_named_project(
    tree: dict[str, ProjectInfo], monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """``board --project <member> --watch [--interval]`` watches the member's board: the
    watcher is handed that project and the interval, and its frame is that board."""
    from aisquare.cli import watch as watch_ui

    _note("which base, rc/p1 or rc/p2?", to="parent", kind="question")
    monkeypatch.chdir(tree["hq"].root)
    handed: list[tuple[float, str | None]] = []
    monkeypatch.setattr(
        watch_ui,
        "run_watch",
        lambda interval, project=None: handed.append(
            (interval, project.id if project is not None else None)
        ),
    )

    watched = runner.invoke(app, ["board", "--project", "api", "--watch", "--interval", "2"])

    assert watched.exit_code == 0, watched.output
    assert handed == [(2.0, tree["api"].id)]
    frame = watch_ui.board_frame(40, 160, tree["api"]).plain
    assert "which base, rc/p1 or rc/p2?" in frame, frame
    assert f"aisquare board — {tree['api'].root.name}" in frame


def test_a_pattern_routes_a_matching_note_and_not_another(
    tree: dict[str, ProjectInfo],
) -> None:
    _settings(pattern="READY|BLOCKED")
    _note("READY: #12 merged")
    _note("lunch at noon")
    texts = [e.text for e in _events(tree["hq"], "routed")]
    assert len(texts) == 1 and "READY: #12 merged" in texts[0]


def test_a_burst_from_one_child_is_one_routed_line_and_a_count(
    tree: dict[str, ProjectInfo], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The storm guard: one routed event per child per 60 s; the rest are folded into a
    count the next one carries. 10 children with 5 results each in a minute is ≤ 10 routed lines."""
    with store_session() as store:
        group = store.find_project_group("app")
        assert group is not None
        children = [_project(tree["hq"].root.parent / f"c{n}", f"c{n}") for n in range(8)]
        groups.add_to_group(store, group.id, [c.id for c in children])
    members = [tree["api"], tree["web"], *children]
    for n, member in enumerate(members):
        _session(f"cccc{n:04d}-0000-0000-0000-000000000000", member, "coder")
        for i in range(5):
            team_service.add_note(
                f"result {i}", kind="result", session_ref=f"cccc{n:04d}-0000-0000-0000-000000000000"
            )
    routed = _events(tree["hq"], "routed")
    assert len(routed) == 10, [e.text for e in routed]

    later = datetime.now(tz=UTC) + timedelta(seconds=61)
    monkeypatch.setattr(parent_service, "_now", lambda: later)
    team_service.add_note(
        "result 5", kind="result", session_ref="cccc0000-0000-0000-0000-000000000000"
    )
    last = max(_events(tree["hq"], "routed"), key=lambda e: e.seq)
    assert "+4 folded" in last.text, last.text


def test_a_question_is_never_folded_by_the_storm_guard(tree: dict[str, ProjectInfo]) -> None:
    """Review of #381, 2: a ``--to parent`` question right after the child's result (inside
    the 60 s window) was folded, so the parent saw only the result. What asks for a
    decision (a question, a block, anything to the parent) always routes."""
    _note("tests green", kind="result")
    _note("may I merge #12?", kind="question", to="parent")
    texts = [e.text for e in _events(tree["hq"], "routed")]
    assert len(texts) == 2 and "may I merge #12?" in texts[1], texts


def test_a_group_without_a_parent_routes_nothing(tmp_path: Path) -> None:
    """Byte for byte as before: no routed line anywhere, and no link marks."""
    a, b = _project(tmp_path / "a", "alpha"), _project(tmp_path / "b", "beta")
    with store_session() as store:
        groups.create_group(store, "plain", [a.id, b.id])
    _session(API_CODER, a, "coder")
    _note("anyone?", kind="question", to="parent")
    with store_session() as store:
        kinds = {e.kind for p in (a, b) for e in store.recent_events(p.id, limit=50)}
    assert "routed" not in kinds


def test_routing_off_routes_nothing(tree: dict[str, ProjectInfo]) -> None:
    _settings(off=True)
    _note("stuck", kind="question", to="parent")
    assert _events(tree["hq"], "routed") == []


# --- linked tasks ---------------------------------------------------------------------------


def test_a_delegated_task_shows_its_origin_routes_up_and_all_done_closes_the_loop(
    tree: dict[str, ProjectInfo], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``task add --project <member> --origin <task>`` from the parent: both boards show the
    link; a child's done routes up; once every child is done, ``linked_all_done`` lands on
    the parent's board for its manager to close the origin."""
    origin, _ = team_service.add_task("ship auth", session_ref=HQ_MANAGER)
    api_task, _ = team_service.add_task(
        "auth API", session_ref=HQ_MANAGER, project="api", origin=str(origin.number)
    )
    web_task, _ = team_service.add_task(
        "auth UI", session_ref=HQ_MANAGER, project="web", origin=str(origin.number)
    )
    assert api_task.project_id == tree["api"].id and web_task.project_id == tree["web"].id

    with store_session() as store:
        hq_board = team_service.render_board(tree["hq"], [], store.team_tasks(tree["hq"].id), [])
        api_board = team_service.render_board(tree["api"], [], store.team_tasks(tree["api"].id), [])
    assert "ship auth → 2 linked, 0 done" in hq_board
    assert f"auth API ← hq#{origin.number}" in api_board

    team_service.claim_task(api_task.id, session_ref=API_CODER)
    team_service.finish_task(api_task.id, session_ref=API_CODER)
    assert any("task_done" in e.text for e in _events(tree["hq"], "routed"))
    assert _events(tree["hq"], "linked_all_done") == []

    later = datetime.now(tz=UTC) + timedelta(seconds=61)  # past the storm guard
    monkeypatch.setattr(parent_service, "_now", lambda: later)
    _session("dddd3333-0000-0000-0000-000000000000", tree["web"], "coder")
    team_service.claim_task(web_task.id, session_ref="dddd3333-0000-0000-0000-000000000000")
    team_service.finish_task(web_task.id, session_ref="dddd3333-0000-0000-0000-000000000000")
    [done] = _events(tree["hq"], "linked_all_done")
    assert done.to_role == "manager" and f"close #{origin.number}" in done.text


def test_a_dropped_child_does_not_hold_the_origin_open(tree: dict[str, ProjectInfo]) -> None:
    origin, _ = team_service.add_task("ship auth", session_ref=HQ_MANAGER)
    kept, _ = team_service.add_task(
        "auth API", session_ref=HQ_MANAGER, project="api", origin=str(origin.number)
    )
    dropped, _ = team_service.add_task(
        "auth UI", session_ref=HQ_MANAGER, project="web", origin=str(origin.number)
    )
    team_service.claim_task(kept.id, session_ref=API_CODER)
    team_service.finish_task(kept.id, session_ref=API_CODER)
    assert _events(tree["hq"], "linked_all_done") == []
    team_service.drop_task(dropped.id, session_ref=HQ_MANAGER)
    [closed] = _events(tree["hq"], "linked_all_done")
    # Not "are done": one was dropped, and the line the parent's manager acts on says so.
    assert f"all 2 tasks delegated from #{origin.number} are closed (1 done, 1 dropped)" in (
        closed.text
    ), closed.text


def test_delegating_to_a_project_this_board_does_not_parent_is_refused(
    tree: dict[str, ProjectInfo], tmp_path: Path
) -> None:
    stranger = _project(tmp_path / "other", "other")
    origin, _ = team_service.add_task("ship auth", session_ref=HQ_MANAGER)
    with pytest.raises(ValueError, match="not a member of a group this board"):
        team_service.add_task(
            "x", session_ref=HQ_MANAGER, project=stranger.codename, origin=str(origin.number)
        )
    with pytest.raises(ValueError, match="--origin goes with --project"):
        team_service.add_task("y", session_ref=HQ_MANAGER, origin=str(origin.number))


def test_a_sandboxed_agent_is_not_told_to_set_a_groups_parent(
    tree: dict[str, ProjectInfo], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its broker runs no configuration, so the refusal a sandboxed agent reads names no
    ``project group parent`` (SMOKE12, F1): the operator names a group's parent. A project
    that is no member is a scope refusal from its sandbox (review r37 of #113, Q5), and its
    own board named as the member the plain refusal, worded for it."""
    from aisquare.services import store_broker

    stranger = _project(tmp_path / "other", "other")
    origin, _ = team_service.add_task("ship auth", session_ref=HQ_MANAGER)
    _sandbox(monkeypatch, tree)
    with pytest.raises(store_broker.OutOfScope, match="names no member of a group") as refused:
        team_service.add_task(
            "x", session_ref=HQ_MANAGER, project=stranger.codename, origin=str(origin.number)
        )
    with pytest.raises(ValueError, match="not a member of a group this board") as itself:
        team_service.add_task("y", session_ref=HQ_MANAGER, project="hq", origin=origin.id)
    for said in (str(refused.value), str(itself.value)):
        assert "project group" not in said
        assert "the human who runs this fleet names a group's parent" in said


# --- a sandboxed parent manager: through its store broker (review r37 of #113, Q5) ----------

LAUNCH_HQ = "launch-hq"


def _sandbox(monkeypatch: pytest.MonkeyPatch, tree: dict[str, ProjectInfo]) -> Path:
    """From here on, every command runs as hq's manager's store broker runs one for its Codex
    launch: on hq's board, which parented api and web as the launch started
    (``store_broker.MEMBERS_ENV``, :meth:`Broker.settle`), as the session its own hook bound
    to the launch. Returns the brokers' log."""
    from aisquare.services import store_broker

    monkeypatch.chdir(tree["hq"].root)
    monkeypatch.setenv("AISQUARE_LAUNCH_ID", LAUNCH_HQ)
    monkeypatch.delenv("AISQUARE_FLEET_AGENT", raising=False)
    with store_session() as store:
        store.set_meta(f"launch-session:{LAUNCH_HQ}", HQ_MANAGER)
    monkeypatch.setenv(store_broker.CALLER_ENV, "1")
    monkeypatch.setenv(store_broker.BOARD_ENV, tree["hq"].id)
    monkeypatch.setenv(store_broker.MEMBERS_ENV, f"{tree['api'].id},{tree['web'].id}")
    return paths.log_dir() / store_broker.LOG_NAME


def _delegation_line(briefing: str, title: str, member: str, origin: TeamTask) -> list[str]:
    """The briefing's delegation verb, its three blanks filled, as a shell splits it."""
    [line] = re.findall(r"`(aisquare task add [^`]+)`", briefing)
    filled = line.replace("…", title).replace("<member>", member)
    return shlex.split(filled.replace("<task>", str(origin.number)))


def test_a_sandboxed_parent_runs_its_routed_pointer_and_its_briefings_delegation_as_written(
    tree: dict[str, ProjectInfo], monkeypatch: pytest.MonkeyPatch, runner: CliRunner
) -> None:
    """The two lines a parent manager acts on, run as written from its sandbox: the routed
    line's pointer shows the member's board, and the briefing's ``task add … --project
    <member> --origin <task>`` adds the task there, linked, as the launch's own session
    though the line names none, its event naming the parent's board. Each is a line in the
    brokers' log beside its refusals. Before, a brokered ``board --project`` read any board,
    and a delegation from the sandbox named nobody (the #66 LIMIT, seq 10405)."""
    _note("which base, rc/p1 or rc/p2?", to="parent", kind="question")
    [routed] = _events(tree["hq"], "routed")
    origin, _ = team_service.add_task("ship auth", session_ref=HQ_MANAGER)
    with store_session() as store:
        briefing = "\n".join(parent_service.briefing(store, tree["hq"].id))
    log = _sandbox(monkeypatch, tree)

    shown = runner.invoke(app, shlex.split(routed.text.rsplit(" — ", 1)[1])[1:])
    added = runner.invoke(
        app, ["--json", *_delegation_line(briefing, "auth API", "api", origin)[1:]]
    )

    assert shown.exit_code == 0, shown.output
    assert "which base, rc/p1 or rc/p2?" in shown.stdout, shown.stdout
    assert added.exit_code == 0, added.output
    task = json.loads(added.stdout)
    assert (task["project_id"], task["created_by"]) == (tree["api"].id, HQ_MANAGER)
    [made] = [e for e in _events(tree["api"], "task_added") if e.task_id == task["id"]]
    assert made.session_id == HQ_MANAGER, "attributed to the parent manager's session"
    assert made.meta == {"from": tree["hq"].id, "origin": origin.id}, made.meta
    with store_session() as store:
        assert store.task_origins(tree["api"].id)[task["id"]][0] == origin.id
    [read, logged] = [ln for ln in log.read_text("utf-8").splitlines() if " admitted: " in ln]
    assert f"launch={LAUNCH_HQ} " in read and f"launch={LAUNCH_HQ} " in logged
    assert f"admitted: board --project api: read api ({tree['api'].id}), a member" in read
    assert f"of a group board {tree['hq'].id} parents, for session {HQ_MANAGER}" in read
    assert f"task add #{task['number']} {task['id']} on api ({tree['api'].id})" in logged
    assert f"from board {tree['hq'].id}, from #{origin.number} {origin.id}" in logged
    assert f"as session {HQ_MANAGER}: added at seq {made.seq}: auth API" in logged

    again = runner.invoke(app, _delegation_line(briefing, "auth API", "api", origin)[1:])
    assert again.exit_code == 0 and "already tracked" in again.stdout, again.output
    assert "already open, not added again: auth API" in log.read_text("utf-8").splitlines()[-1]


def test_a_sandboxed_parent_names_no_session_of_its_own_and_delegates_nothing(
    tree: dict[str, ProjectInfo], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delegation from a sandbox is its launch's own session's: with none bound yet (its
    first hook has not run), nothing is added on the member's board."""
    origin, _ = team_service.add_task("ship auth", session_ref=HQ_MANAGER)
    _sandbox(monkeypatch, tree)
    monkeypatch.setenv("AISQUARE_LAUNCH_ID", "a-launch-no-hook-bound")

    with pytest.raises(ValueError, match="no session of its own yet"):
        team_service.add_task("auth API", project="api", origin=origin.id)

    assert _events(tree["api"], "task_added") == []


#: What a parent manager may not do on a member's board from its sandbox: claim, finish,
#: release or change a task of it, read one in full, or act as one of its sessions (a note,
#: a signal, a task added as one).
_FORBIDDEN = [
    (["task", "claim", "{task}", "--as", HQ_MANAGER[:8]], "out_of_scope"),
    (["task", "claim", "{task}", "--as", API_CODER[:8]], "not_your_session"),
    (["task", "done", "{task}"], "out_of_scope"),
    (["task", "release", "{task}"], "out_of_scope"),
    (["task", "review", "{task}"], "out_of_scope"),
    (["task", "reopen", "{task}", "--reason", "redo"], "out_of_scope"),
    (["task", "block", "{task}", "--reason", "stuck"], "out_of_scope"),
    (["task", "drop", "{task}"], "out_of_scope"),
    (["task", "show", "{task}"], "out_of_scope"),
    (["note", "from the parent", "--as", API_CODER[:8]], "not_your_session"),
    (["team", "signal", "phase", "frozen", "--as", API_CODER[:8]], "not_your_session"),
    (["task", "add", "x", "--project", "api", "--as", API_CODER[:8]], "not_your_session"),
]


@pytest.mark.parametrize(("argv", "error"), _FORBIDDEN, ids=lambda v: " ".join(v)[:40])
def test_from_its_sandbox_a_parent_does_nothing_else_on_a_members_board(
    tree: dict[str, ProjectInfo],
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
    argv: list[str],
    error: str,
) -> None:
    """Its board read and its ``task add`` are all a member's board gives the parent's
    sandbox; each other verb on it is refused, and the member's board is as it was. A fleet
    operation there its broker never runs (``store_broker.vet``)."""
    from aisquare.services import store_broker

    theirs, _ = team_service.add_task("auth API", session_ref=API_CODER)
    team_service.claim_task(theirs.id, session_ref=API_CODER)
    before = (_events(tree["api"], "task_added"), _events(tree["api"], "note"))
    _sandbox(monkeypatch, tree)

    refused = runner.invoke(app, ["--json", *(a.replace("{task}", theirs.id) for a in argv)])

    assert refused.exit_code == 1 and f'"{error}"' in refused.stdout, refused.output
    with store_session() as store:
        kept = store.get_task(theirs.id)
        assert kept is not None and (kept.status, kept.claimed_by) == ("doing", API_CODER)
        kinds = {e.kind for e in store.recent_events(tree["api"].id, limit=50)}
    assert (_events(tree["api"], "task_added"), _events(tree["api"], "note")) == before
    assert kinds <= {"task_added", "task_claimed", "activated"}, kinds
    fleet = store_broker.vet(["fleet", "spawn", "coder", "--project", "api"])
    assert fleet is not None and "fleet operation" in fleet


def test_the_members_a_command_may_name_are_its_brokers_alone(
    tree: dict[str, ProjectInfo], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The members come from the broker, with the mark of a command it runs
    (``store_broker.CALLER_ENV``), never from the variable alone: an MCP tool bound to a
    sandboxed launch's board (``store_broker.scoped_board``) reads no member's board,
    whatever its environment says."""
    from aisquare.services import store_broker

    monkeypatch.setenv(store_broker.MEMBERS_ENV, tree["api"].id)
    with (
        store_broker.scoped_board(tree["hq"].id),
        pytest.raises(store_broker.OutOfScope, match="after this agent launched"),
    ):
        team_service.board_project("api")
    assert team_service.board_project("api").id == tree["api"].id, "outside a sandbox: any"


def test_a_stranger_a_former_member_and_a_member_added_after_launch_are_refused_with_why(
    tree: dict[str, ProjectInfo],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    """Only a member the store says hq parents NOW, and that it parented when the launch
    started: a project in no group of it is refused without a word of its board, one that
    joined after the launch and one that has left are refused saying so. Each refusal is
    recorded (store-broker.log). A member still in the group reads and delegates as before."""
    stranger = _project(tmp_path / "other", "other")
    late = _project(tmp_path / "late", "late")
    team_service.add_note("the stranger's own secret", project_id=stranger.id)
    origin, _ = team_service.add_task("ship auth", session_ref=HQ_MANAGER)
    with store_session() as store:
        groups.add_to_group(store, "app", [late.id])
        groups.remove_from_group(store, [tree["web"].id])
    log = _sandbox(monkeypatch, tree)
    with store_session() as store:  # the store as it is now; the launch kept api and web
        assert {p.id for p in parent_service.members_parented_by(store, tree["hq"].id)} == {
            tree["api"].id,
            late.id,
        }

    for ref, why in (
        ("other", "--project other: names no member of a group this agent's board parents"),
        ("late", "--project late: joined a group this agent's board parents after this agent"),
        ("web", "--project web: no longer a member of a group this agent's board parents"),
    ):
        read = runner.invoke(app, ["board", "--project", ref])
        add = runner.invoke(app, ["task", "add", "x", "--project", ref, "--origin", origin.id])
        for refused in (read, add):
            assert refused.exit_code == 1 and why in refused.output, refused.output
        assert "secret" not in read.output
    with store_session() as store:
        for project in (stranger, late, tree["web"]):
            assert store.team_tasks(project.id) == [], project.codename
    refusals = [
        line for line in log.read_text("utf-8").splitlines() if " board: --project " in line
    ]
    assert len(refusals) == 6, refusals
    several = runner.invoke(app, ["board", "--project", "prj_"])  # hq, api and web's prefix
    assert several.exit_code == 1 and "'prj_' is ambiguous" in several.output, several.output
    member = runner.invoke(app, ["--json", "board", "--project", "api"])
    assert member.exit_code == 0 and json.loads(member.stdout)["project"]["id"] == tree["api"].id


# --- the manager's cycle and the parent's briefing -------------------------------------------


def test_a_member_manager_asks_its_parent_and_a_parent_is_briefed_on_its_group(
    tree: dict[str, ProjectInfo],
) -> None:
    sid = "eeee4444-0000-0000-0000-000000000000"
    cycle = " ".join(
        harness.role_cycle("manager", sid[:8], fleet=lambda v: v, escalate_to_parent=True)
    )
    assert "--kind question --to parent" in cycle and "Ask the human" not in cycle
    plain = " ".join(harness.role_cycle("manager", sid[:8], fleet=lambda v: v))
    assert "Ask the human" in plain, "no parent: as before"

    with store_session() as store:
        lines = parent_service.briefing(store, tree["hq"].id)
    text = "\n".join(lines)
    assert "You are the parent manager of:" in text and "app: api, web" in text
    assert len(text) <= parent_service.BRIEFING_MAX


# --- the CLI --------------------------------------------------------------------------------


def test_the_group_verbs(tree: dict[str, ProjectInfo]) -> None:
    runner = CliRunner()
    shown = runner.invoke(
        app, ["--json", "project", "group", "route", "app", "--kinds", "result,linked"]
    )
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.stdout)["route"] == {"kinds": ["result", "linked"], "off": False}
    bad = runner.invoke(app, ["project", "group", "route", "app", "--kinds", "gossip"])
    assert bad.exit_code != 0 and "unknown kind" in bad.output
    member = runner.invoke(app, ["project", "group", "parent", "app", "api"])
    assert member.exit_code != 0 and "a parent is a project outside it" in member.output
    cleared = runner.invoke(app, ["--json", "project", "group", "parent", "app", "--clear"])
    assert json.loads(cleared.stdout)["parent_project_id"] is None
    capped = runner.invoke(app, ["--json", "project", "group", "cap", "app", "3"])
    assert json.loads(capped.stdout)["max_agents"] == 3


# --- subtree pause and the cap (the fleet suite's fake tmux) ---------------------------------

tmux = fleet_suite.tmux
claude_on_path = fleet_suite.claude_on_path
repo = fleet_suite.repo
project = fleet_suite.project


def _member_of_a_parented_group(project: ProjectInfo, tmp_path: Path) -> ProjectInfo:
    hq = _project(tmp_path / "hq", "hq")
    with store_session() as store:
        group, _ = groups.create_group(store, "app", [project.id])
        store.set_group_settings(group.id, parent_project_id=hq.id)
    return hq


def test_a_group_pause_pauses_parent_and_members_and_an_agent_may_not_spawn_there(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, tmp_path: Path
) -> None:
    hq = _member_of_a_parented_group(project, tmp_path)
    result = CliRunner().invoke(app, ["--json", "fleet", "pause", "--group", "app"])
    assert result.exit_code == 0, result.output
    paused = json.loads(result.stdout)["signals"]
    assert {row["project"]["id"] for row in paused} == {project.id, hq.id}
    assert fleet_service.is_paused(project) and fleet_service.is_paused(hq)

    with pytest.raises(fleet_service.FleetError, match="is paused — an agent may not spawn"):
        fleet_service.spawn(project, "coder", worktree=False, spawned_by=HQ_MANAGER)
    fleet_service.spawn(project, "coder", worktree=False)  # a person's spawn goes ahead

    resumed = CliRunner().invoke(app, ["fleet", "resume", "--group", "app"])
    assert resumed.exit_code == 0 and not fleet_service.is_paused(project)
    fleet_service.spawn(project, "coder", worktree=False, spawned_by=HQ_MANAGER)


def test_a_paused_project_in_no_group_behaves_as_before(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo
) -> None:
    """The refusal is the subtree's: a project with no parent above or below it spawns for
    an agent while paused, as the cycle's own rule has always asked it not to."""
    fleet_service.pause(project)
    assert fleet_service.is_paused(project)
    receipt = fleet_service.spawn(project, "coder", worktree=False, spawned_by=HQ_MANAGER)
    assert receipt.agent.spawned_by == HQ_MANAGER and receipt.agent.ended_at is None


def test_a_group_cap_refuses_the_next_spawn(
    tmux: FakeTmux, claude_on_path: Path, project: ProjectInfo, tmp_path: Path
) -> None:
    _member_of_a_parented_group(project, tmp_path)
    with store_session() as store:
        group = store.find_project_group("app")
        assert group is not None
        store.set_group_settings(group.id, max_agents=1)
    fleet_service.spawn(project, "coder", worktree=False)
    with pytest.raises(fleet_service.CapacityRefused, match="group app already runs 1 agents"):
        fleet_service.spawn(project, "coder", worktree=False)
