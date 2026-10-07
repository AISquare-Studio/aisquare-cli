"""Agent actions from the phone (SPEC §3): tell, stop, restart, switch, and the request ledger.

The fleet service is replaced by recorders (``fleet_service.tell/stop/restart/switch``),
tmux by a pane that writes down what it was sent, and needs-you's view of the agent
(``remote_needs.needs_agent_now`` and its predicates) by a fake whose dialog closes on
an Escape, as Claude Code's does. The project and its rows are real, in the isolated
store, so a pin is checked against what ``fleet ls --all`` would show.
"""

from __future__ import annotations

import dataclasses
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from starlette.requests import Request
from starlette.responses import Response
from typer.testing import CliRunner

from aisquare.cli.app import app as cli
from aisquare.core.paths import remote_audit_path
from aisquare.core.store import store_session
from aisquare.core.tmux import TmuxError
from aisquare.core.workspace import find_project_root, project_id_for
from aisquare.models import FleetAgent, FleetAgentState, FleetAgentStatus, ProjectInfo, TeamTask
from aisquare.services import fleet as fleet_service
from aisquare.services import remote_actions, remote_needs, remote_server
from aisquare.services.fleet import RestartReceipt, StopReceipt, SwitchReceipt, TellResult
from aisquare.services.remote_actions import (
    ACTION_AUDIT_EXCERPT,
    ACTION_ENDPOINTS,
    ACTION_LEDGER_SIZE,
    ACTION_LEDGER_TTL,
    TELL_TEXT_MAX,
    ActionLedger,
    action_audit_excerpt,
    action_handlers,
    action_interrupt_wait,
    fleet_refusal,
    new_action_ledger,
)
from aisquare.services.remote_needs import (
    AgentNow,
    NeedsItem,
    needs_at_input_prompt,
    needs_dialog_open,
    needs_item_current,
)
from aisquare.services.remote_server import (
    COOKIE,
    Device,
    RequestError,
    Runtime,
    Sources,
    Writes,
    build_app,
    live_writes,
    remote_agent_lock,
    write_endpoint_names,
)
from aisquare.services.team import TeamDisabledError
from aisquare.services.transcript import PendingTool, TranscriptTail
from tests.remote_kit_helpers import base, make_client, make_runtime, receive_within, unlock


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


def _audit_lines() -> list[str]:
    path = remote_audit_path()
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _writes_audited() -> list[tuple[str, str]]:
    """``(endpoint, summary)`` of every write's audit line, in order.

    Writes only, the names ``POST api/{name}`` takes: what they leave is what this
    file is about, and an unlock may leave a line of its own.
    """
    writes = set(write_endpoint_names())
    fields = [line.split(" ", 3) for line in _audit_lines()]
    return [(field[2], field[3]) for field in fields if field[2] in writes]


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
    audited = RequestError(409, "still_busy", "Escape was sent", audit="tell coder-1 escape=sent")
    assert audited.request_error_body() == {"error": "still_busy", "message": "Escape was sent"}


def test_a_dispatched_refusal_that_still_did_something_is_audited(
    runtime: Runtime, tmp_path: Path
) -> None:
    """The audit line is the refusal's own, and the body says nothing of it."""

    def half_done(body: dict[str, Any]) -> tuple[dict[str, object], str]:
        raise RequestError(
            409, "still_busy", "Escape was sent", audit="tell coder-1 escape=sent refused"
        )

    writes = Writes({"note": half_done, "task/done": _stale})
    app = build_app(runtime, sources=_sources(), writes=writes, dist_dir=tmp_path)
    runtime.set_allow_write(True)
    client = _unlocked(app, runtime)
    response = client.post(f"{base(runtime)}/api/note", json={})
    assert (response.status_code, response.json()) == (
        409,
        {"error": "still_busy", "message": "Escape was sent"},
    )
    assert client.post(f"{base(runtime)}/api/task/done", json={}).status_code == 409
    assert _writes_audited() == [("note", "tell coder-1 escape=sent refused")], (
        "a refusal that did nothing is not on the trail"
    )


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
    """The next frame of ``kind``: a failed test, not a hang, when the socket goes quiet."""
    for _ in range(limit):
        message = receive_within(ws)
        assert message["type"] == "websocket.send", f"the socket ended: {message}"
        frame: dict[str, Any] = json.loads(message["text"])
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


# --- the actions: a real project and its rows; the fleet, tmux and needs-you faked ---------

LABEL = "coder-1"
PINNED: dict[str, object] = {"agent": LABEL, "agent_id": "agt_one", "confirm": LABEL}
PINNED_ACTIONS = ("agent/stop", "agent/restart", "agent/switch")
DOING = {"agent/stop": "stopping", "agent/restart": "restarting", "agent/switch": "switching"}


def _acted_on(name: str, project: ProjectInfo) -> str:
    """How the audit line of a pinned action refused after it acted begins: the row it
    acted on, and the flags :data:`PINNED` leaves at their defaults."""
    verb = name.removeprefix("agent/")
    flags = {"stop": " force=no", "restart": " fresh=no", "switch": ""}[verb]
    return f"{verb} coder-1@{project.id} agent=agt_one{flags}"


@pytest.fixture
def project(isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProjectInfo:
    """The current project, ``init``-ed in a directory of its own (``api``)."""
    root = tmp_path / "api"
    root.mkdir()
    monkeypatch.chdir(root)
    assert CliRunner().invoke(cli, ["init", "--local", "--no-onboard", "--yes"]).exit_code == 0
    return fleet_service.resolve_project(None)


def _agent(
    project: ProjectInfo, agent_id: str = "agt_one", *, ended: bool = False, minute: int = 0
) -> FleetAgent:
    created = T0 + timedelta(minutes=minute)
    return FleetAgent(
        id=agent_id,
        project_id=project.id,
        label=LABEL,
        role="coder",
        tmux_socket="asq-test",
        pane_id=f"%{7 + minute}",
        cwd=project.root,
        spawned_by="user",
        created_at=created,
        ended_at=created + timedelta(seconds=30) if ended else None,
        exit_status=1 if ended else None,
    )


def _row(
    project: ProjectInfo, agent_id: str = "agt_one", *, ended: bool = False, minute: int = 0
) -> FleetAgent:
    """A row in the store under :data:`LABEL`: live, or ended with exit 1."""
    agent = _agent(project, agent_id, ended=ended, minute=minute)
    with store_session() as store:
        store.upsert_fleet_agent(agent)
    return agent


def _replaced(project: ProjectInfo) -> None:
    """What a manager's ``fleet restart coder-1`` leaves: agt_one ended, agt_new holding it."""
    _row(project, "agt_one", ended=True)
    _row(project, "agt_new", minute=1)


@dataclass(frozen=True)
class Receipts:
    """What each faked fleet call answers: the replaced row is agt_one, the new one agt_two."""

    tell: TellResult
    stop: StopReceipt
    restart: RestartReceipt
    switch: SwitchReceipt


@pytest.fixture
def receipts(project: ProjectInfo) -> Receipts:
    old = _agent(project, "agt_one", ended=True)
    new = _agent(project, "agt_two", minute=1)
    task = TeamTask(
        id="tsk_1", project_id=project.id, key="k", title="ship", created_at=T0, updated_at=T0
    )
    return Receipts(
        tell=TellResult(delivered=False, how="it is working — filed as board note #4 to coder-1"),
        stop=StopReceipt(old, [task]),
        restart=RestartReceipt(
            replaced=old,
            started=new,
            resumed=True,
            was_running=True,
            tmux_session="asq-api",
            notes=["resumed its transcript"],
        ),
        switch=SwitchReceipt(
            stopped=old,
            started=new,
            from_slot=1,
            to_slot=2,
            resumed=True,
            tmux_session="asq-api",
            notes=["headroom: slot 2 has the most left"],
        ),
    )


@pytest.fixture
def log() -> list[str]:
    """What reached the agent and the fleet, in order: ``key Escape``, ``paste``, ``fleet stop``."""
    return []


class FleetCalls:
    """``fleet_service.tell/stop/restart/switch``, replaced: every call is written down and
    answered with what the test gave for it, a receipt or an error to raise."""

    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, object]]] = []
        self.answers: dict[str, object] = {}
        self.entered = threading.Event()
        self.release: threading.Event | None = None
        """Set by a test: every call waits for it, a request held mid-flight."""

    def fake(self, name: str) -> Callable[..., object]:
        def call(*args: Any, **kwargs: object) -> object:
            self.calls.append((name, args, kwargs))
            self.log.append(f"fleet {name}")
            self.entered.set()
            if self.release is not None:
                assert self.release.wait(10), "the test never let the call finish"
            answer = self.answers[name]
            if isinstance(answer, BaseException):
                raise answer
            return answer

        return call

    def names(self) -> list[str]:
        return [name for name, _args, _kwargs in self.calls]


