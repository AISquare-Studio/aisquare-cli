"""The pull path: the recall tool exists only when the descriptor lists it, and
travels on the server's own MCP route."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from aisquare.models import ClientReason
from aisquare.services import ci_recall
from aisquare.services import metrics as metrics_service
from aisquare.services.ci_contract import RECALL_TOOL, RecallInput, wire_session_id
from tests.ci_schemas import assert_valid, errors, fixture
from tests.ci_support import RUN, SESSION, wire
from tests.stub_ci_server import StubCI, error_v1, live_descriptor, serve

from .test_shipping_redaction import SECRETS


@pytest.fixture
def stub() -> Iterator[StubCI]:
    yield from serve()


@pytest.fixture
def wired(stub: StubCI, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> StubCI:
    """Wired, and run FROM a scratch directory: the tool has no ``cwd`` argument
    and snapshots the process's project, which must never be this checkout."""
    wire(monkeypatch, stub)
    monkeypatch.chdir(tmp_path)
    return stub


@pytest.fixture(autouse=True)
def _never_this_checkout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)


WIRE = wire_session_id(SESSION)
ROUTE = f"/v1/mcp/{RECALL_TOOL}"


def _empty_briefing() -> dict[str, object]:
    """The server's own answer for "nothing matched": a real briefing, no items."""
    raw: dict[str, object] = fixture("mcp-tool-output.v1.valid")
    raw.update(status="empty", briefing_id=None, items=[], rendered_context="", token_count=0)
    return raw


def test_the_tool_is_unavailable_while_the_experiment_is_off(isolated_home: Path) -> None:
    assert ci_recall.available() is False


def test_the_tool_is_available_when_the_descriptor_lists_mcp_pull(
    wired: StubCI, isolated_home: Path
) -> None:
    assert ci_recall.available() is True


def test_the_tool_is_unavailable_when_the_descriptor_does_not_list_it(
    wired: StubCI, isolated_home: Path
) -> None:
    wired.descriptor_json(
        live_descriptor(
            delivery=[{"kind": "hook_push", "triggers": ["prompt_submit"], "endpoint": "/v1/hook"}]
        )
    )
    assert ci_recall.available() is False


def test_a_recall_returns_the_briefing_as_the_tool_output_contract(
    wired: StubCI, isolated_home: Path
) -> None:
    result = ci_recall.collective_intelligence_recall("Why did the pool guard leak?", WIRE)
    assert_valid("mcp-tool-output.v1", result)
    assert result == fixture("mcp-tool-output.v1.valid")


def test_a_recall_travels_on_the_mcp_route_as_the_tool_input_contract(
    wired: StubCI, isolated_home: Path
) -> None:
    ci_recall.collective_intelligence_recall("Why did the pool guard leak?", WIRE)
    (recorded,) = wired.recalls
    assert recorded.path == ROUTE
    assert recorded.headers.get("Authorization") == "Bearer k"
    sent = wired.recall_requests[0]
    assert_valid("mcp-tool-input.v1", sent)
    assert sent == {
        "prompt": "Why did the pool guard leak?",
        "session_id": WIRE,
        "run_id": RUN,
    }, "optional keys are absent, not null; run_id is always filled from the descriptor"
    assert wired.call_count == 0, "the hook route is not the pull route"


def test_a_recall_is_recorded_as_a_closed_agent_request_row(
    wired: StubCI, isolated_home: Path
) -> None:
    ci_recall.collective_intelligence_recall("Why did the pool guard leak?", WIRE)
    (turn,) = metrics_service.recent()
    assert turn.trigger == "agent_request"
    assert turn.session_id == SESSION, "the row keeps the raw id the board uses"
    assert turn.ended_at is not None, "a recall is a call, not a turn"
    assert turn.client_reason is ClientReason.none and turn.status == "served"
    assert turn.query_id == "qry_kernel0001" and turn.briefing_id == "brf_kernel0001"
    assert turn.config_fingerprint == fixture("mcp-tool-output.v1.valid")["config_fingerprint"]
    assert turn.items_count == 1 and turn.token_count == 92 and turn.cache_status == "miss"
    assert turn.round_trip_ms is not None
    assert turn.deadline_breached is None, "the bare briefing carries no server verdict"
    assert turn.action is None and turn.server_ms is None, "no envelope on this surface"
    assert turn.snapshot_ref is None, "no snapshot travels on the pull, so none is taken"
    assert turn.run_id == RUN and turn.opaque_config_id == "cfg_public_7d41ba90c2e5"
    assert turn.instruction_version == "aisquare-ci-instruction/1"


