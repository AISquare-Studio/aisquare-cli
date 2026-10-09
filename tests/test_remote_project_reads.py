"""Every read takes ``?project``, and nothing is ever answered from another project's cache.

Board, tasks, memory and explainability (and the note write) used to be pinned
to the project the server was started in, while fleet, panes and send-keys
already took a project: the page could show project B's agents and then B's
coder-1's card would be A's coder-1's, or a 404 (review of #243, finding 5).
The cache keyed each kind once, so even a project-aware read could be served
the other project's snapshot within the same tick.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aisquare.cli.app import app as cli
from aisquare.core.store import store_session
from aisquare.core.workspace import find_project_root, project_id_for
from aisquare.models import FleetAgent, ProjectInfo, TeamSession
from aisquare.services import fleet as fleet_service
from aisquare.services import remote_server
from aisquare.services import team as team_service
from aisquare.services.remote_server import (
    DoctorVerdict,
    NoSuchAgent,
    NoSuchProject,
    Runtime,
    Sources,
    build_app,
    live_sources,
    live_writes,
    remote_board_payload,
)
from tests.remote_kit_helpers import (
    base,
    frame_within,
    make_client,
    make_runtime,
    receive_within,
    unlock,
)

T0 = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)


class Reads:
    """Fake sources that answer with the project they were asked for, and count calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def _scoped(self, kind: str) -> Any:
        def read(project: str | None) -> object:
            self.calls.append((kind, project))
            if project == "nope":
                raise NoSuchProject("no project matches 'nope' (id prefix, name or codename)")
            return {"kind": kind, "project": project}

        return read

    def explainability(self, agent: str, project: str | None) -> dict[str, object]:
        self.calls.append(("explainability", (agent, project)))
        if project == "nope":
            raise NoSuchProject("no project matches 'nope' (id prefix, name or codename)")
        if agent == "ghost":
            raise NoSuchAgent("no live agent 'ghost'")
        return {"available": False, "model": f"model-of-{project}"}

    def transcript(
        self, agent: str, project: str | None, limit: int, before: str | None, width: int | None
    ) -> dict[str, object]:
        return {"lines": [f"width={width}"], "cursor": None, "more": False}

    def sources(self) -> Sources:
        return Sources(
            projects=lambda: [],
            fleet=self._scoped("fleet"),
            board=self._scoped("board"),
            tasks=self._scoped("tasks"),
            memory=self._scoped("memory"),
            panes=lambda agent, project, history: {"rows": [], "width": 0, "height": 0},
            transcript=self.transcript,
            explainability=self.explainability,
        )


@pytest.fixture
def runtime(isolated_home: Path) -> Runtime:
    return make_runtime()


@pytest.fixture
def reads() -> Reads:
    return Reads()


def _client(runtime: Runtime, reads: Reads, tmp_path: Path, *, tick: float = 60.0) -> Any:
    """``tick`` is long on purpose: every read below lands inside one cache lifetime."""
    app = build_app(runtime, sources=reads.sources(), dist_dir=tmp_path, tick=tick)
    client = make_client(app)
    assert unlock(client, runtime).status_code == 200
    return client


# --- ?project on board, tasks and memory ----------------------------------------------------


@pytest.mark.parametrize("kind", ["board", "tasks", "memory", "fleet"])
def test_a_named_project_is_read_and_no_project_is_the_current_one(
    runtime: Runtime, reads: Reads, tmp_path: Path, kind: str
) -> None:
    client = _client(runtime, reads, tmp_path)
    url = f"{base(runtime)}/api/{kind}"
    assert client.get(url).json() == {"kind": kind, "project": None}
    assert client.get(url, params={"project": "prj_b"}).json() == {"kind": kind, "project": "prj_b"}


@pytest.mark.parametrize("kind", ["board", "tasks", "memory"])
def test_project_b_right_after_project_a_in_one_tick_is_b(
    runtime: Runtime, reads: Reads, tmp_path: Path, kind: str
) -> None:
    """The cache key carries the project: one tick, two projects, two payloads."""
    client = _client(runtime, reads, tmp_path)
    url = f"{base(runtime)}/api/{kind}"
    assert client.get(url, params={"project": "prj_a"}).json()["project"] == "prj_a"
    assert client.get(url, params={"project": "prj_b"}).json()["project"] == "prj_b"
    assert client.get(url).json()["project"] is None
    assert client.get(url, params={"project": "prj_a"}).json()["project"] == "prj_a"
    assert reads.calls == [(kind, "prj_a"), (kind, "prj_b"), (kind, None)], "A was cached"