@pytest.fixture
def fleet(monkeypatch: pytest.MonkeyPatch, receipts: Receipts, log: list[str]) -> FleetCalls:
    calls = FleetCalls(log)
    for name in ("tell", "stop", "restart", "switch"):
        calls.answers[name] = getattr(receipts, name)
        monkeypatch.setattr(fleet_service, name, calls.fake(name))
    return calls


class FakePane:
    """The agent's tmux, as far as an action uses it: what reached which pane, in order."""

    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.sent: list[tuple[str, str, str]] = []
        """``(pane id, "key" or "paste", the key or the text)``."""
        self.sockets: list[str] = []
        self.failing: set[str] = set()
        """``"paste"`` or a key name: that call raises ``TmuxError``."""

    def send_keys(self, pane_id: str, *keys: str) -> None:
        for key in keys:
            if key in self.failing:
                raise TmuxError(f"send-keys {key}: lost server")
            self.sent.append((pane_id, "key", key))
            self.log.append(f"key {key}")

    def paste(self, pane_id: str, text: str) -> None:
        if "paste" in self.failing:
            raise TmuxError(f"can't find pane: {pane_id}")
        self.sent.append((pane_id, "paste", text))
        self.log.append("paste")

    def keys(self) -> list[str]:
        return [what for _pane, kind, what in self.sent if kind == "key"]


@pytest.fixture
def pane(monkeypatch: pytest.MonkeyPatch, log: list[str]) -> FakePane:
    fake = FakePane(log)

    def server_for(socket: str, config: object = None) -> FakePane:
        fake.sockets.append(socket)
        return fake

    monkeypatch.setattr(fleet_service, "server_for", server_for)
    return fake


class FakeNeeds:
    """needs-you's view of the agent, changing the way Claude Code's screen does: one Escape
    closes the dialog, or stops a working agent at its prompt. Each change shows from the
    ``lag``-th read after the Escape, as a pane shows it a moment later.

    ``needs_dialog_open`` answers as SPEC §4.5's does for what the fake shows. A dialog is
    open while the fake draws one, while the row reads ``attention`` that no Escape has
    answered, and while one of the agent's current items is a dialog (:func:`_a_dialog`). A
    test that wants an agent with no dialog must therefore show one with none: a card's
    ``limited`` item on a row that reads ``limited``, say, not on one that reads
    ``working``. ``at_prompt`` stands for the rest of what §4.5 reads for the prompt, a
    quiet pane and a transcript whose newest record is an interruption or the agent's own
    words, which the fake does not write out.

    ``tail`` and ``pane_quiet`` are handed over as they are, for the predicates that read
    them: an Escape that closes the dialog answers the tail's pending tools too, as Claude
    Code records a rejected or interrupted tool use."""

    def __init__(self, pane: FakePane) -> None:
        self.pane = pane
        self.state: FleetAgentState = "working"
        self.dialog = False
        self.at_prompt = False
        self.interrupted = False
        """An Escape landed: the transcript's newest record is the interruption, which
        answers an ``attention``."""
        self.pane_is_agent = True
        self.window_gone = False
        self.escape_closes_dialog = True
        self.escape_stops_agent = True
        self.lag = 1
        self.items: tuple[NeedsItem, ...] = ()
        self.tail: TranscriptTail | None = None
        self.pane_quiet: bool | None = True
        self.before_read: Callable[[], None] | None = None
        self.reads = 0
        self._escapes = 0
        self._since_escape: int | None = None
        self._views: list[tuple[AgentNow, bool, bool, bool]] = []

    def needs_agent_now(
        self, project: ProjectInfo, label: str, *, now: datetime | None = None
    ) -> AgentNow:
        self.reads += 1
        if self.before_read is not None:
            self.before_read()
        self._land_escapes()
        with store_session() as store:
            row = store.fleet_agent_by_label(project.id, label, live_only=False)
        if row is None:
            raise fleet_service.NoSuchAgent(f"no agent {label!r}")
        status = None if self.window_gone else FleetAgentStatus(agent=row, state=self.state)
        snap = AgentNow(
            project=project,
            status=status,
            tail=self.tail,
            pane_is_agent=self.pane_is_agent and status is not None,
            pane_quiet=self.pane_quiet,
            items=self.items,
        )
        self._views.append((snap, self.dialog, self.at_prompt, self.interrupted))
        return snap

    def _land_escapes(self) -> None:
        escapes = self.pane.keys().count("Escape")
        if escapes > self._escapes:
            self._escapes, self._since_escape = escapes, 0
        if self._since_escape is None:
            return
        self._since_escape += 1
        if self._since_escape < self.lag:
            return
        self._since_escape = None
        self.interrupted = True
        if self.escape_closes_dialog:
            self.dialog = False
            if self.tail is not None:
                self.tail = dataclasses.replace(self.tail, pending=(), newest="interrupted")
        if self.escape_stops_agent and not self.dialog:
            self.at_prompt = True

    def _view(self, snap: AgentNow) -> tuple[bool, bool, bool]:
        """What the pane showed when ``snap`` was read: a dialog, the prompt, an interruption."""
        for seen, dialog, prompt, interrupted in reversed(self._views):
            if seen is snap:
                return dialog, prompt, interrupted
        raise AssertionError("a snapshot needs_agent_now never gave")

    def needs_dialog_open(self, snap: AgentNow) -> bool:
        if snap.status is None or not snap.pane_is_agent:
            return False
        drawn, _prompt, interrupted = self._view(snap)
        state = snap.status.state
        unanswered = state == "attention" and not interrupted
        return drawn or unanswered or any(_a_dialog(item, state) for item in snap.items)

    def needs_at_input_prompt(self, snap: AgentNow) -> bool:
        return snap.pane_is_agent and self._view(snap)[1] and not self.needs_dialog_open(snap)

    def needs_item_current(self, snap: AgentNow, item_id: str) -> bool:
        return item_id in {item.id for item in snap.items}


def _a_dialog(item: NeedsItem, state: str) -> bool:
    """SPEC §4.5: a current prompt, question or plan is a dialog on the agent's screen, and so
    is a ``limited`` item of a row that does not read ``limited``: the usage-limit dialog."""
    return item.kind in ("permission", "question", "plan") or (
        item.kind == "limited" and state != "limited"
    )


@pytest.fixture
def needs(monkeypatch: pytest.MonkeyPatch, pane: FakePane) -> FakeNeeds:
    fake = FakeNeeds(pane)
    monkeypatch.setattr(remote_needs, "needs_agent_now", fake.needs_agent_now)
    monkeypatch.setattr(remote_needs, "needs_dialog_open", fake.needs_dialog_open)
    monkeypatch.setattr(remote_needs, "needs_at_input_prompt", fake.needs_at_input_prompt)
    monkeypatch.setattr(remote_needs, "needs_item_current", fake.needs_item_current)
    monkeypatch.setattr(remote_needs, "DIALOG_SETTLE_SECONDS", 0.05)
    monkeypatch.setattr(remote_actions, "ACTION_POLL_SECONDS", 0.001)
    monkeypatch.setattr(remote_actions, "action_interrupt_wait", lambda: 0.05)
    return fake


def _item(project: ProjectInfo, item_id: str, kind: str = "limited") -> NeedsItem:
    """A card about coder-1, by default its usage limit: on a row that reads ``limited``,
    the card a Switch is sent from (on any other row, the limit's dialog)."""
    return NeedsItem(
        id=item_id,
        kind=kind,
        project_id=project.id,
        project_name="api",
        agent=LABEL,
        agent_id="agt_one",
        reason="coder-1 hit its usage limit · resets 14:00",
        excerpt="",
        detail={},
        answers=(),
        since=T0,
        actions=("switch", "open", "dismiss"),
        push_after=None,
    )


class Phone:
    """An unlocked phone posting to one app."""

    def __init__(self, app: Any, runtime: Runtime) -> None:
        self.app = app
        self.runtime = runtime
        self.client = _unlocked(app, runtime)

    def post(self, name: str, **body: object) -> Any:
        return self.client.post(f"{base(self.runtime)}/api/{name}", json=body)

    def audit(self) -> list[tuple[str, str]]:
        return _writes_audited()


