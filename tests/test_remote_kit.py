"""The kit the lanes build on (SPEC §1): seams, lifespan, kit_route, the ledger, request ids.

Every lane module is replaced here by a recorder through ``monkeypatch``: the
server reaches them only through their module attributes, inside functions, so
these tests pin the calls each lane plugs into, whatever the lane does behind them.
"""

from __future__ import annotations

import contextlib
import dataclasses
import itertools
import json
import logging
import threading
import tracemalloc
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from typer.testing import CliRunner

from aisquare.cli.app import app as cli
from aisquare.core.paths import remote_audit_path
from aisquare.services import remote_actions, remote_needs, remote_push, remote_server
from aisquare.services.remote_actions import ActionLedger, LedgerEntry
from aisquare.services.remote_needs import NeedsItem, QuickAnswer
from aisquare.services.remote_server import (
    CRASHED,
    IN_PROGRESS,
    NOT_WRITE_GATED,
    READ_ONLY_REASON,
    WRITE_ENDPOINTS,
    Device,
    RemoteKit,
    RequestError,
    Runtime,
    Sources,
    Writes,
    build_app,
    check_public_origin,
    note_public_url,
    remote_agent_lock,
    write_endpoint_names,
)
from tests.remote_kit_helpers import (
    base,
    frame_within,
    make_client,
    make_runtime,
    receive_within,
    unlock,
)


def _sources() -> Sources:
    return Sources(
        projects=lambda: [],
        fleet=lambda project: {},
        board=lambda project: {},
        tasks=lambda project: [],
        memory=lambda project: [],
        panes=lambda agent, project, history: {"rows": [], "width": 0, "height": 0},
        explainability=lambda agent, project: {"available": False},
    )


@pytest.fixture
def runtime(isolated_home: Path) -> Runtime:
    return make_runtime()


