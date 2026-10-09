"""The read cache: one snapshot per kind per tick, and no kind waits for another.

Every cached read goes through it, and every socket's board and fleet frames. The
snapshots were computed under the one lock that guards its table, so the slowest
held up all the others: ``projects`` lists every project's fleet, a tmux call
each, and a tmux that stops answering costs 30 s a call. And the stream's pane
frames go through it too: every socket captured the panes it watched itself
(review of #243, round 2).

A snapshot that raised was no one's answer but its own caller's: every caller
waiting on it computed it again in turn, each in a thread of the default pool,
which every unlock, write and transcript read waits on too (round 3).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest

from aisquare.services import remote_server
from aisquare.services.remote_server import (
    PaneSource,
    ProjectSource,
    Runtime,
    Snapshot,
    Sources,
    build_app,
)
from tests.remote_kit_helpers import (
    ORIGIN,
    base,
    frame_within,
    make_client,
    make_runtime,
    unlock,
)


def _wait_for(done: Callable[[], bool], seconds: float = 5.0) -> None:
    deadline = time.monotonic() + seconds
    while not done():
        assert time.monotonic() < deadline, f"not done in {seconds} s"
        time.sleep(0.01)


async def _eventually(done: Callable[[], bool], seconds: float = 5.0) -> None:
    """:func:`_wait_for` on the event loop, which it leaves free meanwhile."""
    deadline = time.monotonic() + seconds
    while not done():
        assert time.monotonic() < deadline, f"not done in {seconds} s"
        await asyncio.sleep(0.01)


class Ticks:
    """The cache's monotonic clock, moved by hand."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _ask(cache: remote_server._Cache, kind: str, compute: Snapshot) -> object:
    """One caller of ``kind``, on an event loop of its own."""
    return asyncio.run(cache.cached_snapshot(kind, compute))


# --- one kind at a time, and only its own callers wait -----------------------------------------


def test_a_slow_snapshot_holds_up_only_the_callers_of_its_own_kind() -> None:
    cache = remote_server._Cache(ttl=60.0)
    started, release = threading.Event(), threading.Event()

    def projects() -> str:
        started.set()
        release.wait(timeout=10)
        return "projects"

    async def read() -> None:
        slow = asyncio.ensure_future(cache.cached_snapshot("projects:", projects))
        try:
            await _eventually(started.is_set)
            board = cache.cached_snapshot("board:", lambda: "board")
            assert await asyncio.wait_for(board, 2) == "board", "the board waited for projects"
        finally:
            release.set()
        assert await slow == "projects"

    asyncio.run(read())


def test_the_callers_of_one_kind_wait_for_its_one_snapshot() -> None:
    """What the flights are for: one computation per kind per tick, however many ask at once."""
    cache = remote_server._Cache(ttl=60.0)
    computed: list[str] = []
    release = threading.Event()

    def fleet() -> str:
        computed.append("fleet")
        release.wait(timeout=10)
        return "fleet"

    async def read() -> list[object]:
        asked = [asyncio.ensure_future(cache.cached_snapshot("fleet:", fleet)) for _ in range(3)]
        try:
            await asyncio.sleep(0)  # each has asked: the first is computing, the others wait
            await _eventually(lambda: computed == ["fleet"])
            assert list(cache._flights) == ["fleet:"]
        finally:
            release.set()
        return list(await asyncio.gather(*asked))

    assert asyncio.run(read()) == ["fleet"] * 3
    assert computed == ["fleet"]
    assert cache._flights == {}, "a kind's flight goes when its compute ends"


def test_the_callers_waiting_on_a_snapshot_that_fails_share_its_one_failure() -> None:
    """r3 #1, as measured: four callers of a kind whose compute took 0.5 s and raised ran it
    four times, one after another, and the last waited 2 s. A store locked past its
    ``busy_timeout`` is that compute, 5 s each."""
    cache = remote_server._Cache(ttl=60.0)
    computed: list[str] = []

    def locked() -> object:
        computed.append("fleet")
        time.sleep(0.2)
        raise RuntimeError("database is locked")

    async def ask() -> str:
        try:
            await cache.cached_snapshot("fleet:", locked)
        except RuntimeError as exc:
            return str(exc)
        return "no failure"

    async def read() -> list[str]:
        return list(await asyncio.gather(*(ask() for _ in range(4))))

    assert asyncio.run(read()) == ["database is locked"] * 4
    assert computed == ["fleet"], "each caller waiting on it computed it again"
    assert cache._flights == {}


