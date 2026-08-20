"""The CI transport: off costs nothing, and nothing that goes wrong is visible.

Every test here runs against a real socket (``tests/stub_ci_server``) rather
than a patched ``urlopen``, because the failures that matter are transport
behaviour and a mock only proves what the mock believes urllib does.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from aisquare.services import ci_cache, ci_client
from aisquare.services.ci_contract import (
    CONTRACT_HEADER,
    Action,
    DegradationReason,
    ToolRef,
    Trigger,
)
from tests.stub_ci_server import StubCI, serve

_SESSION = "ses_test"
_TRACE = "trc_test"
_PROJECT = "prj_test"


@pytest.fixture
def stub() -> Iterator[StubCI]:
    yield from serve()


@pytest.fixture
def wired(stub: StubCI, monkeypatch: pytest.MonkeyPatch) -> StubCI:
    """A stub endpoint with the client switched on and pointed at it."""
    monkeypatch.setenv(ci_client.ENABLED_ENV_VAR, "1")
    monkeypatch.setenv(ci_client.URL_ENV_VAR, stub.url)
    return stub


def _call(**kwargs: object) -> ci_client.Call:
    return ci_client.call(
        Trigger.prompt_submit,
        session_id=_SESSION,
        trace_id=_TRACE,
        project_id=_PROJECT,
        prompt="why does the brain lock use msvcrt",
        **kwargs,  # type: ignore[arg-type]
    )


# --- off costs nothing --------------------------------------------------------


def test_disabled_by_default_makes_no_request(
    stub: StubCI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The state every existing user is in. A URL alone must not switch it on."""
    monkeypatch.delenv(ci_client.ENABLED_ENV_VAR, raising=False)
    monkeypatch.setenv(ci_client.URL_ENV_VAR, stub.url)
    result = _call()
    assert result.reason is DegradationReason.disabled
    assert result.action is Action.allow
    assert stub.call_count == 0


def test_disabled_costs_no_measurable_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """``prompt_submit`` is synchronous in front of a developer who has just hit
    enter, so off has to mean zero latency, not a fast failure."""
    monkeypatch.delenv(ci_client.ENABLED_ENV_VAR, raising=False)
    assert _call().round_trip_ms == 0


def test_enabled_without_a_url_makes_no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ci_client.ENABLED_ENV_VAR, "1")
    monkeypatch.delenv(ci_client.URL_ENV_VAR, raising=False)
    result = _call()
    assert result.reason is DegradationReason.not_configured
    assert result.round_trip_ms == 0


def test_env_off_beats_config_on(stub: StubCI, monkeypatch: pytest.MonkeyPatch) -> None:
    """The state you want reachable in a hurry when it misbehaves."""
    monkeypatch.setenv(ci_client.URL_ENV_VAR, stub.url)
    monkeypatch.setenv(ci_client.ENABLED_ENV_VAR, "0")
    assert _call().reason is DegradationReason.disabled
    assert stub.call_count == 0


# --- the happy path -----------------------------------------------------------


def test_an_inject_response_comes_back_intact(wired: StubCI) -> None:
    wired.respond_json(
        {
            "contract": 1,
            "action": "inject",
            "context": "## Possibly relevant\n\n- brain.py",
            "server_ms": 42,
            "flags_applied": ["ci_retrieval"],
        }
    )
    result = _call()
    assert result.action is Action.inject
    assert result.reason is DegradationReason.none
    assert result.outcome.response.context is not None
    assert result.server_ms == 42


def _header(stub: StubCI, name: str) -> str | None:
    """One header, matched case-insensitively as HTTP defines them."""
    wanted = name.lower()
    return next(
        (value for key, value in stub.headers[0].items() if key.lower() == wanted),
        None,
    )


def test_the_request_carries_the_contract_header_and_bearer(
    wired: StubCI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ci_client.KEY_ENV_VAR, "sekrit")
    _call()
    assert _header(wired, CONTRACT_HEADER) == "1"
    assert _header(wired, "Authorization") == "Bearer sekrit"


def test_the_contract_header_arrives_case_normalised(wired: StubCI) -> None:
    """urllib title-cases header names, so ``X-CI-Contract`` goes out as
    ``X-Ci-Contract``. HTTP field names are case-insensitive and any correct
    server handles it, but a server matching the literal string would silently
    see no contract version and guess — so this is pinned rather than left to
    be rediscovered at integration. Documented in docs/ci-contract.md."""
    _call()
    sent = [key for key in wired.headers[0] if key.lower() == CONTRACT_HEADER.lower()]
    assert sent == ["X-Ci-Contract"]


def test_the_key_is_never_read_from_config(wired: StubCI, monkeypatch: pytest.MonkeyPatch) -> None:
    """config.toml is a file people paste into issues; a token there leaks by
    being ordinary."""
    monkeypatch.delenv(ci_client.KEY_ENV_VAR, raising=False)
    _call()
    assert _header(wired, "Authorization") is None


def test_the_payload_matches_the_contract(wired: StubCI) -> None:
    ci_client.call(
        Trigger.tool_intercept,
        session_id=_SESSION,
        trace_id=_TRACE,
        project_id=_PROJECT,
        tool=ToolRef(name="Grep", args={"pattern": "msvcrt"}),
        arm="B",
    )
    sent = wired.requests[0]
    assert sent["trigger"] == "tool_intercept"
    assert sent["tool"] == {"name": "Grep", "args": {"pattern": "msvcrt"}}
    assert sent["prompt"] is None
    assert sent["arm"] == "B"


