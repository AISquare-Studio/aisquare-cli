"""The read cache: one snapshot per kind per tick, and no kind waits for another.

Every cached read goes through it, and every socket's board and fleet frames. The
snapshots were computed under the one lock that guards its table, so the slowest
held up all the others: ``projects`` lists every project's fleet, a tmux call
each, and a tmux that stops answering costs 30 s a call. And the stream's pane
frames go through it too: every socket captured the panes it watched itself
(review of #243, round 2).
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from aisquare.services import remote_server
from aisquare.services.remote_server import PaneSource, Snapshot, Sources, build_app
from tests.remote_kit_helpers import base, frame_within, make_client, make_runtime, unlock


def _wait_for(done: Callable[[], bool], seconds: float = 5.0) -> None:
    deadline = time.monotonic() + seconds
    while not done():
        assert time.monotonic() < deadline, f"not done in {seconds} s"
        time.sleep(0.01)


# --- one kind at a time, and only its own callers wait -----------------------------------------


def test_a_slow_snapshot_holds_up_only_the_callers_of_its_own_kind() -> None:
    cache = remote_server._Cache(ttl=60.0)
    started, release = threading.Event(), threading.Event()

    def projects() -> str:
        started.set()
        release.wait(timeout=10)
        return "projects"

    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            slow = pool.submit(cache.cached_snapshot, "projects:", projects)
            assert started.wait(timeout=5)
            board = pool.submit(cache.cached_snapshot, "board:", lambda: "board")
            assert board.result(timeout=2) == "board", "the board waited for projects"
        finally:
            release.set()
        assert slow.result(timeout=5) == "projects"


def test_the_callers_of_one_kind_wait_for_its_one_snapshot() -> None:
    """What the lock is for: one computation per kind per tick, however many ask at once."""
    cache = remote_server._Cache(ttl=60.0)
    computed: list[str] = []
    release = threading.Event()

    def fleet() -> str:
        computed.append("fleet")
        release.wait(timeout=10)
        return "fleet"

    with ThreadPoolExecutor(max_workers=3) as pool:
        try:
            asked = [pool.submit(cache.cached_snapshot, "fleet:", fleet) for _ in range(3)]
            _wait_for(lambda: computed == ["fleet"])
            _wait_for(lambda: cache._turns["fleet:"].callers == 3)  # the other two wait for it
        finally:
            release.set()
        assert [ask.result(timeout=5) for ask in asked] == ["fleet"] * 3
    assert computed == ["fleet"]
    assert cache._turns == {}, "a kind's turn goes with its last caller"


def test_a_snapshot_that_fails_leaves_the_next_caller_to_compute_it() -> None:
    cache = remote_server._Cache(ttl=60.0)

    def broken() -> object:
        raise RuntimeError("tmux did not answer")

    with pytest.raises(RuntimeError, match="tmux did not answer"):
        cache.cached_snapshot("fleet:", broken)
    assert cache.cached_snapshot("fleet:", lambda: "fleet") == "fleet"
    assert cache._turns == {}


def test_the_turns_hold_only_the_kinds_in_flight_whatever_kinds_are_asked_for() -> None:
    """A kind is whatever spelling of ``?project=`` a client sent: the turns, like the
    snapshots, must not keep one per spelling."""
    cache = remote_server._Cache(ttl=0.0)
    for spelling in range(500):
        cache.cached_snapshot(f"fleet:{spelling}", lambda: "fleet")
    assert cache._turns == {}


# --- what a phone saw -----------------------------------------------------------------------------


def _sources(
    projects: Snapshot = lambda: [],
    panes: PaneSource = lambda agent, project, history: {"rows": [], "width": 0, "height": 0},
) -> Sources:
    return Sources(
        projects=projects,
        fleet=lambda project: {"agents": []},
        board=lambda project: {},
        tasks=lambda project: [],
        memory=lambda project: [],
        panes=panes,
        explainability=lambda agent, project: {"available": False},
    )


def test_the_fleet_answers_while_the_projects_screen_is_still_being_read(
    isolated_home: Path, tmp_path: Path
) -> None:
    """The Projects screen polls every 15 s, and its read held the cache's lock throughout:
    ``GET api/fleet`` waited for it, as did every socket's board and fleet frames."""
    started, release = threading.Event(), threading.Event()

    def projects() -> object:
        started.set()
        release.wait(timeout=10)
        return []

    runtime = make_runtime()
    client = make_client(build_app(runtime, sources=_sources(projects), dist_dir=tmp_path))
    assert unlock(client, runtime).status_code == 200
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            listed = pool.submit(client.get, f"{base(runtime)}/api/projects")
            assert started.wait(timeout=5)
            fleet = pool.submit(client.get, f"{base(runtime)}/api/fleet")
            assert fleet.result(timeout=2).json() == {"agents": []}, "it waited for projects"
        finally:
            release.set()
        assert listed.result(timeout=5).json() == []