@pytest.fixture
def phone(runtime: Runtime, tmp_path: Path, project: ProjectInfo) -> Phone:
    """A phone with writes on, on an app whose writes are the real ones."""
    app = build_app(runtime, sources=_sources(), writes=live_writes(), dist_dir=tmp_path)
    runtime.set_allow_write(True)
    return Phone(app, runtime)


# --- the actions are writes ------------------------------------------------------------------


def test_the_write_dispatcher_answers_every_action() -> None:
    assert tuple(action_handlers()) == ACTION_ENDPOINTS
    assert set(ACTION_ENDPOINTS) <= set(write_endpoint_names())
    assert set(ACTION_ENDPOINTS) <= set(live_writes().handlers)


@pytest.mark.parametrize("name", ACTION_ENDPOINTS)
def test_every_action_is_403_until_writes_are_on(
    phone: Phone, fleet: FleetCalls, needs: FakeNeeds, project: ProjectInfo, name: str
) -> None:
    _row(project)
    phone.runtime.set_allow_write(False)
    refused = phone.post(name, **PINNED, text="carry on")
    assert refused.status_code == 403 and refused.json()["error"] == "read_only"
    assert fleet.calls == [] and needs.reads == 0
    phone.runtime.set_allow_write(True)
    assert phone.post(name, **PINNED, text="carry on").status_code == 200


# --- stop, restart, switch: what reaches the fleet, and what comes back ----------------------


def test_stop_stops_the_pinned_row_and_audits_what_it_did(
    phone: Phone, fleet: FleetCalls, needs: FakeNeeds, project: ProjectInfo, receipts: Receipts
) -> None:
    _row(project)
    response = phone.post("agent/stop", **PINNED)
    assert response.status_code == 200, response.text
    ((name, args, kwargs),) = fleet.calls
    assert (name, args[0].id, args[1:], kwargs) == (
        "stop",
        project.id,
        (LABEL,),
        {"force": False, "agent_id": "agt_one"},
    )
    assert response.json() == {
        "agent": receipts.stop.agent.model_dump(mode="json"),
        "claims_released": ["tsk_1"],
        "release_failed": None,
        "project": project.id,
    }
    assert phone.audit() == [
        ("agent/stop", f"stop coder-1@{project.id} agent=agt_one force=no dismissed=no released=1")
    ]


def test_a_stop_whose_claims_were_not_released_is_still_a_200_that_says_so(
    phone: Phone, fleet: FleetCalls, needs: FakeNeeds, project: ProjectInfo, receipts: Receipts
) -> None:
    _row(project)
    said = "the store was locked; the claims stay with the ended session"
    fleet.answers["stop"] = StopReceipt(receipts.stop.agent, [], release_failed=said)
    response = phone.post("agent/stop", **PINNED)
    assert response.status_code == 200
    assert response.json()["release_failed"] == said
    assert response.json()["claims_released"] == []


def test_restart_passes_fresh_the_pin_and_spawned_by_user(
    phone: Phone, fleet: FleetCalls, needs: FakeNeeds, project: ProjectInfo
) -> None:
    _row(project)
    response = phone.post("agent/restart", **PINNED, fresh=True)
    assert response.status_code == 200, response.text
    ((name, args, kwargs),) = fleet.calls
    assert (name, args[0].id, args[1:]) == ("restart", project.id, (LABEL,))
    assert kwargs == {"fresh": True, "spawned_by": "user", "agent_id": "agt_one"}
    assert phone.audit() == [
        (
            "agent/restart",
            f"restart coder-1@{project.id} fresh=yes dismissed=no resumed=yes started=agt_two",
        )
    ]


def test_switch_passes_to_fresh_and_reason_and_spawned_by_user(
    phone: Phone, fleet: FleetCalls, needs: FakeNeeds, project: ProjectInfo
) -> None:
    _row(project)
    response = phone.post("agent/switch", **PINNED, to="2", reason="session limit")
    assert response.status_code == 200, response.text
    ((name, args, kwargs),) = fleet.calls
    assert (name, args[0].id, args[1:]) == ("switch", project.id, (LABEL,))
    assert kwargs == {"to": "2", "fresh": False, "reason": "session limit", "spawned_by": "user"}
    assert phone.audit() == [
        (
            "agent/switch",
            f"switch coder-1@{project.id} slot=1->2 dismissed=no resumed=yes started=agt_two",
        )
    ]


def test_a_switch_from_no_recorded_slot_audits_a_dash(
    phone: Phone, fleet: FleetCalls, needs: FakeNeeds, project: ProjectInfo, receipts: Receipts
) -> None:
    _row(project)
    fleet.answers["switch"] = dataclasses.replace(receipts.switch, from_slot=None)
    assert phone.post("agent/switch", **PINNED).status_code == 200
    assert phone.audit()[0][1].startswith(f"switch coder-1@{project.id} slot=-->2 ")


@pytest.mark.parametrize(
    ("name", "argv"),
    [
        ("agent/stop", ["fleet", "stop", LABEL]),
        ("agent/restart", ["fleet", "restart", LABEL]),
        ("agent/switch", ["fleet", "switch", LABEL]),
    ],
)
def test_an_action_answers_what_the_cli_prints_plus_its_project(
    phone: Phone,
    fleet: FleetCalls,
    needs: FakeNeeds,
    project: ProjectInfo,
    name: str,
    argv: list[str],
) -> None:
    """The phone reads the CLI's own ``--json`` payload, with nothing invented beside it."""
    _row(project)
    printed = CliRunner().invoke(cli, ["--json", *argv])
    assert printed.exit_code == 0, printed.output
    response = phone.post(name, **PINNED)
    assert response.status_code == 200
    answered = response.json()
    assert answered.pop("project") == project.id
    assert answered == json.loads(printed.stdout)


def test_a_named_project_is_the_one_acted_on_and_an_unknown_one_is_404(
    phone: Phone, fleet: FleetCalls, needs: FakeNeeds, tmp_path: Path
) -> None:
    other_root = tmp_path / "web"
    other_root.mkdir()
    other = ProjectInfo(
        id=project_id_for(find_project_root(other_root)), root=other_root, linked_repos=[]
    )
    with store_session() as store:
        store.ensure_project(other)
        store.onboard_project(other)
    _row(other)
    response = phone.post("agent/stop", **PINNED, project=other.id)
    assert response.status_code == 200, response.text
    assert response.json()["project"] == other.id
    assert fleet.calls[0][1][0].id == other.id
    missing = phone.post("agent/stop", **PINNED, project="no-such-project")
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"
    assert len(fleet.calls) == 1


# --- confirm and the pin -------------------------------------------------------------------


@pytest.mark.parametrize("name", PINNED_ACTIONS)
@pytest.mark.parametrize(
    "confirm", [{}, {"confirm": None}, {"confirm": "Coder-1"}, {"confirm": " coder-1"}]
)
def test_the_label_must_be_confirmed_exactly(
    phone: Phone,
    fleet: FleetCalls,
    needs: FakeNeeds,
    project: ProjectInfo,
    name: str,
    confirm: dict[str, object],
) -> None:
    _row(project)
    response = phone.post(name, agent=LABEL, agent_id="agt_one", **confirm)
    assert response.status_code == 400
    assert response.json() == {
        "error": "confirm_required",
        "message": "confirm by sending confirm=coder-1",
    }
    assert fleet.calls == [] and needs.reads == 0


@pytest.mark.parametrize("name", PINNED_ACTIONS)
def test_stop_restart_and_switch_need_the_agent_id_they_mean(
    phone: Phone, fleet: FleetCalls, needs: FakeNeeds, project: ProjectInfo, name: str
) -> None:
    _row(project)
    response = phone.post(name, agent=LABEL, confirm=LABEL)
    assert response.status_code == 400
    assert response.json() == {"error": "invalid", "message": "'agent_id' is required"}
    assert fleet.calls == []


