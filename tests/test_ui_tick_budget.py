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

import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aisquare.cli.ui import app as app_mod
from aisquare.cli.ui.sidebar import group_header_text
from aisquare.core import tmux as tmux_core
from aisquare.core.store import ContextStore, store_session
from aisquare.core.workspace import project_id_for
from aisquare.models import FleetAgent, ProjectInfo, TeamSession
from aisquare.services import fleet as fleet_service
from aisquare.services.project_groups import arrange

PROJECTS = 50
WITH_AGENTS = 10
AGENTS_EACH = 3
STARTED = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)

_SEP = tmux_core._SEP


class Panes:
    """A fake tmux server: every pane by session, answering any ``list-panes -F`` format."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.panes: list[dict[str, str]] = []

    def add(self, session: str, pane_id: str, *, dead: bool = False) -> None:
        n = len(self.panes) + 1
        self.panes.append(
            {
                "session_name": session,
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
            session = args[args.index("-t") + 1].lstrip("=")
            panes = [p for p in panes if p["session_name"] == session]
        elif verb == "display-message":
            panes = panes[:1]
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


def test_one_read_of_50_projects_is_one_store_session_and_one_tmux_process(
    fleet: tuple[list[ProjectInfo], Panes], monkeypatch: pytest.MonkeyPatch
) -> None:
    _projects, panes = fleet
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

    with store_session() as store:
        many = fleet_service.list_agents_many(fleet_service.read_listings(store, projects))
    # The dead pane's row was ended by the first listing: the second reads it ended too.
    one_by_one = {p.id: fleet_service.list_agents(p) for p in projects}

    assert many.errors == {}
    for project in projects:
        assert many.agents.get(project.id, []) == one_by_one[project.id], project.codename
    assert [s.agent.label for s in one_by_one[projects[12].id]] == ["ended"]
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
