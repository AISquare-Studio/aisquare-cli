"""A write's body is read strictly: each field of its own type, or a 400 before anything runs.

``bool()`` of a JSON string is true for ``"false"``: ``"enter": "false"`` pressed Enter
after the keys it came with, which takes a dialog's highlighted option ("1. Yes"), on
``send-keys`` and on a card's quick answer alike. And a field of the wrong type read
as absent did the rest: ``"text": 3`` with ``"enter": true`` sent the Enter alone, and
``"project": 2048`` typed into the CURRENT project's agent (review of #243, round 3).
The agent actions already refused both; their readers and the server's are one set now.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aisquare.services import fleet as fleet_service
from aisquare.services import remote_actions, remote_server
from aisquare.services.remote_needs import _needs_answer_body
from aisquare.services.remote_server import (
    RequestError,
    Runtime,
    Sources,
    build_app,
    live_writes,
)
from tests.remote_kit_helpers import base, frame_within, make_client, make_runtime, unlock

NOT_BOOLEANS: list[object] = ["false", "true", "0", "no", 0, 1, [], {}]
"""What a script may send for a flag that ``bool()`` read as it pleased."""


def _sources(**kw: Any) -> Sources:
    return Sources(
        projects=lambda: [],
        fleet=lambda project: {},
        board=lambda project: {},
        tasks=lambda project: [],
        memory=lambda project: [],
        panes=kw.get("panes", lambda agent, project, history: {"rows": [], "width": 0}),
        explainability=lambda agent, project: {"available": False},
    )


@pytest.fixture
def runtime(isolated_home: Path) -> Runtime:
    return make_runtime()


class FakePane:
    """The tmux server ``send-keys`` reaches: what arrived, on a pane that runs the agent."""

    STARTED = datetime(2026, 10, 7, 8, 0, tzinfo=UTC)

    def __init__(self) -> None:
        self.sent: list[tuple[str, ...]] = []
        self.projects: list[str | None] = []

    def pane_facts(self, pane_id: str) -> SimpleNamespace:
        return SimpleNamespace(dead=False, current_command="claude")

    def started_at(self) -> datetime:
        return self.STARTED

    def send_literal(self, pane_id: str, text: str) -> None:
        self.sent.append(("literal", text))

    def send_keys(self, pane_id: str, *keys: str) -> None:
        self.sent.append(("keys", *keys))


@pytest.fixture
def pane(monkeypatch: pytest.MonkeyPatch) -> FakePane:
    """One live agent ``coder-1`` in whatever project a body names, its pane a FakePane."""
    import aisquare.core.store as store_module

    fake = FakePane()

    class Agent:
        pane_id = "%1"
        tmux_socket = "asq"
        created_at = FakePane.STARTED + timedelta(hours=1)

    class Store:
        def fleet_agent_by_label(self, *args: object, **kwargs: object) -> Agent:
            return Agent()

        def __enter__(self) -> Store:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    def resolve(ref: str | None) -> SimpleNamespace:
        fake.projects.append(ref)
        return SimpleNamespace(id=f"prj_{ref or 'current'}", root=Path("/tmp/p"))

    monkeypatch.setattr(remote_server, "_resolve_project", resolve)
    monkeypatch.setattr(store_module, "store_session", lambda: Store())
    monkeypatch.setattr(fleet_service, "server_for", lambda socket, config=None: fake)
    return fake


def _send_keys(body: dict[str, Any]) -> tuple[dict[str, object], str]:
    return live_writes().handlers["send-keys"](body)


# --- flags: a JSON boolean or nothing -----------------------------------------------------


@pytest.mark.parametrize("value", NOT_BOOLEANS, ids=repr)
def test_enter_that_is_not_a_json_boolean_is_refused_and_presses_nothing(
    pane: FakePane, value: object
) -> None:
    """The finding's own body: ``Down`` to move a permission dialog's selection, then an
    Enter nobody asked for, which approved the option it had just moved to."""
    with pytest.raises(RequestError) as refused:
        _send_keys({"agent": "coder-1", "keys": ["Down"], "enter": value})
    assert (refused.value.status, refused.value.error) == (400, "invalid")
    assert refused.value.message == "'enter' must be true or false"
    assert pane.sent == []


@pytest.mark.parametrize("value", NOT_BOOLEANS, ids=repr)
def test_confirm_exit_that_is_not_a_json_boolean_is_refused_before_the_guard(
    pane: FakePane, value: object
) -> None:
    with pytest.raises(RequestError) as refused:
        _send_keys({"agent": "coder-1", "keys": ["C-c"], "confirm_exit": value})
    assert (refused.value.status, refused.value.error) == (400, "invalid")
    assert pane.sent == []


@pytest.mark.parametrize("value", NOT_BOOLEANS, ids=repr)
def test_a_dialog_guard_that_is_not_a_json_boolean_is_refused_and_types_nothing(
    pane: FakePane, value: object
) -> None:
    """``dialog_guard`` keeps a sender that cannot see the pane from answering a prompt with
    its text and Enter. Read by ``bool()``, ``0`` would have typed them all the same."""
    with pytest.raises(RequestError) as refused:
        _send_keys({"agent": "coder-1", "text": "1", "enter": True, "dialog_guard": value})
    assert (refused.value.status, refused.value.error) == (400, "invalid")
    assert refused.value.message == "'dialog_guard' must be true or false"
    assert pane.sent == []


def test_a_json_boolean_or_none_is_read_as_it_says(pane: FakePane) -> None:
    _send_keys({"agent": "coder-1", "keys": ["Down"], "enter": False})
    _send_keys({"agent": "coder-1", "keys": ["Up"], "enter": None})
    _send_keys({"agent": "coder-1", "keys": ["1"], "enter": True})
    assert pane.sent == [("keys", "Down"), ("keys", "Up"), ("keys", "1"), ("keys", "Enter")]


@pytest.mark.parametrize("value", NOT_BOOLEANS, ids=repr)
def test_a_quick_answer_with_enter_that_is_not_a_json_boolean_is_refused(value: object) -> None:
    with pytest.raises(RequestError) as refused:
        _needs_answer_body({"id": "ny_1", "keys": ["2"], "enter": value})
    assert (refused.value.status, refused.value.error) == (400, "invalid")
    assert _needs_answer_body({"id": "ny_1", "keys": ["2"]})[3] is False
    assert _needs_answer_body({"id": "ny_1", "keys": ["2"], "enter": True})[3] is True


def test_the_actions_read_their_fields_with_the_servers_own_readers() -> None:
    """Two copies drifted: the actions refused a value of the wrong type, and the server's
    own copy read the same value as absent."""
    assert remote_actions.action_flag is remote_server._remote_flag
    assert remote_actions.action_ref is remote_server._optional_ref
    assert remote_actions.action_required is remote_server._required


# --- strings: a string, or nothing --------------------------------------------------------


def test_text_that_is_not_a_string_is_refused_and_its_enter_is_not_sent(pane: FakePane) -> None:
    with pytest.raises(RequestError) as refused:
        _send_keys({"agent": "coder-1", "text": 3, "enter": True})
    assert (refused.value.status, refused.value.error) == (400, "invalid")
    assert refused.value.message == "'text' must be a string"
    assert pane.sent == [], "the Enter alone took the dialog's highlighted option"


def test_a_project_that_is_not_a_string_is_refused_not_read_as_the_current_one(
    pane: FakePane,
) -> None:
    with pytest.raises(RequestError) as refused:
        _send_keys({"agent": "coder-1", "project": 2048, "keys": ["1"]})
    assert (refused.value.status, refused.value.error) == (400, "invalid")
    assert pane.projects == [] and pane.sent == []
    _send_keys({"agent": "coder-1", "project": "2048", "keys": ["1"]})
    assert pane.projects == ["2048"] and pane.sent == [("keys", "1")]


class FakeTeam:
    """``team_service``'s writes as the handlers call them, recorded."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def add_note(self, text: str, **kwargs: Any) -> Any:
        self.calls.append(("note", {"text": text, **kwargs}))
        envelope = SimpleNamespace(model_dump=lambda mode: {"seq": 7})
        return SimpleNamespace(kind=kwargs["kind"], seq=7, as_envelope=lambda: envelope)

    def claim_task(self, ref: str, *, session_ref: str | None) -> Any:
        self.calls.append(("claim", {"ref": ref, "session_ref": session_ref}))
        return SimpleNamespace(id=ref, model_dump=lambda mode: {"id": ref})

    def finish_task(self, ref: str, *, note: str | None, session_ref: str | None) -> Any:
        self.calls.append(("done", {"ref": ref, "note": note, "session_ref": session_ref}))
        return SimpleNamespace(id=ref, model_dump=lambda mode: {"id": ref})


