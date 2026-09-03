"""Hook contract v2: the vendored schemas, their fixtures, and the models' mirror of them.

The fixtures and schemas under ``tests/fixtures/ci_contract/v2`` are the
server's bytes. These tests hold three things together: the vendored schemas
accept their valid fixtures and refuse their invalid ones for the documented
reason (which proves the ``$ref`` resolver, not just the files); every request
this build can emit validates against the server's schema *via jsonschema*,
never via a Python reading of it; and every fixture round-trips through the
pydantic models unchanged, so a field renamed on one side goes red here rather
than at integration.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from aisquare.core.ids import new_trace_id
from aisquare.models import ClientReason
from aisquare.services.ci_contract import (
    CONTRACT_VERSION,
    MAX_DETAIL_CHARS,
    RECALL_ROUTE,
    RECALL_TOOL,
    RFC3339_Z,
    SESSION_ID,
    Briefing,
    DeliveryDescriptor,
    ErrorRecord,
    HookRequest,
    HookResponse,
    RecallInput,
    degraded,
    is_contract_current,
    observed_now,
    parse_briefing,
    parse_response,
    wire_session_id,
)
from tests import ci_schemas
from tests.ci_schemas import CONTRACTS, assert_valid, errors, fixture, fixture_text
from tests.ci_support import request

REPO = Path(__file__).resolve().parents[1]

# --- the vendored corpus --------------------------------------------------------


@pytest.mark.parametrize("name", CONTRACTS)
def test_every_vendored_contract_has_a_schema_and_both_fixtures(name: str) -> None:
    assert (ci_schemas.SCHEMAS / f"{name}.schema.json").exists()
    assert (ci_schemas.FIXTURES / f"{name}.valid.json").exists()
    assert (ci_schemas.FIXTURES / f"{name}.invalid.json").exists()


@pytest.mark.parametrize("name", CONTRACTS)
def test_the_valid_fixtures_satisfy_the_vendored_schemas(name: str) -> None:
    assert errors(name, fixture(f"{name}.valid")) == []


#: Where each invalid fixture fails, per the server's INVALID-CASES.md. Pinned so
#: the resolver is proven: the hook-response and descriptor schemas only produce
#: THESE paths when their ``$ref``s and ``oneOf``s actually resolve.
_DOCUMENTED_FAILURE = {
    "hook-request.experimental-v2": "$.prompt",
    "hook-response.experimental-v2": "$.action",
    "mcp-tool-input.v1": "$",
    "mcp-tool-output.v1": "$.items[0]",
    "client-delivery-descriptor.v1": "$",
    "delivery-capability-manifest.v1": "$.capabilities[1]",
    "error.v1": "$.code",
}


@pytest.mark.parametrize("name", CONTRACTS)
def test_the_invalid_fixtures_fail_for_exactly_the_documented_reason(name: str) -> None:
    found = errors(name, fixture(f"{name}.invalid"))
    assert len(found) == 1, found
    assert found[0].startswith(_DOCUMENTED_FAILURE[name] + ":"), found[0]


def test_the_response_schema_really_follows_its_references() -> None:
    """A briefing item with a leaked source label must fail INSIDE the response,
    which only happens if ``$ref`` to mcp-tool-output.v1 resolves."""
    response = fixture("hook-response.experimental-v2.valid")
    response["briefing"]["items"][0]["source_kind"] = "fixture_oracle"
    found = errors("hook-response.experimental-v2", response)
    assert any(line.startswith("$.briefing") for line in found), found


def test_the_response_schema_reaches_the_error_catalog() -> None:
    response = fixture("hook-response.experimental-v2.invalid")
    response["action"] = "noop"
    response["errors"][0]["code"] = "free text"
    found = errors("hook-response.experimental-v2", response)
    assert any(line.startswith("$.errors[0].code") for line in found), found


def test_vendored_bytes_match_the_server_when_its_checkout_is_beside_this_one() -> None:
    """The server's bytes win. When ``aisquare-ci`` is cloned as a sibling, every
    vendored file must be byte-identical to it; anywhere else this is skipped."""
    server = REPO.parent / "aisquare-ci" / "contracts"
    if not server.is_dir():
        pytest.skip("no sibling aisquare-ci checkout to compare against")
    drifted: list[str] = []
    for name in CONTRACTS:
        family = "kernel" if name == "error.v1" else "delivery"
        pairs = [
            (
                ci_schemas.SCHEMAS / f"{name}.schema.json",
                server / "jsonschema" / family / f"{name}.schema.json",
            ),
            (
                ci_schemas.FIXTURES / f"{name}.valid.json",
                server / "fixtures" / "valid" / f"{name}.valid.json",
            ),
            (
                ci_schemas.FIXTURES / f"{name}.invalid.json",
                server / "fixtures" / "invalid" / f"{name}.invalid.json",
            ),
        ]
        drifted += [
            str(ours.name) for ours, theirs in pairs if ours.read_bytes() != theirs.read_bytes()
        ]
    assert not drifted, f"re-vendor from the server: {drifted}"


# --- round trips through the models ----------------------------------------------


def test_the_request_fixture_round_trips_unchanged() -> None:
    raw = fixture("hook-request.experimental-v2.valid")
    assert HookRequest.model_validate(raw).to_wire() == raw


def test_the_response_fixture_round_trips_unchanged() -> None:
    raw = fixture("hook-response.experimental-v2.valid")
    assert HookResponse.model_validate(raw).model_dump(mode="json") == raw


def test_the_briefing_fixture_round_trips_unchanged() -> None:
    raw = fixture("mcp-tool-output.v1.valid")
    assert Briefing.model_validate(raw).model_dump(mode="json") == raw


def test_the_descriptor_fixture_round_trips_unchanged() -> None:
    raw = fixture("client-delivery-descriptor.v1.valid")
    assert DeliveryDescriptor.model_validate(raw).model_dump(mode="json") == raw


def test_the_recall_input_fixture_round_trips_unchanged() -> None:
    raw = fixture("mcp-tool-input.v1.valid")
    assert RecallInput.model_validate(raw).model_dump(mode="json", exclude_none=True) == raw


def test_the_error_fixture_round_trips_unchanged() -> None:
    raw = fixture("error.v1.valid")
    assert ErrorRecord.model_validate(raw).model_dump(mode="json") == raw


def test_the_briefing_inside_the_response_is_the_tool_output_fixture() -> None:
    """Push and pull return the same object; the fixtures say so byte for byte."""
    response = fixture("hook-response.experimental-v2.valid")
    assert response["briefing"] == fixture("mcp-tool-output.v1.valid")


@pytest.mark.parametrize(
    ("model", "name"),
    [
        (HookRequest, "hook-request.experimental-v2"),
        (DeliveryDescriptor, "client-delivery-descriptor.v1"),
        (RecallInput, "mcp-tool-input.v1"),
        (Briefing, "mcp-tool-output.v1"),
    ],
)
def test_the_models_refuse_what_the_schemas_refuse(model: type[BaseModel], name: str) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(fixture(f"{name}.invalid"))


def test_the_invalid_response_fixture_is_a_schema_mismatch() -> None:
    """``action: block`` — the one case the whole schema exists to make impossible."""
    outcome = parse_response(status=200, body=fixture_text("hook-response.experimental-v2.invalid"))
    assert outcome.reason is ClientReason.schema_mismatch
    assert "action" in outcome.detail


def test_the_error_code_is_the_one_place_the_model_is_looser_than_the_schema() -> None:
    """Deliberate, and recorded here so it cannot pass for an accident: the
    catalog is the server's, and a code this build has never seen is data to
    record verbatim, not a reason to discard an otherwise valid response."""
    raw = fixture("error.v1.invalid")
    assert errors("error.v1", raw), "the schema must still refuse free-text codes"
    assert ErrorRecord.model_validate(raw).code == raw["code"]


# --- what this build emits validates against the server's schema -------------------


@pytest.mark.parametrize("trigger", ["session_start", "prompt_submit", "agent_request"])
def test_every_request_this_build_can_emit_validates_via_jsonschema(trigger: str) -> None:
    prompt = None if trigger == "session_start" else "find the lock implementation"
    built = request(trigger=trigger, prompt=prompt, snapshot_ref="a" * 40)
    assert errors("hook-request.experimental-v2", built.to_wire()) == []


def test_a_request_with_nothing_optional_still_validates() -> None:
    built = request(project_ref=None, snapshot_ref=None)
    body = built.to_wire()
    assert body["project_ref"] is None and body["snapshot_ref"] is None
    assert_valid("hook-request.experimental-v2", body)


def test_the_wire_body_carries_all_ten_fields_with_nulls_kept() -> None:
    body = request(snapshot_ref=None).to_wire()
    assert set(body) == set(fixture("hook-request.experimental-v2.valid"))
    assert body["snapshot_ref"] is None


def test_a_session_start_with_a_prompt_cannot_be_built() -> None:
    with pytest.raises(ValidationError, match="null on session_start"):
        request(trigger="session_start", prompt="hello")


def test_a_prompt_submit_without_a_prompt_cannot_be_built() -> None:
    with pytest.raises(ValidationError, match="prompt is required"):
        request(prompt=None)


def test_a_ref_name_is_not_a_snapshot() -> None:
    with pytest.raises(ValidationError, match="snapshot_ref"):
        request(snapshot_ref="refs/aisquare/wip/trc_x")


@pytest.mark.parametrize("field", ["run_id", "session_id", "trace_id"])
def test_a_scope_id_cannot_ride_in_through_an_id_field(field: str) -> None:
    with pytest.raises(ValidationError, match=field):
        request(**{field: "ws_kernel01"})


def test_an_unknown_request_field_is_refused_not_ignored() -> None:
    """Silent ignoring is the dangerous variant: a client that believes it is
    scoping a request, and a reviewer who reads it as if it worked."""
    with pytest.raises(ValidationError):
        request(workspace_id="ws_x")


def test_the_client_clock_is_written_the_way_the_schema_wants() -> None:
    assert RFC3339_Z.match(observed_now())


# --- the session id on the wire ----------------------------------------------------


def test_a_claude_code_session_id_is_prefixed_and_valid() -> None:
    wire = wire_session_id("3f2b6c2e-6d1a-4c5e-9c4b-1a2b3c4d5e6f")
    assert wire == "ses_3f2b6c2e-6d1a-4c5e-9c4b-1a2b3c4d5e6f"
    assert SESSION_ID.match(wire)


def test_an_already_prefixed_id_is_not_prefixed_twice() -> None:
    assert wire_session_id("ses_kernel0001") == "ses_kernel0001"


@pytest.mark.parametrize("hostile", ["", "../../escape", "a b:c", "_leading", "x" * 300, "ses_"])
def test_no_session_id_can_produce_an_invalid_wire_form(hostile: str) -> None:
    assert SESSION_ID.match(wire_session_id(hostile))


# --- the ladder --------------------------------------------------------------------


def test_the_valid_response_parses_undegraded() -> None:
    outcome = parse_response(status=200, body=fixture_text("hook-response.experimental-v2.valid"))
    assert outcome.reason is ClientReason.none
    assert outcome.action == "inject"
    assert outcome.briefing is not None and outcome.briefing.query_id == "qry_kernel0001"


def test_non_200_degrades_as_http_error() -> None:
    outcome = parse_response(status=503, body=fixture_text("hook-response.experimental-v2.valid"))
    assert outcome.reason is ClientReason.http_error
    assert outcome.response is None
    assert outcome.error_codes == () and outcome.detail == "status 503"


def test_a_non_200_with_an_error_body_keeps_the_code_and_the_sentence() -> None:
    """Live, a 503 says *why* in an error.v1 body — "run … has no completed
    build" — and a bare ``status 503`` would throw that sentence away."""
    from tests.stub_ci_server import error_v1

    body = error_v1(
        "dependency_unavailable", 503, "run run_x has no completed build", run_id="run_x"
    )
    assert errors("error.v1", body) == [], "the stub writes what the schema pins"
    outcome = parse_response(status=503, body=json.dumps(body))
    assert outcome.reason is ClientReason.http_error and outcome.response is None
    assert outcome.error_codes == ("dependency_unavailable",)
    assert outcome.detail == "status 503 dependency_unavailable: run run_x has no completed build"