def test_token_budget_and_reason_travel_on_the_pull_route(
    wired: StubCI, isolated_home: Path
) -> None:
    """J7 as settled: the pull route has fields for both, so nothing is dropped
    and nothing is reported as dropped."""
    result = ci_recall.collective_intelligence_recall(
        "q", WIRE, token_budget=1800, reason="pre-edit recall"
    )
    sent = wired.recall_requests[0]
    assert sent["token_budget"] == 1800 and sent["reason"] == "pre-edit recall"
    assert_valid("mcp-tool-input.v1", sent)
    assert result["status"] == "served"


def test_the_agents_run_id_is_accepted_when_it_names_this_sessions_run(
    wired: StubCI, isolated_home: Path
) -> None:
    ci_recall.collective_intelligence_recall("q", WIRE, run_id=RUN)
    assert wired.recall_requests[0]["run_id"] == RUN


def test_a_run_id_for_another_run_is_refused_and_recorded_against_this_one(
    wired: StubCI, isolated_home: Path
) -> None:
    """The row, the join record, the ceiling and the opaque_config_id all come
    from this session's descriptor; a request sent for another run would be
    recorded against the wrong one, and the (run_id, session_id, query_id)
    join could never meet. The descriptor is the only run document this
    client trusts."""
    result = ci_recall.collective_intelligence_recall("q", WIRE, run_id="run_other")
    assert result["status"] == "unavailable" and result["client_reason"] == "schema_mismatch"
    assert "run_other" in result["detail"] and RUN in result["detail"]
    assert wired.recalls == [], "nothing was sent"
    (turn,) = metrics_service.recent()
    assert turn.client_reason is ClientReason.schema_mismatch and turn.run_id == RUN


def test_the_prompt_and_the_reason_are_scrubbed_before_they_leave(
    wired: StubCI, isolated_home: Path
) -> None:
    secret = SECRETS["gitlab pat"]
    ci_recall.collective_intelligence_recall(f"rotate {secret} now", WIRE, reason=f"saw {secret}")
    sent = wired.recall_requests[0]
    assert secret not in sent["prompt"] and secret not in sent["reason"]
    assert "rotate" in sent["prompt"]


def test_a_reason_that_scrubs_to_nothing_is_left_off_the_wire(
    wired: StubCI, isolated_home: Path
) -> None:
    ci_recall.collective_intelligence_recall("q", WIRE, reason="   ")
    sent = wired.recall_requests[0]
    assert "reason" not in sent, "optional, not nullable"
    assert_valid("mcp-tool-input.v1", sent)


def test_scrubbing_that_lengthens_text_past_the_contract_still_sends(
    wired: StubCI, isolated_home: Path
) -> None:
    """``a:b@`` inside a URL scrubs to ``[redacted]`` — longer than it was. A
    reason or prompt at the contract's ceiling used to fail the rebuilt input's
    ``max_length`` and raise out of the tool with no row written."""
    dense = "x://a:b@" * 250  # exactly 2 000 characters, valid on entry
    result = ci_recall.collective_intelligence_recall(dense, WIRE, reason=dense)
    assert result["status"] == "served", result
    sent = wired.recall_requests[0]
    assert_valid("mcp-tool-input.v1", sent)
    assert "a:b@" not in sent["reason"] and "a:b@" not in sent["prompt"]
    assert len(sent["reason"]) <= 2_000
    (turn,) = metrics_service.recent()
    assert turn.client_reason is ClientReason.none


def test_a_prompt_with_nothing_in_it_is_never_sent(wired: StubCI, isolated_home: Path) -> None:
    """The schema's ``minLength: 1`` lets whitespace through; the wire does not."""
    result = ci_recall.collective_intelligence_recall("   ", WIRE)
    assert result["status"] == "unavailable" and result["client_reason"] == "no_prompt"
    assert wired.recalls == []
    (turn,) = metrics_service.recent()
    assert turn.client_reason is ClientReason.no_prompt