@pytest.fixture
def team(monkeypatch: pytest.MonkeyPatch) -> FakeTeam:
    from aisquare.services import team as team_service

    fake = FakeTeam()
    for name in ("add_note", "claim_task", "finish_task"):
        monkeypatch.setattr(team_service, name, getattr(fake, name))
    return fake


@pytest.mark.parametrize(
    ("name", "body"),
    [
        ("note", {"text": "x", "project": 2048}),
        ("note", {"text": "x", "kind": 7}),
        ("note", {"text": "x", "to": ["manager"]}),
        ("note", {"text": "x", "as": 1}),
        ("note", {"text": "x", "task": {"id": "tsk_1"}}),
        ("task/claim", {"ref": "tsk_1", "as": True}),
        ("task/done", {"ref": "tsk_1", "note": 5}),
        ("task/done", {"ref": "tsk_1", "as": 5}),
    ],
    ids=repr,
)
def test_a_note_or_task_field_that_is_not_a_string_is_refused_before_the_board(
    team: FakeTeam, name: str, body: dict[str, Any]
) -> None:
    """Read as absent, ``"project": 2048`` posted on the current board, and ``"kind": 7``
    a plain note, where any other wrong kind is a 400."""
    with pytest.raises(RequestError) as refused:
        live_writes().handlers[name](body)
    assert (refused.value.status, refused.value.error) == (400, "invalid")
    assert "must be a string" in refused.value.message
    assert team.calls == []