def test_an_error_body_this_build_cannot_read_is_still_a_plain_http_error() -> None:
    for body in ("", "<html/>", "[]", '{"code": "x"}', json.dumps({"schema_version": "error/v2"})):
        outcome = parse_response(status=502, body=body)
        assert outcome.reason is ClientReason.http_error and outcome.error_codes == (), body
        assert outcome.detail == "status 502"


def test_a_servers_sentence_reaches_a_detail_printable_only() -> None:
    """The same sanitiser the frame applies: control codes, bidi overrides and
    lone surrogates in an error.v1 message never reach doctor's output."""
    from tests.stub_ci_server import error_v1

    body = error_v1("dependency_unavailable", 503, "no build\x1b[31m \u202ehidden\u202c end")
    outcome = parse_response(status=503, body=json.dumps(body))
    assert outcome.detail == "status 503 dependency_unavailable: no build[31m hidden end"
    # A lone surrogate in the message: pydantic refuses the string outright, so
    # the body is simply not read — the bare status, and nothing raised.
    body = error_v1("dependency_unavailable", 503, "no build \ud800 end")
    outcome = parse_response(status=503, body=json.dumps(body))
    assert outcome.detail == "status 503" and outcome.error_codes == ()
    outcome.detail.encode("utf-8")


def test_a_long_error_message_is_clipped_in_the_detail_not_on_the_row() -> None:
    from tests.stub_ci_server import error_v1

    body = error_v1("dependency_unavailable", 503, "x" * 1_500)
    outcome = parse_response(status=503, body=json.dumps(body))
    assert len(outcome.detail) == MAX_DETAIL_CHARS and outcome.detail.endswith("…")
    assert outcome.error_codes == ("dependency_unavailable",)


