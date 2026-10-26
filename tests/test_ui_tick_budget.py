"""``asq``'s tick costs what its agents cost, not what its project list costs (#83).

Every two seconds the sidebar's worker read every listed project: a store session each,
and two tmux processes each (its session's windows, then every pane's last output),
whether the project had an agent or not. Filed on 0.6.0 with 305 registered projects;
#272 measured ~11 ms a project, so ~550 ms per read at 50.

The budget here is counted, not timed, so it holds on any machine: 50 projects, 10 of
them with 3 live agents on one tmux server, and a fake tmux that answers any ``-F``
format from one table of panes. One read is one store session and one tmux process. A
timing line prints under ``pytest -s``, as #272's ``tests/test_terminal_control.py`` does.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aisquare.cli.ui import app as app_mod
from aisquare.cli.ui.sidebar import group_header_text
from aisquare.core import launch_seal
from aisquare.core import tmux as tmux_core
from aisquare.core.store import ContextStore, store_session
from aisquare.core.workspace import project_id_for
from aisquare.models import FleetAgent, LaunchSpec, ProjectInfo, TeamSession
from aisquare.services import fleet as fleet_service
from aisquare.services.project_groups import arrange

PROJECTS = 50
WITH_AGENTS = 10
AGENTS_EACH = 3
STOPPED = 5  # of the projects with agents, this many also have a worker that was stopped
STARTED = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)

_SEP = tmux_core._SEP

#: The fleet's read asks tmux by its socket, and tmux sockets are POSIX: on Windows
#: ``socket_path()`` refuses, so the read never reaches the fake server (run 40073711742).
TMUX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="tmux sockets are POSIX: socket_path() refuses on Windows"
)


class Panes:
    """A fake tmux server: every pane by session, answering any ``list-panes -F`` format."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.panes: list[dict[str, str]] = []
        self.session_ids: dict[str, str] = {}

    def add(self, session: str, pane_id: str, *, dead: bool = False) -> None:
        n = len(self.panes) + 1
        self.panes.append(
            {
                "session_name": session,
                # tmux's own id for the session, which it keeps whatever the session is called
                "session_id": self.session_ids.setdefault(session, f"${len(self.session_ids)}"),
                "window_id": f"@{n}",
                "window_name": f"w{n}",
                "pane_id": pane_id,
                "pane_dead": "1" if dead else "0",
                "pane_dead_status": "0" if dead else "",
                "pane_current_command": "claude",
                "window_activity_flag": "0",
                "start_time": str(int(STARTED.timestamp())),
                "window_activity": str(int(STARTED.timestamp()) + n),
            }
        )

    def __call__(
        self, argv: Sequence[str], stdin: bytes | None, *, timeout: float | None = None
    ) -> tmux_core.Completed:
        args = list(argv)
        self.calls.append(tuple(args))
        verb = next((a for a in args if a in ("list-panes", "display-message")), None)
        fmt = args[args.index("-F") + 1] if "-F" in args else args[-1]
        fields = [f.strip("#{}") for f in fmt.split(_SEP)]
        panes = self.panes
        if verb == "list-panes" and "-t" in args:
            session = args[args.index("-t") + 1].lstrip("=")  # a name (`=asq-x`) or an id (`$3`)
            panes = [p for p in panes if session in (p["session_name"], p["session_id"])]
        elif verb == "display-message":
            # One pane's facts, by the id it is asked for (`display-message -t %N`), as tmux.
            asked = args[args.index("-t") + 1] if "-t" in args else None
            panes = [p for p in panes if p["pane_id"] == asked] if asked else panes[:1]
        lines = [_SEP.join(p.get(field, "") for field in fields) for p in panes]
        return tmux_core.Completed(0, "\n".join(lines) + ("\n" if lines else ""), "")


@pytest.fixture
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[list[ProjectInfo], Panes]:
    """50 listed projects, the first 10 with three live agents each on one server."""
    panes = Panes()
    monkeypatch.setattr(tmux_core, "_tmux", panes)
    projects: list[ProjectInfo] = []
    with store_session() as store:
        for n in range(PROJECTS):
            root = (tmp_path / f"p{n:02d}").resolve()
            root.mkdir()
            project = store.onboard_project(
                ProjectInfo(id=project_id_for(root), root=root), codename=f"proj-{n:02d}"
            )
            projects.append(project)
            if n >= WITH_AGENTS:
                continue
            for k in range(AGENTS_EACH):
                pane = f"%{n * AGENTS_EACH + k}"
                # A session whose hooks have run since the row began: no folder-trust capture.
                store.upsert_session(
                    TeamSession(
                        id=f"ses_{n:02d}_{k}", project_id=project.id, role="coder",
                        label=f"coder-{k}", started_at=STARTED,
                        last_seen_at=STARTED + timedelta(minutes=1), state="working",
                    )
                )  # fmt: skip
                store.upsert_fleet_agent(
                    FleetAgent(
                        id=f"agt_{n:02d}_{k}", project_id=project.id, label=f"coder-{k}",
                        role="coder", pane_id=pane, session_id=f"ses_{n:02d}_{k}", cwd=root,
                        created_at=STARTED,
                    )
                )  # fmt: skip
                panes.add(fleet_service.session_name(f"proj-{n:02d}"), pane)
    return projects, panes