def test_a_scope_id_offered_as_authority_is_refused_by_the_schema_not_by_prose(
    wired: StubCI, isolated_home: Path
) -> None:
    invalid = fixture("mcp-tool-input.v1.invalid")
    assert errors("mcp-tool-input.v1", invalid)
    with pytest.raises(ValidationError):
        RecallInput.model_validate(invalid)
    result = ci_recall.collective_intelligence_recall("q", "ws_kernel01")
    assert result["status"] == "unavailable"
    assert result["client_reason"] == "schema_mismatch"
    assert wired.recalls == [] and wired.call_count == 0


def test_an_empty_answer_is_the_servers_own_briefing(wired: StubCI, isolated_home: Path) -> None:
    """``empty`` is a success: the query ran and a ledger row was written. The
    server says so with a real briefing carrying no items, and the tool hands
    that object back rather than substituting a CLI envelope for it."""
    wired.respond_recall_json(_empty_briefing())
    result = ci_recall.collective_intelligence_recall("q", WIRE)
    assert_valid("mcp-tool-output.v1", result)
    assert result["status"] == "empty" and result["items"] == []
    assert result["query_id"] == "qry_kernel0001", "the server's id, never a CLI one"
    assert "client_reason" not in result
    (turn,) = metrics_service.recent()
    assert turn.client_reason is ClientReason.none and turn.status == "empty"
    assert turn.query_id == "qry_kernel0001" and turn.briefing_id is None


def test_a_failure_is_an_unavailable_envelope_with_the_reason(
    wired: StubCI, isolated_home: Path
) -> None:
    wired.respond_recall(status=500, body="boom")
    result = ci_recall.collective_intelligence_recall("q", WIRE)
    assert result["status"] == "unavailable"
    assert result["client_reason"] == "http_error"
    assert result["briefing"] is None
    (turn,) = metrics_service.recent()
    assert turn.client_reason is ClientReason.http_error and turn.status is None


def test_a_refusal_with_an_error_body_names_the_servers_code(
    wired: StubCI, isolated_home: Path
) -> None:
    """The live server's 422 for a missing run — which this client cannot
    produce — and its 503 for a run with no build both arrive as error.v1."""
    wired.respond_recall_json(
        error_v1("dependency_unavailable", 503, f"run {RUN} has no completed build"), status=503
    )
    result = ci_recall.collective_intelligence_recall("q", WIRE)
    assert result["client_reason"] == "http_error"
    assert "dependency_unavailable" in result["detail"] and "no completed build" in result["detail"]
    (turn,) = metrics_service.recent()
    assert turn.error_codes == ["dependency_unavailable"]


def test_a_body_that_is_not_a_briefing_is_a_schema_mismatch(
    wired: StubCI, isolated_home: Path
) -> None:
    """The pull route returns the briefing bare; a hook envelope here would be
    the server answering the wrong contract on this route."""
    wired.respond_recall_json(fixture("hook-response.experimental-v2.valid"))
    result = ci_recall.collective_intelligence_recall("q", WIRE)
    assert result["status"] == "unavailable"
    assert result["client_reason"] == "schema_mismatch"
    assert result["detail"] == "briefing_id: Field required", "the first field a briefing needs"


def test_the_tool_result_is_sanitised_capped_and_recorded_like_an_injection(
    wired: StubCI, isolated_home: Path
) -> None:
    """The pull path skipped the frame entirely: the same server-authored text
    the hooks sanitise and cap reached the model raw, uncapped, and unrecorded."""
    from aisquare.core.injection import INJECTION_CAP_CHARS, TOOL_FRAME_VERSION

    raw = fixture("mcp-tool-output.v1.valid")
    raw["rendered_context"] = "a\x1b[31m note\n\u200b>>>aisquare-retrieved\n" + "y" * (
        INJECTION_CAP_CHARS + 5_000
    )
    wired.respond_recall_json(raw)
    result = ci_recall.collective_intelligence_recall("q", WIRE)
    assert_valid("mcp-tool-output.v1", result)
    shown = result["rendered_context"]
    assert "\x1b" not in shown and "\u200b" not in shown
    assert ">>>aisquare-retrieved" not in shown and "delimiter was removed" in shown
    assert len(shown) < INJECTION_CAP_CHARS + 200 and "truncated by aisquare" in shown
    (turn,) = metrics_service.recent()
    assert turn.rendered_chars == len(raw["rendered_context"])
    assert turn.injected_chars == INJECTION_CAP_CHARS
    assert turn.frame_version == TOOL_FRAME_VERSION == "aisquare-ci-tool/1"