class RecordingLedger(ActionLedger):
    """A ledger that does what the real one must, and writes down every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.finished: dict[tuple[str, str], tuple[int, dict[str, object]]] = {}
        self.running: set[tuple[str, str]] = set()
        self.recent: dict[str, list[LedgerEntry]] = {}

    def ledger_replay(
        self, device_id: str, request_id: str
    ) -> tuple[int, dict[str, object]] | None:
        self.calls.append(("replay", request_id))
        return self.finished.get((device_id, request_id))

    def ledger_begin(self, device_id: str, request_id: str, endpoint: str) -> bool:
        self.calls.append(("begin", request_id, endpoint))
        if (device_id, request_id) in self.running:
            return False
        self.running.add((device_id, request_id))
        return True

    def ledger_finish(
        self, device_id: str, request_id: str, status: int, body: dict[str, object]
    ) -> None:
        self.calls.append(("finish", request_id, status, body))
        self.running.discard((device_id, request_id))
        self.finished[(device_id, request_id)] = (status, body)

    def ledger_recent(self, device_id: str) -> list[LedgerEntry]:
        return self.recent.get(device_id, [])


Endpoint = Callable[[Request, Device, dict[str, Any]], Any]


def _lane_app(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: Endpoint,
    *,
    path: str = "/api/needs/answer",
    methods: tuple[str, ...] = ("POST",),
    write_gated: bool = True,
) -> Any:
    """An app whose needs lane serves ``endpoint`` at ``path``, with a recording ledger."""
    monkeypatch.setattr(
        remote_needs,
        "needs_routes",
        lambda kit: [kit.kit_route(path, endpoint, methods=list(methods), write_gated=write_gated)],
    )
    app = build_app(runtime, sources=_sources(), writes=Writes({}), dist_dir=tmp_path)
    app.kit.ledger = RecordingLedger()
    return app


def _unlocked(app: Any, runtime: Runtime) -> Any:
    client = make_client(app)
    assert unlock(client, runtime).status_code == 200
    return client


def _device_id(client: Any, runtime: Runtime) -> str:
    """The id of the device this client unlocked (its cookie is a secret, not its id)."""
    rows = client.get(f"{base(runtime)}/api/devices").json()
    return str(next(row["id"] for row in rows if row["current"]))


# --- the lifespan: lanes start with the server and stop with it ---------------------------


def test_the_lanes_start_with_the_app_and_stop_in_reverse_after_it(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    kits: list[RemoteKit] = []

    def starter(name: str) -> Callable[[RemoteKit], Callable[[], None]]:
        def start(kit: RemoteKit) -> Callable[[], None]:
            events.append(f"{name} started")
            kits.append(kit)
            return lambda: events.append(f"{name} stopped")

        return start

    monkeypatch.setattr(remote_needs, "start_needs_watch", starter("needs"))
    monkeypatch.setattr(remote_push, "start_push_sender", starter("push"))
    app = build_app(runtime, sources=_sources(), dist_dir=tmp_path)
    assert events == [], "nothing starts until the server does"
    with make_client(app) as client:
        assert events == ["needs started", "push started"], "the watcher, then its listener"
        assert all(kit is app.kit for kit in kits)
        assert unlock(client, runtime).status_code == 200
    assert events == ["needs started", "push started", "push stopped", "needs stopped"]


def test_a_lane_that_fails_to_start_costs_its_feature_not_the_server(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped: list[str] = []

    def broken(kit: RemoteKit) -> Callable[[], None]:
        raise RuntimeError("the watcher could not start")

    monkeypatch.setattr(remote_needs, "start_needs_watch", broken)
    monkeypatch.setattr(
        remote_push, "start_push_sender", lambda kit: lambda: stopped.append("push")
    )
    app = build_app(runtime, sources=_sources(), dist_dir=tmp_path)
    with make_client(app) as client:
        assert unlock(client, runtime).status_code == 200
        assert client.get(f"{base(runtime)}/api/remote").status_code == 200
    assert stopped == ["push"], "the lane that did start still stops"


# --- lane routes: before the write catch-all, behind the same gates -----------------------


def test_a_lane_route_is_reached_before_the_write_catch_all(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    async def dismiss(request: Request, device: Device, body: dict[str, Any]) -> Response:
        seen.append((device.id, body))
        return JSONResponse({"dismissed": body.get("id")})

    app = _lane_app(
        runtime, tmp_path, monkeypatch, dismiss, path="/api/needs/dismiss", write_gated=False
    )
    client = _unlocked(app, runtime)
    device_id = _device_id(client, runtime)
    response = client.post(f"{base(runtime)}/api/needs/dismiss", json={"id": "ny_1"})
    assert response.status_code == 200, "the dispatcher would have answered 404 for this name"
    assert response.json() == {"dismissed": "ny_1"}
    assert seen == [(device_id, {"id": "ny_1"})], "the gate's device and the parsed body"


def test_a_lane_get_route_gets_an_empty_body(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def recent(request: Request, device: Device, body: dict[str, Any]) -> Response:
        return JSONResponse({"body": body})

    app = _lane_app(
        runtime,
        tmp_path,
        monkeypatch,
        recent,
        path="/api/actions/recent",
        methods=("GET",),
        write_gated=False,
    )
    client = _unlocked(app, runtime)
    assert client.get(f"{base(runtime)}/api/actions/recent").json() == {"body": {}}


def test_kit_route_refuses_to_build_a_write_without_the_gate(runtime: Runtime) -> None:
    async def endpoint(request: Request, device: Device, body: dict[str, Any]) -> Response:
        return JSONResponse({})

    kit = RemoteKit(runtime)
    with pytest.raises(ValueError, match="NOT_WRITE_GATED"):
        kit.kit_route("/api/agent/nuke", endpoint, methods=["POST"], write_gated=False)
    kit.kit_route("/api/needs", endpoint, methods=["GET"], write_gated=False)  # a read: fine
    assert ("POST", "/api/needs/dismiss") in NOT_WRITE_GATED
    kit.kit_route("/api/needs/dismiss", endpoint, methods=["POST"], write_gated=False)


def test_a_gated_lane_route_is_403_until_writes_are_on(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[dict[str, Any]] = []

    async def answer(request: Request, device: Device, body: dict[str, Any]) -> Response:
        ran.append(body)
        return JSONResponse({"answered": body["id"]})

    app = _lane_app(runtime, tmp_path, monkeypatch, answer)
    client = _unlocked(app, runtime)
    refused = client.post(f"{base(runtime)}/api/needs/answer", json={"id": "ny_1"})
    assert refused.status_code == 403
    assert refused.json() == {"error": "read_only", "message": READ_ONLY_REASON}
    assert ran == [] and app.kit.ledger.calls == []
    runtime.set_allow_write(True)
    ok = client.post(f"{base(runtime)}/api/needs/answer", json={"id": "ny_1"})
    assert ok.status_code == 200 and ran == [{"id": "ny_1"}]


def test_an_endpoint_that_raises_a_refusal_answers_in_the_one_shape(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def answer(request: Request, device: Device, body: dict[str, Any]) -> Response:
        if body.get("missing"):
            raise LookupError("no live agent 'ghost'")
        raise RequestError(400, "not_answerable", "reply on the board instead")

    app = _lane_app(runtime, tmp_path, monkeypatch, answer)
    runtime.set_allow_write(True)
    client = _unlocked(app, runtime)
    refused = client.post(f"{base(runtime)}/api/needs/answer", json={})
    assert refused.status_code == 400
    assert refused.json() == {"error": "not_answerable", "message": "reply on the board instead"}
    gone = client.post(f"{base(runtime)}/api/needs/answer", json={"missing": True})
    assert gone.status_code == 404 and gone.json()["error"] == "not_found"


# --- the ledger, through kit_route --------------------------------------------------------


def test_a_retried_request_id_is_answered_from_the_ledger_without_running_again(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[dict[str, Any]] = []

    async def answer(request: Request, device: Device, body: dict[str, Any]) -> Response:
        ran.append(body)
        return JSONResponse({"answered": body["id"], "run": len(ran)})

    app = _lane_app(runtime, tmp_path, monkeypatch, answer)
    runtime.set_allow_write(True)
    client = _unlocked(app, runtime)
    first = client.post(
        f"{base(runtime)}/api/needs/answer", json={"id": "ny_1", "request_id": "c0ffee"}
    )
    again = client.post(
        f"{base(runtime)}/api/needs/answer", json={"id": "ny_1", "request_id": "c0ffee"}
    )
    assert first.status_code == again.status_code == 200
    assert first.json() == again.json() == {"answered": "ny_1", "run": 1}
    assert ran == [{"id": "ny_1"}], "request_id is the ledger's, never the endpoint's"
    assert app.kit.ledger.calls == [
        ("replay", "c0ffee"),
        ("begin", "c0ffee", "needs/answer"),
        ("finish", "c0ffee", 200, {"answered": "ny_1", "run": 1}),
        ("replay", "c0ffee"),
    ]


def test_a_request_id_still_running_is_409_in_progress(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[dict[str, Any]] = []

    async def answer(request: Request, device: Device, body: dict[str, Any]) -> Response:
        ran.append(body)
        return JSONResponse({})

    app = _lane_app(runtime, tmp_path, monkeypatch, answer)
    runtime.set_allow_write(True)
    client = _unlocked(app, runtime)
    app.kit.ledger.running.add((_device_id(client, runtime), "slow-1"))
    response = client.post(f"{base(runtime)}/api/needs/answer", json={"request_id": "slow-1"})
    assert response.status_code == 409
    assert response.json() == {"error": "in_progress", "message": IN_PROGRESS}
    assert ran == []


def test_a_refusal_is_stored_too_so_a_retry_gets_the_same_refusal(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stale = {"error": "stale", "message": "coder-1 no longer shows that question", "current": []}

    async def answer(request: Request, device: Device, body: dict[str, Any]) -> Response:
        return JSONResponse(stale, status_code=409)

    app = _lane_app(runtime, tmp_path, monkeypatch, answer)
    runtime.set_allow_write(True)
    client = _unlocked(app, runtime)
    first = client.post(f"{base(runtime)}/api/needs/answer", json={"request_id": "r1"})
    again = client.post(f"{base(runtime)}/api/needs/answer", json={"request_id": "r1"})
    assert (first.status_code, first.json()) == (again.status_code, again.json()) == (409, stale)
    assert ("finish", "r1", 409, stale) in app.kit.ledger.calls


def test_an_endpoint_that_crashes_never_leaves_its_request_id_running(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def answer(request: Request, device: Device, body: dict[str, Any]) -> Response:
        raise RuntimeError("a bug")

    app = _lane_app(runtime, tmp_path, monkeypatch, answer)
    runtime.set_allow_write(True)
    client = _unlocked(app, runtime)
    with pytest.raises(RuntimeError, match="a bug"):
        client.post(f"{base(runtime)}/api/needs/answer", json={"request_id": "r2"})
    crashed = {"error": "internal_error", "message": CRASHED}
    assert ("finish", "r2", 500, crashed) in app.kit.ledger.calls
    assert app.kit.ledger.running == set()
    retried = client.post(f"{base(runtime)}/api/needs/answer", json={"request_id": "r2"})
    assert (retried.status_code, retried.json()) == (500, crashed), "the one shape, sentence too"


@pytest.mark.parametrize("request_id", ["../x", "a" * 65, "", "with space", 7, ["x"]])
def test_a_malformed_request_id_is_400_before_the_ledger_or_the_endpoint(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request_id: object
) -> None:
    ran: list[dict[str, Any]] = []

    async def answer(request: Request, device: Device, body: dict[str, Any]) -> Response:
        ran.append(body)
        return JSONResponse({})

    app = _lane_app(runtime, tmp_path, monkeypatch, answer)
    runtime.set_allow_write(True)
    client = _unlocked(app, runtime)
    response = client.post(f"{base(runtime)}/api/needs/answer", json={"request_id": request_id})
    assert response.status_code == 400, request_id
    assert response.json()["error"] == "invalid"
    assert ran == [] and app.kit.ledger.calls == []


def test_the_longest_request_id_is_accepted(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def answer(request: Request, device: Device, body: dict[str, Any]) -> Response:
        return JSONResponse({})

    app = _lane_app(runtime, tmp_path, monkeypatch, answer)
    runtime.set_allow_write(True)
    client = _unlocked(app, runtime)
    longest = "A_z-9" * 12 + "abcd"
    assert len(longest) == 64
    ok = client.post(f"{base(runtime)}/api/needs/answer", json={"request_id": longest})
    assert ok.status_code == 200


def test_an_ungated_route_leaves_request_id_in_the_body_and_the_ledger_alone(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def dismiss(request: Request, device: Device, body: dict[str, Any]) -> Response:
        return JSONResponse({"body": body})

    app = _lane_app(
        runtime, tmp_path, monkeypatch, dismiss, path="/api/needs/dismiss", write_gated=False
    )
    client = _unlocked(app, runtime)
    sent = {"id": "ny_1", "request_id": "../not-checked-here"}
    response = client.post(f"{base(runtime)}/api/needs/dismiss", json=sent)
    assert response.json() == {"body": sent}
    assert app.kit.ledger.calls == []


# --- the dispatcher: the plan's writes, then the agent actions ----------------------------


def test_write_endpoint_names_are_the_plan_then_the_actions(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    told: list[dict[str, Any]] = []

    def tell(body: dict[str, Any]) -> tuple[dict[str, object], str]:
        told.append(body)
        return {"label": body["agent"], "delivered": True}, f"tell {body['agent']}"

    assert write_endpoint_names() == WRITE_ENDPOINTS + remote_actions.ACTION_ENDPOINTS
    monkeypatch.setattr(remote_actions, "ACTION_ENDPOINTS", ("agent/tell",))
    monkeypatch.setattr(remote_actions, "action_handlers", lambda: {"agent/tell": tell})
    assert write_endpoint_names() == (*WRITE_ENDPOINTS, "agent/tell")
    assert "agent/tell" in remote_server.live_writes().handlers
    app = build_app(runtime, sources=_sources(), dist_dir=tmp_path)
    client = _unlocked(app, runtime)
    assert client.post(f"{base(runtime)}/api/agent/tell", json={"agent": "c1"}).status_code == 403
    runtime.set_allow_write(True)
    response = client.post(f"{base(runtime)}/api/agent/tell", json={"agent": "c1"})
    assert response.status_code == 200 and response.json() == {"label": "c1", "delivered": True}
    assert told == [{"agent": "c1"}]
    last = remote_audit_path().read_text().splitlines()[-1]
    assert last.split(" ", 3)[2:] == ["agent/tell", "tell c1"]


def test_the_dispatcher_goes_through_the_ledger_and_audits_once(
    runtime: Runtime, tmp_path: Path
) -> None:
    ran: list[dict[str, Any]] = []

    def note(body: dict[str, Any]) -> tuple[dict[str, object], str]:
        ran.append(body)
        if body.get("refuse"):
            raise RequestError(409, "busy", "another action on coder-1 is still running")
        return {"event": len(ran)}, "note seq=1"

    app = build_app(runtime, sources=_sources(), writes=Writes({"note": note}), dist_dir=tmp_path)
    app.kit.ledger = RecordingLedger()
    runtime.set_allow_write(True)
    client = _unlocked(app, runtime)
    url = f"{base(runtime)}/api/note"
    first = client.post(url, json={"text": "hi", "request_id": "n1"})
    again = client.post(url, json={"text": "hi", "request_id": "n1"})
    assert first.json() == again.json() == {"event": 1}
    assert ran == [{"text": "hi"}], "the retry was answered from the ledger"
    assert _audited("note") == 1
    refused = client.post(url, json={"refuse": True, "request_id": "n2"})
    replayed = client.post(url, json={"refuse": True, "request_id": "n2"})
    assert refused.status_code == replayed.status_code == 409
    assert (
        refused.json()
        == replayed.json()
        == {
            "error": "busy",
            "message": "another action on coder-1 is still running",
        }
    )
    assert len(ran) == 2 and _audited("note") == 1
    assert ("begin", "n1", "note") in app.kit.ledger.calls
    bad = client.post(url, json={"text": "hi", "request_id": "../x"})
    assert bad.status_code == 400 and len(ran) == 2


def _audited(endpoint: str) -> int:
    """How many audit lines this endpoint has (the unlock has its own)."""
    lines = remote_audit_path().read_text().splitlines()
    return sum(line.split(" ")[2] == endpoint for line in lines)


# --- the kit's own answers ---------------------------------------------------------------


def test_kit_refuse_carries_extra_keys_and_headers(runtime: Runtime) -> None:
    kit = RemoteKit(runtime)
    stale = kit.kit_refuse(409, "stale", "gone", current=[{"id": "ny_2"}])
    assert stale.status_code == 409
    assert stale.body == b'{"error":"stale","message":"gone","current":[{"id":"ny_2"}]}'
    throttled = kit.kit_refuse(429, "push_test_throttled", "wait", headers={"Retry-After": "7"})
    assert throttled.headers["retry-after"] == "7"


def test_one_agent_has_one_lock_whoever_asks(runtime: Runtime) -> None:
    lock = remote_agent_lock("prj_a", "coder-1")
    assert remote_agent_lock("prj_a", "coder-1") is lock
    assert remote_agent_lock("prj_b", "coder-1") is not lock, "the same label, another project"
    assert lock.acquire(blocking=False)
    try:
        assert not remote_agent_lock("prj_a", "coder-1").acquire(blocking=False)
    finally:
        lock.release()


def test_a_needs_item_serializes_to_the_wire_shape_without_push_after() -> None:
    since = datetime(2026, 10, 7, 10, 12, 3, tzinfo=UTC)
    item = NeedsItem(
        id="ny_9f2c41aa0b3d7e15",
        kind="question",
        project_id="prj_8c1e",
        project_name="aisquare-cli",
        agent="coder-auth",
        agent_id="agt_51a",
        reason="coder-auth asks you a question",
        excerpt="Which store?",
        detail={"questions": []},
        answers=(QuickAnswer("Redis", ("1",)), QuickAnswer("Cancel", ("Escape",))),
        since=since,
        actions=("answer", "open", "dismiss"),
        push_after=since,
    )
    assert item.needs_item_json() == {
        "id": "ny_9f2c41aa0b3d7e15",
        "kind": "question",
        "project": {"id": "prj_8c1e", "name": "aisquare-cli"},
        "agent": "coder-auth",
        "agent_id": "agt_51a",
        "reason": "coder-auth asks you a question",
        "excerpt": "Which store?",
        "detail": {"questions": []},
        "answers": [{"label": "Redis", "keys": ["1"]}, {"label": "Cancel", "keys": ["Escape"]}],
        "since": "2026-10-07T10:12:03+00:00",
        "actions": ["answer", "open", "dismiss"],
    }


# --- the stream's seams: needs, actions, heartbeat ----------------------------------------


def _frames(ws: Any, count: int) -> list[dict[str, Any]]:
    return [frame_within(ws) for _ in range(count)]


def _until(ws: Any, kind: str, *, limit: int = 60) -> dict[str, Any]:
    for _ in range(limit):
        frame = frame_within(ws)
        if frame["type"] == kind:
            return frame
    raise AssertionError(f"no {kind} frame in {limit}")


def _stream_app(runtime: Runtime, tmp_path: Path, **kw: Any) -> tuple[Any, Any]:
    sources = kw.pop("sources", None) or _sources()
    app = build_app(runtime, sources=sources, dist_dir=tmp_path, tick=kw.pop("tick", 0.02), **kw)
    return app, _unlocked(app, runtime)


def _frame_within(ws: Any) -> dict[str, Any]:
    """The next frame; a failed test if the socket closed or went silent instead."""
    message = receive_within(ws)
    assert message["type"] == "websocket.send", f"the socket ended: {message}"
    frame: dict[str, Any] = json.loads(message["text"])
    return frame


def _lane_bug(*args: object) -> Any:
    raise RuntimeError("a bug in a lane")


def test_the_heartbeat_is_never_on_the_first_tick(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``heartbeat=0`` makes a beat due on every tick, the first one included, so only the
    first-tick rule holds it back. A needs frame that changes on every tick marks where
    each tick begins: a beat on the first tick would come before the second one."""
    ticks = itertools.count(1)
    monkeypatch.setattr(
        remote_needs, "needs_ws_frames", lambda kit: [("needs_you", {"tick": next(ticks)})]
    )
    _app, client = _stream_app(runtime, tmp_path, heartbeat=0)
    with client.websocket_connect(f"{base(runtime)}/ws") as ws:
        first = _frames(ws, 6)
    assert [frame["type"] for frame in first] == [
        *("board", "fleet", "remote", "needs_you"),  # the first tick
        *("needs_you", "heartbeat"),  # the second
    ]
    assert [frame["payload"] for frame in first[3:5]] == [{"tick": 1}, {"tick": 2}]
    assert first[5]["payload"] == {"needs_scanned_at": None}