# --- the stream's pane frames ---------------------------------------------------------------------


def _until(ws: Any, kind: str) -> dict[str, Any]:
    for _ in range(40):
        frame = frame_within(ws)
        if frame["type"] == kind:
            return frame
    raise AssertionError(f"no {kind} frame in 40")


@pytest.mark.parametrize("gone", [False, True], ids=["a capture", "an agent that is gone"])
def test_sockets_watching_one_pane_share_its_capture(
    isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gone: bool
) -> None:
    """Board and fleet went through the cache, and every socket captured its panes itself:
    two phones on the same cards, or a second tab, captured each pane twice a tick, a tmux
    process and a store open each, on a pool of four threads. A failure is a frame too, and
    shared as a capture is."""
    captured: list[tuple[str, str | None, int]] = []
    asked: list[str] = []
    release = threading.Event()

    class Recorded(remote_server._Cache):
        def cached_snapshot(self, kind: str, compute: Snapshot) -> object:
            asked.append(kind)
            return super().cached_snapshot(kind, compute)

    def panes(agent: str, project: str | None, history: int) -> dict[str, object]:
        captured.append((agent, project, history))
        release.wait(timeout=10)
        if gone:
            raise remote_server.NoSuchAgent(f"no live agent {agent!r}")
        return {"rows": [f"capture {len(captured)}"], "width": 80, "height": 1}

    monkeypatch.setattr(remote_server, "_Cache", Recorded)
    runtime = make_runtime()
    app = build_app(runtime, sources=_sources(panes=panes), dist_dir=tmp_path, tick=0.5)
    client = make_client(app)
    assert unlock(client, runtime).status_code == 200
    url = f"{base(runtime)}/ws"
    with client.websocket_connect(url) as first, client.websocket_connect(url) as second:
        try:
            for ws in (first, second):
                _until(ws, "remote")
                ws.send_text(json.dumps({"subscribe": "coder-1"}))
            # Both sockets have asked for the pane: of the cache, or of tmux itself.
            _wait_for(lambda: sum(k.startswith("pane:") for k in asked) >= 2 or len(captured) >= 2)
            while_held = list(captured)
        finally:
            release.set()
        frames = [_until(ws, "pane") for ws in (first, second)]
    assert while_held == [("coder-1", None, 0)], "one capture, which the other socket waited for"
    shared: dict[str, object] = (
        {"rows": [], "width": 0, "height": 0, "error": "no live agent 'coder-1'"}
        if gone
        else {"rows": ["capture 1"], "width": 80, "height": 1}
    )
    assert [frame["payload"] for frame in frames] == [shared, shared]


def test_a_shared_capture_is_still_one_projects_pane(isolated_home: Path, tmp_path: Path) -> None:
    """The same label in two projects is two agents, and a ':' in a ref or a label must
    not make two subscriptions one cached capture."""

    def panes(agent: str, project: str | None, history: int) -> dict[str, object]:
        return {"rows": [f"{project}/{agent}"], "width": 80, "height": 1}

    runtime = make_runtime()
    app = build_app(runtime, sources=_sources(panes=panes), dist_dir=tmp_path, tick=0.05)
    client = make_client(app)
    assert unlock(client, runtime).status_code == 200
    wanted = [("prj_a", "coder-1"), ("prj_b", "coder-1"), ("a:b", "c"), ("a", "b:c")]
    with client.websocket_connect(f"{base(runtime)}/ws") as ws:
        for project, label in wanted:
            ws.send_text(json.dumps({"subscribe": label, "project": project}))
        seen: dict[tuple[str, str], list[str]] = {}
        while len(seen) < len(wanted):
            frame = _until(ws, "pane")
            seen[(frame["project"], frame["agent"])] = frame["payload"]["rows"]
    assert seen == {(project, label): [f"{project}/{label}"] for project, label in wanted}