def test_blank_refs_still_mean_none(team: FakeTeam) -> None:
    live_writes().handlers["note"]({"text": "x", "to": "  ", "as": "", "task": None})
    _name, call = team.calls[-1]
    assert (call["to_role"], call["session_ref"], call["task_ref"]) == (None, None, None)


def test_the_body_fields_go_over_http_as_400s(
    runtime: Runtime, pane: FakePane, tmp_path: Path
) -> None:
    app = build_app(runtime, sources=_sources(), writes=live_writes(), dist_dir=tmp_path)
    client = make_client(app)
    assert unlock(client, runtime).status_code == 200
    runtime.set_allow_write(True)
    url = f"{base(runtime)}/api/send-keys"
    for body in (
        {"agent": "coder-1", "text": 3, "enter": True},
        {"agent": "coder-1", "project": 2048, "keys": ["1"]},
        {"agent": "coder-1", "keys": ["Down"], "enter": "false"},
    ):
        response = client.post(url, json=body)
        assert response.status_code == 400, body
        assert response.json()["error"] == "invalid"
    assert pane.sent == []


# --- the socket: a project that is no name names none ---------------------------------------


def test_a_subscription_whose_project_is_not_a_string_is_ignored(
    runtime: Runtime, tmp_path: Path
) -> None:
    """Read as ``""``, a ``"project": 7`` watched the CURRENT project's ``coder-1``."""
    asked: list[tuple[str, str | None]] = []

    def panes(agent: str, project: str | None, history: int) -> dict[str, object]:
        asked.append((agent, project))
        return {"rows": [agent], "width": 1}

    app = build_app(runtime, sources=_sources(panes=panes), dist_dir=tmp_path, tick=0.02)
    client = make_client(app)
    assert unlock(client, runtime).status_code == 200
    with client.websocket_connect(f"{base(runtime)}/ws") as ws:
        ws.send_text(json.dumps({"subscribe": "coder-1", "project": 7}))
        ws.send_text(json.dumps({"subscribe": "coder-2", "project": "prj_b"}))
        frame = frame_within(ws)
        while frame["type"] != "pane":
            frame = frame_within(ws)
    assert (frame["agent"], frame["project"]) == ("coder-2", "prj_b")
    assert ("coder-1", None) not in asked and asked[0] == ("coder-2", "prj_b")