@pytest.mark.parametrize("name", PINNED_ACTIONS)
def test_a_row_replaced_since_is_409_stale_before_anything_reaches_the_agent(
    phone: Phone,
    fleet: FleetCalls,
    needs: FakeNeeds,
    pane: FakePane,
    project: ProjectInfo,
    name: str,
) -> None:
    """The manager restarted coder-1 while the phone showed the old one: acting by label
    would stop, restart or move the newcomer."""
    _replaced(project)
    response = phone.post(name, **PINNED, needs_id="ny_1", dismiss_dialog=True)
    assert response.status_code == 409
    assert response.json() == {
        "error": "stale",
        "message": "'coder-1' is another agent now (agt_new) — nothing was done",
        "current": {"agent_id": "agt_new"},
    }
    assert fleet.calls == [] and needs.reads == 0 and pane.sent == []
    assert phone.audit() == []


@pytest.mark.parametrize("name", PINNED_ACTIONS)
def test_a_pinned_label_no_row_holds_is_stale_with_no_current_agent(
    phone: Phone, fleet: FleetCalls, needs: FakeNeeds, project: ProjectInfo, name: str
) -> None:
    response = phone.post(name, **PINNED)
    assert response.status_code == 409
    assert response.json() == {
        "error": "stale",
        "message": "there is no agent 'coder-1' in api now — nothing was done",
        "current": {"agent_id": None},
    }
    assert fleet.calls == []


def test_a_label_no_row_holds_makes_no_lock(
    phone: Phone, fleet: FleetCalls, needs: FakeNeeds, project: ProjectInfo
) -> None:
    """The lock registry is process-wide and never shrinks, and a label is whatever a body
    says: a phone posting made-up labels must not grow it."""
    response = phone.post("agent/stop", **{**PINNED, "agent": "ghost-7", "confirm": "ghost-7"})
    assert response.status_code == 409
    assert (project.id, "ghost-7") not in remote_server._agent_locks


@pytest.mark.parametrize("name", PINNED_ACTIONS)
def test_a_card_whose_item_is_gone_is_409_stale_with_the_agents_items_now(
    phone: Phone, fleet: FleetCalls, needs: FakeNeeds, project: ProjectInfo, name: str
) -> None:
    """The automatic hand-over moved coder-1 while the card for its limit was on screen:
    the card's item went with it, and a Switch from that card must not move it again."""
    _row(project)
    now = _item(project, "ny_after", kind="asked")
    needs.items = (now,)
    response = phone.post(name, **PINNED, needs_id="ny_before")
    assert response.status_code == 409
    assert response.json() == {
        "error": "stale",
        "message": "coder-1 no longer shows what that card was about — nothing was done",
        "current": [now.needs_item_json()],
    }
    assert fleet.calls == []


def test_a_card_whose_item_is_still_current_goes_through_with_one_read(
    phone: Phone, fleet: FleetCalls, needs: FakeNeeds, project: ProjectInfo
) -> None:
    """A parked agent's card: the row reads ``limited``, so its item is no dialog."""
    _row(project)
    needs.state = "limited"
    needs.items = (_item(project, "ny_limit"),)
    response = phone.post("agent/switch", **PINNED, needs_id="ny_limit")
    assert response.status_code == 200, response.text
    assert fleet.names() == ["switch"]
    assert needs.reads == 1, "the card's read serves the dialog guard too"


@pytest.mark.parametrize(
    ("field", "value", "status"),
    [
        ("needs_id", "n" * 65, 413),
        ("to", "x" * 201, 413),
        ("reason", "x" * 201, 413),
        ("needs_id", 7, 400),
        ("force", "false", 400),
        ("fresh", 1, 400),
        ("dismiss_dialog", "yes", 400),
    ],
)
def test_a_field_that_is_too_long_or_the_wrong_type_is_refused(
    phone: Phone,
    fleet: FleetCalls,
    needs: FakeNeeds,
    project: ProjectInfo,
    field: str,
    value: object,
    status: int,
) -> None:
    """``"false"`` for ``force`` is the case that matters: read as true, it would kill the
    agent without its ``/exit``."""
    _row(project)
    name = "agent/stop" if field == "force" else "agent/switch"
    response = phone.post(name, **PINNED, **{field: value})
    assert response.status_code == status, response.text
    assert response.json()["error"] == ("too_large" if status == 413 else "invalid")
    assert fleet.calls == []


@pytest.mark.parametrize("name", ACTION_ENDPOINTS)
@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_needs_id_is_a_400_not_a_card_check_turned_off(
    phone: Phone, fleet: FleetCalls, needs: FakeNeeds, project: ProjectInfo, name: str, blank: str
) -> None:
    """The card's item is gone, so its id would be 409 ``stale``. A page that sends the id
    blank (``card.id ?? ""``) must not get the action done as if it came from no card."""
    _row(project)
    response = phone.post(name, **PINNED, text="hi", needs_id=blank)
    assert (response.status_code, response.json()) == (
        400,
        {"error": "invalid", "message": "'needs_id' is blank: send the id, or leave it out"},
    )
    assert fleet.calls == [] and needs.reads == 0


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_agent_id_on_a_tell_is_a_400_not_a_tell_to_whoever_holds_the_label(
    phone: Phone, fleet: FleetCalls, needs: FakeNeeds, project: ProjectInfo, blank: str
) -> None:
    _replaced(project)
    response = phone.post("agent/tell", agent=LABEL, agent_id=blank, text="hi")
    assert (response.status_code, response.json()) == (
        400,
        {"error": "invalid", "message": "'agent_id' is blank: send the id, or leave it out"},
    )
    assert fleet.calls == []


# --- the dialog guard ------------------------------------------------------------------------


@pytest.mark.parametrize("name", PINNED_ACTIONS)
def test_an_open_dialog_refuses_the_action_and_nothing_reaches_the_agent(
    phone: Phone,
    fleet: FleetCalls,
    needs: FakeNeeds,
    pane: FakePane,
    project: ProjectInfo,
    name: str,
) -> None:
    """The stop's ``/exit`` and Enter would answer the dialog: "1. Yes" to a Bash command."""
    _row(project)
    needs.dialog = True
    response = phone.post(name, **PINNED)
    assert response.status_code == 409
    assert response.json() == {
        "error": "dialog_open",
        "message": f"coder-1 is showing a prompt; {DOING[name]} would answer it — "
        "send dismiss_dialog: true to press Esc (No) first",
    }
    assert fleet.calls == [] and pane.sent == []
    assert phone.audit() == [], "nothing reached the agent, so only the ledger keeps it"


@pytest.mark.parametrize("name", PINNED_ACTIONS)
def test_dismiss_dialog_sends_one_escape_then_acts_once_the_dialog_closed(
    phone: Phone,
    fleet: FleetCalls,
    needs: FakeNeeds,
    pane: FakePane,
    project: ProjectInfo,
    log: list[str],
    name: str,
) -> None:
    _row(project)
    needs.dialog = True
    needs.lag = 3  # the dialog is still drawn on the first two reads after the Escape
    response = phone.post(name, **PINNED, dismiss_dialog=True)
    assert response.status_code == 200, response.text
    assert log == ["key Escape", f"fleet {name.removeprefix('agent/')}"]
    assert pane.sent == [("%7", "key", "Escape")] and pane.sockets == ["asq-test"]
    assert needs.reads == 4, "the guard's read, then a poll until the dialog had closed"
    assert "dismissed=yes" in phone.audit()[0][1]


@pytest.mark.parametrize("name", PINNED_ACTIONS)
def test_a_dialog_still_open_after_the_escape_is_409_and_nothing_else_is_done(
    phone: Phone,
    fleet: FleetCalls,
    needs: FakeNeeds,
    pane: FakePane,
    project: ProjectInfo,
    name: str,
) -> None:
    """Refused, but not before its Escape answered a prompt "No": that is on the trail."""
    _row(project)
    needs.dialog = True
    needs.escape_closes_dialog = False
    response = phone.post(name, **PINNED, dismiss_dialog=True)
    assert response.status_code == 409
    assert response.json() == {
        "error": "dialog_open",
        "message": "Escape was sent, but coder-1 still shows a prompt — nothing else was done",
    }
    assert pane.keys() == ["Escape"], "one Escape: a second one opens the Rewind selector"
    assert fleet.calls == []
    assert phone.audit() == [
        (name, f"{_acted_on(name, project)} dismissed=yes refused=dialog_open")
    ]


