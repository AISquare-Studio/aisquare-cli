"""Agent actions from the phone (SPEC §3): tell, stop, restart, switch, and the request ledger.

The fleet service is replaced by recorders (``fleet_service.tell/stop/restart/switch``),
tmux by a pane that writes down what it was sent, and needs-you's view of the agent
(``remote_needs.needs_agent_now`` and its predicates) by a fake whose dialog closes on
an Escape, as Claude Code's does. The project and its rows are real, in the isolated
store, so a pin is checked against what ``fleet ls --all`` would show.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from starlette.requests import Request
from starlette.responses import Response

from aisquare.services import remote_needs
from aisquare.services.remote_actions import (
    ACTION_LEDGER_SIZE,
    ACTION_LEDGER_TTL,
    ActionLedger,
    new_action_ledger,
)
from aisquare.services.remote_server import (
    Device,
    RequestError,
    Runtime,
    Sources,
    Writes,
    build_app,
)
from tests.remote_kit_helpers import base, make_client, make_runtime, unlock


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


def _unlocked(app: Any, runtime: Runtime) -> Any:
    client = make_client(app)
    assert unlock(client, runtime).status_code == 200
    return client


# --- a refusal can say what is current ---------------------------------------------------

STALE = {
    "error": "stale",
    "message": "'coder-1' is another agent now (agt_2) — nothing was done",
    "current": {"agent_id": "agt_2"},
}


def _stale(_body: dict[str, Any]) -> tuple[dict[str, object], str]:
    raise RequestError(409, "stale", str(STALE["message"]), current={"agent_id": "agt_2"})


def test_a_dispatched_refusal_carries_its_extra_keys(runtime: Runtime, tmp_path: Path) -> None:
    """The write dispatcher once kept only ``{error, message}`` of a refusal, so a handler it
    runs could not answer 409 ``stale`` with ``current``, the field the page refreshes from."""
    app = build_app(runtime, sources=_sources(), writes=Writes({"note": _stale}), dist_dir=tmp_path)
    runtime.set_allow_write(True)
    client = _unlocked(app, runtime)
    response = client.post(f"{base(runtime)}/api/note", json={})
    assert (response.status_code, response.json()) == (409, STALE)


def test_a_lane_route_refusal_carries_its_extra_keys_too(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same refusal raised under ``kit_route`` answers the same body: one shape, two paths."""

    async def answer(request: Request, device: Device, body: dict[str, Any]) -> Response:
        _stale(body)
        raise AssertionError("unreachable")

    monkeypatch.setattr(
        remote_needs,
        "needs_routes",
        lambda kit: [
            kit.kit_route("/api/needs/answer", answer, methods=["POST"], write_gated=True)
        ],
    )
    app = build_app(runtime, sources=_sources(), writes=Writes({}), dist_dir=tmp_path)
    runtime.set_allow_write(True)
    client = _unlocked(app, runtime)
    response = client.post(f"{base(runtime)}/api/needs/answer", json={"id": "ny_1"})
    assert (response.status_code, response.json()) == (409, STALE)


def test_a_refusal_without_extras_keeps_the_one_shape() -> None:
    plain = RequestError(409, "busy", "another action on coder-1 is still running")
    assert plain.request_error_body() == {
        "error": "busy",
        "message": "another action on coder-1 is still running",
    }
    stale = RequestError(409, "stale", "gone", current=[], headers="kept as a key")
    assert stale.request_error_body() == {
        "error": "stale",
        "message": "gone",
        "current": [],
        "headers": "kept as a key",
    }, "an extra key is body, never mistaken for a parameter of the response"


# --- the ledger ----------------------------------------------------------------------------

T0 = datetime(2026, 10, 7, 10, 0, tzinfo=UTC)


class Clock:
    """A clock a test moves by hand."""

    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


def _finished(
    ledger: ActionLedger, device_id: str, request_id: str, status: int = 200, **body: object
) -> None:
    assert ledger.ledger_begin(device_id, request_id, "agent/restart")
    ledger.ledger_finish(device_id, request_id, status, dict(body))


def test_a_finished_request_is_replayed_and_one_still_running_cannot_begin_twice() -> None:
    ledger = ActionLedger(clock=Clock())
    assert ledger.ledger_replay("dev_a", "r1") is None, "nothing finished yet"
    assert ledger.ledger_begin("dev_a", "r1", "agent/restart")
    assert not ledger.ledger_begin("dev_a", "r1", "agent/restart"), "still running"
    assert ledger.ledger_begin("dev_b", "r1", "agent/restart"), "another device's id is its own"
    ledger.ledger_finish("dev_a", "r1", 409, {"error": "stale", "current": {"agent_id": "a2"}})
    assert ledger.ledger_replay("dev_a", "r1") == (
        409,
        {"error": "stale", "current": {"agent_id": "a2"}},
    ), "a refusal is replayed as the refusal it was"
    assert ledger.ledger_replay("dev_b", "r1") is None, "dev_b's run has not finished"