class Ticks:
    """The cache's monotonic clock, moved by hand."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_the_cache_keeps_only_what_was_read_within_the_last_tick() -> None:
    """Every ``?project=`` spelling that resolves is a kind of its own (``<id>*``, ``<id>**``
    and so on, which the store's glob reads as ``<id>``), and a snapshot stayed until its
    spelling was read again: 300 spellings of one project held 300 memory payloads."""
    ticks = Ticks()
    cache = remote_server._Cache(ttl=0.9, clock=ticks)

    async def read() -> None:
        for n in range(300):
            assert await cache.cached_snapshot(f"memory:prj_7b68{'*' * n}", lambda: "x" * 1_000)
            ticks.now += 1.0

    asyncio.run(read())
    assert list(cache._values) == [f"memory:prj_7b68{'*' * 299}"]


def test_however_many_kinds_one_tick_reads_the_cache_keeps_the_newest_few() -> None:
    ticks = Ticks()
    cache = remote_server._Cache(ttl=0.9, clock=ticks)
    spellings = [f"memory:prj_7b68{'*' * n}" for n in range(300)]

    async def read() -> None:
        for spelling in spellings:
            await cache.cached_snapshot(spelling, lambda: "x" * 1_000)

    asyncio.run(read())
    assert list(cache._values) == spellings[-remote_server.CACHE_KINDS_MAX :]
    computed: list[str] = []

    def again() -> str:
        computed.append("again")
        return "y"

    assert asyncio.run(cache.cached_snapshot(spellings[-1], again)) == "x" * 1_000
    assert computed == [], "within the tick, what was read is still answered from the cache"


@pytest.mark.parametrize("kind", ["board", "tasks", "memory"])
def test_an_unknown_project_is_a_404_shaped_like_an_unknown_agent(
    runtime: Runtime, reads: Reads, tmp_path: Path, kind: str
) -> None:
    client = _client(runtime, reads, tmp_path)
    response = client.get(f"{base(runtime)}/api/{kind}", params={"project": "nope"})
    assert response.status_code == 404
    assert response.json() == {
        "error": "not_found",
        "message": "no project matches 'nope' (id prefix, name or codename)",
    }


# --- ?project on explainability ---------------------------------------------------------------


def test_explainability_reads_the_named_projects_agent(
    runtime: Runtime, reads: Reads, tmp_path: Path
) -> None:
    client = _client(runtime, reads, tmp_path)
    url = f"{base(runtime)}/api/explainability/coder-1"
    assert client.get(url, params={"project": "prj_b"}).json()["model"] == "model-of-prj_b"
    assert client.get(url).json()["model"] == "model-of-None"
    assert ("explainability", ("coder-1", "prj_b")) in reads.calls


def test_explainability_for_an_unknown_project_or_agent_is_404_not_a_card(
    runtime: Runtime, reads: Reads, tmp_path: Path
) -> None:
    """A lookup failure is a 404; only a failure of the card itself is an unavailable card.
    An agent that is gone says so, which the page reads as "go back to the fleet"."""
    client = _client(runtime, reads, tmp_path)
    url = f"{base(runtime)}/api/explainability"
    for path, params, error in (
        ("coder-1", {"project": "nope"}, "not_found"),
        ("ghost", {}, "no_such_agent"),
    ):
        response = client.get(f"{url}/{path}", params=params)
        assert response.status_code == 404, path
        assert response.json()["error"] == error, path


# --- ?width on transcripts ------------------------------------------------------------------


def test_a_width_from_20_to_200_reaches_the_transcript(
    runtime: Runtime, reads: Reads, tmp_path: Path
) -> None:
    client = _client(runtime, reads, tmp_path)
    url = f"{base(runtime)}/api/transcript/coder-1"
    assert client.get(url).json()["lines"] == ["width=None"], "absent: the pane's own width"
    for width in (20, 37, 200):
        assert client.get(url, params={"width": width}).json()["lines"] == [f"width={width}"]


@pytest.mark.parametrize("width", ["19", "201", "abc", "5.5", "-40"])
def test_a_width_outside_20_to_200_is_400(
    runtime: Runtime, reads: Reads, tmp_path: Path, width: str
) -> None:
    client = _client(runtime, reads, tmp_path)
    response = client.get(f"{base(runtime)}/api/transcript/coder-1", params={"width": width})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid" and "width" in response.json()["message"]


# --- the live reads, over two real projects in one store --------------------------------------


@pytest.fixture
def two_projects(
    isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[ProjectInfo, ProjectInfo]:
    """``(current, other)``: one pinned active by ``init``, one merely registered."""
    current_dir = tmp_path / "current"
    current_dir.mkdir()
    monkeypatch.chdir(current_dir)
    runner = CliRunner()
    assert runner.invoke(cli, ["init", "--local", "--no-onboard", "--yes"]).exit_code == 0
    assert runner.invoke(cli, ["team", "on"]).exit_code == 0
    current = fleet_service.resolve_project(None)
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other = ProjectInfo(
        id=project_id_for(find_project_root(other_dir)), root=other_dir, linked_repos=[]
    )
    with store_session() as store:
        store.ensure_project(other)
        store.onboard_project(other)
    assert other.id != current.id
    return current, other


def _seed_agent(project: ProjectInfo, label: str, model: str) -> None:
    session_id = f"ses_{project.id[-6:]}"
    with store_session() as store:
        store.upsert_session(
            TeamSession(
                id=session_id,
                project_id=project.id,
                role="coder",
                started_at=T0,
                last_seen_at=T0,
                model=model,
            )
        )
        store.upsert_fleet_agent(
            FleetAgent(
                id=f"agt_{project.id[-6:]}_{label}",
                project_id=project.id,
                label=label,
                role="coder",
                pane_id="%9",
                session_id=session_id,
                cwd=project.root,
                created_at=T0,
            )
        )


def _board_project_id(board: dict[str, object]) -> object:
    project = board["project"]
    assert isinstance(project, dict)
    return project["id"]


def _event_texts(board: object) -> list[str]:
    """The text of every event on a board payload (each one a capture-pipe envelope)."""
    assert isinstance(board, dict)
    return [event["payload"]["text"] for event in board["events"]]


def test_the_board_of_another_project_is_that_projects_board(
    two_projects: tuple[ProjectInfo, ProjectInfo],
) -> None:
    current, other = two_projects
    team_service.add_note("on the current board", cwd=current.root)
    team_service.add_note("on the other board", cwd=other.root)
    here, there = remote_board_payload(None), remote_board_payload(other.id)
    assert _board_project_id(here) == current.id and _board_project_id(there) == other.id
    assert "on the other board" in _event_texts(there)
    assert "on the other board" not in _event_texts(here)
    with pytest.raises(NoSuchProject):
        remote_board_payload("no-such-project")


def test_the_board_is_read_from_the_projects_root_as_asq_board_there_would(
    two_projects: tuple[ProjectInfo, ProjectInfo], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``cwd=root`` is what keeps ``AISQUARE_TEAM_HUB`` and worktree resolution the CLI's."""
    _current, other = two_projects
    asked: list[Path | None] = []
    real = team_service.board_data

    def spy(cwd: Path | None = None, **kwargs: Any) -> Any:
        asked.append(cwd)
        return real(cwd, **kwargs)

    monkeypatch.setattr(team_service, "board_data", spy)
    remote_board_payload(other.id)
    remote_board_payload(None)
    assert asked == [other.root, None]


def test_the_board_tab_gets_the_newest_events_it_draws_not_the_clis_five(
    two_projects: tuple[ProjectInfo, ProjectInfo], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``board_data``'s default is ``asq board --json``'s glance, five events, and the Board
    tab was given no more: a question a card sent the human to "reply on the board" to was
    gone from it once five newer lines were (review of #243, round 3)."""
    current, _other = two_projects
    for n in range(9):
        team_service.add_note(f"line {n}", cwd=current.root)
    lines = [text for text in _event_texts(remote_board_payload(None)) if text.startswith("line")]
    assert sorted(lines) == [f"line {n}" for n in range(9)]
    printed = _json_of_board()
    assert len(printed["events"]) == 5, "the CLI's glance is unchanged"
    board = remote_board_payload(None)
    assert {key: board[key] for key in ("project", "sessions", "tasks")} == {
        key: printed[key] for key in ("project", "sessions", "tasks")
    }
    monkeypatch.setattr(remote_server, "BOARD_EVENTS", 7)
    newest = [text for text in _event_texts(remote_board_payload(None)) if text.startswith("line")]
    assert sorted(newest) == [f"line {n}" for n in range(2, 9)], "the newest, as many as asked"


def test_under_a_hub_each_projects_board_frame_is_the_hubs_board_named_for_that_project(
    two_projects: tuple[ProjectInfo, ProjectInfo],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """r4 2/9, on a real store: with ``AISQUARE_TEAM_HUB`` set, every project's board is the
    hub's, as ``asq board`` reads it there, and the board names the hub's project. The page
    went by that id and drew none of it. The frame names the project the socket asked for."""
    _current, other = two_projects
    hub = tmp_path / "hub"
    hub.mkdir()
    monkeypatch.setenv("AISQUARE_TEAM_HUB", str(hub))
    team_service.add_note("on the hub's board", cwd=other.root)
    board = remote_board_payload(other.id)
    assert _board_project_id(board) not in (other.id, None), "the hub's project, not other's"
    runtime = make_runtime()
    client = make_client(build_app(runtime, sources=live_sources(), dist_dir=tmp_path, tick=0.05))
    assert unlock(client, runtime).status_code == 200
    with client.websocket_connect(f"{base(runtime)}/ws") as ws:
        ws.send_text(json.dumps({"subscribe_board": other.id}))
        frame = _until(ws, lambda f: f["type"] == "board")
    assert frame["project"] == other.id
    assert frame["payload"]["project"]["id"] == _board_project_id(board)
    assert "on the hub's board" in _event_texts(frame["payload"])


def _seed_board(project: ProjectInfo) -> None:
    """Three sessions, two of which write on the board, a task, and three notes."""
    with store_session() as store:
        for n, seen in enumerate(("10:01", "10:03", "10:02")):
            store.upsert_session(
                TeamSession(
                    id=f"ses_{project.id[-6:]}_{n}",
                    project_id=project.id,
                    role="coder",
                    label=f"coder-{n}",
                    started_at=T0,
                    last_seen_at=datetime.fromisoformat(f"2026-10-07T{seen}:00+00:00"),
                )
            )
    team_service.add_task("ship it", cwd=project.root)
    team_service.add_note("from coder-0", session_ref=f"ses_{project.id[-6:]}_0", cwd=project.root)
    team_service.add_note("from coder-1", session_ref=f"ses_{project.id[-6:]}_1", cwd=project.root)
    team_service.add_note("from the desk", cwd=project.root)


def test_the_board_frame_is_what_the_board_read_makes_of_it(
    two_projects: tuple[ProjectInfo, ProjectInfo],
) -> None:
    """r4 5/9 reads the frame for what it carries; it must carry what it did, the events
    and the sessions they name, newest seen first, for a named project and the current."""
    current, other = two_projects
    for project in (current, other):
        _seed_board(project)
    sources = live_sources()
    assert sources.board_frame is not None
    for ref in (other.id, None):
        frame = sources.board_frame(ref)
        assert frame == remote_server.remote_board_frame(remote_board_payload(ref))
        assert isinstance(frame, dict)
        assert [one["label"] for one in frame["sessions"]] == ["coder-1", "coder-0"]


def test_a_board_frame_runs_no_git_writes_nothing_and_reads_no_session_it_drops(
    two_projects: tuple[ProjectInfo, ProjectInfo],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """r4 5/9: a phone on the Board tab cost a ``git rev-parse`` and a store write
    (``ensure_project``) every second, and every session and task the project ever had was
    read for the frame to drop. Measured over the live stream: the board's project is
    resolved once for the server, and a frame reads its events and the sessions they name."""
    from aisquare.core import orchestrator, workspace
    from aisquare.core.store import SqliteStore

    _current, other = two_projects
    _seed_board(other)
    asked: list[tuple[str, object]] = []

    def spy(name: str, real: Any, *, store: bool = True) -> Any:
        def called(*args: Any, **kwargs: Any) -> Any:
            asked.append((name, args[1] if store else args[0]))
            return real(*args, **kwargs)

        return called

    for name in ("ensure_project", "team_sessions", "team_tasks", "recent_events"):
        monkeypatch.setattr(SqliteStore, name, spy(name, getattr(SqliteStore, name)))
    git = spy("git", workspace.git_common_root, store=False)
    monkeypatch.setattr(orchestrator, "git_common_root", git)

    def of_other(name: str) -> list[object]:
        ids = (other.id, other.root, other.root.resolve())
        return [
            arg
            for asked_name, arg in asked
            if asked_name == name and (getattr(arg, "id", arg) in ids)
        ]

    runtime = make_runtime()
    client = make_client(build_app(runtime, sources=live_sources(), dist_dir=tmp_path, tick=0.02))
    assert unlock(client, runtime).status_code == 200
    with client.websocket_connect(f"{base(runtime)}/ws") as ws:
        ws.send_text(json.dumps({"subscribe_board": other.id}))
        frame = _until(ws, lambda f: f["type"] == "board")
        deadline = time.monotonic() + 10
        while len(of_other("recent_events")) < 4:
            assert time.monotonic() < deadline, "the board frame was read fewer than 4 times"
            time.sleep(0.01)
    assert [event["payload"]["text"] for event in frame["payload"]["events"]] == [
        "ship it",
        "from coder-0",
        "from coder-1",
        "from the desk",
    ]
    assert len(of_other("git")) <= 1, "its project resolved once, not once a frame"
    assert of_other("ensure_project") == [], "a frame writes nothing"
    assert of_other("team_sessions") == [] and of_other("team_tasks") == []
    whole = client.get(f"{base(runtime)}/api/board", params={"project": other.id}).json()
    assert len(whole["sessions"]) == 3 and len(whole["tasks"]) == 1, "the read is the whole board"


def _json_of_board() -> dict[str, Any]:
    result = CliRunner().invoke(cli, ["--json", "board"])
    assert result.exit_code == 0, result.output
    board: dict[str, Any] = json.loads(result.stdout.strip().splitlines()[-1])
    return board


def test_the_board_carries_as_many_events_as_the_board_tab_draws() -> None:
    script = (Path(remote_server.__file__).parents[1] / "web" / "remote" / "app.js").read_text(
        encoding="utf-8"
    )
    drawn = re.search(r"for \(const event of events\.slice\(0, (\d+)\)\)", script)
    assert drawn is not None, "the Board tab draws a slice of the board's events"
    assert int(drawn.group(1)) == remote_server.BOARD_EVENTS


def test_tasks_and_memory_of_another_project_are_that_projects(
    two_projects: tuple[ProjectInfo, ProjectInfo],
) -> None:
    from aisquare.core.entries import new_entry

    current, other = two_projects
    team_service.add_task("a current task", cwd=current.root)
    team_service.add_task("another project's task", cwd=other.root)
    with store_session() as store:
        store.add(new_entry("a current fact", "project", current.id, [], "cli"))
        store.add(new_entry("another project's fact", "project", other.id, [], "cli"))
    reads = live_sources()

    def titles(payload: object, key: str) -> set[str]:
        assert isinstance(payload, list)
        return {row[key] for row in payload}

    assert titles(reads.tasks(other.id), "title") == {"another project's task"}
    assert titles(reads.tasks(None), "title") == {"a current task"}
    assert "another project's fact" in titles(reads.memory(other.id), "text")
    assert "a current fact" not in titles(reads.memory(other.id), "text")
    assert "a current fact" in titles(reads.memory(None), "text")


def test_the_card_of_another_projects_coder_is_that_coders_card(
    two_projects: tuple[ProjectInfo, ProjectInfo], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The finding: the same label in two projects, and B's card used to be A's."""
    current, other = two_projects
    _seed_agent(current, "coder-1", "model-of-the-current-project")
    _seed_agent(other, "coder-1", "model-of-the-other-project")
    monkeypatch.setattr(
        remote_server, "_doctor_verdict", lambda: DoctorVerdict(sdk_present=True, red=[])
    )
    monkeypatch.setattr(remote_server, "_explainability_policy", lambda: None)
    reads = live_sources()
    assert reads.explainability("coder-1", other.id)["model"] == "model-of-the-other-project"
    assert reads.explainability("coder-1", None)["model"] == "model-of-the-current-project"
    with pytest.raises(NoSuchProject):
        reads.explainability("coder-1", "no-such-project")


def test_a_note_with_a_project_lands_on_that_projects_board(
    two_projects: tuple[ProjectInfo, ProjectInfo],
) -> None:
    _current, other = two_projects
    write_note = live_writes().handlers["note"]
    write_note({"text": "for the other board", "project": other.id})
    write_note({"text": "for the current board"})
    assert "for the other board" in _event_texts(remote_board_payload(other.id))
    assert "for the other board" not in _event_texts(remote_board_payload(None))
    assert "for the current board" in _event_texts(remote_board_payload(None))
    with pytest.raises(NoSuchProject):
        write_note({"text": "nowhere", "project": "no-such-project"})


def test_claiming_another_projects_task_needs_no_project(
    two_projects: tuple[ProjectInfo, ProjectInfo],
) -> None:
    """A task ref names its task in every project, and the claim lands on the task's
    own board: the task writes were never pinned to the current project."""
    _current, other = two_projects
    task, _created = team_service.add_task("another project's task", cwd=other.root)
    result, summary = live_writes().handlers["task/claim"]({"ref": task.id})
    claimed = result["task"]
    assert isinstance(claimed, dict) and claimed["project_id"] == other.id
    assert summary == f"claimed {task.id} as=-"
    assert "another project's task" in _event_texts(remote_board_payload(other.id))


# --- the socket: project panes, subscribe_board, caps ---------------------------------------


class Panes:
    """Fake captures that say whose pane they are, and remember who asked."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, str | None]] = []

    def __call__(self, agent: str, project: str | None, history: int) -> dict[str, object]:
        self.asked.append((agent, project))
        return {"rows": [f"{project}:{agent}"], "width": 1, "height": 1}


def _socket_client(runtime: Runtime, tmp_path: Path, reads: Reads, panes: Panes) -> Any:
    sources = dataclasses.replace(reads.sources(), panes=panes)
    app = build_app(runtime, sources=sources, dist_dir=tmp_path, tick=0.02)
    client = make_client(app)
    assert unlock(client, runtime).status_code == 200
    return client


def _until(ws: Any, match: Any, *, limit: int = 100) -> dict[str, Any]:
    for _ in range(limit):
        frame = frame_within(ws)
        if match(frame):
            return frame
    raise AssertionError(f"no matching frame in {limit}")


def _pane(agent: str, project: str | None = None) -> Any:
    return lambda f: f["type"] == "pane" and f["agent"] == agent and f.get("project") == project


def _captured_within(panes: Panes, pane: tuple[str, str | None], seconds: float = 5.0) -> bool:
    """Whether ``pane`` is captured within ``seconds``, its frame unchanged and so not sent.

    Not within one tick: the stream's captures go through the read cache, one per pane per
    tick however many sockets watch it, and the cache serves one younger than 0.9 of a
    tick. On windows-latest, CPython 3.12's monotonic clock moves in 15.6 ms steps and
    asyncio fires a timer up to one step early, so two 20 ms ticks can read one step apart,
    inside the 18 ms ttl: the tick that sent coder-3's first frame served coder-1's capture
    from the tick before and made no new one (CI run 37719330211).
    """
    deadline = time.monotonic() + seconds
    while pane not in panes.asked:
        if time.monotonic() > deadline:
            return False
        time.sleep(0.005)
    return True


def test_a_pane_subscription_reads_the_pane_of_the_project_it_names(
    runtime: Runtime, reads: Reads, tmp_path: Path
) -> None:
    """The finding: a phone watching project B's coder-1 was streamed project A's coder-1."""
    panes = Panes()
    client = _socket_client(runtime, tmp_path, reads, panes)
    with client.websocket_connect(f"{base(runtime)}/ws") as ws:
        ws.send_text(json.dumps({"subscribe": "coder-1", "project": "prj_b"}))
        theirs = _until(ws, _pane("coder-1", "prj_b"))
        ws.send_text(json.dumps({"subscribe": "coder-1"}))
        ours = _until(ws, _pane("coder-1"))
    assert theirs["payload"]["rows"] == ["prj_b:coder-1"]
    assert set(theirs) == {"type", "agent", "project", "payload", "ts"}
    assert ours["payload"]["rows"] == ["None:coder-1"]
    assert set(ours) == {"type", "agent", "payload", "ts"}, "no project named, no project key"
    assert {("coder-1", "prj_b"), ("coder-1", None)} <= set(panes.asked)


def test_unsubscribing_one_project_leaves_the_same_label_in_another(
    runtime: Runtime, reads: Reads, tmp_path: Path
) -> None:
    panes = Panes()
    client = _socket_client(runtime, tmp_path, reads, panes)
    with client.websocket_connect(f"{base(runtime)}/ws") as ws:
        ws.send_text(json.dumps({"subscribe": "coder-1", "project": "prj_a"}))
        ws.send_text(json.dumps({"subscribe": "coder-1", "project": "prj_b"}))
        _until(ws, _pane("coder-1", "prj_b"))
        ws.send_text(json.dumps({"unsubscribe": "coder-1", "project": "prj_a"}))
        ws.send_text(json.dumps({"subscribe": "coder-2", "project": "prj_a"}))
        _until(ws, _pane("coder-2", "prj_a"))
        panes.asked.clear()
        ws.send_text(json.dumps({"subscribe": "coder-3"}))
        _until(ws, _pane("coder-3"))
        still = _captured_within(panes, ("coder-1", "prj_b"))
    assert still, "the same label in another project: still captured"
    assert ("coder-1", "prj_a") not in panes.asked, "unsubscribed: no longer captured"


def test_a_pane_unsubscribed_while_it_is_captured_gets_no_frame(
    runtime: Runtime, reads: Reads, tmp_path: Path
) -> None:
    """The capture runs on the pool while the socket reads on, so an unsubscribe can land
    mid-capture. It wins: no frame is sent for the pane, and nothing is kept for it (a
    frame kept for a label nobody watches is memory a client could pile up)."""
    started, release = threading.Event(), threading.Event()

    class SlowPanes(Panes):
        def __call__(self, agent: str, project: str | None, history: int) -> dict[str, object]:
            if agent == "slow":
                started.set()
                release.wait(10)
            return super().__call__(agent, project, history)

    client = _socket_client(runtime, tmp_path, reads, SlowPanes())
    cap = remote_server.WS_PANE_SUBSCRIPTIONS_MAX
    with client.websocket_connect(f"{base(runtime)}/ws") as ws:
        ws.send_text(json.dumps({"subscribe": "slow"}))
        assert started.wait(10), "the capture of slow never began"
        ws.send_text(json.dumps({"unsubscribe": "slow"}))
        # The reader answers a subscription past the cap at once, so when the error frame
        # is here the unsubscribe sent before it has been read as well.
        for n in range(cap + 1):
            ws.send_text(json.dumps({"subscribe": f"coder-{n}"}))
        _until(ws, lambda f: f["type"] == "error")
        release.set()
        after = [_until(ws, lambda f: f["type"] == "pane") for _ in range(cap)]
    assert [frame["agent"] for frame in after] == [f"coder-{n}" for n in range(cap)]


def test_subscribe_board_picks_which_projects_board_frames_arrive(
    runtime: Runtime, reads: Reads, tmp_path: Path
) -> None:
    client = _socket_client(runtime, tmp_path, reads, Panes())
    is_board = lambda f: f["type"] == "board"  # noqa: E731
    with client.websocket_connect(f"{base(runtime)}/ws") as ws:
        ws.send_text(json.dumps({"subscribe_board": ""}))
        assert _until(ws, is_board)["payload"]["project"] is None
        ws.send_text(json.dumps({"subscribe_board": "prj_b"}))
        assert _until(ws, is_board)["payload"]["project"] == "prj_b"
        ws.send_text(json.dumps({"subscribe_board": None}))
        assert _until(ws, is_board)["payload"]["project"] is None, (
            "back to the current board, re-sent although it did not change"
        )
        ws.send_text(json.dumps({"subscribe_fleet": "prj_b"}))
        assert _until(ws, lambda f: f["type"] == "fleet")["payload"]["project"] == "prj_b"
        ws.send_text(json.dumps({"subscribe_fleet": None}))
        assert _until(ws, lambda f: f["type"] == "fleet")["payload"]["project"] is None


def test_a_board_frame_names_the_project_its_subscription_named(
    runtime: Runtime, tmp_path: Path
) -> None:
    """r4 2/9: under ``AISQUARE_TEAM_HUB`` every project's board is the hub's, so the board a
    frame carries names the hub's project, and the page, which went by that id, drew none.
    The frame names the subscription's project, as a pane frame does: none for the current
    project's, which no subscription named."""
    hub = {"project": {"id": "prj_hub"}, "sessions": [], "events": []}
    sources = dataclasses.replace(Reads().sources(), board=lambda project: hub)
    client = make_client(build_app(runtime, sources=sources, dist_dir=tmp_path, tick=0.02))
    assert unlock(client, runtime).status_code == 200
    is_board = lambda f: f["type"] == "board"  # noqa: E731
    with client.websocket_connect(f"{base(runtime)}/ws") as ws:
        ws.send_text(json.dumps({"subscribe_board": "prj_b"}))
        named = _until(ws, is_board)
        ws.send_text(json.dumps({"subscribe_board": None}))
        current = _until(ws, is_board)
    assert named["project"] == "prj_b" and named["payload"] == hub
    assert set(current) == {"type", "payload", "ts"} and current["payload"] == hub


def test_a_board_that_cannot_be_read_is_a_frame_that_says_why(
    runtime: Runtime, tmp_path: Path
) -> None:
    """r4 4/9: a board snapshot that raised was skipped and logged at debug level, and the
    page's Board tab said Loading… for as long as it was open: the orchestrator off, the
    project removed, the store locked. The frame says why now, and the board replaces it
    once it can be read again."""
    failing = [True]

    def board(project: str | None) -> object:
        if project == "nope":
            raise NoSuchProject("no project matches 'nope' (id prefix, name or codename)")
        if failing[0]:
            raise team_service.TeamDisabledError()
        return {"project": {"id": "prj_b"}, "sessions": [], "events": []}

    sources = dataclasses.replace(Reads().sources(), board=board)
    client = make_client(build_app(runtime, sources=sources, dist_dir=tmp_path, tick=0.02))
    assert unlock(client, runtime).status_code == 200
    is_board = lambda f: f["type"] == "board"  # noqa: E731
    with client.websocket_connect(f"{base(runtime)}/ws") as ws:
        ws.send_text(json.dumps({"subscribe_board": "prj_b"}))
        unread = _until(ws, is_board)
        failing[0] = False
        read = _until(ws, is_board)
        ws.send_text(json.dumps({"subscribe_board": "nope"}))
        gone = _until(ws, is_board)
    assert unread["project"] == "prj_b"
    assert unread["payload"] == {
        "project": None,
        "sessions": [],
        "events": [],
        "error": "the agent orchestrator is disabled (AISQUARE_TEAM=0)",
    }
    assert read["payload"]["project"] == {"id": "prj_b"} and "error" not in read["payload"]
    assert gone["project"] == "nope" and "no project matches 'nope'" in gone["payload"]["error"]


def _captures(panes: Panes, pane: tuple[str, str | None], count: int) -> None:
    """Wait for ``pane``'s ``count``-th capture: a tick each, and a tick reads its board,
    when its socket wants one, before it captures any pane."""
    deadline = time.monotonic() + 10
    while panes.asked.count(pane) < count:
        assert time.monotonic() < deadline, f"{pane} captured {panes.asked.count(pane)} times"
        time.sleep(0.005)


def test_no_board_is_read_or_sent_until_the_socket_asks_and_none_once_it_stops(
    runtime: Runtime, reads: Reads, tmp_path: Path
) -> None:
    """r3 #6: a board is every session and task its project ever had, and it changes with
    every session's heartbeat. Every socket was sent the current project's from the moment
    it opened, and again on each change, whatever screen the phone showed: over mobile
    data, while only the page's Board tab draws it. Now no board is read or sent until
    ``subscribe_board``, and ``{"subscribe_board": false}`` stops them again."""
    panes = Panes()
    client = _socket_client(runtime, tmp_path, reads, panes)

    def boards() -> list[tuple[str, object]]:
        return [call for call in reads.calls if call[0] == "board"]

    with client.websocket_connect(f"{base(runtime)}/ws") as ws:
        ws.send_text(json.dumps({"subscribe": "first"}))
        _captures(panes, ("first", None), 3)
        unasked = boards()
        ws.send_text(json.dumps({"subscribe_board": None}))
        asked = _until(ws, lambda f: f["type"] == "board")
        ws.send_text(json.dumps({"subscribe_board": False}))
        ws.send_text(json.dumps({"subscribe": "second"}))
        _until(ws, _pane("second"))  # read in order: every tick from the next one on stopped
        reads.calls.clear()
        _captures(panes, ("second", None), panes.asked.count(("second", None)) + 3)
        stopped = boards()
        ws.send_text(json.dumps({"subscribe": "third"}))
        later = [frame_within(ws)]
        while not _pane("third")(later[-1]):
            later.append(frame_within(ws))
    assert unasked == [], "a socket that never asked was read a board on every tick"
    assert asked["payload"]["project"] is None
    assert stopped == [], "a socket that said false was still read a board"
    assert [frame for frame in later if frame["type"] == "board"] == []


def test_no_fleet_is_read_or_sent_until_the_socket_asks_and_none_once_it_stops(
    runtime: Runtime, reads: Reads, tmp_path: Path
) -> None:
    """r4 7/9: every socket was read the current project's fleet every tick from the moment
    it opened, a ``fleet ls`` each (tmux, the store, a write for a pane found dead), whatever
    screen the phone showed, though only the page's project and agent screens draw it. Now
    no fleet is read or sent until ``subscribe_fleet``, and ``{"subscribe_fleet": false}``
    stops them again, as for the board."""
    panes = Panes()
    client = _socket_client(runtime, tmp_path, reads, panes)

    def fleets() -> list[tuple[str, object]]:
        return [call for call in reads.calls if call[0] == "fleet"]

    with client.websocket_connect(f"{base(runtime)}/ws") as ws:
        ws.send_text(json.dumps({"subscribe": "first"}))
        _captures(panes, ("first", None), 3)
        unasked = fleets()
        ws.send_text(json.dumps({"subscribe_fleet": "prj_b"}))
        asked = _until(ws, lambda f: f["type"] == "fleet")
        ws.send_text(json.dumps({"subscribe_fleet": False}))
        ws.send_text(json.dumps({"subscribe": "second"}))
        _until(ws, _pane("second"))  # read in order: every tick from the next one on stopped
        reads.calls.clear()
        _captures(panes, ("second", None), panes.asked.count(("second", None)) + 3)
        stopped = fleets()
        ws.send_text(json.dumps({"subscribe": "third"}))
        later = [frame_within(ws)]
        while not _pane("third")(later[-1]):
            later.append(frame_within(ws))
    assert unasked == [], "a socket that never asked was read a fleet on every tick"
    assert asked["payload"]["project"] == "prj_b"
    assert stopped == [], "a socket that said false was still read a fleet"
    assert [frame for frame in later if frame["type"] == "fleet"] == []


def test_a_false_that_lands_while_a_board_is_read_sends_no_board_frame(
    runtime: Runtime, tmp_path: Path
) -> None:
    """A tick was reading the board when ``{"subscribe_board": false}`` came, and the board
    went out once the read was back: every session and task once more, to a page that had
    left its Board tab. The tick looks at what the socket wants after the read too."""
    reading, release = threading.Event(), threading.Event()

    def board(project: str | None) -> object:
        reading.set()
        release.wait(5)
        return {"project": {"id": "p1"}, "sessions": [], "events": []}

    sources = dataclasses.replace(Reads().sources(), board=board, panes=Panes())
    client = make_client(build_app(runtime, sources=sources, dist_dir=tmp_path, tick=0.02))
    assert unlock(client, runtime).status_code == 200
    with client.websocket_connect(f"{base(runtime)}/ws") as ws:
        ws.send_text(json.dumps({"subscribe_board": None}))
        assert reading.wait(5), "a tick is reading the board"
        ws.send_text(json.dumps({"subscribe_board": False}))
        time.sleep(0.2)  # the reader takes the false while that read still runs
        release.set()
        ws.send_text(json.dumps({"subscribe": "after"}))
        frames = [frame_within(ws)]
        while not _pane("after")(frames[-1]):
            frames.append(frame_within(ws))
    assert [frame["type"] for frame in frames if frame["type"] == "board"] == []


def test_a_board_switch_while_a_board_is_read_never_sends_the_old_projects_board(
    runtime: Runtime, tmp_path: Path
) -> None:
    """The board's twin of the next test, which only the fleet had: a tick reading the
    current project's board when ``{"subscribe_board": "prj_b"}`` landed would let that
    board out as prj_b's once the read was back. The tick sends a board only for the
    project still asked for, as it sends a fleet."""
    reading, release = threading.Event(), threading.Event()
    hold = [False]

    def board(project: str | None) -> object:
        if project is None and hold[0]:
            reading.set()
            release.wait(5)
        return {"project": project, "sessions": [], "events": []}

    sources = dataclasses.replace(Reads().sources(), board=board)
    client = make_client(build_app(runtime, sources=sources, dist_dir=tmp_path, tick=0.02))
    assert unlock(client, runtime).status_code == 200
    is_board = lambda f: f["type"] == "board"  # noqa: E731
    with client.websocket_connect(f"{base(runtime)}/ws") as ws:
        ws.send_text(json.dumps({"subscribe_board": None}))
        assert _until(ws, is_board)["payload"]["project"] is None
        hold[0] = True
        assert reading.wait(5), "a tick is reading the current project's board"
        ws.send_text(json.dumps({"subscribe_board": "prj_b"}))
        time.sleep(0.2)  # the reader takes the switch while that read still runs
        release.set()
        assert _until(ws, is_board)["payload"]["project"] == "prj_b"


def test_a_switch_while_a_snapshot_is_read_never_sends_the_old_projects_frame(
    runtime: Runtime, tmp_path: Path
) -> None:
    """The tick read the current project's fleet, the switch landed meanwhile and dropped
    the last frame, and the old project's frame went out as if it were the new one's: the
    page showed it for a tick, and ``test_subscribe_board_picks_which_projects_board_frames_arrive``
    failed about one run in six."""
    reading, release = threading.Event(), threading.Event()
    hold = [False]

    def fleet(project: str | None) -> object:
        if project is None and hold[0]:
            reading.set()
            release.wait(5)
        return {"kind": "fleet", "project": project}

    sources = dataclasses.replace(Reads().sources(), fleet=fleet)
    client = make_client(build_app(runtime, sources=sources, dist_dir=tmp_path, tick=0.02))
    assert unlock(client, runtime).status_code == 200
    is_fleet = lambda f: f["type"] == "fleet"  # noqa: E731
    with client.websocket_connect(f"{base(runtime)}/ws") as ws:
        ws.send_text(json.dumps({"subscribe_fleet": None}))
        assert _until(ws, is_fleet)["payload"]["project"] is None
        hold[0] = True
        assert reading.wait(5), "a tick is reading the current project's fleet"
        ws.send_text(json.dumps({"subscribe_fleet": "prj_b"}))
        time.sleep(0.2)  # the reader takes the switch while that read still runs
        release.set()
        assert _until(ws, is_fleet)["payload"]["project"] == "prj_b"


def test_a_ninth_pane_subscription_is_refused_with_an_error_frame(
    runtime: Runtime, reads: Reads, tmp_path: Path
) -> None:
    panes = Panes()
    client = _socket_client(runtime, tmp_path, reads, panes)
    cap = remote_server.WS_PANE_SUBSCRIPTIONS_MAX
    with client.websocket_connect(f"{base(runtime)}/ws") as ws:
        for n in range(cap):
            ws.send_text(json.dumps({"subscribe": f"coder-{n}"}))
        ws.send_text(json.dumps({"subscribe": "coder-0"}))  # already one: no new slot
        ws.send_text(json.dumps({"subscribe": f"coder-{cap}"}))
        refused = _until(ws, lambda f: f["type"] == "error")
        ws.send_text(json.dumps({"unsubscribe": "coder-0"}))
        ws.send_text(json.dumps({"subscribe": f"coder-{cap}"}))
        _until(ws, _pane(f"coder-{cap}"))
    assert refused["payload"]["error"] == "too_many_subscriptions"
    assert str(cap) in refused["payload"]["message"]


def test_a_client_message_over_4096_characters_is_ignored(
    runtime: Runtime, reads: Reads, tmp_path: Path
) -> None:
    panes = Panes()
    client = _socket_client(runtime, tmp_path, reads, panes)
    edge = json.dumps({"subscribe": "edge"})
    edge = edge[:-1] + " " * (remote_server.WS_CLIENT_MESSAGE_MAX - len(edge)) + "}"
    assert len(edge) == remote_server.WS_CLIENT_MESSAGE_MAX
    with client.websocket_connect(f"{base(runtime)}/ws") as ws:
        ws.send_text(json.dumps({"subscribe": "x" * remote_server.WS_CLIENT_MESSAGE_MAX}))
        ws.send_bytes(json.dumps({"subscribe": "bytes"}).encode())
        ws.send_text(edge)
        first = _until(ws, lambda f: f["type"] == "pane")
    assert first["agent"] == "edge", "the long message and the binary one were never read"
    assert all(agent == "edge" for agent, _project in panes.asked)


def test_a_client_message_nested_too_deep_to_parse_is_ignored_and_the_socket_reads_on(
    runtime: Runtime, reads: Reads, tmp_path: Path
) -> None:
    """4 096 ``[`` fit the size cap and are past what ``json`` recurses into on 3.11, the
    CI floor, where they raise RecursionError rather than ValueError. That once ended
    the socket's reader; now they are ignored like any other message that is not JSON.
    Every read is bounded: a reader that died would leave the socket silent, not closed."""
    client = _socket_client(runtime, tmp_path, reads, Panes())
    with client.websocket_connect(f"{base(runtime)}/ws") as ws:
        ws.send_text("[" * remote_server.WS_CLIENT_MESSAGE_MAX)
        ws.send_text(json.dumps({"subscribe": "after"}))
        kinds: list[str] = []
        while "pane" not in kinds:
            assert len(kinds) < 20, kinds
            message = receive_within(ws)
            assert message["type"] == "websocket.send", f"the socket ended: {message}"
            frame = json.loads(message["text"])
            kinds.append(frame["type"])
    assert frame["agent"] == "after"