@pytest.mark.parametrize("name", PINNED_ACTIONS)
def test_a_row_replaced_while_the_dialog_closes_is_stale_and_says_the_escape_went(
    phone: Phone,
    fleet: FleetCalls,
    needs: FakeNeeds,
    pane: FakePane,
    project: ProjectInfo,
    name: str,
) -> None:
    """The Escape reached the pinned agent, then the manager's restart took the label: the
    sentence must not say nothing was done, and the trail keeps the Escape."""
    _row(project)
    needs.dialog = True
    needs.lag = 3

    def replaced_on_the_second_read() -> None:
        if needs.reads == 2:
            _replaced(project)

    needs.before_read = replaced_on_the_second_read
    response = phone.post(name, **PINNED, dismiss_dialog=True)
    assert response.status_code == 409
    assert response.json() == {
        "error": "stale",
        "message": "'coder-1' is another agent now (agt_new) — Escape was sent, "
        "nothing else was done",
        "current": {"agent_id": "agt_new"},
    }
    assert pane.keys() == ["Escape"] and fleet.calls == []
    assert phone.audit() == [(name, f"{_acted_on(name, project)} dismissed=yes refused=stale")]


def test_a_force_stop_types_nothing_so_it_skips_the_guard(
    phone: Phone, fleet: FleetCalls, needs: FakeNeeds, pane: FakePane, project: ProjectInfo
) -> None:
    _row(project)
    needs.dialog = True
    response = phone.post("agent/stop", **PINNED, force=True)
    assert response.status_code == 200, response.text
    assert fleet.calls[0][2] == {"force": True, "agent_id": "agt_one"}
    assert needs.reads == 0 and pane.sent == []
    assert "force=yes dismissed=no" in phone.audit()[0][1]


@pytest.mark.parametrize("name", ["agent/stop", "agent/restart"])
def test_an_ended_row_shows_no_dialog_so_it_skips_the_guard(
    phone: Phone, fleet: FleetCalls, needs: FakeNeeds, project: ProjectInfo, name: str
) -> None:
    """A crashed agent's row: Stop removes the window it left, Restart starts it again."""
    _row(project, ended=True)
    needs.dialog = True  # never asked
    response = phone.post(name, **PINNED)
    assert response.status_code == 200, response.text
    assert needs.reads == 0
    assert fleet.names() == [name.removeprefix("agent/")]


@pytest.mark.parametrize("name", PINNED_ACTIONS)
def test_a_row_replaced_while_the_guard_looked_is_stale_before_any_escape(
    phone: Phone,
    fleet: FleetCalls,
    needs: FakeNeeds,
    pane: FakePane,
    project: ProjectInfo,
    name: str,
) -> None:
    """The pin held when it was checked; the manager's restart landed a moment later. The
    Escape would have gone to the newcomer's pane."""
    _row(project)
    needs.dialog = True
    needs.before_read = lambda: _replaced(project)
    response = phone.post(name, **PINNED, dismiss_dialog=True)
    assert response.status_code == 409
    assert response.json()["current"] == {"agent_id": "agt_new"}
    assert pane.sent == [] and fleet.calls == []


# --- the fleet's own refusals ----------------------------------------------------------------

REFUSALS = [
    (fleet_service.FleetUnavailable("tmux 3.1 is too old for the fleet"), 503, "fleet_unavailable"),
    (fleet_service.NoSuchProject("no project matches 'web'"), 404, "not_found"),
    (fleet_service.NoSuchAgent("no live agent 'coder-1' in api"), 404, "no_such_agent"),
    (
        fleet_service.FleetError(
            "cannot restart 'coder-1': a hand-over is already moving it and starts the "
            "replacement itself — nothing was done"
        ),
        409,
        "fleet_error",
    ),
    (TeamDisabledError(), 409, "team_disabled"),
    (ValueError("'to' names no account"), 400, "invalid"),
]


@pytest.mark.parametrize("name", PINNED_ACTIONS)
@pytest.mark.parametrize(("error", "status", "code"), REFUSALS)
def test_a_fleet_refusal_answers_as_the_cli_maps_it_and_is_on_the_trail(
    phone: Phone,
    fleet: FleetCalls,
    needs: FakeNeeds,
    project: ProjectInfo,
    name: str,
    error: Exception,
    status: int,
    code: str,
) -> None:
    """The fleet may have acted before it refused: a restart or a switch stops the agent
    before it starts the next one, and a stop types ``/exit`` before it kills the window."""
    _row(project)
    fleet.answers[name.removeprefix("agent/")] = error
    response = phone.post(name, **PINNED)
    assert response.status_code == status
    assert response.json() == {"error": code, "message": str(error)}
    assert phone.audit() == [(name, f"{_acted_on(name, project)} dismissed=no failed={code}")]


@pytest.mark.parametrize("name", PINNED_ACTIONS)
def test_a_fleet_call_that_fails_after_a_dismissal_records_both(
    phone: Phone,
    fleet: FleetCalls,
    needs: FakeNeeds,
    pane: FakePane,
    project: ProjectInfo,
    name: str,
) -> None:
    _row(project)
    needs.dialog = True
    fleet.answers[name.removeprefix("agent/")] = fleet_service.FleetError("tmux went away")
    response = phone.post(name, **PINNED, dismiss_dialog=True)
    assert response.status_code == 409 and response.json()["error"] == "fleet_error"
    assert pane.keys() == ["Escape"]
    assert phone.audit() == [(name, f"{_acted_on(name, project)} dismissed=yes failed=fleet_error")]


def test_an_unexpected_failure_of_the_fleet_call_is_a_400_on_the_trail(
    phone: Phone, fleet: FleetCalls, needs: FakeNeeds, project: ProjectInfo
) -> None:
    """Not a refusal the fleet words, but the agent may be stopped all the same."""
    _row(project)
    fleet.answers["restart"] = RuntimeError("the store went away")
    response = phone.post("agent/restart", **PINNED)
    assert (response.status_code, response.json()) == (
        400,
        {"error": "write_failed", "message": "the store went away"},
    )
    assert phone.audit() == [
        ("agent/restart", f"{_acted_on('agent/restart', project)} dismissed=no failed=write_failed")
    ]


def test_fleet_refusal_maps_the_servers_own_lookups_too() -> None:
    """needs-you may say an agent or project is unknown in the server's own words."""
    agent = fleet_refusal(remote_server.NoSuchAgent("no agent 'coder-1'"))
    project = fleet_refusal(remote_server.NoSuchProject("no project 'web'"))
    other = fleet_refusal(fleet_service.FleetError("anything else the fleet refused"))
    assert (agent.status, agent.error) == (404, "no_such_agent")
    assert (project.status, project.error) == (404, "not_found")
    assert (other.status, other.error) == (409, "fleet_error")


# --- one action per agent; the ledger in front -------------------------------------------------


def _second_client(phone: Phone) -> Any:
    """Another connection from the same phone, for a request sent while one runs."""
    client = make_client(phone.app)
    client.cookies.set(COOKIE, phone.client.cookies[COOKIE])
    return client


def test_a_second_action_on_the_agent_while_one_runs_is_409_busy(
    phone: Phone, fleet: FleetCalls, needs: FakeNeeds, project: ProjectInfo
) -> None:
    _row(project)
    fleet.release = threading.Event()
    first: list[Any] = []
    running = threading.Thread(
        target=lambda: first.append(phone.post("agent/restart", **PINNED)), daemon=True
    )
    running.start()
    try:
        assert fleet.entered.wait(10), "the first request never reached the fleet"
        second = _second_client(phone).post(
            f"{base(phone.runtime)}/api/agent/stop", json={**PINNED, "force": True}
        )
    finally:
        fleet.release.set()
        running.join(10)
    assert second.status_code == 409
    assert second.json() == {
        "error": "busy",
        "message": "another action on coder-1 is still running",
    }
    assert first[0].status_code == 200
    assert fleet.names() == ["restart"]


def test_a_quick_answer_in_flight_makes_an_action_busy_too(
    phone: Phone, fleet: FleetCalls, needs: FakeNeeds, project: ProjectInfo
) -> None:
    """needs/answer takes the same lock for the same agent: a stop must not type ``/exit``
    into the dialog a quick answer is answering."""
    _row(project)
    held = remote_agent_lock(project.id, LABEL)
    assert held.acquire(blocking=False)
    try:
        response = phone.post("agent/stop", **PINNED)
    finally:
        held.release()
    assert response.status_code == 409 and response.json()["error"] == "busy"
    assert fleet.calls == []
    assert phone.post("agent/stop", **PINNED).status_code == 200, "released, it acts"


