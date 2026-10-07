"""Every read takes ``?project``, and nothing is ever answered from another project's cache.

Board, tasks, memory and explainability (and the note write) used to be pinned
to the project the server was started in, while fleet, panes and send-keys
already took a project: the page could show project B's agents and then B's
coder-1's card would be A's coder-1's, or a 404 (review of #243, finding 5).
The cache keyed each kind once, so even a project-aware read could be served
the other project's snapshot within the same tick.
"""

from __future__ import annotations

import dataclasses
import json
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
from tests.remote_kit_helpers import base, make_client, make_runtime, receive_within, unlock

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
    """A lookup failure is a 404; only a failure of the card itself is an unavailable card."""
    client = _client(runtime, reads, tmp_path)
    url = f"{base(runtime)}/api/explainability"
    for path, params in (("coder-1", {"project": "nope"}), ("ghost", {})):
        response = client.get(f"{url}/{path}", params=params)
        assert response.status_code == 404, path
        assert response.json()["error"] == "not_found"


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
        frame: dict[str, Any] = json.loads(ws.receive_text())
        if match(frame):
            return frame
    raise AssertionError(f"no matching frame in {limit}")


def _pane(agent: str, project: str | None = None) -> Any:
    return lambda f: f["type"] == "pane" and f["agent"] == agent and f.get("project") == project


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
    assert ("coder-1", "prj_a") not in panes.asked, "unsubscribed: no longer captured"
    assert ("coder-1", "prj_b") in panes.asked


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
        assert _until(ws, is_board)["payload"] == {"kind": "board", "project": None}
        ws.send_text(json.dumps({"subscribe_board": "prj_b"}))
        assert _until(ws, is_board)["payload"] == {"kind": "board", "project": "prj_b"}
        ws.send_text(json.dumps({"subscribe_board": None}))
        assert _until(ws, is_board)["payload"] == {"kind": "board", "project": None}, (
            "back to the current board, re-sent although it did not change"
        )
        ws.send_text(json.dumps({"subscribe_fleet": "prj_b"}))
        assert _until(ws, lambda f: f["type"] == "fleet")["payload"]["project"] == "prj_b"
        ws.send_text(json.dumps({"subscribe_fleet": None}))
        assert _until(ws, lambda f: f["type"] == "fleet")["payload"]["project"] is None


def test_a_switch_while_a_snapshot_is_read_never_sends_the_old_projects_frame(
    runtime: Runtime, tmp_path: Path
) -> None:
    """The tick read the current project's fleet, the switch landed meanwhile and dropped
    the last frame, and the old project's frame went out as if it were the new one's: the
    page showed it for a tick, and the test above failed about one run in six."""
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
