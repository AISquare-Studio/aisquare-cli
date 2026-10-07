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
    ACTION_ENDPOINTS,
    ACTION_LEDGER_SIZE,
    ACTION_LEDGER_TTL,
    ActionLedger,
    action_handlers,
    fleet_refusal,
    new_action_ledger,
)
from aisquare.services.remote_needs import AgentNow, NeedsItem
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


# --- the actions: a real project and its rows; the fleet, tmux and needs-you faked ---------

LABEL = "coder-1"
PINNED: dict[str, object] = {"agent": LABEL, "agent_id": "agt_one", "confirm": LABEL}
PINNED_ACTIONS = ("agent/stop", "agent/restart", "agent/switch")
DOING = {"agent/stop": "stopping", "agent/restart": "restarting", "agent/switch": "switching"}


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
    ``lag``-th read after the Escape, as a pane shows it a moment later."""

    def __init__(self, pane: FakePane) -> None:
        self.pane = pane
        self.state: FleetAgentState = "working"
        self.dialog = False
        self.at_prompt = False
        self.pane_is_agent = True
        self.window_gone = False
        self.escape_closes_dialog = True
        self.escape_stops_agent = True
        self.lag = 1
        self.items: tuple[NeedsItem, ...] = ()
        self.before_read: Callable[[], None] | None = None
        self.reads = 0
        self._escapes = 0
        self._since_escape: int | None = None
        self._views: list[tuple[AgentNow, bool, bool]] = []

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
            tail=None,
            pane_is_agent=self.pane_is_agent and status is not None,
            pane_quiet=True,
            items=self.items,
        )
        self._views.append((snap, self.dialog, self.at_prompt))
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
        if self.escape_closes_dialog:
            self.dialog = False
        if self.escape_stops_agent and not self.dialog:
            self.at_prompt = True

    def _view(self, snap: AgentNow) -> tuple[bool, bool]:
        for seen, dialog, prompt in reversed(self._views):
            if seen is snap:
                return dialog, prompt
        raise AssertionError("a snapshot needs_agent_now never gave")

    def needs_dialog_open(self, snap: AgentNow) -> bool:
        return snap.pane_is_agent and self._view(snap)[0]

    def needs_at_input_prompt(self, snap: AgentNow) -> bool:
        dialog, prompt = self._view(snap)
        return snap.pane_is_agent and prompt and not dialog

    def needs_item_current(self, snap: AgentNow, item_id: str) -> bool:
        return item_id in {item.id for item in snap.items}


@pytest.fixture
def needs(monkeypatch: pytest.MonkeyPatch, pane: FakePane) -> FakeNeeds:
    fake = FakeNeeds(pane)
    monkeypatch.setattr(remote_needs, "needs_agent_now", fake.needs_agent_now)
    monkeypatch.setattr(remote_needs, "needs_dialog_open", fake.needs_dialog_open)
    monkeypatch.setattr(remote_needs, "needs_at_input_prompt", fake.needs_at_input_prompt)
    monkeypatch.setattr(remote_needs, "needs_item_current", fake.needs_item_current)
    monkeypatch.setattr(remote_needs, "DIALOG_SETTLE_SECONDS", 0.05)
    monkeypatch.setattr(remote_actions, "ACTION_POLL_SECONDS", 0.001)
    return fake


def _item(project: ProjectInfo, item_id: str, kind: str = "limited") -> NeedsItem:
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
        """``(endpoint, summary)`` of every audit line, in order."""
        path = remote_audit_path()
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        return [(line.split(" ", 3)[2], line.split(" ", 3)[3]) for line in lines]


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
    _row(project)
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
def test_a_fleet_refusal_answers_as_the_cli_maps_it(
    phone: Phone,
    fleet: FleetCalls,
    needs: FakeNeeds,
    project: ProjectInfo,
    name: str,
    error: Exception,
    status: int,
    code: str,
) -> None:
    _row(project)
    fleet.answers[name.removeprefix("agent/")] = error
    response = phone.post(name, **PINNED)
    assert response.status_code == status
    assert response.json() == {"error": code, "message": str(error)}
    assert phone.audit() == [], "a refusal is not a write that went through"


def test_fleet_refusal_maps_the_servers_own_lookups_too() -> None:
    """needs-you may say an agent or project is unknown in the server's own words."""
    agent = fleet_refusal(remote_server.NoSuchAgent("no agent 'coder-1'"))
    project = fleet_refusal(remote_server.NoSuchProject("no project 'web'"))
    interrupted = fleet_refusal(fleet_service.FleetError("anything else the fleet refused"))
    assert (agent.status, agent.error) == (404, "no_such_agent")
    assert (project.status, project.error) == (404, "not_found")
    assert (interrupted.status, interrupted.error) == (409, "fleet_error")


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