def test_a_replayed_request_id_answers_from_the_ledger_without_a_second_restart(
    phone: Phone, fleet: FleetCalls, needs: FakeNeeds, project: ProjectInfo
) -> None:
    """The phone slept through the restart, woke, and sent it again."""
    _row(project)
    first = phone.post("agent/restart", **PINNED, request_id="c0ffee")
    again = phone.post("agent/restart", **PINNED, request_id="c0ffee")
    assert first.status_code == again.status_code == 200
    assert first.json() == again.json()
    assert fleet.names() == ["restart"]
    assert len(phone.audit()) == 1


def test_a_request_id_still_running_is_409_in_progress(
    phone: Phone, fleet: FleetCalls, needs: FakeNeeds, project: ProjectInfo
) -> None:
    _row(project)
    fleet.release = threading.Event()
    first: list[Any] = []
    running = threading.Thread(
        target=lambda: first.append(phone.post("agent/switch", **PINNED, request_id="sw-1")),
        daemon=True,
    )
    running.start()
    try:
        assert fleet.entered.wait(10)
        retry = _second_client(phone).post(
            f"{base(phone.runtime)}/api/agent/switch", json={**PINNED, "request_id": "sw-1"}
        )
    finally:
        fleet.release.set()
        running.join(10)
    assert retry.status_code == 409 and retry.json()["error"] == "in_progress"
    assert first[0].status_code == 200 and fleet.names() == ["switch"]
    recent = phone.client.get(f"{base(phone.runtime)}/api/actions/recent").json()["actions"]
    assert [(e["request_id"], e["endpoint"], e["status"]) for e in recent] == [
        ("sw-1", "agent/switch", 200)
    ]


# --- agent/tell ------------------------------------------------------------------------------


def test_auto_is_fleet_tell_and_answers_what_the_cli_prints_plus_mode_and_project(
    phone: Phone, fleet: FleetCalls, needs: FakeNeeds, project: ProjectInfo
) -> None:
    _row(project)
    printed = CliRunner().invoke(cli, ["--json", "fleet", "tell", LABEL, "ship it"])
    assert printed.exit_code == 0, printed.output
    fleet.calls.clear()
    response = phone.post("agent/tell", agent=LABEL, text="ship it")
    assert response.status_code == 200, response.text
    ((name, args, kwargs),) = fleet.calls
    assert (name, args[0].id, args[1:], kwargs) == (
        "tell",
        project.id,
        (LABEL, "ship it"),
        {"sender": None},
    )
    answered = response.json()
    assert (answered.pop("mode"), answered.pop("project")) == ("auto", project.id)
    assert answered == json.loads(printed.stdout)
    assert needs.reads == 0, "auto is fleet tell itself, which reads the agent on its own"
    assert phone.audit() == [
        ("agent/tell", f'tell coder-1@{project.id} mode=auto delivered=no text=7ch "ship it"')
    ]


def test_prompt_types_at_an_interrupted_prompt_while_the_row_still_reads_working(
    phone: Phone,
    fleet: FleetCalls,
    needs: FakeNeeds,
    pane: FakePane,
    project: ProjectInfo,
    log: list[str],
) -> None:
    """After an Escape no Stop hook fires, so the row reads ``working`` for up to 30 min
    while the agent waits at its prompt, and ``auto`` would only have filed a note."""
    _row(project)
    needs.state, needs.at_prompt = "working", True
    text = "use the cache from #12\nthen run the tests"
    response = phone.post("agent/tell", agent=LABEL, text=text, mode="prompt")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "label": LABEL,
        "delivered": True,
        "how": "typed into its pane at its prompt",
        "mode": "prompt",
        "project": project.id,
    }
    assert log == ["paste", "key Enter"], "one bracketed paste for every line, then Enter"
    assert pane.sent == [("%7", "paste", text), ("%7", "key", "Enter")]
    assert pane.sockets == ["asq-test"] and fleet.calls == []
    assert phone.audit()[0][1].startswith(f"tell coder-1@{project.id} mode=prompt delivered=yes")


@pytest.mark.parametrize("mode", ["prompt", "interrupt"])
@pytest.mark.parametrize(
    ("setup", "code", "message"),
    [
        (
            {"dialog": True},
            "dialog_open",
            "coder-1 is showing a prompt; typing now would answer it — "
            "answer it or dismiss it first",
        ),
        (
            {"pane_is_agent": False, "state": "waiting"},
            "not_agent",
            "coder-1's pane is not running the agent (it reads waiting) — nothing was sent",
        ),
        (
            {"window_gone": True},
            "not_agent",
            "coder-1's pane is not running the agent (it reads ended) — nothing was sent",
        ),
    ],
)
def test_a_tell_that_types_refuses_a_dialog_and_a_pane_without_the_agent(
    phone: Phone,
    fleet: FleetCalls,
    needs: FakeNeeds,
    pane: FakePane,
    project: ProjectInfo,
    mode: str,
    setup: dict[str, object],
    code: str,
    message: str,
) -> None:
    """Refused before anything is sent, the interrupt's Escape included."""
    _row(project)
    for attribute, value in setup.items():
        setattr(needs, attribute, value)
    response = phone.post("agent/tell", agent=LABEL, text="hi", mode=mode)
    assert response.status_code == 409
    assert response.json() == {"error": code, "message": message}
    assert pane.sent == [] and fleet.calls == [] and phone.audit() == []


NOT_YET = (
    "coder-1 is not idle at its prompt yet — try again in a few seconds, or use Interrupt & tell"
)


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        ({"state": "working"}, "coder-1 is working — use Interrupt & tell"),
        (
            {"state": "limited"},
            "coder-1 hit its usage limit, and a message will not get past it — use Switch account",
        ),
        ({"state": "waiting"}, NOT_YET),
        ({"state": "attention", "interrupted": True}, NOT_YET),
        ({"state": "unknown"}, NOT_YET),
    ],
)
def test_prompt_will_not_type_into_an_agent_that_is_not_at_its_prompt(
    phone: Phone,
    needs: FakeNeeds,
    pane: FakePane,
    project: ProjectInfo,
    setup: dict[str, object],
    message: str,
) -> None:
    """The refusal says what the agent is doing in words, and what would work instead. An
    interrupt cannot help an agent parked on its usage limit: the message would fail on the
    same limit. An ``attention`` an Escape already answered is a pane still redrawing."""
    _row(project)
    for attribute, value in setup.items():
        setattr(needs, attribute, value)
    response = phone.post("agent/tell", agent=LABEL, text="hi", mode="prompt")
    assert response.status_code == 409
    assert response.json() == {"error": "agent_busy", "message": message}
    assert pane.sent == [] and phone.audit() == []


def test_interrupt_sends_one_escape_then_types_once_at_the_prompt(
    phone: Phone,
    fleet: FleetCalls,
    needs: FakeNeeds,
    pane: FakePane,
    project: ProjectInfo,
    log: list[str],
) -> None:
    _row(project)
    needs.state, needs.lag = "working", 3
    response = phone.post(
        "agent/tell", agent=LABEL, text="stop and fix the build", mode="interrupt"
    )
    assert response.status_code == 200, response.text
    assert response.json()["delivered"] is True
    assert (
        response.json()["how"]
        == "interrupted it with Escape, then typed into its pane at its prompt"
    )
    assert log == ["key Escape", "paste", "key Enter"], "one Escape: two open the Rewind selector"
    assert needs.reads == 4, "the first read, then a poll until the prompt was back"
    assert fleet.calls == []


def test_an_interrupt_that_does_not_stop_the_agent_types_nothing(
    phone: Phone, needs: FakeNeeds, pane: FakePane, project: ProjectInfo
) -> None:
    """Nothing typed, but the Escape cut the agent's turn short: that is on the trail."""
    _row(project)
    needs.escape_stops_agent = False
    response = phone.post("agent/tell", agent=LABEL, text="hi", mode="interrupt")
    assert response.status_code == 409
    assert response.json() == {
        "error": "still_busy",
        "message": "Escape was sent; coder-1 has not stopped yet — nothing was typed",
    }
    assert pane.keys() == ["Escape"] and [kind for _p, kind, _w in pane.sent] == ["key"]
    assert phone.audit() == [
        (
            "agent/tell",
            f"tell coder-1@{project.id} mode=interrupt delivered=no escape=sent "
            'refused=still_busy text=2ch "hi"',
        )
    ]