@pytest.mark.parametrize("status", [201, 204, 301, 400, 401, 429, 500])
def test_only_exactly_200_is_a_response(status: int) -> None:
    body = fixture_text("hook-response.experimental-v2.valid")
    assert parse_response(status=status, body=body).reason is ClientReason.http_error


@pytest.mark.parametrize("body", ["", "not json", "null", "[]", "42", '"x"'])
def test_bodies_that_are_not_an_object_are_malformed(body: str) -> None:
    assert parse_response(status=200, body=body).reason is ClientReason.malformed_body


def test_twenty_thousand_levels_of_nesting_do_not_raise() -> None:
    """``json.loads`` raises RecursionError, not ValueError, on deep nesting —
    verified on 3.12 at ~20 KB of ``[``. A total parser catches Exception."""
    outcome = parse_response(status=200, body="[" * 20_000 + "]" * 20_000)
    assert outcome.reason is ClientReason.malformed_body


def test_deep_nesting_inside_a_field_does_not_raise_either() -> None:
    body = '{"contract": 2, "errors": ' + "[" * 20_000 + "]" * 20_000 + "}"
    assert parse_response(status=200, body=body).reason in (
        ClientReason.malformed_body,
        ClientReason.schema_mismatch,
    )


@pytest.mark.parametrize("contract", ["1", "3", "true", "2.0", '"2"', "null"])
def test_anything_but_the_integer_two_is_a_contract_mismatch(contract: str) -> None:
    body = fixture_text("hook-response.experimental-v2.valid").replace(
        '"contract": 2', f'"contract": {contract}', 1
    )
    outcome = parse_response(status=200, body=body)
    assert outcome.reason is ClientReason.contract_mismatch
    assert str(CONTRACT_VERSION) in outcome.detail


