"""Every command aisquare's own text tells a sandboxed agent to run is one its broker runs
(SMOKE12, F1).

A Codex agent's sandbox refused ``aisquare team show <seq>``, the pointer every cut update
line ends with: the broker's whitelist lacked it, and store-broker.log held 6 refusals on
real Codex 0.166.0. The commands are read here from the texts themselves, rendered by the
code that writes them for a Codex session (its board and start pack, its role's cycle, its
kickoff in every state, its update and wake lines, the watch line, its restart, resume,
fork and hand-off lines, a brief's hint, the claim's own hint, the team commands' errors,
and a parent manager's start pack with its group's briefing and the routed line a member's
question sends it), never from a list kept beside them: a new pointer the broker refuses
fails here. A parent manager's two lines also run as written through its broker, and do
nothing else on a member's board (review r37 of #113, Q5).
"""

from __future__ import annotations

import json
import os
import re
import shlex
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app
from aisquare.core import harness, paths
from aisquare.core.store import store_session
from aisquare.models import FleetAgent, ProjectInfo, TeamEvent, TeamSession, TeamTask
from aisquare.services import briefs, store_broker, watch
from aisquare.services import fleet as fleet_service
from aisquare.services import parent as parent_service
from aisquare.services import team as team_service
from tests.test_store_broker import (  # noqa: F401 - the fixture, by name
    LAUNCH,
    MINE,
    NONCE,
    THEIRS,
    _answer,
    _broker,
    _request,
    parented,
    work,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the broker is POSIX's (FIFOs)")

#: ``aisquare <command> [<subcommand>]`` (or ``asq …``) wherever a text names one.
COMMAND = re.compile(r"(?<![\w-])(?:aisquare|asq)\s+([a-z][a-z-]*)(?:\s+([a-z][a-z-]*))?")


def _codex(session: TeamSession, role: str) -> TeamSession:
    return session.model_copy(update={"agent": "codex", "role": role, "label": f"{role}-x"})


def _kickoffs(task: TeamTask, sid: str) -> Iterator[str]:
    """The kickoff in every state the assignment block has, for every kind of role, built
    before the session exists (a label) and on it (its id)."""
    past = datetime.now(tz=UTC) - timedelta(hours=1)
    future = datetime.now(tz=UTC) + timedelta(hours=1)
    states: list[dict[str, object]] = [
        {"status": "todo", "claimed_by": None},
        {"status": "todo", "claimed_by": None, "needs": ["tsk_needed"]},
        {"status": "doing", "claimed_by": sid, "claim_expires_at": future},
        {"status": "doing", "claimed_by": THEIRS, "claim_expires_at": past},
        {"status": "doing", "claimed_by": THEIRS, "claim_expires_at": future},
        {"status": "review", "claimed_by": sid},
        {"status": "review", "claimed_by": THEIRS},
        {"status": "blocked", "claimed_by": None},
    ]
    for role in ("coder", "tester", "reviewer", "validator", "manager", "planner"):
        for state in states:
            shaped = task.model_copy(update=state)
            for session_id in (None, sid):
                yield team_service.kickoff_prompt(
                    shaped,
                    role=role,
                    label=f"{role}-01m6j729x",
                    session_id=session_id,
                    statuses={"tsk_needed": "todo"},
                )


def _fleet_lines(project: ProjectInfo, task: TeamTask, events: list[TeamEvent]) -> list[str]:
    """A restart's, a resume's, a fork's and a hand-off's first lines."""
    row = FleetAgent(
        id="agent-1",
        project_id=project.id,
        label="coder-x",
        role="coder",
        agent="codex",
        pane_id="%1",
        cwd=project.root,
        created_at=datetime.now(tz=UTC),
    )
    return [
        fleet_service._restart_prompt(row),
        fleet_service._resume_prompt(row, None),
        fleet_service._resume_prompt(row, None, automatic=False),
        fleet_service._fork_prompt(row, task, events, resumed=True),
        fleet_service._fork_prompt(row, task, events, resumed=False),
        fleet_service._handoff_prompt(row, task, events, "a usage limit"),
        fleet_service._handoff_prompt(row, None, events, None),
    ]


def _agent_texts(work: Path) -> dict[str, str]:  # noqa: F811
    """Every text aisquare hands an agent, rendered for a Codex session, by where it comes
    from."""
    for n in range(team_service._BOARD_TASKS + 2):  # past what the board lists
        team_service.add_task(f"task {n}", role="coder", detail="its contract", cwd=work)
    long = team_service.add_note("a first line\nand the rest of it", session_ref=MINE, cwd=work)
    with store_session() as store:
        me = store.get_session(MINE)
        assert me is not None
        project = store.get_project(me.project_id)
        assert project is not None
        sessions = store.team_sessions(project.id)
        tasks = store.team_tasks(project.id)
        events = store.recent_events(project.id, limit=20)
        texts: dict[str, str] = {}
        for role in ("coder", "tester", "reviewer", "validator", "planner", "ui-tester"):
            session = _codex(me, role)
            assigned = team_service.Assignment(tasks[0], False, [], False)
            texts[f"board ({role})"] = team_service._render_board(
                project,
                [session, *sessions],
                tasks,
                events,
                me=session,
                assigned=assigned,
                fleet=project,
                cwd=work,
            )
            texts[f"cycle with no project ({role})"] = "\n".join(
                team_service._role_cycle(session, None, tasks[0])
            )
        manager = _codex(me, "manager")
        texts["manager's start pack"] = team_service._start_pack(
            store, project, manager, assigned=None, persona_note=None, fleet=project, cwd=work
        )
        texts["manager's cycle with no project"] = "\n".join(
            team_service._role_cycle(manager, None)
        )
        texts["manager's cycle under a parent"] = "\n".join(
            harness.role_cycle(
                "manager",
                MINE[:8],
                fleet=lambda verb: f"aisquare fleet {verb} --project x",
                escalate_to_parent=True,
            )
        )
    authors = team_service.authors_of(sessions)
    texts["delta"] = team_service._render_delta(events, authors, truncated=True, board="b")
    texts["wake"] = team_service._render_wakeup(events, authors, truncated=True, board="b")
    texts["kickoffs"] = "\n".join(_kickoffs(tasks[0], MINE))
    texts["watch line"] = watch.event_text(
        [watch.WatchItem("i1", project.id, "p", "permission", "a dialog")],
        Path("/watch/p/20261103T000000Z.json"),
    )
    texts["fleet lines"] = "\n".join(_fleet_lines(project, tasks[0], events))
    texts["brief hints"] = briefs.record_hint(project.id, tasks[0].id, "B1") + briefs._MORE
    claimed = CliRunner().invoke(app, ["task", "claim", tasks[1].id, "--as", MINE[:8]])
    assert claimed.exit_code == 0, claimed.output
    texts["claim's hint"] = claimed.output
    texts["errors"] = "\n".join(
        str(error)
        for error in (
            team_service.DeliveryUnconfirmedError("evt_1", "b"),
            team_service.ForeignSessionError(THEIRS, [MINE]),
            team_service.NoSuchSession("coder-x", "why"),
        )
    )
    assert long.seq is not None and f"aisquare team show {long.seq}" in texts["delta"]
    texts.update(_parent_texts(work, project))
    return texts


#: A member's coder, whose question routes up to the parent's board.
MEMBER_CODER = "cccc3333-0000-0000-0000-000000000000"


def _routed_line(work: Path, member: ProjectInfo) -> TeamEvent:  # noqa: F811
    """The ``routed`` line a member's question to its parent puts on the parent's board."""
    now = datetime.now(tz=UTC)
    with store_session() as store:
        store.upsert_session(
            TeamSession(
                id=MEMBER_CODER,
                project_id=member.id,
                role="coder",
                started_at=now,
                last_seen_at=now,
            )
        )
    team_service.add_note(
        "which base, rc/p1 or rc/p2?", kind="question", to_role="parent", session_ref=MEMBER_CODER
    )
    with store_session() as store:
        own = store.get_session(MINE)
        assert own is not None
        events = store.recent_events(own.project_id, limit=50)
    return next(event for event in events if event.kind == "routed")


def _parent_texts(work: Path, project: ProjectInfo) -> dict[str, str]:  # noqa: F811
    """A parent manager's own texts: its start pack, with its group's briefing (the
    delegation verb), and the routed line a member's question puts on its board."""
    [member] = parented(work, "api")
    routed = _routed_line(work, member)
    with store_session() as store:
        me = store.get_session(MINE)
        assert me is not None
        pack = team_service._start_pack(
            store,
            project,
            _codex(me, "manager"),
            assigned=None,
            persona_note=None,
            fleet=project,
            cwd=work,
        )
    assert "You are the parent manager of:" in pack and "aisquare task add" in pack, pack
    assert routed.text.endswith(" — aisquare board --project api"), routed.text
    return {"parent manager's start pack": pack, "routed line": routed.text}


def test_every_command_aisquare_tells_a_codex_agent_to_run_runs_through_its_broker(
    work: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each command the texts name is one the broker runs, or one the CLI runs in the
    sandbox itself (``store_broker.LOCAL``: ``asq exec``). A fleet operation (a manager's
    spawn, a cleanup's reap, the memory gate, the watcher a manager detaches) the broker
    never runs: a sandboxed agent asks for it to run outside its sandbox, which is what the
    broker's refusal tells it (``store_broker.fleet_operation``)."""
    monkeypatch.setattr(harness, "_test_gate_on", lambda: True)  # its line too
    found: dict[tuple[str, ...], str] = {}
    by_source: dict[str, set[tuple[str, ...]]] = {}
    for source, text in _agent_texts(work).items():
        for match in COMMAND.finditer(text):
            words = [word for word in match.groups() if word]
            named = store_broker.command_path(words)
            assert not isinstance(named, str), (source, match.group(0))
            found.setdefault(named[0], source)
            by_source.setdefault(source, set()).add(named[0])
    refused = []
    for path, source in sorted(found.items()):
        if path[0] in store_broker.LOCAL:
            continue
        why = store_broker.vet(list(path))
        if store_broker.fleet_operation(path):
            assert why is not None and "fleet operation" in why and "an approval" in why, path
            continue
        if why is not None:
            refused.append(f"aisquare {' '.join(path)} (in the {source})")
    assert refused == [], refused
    # The texts were read, every kind of pointer among them.
    assert {
        ("team", "show"),
        ("team", "verify"),
        ("watch", "show"),
        ("task", "claim"),
        ("task", "show"),
        ("task", "review"),
        ("task", "list"),
        ("board",),
        ("task", "add"),
        ("note",),
        ("log",),
        ("brief", "evidence"),
        ("fleet", "gate"),
        ("watch",),
    } <= set(found), sorted(found)
    # A parent manager's own (review r37 of #113, Q5): its delegation, and its routed pointer.
    assert ("task", "add") in by_source.get("parent manager's start pack", set()), by_source
    assert by_source.get("routed line") == {("board",)}, by_source


def test_a_cycle_with_no_project_names_project_list_only_to_an_unsandboxed_session(
    work: Path,  # noqa: F811
) -> None:
    """Listing the projects reads the whole machine's, not the agent's own: its broker
    refuses it, so a Codex session is told who can name its project instead."""
    with store_session() as store:
        me = store.get_session(MINE)
    assert me is not None
    claude = "\n".join(team_service._role_cycle(me.model_copy(update={"role": "coder"}), None))
    codex = "\n".join(team_service._role_cycle(_codex(me, "coder"), None))
    assert "`aisquare project list` names them" in claude
    assert "project list" not in codex and "the human who runs this fleet" in codex
    assert store_broker.vet(["project", "list"]) is not None


# --- team show, end to end ---------------------------------------------------------------


def test_team_show_runs_through_the_broker_for_its_own_board_alone(
    work: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    """The pointer an update line ends with prints the event in full through the broker; an
    event of another board is not shown, only said to be elsewhere, as ``team verify``
    says it (both read the caller's board, and a brokered call's is its launch's)."""
    event = team_service.add_note(
        "the first line\nthe whole of the rest", session_ref=MINE, cwd=work
    )
    broker = _broker(tmp_path)

    shown = _answer(broker, _request(["team", "show", str(event.seq), "--as", MINE[:8]], work))
    bare = _answer(broker, _request(["team", "show", str(event.seq)], work, rid="1" * 32))

    assert shown["code"] == 0, shown
    assert "the whole of the rest" in str(shown["stdout"])
    assert bare["code"] == 0 and "the whole of the rest" in str(bare["stdout"]), bare
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    other = team_service.activate(elsewhere)
    theirs = team_service.add_note("another board's secret", project_id=other.id)
    told = _answer(broker, _request(["team", "show", str(theirs.seq)], work, rid="2" * 32))
    assert told["code"] != 0 and "another board's secret" not in str(told["stdout"]), told
    assert "no event matches" in str(told["stderr"])


# --- watch show, its own project's alone ----------------------------------------------------


def _detail(project_id: str, when: datetime) -> Path:
    item = watch.WatchItem(f"i-{project_id}", project_id, "p", "permission", f"{project_id}'s")
    return watch._write_detail(project_id, [item], when)


def test_watch_show_through_the_broker_reads_its_own_projects_detail_files_alone(
    work: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    """The broker runs it outside the sandbox, where any file the user may read is: a
    path is read only when it is a detail file of the launch's own project, and the newest
    is that project's newest, never another's newer one."""
    with store_session() as store:
        me = store.get_session(MINE)
    assert me is not None
    now = datetime.now(tz=UTC)
    mine = _detail(me.project_id, now)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    theirs = _detail(team_service.activate(elsewhere).id, now + timedelta(seconds=5))
    login = tmp_path / "auth.json"
    login.write_text(json.dumps({"project_id": me.project_id, "token": "secret"}), "utf-8")
    broker = _broker(tmp_path)

    own = _answer(broker, _request(["watch", "show", str(mine)], work))
    newest = _answer(broker, _request(["watch", "show"], work, rid="1" * 32))
    other = _answer(broker, _request(["watch", "show", str(theirs)], work, rid="2" * 32))
    secret = _answer(broker, _request(["watch", "show", str(login)], work, rid="3" * 32))

    assert own["code"] == 0 and f"{me.project_id}'s" in str(own["stdout"]), own
    assert newest["code"] == 0 and str(mine) in str(newest["stdout"]), newest
    for refused in (other, secret):
        assert refused["code"] == 1, refused
        assert "not a watch detail file of this agent's project" in str(refused["stderr"])
    assert "secret" not in str(secret["stdout"])
    unbrokered = CliRunner().invoke(app, ["watch", "show", str(theirs)])
    assert unbrokered.exit_code == 0, "run outside a sandbox, it reads any as it did"


def test_the_watcher_itself_stays_out_of_the_broker() -> None:
    """``watch show`` reads; the watcher, its detach and its stop run processes."""
    for argv in (["watch"], ["watch", "--detach"], ["watch", "--stop"], ["watch", "--once"]):
        why = store_broker.vet(argv)
        assert why is not None and "fleet operation" in why and "an approval" in why, argv
        assert store_broker.refusal_kind(argv) == "fleet"
    assert store_broker.vet(["watch", "show", "/x.json"]) is None
    assert store_broker.vet(["team", "show", "12"]) is None


# --- a parent manager's lines, through its broker (review r37 of #113, Q5) -----------------


def _rid(n: int) -> str:
    return f"{n:032x}"


def test_a_parent_managers_routed_lines_run_as_written_through_its_broker(
    work: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    """A Codex parent manager acts on two lines: the routed line's pointer reads the member's
    board, and its briefing's ``task add … --project <member> --origin <task>`` delegates
    there. Both run through its broker exactly as written, the add as the launch's own
    session and logged; nothing else on the member's board does (a claim, a done, a release,
    a fleet operation), nor a member that joined after the broker started, nor the members of
    a broker that never took them at its start."""
    [member] = parented(work, "api")
    routed = _routed_line(work, member)
    origin, _ = team_service.add_task("ship auth", cwd=work)
    with store_session() as store:
        briefing = "\n".join(parent_service.briefing(store, origin.project_id))
    [verb] = re.findall(r"`(aisquare task add [^`]+)`", briefing)
    filled = verb.replace("…", "auth API").replace("<member>", "api")
    delegation = shlex.split(filled.replace("<task>", str(origin.number)))
    pointer = shlex.split(routed.text.rsplit(" — ", 1)[1])
    broker = _broker(tmp_path)
    broker.settle()

    read = _answer(broker, _request(pointer[1:], work))
    added = _answer(broker, _request(["--json", *delegation[1:]], work, rid=_rid(1)))

    assert read["code"] == 0 and "which base, rc/p1 or rc/p2?" in str(read["stdout"]), read
    assert added["code"] == 0, added
    task = json.loads(str(added["stdout"]))
    assert (task["project_id"], task["created_by"]) == (member.id, MINE)
    log = (paths.log_dir() / store_broker.LOG_NAME).read_text(encoding="utf-8")
    assert f"launch={LAUNCH} " in log and f" admitted: task add #{task['number']} " in log
    assert f" admitted: board --project api: read api ({member.id})" in log
    assert f"on api ({member.id}) from board {origin.project_id}" in log

    forbidden = [
        ["task", "claim", task["id"], "--as", MINE[:8]],
        ["task", "done", task["id"]],
        ["task", "release", task["id"]],
        ["task", "show", task["id"]],
        ["task", "add", "x", "--project", "api", "--as", MEMBER_CODER[:8]],
        ["fleet", "spawn", "coder", "--project", "api"],
    ]
    for n, argv in enumerate(forbidden, start=2):
        refused = _answer(broker, _request(argv, work, rid=_rid(n)))
        assert refused["code"] not in (0, None), (argv, refused)
    with store_session() as store:
        kept = store.get_task(task["id"])
    assert kept is not None and (kept.status, kept.claimed_by) == ("todo", None)

    parented(work, "late")
    late = _answer(broker, _request(["board", "--project", "late"], work, rid=_rid(20)))
    assert late["code"] == 1 and "after this agent launched" in str(late["stderr"]), late
    unsettled = store_broker.Broker(tmp_path / "unsettled", NONCE, dict(os.environ))
    never = _answer(unsettled, _request(pointer[1:], work, rid=_rid(21)))
    assert never["code"] == 1 and "after this agent launched" in str(never["stderr"]), never
