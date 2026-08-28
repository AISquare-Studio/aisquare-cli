"""The CI wire contract: fixture round-trips and the degradation ladder.

The fixtures under ``tests/fixtures/ci_contract`` are the artifact the server
team codes against, so these tests are the thing that keeps this build and that
server describing the same protocol. A change that makes a fixture stop parsing
is a contract break, and it should read as one here rather than as a surprise
at integration time.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aisquare.core.ids import TRACE_PREFIX, new_trace_id
from aisquare.services.ci_contract import (
    ALLOW,
    CLIENT_BACKSTOP_SECONDS,
    CONTRACT_VERSION,
    Action,
    DegradationReason,
    HookRequest,
    HookResponse,
    Trigger,
    degraded,
    parse_response,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "ci_contract"


def _read(*parts: str) -> str:
    return (_FIXTURES.joinpath(*parts)).read_text(encoding="utf-8")


# --- requests -----------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["session_start", "prompt_submit", "tool_intercept", "agent_request"]
)
def test_every_trigger_fixture_round_trips(name: str) -> None:
    raw = json.loads(_read("requests", f"{name}.json"))
    request = HookRequest.model_validate(raw)
    assert request.trigger is Trigger(name)
    assert request.to_wire() == raw


def test_every_trigger_has_a_fixture() -> None:
    """A new trigger without a fixture is a contract change nobody documented."""
    on_disk = {path.stem for path in (_FIXTURES / "requests").glob("*.json")}
    assert on_disk == {trigger.value for trigger in Trigger}


def test_the_wire_body_keeps_nulls() -> None:
    """§02 shows explicit nulls; dropping them changes the shape servers parse."""
    request = HookRequest(
        trigger=Trigger.session_start,
        session_id="ses_x",
        trace_id=new_trace_id(),
        project_id="prj_x",
    )
    body = request.to_wire()
    assert body["run_id"] is None
    assert body["arm"] is None
    assert body["prompt"] is None
    assert body["tool"] is None


def test_trigger_serializes_as_its_string_not_an_enum_repr() -> None:
    request = HookRequest(
        trigger=Trigger.prompt_submit,
        session_id="ses_x",
        trace_id=new_trace_id(),
        project_id="prj_x",
        prompt="hello",
    )
    assert json.loads(json.dumps(request.to_wire()))["trigger"] == "prompt_submit"


# --- responses ----------------------------------------------------------------


@pytest.mark.parametrize("name", ["inject", "substitute", "allow", "noop"])
def test_every_action_fixture_parses_undegraded(name: str) -> None:
    outcome = parse_response(status=200, body=_read("responses", f"{name}.json"))
    assert outcome.reason is DegradationReason.none
    assert not outcome.degraded
    assert outcome.response.action is Action(name)


def test_every_action_has_a_fixture() -> None:
    on_disk = {path.stem for path in (_FIXTURES / "responses").glob("*.json")}
    assert on_disk == {action.value for action in Action}


def test_inject_carries_context_and_provenance() -> None:
    outcome = parse_response(status=200, body=_read("responses", "inject.json"))
    response = outcome.response
    assert response.context
    assert [p.source for p in response.provenance] == ["src/aisquare/core/brain.py"]
    assert response.cache_hint is not None
    assert response.cache_hint.ttl_s == 900


def test_server_ms_survives_so_network_cost_stays_separable() -> None:
    """Round-trip minus ``server_ms`` is the network cost. Losing it folds the
    two together and makes a slow link look like a slow server."""
    outcome = parse_response(status=200, body=_read("responses", "inject.json"))
    assert outcome.response.server_ms == 118


# --- the degradation ladder ---------------------------------------------------


def test_non_200_degrades_as_http_error() -> None:
    outcome = parse_response(status=503, body=_read("responses", "inject.json"))
    assert outcome.reason is DegradationReason.http_error
    assert outcome.response is ALLOW


def test_body_that_is_not_json_degrades_as_malformed() -> None:
    outcome = parse_response(status=200, body=_read("degraded", "malformed_body.json"))
    assert outcome.reason is DegradationReason.malformed_body


def test_json_that_is_not_an_object_degrades_as_malformed() -> None:
    outcome = parse_response(status=200, body=_read("degraded", "not_an_object.json"))
    assert outcome.reason is DegradationReason.malformed_body


def test_empty_body_degrades_rather_than_raising() -> None:
    assert parse_response(status=200, body="").reason is DegradationReason.malformed_body


def test_a_newer_contract_degrades_before_it_is_interpreted() -> None:
    outcome = parse_response(status=200, body=_read("degraded", "contract_mismatch.json"))
    assert outcome.reason is DegradationReason.contract_mismatch
    assert "2" in outcome.detail


def test_an_unknown_action_on_the_current_contract_is_its_own_reason() -> None:
    outcome = parse_response(status=200, body=_read("degraded", "unknown_action.json"))
    assert outcome.reason is DegradationReason.unknown_action


def test_a_bad_field_on_a_known_action_is_schema_mismatch() -> None:
    outcome = parse_response(status=200, body=_read("degraded", "schema_mismatch.json"))
    assert outcome.reason is DegradationReason.schema_mismatch
    assert "server_ms" in outcome.detail


def test_contract_is_checked_before_action() -> None:
    """Order matters: a skewed server sending an unknown action must report the
    skew, which an upgrade fixes, not an action mismatch, which implies a bug."""
    body = json.dumps({"contract": 99, "action": "rewrite_history"})
    assert parse_response(status=200, body=body).reason is DegradationReason.contract_mismatch


def test_action_is_checked_before_full_validation() -> None:
    """Otherwise every unknown action reports as ``schema_mismatch`` and the
    two causes become indistinguishable in the recorded data."""
    body = json.dumps({"contract": CONTRACT_VERSION, "action": "nope", "server_ms": "late"})
    assert parse_response(status=200, body=body).reason is DegradationReason.unknown_action


@pytest.mark.parametrize(
    "body",
    [
        "",
        "null",
        "[]",
        "{}",
        '{"contract": 1}',
        '{"action": "inject"}',
        '{"contract": null, "action": null}',
        '{"contract": 1, "action": "inject", "provenance": "not a list"}',
        '{"contract": 1, "action": "inject", "cache_hint": {"ttl_s": "soon"}}',
    ],
)
def test_no_body_can_make_parsing_raise(body: str) -> None:
    """The whole module's contract in one test: a caller on the hot path never
    needs a try/except, because there is nothing here that raises."""
    outcome = parse_response(status=200, body=body)
    assert outcome.response.action is Action.allow


@pytest.mark.parametrize("status", [200, 204, 301, 400, 401, 429, 500, 503])
def test_no_status_can_make_parsing_raise(status: int) -> None:
    outcome = parse_response(status=status, body='{"contract": 1, "action": "noop"}')
    assert (outcome.reason is DegradationReason.none) is (status == 200)


def test_every_degradation_reason_resolves_to_allow() -> None:
    """Whatever went wrong, the session continues untouched."""
    for reason in DegradationReason:
        assert degraded(reason).response.action is Action.allow


def test_a_degraded_outcome_never_claims_the_server_decided() -> None:
    for reason in DegradationReason:
        outcome = degraded(reason)
        assert outcome.degraded is (reason is not DegradationReason.none)


# --- constants that other builds depend on ------------------------------------


def test_allow_sentinel_is_contract_current() -> None:
    assert ALLOW.contract == CONTRACT_VERSION
    assert ALLOW.action is Action.allow


def test_backstop_stays_under_claude_codes_prompt_hook_cancellation() -> None:
    """UserPromptSubmit hooks are cancelled at 30s and their output discarded —
    which degrades with no reason recorded. The backstop must fire first so the
    hook always returns a decision of its own."""
    assert CLIENT_BACKSTOP_SECONDS < 30.0


def test_trace_ids_are_prefixed_and_time_sortable() -> None:
    first = new_trace_id()
    second = new_trace_id()
    assert first.startswith(TRACE_PREFIX)
    assert first != second


def test_response_defaults_are_empty_not_none() -> None:
    """Callers iterate these without guarding; ``None`` would raise on the hot path."""
    response = HookResponse(contract=CONTRACT_VERSION, action=Action.noop)
    assert response.provenance == []
    assert response.flags_applied == []