def test_the_ledger_shows_a_device_its_own_requests_newest_first() -> None:
    clock = Clock()
    ledger = ActionLedger(clock=clock)
    _finished(ledger, "dev_a", "r1", started="agt_1")
    clock.now += timedelta(seconds=30)
    _finished(ledger, "dev_a", "r2", 409, error="busy")
    _finished(ledger, "dev_b", "theirs")
    assert ledger.ledger_recent("dev_a") == [
        {
            "request_id": "r2",
            "endpoint": "agent/restart",
            "status": 409,
            "body": {"error": "busy"},
            "at": "2026-10-07T10:00:30+00:00",
        },
        {
            "request_id": "r1",
            "endpoint": "agent/restart",
            "status": 200,
            "body": {"started": "agt_1"},
            "at": "2026-10-07T10:00:00+00:00",
        },
    ]
    assert [entry["request_id"] for entry in ledger.ledger_recent("dev_b")] == ["theirs"]
    assert ledger.ledger_recent("dev_c") == []


def test_what_recent_returns_cannot_change_the_ledger() -> None:
    ledger = ActionLedger(clock=Clock())
    _finished(ledger, "dev_a", "r1")
    ledger.ledger_recent("dev_a")[0]["status"] = 500
    assert ledger.ledger_recent("dev_a")[0]["status"] == 200


def test_entries_expire_after_the_ttl() -> None:
    clock = Clock()
    ledger = ActionLedger(clock=clock)
    _finished(ledger, "dev_a", "r1")
    clock.now = T0 + ACTION_LEDGER_TTL - timedelta(seconds=1)
    assert ledger.ledger_replay("dev_a", "r1") == (200, {})
    assert len(ledger.ledger_recent("dev_a")) == 1
    clock.now = T0 + ACTION_LEDGER_TTL
    assert ledger.ledger_replay("dev_a", "r1") is None, "a retry this late runs anew"
    assert ledger.ledger_recent("dev_a") == []


def test_a_request_whose_ending_was_never_recorded_stops_blocking_its_id_after_the_ttl() -> None:
    clock = Clock()
    ledger = ActionLedger(clock=clock)
    assert ledger.ledger_begin("dev_a", "lost", "agent/switch")
    clock.now = T0 + ACTION_LEDGER_TTL - timedelta(seconds=1)
    assert not ledger.ledger_begin("dev_a", "lost", "agent/switch")
    clock.now = T0 + ACTION_LEDGER_TTL
    assert ledger.ledger_begin("dev_a", "lost", "agent/switch")


def test_expiry_reaches_a_device_that_never_asks_again() -> None:
    """A phone that is gone for good must not keep its answers in memory until the server
    stops: any device's call drops what expired for every device."""
    clock = Clock()
    ledger = ActionLedger(clock=clock)
    _finished(ledger, "dev_gone", "r1")
    assert ledger.ledger_begin("dev_gone", "r2", "agent/stop")
    clock.now = T0 + ACTION_LEDGER_TTL
    ledger.ledger_recent("dev_other")
    assert ledger._finished == {} and ledger._running == {}


def test_the_ledger_keeps_the_newest_fifty_per_device() -> None:
    ledger = ActionLedger(clock=Clock())
    for n in range(ACTION_LEDGER_SIZE + 5):
        _finished(ledger, "dev_a", f"r{n}")
    _finished(ledger, "dev_b", "kept")
    recent = [entry["request_id"] for entry in ledger.ledger_recent("dev_a")]
    assert len(recent) == ACTION_LEDGER_SIZE
    assert recent[0] == f"r{ACTION_LEDGER_SIZE + 4}" and recent[-1] == "r5"
    assert ledger.ledger_replay("dev_a", "r4") is None, "the oldest went first"
    assert ledger.ledger_recent("dev_b")[0]["request_id"] == "kept"


def test_a_request_id_that_finishes_again_is_shown_once_as_the_newest() -> None:
    ledger = ActionLedger(clock=Clock())
    _finished(ledger, "dev_a", "r1")
    _finished(ledger, "dev_a", "r2")
    _finished(ledger, "dev_a", "r1", 409)
    assert [(e["request_id"], e["status"]) for e in ledger.ledger_recent("dev_a")] == [
        ("r1", 409),
        ("r2", 200),
    ]