def test_a_missing_contract_is_a_mismatch_not_a_schema_error() -> None:
    assert parse_response(status=200, body="{}").reason is ClientReason.contract_mismatch


def test_is_contract_current_is_exact() -> None:
    assert is_contract_current(2)
    assert not is_contract_current(True)
    assert not is_contract_current(2.0)
    assert not is_contract_current("2")


def _response(**changes: object) -> str:
    raw = fixture("hook-response.experimental-v2.valid")
    raw.update(changes)
    return json.dumps(raw)


@pytest.mark.parametrize(
    ("changes", "why"),
    [
        ({"action": "block"}, "block is not a v2 action"),
        ({"action": "noop"}, "served requires inject"),
        ({"briefing": None}, "inject requires a briefing"),
        ({"config_fingerprint": None}, "inject requires a fingerprint"),
        ({"status": "empty"}, "empty requires noop"),
        ({"status": "unavailable"}, "unavailable requires noop"),
        ({"status": "degraded"}, "degraded requires an error"),
        (
            {"deadline": {"server_ms": 1, "client_safety_ms": 1, "breached": True}},
            "breached requires unavailable",
        ),
        ({"errors": [fixture("error.v1.valid")]}, "served requires no errors"),
        ({"server_ms": -1}, "negative server_ms"),
        ({"arm": "B"}, "unknown key"),
    ],
)
def test_every_cross_field_rule_is_a_schema_mismatch(changes: dict[str, object], why: str) -> None:
    outcome = parse_response(status=200, body=_response(**changes))
    assert outcome.reason is ClientReason.schema_mismatch, why
    assert_broken = errors("hook-response.experimental-v2", json.loads(_response(**changes)))
    assert assert_broken, f"the vendored schema accepts what the model refuses: {why}"