def test_the_client_never_mints_a_run_id(wired: StubCI) -> None:
    """That format is the server's; a client generating its own forks the run
    space silently."""
    _call()
    assert wired.requests[0]["run_id"] is None


# --- every failure resolves to allow ------------------------------------------


def test_a_500_degrades_to_allow(wired: StubCI) -> None:
    wired.respond(status=500, body="internal error")
    result = _call()
    assert result.action is Action.allow
    assert result.reason is DegradationReason.http_error


def test_garbage_degrades_to_allow(wired: StubCI) -> None:
    wired.respond(status=200, body="<html>not json</html>")
    result = _call()
    assert result.action is Action.allow
    assert result.reason is DegradationReason.malformed_body


def test_a_contract_skew_degrades_to_allow(wired: StubCI) -> None:
    wired.respond_json({"contract": 99, "action": "inject", "context": "x"})
    assert _call().reason is DegradationReason.contract_mismatch


def test_a_refused_connection_degrades_to_allow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ci_client.ENABLED_ENV_VAR, "1")
    monkeypatch.setenv(ci_client.URL_ENV_VAR, "http://127.0.0.1:1")
    result = _call()
    assert result.action is Action.allow
    assert result.reason is DegradationReason.transport_error


def test_a_hang_degrades_to_allow_at_the_backstop(
    wired: StubCI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backstop is 10s in production; shortened here so the suite does not
    wait it out. What is under test is that it fires at all."""
    monkeypatch.setattr(ci_client, "CLIENT_BACKSTOP_SECONDS", 0.3)
    wired.respond(status=200, body='{"contract": 1, "action": "noop"}', delay_s=1.5)
    result = _call()
    assert result.action is Action.allow
    assert result.reason is DegradationReason.backstop_exceeded


def test_a_500_body_is_still_read(wired: StubCI) -> None:
    """A server that explains itself in the body of a 429 should not be thrown
    away as a bare status."""
    wired.respond(status=429, body='{"contract": 1, "action": "allow"}')
    assert _call().reason is DegradationReason.http_error


def test_nothing_the_server_does_raises(wired: StubCI) -> None:
    """One test for the property the whole hot path depends on."""
    for status, body in [
        (200, ""),
        (200, "null"),
        (200, "[]"),
        (200, '{"contract": 1}'),
        (200, '{"contract": 1, "action": "inject", "server_ms": "late"}'),
        (204, ""),
        (500, "boom"),
        (503, "<html/>"),
    ]:
        wired.respond(status=status, body=body)
        assert _call().action is Action.allow


# --- no retries ---------------------------------------------------------------


def test_a_failure_is_attempted_exactly_once(wired: StubCI) -> None:
    """A retry doubles the latency being measured — it makes a slow endpoint
    slower and contaminates the number the experiment exists to collect."""
    wired.respond(status=500, body="boom")
    _call()
    assert wired.call_count == 1


# --- timing -------------------------------------------------------------------


def test_network_cost_stays_separable_from_server_cost(wired: StubCI) -> None:
    """Folded together, a slow link is indistinguishable from a slow server."""
    wired.respond_json({"contract": 1, "action": "noop", "server_ms": 0})
    result = _call()
    assert result.network_ms is not None
    assert result.network_ms == result.round_trip_ms


def test_network_ms_is_none_when_the_server_reported_no_timing(wired: StubCI) -> None:
    wired.respond_json({"contract": 1, "action": "noop"})
    assert _call().network_ms is None


# --- the prefetch -------------------------------------------------------------


def test_a_warm_cache_is_read_without_a_request(wired: StubCI) -> None:
    """The prefetch's whole purpose: the common path costs a local file read,
    not a synchronous round trip in front of a waiting developer."""
    wired.respond_json(
        {
            "contract": 1,
            "action": "inject",
            "context": "bundle",
            "cache_hint": {"ttl_s": 900, "key": "K"},
        }
    )
    warm = ci_client.call(
        Trigger.session_start,
        session_id=_SESSION,
        trace_id=_TRACE,
        project_id=_PROJECT,
    )
    assert warm.cache_hit is False
    assert wired.call_count == 1

    hit = _call(cache_key="K")
    assert hit.cache_hit is True
    assert hit.action is Action.inject
    assert hit.outcome.response.context == "bundle"
    assert wired.call_count == 1, "a warm read must not reach the endpoint"


def test_a_cold_key_falls_through_to_a_request(wired: StubCI) -> None:
    result = _call(cache_key="never-written")
    assert result.cache_hit is False
    assert wired.call_count == 1


def test_a_degraded_response_is_never_cached(wired: StubCI) -> None:
    """Caching a failure would turn one bad minute into fifteen."""
    wired.respond_json(
        {"contract": 99, "action": "inject", "cache_hint": {"ttl_s": 900, "key": "K"}}
    )
    _call()
    assert ci_cache.read(_SESSION, "K") is None


def test_a_response_without_a_hint_is_not_cached(wired: StubCI) -> None:
    """What is cacheable stays a server-side decision."""
    wired.respond_json({"contract": 1, "action": "inject", "context": "x"})
    _call()
    assert ci_cache.read(_SESSION, "K") is None


def test_the_cached_body_round_trips_through_the_contract(wired: StubCI) -> None:
    """A hit must be parsed by the same ladder as a live response, so a cache
    written by an older build degrades rather than being trusted."""
    ci_cache.write(_SESSION, "K", json.dumps({"contract": 99, "action": "inject"}), 900)
    result = _call(cache_key="K")
    assert result.cache_hit is True
    assert result.reason is DegradationReason.contract_mismatch
    assert result.action is Action.allow
