"""The transport's assertion matrix: bounds, classification, cache, secrecy.

Every test here drives the real :class:`HttpPlatformTransport` through a real
``httpx2.Client`` whose transport is a mock, so the code under test is the code
that would run against the gateway — the request it builds, the headers it
attaches, the streaming read, the decode and the bounds. Nothing is stubbed at
the method level, because the interesting failures in this module are in the
seams between those steps.

**No live call is made by this file, and none was made to write it.** The
deployed Explainability and Praxis APIs were never contacted during P12; the
shapes come from source-mined evidence recorded in the Office repository.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import pytest

from aisquare.office.adapters.platform_transport import (
    CONTEXT_TOTAL_S,
    NOT_FOUND_OR_MASKED,
    WRITE_TOTAL_S,
    HttpPlatformTransport,
    budget_for,
    classify_status,
    default_client,
    normalized_query,
)
from aisquare.office.config import OfficeConfig
from aisquare.office.models import PlatformBinding, PlatformQuery
from aisquare.office.platform_config import (
    AGENT_UID_ENV_VAR,
    STUDIO_ENV_VAR,
    WORKSPACE_ENV_VAR,
    PlatformProfile,
    resolve_binding,
)
from aisquare.office.platform_redaction import MARKER

HOME = Path("/tmp/office-home")
KEY = "wk_live_synthetic0123456789abcdefghij"
AGENT = "00000000-0000-4000-8000-000000000000"
WORKSPACE = "1042"
STUDIO = "482"
RUNS_PATH = f"/v1/workspaces/{WORKSPACE}/runs"

PROFILE = PlatformProfile(
    name="stg",
    base_url="https://gateway.test",
    api_key_env="EXPLAINABILITY_API_KEY",
    key_source="env",
)

Responder = Callable[[httpx2.Request], httpx2.Response]


class FakeClock:
    """Wall time that never moves and monotonic time the test drives.

    ``step`` makes every reading of the monotonic clock jump, which is how the
    total-budget path is exercised without a slow test: a body that keeps
    arriving inside every per-chunk read deadline is exactly the case the total
    budget exists for, and it cannot be produced by waiting.
    """

    def __init__(self, *, step: float = 0.0) -> None:
        self._monotonic = 0.0
        self._step = step
        self._now = datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        value = self._monotonic
        self._monotonic += self._step
        return value

    def advance(self, seconds: float) -> None:
        self._monotonic += seconds


class FakeSource:
    def __init__(self, credential: str | None = KEY, profile: PlatformProfile = PROFILE) -> None:
        self._credential = credential
        self._profile = profile
        self.credential_calls = 0

    def profile(self, name: str | None) -> PlatformProfile:
        return self._profile

    def credential(self, profile: PlatformProfile) -> str | None:
        self.credential_calls += 1
        return self._credential


class Recorder:
    """Counts what actually reached the wire, which several tests assert on."""

    def __init__(self, responder: Responder) -> None:
        self.requests: list[httpx2.Request] = []
        self._responder = responder

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self._responder(request)


def json_response(
    status: int, payload: object, headers: Mapping[str, str] | None = None
) -> httpx2.Response:
    return httpx2.Response(status, json=payload, headers=dict(headers or {}))


def make_binding(
    source: FakeSource,
    *,
    studio: str | None = None,
    agent: str | None = None,
    config: OfficeConfig | None = None,
) -> PlatformBinding:
    env = {WORKSPACE_ENV_VAR: WORKSPACE}
    if studio is not None:
        env[STUDIO_ENV_VAR] = studio
    if agent is not None:
        env[AGENT_UID_ENV_VAR] = agent
    resolution = resolve_binding(
        project_id="prj_office",
        config=config if config is not None else OfficeConfig(home=HOME),
        source=source,
        env=env,
    )
    assert resolution.binding is not None, resolution.error
    return resolution.binding


def build(
    responder: Responder,
    *,
    clock: FakeClock | None = None,
    source: FakeSource | None = None,
    config: OfficeConfig | None = None,
    cache_ttl_s: float = 15.0,
    max_body_bytes: int = 2 * 1_048_576,
) -> tuple[HttpPlatformTransport, Recorder, FakeSource]:
    recorder = Recorder(responder)
    credentials = source if source is not None else FakeSource()
    transport = HttpPlatformTransport(
        config=config if config is not None else OfficeConfig(home=HOME),
        clock=clock if clock is not None else FakeClock(),
        source=credentials,
        client=httpx2.Client(
            transport=httpx2.MockTransport(recorder), trust_env=False, follow_redirects=False
        ),
        cache_ttl_s=cache_ttl_s,
        max_body_bytes=max_body_bytes,
    )
    return transport, recorder, credentials


def ok_runs(_request: httpx2.Request) -> httpx2.Response:
    return json_response(200, {"status": "ok", "runs": []})


# --------------------------------------------------------------------------
# The configured happy path
# --------------------------------------------------------------------------


def test_a_configured_workspace_read_sends_the_key_once_in_one_header() -> None:
    transport, recorder, _ = build(ok_runs)
    binding = make_binding(FakeSource())

    result = transport.request("GET", RUNS_PATH, binding=binding)

    assert result.error is None
    sent = recorder.requests[0]
    assert sent.headers.get_list("x-api-key") == [KEY]
    assert "authorization" not in sent.headers
    assert "cookie" not in sent.headers
    assert KEY not in str(sent.url)


def test_a_two_hundred_becomes_a_bounded_response_with_the_clock_s_timestamp() -> None:
    clock = FakeClock()
    transport, _, _ = build(
        lambda _r: json_response(
            200,
            {"runs": [], "next_offset": None},
            {"x-request-id": "req-1", "set-cookie": "session=abc", "date": "Thu, 11 Sep 2026"},
        ),
        clock=clock,
    )

    result = transport.request("GET", RUNS_PATH, binding=make_binding(FakeSource()))

    response = result.response
    assert response is not None
    assert response.status_code == 200
    assert response.json_body == {"runs": [], "next_offset": None}
    assert response.received_at == clock.now()
    assert response.request_id == "req-1"
    assert "set-cookie" not in response.allowed_headers
    assert "date" in response.allowed_headers


def test_an_empty_list_is_a_successful_read_and_never_an_outage() -> None:
    """Checklist item 6, and the shape a brand-new workspace really answers."""
    transport, _, _ = build(
        lambda _r: json_response(200, {"status": "ok", "runs": [], "studios_read": []})
    )

    result = transport.request("GET", RUNS_PATH, binding=make_binding(FakeSource()))

    assert result.error is None
    assert result.response is not None
    assert result.response.json_body == {"status": "ok", "runs": [], "studios_read": []}


# --------------------------------------------------------------------------
# Refusals that happen before any request is sent
# --------------------------------------------------------------------------


def test_a_missing_credential_is_unconfigured_and_sends_nothing() -> None:
    transport, recorder, _ = build(ok_runs, source=FakeSource(credential=None))
    binding = make_binding(FakeSource())

    result = transport.request("GET", RUNS_PATH, binding=binding)

    assert result.error is not None
    assert result.error.code == "service_unconfigured"
    assert recorder.requests == []


def test_a_workspace_the_binding_did_not_resolve_is_refused_before_transport() -> None:
    transport, recorder, _ = build(ok_runs)

    result = transport.request(
        "GET", "/v1/workspaces/9999/runs", binding=make_binding(FakeSource())
    )

    assert result.error is not None
    assert result.error.code == "binding_required"
    assert recorder.requests == []


def test_a_studio_route_without_a_bound_studio_is_refused_before_transport() -> None:
    transport, recorder, _ = build(ok_runs)

    result = transport.request(
        "GET", f"/v1/studios/{STUDIO}/praxis/context", binding=make_binding(FakeSource())
    )

    assert result.error is not None
    assert result.error.code == "binding_required"
    assert "studio" in result.error.detail
    assert recorder.requests == []


def test_a_path_carrying_its_own_query_or_traversal_is_refused() -> None:
    transport, recorder, _ = build(ok_runs)
    binding = make_binding(FakeSource())

    for path in (f"{RUNS_PATH}?limit=1", "/v1/workspaces/../admin", "relative/path"):
        result = transport.request("GET", path, binding=binding)
        assert result.error is not None, path
        assert result.error.code == "internal", path
    assert recorder.requests == []


def test_the_unreachable_praxis_routes_are_a_capability_fact_and_never_retried() -> None:
    """The resolved question, encoded: a workspace key 403s on every
    studio-scoped Praxis guard, so Office does not spend a request finding out
    — this time or any subsequent time."""
    transport, recorder, _ = build(ok_runs)
    binding = make_binding(FakeSource(), studio=STUDIO)

    first = transport.request("GET", f"/v1/studios/{STUDIO}/praxis/insights", binding=binding)
    second = transport.request("GET", f"/v1/studios/{STUDIO}/praxis/insights", binding=binding)

    assert first.error is not None
    assert first.error.code == "unsupported_capability"
    assert first.error.retryable is False
    assert second.error is not None
    assert second.error.code == "unsupported_capability"
    assert recorder.requests == []


def test_the_context_route_is_allowed_through_because_its_guard_differs() -> None:
    transport, recorder, _ = build(lambda _r: json_response(200, {"source": "praxis"}))
    binding = make_binding(FakeSource(), studio=STUDIO)

    result = transport.request(
        "GET", f"/v1/studios/{STUDIO}/praxis/context", binding=binding, timeout_class="context"
    )

    assert result.error is None
    assert len(recorder.requests) == 1


def test_an_unknown_timeout_class_is_refused_rather_than_silently_defaulted() -> None:
    transport, recorder, _ = build(ok_runs)

    result = transport.request(
        "GET", RUNS_PATH, binding=make_binding(FakeSource()), timeout_class="eventually"
    )

    assert result.error is not None
    assert result.error.code == "internal"
    assert recorder.requests == []


# --------------------------------------------------------------------------
# Statuses
# --------------------------------------------------------------------------


def test_an_error_status_is_preserved_as_a_response_so_the_adapter_can_read_it() -> None:
    """A 404 has four meanings in this API. Collapsing it into a transport
    error here would throw away the only evidence that separates them."""
    transport, _, _ = build(lambda _r: json_response(404, {"detail": "Workspace not found"}))

    result = transport.request("GET", RUNS_PATH, binding=make_binding(FakeSource()))

    assert result.error is None
    assert result.response is not None
    assert result.response.status_code == 404
    assert result.response.json_body == {"detail": "Workspace not found"}


def test_classify_maps_the_statuses_the_packet_brief_enumerates() -> None:
    assert classify_status(200)[0] == "ok"
    assert classify_status(401)[0] == "unauthorized"
    assert classify_status(403)[0] == "forbidden"
    assert classify_status(429)[1] is not None
    assert classify_status(429)[1].code == "rate_limited"  # type: ignore[union-attr]
    assert classify_status(429)[1].retryable is True  # type: ignore[union-attr]
    assert classify_status(502)[1].retryable is True  # type: ignore[union-attr]
    assert classify_status(501)[0] == "unsupported"


def test_a_not_configured_five_oh_three_is_a_capability_fact_not_an_outage() -> None:
    """Retrying a permanent deployment fact forever is the failure mode here."""
    status, error = classify_status(503, detail="praxis not configured")

    assert status == "unsupported"
    assert error is not None
    assert error.code == "unsupported_capability"
    assert error.retryable is False


def test_an_outage_five_oh_three_stays_retryable() -> None:
    status, error = classify_status(503, detail="None of this workspace's studios could be read")

    assert status == "unavailable"
    assert error is not None
    assert error.retryable is True


def test_a_forbidden_detail_distinguishes_a_missing_scope_from_a_missing_owner() -> None:
    """``_enforce_ingest_scope`` runs before the ownership check, so a bare
    "forbidden" leaves an operator unable to tell which one to fix."""
    _, scope = classify_status(403, detail="API key lacks ingest:write scope")
    _, owner = classify_status(403, detail="Studio ID mismatch")

    assert scope is not None
    assert owner is not None
    assert "scope" in scope.detail
    assert "own" in owner.detail
    assert scope.detail != owner.detail


def test_a_classified_four_oh_four_says_it_may_be_masked_rather_than_absent() -> None:
    _, error = classify_status(404, detail="Run not found")

    assert error is not None
    assert error.detail.startswith(NOT_FOUND_OR_MASKED)
    assert error.retryable is False


def test_a_permanent_four_hundred_is_reported_as_not_retryable() -> None:
    _, error = classify_status(400, detail="since='yesterday' is not an ISO-8601 timestamp")

    assert error is not None
    assert error.retryable is False
    assert error.code == "upstream_invalid"


# --------------------------------------------------------------------------
# Failures that produce an error rather than a response
# --------------------------------------------------------------------------


def test_a_timeout_is_a_retryable_timeout_and_is_attempted_exactly_once() -> None:
    def slow(_request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("the gateway went quiet")

    transport, recorder, _ = build(slow)

    result = transport.request("GET", RUNS_PATH, binding=make_binding(FakeSource()))

    assert result.error is not None
    assert result.error.code == "timeout"
    assert result.error.retryable is True
    assert len(recorder.requests) == 1


def test_a_body_still_arriving_at_the_total_budget_is_a_timeout() -> None:
    """The case a per-socket read deadline cannot catch: every chunk arrives in
    time and the body never ends."""
    clock = FakeClock(step=4.0)
    transport, _, _ = build(
        lambda _r: httpx2.Response(
            200, content=b'{"runs": []}', headers={"content-type": "application/json"}
        ),
        clock=clock,
    )

    result = transport.request("GET", RUNS_PATH, binding=make_binding(FakeSource()))

    assert result.error is not None
    assert result.error.code == "timeout"


def test_an_unreachable_gateway_is_retryable_and_names_no_credential() -> None:
    def refused(_request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError(f"connection refused while sending {KEY}")

    transport, _, _ = build(refused)

    result = transport.request("GET", RUNS_PATH, binding=make_binding(FakeSource()))

    assert result.error is not None
    assert result.error.code == "service_unavailable"
    assert result.error.retryable is True
    assert KEY not in result.error.detail


def test_a_non_json_content_type_is_rejected_rather_than_guessed_at() -> None:
    transport, _, _ = build(
        lambda _r: httpx2.Response(200, content=b"<html>", headers={"content-type": "text/html"})
    )

    result = transport.request("GET", RUNS_PATH, binding=make_binding(FakeSource()))

    assert result.error is not None
    assert result.error.code == "upstream_invalid"


def test_an_undecodable_body_is_rejected_with_a_stable_code() -> None:
    transport, _, _ = build(
        lambda _r: httpx2.Response(
            200, content=b"{not json", headers={"content-type": "application/json"}
        )
    )

    result = transport.request("GET", RUNS_PATH, binding=make_binding(FakeSource()))

    assert result.error is not None
    assert result.error.code == "upstream_invalid"


def test_an_oversized_body_is_stopped_at_the_ceiling_not_after_it() -> None:
    payload = b'["' + b"x" * 4096 + b'"]'
    transport, _, _ = build(
        lambda _r: httpx2.Response(
            200, content=payload, headers={"content-type": "application/json"}
        ),
        max_body_bytes=512,
    )

    result = transport.request("GET", RUNS_PATH, binding=make_binding(FakeSource()))

    assert len(payload) > 512
    assert result.error is not None
    assert result.error.code == "upstream_invalid"
    assert "512" in result.error.detail


def test_a_body_nested_past_the_depth_ceiling_is_rejected() -> None:
    deep: object = "leaf"
    for _ in range(30):
        deep = [deep]
    transport, _, _ = build(lambda _r: json_response(200, {"runs": deep}))

    result = transport.request("GET", RUNS_PATH, binding=make_binding(FakeSource()))

    assert result.error is not None
    assert result.error.code == "upstream_invalid"
    assert "nests" in result.error.detail


def test_a_redirect_is_answered_rather_than_followed_with_the_key_attached() -> None:
    transport, recorder, _ = build(
        lambda _r: httpx2.Response(302, headers={"location": "https://elsewhere.test/v1/runs"})
    )

    result = transport.request("GET", RUNS_PATH, binding=make_binding(FakeSource()))

    assert len(recorder.requests) == 1
    assert recorder.requests[0].url.host == "gateway.test"
    assert result.error is not None


def test_a_redirect_is_refused_even_by_an_injected_client_that_would_follow() -> None:
    """The no-follow rule is stated on the request, so the dependency-injected
    client cannot relax it. Otherwise a client built with follow_redirects=True
    would re-send X-API-KEY to whatever host the Location header named."""
    recorder = Recorder(
        lambda request: (
            httpx2.Response(302, headers={"location": "https://elsewhere.test/v1/runs"})
            if request.url.host == "gateway.test"
            else json_response(200, {"runs": []})
        )
    )
    transport = HttpPlatformTransport(
        config=OfficeConfig(home=HOME),
        clock=FakeClock(),
        source=FakeSource(),
        client=httpx2.Client(
            transport=httpx2.MockTransport(recorder), trust_env=False, follow_redirects=True
        ),
    )

    result = transport.request("GET", RUNS_PATH, binding=make_binding(FakeSource()))

    assert [request.url.host for request in recorder.requests] == ["gateway.test"]
    assert result.error is not None


# --------------------------------------------------------------------------
# Caching and the binding boundary
# --------------------------------------------------------------------------


def test_an_identical_read_is_served_from_the_cache() -> None:
    transport, recorder, _ = build(ok_runs)
    binding = make_binding(FakeSource())

    first = transport.request("GET", RUNS_PATH, binding=binding)
    second = transport.request("GET", RUNS_PATH, binding=binding)

    assert first.response is not None
    assert second.response is not None
    assert len(recorder.requests) == 1


def test_query_variants_do_not_share_a_cache_entry() -> None:
    transport, recorder, _ = build(ok_runs)
    binding = make_binding(FakeSource())

    transport.request("GET", RUNS_PATH, query=PlatformQuery(limit=1), binding=binding)
    transport.request("GET", RUNS_PATH, query=PlatformQuery(limit=2), binding=binding)

    assert len(recorder.requests) == 2
    assert str(recorder.requests[0].url) != str(recorder.requests[1].url)


def test_a_query_is_normalised_to_one_deterministic_order() -> None:
    left = normalized_query(PlatformQuery(limit=5, filters={"status": "ready", "agent": "a"}))
    right = normalized_query(PlatformQuery(limit=5, filters={"agent": "a", "status": "ready"}))

    assert left == right
    assert left == (("agent", "a"), ("limit", "5"), ("status", "ready"))


def test_a_cached_entry_expires_after_its_ttl() -> None:
    clock = FakeClock()
    transport, recorder, _ = build(ok_runs, clock=clock, cache_ttl_s=5.0)
    binding = make_binding(FakeSource())

    transport.request("GET", RUNS_PATH, binding=binding)
    clock.advance(6.0)
    transport.request("GET", RUNS_PATH, binding=binding)

    assert len(recorder.requests) == 2


def test_a_post_is_never_cached() -> None:
    transport, recorder, _ = build(lambda _r: json_response(200, {"ok": True}))
    binding = make_binding(FakeSource(), studio=STUDIO)

    transport.request("POST", f"/v1/studios/{STUDIO}/praxis/context", binding=binding)
    transport.request("POST", f"/v1/studios/{STUDIO}/praxis/context", binding=binding)

    assert len(recorder.requests) == 2


def test_a_revoked_credential_drops_the_cached_body_it_had_already_shown() -> None:
    """Losing authorisation is a hard cache boundary: a revoked binding must not
    keep painting a workspace's data."""
    detail_path = f"{RUNS_PATH}/abc"

    def responder(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == detail_path:
            return json_response(403, {"detail": "API key is not scoped to this workspace"})
        return json_response(200, {"runs": []})

    transport, recorder, _ = build(responder)
    binding = make_binding(FakeSource())

    transport.request("GET", RUNS_PATH, binding=binding)
    denied = transport.request("GET", detail_path, binding=binding)

    assert denied.response is not None
    assert denied.response.status_code == 403
    assert transport.last_success_at(binding) is None

    transport.request("GET", RUNS_PATH, binding=binding)

    assert [request.url.path for request in recorder.requests] == [
        RUNS_PATH,
        detail_path,
        RUNS_PATH,
    ]


def test_a_successful_read_records_a_last_success_for_that_revision_only() -> None:
    clock = FakeClock()
    transport, _, _ = build(ok_runs, clock=clock)
    binding = make_binding(FakeSource())

    transport.request("GET", RUNS_PATH, binding=binding)

    other = PlatformBinding(
        binding_id=binding.binding_id,
        revision=binding.revision + 1,
        project_id=binding.project_id,
        profile_name=binding.profile_name,
        workspace_id=binding.workspace_id,
    )
    assert transport.last_success_at(binding) == clock.now()
    assert transport.last_success_at(other) is None


def test_a_binding_whose_credential_moved_is_refused_and_its_cache_dropped() -> None:
    """The rotation nobody notices: same scope, same profile, new key. Without
    the revision check the caller would keep being served the old key's body."""
    transport, recorder, _ = build(ok_runs)
    binding = make_binding(FakeSource())
    transport.request("GET", RUNS_PATH, binding=binding)

    rotated = PlatformBinding(
        binding_id=binding.binding_id,
        revision=binding.revision + 1,
        project_id=binding.project_id,
        profile_name=binding.profile_name,
        workspace_id=binding.workspace_id,
    )
    result = transport.request("GET", RUNS_PATH, binding=rotated)

    assert result.error is not None
    assert result.error.code == "binding_required"
    assert len(recorder.requests) == 1
    assert transport.last_success_at(binding) is None


def test_invalidate_reports_how_many_entries_it_dropped() -> None:
    transport, _, _ = build(ok_runs)
    binding = make_binding(FakeSource())
    transport.request("GET", RUNS_PATH, query=PlatformQuery(limit=1), binding=binding)
    transport.request("GET", RUNS_PATH, query=PlatformQuery(limit=2), binding=binding)

    dropped = transport.invalidate(binding_id=binding.binding_id)

    assert dropped == 2
    assert transport.invalidate(binding_id=binding.binding_id) == 0


# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------


def test_the_preflight_plan_never_carries_a_run_id_and_never_writes() -> None:
    """A context read with ``run_id`` makes the assembler record a durable
    injection — audit evidence that an agent consumed a packet it never saw."""
    transport, _, _ = build(ok_runs)
    binding = make_binding(FakeSource(), studio=STUDIO, agent=AGENT)

    plan = transport.preflight_plan(binding)

    assert plan
    for probe in plan:
        assert probe.method == "GET", probe.name
        assert "run_id" not in probe.path, probe.name
        assert "/signals" not in probe.path, probe.name
        assert "/injection" not in probe.path, probe.name
        if probe.query is not None:
            assert "run_id" not in probe.query.filters, probe.name


def test_the_preflight_plan_starts_with_the_narrowest_scoped_read() -> None:
    transport, _, _ = build(ok_runs)
    binding = make_binding(FakeSource(), studio=STUDIO, agent=AGENT)

    plan = transport.preflight_plan(binding)

    assert [probe.name for probe in plan] == [
        "workspace-runs-list",
        "praxis-agent-insights",
        "praxis-context",
    ]
    assert plan[0].query is not None
    assert plan[0].query.limit == 1
    assert plan[2].timeout_class == "context"


def test_the_preflight_plan_omits_probes_the_binding_cannot_support() -> None:
    transport, _, _ = build(ok_runs)

    plan = transport.preflight_plan(make_binding(FakeSource()))

    assert [probe.name for probe in plan] == ["workspace-runs-list"]


def test_a_run_detail_is_probed_only_after_the_list_supplies_its_id() -> None:
    def responder(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == RUNS_PATH:
            return json_response(200, {"runs": [{"run_id": "abc123"}]})
        return json_response(200, {"run_id": "abc123"})

    transport, recorder, _ = build(responder)

    observed = transport.run_preflight(make_binding(FakeSource()))

    assert [probe.name for probe, _ in observed] == ["workspace-runs-list", "run-detail"]
    assert recorder.requests[1].url.path == f"{RUNS_PATH}/abc123"


def test_no_run_detail_is_probed_when_the_list_returned_nothing() -> None:
    """Probing for an object the list did not name would be asking the gateway
    to confirm whether it exists."""
    transport, recorder, _ = build(ok_runs)

    observed = transport.run_preflight(make_binding(FakeSource()))

    assert [probe.name for probe, _ in observed] == ["workspace-runs-list"]
    assert len(recorder.requests) == 1


# --------------------------------------------------------------------------
# Secrecy, budgets and the browser boundary
# --------------------------------------------------------------------------


def test_the_key_appears_in_no_result_error_or_captured_fixture() -> None:
    def leaky(request: httpx2.Request) -> httpx2.Response:
        return json_response(
            403,
            {"detail": f"key {request.headers.get('x-api-key')} is not scoped to this workspace"},
        )

    transport, _, _ = build(leaky)
    binding = make_binding(FakeSource())

    result = transport.request("GET", RUNS_PATH, binding=binding)
    fixture = transport.capture_fixture(result, "denied")
    _, classified = classify_status(
        result.response.status_code,  # type: ignore[union-attr]
        detail=str(result.response.json_body),  # type: ignore[union-attr]
    )

    assert KEY not in json.dumps(fixture)
    assert KEY not in repr(result)
    assert classified is not None
    assert KEY not in classified.detail
    assert MARKER in json.dumps(result.response.json_body)  # type: ignore[union-attr]


def test_a_captured_fixture_records_a_shape_and_no_values() -> None:
    transport, _, _ = build(
        lambda _r: json_response(
            200, {"runs": [{"run_id": "abc", "cost_usd": 1.5}], "next_offset": None}
        )
    )

    fixture = transport.capture_fixture(
        transport.request("GET", RUNS_PATH, binding=make_binding(FakeSource())), "runs"
    )

    assert fixture["shape"] == {
        "object": {
            "next_offset": "null",
            "runs": {"array": {"object": {"cost_usd": "number", "run_id": "string"}}},
        }
    }
    assert "abc" not in json.dumps(fixture)
    assert "1.5" not in json.dumps(fixture)


def test_a_captured_fixture_keeps_header_names_without_their_values() -> None:
    transport, _, _ = build(
        lambda _r: json_response(
            200, {"runs": []}, {"date": "Thu, 11 Sep 2026", "x-request-id": "r"}
        )
    )

    fixture = transport.capture_fixture(
        transport.request("GET", RUNS_PATH, binding=make_binding(FakeSource())), "runs"
    )

    assert "date" in fixture["headers_present"]  # type: ignore[operator]
    assert "Thu, 11 Sep 2026" not in json.dumps(fixture)
    assert fixture["has_request_id"] is True


def test_the_ordinary_budget_follows_the_configured_remote_timeout() -> None:
    config = OfficeConfig(home=HOME, remote_timeout_s=7.0)

    ordinary = budget_for("ordinary", config)
    context = budget_for("context", config)
    write = budget_for("write", config)

    assert ordinary is not None
    assert ordinary.total == 7.0
    assert ordinary.connect == 1.0
    assert context is not None
    assert context.total == CONTEXT_TOTAL_S
    assert write is not None
    assert write.total == WRITE_TOTAL_S
    assert budget_for("whenever", config) is None


def test_the_configured_budget_reaches_the_request_that_is_actually_built() -> None:
    transport, recorder, _ = build(ok_runs, config=OfficeConfig(home=HOME, remote_timeout_s=4.0))

    transport.request("GET", RUNS_PATH, binding=make_binding(FakeSource()))

    timeout = recorder.requests[0].extensions["timeout"]
    assert timeout["read"] == 4.0
    assert timeout["connect"] == 1.0


def test_the_default_client_does_not_trust_the_ambient_environment() -> None:
    """An ambient ``HTTPS_PROXY`` would route a workspace key through whatever
    an operator's shell happens to name."""
    client = default_client()

    try:
        assert client.trust_env is False
        assert client.follow_redirects is False
    finally:
        client.close()


def test_a_transport_response_is_not_a_wire_model_the_server_could_serialise() -> None:
    """P15 never serialises this object; P13/P14 map it into a ServiceResult."""
    transport, _, _ = build(ok_runs)

    result = transport.request("GET", RUNS_PATH, binding=make_binding(FakeSource()))

    response = result.response
    assert response is not None
    assert not hasattr(response, "model_dump")
    assert not hasattr(response, "model_dump_json")
    with pytest.raises(TypeError):
        json.dumps(response)