def test_an_inject_with_no_briefing_never_parses_as_a_healthy_decision() -> None:
    """The failure the module exists to make impossible: N successful injections
    recorded where the agent saw nothing."""
    outcome = parse_response(status=200, body=_response(briefing=None))
    assert outcome.degraded
    assert outcome.action == "noop"


def test_an_empty_answer_is_a_healthy_noop() -> None:
    raw = fixture("hook-response.experimental-v2.valid")
    raw.update(status="empty", action="noop", briefing=None)
    outcome = parse_response(status=200, body=json.dumps(raw))
    assert outcome.reason is ClientReason.none
    assert outcome.action == "noop"
    assert outcome.response is not None and outcome.response.status == "empty"


def test_a_degraded_answer_may_still_inject() -> None:
    raw = fixture("hook-response.experimental-v2.valid")
    raw.update(status="degraded", errors=[fixture("error.v1.valid")])
    outcome = parse_response(status=200, body=json.dumps(raw))
    assert outcome.reason is ClientReason.none
    assert outcome.action == "inject"
    assert outcome.response is not None
    assert [e.code for e in outcome.response.errors] == ["trace_batch_span_mismatch"]


def test_server_controlled_text_in_the_detail_is_clipped() -> None:
    body = '{"contract": "' + "x" * 200_000 + '"}'
    outcome = parse_response(status=200, body=body)
    assert outcome.reason is ClientReason.contract_mismatch
    assert len(outcome.detail) < MAX_DETAIL_CHARS + 100


def test_a_schema_mismatch_names_the_field() -> None:
    outcome = parse_response(status=200, body=_response(server_ms="late"))
    assert outcome.reason is ClientReason.schema_mismatch
    assert "server_ms" in outcome.detail


# --- outcomes ------------------------------------------------------------------------


def test_a_degraded_outcome_carries_no_response_and_acts_as_noop() -> None:
    for reason in ClientReason:
        if reason is ClientReason.none:
            continue
        outcome = degraded(reason, "why")
        assert outcome.response is None
        assert outcome.action == "noop"
        assert outcome.briefing is None
        assert outcome.degraded


def test_a_degraded_outcome_needs_a_real_reason() -> None:
    with pytest.raises(ValueError):
        degraded(ClientReason.none)


def test_a_parsed_response_is_immutable() -> None:
    """A shared outcome must never be mutated by one caller and read by the next."""
    response = HookResponse.model_validate(fixture("hook-response.experimental-v2.valid"))
    with pytest.raises(ValidationError):
        response.server_ms = 0


def test_a_briefing_that_says_served_must_carry_an_item() -> None:
    raw = fixture("mcp-tool-output.v1.valid")
    raw["items"] = []
    with pytest.raises(ValidationError, match="at least one item"):
        Briefing.model_validate(raw)