def test_the_interrupt_waits_out_the_quiet_window_before_the_settle_time() -> None:
    """needs_at_input_prompt wants a pane with no output for ``fleet.ACTIVITY_WINDOW``, and
    the interrupt's own redraw is output: waiting only the 3 s settle time could only
    ever have answered still_busy."""
    assert action_interrupt_wait() == (
        fleet_service.ACTIVITY_WINDOW.total_seconds() + remote_needs.DIALOG_SETTLE_SECONDS
    )
    assert action_interrupt_wait() == 8.0


class FakeTime:
    """``time`` for the module under test: sleeping moves the clock, nothing else does."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_the_interrupt_reads_the_agent_every_quarter_second_until_the_wait_is_over(
    phone: Phone,
    needs: FakeNeeds,
    project: ProjectInfo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _row(project)
    fake_time = FakeTime()
    monkeypatch.setattr(remote_actions, "time", fake_time)
    monkeypatch.setattr(remote_actions, "ACTION_POLL_SECONDS", 0.25)
    monkeypatch.setattr(remote_actions, "action_interrupt_wait", action_interrupt_wait)
    monkeypatch.setattr(remote_needs, "DIALOG_SETTLE_SECONDS", 3.0)
    needs.escape_stops_agent = False
    response = phone.post("agent/tell", agent=LABEL, text="hi", mode="interrupt")
    assert response.status_code == 409 and response.json()["error"] == "still_busy"
    assert fake_time.slept == [0.25] * 32, "8 s of polls: the quiet window, then the settle time"
    assert needs.reads == 1 + 32


@pytest.mark.parametrize("pin", [{"agent_id": "agt_one"}, {}])
def test_a_row_replaced_while_the_interrupt_waits_gets_nothing_typed(
    phone: Phone, needs: FakeNeeds, pane: FakePane, project: ProjectInfo, pin: dict[str, str]
) -> None:
    """Every read is checked against the row that held the label when the lock was taken,
    pinned or not: the Escape went to agt_one, and the text must not land in the
    newcomer's pane."""
    _row(project)
    needs.lag = 3

    def replaced_on_the_second_read() -> None:
        if needs.reads == 2:
            _replaced(project)

    needs.before_read = replaced_on_the_second_read
    response = phone.post("agent/tell", agent=LABEL, **pin, text="hi", mode="interrupt")
    assert response.status_code == 409
    assert response.json() == {
        "error": "stale",
        "message": "'coder-1' is another agent now (agt_new) — Escape was sent, "
        "nothing else was done",
        "current": {"agent_id": "agt_new"},
    }
    assert pane.sent == [("%7", "key", "Escape")], "nothing typed, into either pane"
    assert phone.audit() == [
        (
            "agent/tell",
            f"tell coder-1@{project.id} mode=interrupt delivered=no escape=sent "
            'refused=stale text=2ch "hi"',
        )
    ]


def test_a_paste_that_fails_after_the_interrupts_escape_is_on_the_trail(
    phone: Phone, needs: FakeNeeds, pane: FakePane, project: ProjectInfo
) -> None:
    _row(project)
    pane.failing = {"paste"}
    response = phone.post("agent/tell", agent=LABEL, text="hi", mode="interrupt")
    assert response.status_code == 409
    assert response.json() == {
        "error": "fleet_error",
        "message": "Escape was sent, but tmux could not type into coder-1's pane "
        "(can't find pane: %7) — nothing was typed",
    }
    assert phone.audit() == [
        (
            "agent/tell",
            f"tell coder-1@{project.id} mode=interrupt delivered=no escape=sent "
            'failed=fleet_error text=2ch "hi"',
        )
    ]


def test_a_paste_that_fails_types_nothing_and_is_a_409(
    phone: Phone, needs: FakeNeeds, pane: FakePane, project: ProjectInfo
) -> None:
    """tmux's paste is one call, so nothing reached the agent, and only the ledger keeps it."""
    _row(project)
    needs.at_prompt = True
    pane.failing = {"paste"}
    response = phone.post("agent/tell", agent=LABEL, text="hi", mode="prompt")
    assert response.status_code == 409
    assert response.json() == {
        "error": "fleet_error",
        "message": "tmux could not type into coder-1's pane (can't find pane: %7) "
        "— nothing was typed",
    }
    assert phone.audit() == []


def test_an_enter_that_fails_after_the_paste_is_a_200_that_says_it_was_not_sent(
    phone: Phone, needs: FakeNeeds, pane: FakePane, project: ProjectInfo
) -> None:
    """The text reached a live agent's prompt, unsent: that is audited, and the page keeps
    the card because ``delivered`` is false."""
    _row(project)
    needs.at_prompt = True
    pane.failing = {"Enter"}
    response = phone.post("agent/tell", agent=LABEL, text="hi", mode="prompt")
    assert response.status_code == 200
    assert response.json()["delivered"] is False
    assert response.json()["how"] == (
        "pasted it at its prompt, but tmux could not press Enter (send-keys Enter: lost "
        "server) — press Enter on the pad to send it"
    )
    assert phone.audit()[0][1].startswith(f"tell coder-1@{project.id} mode=prompt delivered=no")


@pytest.mark.parametrize(
    ("body", "status", "message"),
    [
        ({}, 400, "'text' is required: what to tell the agent"),
        ({"text": ""}, 400, "'text' is required: what to tell the agent"),
        ({"text": 7}, 400, "'text' is required: what to tell the agent"),
        ({"text": "x" * (TELL_TEXT_MAX + 1)}, 413, "'text' is over 8000 characters"),
        ({"text": "hi", "mode": "shout"}, 400, "'mode' is auto, prompt or interrupt"),
        ({"text": "hi", "mode": ["prompt"]}, 400, "'mode' is auto, prompt or interrupt"),
    ],
)
def test_a_tell_needs_text_within_the_cap_and_a_known_mode(
    phone: Phone,
    fleet: FleetCalls,
    needs: FakeNeeds,
    project: ProjectInfo,
    body: dict[str, object],
    status: int,
    message: str,
) -> None:
    _row(project)
    response = phone.post("agent/tell", agent=LABEL, **body)
    assert response.status_code == status
    assert response.json()["message"] == message
    assert fleet.calls == []


@pytest.mark.parametrize("text", [" ", "x" * TELL_TEXT_MAX])
def test_whitespace_is_text_and_the_longest_tell_is_taken(
    phone: Phone, fleet: FleetCalls, needs: FakeNeeds, project: ProjectInfo, text: str
) -> None:
    _row(project)
    response = phone.post("agent/tell", agent=LABEL, text=text, mode=None)
    assert response.status_code == 200, response.text
    assert response.json()["mode"] == "auto"
    assert fleet.calls[0][1][2] == text


def test_the_tell_audit_line_keeps_how_the_text_began_and_nothing_it_could_forge(
    phone: Phone, fleet: FleetCalls, needs: FakeNeeds, project: ProjectInfo
) -> None:
    _row(project)
    text = "first line\n2026-10-07T10:00:00+00:00 dev_x agent/stop forged\x1b[2J" + "y" * 300
    before = _audit_lines()
    response = phone.post("agent/tell", agent=LABEL, text=text)
    assert response.status_code == 200
    lines = _audit_lines()
    assert len(lines) == len(before) + 1, "a newline in the text did not begin a line of its own"
    excerpt = action_audit_excerpt(text)
    assert lines[-1].endswith(
        f'tell coder-1@{project.id} mode=auto delivered=no text={len(text)}ch "{excerpt}"'
    )
    assert excerpt.startswith("first line?2026-10-07T10:00:00+00:00 dev_x agent/stop forged?[2J")


def test_an_audit_excerpt_is_at_most_120_printable_characters() -> None:
    assert action_audit_excerpt("a\nb\tc\N{LINE SEPARATOR}d\N{RIGHT-TO-LEFT OVERRIDE}e") == (
        "a?b?c?d?e"
    )
    assert action_audit_excerpt("x" * ACTION_AUDIT_EXCERPT) == "x" * ACTION_AUDIT_EXCERPT
    cut = action_audit_excerpt("x" * (ACTION_AUDIT_EXCERPT + 1))
    assert cut == "x" * (ACTION_AUDIT_EXCERPT - 1) + "…" and len(cut) == ACTION_AUDIT_EXCERPT


def test_a_pinned_tell_goes_only_to_the_agent_it_names(
    phone: Phone, fleet: FleetCalls, needs: FakeNeeds, project: ProjectInfo
) -> None:
    _replaced(project)
    response = phone.post("agent/tell", agent=LABEL, agent_id="agt_one", text="hi")
    assert response.status_code == 409
    assert response.json()["current"] == {"agent_id": "agt_new"}
    assert fleet.calls == []


