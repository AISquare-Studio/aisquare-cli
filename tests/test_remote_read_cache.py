"""The read cache: one snapshot per kind per tick, and no kind waits for another.

Every cached read goes through it, and every socket's board and fleet frames. The
snapshots were computed under the one lock that guards its table, so the slowest
held up all the others: ``projects`` lists every project's fleet, a tmux call
each, and a tmux that stops answering costs 30 s a call (review of #243, round 2).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from aisquare.services import remote_server
from aisquare.services.remote_server import Snapshot, Sources, build_app
from tests.remote_kit_helpers import base, make_client, make_runtime, unlock


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


def _sources(projects: Snapshot) -> Sources:
    return Sources(
        projects=projects,
        fleet=lambda project: {"agents": []},
        board=lambda project: {},
        tasks=lambda project: [],
        memory=lambda project: [],
        panes=lambda agent, project, history: {"rows": [], "width": 0, "height": 0},
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