def test_an_unavailable_briefing_must_carry_nothing() -> None:
    raw = fixture("mcp-tool-output.v1.valid")
    raw.update(status="unavailable", briefing_id=None)
    with pytest.raises(ValidationError, match="unavailable"):
        Briefing.model_validate(raw)


# --- the descriptor --------------------------------------------------------------


def test_the_descriptor_says_how_to_deliver() -> None:
    descriptor = DeliveryDescriptor.model_validate(fixture("client-delivery-descriptor.v1.valid"))
    assert descriptor.pushes("prompt_submit")
    assert not descriptor.pushes("session_start")
    assert descriptor.hook_push is not None and descriptor.hook_push.endpoint == "/v1/hook"
    assert descriptor.mcp_pull is not None
    assert descriptor.expired()  # the fixture's expiry has passed


def test_the_descriptor_has_no_room_for_an_arm() -> None:
    """The blinding leak, refused at the only moment anyone is looking."""
    raw = fixture("client-delivery-descriptor.v1.valid")
    raw["arm_kind"] = "architecture_candidate"
    with pytest.raises(ValidationError):
        DeliveryDescriptor.model_validate(raw)


@pytest.mark.parametrize(
    "delivery",
    [
        [
            {"kind": "hook_push", "triggers": ["prompt_submit"], "endpoint": "/v1/hook"},
            {"kind": "hook_push", "triggers": ["session_start"], "endpoint": "/v1/hook"},
        ],
        [{"kind": "direct_api"}, {"kind": "mcp_pull", "tool": "collective_intelligence_recall"}],
        [
            {
                "kind": "hook_push",
                "triggers": ["prompt_submit", "prompt_submit"],
                "endpoint": "/v1/hook",
            }
        ],
        [{"kind": "hook_push", "triggers": ["prompt_submit"], "endpoint": "https://elsewhere/x"}],
        [{"kind": "mcp_pull", "tool": "collective_intelligence_explain"}],
        [],
    ],
)
def test_the_descriptor_refuses_what_the_schema_refuses(delivery: list[dict[str, object]]) -> None:
    raw = fixture("client-delivery-descriptor.v1.valid")
    raw["delivery"] = delivery
    assert errors("client-delivery-descriptor.v1", raw), "premise: the schema refuses it"
    with pytest.raises(ValidationError):
        DeliveryDescriptor.model_validate(raw)


def test_the_pull_route_is_the_servers() -> None:
    assert RECALL_ROUTE + RECALL_TOOL == "/v1/mcp/collective_intelligence_recall"


def test_every_recall_body_this_build_can_emit_validates_via_jsonschema() -> None:
    full = RecallInput.model_validate(fixture("mcp-tool-input.v1.valid"))
    assert errors("mcp-tool-input.v1", full.to_wire()) == []
    assert full.to_wire() == fixture("mcp-tool-input.v1.valid")
    bare = RecallInput(prompt="q", session_id="ses_x", run_id="run_x")
    assert errors("mcp-tool-input.v1", bare.to_wire()) == []
    assert bare.to_wire() == {"prompt": "q", "session_id": "ses_x", "run_id": "run_x"}, (
        "the optional keys are typed string/integer, never null — absent is the only valid unset"
    )


# --- integers the way JSON Schema means them ------------------------------------------