def test_the_heartbeat_arrives_unchanged_or_not_and_carries_the_last_scan(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(remote_needs, "needs_scanned_iso", lambda kit: "2026-10-07T10:12:05+00:00")
    _app, client = _stream_app(runtime, tmp_path, heartbeat=0.05)
    with client.websocket_connect(f"{base(runtime)}/ws") as ws:
        assert [frame["type"] for frame in _frames(ws, 3)] == ["board", "fleet", "remote"]
        beats = [_until(ws, "heartbeat"), _until(ws, "heartbeat")]
    assert all(
        beat["payload"] == {"needs_scanned_at": "2026-10-07T10:12:05+00:00"} for beat in beats
    )


def test_needs_frames_come_after_board_fleet_and_remote(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        remote_needs, "needs_ws_frames", lambda kit: [("needs_you", {"items": [{"id": "ny_1"}]})]
    )
    _app, client = _stream_app(runtime, tmp_path)
    with client.websocket_connect(f"{base(runtime)}/ws") as ws:
        first = _frames(ws, 4)
    assert [frame["type"] for frame in first] == ["board", "fleet", "remote", "needs_you"]
    assert first[3]["payload"] == {"items": [{"id": "ny_1"}]}


def test_the_action_frame_shows_this_devices_ledger_only_when_it_has_entries(
    runtime: Runtime, tmp_path: Path
) -> None:
    app, client = _stream_app(runtime, tmp_path, heartbeat=0)
    ledger = RecordingLedger()
    app.kit.ledger = ledger
    device_id = _device_id(client, runtime)
    entry: LedgerEntry = {
        "request_id": "c0ffee",
        "endpoint": "agent/restart",
        "status": 200,
        "body": {"started": "agt_2"},
        "at": "2026-10-07T10:12:05+00:00",
    }
    with client.websocket_connect(f"{base(runtime)}/ws") as ws:
        quiet = _frames(ws, 4)
        assert "action" not in [frame["type"] for frame in quiet], "an empty ledger sends nothing"
        ledger.recent[device_id] = [entry]
        assert _until(ws, "action")["payload"] == {"actions": [entry]}
        ledger.recent["dev_someone_else"] = [{**entry, "request_id": "theirs"}]
        ledger.recent[device_id] = [{**entry, "request_id": "next"}, entry]
        actions = _until(ws, "action")["payload"]["actions"]
    assert [action["request_id"] for action in actions] == ["next", "c0ffee"]


@pytest.mark.parametrize("seam", ["needs_ws_frames", "ledger_recent", "needs_scanned_iso"])
def test_a_lane_seam_that_raises_costs_its_own_frame_and_never_the_socket(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    seam: str,
) -> None:
    """Every tick calls these three, and the lanes behind them land after this region is
    frozen. A bug in one used to end the socket at once, on every phone, every tick it
    recurred: now the heartbeat still beats (with no scan time when that is what failed),
    panes still stream, and the bug is logged once per socket, not once per tick."""
    scanned = "2026-10-07T10:12:05+00:00"
    monkeypatch.setattr(remote_needs, "needs_scanned_iso", lambda kit: scanned)
    app, client = _stream_app(runtime, tmp_path, heartbeat=0)
    if seam == "ledger_recent":
        monkeypatch.setattr(app.kit.ledger, "ledger_recent", _lane_bug)
    else:
        monkeypatch.setattr(remote_needs, seam, _lane_bug)
    seen: list[str] = []
    beats: list[object] = []
    with client.websocket_connect(f"{base(runtime)}/ws") as ws:
        ws.send_text(json.dumps({"subscribe": "coder-1"}))
        while seen.count("heartbeat") < 3 or "pane" not in seen:  # 4 ticks, 3 or 4 failures
            assert len(seen) < 50, seen
            frame = _frame_within(ws)
            seen.append(frame["type"])
            if frame["type"] == "heartbeat":
                beats.append(frame["payload"])
    expected = None if seam == "needs_scanned_iso" else scanned
    assert beats[:3] == [{"needs_scanned_at": expected}] * 3
    logged = [
        r
        for r in caplog.records
        if r.name == remote_server.log.name and r.levelno >= logging.WARNING
    ]
    assert [r.getMessage() for r in logged] == [
        f"remote: {seam} failed; the stream goes on without it"
    ]
    assert logged[0].exc_info is not None, "the warning carries the lane's traceback"


def test_a_stream_that_fails_otherwise_closes_1011_not_a_dropped_link(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What is not a lane's to lose still ends the socket, but with a close frame the
    page reads (1011, reconnect with backoff), where returning without one left the
    phone an abnormal 1006 with no reason."""
    _app, client = _stream_app(runtime, tmp_path)
    monkeypatch.setattr(runtime, "remote_json", _lane_bug)
    with client.websocket_connect(f"{base(runtime)}/ws") as ws:
        assert [_frame_within(ws)["type"] for _ in range(2)] == ["board", "fleet"]
        closed = receive_within(ws)
    assert closed["type"] == "websocket.close" and closed["code"] == 1011, closed


# --- sockets per device, the pane pool, and what a socket keeps ---------------------------


def test_a_fifth_socket_from_one_device_closes_its_oldest_with_4409(
    runtime: Runtime, tmp_path: Path
) -> None:
    """A phone that slept left a half-open socket behind; the woken phone must get in.

    ``heartbeat=0``: a live socket gets a frame every tick, so "still open" is observable.
    """
    app, client = _stream_app(runtime, tmp_path, heartbeat=0)
    device_id = _device_id(client, runtime)
    with contextlib.ExitStack() as stack:
        sockets = []
        for _ in range(remote_server.WS_SOCKETS_PER_DEVICE + 1):
            ws = stack.enter_context(client.websocket_connect(f"{base(runtime)}/ws"))
            _until(ws, "remote")  # registered: it has ticked
            sockets.append(ws)
        closed = None
        for _ in range(1000):
            message = receive_within(sockets[0])
            if message["type"] == "websocket.close":
                closed = message
                break
        assert closed is not None and closed["code"] == remote_server.WS_CLOSE_REPLACED
        assert len(app.kit.sockets[device_id]) == remote_server.WS_SOCKETS_PER_DEVICE
        for live in sockets[1:]:
            _until(live, "heartbeat")  # the four newest are untouched
    assert device_id not in app.kit.sockets, "every socket that ended was forgotten"


def test_another_devices_sockets_are_not_counted_against_this_one(
    runtime: Runtime, tmp_path: Path
) -> None:
    app, client = _stream_app(runtime, tmp_path)
    other = make_client(app)
    unlock(other, runtime)
    with contextlib.ExitStack() as stack:
        for _ in range(remote_server.WS_SOCKETS_PER_DEVICE):
            _until(stack.enter_context(client.websocket_connect(f"{base(runtime)}/ws")), "remote")
        theirs = stack.enter_context(other.websocket_connect(f"{base(runtime)}/ws"))
        _until(theirs, "remote")
        assert sorted(len(live) for live in app.kit.sockets.values()) == [1, 4]


def test_pane_captures_run_on_the_pane_pool_and_the_lifespan_shuts_it(
    runtime: Runtime, tmp_path: Path
) -> None:
    threads: list[str] = []

    def panes(agent: str, project: str | None, history: int) -> dict[str, object]:
        threads.append(threading.current_thread().name)
        return {"rows": [agent], "width": 1, "height": 1}

    sources = dataclasses.replace(_sources(), panes=panes)
    app = build_app(runtime, sources=sources, dist_dir=tmp_path, tick=0.02)
    with make_client(app) as client:
        unlock(client, runtime)
        with client.websocket_connect(f"{base(runtime)}/ws") as ws:
            ws.send_text(json.dumps({"subscribe": "coder-1"}))
            assert _until(ws, "pane")["payload"]["rows"] == ["coder-1"]
        pool = app.kit.pane_pool
        assert pool is not None
    assert threads and all(name.startswith("asq-remote-pane") for name in threads), threads
    assert app.kit.pane_pool is None
    with pytest.raises(RuntimeError):
        pool.submit(print)  # shut down with the server


def test_a_socket_cycling_through_pane_labels_keeps_none_of_the_old_ones(
    runtime: Runtime, tmp_path: Path
) -> None:
    """Unsubscribing forgets a pane's last frame along with its subscription.

    The frame once stayed behind, so any unlocked device, a read-only one too, could
    grow the server without bound: subscribe a fresh 4 000-character label, take its
    frame (for an unknown agent, an error that repeats the label), unsubscribe, again.
    That memory is the stream's own and out of a test's reach, so it is measured: what
    ``tracemalloc`` sees allocated under this module and still held after 100 labels,
    which is about 800 KB when every label's frame is kept.
    """

    def panes(agent: str, project: str | None, history: int) -> dict[str, object]:
        return {"rows": [], "width": 0, "height": 0, "error": f"no live agent {agent!r}"}

    sources = dataclasses.replace(_sources(), panes=panes)
    _app, client = _stream_app(runtime, tmp_path, sources=sources, tick=0.005)
    here = [tracemalloc.Filter(True, remote_server.__file__, all_frames=True)]

    def cycle(ws: Any, n: int) -> None:
        label = f"{n:03d}" + "x" * 4_000
        ws.send_text(json.dumps({"subscribe": label}))
        while _until(ws, "pane")["agent"] != label:
            pass
        ws.send_text(json.dumps({"unsubscribe": label}))

    tracemalloc.start(32)
    try:
        with client.websocket_connect(f"{base(runtime)}/ws") as ws:
            for n in range(5):  # the pool's thread and the caches exist before the count
                cycle(ws, n)
            before = tracemalloc.take_snapshot().filter_traces(here)
            for n in range(5, 105):
                cycle(ws, n)
            after = tracemalloc.take_snapshot().filter_traces(here)
    finally:
        tracemalloc.stop()
    held = sum(stat.size_diff for stat in after.compare_to(before, "filename"))
    assert held < 200_000, f"{held} bytes more held after 100 labels than before them"


# --- the public URL: authoritative sources only (SPEC §5.8) -------------------------------


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("https://abcd-12.ngrok-free.app", "https://abcd-12.ngrok-free.app"),
        ("https://abcd-12.ngrok-free.app/r/tok_123/", "https://abcd-12.ngrok-free.app"),
        ("https://Remote.Example.COM:443/x?y#z", "https://remote.example.com"),
        ("https://xn--bcher-kva.example/", "https://xn--bcher-kva.example"),
    ],
)
def test_a_public_url_on_https_and_a_dns_name_gives_its_origin(url: str, origin: str) -> None:
    assert check_public_origin(url) == origin


@pytest.mark.parametrize(
    "url",
    [
        "http://abcd-12.ngrok-free.app/",  # not https
        "https://127.0.0.1/",  # an IP literal
        "https://[::1]/",
        "https://0x7f.1/",  # a name a browser reads as 127.0.0.1
        "https://2130706433/",
        "https://user@abcd-12.ngrok-free.app/",  # userinfo
        "https://user:pw@abcd-12.ngrok-free.app/",
        "https://abcd-12.ngrok-free.app:8443/",  # another port
        "https://abcd-12.ngrok-free.app:99999/",
        "https://localhost/",  # not a DNS name anyone else resolves
        "https://example.com./",  # another origin than example.com
        "https://-bad.example/",
        "https://bücher.example/",
        "ftp://example.com/",
        "not a url",
    ],
)
def test_anything_else_is_refused(url: str) -> None:
    with pytest.raises(ValueError):
        check_public_origin(url)


def test_note_public_url_sets_and_clears_the_servers_origin(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(remote_server, "_runtime", None)
    server = remote_server.runtime()
    note_public_url("https://abcd-12.ngrok-free.app/r/whatever/")
    assert server.remote_public_origin() == "https://abcd-12.ngrok-free.app"
    with pytest.raises(ValueError):
        note_public_url("http://evil.example/")
    assert server.remote_public_origin() == "https://abcd-12.ngrok-free.app", "a refusal keeps it"
    with pytest.raises(ValueError):
        server.note_public_origin("https://127.0.0.1")  # the runtime checks again
    note_public_url(None)
    assert server.remote_public_origin() is None


def test_no_request_header_teaches_the_server_its_public_origin(
    runtime: Runtime, tmp_path: Path
) -> None:
    """What a forged ``Host`` plus ``X-Forwarded-Proto: https`` would buy, if it were read:
    a push link to a page the attacker serves, where the human types the passphrase."""
    app = build_app(runtime, sources=_sources(), dist_dir=tmp_path, tick=0.02)
    forged = {
        "host": "evil.ngrok-free.app",
        "x-forwarded-proto": "https",
        "x-forwarded-host": "evil.ngrok-free.app",
        "forwarded": "host=evil.ngrok-free.app;proto=https",
    }
    # Each Origin matches the forged Host, as a sender who writes both would make it.
    # https: the forged X-Forwarded-Proto makes unlock's cookie Secure.
    client = make_client(
        app,
        base_url="https://testserver",
        headers={**forged, "origin": "https://evil.ngrok-free.app"},
    )
    assert unlock(client, runtime).status_code == 200
    assert client.get(f"{base(runtime)}/api/remote").status_code == 200
    # The test client opens sockets on ws:// only, where a Secure cookie is not sent.
    plain = make_client(app)
    assert unlock(plain, runtime).status_code == 200
    handshake = {**forged, "origin": "http://evil.ngrok-free.app"}
    with plain.websocket_connect(f"{base(runtime)}/ws", headers=handshake) as ws:
        _until(ws, "remote")
    assert app.kit.kit_public_url() is None
    runtime.note_public_origin("https://abcd-12.ngrok-free.app")
    assert client.get(f"{base(runtime)}/api/remote").status_code == 200
    assert app.kit.kit_public_url() == f"https://abcd-12.ngrok-free.app/r/{runtime.token}/"


# --- asq remote needs ------------------------------------------------------------------------


def test_asq_remote_needs_prints_the_lanes_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"items": [], "scanned_at": "2026-10-07T10:12:05+00:00"}
    monkeypatch.setattr(remote_needs, "needs_cli_payload", lambda: payload)
    as_json = CliRunner().invoke(cli, ["--json", "remote", "needs"])
    assert as_json.exit_code == 0, as_json.output
    assert json.loads(as_json.stdout) == payload
    human = CliRunner().invoke(cli, ["remote", "needs"])
    assert human.exit_code == 0 and human.stdout.strip() == "nothing needs you"


def test_asq_remote_needs_prints_one_line_per_item(monkeypatch: pytest.MonkeyPatch) -> None:
    since = datetime.now(UTC) - timedelta(hours=1, minutes=5, seconds=10)
    item = {
        "kind": "question",
        "project": {"id": "prj_8c1e", "name": "aisquare-cli"},
        "agent": "coder-auth",
        "reason": "coder-auth asks you a question [/b]",  # agents' text: never markup
        "since": since.isoformat(timespec="seconds"),
    }
    project_level = {**item, "kind": "fleet_down", "agent": None, "reason": "tmux is down"}
    monkeypatch.setattr(remote_needs, "needs_cli_payload", lambda: {"items": [item, project_level]})
    result = CliRunner().invoke(cli, ["remote", "needs"])
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "⚑ question · aisquare-cli · coder-auth — coder-auth asks you a question [/b] (1h05m)",
        "⚑ fleet_down · aisquare-cli · - — tmux is down (1h05m)",
    ]


def test_a_socket_that_cannot_be_closed_does_not_cost_the_new_one_its_place(
    runtime: Runtime,
) -> None:
    """An evicted socket whose loop is already gone raises from its closer; the new
    socket is still counted and the eviction still happens."""
    kit = RemoteKit(runtime)
    closed: list[int] = []

    def gone(code: int) -> None:
        raise RuntimeError("Event loop is closed")

    kit.kit_socket_opened("dev_1", gone)
    live = [closed.append for _ in range(remote_server.WS_SOCKETS_PER_DEVICE)]
    for closer in live:
        kit.kit_socket_opened("dev_1", closer)
    assert kit.sockets["dev_1"] == live, "the dead one was evicted, the four newest kept"
    kit.kit_socket_opened("dev_1", closed.append)
    assert closed == [remote_server.WS_CLOSE_REPLACED]


def test_a_gated_route_still_answers_its_reads_while_writes_are_off(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write gate and the ledger are for what changes something; a GET never does."""

    async def subscription(request: Request, device: Device, body: dict[str, Any]) -> Response:
        return JSONResponse({"method": request.method})

    app = _lane_app(
        runtime, tmp_path, monkeypatch, subscription, methods=("GET", "POST"), write_gated=True
    )
    client = _unlocked(app, runtime)
    url = f"{base(runtime)}/api/needs/answer"
    assert client.get(url, params={"request_id": "r1"}).json() == {"method": "GET"}
    assert client.post(url, json={}).status_code == 403
    assert app.kit.ledger.calls == []