def test_a_new_app_starts_with_an_empty_ledger_of_its_own(runtime: Runtime, tmp_path: Path) -> None:
    first = build_app(runtime, sources=_sources(), dist_dir=tmp_path)
    second = build_app(runtime, sources=_sources(), dist_dir=tmp_path)
    assert isinstance(first.kit.ledger, ActionLedger)
    assert first.kit.ledger is not second.kit.ledger
    assert new_action_ledger().ledger_recent("dev_a") == []


# --- the ledger behind the routes: replays, GET api/actions/recent, the action frame -------


def _noting(ran: list[dict[str, Any]]) -> Writes:
    def note(body: dict[str, Any]) -> tuple[dict[str, object], str]:
        ran.append(body)
        if body.get("text") == "stale":
            raise RequestError(409, "stale", "gone", current={"agent_id": "agt_2"})
        return {"event": len(ran)}, f"note seq={len(ran)}"

    return Writes({"note": note})


def test_a_retried_request_id_is_answered_from_the_real_ledger(
    runtime: Runtime, tmp_path: Path
) -> None:
    ran: list[dict[str, Any]] = []
    app = build_app(runtime, sources=_sources(), writes=_noting(ran), dist_dir=tmp_path)
    runtime.set_allow_write(True)
    client = _unlocked(app, runtime)
    url = f"{base(runtime)}/api/note"
    first = client.post(url, json={"text": "hi", "request_id": "c0ffee"})
    again = client.post(url, json={"text": "hi", "request_id": "c0ffee"})
    assert first.json() == again.json() == {"event": 1}
    refused = client.post(url, json={"text": "stale", "request_id": "r2"})
    replayed = client.post(url, json={"text": "stale", "request_id": "r2"})
    assert refused.status_code == replayed.status_code == 409
    assert (
        refused.json()
        == replayed.json()
        == {
            "error": "stale",
            "message": "gone",
            "current": {"agent_id": "agt_2"},
        }
    )
    assert len(ran) == 2, "each request id ran once"


def test_get_actions_recent_shows_only_the_asking_devices_requests(
    runtime: Runtime, tmp_path: Path
) -> None:
    ran: list[dict[str, Any]] = []
    app = build_app(runtime, sources=_sources(), writes=_noting(ran), dist_dir=tmp_path)
    runtime.set_allow_write(True)
    mine, theirs = _unlocked(app, runtime), _unlocked(app, runtime)
    url = f"{base(runtime)}/api/note"
    mine.post(url, json={"text": "a", "request_id": "mine-1"})
    mine.post(url, json={"text": "b"})  # no request id: nothing to remember
    theirs.post(url, json={"text": "c", "request_id": "theirs-1"})
    recent = mine.get(f"{base(runtime)}/api/actions/recent")
    assert recent.status_code == 200
    assert [
        (e["request_id"], e["endpoint"], e["status"], e["body"]) for e in recent.json()["actions"]
    ] == [("mine-1", "note", 200, {"event": 1})]
    other = theirs.get(f"{base(runtime)}/api/actions/recent").json()["actions"]
    assert [entry["request_id"] for entry in other] == ["theirs-1"]


def test_get_actions_recent_answers_while_writes_are_off(runtime: Runtime, tmp_path: Path) -> None:
    """A read: a phone learns how its last action ended even after writes were turned off."""
    app = build_app(runtime, sources=_sources(), dist_dir=tmp_path)
    client = _unlocked(app, runtime)
    assert runtime.allow_write is False
    response = client.get(f"{base(runtime)}/api/actions/recent")
    assert (response.status_code, response.json()) == (200, {"actions": []})


def _until(ws: Any, kind: str, *, limit: int = 60) -> dict[str, Any]:
    for _ in range(limit):
        frame: dict[str, Any] = json.loads(ws.receive_text())
        if frame["type"] == kind:
            return frame
    raise AssertionError(f"no {kind} frame in {limit}")


def test_the_action_frame_carries_this_devices_ledger_and_no_one_elses(
    runtime: Runtime, tmp_path: Path
) -> None:
    ran: list[dict[str, Any]] = []
    app = build_app(runtime, sources=_sources(), writes=_noting(ran), dist_dir=tmp_path, tick=0.02)
    runtime.set_allow_write(True)
    mine, theirs = _unlocked(app, runtime), _unlocked(app, runtime)
    url = f"{base(runtime)}/api/note"
    theirs.post(url, json={"text": "c", "request_id": "theirs-1"})
    with mine.websocket_connect(f"{base(runtime)}/ws") as ws:
        first = [json.loads(ws.receive_text())["type"] for _ in range(3)]
        assert first == ["board", "fleet", "remote"]
        mine.post(url, json={"text": "a", "request_id": "mine-1"})
        frame = _until(ws, "action")
    assert [entry["request_id"] for entry in frame["payload"]["actions"]] == ["mine-1"]