def test_a_snapshot_that_fails_is_its_ticks_answer_and_the_next_tick_computes_it_again() -> None:
    """A failure is kept for the tick as a value is, and for no longer."""
    ticks = Ticks()
    cache = remote_server._Cache(ttl=0.9, clock=ticks)
    computed: list[str] = []

    def broken() -> object:
        computed.append("broken")
        raise RuntimeError("tmux did not answer")

    def fleet() -> object:
        computed.append("fleet")
        return "fleet"

    with pytest.raises(RuntimeError, match="tmux did not answer"):
        _ask(cache, "fleet:", broken)
    with pytest.raises(RuntimeError, match="tmux did not answer"):
        _ask(cache, "fleet:", fleet)
    assert computed == ["broken"], "within its tick, the failure is the answer"
    ticks.now += 1.0
    assert _ask(cache, "fleet:", fleet) == "fleet"
    assert computed == ["broken", "fleet"]
    assert cache._flights == {}


def test_a_failure_raised_to_every_caller_keeps_the_traceback_it_was_raised_with() -> None:
    """Raised again as it is, one exception would carry every caller's frames, one more set
    each time it is raised, for as long as the tick keeps it."""
    cache = remote_server._Cache(ttl=60.0)

    def broken() -> object:
        raise RuntimeError("tmux did not answer")

    depths = []
    for _ in range(3):
        with pytest.raises(RuntimeError) as raised:
            _ask(cache, "fleet:", broken)
        depths.append(len(raised.traceback))
    assert depths[0] == depths[1] == depths[2]


@pytest.mark.parametrize("given", [False, True], ids=["the default pool", "a pool of its own"])
def test_callers_waiting_on_a_kind_take_no_thread_of_the_pool_it_is_computed_on(
    given: bool,
) -> None:
    """Only a compute takes a thread. Each caller waited in one, so a few waiting on one slow
    kind held every thread of its pool: the default pool, which every unlock, write and
    transcript read waits on too, or the stream's four pane threads."""
    cache = remote_server._Cache(ttl=60.0)
    started, release = threading.Event(), threading.Event()

    def fleet() -> str:
        started.set()
        release.wait(timeout=10)
        return "fleet"

    async def read(pool: ThreadPoolExecutor) -> list[object]:
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=2))
        on = pool if given else None
        waiting = [
            asyncio.ensure_future(cache.cached_snapshot("fleet:", fleet, on)) for _ in range(6)
        ]
        try:
            await _eventually(started.is_set)
            board = cache.cached_snapshot("board:", lambda: "board", on)
            assert await asyncio.wait_for(board, 2) == "board", "the waiters held the pool"
            unlocked = loop.run_in_executor(on, lambda: "unlocked")
            assert await asyncio.wait_for(unlocked, 2) == "unlocked"
        finally:
            release.set()
        return list(await asyncio.gather(*waiting))

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert asyncio.run(read(pool)) == ["fleet"] * 6


def test_a_caller_whose_wait_is_cancelled_leaves_the_others_their_snapshot() -> None:
    """A socket that closes cancels its own wait, never the compute the others wait on."""
    cache = remote_server._Cache(ttl=60.0)
    computed: list[str] = []
    release = threading.Event()

    def fleet() -> str:
        computed.append("fleet")
        release.wait(timeout=10)
        return "fleet"

    async def read() -> object:
        first = asyncio.ensure_future(cache.cached_snapshot("fleet:", fleet))
        second = asyncio.ensure_future(cache.cached_snapshot("fleet:", fleet))
        await _eventually(lambda: computed == ["fleet"])
        first.cancel()
        await asyncio.sleep(0.05)
        release.set()
        return await asyncio.wait_for(second, 5)

    assert asyncio.run(read()) == "fleet"
    assert computed == ["fleet"]


def test_a_compute_whose_pool_shut_down_before_it_ran_ends_every_wait_on_it() -> None:
    """The lifespan shuts the pane pool down with what is queued on it: a capture that never
    ran must still end the waits on it, or a socket still ticking waits for ever."""
    cache = remote_server._Cache(ttl=60.0)
    release = threading.Event()

    async def read() -> list[object]:
        pool = ThreadPoolExecutor(max_workers=1)
        busy = asyncio.get_running_loop().run_in_executor(pool, release.wait, 10)
        queued = [
            asyncio.ensure_future(cache.cached_snapshot("pane:", lambda: "screen", pool))
            for _ in range(2)
        ]
        await asyncio.sleep(0.05)
        pool.shutdown(wait=False, cancel_futures=True)
        release.set()
        await busy
        return list(await asyncio.wait_for(asyncio.gather(*queued, return_exceptions=True), 5))

    answers = asyncio.run(read())
    assert [type(answer) for answer in answers] == [RuntimeError, RuntimeError]
    assert "thread pool shut down" in str(answers[1])
    assert cache._flights == {}
    with ThreadPoolExecutor(max_workers=1) as pool:
        again = cache.cached_snapshot("pane:", lambda: "screen", pool)
        assert asyncio.run(again) == "screen", "a failure the pool caused is not kept"


