"""Agent actions from the phone (SPEC §3): tell, stop, restart, switch, and the request ledger.

The fleet service is replaced by recorders (``fleet_service.tell/stop/restart/switch``),
tmux by a pane that writes down what it was sent, and needs-you's view of the agent
(``remote_needs.needs_agent_now`` and its predicates) by a fake whose dialog closes on
an Escape, as Claude Code's does. The project and its rows are real, in the isolated
store, so a pin is checked against what ``fleet ls --all`` would show.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from starlette.requests import Request
from starlette.responses import Response

from aisquare.services import remote_needs
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