def _floated(
    value: object, *, skip: frozenset[str] = frozenset({"contract", "contract_version"})
) -> object:
    """Every integer in ``value`` as ``N.0`` — a producer that serialises doubles."""
    if isinstance(value, dict):
        return {k: (v if k in skip else _floated(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_floated(v) for v in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return float(value)
    return value


@pytest.mark.parametrize(
    ("name", "model"),
    [
        ("hook-response.experimental-v2", HookResponse),
        ("mcp-tool-output.v1", Briefing),
        ("client-delivery-descriptor.v1", DeliveryDescriptor),
        ("error.v1", ErrorRecord),
        ("mcp-tool-input.v1", RecallInput),
        ("hook-request.experimental-v2", HookRequest),
    ],
)
def test_integral_floats_are_the_integers_the_schema_says_they_are(
    name: str, model: type[BaseModel]
) -> None:
    """``"type": "integer"`` matches ``63.0``; strict pydantic did not, so a server
    that serialises numbers as doubles turned every good response into a
    recorded schema_mismatch — the endpoint looked broken and the count the
    experiment collects was a failure count."""
    raw = fixture(f"{name}.valid")
    floated = _floated(raw)
    assert json.dumps(floated) != json.dumps(raw), "the fixture has at least one integer to float"
    assert errors(name, floated) == [], "the schema accepts the floated fixture"
    assert model.model_validate(floated) == model.model_validate(raw)


def test_a_fractional_number_or_a_bool_is_still_refused_where_an_integer_is_due() -> None:
    raw = fixture("hook-response.experimental-v2.valid")
    for bad in (63.5, True, "63"):
        raw["server_ms"] = bad
        assert errors("hook-response.experimental-v2", raw), repr(bad)
        outcome = parse_response(status=200, body=json.dumps(raw))
        assert outcome.reason is ClientReason.schema_mismatch and "server_ms" in outcome.detail


# --- the pull ladder ---------------------------------------------------------------


def test_the_valid_briefing_parses_undegraded() -> None:
    outcome = parse_briefing(status=200, body=fixture_text("mcp-tool-output.v1.valid"))
    assert outcome.reason is ClientReason.none and not outcome.degraded
    assert outcome.briefing is not None and outcome.briefing.status == "served"


def test_an_empty_briefing_is_a_healthy_pull() -> None:
    raw = fixture("mcp-tool-output.v1.valid")
    raw.update(status="empty", briefing_id=None, items=[], rendered_context="", token_count=0)
    outcome = parse_briefing(status=200, body=json.dumps(raw))
    assert outcome.reason is ClientReason.none
    assert outcome.briefing is not None and outcome.briefing.status == "empty"


def test_a_non_200_pull_is_an_http_error_with_the_servers_code() -> None:
    from tests.stub_ci_server import error_v1

    body = error_v1("scope_resolution_failed", 422, "called without run_id")
    outcome = parse_briefing(status=422, body=json.dumps(body))
    assert outcome.reason is ClientReason.http_error and outcome.briefing is None
    assert outcome.error_codes == ("scope_resolution_failed",)
    assert outcome.detail == "status 422 scope_resolution_failed: called without run_id"
    assert parse_briefing(status=500, body="boom").detail == "status 500"


@pytest.mark.parametrize("body", ["", "not json", "null", "[]", "42", "x" * 10, "{" * 20_000])
def test_a_pull_body_that_is_not_an_object_is_malformed(body: str) -> None:
    assert parse_briefing(status=200, body=body).reason is ClientReason.malformed_body


def test_a_hook_envelope_on_the_pull_route_is_a_schema_mismatch_naming_the_field() -> None:
    """No ``contract`` rung on this ladder: the briefing has no such field, so a
    server that moved on shows as the first unknown or missing field."""
    outcome = parse_briefing(status=200, body=fixture_text("hook-response.experimental-v2.valid"))
    assert outcome.reason is ClientReason.schema_mismatch and outcome.briefing is None
    assert outcome.detail == "briefing_id: Field required", "the first field a briefing needs"


def test_a_served_pull_without_items_is_a_schema_mismatch_not_a_briefing() -> None:
    raw = fixture("mcp-tool-output.v1.valid")
    raw["items"] = []
    outcome = parse_briefing(status=200, body=json.dumps(raw))
    assert outcome.reason is ClientReason.schema_mismatch
    assert "at least one item" in outcome.detail


def test_a_recall_input_cannot_carry_authority() -> None:
    with pytest.raises(ValidationError):
        RecallInput.model_validate({"prompt": "x", "session_id": "ses_a", "workspace_id": "ws_1"})
    with pytest.raises(ValidationError, match="session_id"):
        RecallInput(prompt="x", session_id="ws_kernel01")


def test_trace_ids_fit_the_contract() -> None:
    body = request(trace_id=new_trace_id()).to_wire()
    assert errors("hook-request.experimental-v2", body) == []