def test_the_flights_hold_only_the_kinds_in_flight_whatever_kinds_are_asked_for() -> None:
    """A kind is whatever spelling of ``?project=`` a client sent: the flights, like the
    snapshots, must not keep one per spelling."""
    cache = remote_server._Cache(ttl=0.0)

    async def read() -> None:
        for spelling in range(500):
            await cache.cached_snapshot(f"fleet:{spelling}", lambda: "fleet")

    asyncio.run(read())
    assert cache._flights == {}


# --- what a phone saw -----------------------------------------------------------------------------


def _sources(
    projects: Snapshot = lambda: [],
    panes: PaneSource = lambda agent, project, history: {"rows": [], "width": 0, "height": 0},
    fleet: ProjectSource = lambda project: {"agents": []},
) -> Sources:
    return Sources(
        projects=projects,
        fleet=fleet,
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


def _phone(app: Any, runtime: Runtime) -> httpx.AsyncClient:
    """An unlocked phone on the test's own event loop, as every request shares uvicorn's.

    ``TestClient`` gives each request and each socket a loop and a default pool of
    its own, where nothing one waits on can hold up another."""
    client = make_client(app)
    assert unlock(client, runtime).status_code == 200
    cookie = f"{remote_server.COOKIE}={client.cookies.get(remote_server.COOKIE)}"
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"origin": ORIGIN, "cookie": cookie},
    )


def test_reads_waiting_on_a_snapshot_that_fails_share_its_one_failure(
    isolated_home: Path, tmp_path: Path
) -> None:
    """r3 #1: phones reading one project's fleet while its store is locked. Each read waiting
    on the snapshot computed it again when the one before it failed, so the last of N waited
    N x the 5 s ``busy_timeout`` for its 503."""
    computed: list[str] = []

    def fleet(project: str | None) -> object:
        computed.append("fleet")
        time.sleep(0.2)
        raise RuntimeError("database is locked")

    runtime = make_runtime()
    app = build_app(runtime, sources=_sources(fleet=fleet), dist_dir=tmp_path)

    async def read() -> list[httpx.Response]:
        async with _phone(app, runtime) as phone:
            url = f"{base(runtime)}/api/fleet"
            return list(await asyncio.gather(*(phone.get(url) for _ in range(5))))

    answers = asyncio.run(read())
    assert [answer.status_code for answer in answers] == [503] * 5
    assert {answer.json()["message"] for answer in answers} == {"database is locked"}
    assert computed == ["fleet"], "each read waiting on it computed it again"


def test_reads_waiting_on_a_slow_snapshot_hold_no_thread_of_the_default_pool(
    isolated_home: Path, tmp_path: Path
) -> None:
    """r3 #1: each read waiting on a snapshot held a thread of the loop's default pool while
    it waited, and so held up every read, unlock and write behind it: here a pool of two,
    which on a four-core machine is eight."""
    started, release = threading.Event(), threading.Event()

    def fleet(project: str | None) -> object:
        started.set()
        release.wait(timeout=10)
        return {"agents": []}

    runtime = make_runtime()
    app = build_app(runtime, sources=_sources(fleet=fleet), dist_dir=tmp_path)

    async def read() -> tuple[list[httpx.Response], httpx.Response]:
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=2))
        async with _phone(app, runtime) as phone:
            url = f"{base(runtime)}/api/fleet"
            fleets = [asyncio.ensure_future(phone.get(url)) for _ in range(5)]
            try:
                await _eventually(started.is_set)
                await asyncio.sleep(0.1)  # every fleet read is waiting on the one compute
                board = await asyncio.wait_for(phone.get(f"{base(runtime)}/api/board"), 2)
            finally:
                release.set()
            return list(await asyncio.gather(*fleets)), board

    fleets, board = asyncio.run(read())
    assert board.status_code == 200, "the board read waited behind the fleet reads"
    assert [answer.json() for answer in fleets] == [{"agents": []}] * 5


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
        async def cached_snapshot(
            self, kind: str, compute: Snapshot, pool: Any | None = None
        ) -> object:
            asked.append(kind)
            return await super().cached_snapshot(kind, compute, pool)

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