def test_a_tell_to_a_label_no_row_holds_is_404_and_makes_no_lock(
    phone: Phone, fleet: FleetCalls, needs: FakeNeeds, project: ProjectInfo
) -> None:
    response = phone.post("agent/tell", agent="ghost-7", text="hi")
    assert response.status_code == 404
    assert response.json() == {
        "error": "no_such_agent",
        "message": "no agent 'ghost-7' in api — `aisquare fleet ls --all` shows every row",
    }
    assert fleet.calls == []
    assert (project.id, "ghost-7") not in remote_server._agent_locks


def test_a_tell_from_a_card_whose_item_is_gone_is_stale(
    phone: Phone, fleet: FleetCalls, needs: FakeNeeds, project: ProjectInfo
) -> None:
    _row(project)
    response = phone.post("agent/tell", agent=LABEL, text="yes, Redis", needs_id="ny_gone")
    assert response.status_code == 409
    assert response.json()["current"] == []
    assert fleet.calls == []


def test_a_tell_takes_the_agents_lock_too(
    phone: Phone, fleet: FleetCalls, needs: FakeNeeds, project: ProjectInfo
) -> None:
    _row(project)
    held = remote_agent_lock(project.id, LABEL)
    assert held.acquire(blocking=False)
    try:
        response = phone.post("agent/tell", agent=LABEL, text="hi")
    finally:
        held.release()
    assert response.status_code == 409 and response.json()["error"] == "busy"
    assert fleet.calls == []


@pytest.mark.parametrize(("error", "status", "code"), REFUSALS)
def test_a_refused_auto_tell_answers_as_the_cli_maps_it_and_is_on_the_trail(
    phone: Phone,
    fleet: FleetCalls,
    needs: FakeNeeds,
    project: ProjectInfo,
    error: Exception,
    status: int,
    code: str,
) -> None:
    """``fleet tell`` pastes before it presses Enter, and files a note when that fails: it
    can refuse with the text already in the pane."""
    _row(project)
    fleet.answers["tell"] = error
    response = phone.post("agent/tell", agent=LABEL, text="hi")
    assert (response.status_code, response.json()) == (
        status,
        {"error": code, "message": str(error)},
    )
    assert phone.audit() == [
        (
            "agent/tell",
            f'tell coder-1@{project.id} mode=auto delivered=no failed={code} text=2ch "hi"',
        )
    ]


# --- with needs-you's own predicates (lane C) ------------------------------------------------
#
# Above, needs-you is faked whole. Here only the agent's snapshot is: the predicates are
# the module's own, held to what SPEC §4.5 says they answer for that snapshot.


@pytest.fixture
def own_predicates(needs: FakeNeeds, monkeypatch: pytest.MonkeyPatch) -> FakeNeeds:
    monkeypatch.setattr(remote_needs, "needs_dialog_open", needs_dialog_open)
    monkeypatch.setattr(remote_needs, "needs_at_input_prompt", needs_at_input_prompt)
    monkeypatch.setattr(remote_needs, "needs_item_current", needs_item_current)
    return needs


def test_a_card_whose_item_needs_you_still_lists_goes_through(
    phone: Phone, fleet: FleetCalls, own_predicates: FakeNeeds, project: ProjectInfo
) -> None:
    """``needs_item_current`` is true for an id among the snapshot's items. The row reads
    ``limited``: on one that does not, ``needs_dialog_open`` reads the item as the limit's
    dialog, and the switch is refused."""
    _row(project)
    own_predicates.state = "limited"
    own_predicates.items = (_item(project, "ny_limit"),)
    response = phone.post("agent/switch", **PINNED, needs_id="ny_limit")
    assert response.status_code == 200, response.text
    assert fleet.names() == ["switch"]


def test_a_current_permission_item_is_an_open_dialog_to_the_guard(
    phone: Phone,
    fleet: FleetCalls,
    own_predicates: FakeNeeds,
    pane: FakePane,
    project: ProjectInfo,
) -> None:
    """``needs_dialog_open`` is true while a ``permission`` item is current, whatever the
    row reads: the stop's Enter would approve the command."""
    _row(project)
    own_predicates.items = (_item(project, "ny_perm", kind="permission"),)
    response = phone.post("agent/stop", **PINNED)
    assert response.status_code == 409, response.text
    assert response.json()["error"] == "dialog_open"
    assert fleet.calls == [] and pane.sent == []


def _a_prompt_just_drawn() -> TranscriptTail:
    """A Bash permission prompt Claude Code drew a moment ago: a tool use with no result."""
    drawn = T0 + timedelta(minutes=5)
    push = PendingTool(
        tool_use_id="toolu_push",
        name="Bash",
        summary="Bash(git push --force)",
        input={"command": "git push --force"},
        at=drawn,
    )
    return TranscriptTail(
        pending=(push,),
        newest="assistant_tool",
        newest_at=drawn,
        last_text=None,
        last_text_at=None,
        marker_key="rec-push",
    )


@pytest.mark.parametrize("name", PINNED_ACTIONS)
def test_a_prompt_too_new_for_needs_you_to_see_still_refuses_the_action(
    phone: Phone,
    fleet: FleetCalls,
    own_predicates: FakeNeeds,
    pane: FakePane,
    project: ProjectInfo,
    name: str,
) -> None:
    """A prompt's first seconds: its pane printed within the last 5 s and the notification
    that makes the row ``attention`` comes at 6 s, so needs-you reads a tool at work, and
    the stop's ``/exit`` and Enter answered "1. Yes" to ``git push --force``."""
    _row(project)
    own_predicates.tail = _a_prompt_just_drawn()
    own_predicates.pane_quiet = False
    response = phone.post(name, **PINNED)
    assert response.status_code == 409, response.text
    assert response.json() == {
        "error": "dialog_open",
        "message": "coder-1 has a tool pending, and a prompt for it may have just opened; "
        f"{DOING[name]} could answer it — send dismiss_dialog: true to press Esc (No) "
        "first, which also stops a running tool",
    }
    assert fleet.calls == [] and pane.sent == []


@pytest.mark.parametrize("name", PINNED_ACTIONS)
def test_dismiss_dialog_answers_a_pending_tool_with_esc_then_acts(
    phone: Phone,
    fleet: FleetCalls,
    own_predicates: FakeNeeds,
    pane: FakePane,
    project: ProjectInfo,
    log: list[str],
    name: str,
) -> None:
    """The Escape answers the prompt "No", or stops the tool if it was one at work; the
    action goes on once the transcript holds the tool use's result."""
    _row(project)
    own_predicates.tail = _a_prompt_just_drawn()
    own_predicates.pane_quiet = False
    response = phone.post(name, **PINNED, dismiss_dialog=True)
    assert response.status_code == 200, response.text
    assert log == ["key Escape", f"fleet {name.removeprefix('agent/')}"]
    assert "dismissed=yes" in phone.audit()[0][1]


def test_a_tool_still_pending_after_the_escape_is_409_and_nothing_else_is_done(
    phone: Phone,
    fleet: FleetCalls,
    own_predicates: FakeNeeds,
    pane: FakePane,
    project: ProjectInfo,
) -> None:
    _row(project)
    own_predicates.tail = _a_prompt_just_drawn()
    own_predicates.pane_quiet = False
    own_predicates.escape_closes_dialog = False
    response = phone.post("agent/stop", **PINNED, dismiss_dialog=True)
    assert response.status_code == 409
    assert response.json() == {
        "error": "dialog_open",
        "message": "Escape was sent, but coder-1 still has its tool pending — "
        "nothing else was done",
    }
    assert pane.keys() == ["Escape"] and fleet.calls == []


def test_prompt_types_into_a_quiet_waiting_agent_that_has_no_transcript(
    phone: Phone, own_predicates: FakeNeeds, pane: FakePane, project: ProjectInfo
) -> None:
    """``needs_at_input_prompt``: with no readable tail, a quiet pane running the agent and
    a row that reads ``waiting`` is at its prompt."""
    _row(project)
    own_predicates.state = "waiting"
    response = phone.post("agent/tell", agent=LABEL, text="hi", mode="prompt")
    assert response.status_code == 200, response.text
    assert [kind for _pane, kind, _what in pane.sent] == ["paste", "key"]
