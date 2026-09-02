"""The pull path: the recall tool exists only when the descriptor lists it."""

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
from tests.ci_support import SESSION, wire
from tests.stub_ci_server import StubCI, live_descriptor, serve


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


def test_a_recall_travels_as_an_agent_request_and_is_recorded(
    wired: StubCI, isolated_home: Path
) -> None:
    ci_recall.collective_intelligence_recall("Why did the pool guard leak?", WIRE)
    sent = wired.requests[0]
    assert_valid("hook-request.experimental-v2", sent)
    assert sent["trigger"] == "agent_request"
    assert sent["session_id"] == WIRE
    assert sent["prompt"] == "Why did the pool guard leak?"
    (turn,) = metrics_service.recent()
    assert turn.trigger == "agent_request"
    assert turn.session_id == SESSION, "the row keeps the raw id the board uses"
    assert turn.ended_at is not None, "a recall is a call, not a turn"
    assert turn.trace_id == sent["trace_id"]
    assert turn.client_reason is ClientReason.none and turn.status == "served"


def test_arguments_the_hook_cannot_carry_are_reported_not_dropped_silently(
    wired: StubCI, isolated_home: Path
) -> None:
    result = ci_recall.collective_intelligence_recall(
        "q", WIRE, token_budget=1800, reason="pre-edit recall"
    )
    assert result["not_forwarded"] == ["token_budget", "reason"]
    assert "token_budget" not in wired.requests[0]


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
    assert wired.call_count == 0


def test_an_empty_answer_is_a_small_envelope(wired: StubCI, isolated_home: Path) -> None:
    raw = fixture("hook-response.experimental-v2.valid")
    raw.update(status="empty", action="noop", briefing=None)
    wired.respond_json(raw)
    result = ci_recall.collective_intelligence_recall("q", WIRE)
    assert result["status"] == "empty"
    assert result["client_reason"] == "none"
    assert result["briefing"] is None


def test_a_failure_is_an_unavailable_envelope_with_the_reason(
    wired: StubCI, isolated_home: Path
) -> None:
    wired.respond(status=500, body="boom")
    result = ci_recall.collective_intelligence_recall("q", WIRE)
    assert result["status"] == "unavailable"
    assert result["client_reason"] == "http_error"
    (turn,) = metrics_service.recent()
    assert turn.client_reason is ClientReason.http_error


def test_a_recall_while_off_is_unavailable_and_recorded(isolated_home: Path) -> None:
    result = ci_recall.collective_intelligence_recall("q", WIRE)
    assert result["status"] == "unavailable"
    assert result["client_reason"] == "disabled"
    (turn,) = metrics_service.recent()
    assert turn.trigger == "agent_request" and turn.client_reason is ClientReason.disabled


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


def test_the_tool_module_never_imports_mcp() -> None:
    import sys

    source = Path(ci_recall.__file__).read_text(encoding="utf-8")
    assert "import mcp" not in source and "from mcp" not in source
    assert "aisquare.services.ci_recall" in sys.modules