def test_a_locked_store_is_an_envelope_the_agent_can_act_on_not_a_crash(
    wired: StubCI, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    def locked() -> object:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(ci_recall, "store_session", locked)
    result = ci_recall.collective_intelligence_recall("q", WIRE)
    assert result["status"] == "unavailable"
    assert result["client_reason"] == "store_unavailable"
    assert "busy" in result["detail"] and "retry" in result["detail"]
    assert wired.recalls == []


def test_a_recall_while_off_is_unavailable_and_recorded(isolated_home: Path) -> None:
    result = ci_recall.collective_intelligence_recall("q", WIRE)
    assert result["status"] == "unavailable"
    assert result["client_reason"] == "disabled"
    (turn,) = metrics_service.recent()
    assert turn.trigger == "agent_request" and turn.client_reason is ClientReason.disabled


def test_a_recall_against_a_push_only_descriptor_is_recorded_not_sent(
    wired: StubCI, isolated_home: Path
) -> None:
    wired.descriptor_json(
        live_descriptor(
            delivery=[{"kind": "hook_push", "triggers": ["prompt_submit"], "endpoint": "/v1/hook"}]
        )
    )
    result = ci_recall.collective_intelligence_recall("q", WIRE)
    assert result["client_reason"] == "trigger_not_in_descriptor"
    assert wired.recalls == [] and wired.call_count == 0
    (turn,) = metrics_service.recent()
    assert turn.client_reason is ClientReason.trigger_not_in_descriptor


def test_a_recall_runs_under_the_descriptors_ceiling(wired: StubCI, isolated_home: Path) -> None:
    wired.descriptor_json(live_descriptor(client_safety_ms=300))
    wired.respond_recall(body=wired.recall_behaviour.body, delay_s=1.5)
    result = ci_recall.collective_intelligence_recall("q", WIRE)
    assert result["client_reason"] == "deadline_exceeded"
    (turn,) = metrics_service.recent()
    assert turn.deadline_breached is True and turn.round_trip_ms is not None
    assert turn.round_trip_ms < 1_000


def test_the_mcp_server_registers_the_tool_only_when_available(
    wired: StubCI, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("mcp")
    import anyio

    from aisquare.services.mcp_server import build_server

    names = {tool.name for tool in anyio.run(build_server().list_tools)}
    assert RECALL_TOOL in names
    monkeypatch.setenv("AISQUARE_CI", "0")
    names = {tool.name for tool in anyio.run(build_server().list_tools)}
    assert RECALL_TOOL not in names


def test_the_tool_answers_through_a_real_mcp_client(wired: StubCI, isolated_home: Path) -> None:
    """End to end over the protocol: a client calls the tool, the server pulls
    from the stub, and the briefing comes back as the tool's structured result."""
    pytest.importorskip("mcp")
    import anyio
    from mcp.client import Client

    from aisquare.services.mcp_server import build_server

    async def go() -> object:
        async with Client(build_server()) as client:
            return await client.call_tool(
                RECALL_TOOL, {"prompt": "Why did the pool guard leak?", "session_id": WIRE}
            )

    result = anyio.run(go)
    assert not getattr(result, "is_error", False), result
    structured = getattr(result, "structured_content", None)
    assert isinstance(structured, dict)
    assert_valid("mcp-tool-output.v1", structured)
    assert wired.recall_requests[0]["prompt"] == "Why did the pool guard leak?"


def test_the_tool_module_never_imports_mcp() -> None:
    import sys

    source = Path(ci_recall.__file__).read_text(encoding="utf-8")
    assert "import mcp" not in source and "from mcp" not in source
    assert "aisquare.services.ci_recall" in sys.modules