@contextmanager
def counted(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Every store session opened, by the UI's read or the fleet service's."""
    opened: list[str] = []

    def counting(where: str) -> object:
        @contextmanager
        def session() -> Iterator[ContextStore]:
            opened.append(where)
            with store_session() as store:
                yield store

        return session

    monkeypatch.setattr(app_mod, "store_session", counting("ui"))
    monkeypatch.setattr(fleet_service, "store_session", counting("fleet"))
    yield opened


@TMUX_ONLY
def test_one_read_of_50_projects_is_one_store_session_and_one_tmux_process(
    fleet: tuple[list[ProjectInfo], Panes], monkeypatch: pytest.MonkeyPatch
) -> None:
    _projects, panes = fleet
    # The rows have no launch record: the one-time census (#113) makes theirs, as the first
    # fleet read of a machine does, so the read counted is the steady state it is budgeted for.
    fleet_service.take_census()
    with counted(monkeypatch) as opened:
        panes.calls.clear()
        began = time.perf_counter()
        read = app_mod._read_fleet(False, viewed=())
        took = (time.perf_counter() - began) * 1000
    print(
        f"\n{PROJECTS} projects, {WITH_AGENTS} with agents: one read {took:.1f} ms, "
        f"{len(opened)} store sessions, {len(panes.calls)} tmux processes"
    )

    assert read.error is None and not read.notices
    shown = {pid: [s.agent.label for s in agents] for pid, agents in read.agents.items() if agents}
    assert len(shown) == WITH_AGENTS and all(len(v) == AGENTS_EACH for v in shown.values())
    assert opened == ["ui"], "one store session per read"
    verbs = [next(a for a in call if a in ("list-panes", "capture-pane", "display-message"))
             for call in panes.calls]  # fmt: skip
    assert len(panes.calls) == 1, f"one tmux process per server per read, not {verbs}"


def test_a_project_without_rows_costs_no_tmux_call(
    fleet: tuple[list[ProjectInfo], Panes], monkeypatch: pytest.MonkeyPatch
) -> None:
    projects, panes = fleet
    quiet = projects[WITH_AGENTS:]
    panes.calls.clear()
    with store_session() as store:
        listings = fleet_service.read_listings(store, quiet)
    assert fleet_service.list_agents_many(listings).agents == {}
    assert panes.calls == []


def end_a_worker(
    panes: Panes,
    project: ProjectInfo,
    n: int,
    *,
    spawned: bool,
    label: str = "stopped",
    window: str = "killed",
) -> FleetAgent:
    """A worker of ``project`` whose row is ended: ``stopped`` as ``fleet stop`` leaves it, and
    its ``window`` killed (the default: no pane of it on the server), lingering dead
    (``remain-on-exit``, the 💤 row) or still running (ended in the store only). If a spawn
    made it (``spawned``), its launch record names its session and its entry in the project's
    window index is left standing, which only ``reap`` and ``shutdown`` forget (``_stop_row``
    does not; W1b's auto-stop makes exactly such rows). Unless, the census makes its record
    from the row (``take_census``, taken after), which names no session."""
    ended = STARTED + timedelta(minutes=30)
    pane = f"%9{n:02d}"
    with store_session() as store:
        row = store.upsert_fleet_agent(
            FleetAgent(
                id=f"agt_{label}_{n:02d}", project_id=project.id, label=label, role="coder",
                pane_id=pane, cwd=project.root, created_at=STARTED, ended_at=ended,
                exit_status=0,
            )
        )  # fmt: skip
        store.mark_fleet_agent_stopped(row.id)
    name = fleet_service.session_name(project.codename or "")
    if window != "killed":
        panes.add(name, pane, dead=window == "lingers")
    if not spawned:
        return row
    session_id = panes.session_ids[name]
    record = launch_seal.LaunchRecord(
        id=row.id, project_id=project.id, project_root=str(project.root), role="coder",
        agent="claude", binary="claude", cwd=str(project.root),
        spec=LaunchSpec(binary="claude"), label=row.label,
    )  # fmt: skip
    assert launch_seal.write(record) is None
    place = launch_seal.WindowPlace(
        tmux_socket=row.tmux_socket or "", pane_id=pane, created_at=STARTED,
        server_started_at=STARTED, session=name, session_id=session_id,
    )  # fmt: skip
    assert launch_seal.placed(row.id, place) is None
    where = launch_seal.SessionPlace(
        tmux_socket=place.tmux_socket, session_id=session_id, name=name,
        created_at=STARTED, server_started_at=STARTED,
    )  # fmt: skip
    entry = launch_seal.WindowEntry(
        row_id=row.id, tmux_socket=place.tmux_socket, session_id=session_id,
        pane_id=pane, created_at=STARTED, server_started_at=STARTED,
    )  # fmt: skip
    assert launch_seal.add_project_session(project.id, where, entry) is None
    return row


@TMUX_ONLY
@pytest.mark.parametrize("spawned", [True, False], ids=["a recorded window", "a census record"])
def test_a_project_with_a_stopped_worker_still_costs_the_tick_one_tmux_process(
    fleet: tuple[list[ProjectInfo], Panes], monkeypatch: pytest.MonkeyPatch, spawned: bool
) -> None:
    """W3's budget (SCOPE row 4) once a project has a worker that was stopped (r23, 3):
    ``_running_ids`` asked each project's server for its start and for every live pane, two
    tmux processes per project per tick for a row that ended long ago, because the entry of
    its window is still in the index. Answered from the tick's one answer, it costs none."""
    projects, panes = fleet
    monkeypatch.setattr(fleet_service, "_now", lambda: STARTED + timedelta(hours=1))
    if spawned:
        fleet_service.take_census()  # the live rows' records; the stopped rows' are real ones
    stopped = [end_a_worker(panes, projects[n], n, spawned=spawned) for n in range(STOPPED)]
    if not spawned:
        fleet_service.take_census()
    with counted(monkeypatch):
        panes.calls.clear()
        read = app_mod._read_fleet(False, viewed=())
    verbs = [next(a for a in call if a in ("list-panes", "capture-pane", "display-message"))
             for call in panes.calls]  # fmt: skip
    assert read.error is None and not read.notices
    assert len(panes.calls) == 1, f"one tmux process per server per read, not {verbs}"

    # Cost, not correctness: each project reads what its own read says, the stopped rows'
    # killed windows shown nowhere, the live agents as before.
    for project in projects[:STOPPED]:
        labels = [s.agent.label for s in read.agents[project.id]]
        assert labels == [f"coder-{k}" for k in range(AGENTS_EACH)], project.codename
        assert read.agents[project.id] == fleet_service.list_agents(project), project.codename
    assert {row.id for row in stopped}.isdisjoint(
        s.agent.id for agents in read.agents.values() for s in agents
    )


@TMUX_ONLY
def test_the_tick_finds_the_window_of_an_ended_row_where_the_single_read_does(
    fleet: tuple[list[ProjectInfo], Panes], monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the tick no longer asks tmux (``_running_ids``, r23 finding 3) it reads out of
    the server's one answer, and finds the same: a row ended in the store whose window runs
    is an agent of the project's, one whose dead window lingers is its 💤 row, and one whose
    window was killed is neither, in the tick and in the project's own read alike."""
    projects, panes = fleet
    monkeypatch.setattr(fleet_service, "_now", lambda: STARTED + timedelta(hours=1))
    fleet_service.take_census()
    runs = end_a_worker(panes, projects[0], 0, spawned=True, label="runs", window="runs")
    lingers = end_a_worker(panes, projects[1], 1, spawned=True, label="lingers", window="lingers")
    killed = end_a_worker(panes, projects[2], 2, spawned=True)

    with store_session() as store:
        many = fleet_service.list_agents_many(fleet_service.read_listings(store, projects))
    for project in projects[:3]:
        assert many.agents[project.id] == fleet_service.list_agents(project), project.codename
    shown = {s.agent.id for agents in many.agents.values() for s in agents}
    assert runs.id in shown and lingers.id in shown and killed.id not in shown
    [lingering] = [s for s in many.agents[projects[1].id] if s.agent.id == lingers.id]
    assert lingering.state == "exited"
    [running] = [s for s in many.agents[projects[0].id] if s.agent.id == runs.id]
    assert running.agent.ended_at is None, "listed and acted on as running, whatever ended_at"


@TMUX_ONLY
def test_list_agents_many_answers_what_list_agents_answers(
    fleet: tuple[list[ProjectInfo], Panes], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per project, the same statuses: a dead pane ended, an ended row whose window lingers,
    one ended a day ago, and the quiet projects' nothing."""
    projects, panes = fleet
    now = STARTED + timedelta(hours=1)
    monkeypatch.setattr(fleet_service, "_now", lambda: now)
    with store_session() as store:
        dead = store.upsert_fleet_agent(
            FleetAgent(
                id="agt_dead", project_id=projects[0].id, label="dead", role="coder",
                pane_id="%900", cwd=projects[0].root, created_at=STARTED,
            )
        )  # fmt: skip
        store.upsert_fleet_agent(
            FleetAgent(
                id="agt_old", project_id=projects[11].id, label="old", role="coder",
                pane_id="%901", cwd=projects[11].root, created_at=STARTED - timedelta(days=3),
                ended_at=STARTED - timedelta(days=2),
            )
        )  # fmt: skip
        store.upsert_fleet_agent(
            FleetAgent(
                id="agt_ended", project_id=projects[12].id, label="ended", role="coder",
                pane_id="%902", cwd=projects[12].root, created_at=STARTED,
                ended_at=STARTED + timedelta(minutes=30),
            )
        )  # fmt: skip
    panes.add(fleet_service.session_name("proj-00"), dead.pane_id, dead=True)
    panes.add(fleet_service.session_name("proj-12"), "%902", dead=True)
    fleet_service.take_census()  # every row above, the dead and the ended ones too, has its record

    with store_session() as store:
        many = fleet_service.list_agents_many(fleet_service.read_listings(store, projects))
    # The dead pane's row was ended by the first listing: the second reads it ended too.
    one_by_one = {p.id: fleet_service.list_agents(p) for p in projects}

    assert many.errors == {}
    for project in projects:
        # The dead row has no launch record of its own session (the census made its record
        # from the row: no session to look in): the read that ended it shows its lingering
        # window as 💤 exited, and the next, finding it ended, does not (#113).
        got = [s for s in many.agents.get(project.id, []) if s.agent.id != dead.id]
        assert got == one_by_one[project.id], project.codename
    [ended_now] = [s for s in many.agents[projects[0].id] if s.agent.id == dead.id]
    assert ended_now.state == "exited"
    # Main asserted `["ended"]` here: an ended row whose window lingers shows as 💤. It is
    # narrowed on purpose (r23, 10): this row's record is the census's, which names the
    # session by its name and not by tmux's id (`WindowPlace.session_id`), and the listing
    # looks an ended row up in the sessions its record names by id. Left out, by the single
    # read and the tick alike; a real spawn's lingering window, recorded with its session's
    # id, is 💤 in both (`test_the_tick_finds_the_window_of_an_ended_row...`, below).
    assert one_by_one[projects[12].id] == [] == many.agents.get(projects[12].id, [])
    assert one_by_one[projects[11].id] == []


def test_a_collapsed_groups_hidden_members_are_counted_not_read(
    fleet: tuple[list[ProjectInfo], Panes], monkeypatch: pytest.MonkeyPatch
) -> None:
    projects, panes = fleet
    with store_session() as store:
        group = store.create_project_group("folded")
        store.update_project_group(group.id, collapsed=True)
        for project in projects[:4]:
            store.update_project_layout(project.id, group_id=group.id)
        store.update_project_layout(projects[0].id, pinned_at=STARTED)  # shown under Pinned
    panes.calls.clear()
    read = app_mod._read_fleet(False, viewed=[projects[1].id])  # one is open in a view

    # The pinned member and the viewed one are read; the other two are not keys at all.
    assert {projects[0].id, projects[1].id} <= read.agents.keys()
    assert projects[2].id not in read.agents and projects[3].id not in read.agents
    assert read.live[projects[2].id] == AGENTS_EACH and read.live[projects[3].id] == AGENTS_EACH

    arrangement = arrange(read.projects, read.groups)
    entry = next(e for e in arrangement.groups if e.group.id == group.id)
    header = group_header_text(entry, read.agents, read.live).plain
    assert header.endswith(f"  {4 * AGENTS_EACH}"), header


def test_a_scoped_ui_lists_and_reads_only_its_projects(
    fleet: tuple[list[ProjectInfo], Panes], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ui --project a --project b``: those two cards, and only their agents asked for."""
    projects, _panes = fleet
    scope = {projects[0].id, projects[20].id}
    read = app_mod._read_fleet(False, viewed=(), scope=scope)
    assert [p.id for p in read.projects] == [projects[0].id, projects[20].id]
    assert len(read.agents[projects[0].id]) == AGENTS_EACH and read.agents[projects[20].id] == []