def test_sockets_waiting_on_one_slow_capture_leave_the_pane_threads_to_other_panes(
    isolated_home: Path, tmp_path: Path
) -> None:
    """r3 #1, for the pane frames: each socket waiting on a capture another socket was taking
    held one of the stream's four pane threads while it waited. Four sockets on one slow
    pane held all four, and no other pane was captured for any socket until it answered."""
    captured: list[str] = []
    release = threading.Event()

    def panes(agent: str, project: str | None, history: int) -> dict[str, object]:
        captured.append(agent)
        if agent == "slow":
            release.wait(timeout=10)
        return {"rows": [agent], "width": 80, "height": 1}

    runtime = make_runtime()
    app = build_app(runtime, sources=_sources(panes=panes), dist_dir=tmp_path, tick=0.05)
    url = f"{base(runtime)}/ws"
    watching, other = make_client(app), make_client(app)  # two phones: four sockets each at most
    assert unlock(watching, runtime).status_code == unlock(other, runtime).status_code == 200
    with contextlib.ExitStack() as sockets:
        try:
            for _ in range(remote_server.PANE_CAPTURE_WORKERS):
                ws = sockets.enter_context(watching.websocket_connect(url))
                _until(ws, "remote")
                ws.send_text(json.dumps({"subscribe": "slow"}))
            _wait_for(lambda: "slow" in captured)
            time.sleep(0.3)  # every socket's tick is waiting on the one slow capture now
            quick = sockets.enter_context(other.websocket_connect(url))
            _until(quick, "remote")
            quick.send_text(json.dumps({"subscribe": "quick"}))
            deadline = time.monotonic() + 3
            frame = frame_within(quick, 3)
            while frame["type"] != "pane":
                frame = frame_within(quick, max(0.1, deadline - time.monotonic()))
        finally:
            release.set()
    assert frame["payload"]["rows"] == ["quick"]
    assert captured.count("slow") >= 1


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


# --- the stream's change detection ---------------------------------------------------------------


def test_a_payload_that_did_not_change_is_not_encoded_again_to_find_that_out(
    isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """r4 8/9: a frame goes out only when its payload changed, and to find out every socket
    encoded every payload on every tick, with ``sort_keys`` and on the event loop: the needs
    feed is one list for the three seconds between scans, 100 to 300 KB to encode, and the
    fleet one snapshot every socket shares within a tick. Three sockets, a beat on every
    tick: each encodes the feed and the fleet once, to send them, and a feed that changed
    goes out."""
    from types import SimpleNamespace

    from aisquare.services import remote_needs

    items = [{"id": "ny_1", "detail": {"text": "x" * 4_000}}]
    feed = [items]
    encoded: list[str] = []

    def dumps(obj: Any, **kwargs: Any) -> str:
        payload = obj.get("payload", obj) if isinstance(obj, dict) else obj
        if isinstance(payload, dict) and payload.get("items") is items:
            encoded.append("needs")
        elif isinstance(payload, dict) and payload.get("project") == {"id": "prj_x"}:
            encoded.append("fleet")
        return json.dumps(obj, **kwargs)

    monkeypatch.setattr(remote_server, "json", SimpleNamespace(dumps=dumps, loads=json.loads))
    monkeypatch.setattr(
        remote_needs, "needs_ws_frames", lambda kit: [("needs_you", {"items": feed[0]})]
    )

    def fleet(project: str | None) -> object:
        return {"project": {"id": "prj_x"}, "agents": []}  # an equal snapshot, made anew

    runtime = make_runtime()
    app = build_app(
        runtime, sources=_sources(fleet=fleet), dist_dir=tmp_path, tick=0.02, heartbeat=0
    )
    client = make_client(app)
    assert unlock(client, runtime).status_code == 200
    url = f"{base(runtime)}/ws"
    with contextlib.ExitStack() as sockets:
        opened = [sockets.enter_context(client.websocket_connect(url)) for _ in range(3)]
        for ws in opened:
            ws.send_text(json.dumps({"subscribe_fleet": None}))
        for ws in opened:
            beats = 0
            while beats < 6:
                beats += frame_within(ws)["type"] == "heartbeat"
        unchanged = list(encoded)
        feed[0] = [{"id": "ny_2", "detail": {"text": "y"}}]
        changed = [_until(ws, "needs_you")["payload"]["items"][0]["id"] for ws in opened]
    assert unchanged.count("needs") == 3, unchanged.count("needs")
    assert unchanged.count("fleet") == 3, unchanged.count("fleet")
    assert changed == ["ny_2"] * 3
