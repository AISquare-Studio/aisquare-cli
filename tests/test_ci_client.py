"""The CI transport: off costs nothing, the deadline is wall-clock, nothing raises.

Every test here runs against a real socket (``tests/stub_ci_server``) rather
than a patched ``urlopen``, because the failures that matter — a stalled
connection, a body that dribbles in past the ceiling, an oversized body — are
transport behaviour, and a mock only proves what the mock believes urllib does.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from aisquare.core.config import AppConfig, ExperimentSettings, save_config
from aisquare.models import ClientReason
from aisquare.services import ci_client
from tests.ci_schemas import errors, fixture, fixture_text
from tests.ci_support import RUN, request, wire
from tests.stub_ci_server import StubCI, serve


@pytest.fixture
def stub() -> Iterator[StubCI]:
    yield from serve()


@pytest.fixture
def wired(stub: StubCI, monkeypatch: pytest.MonkeyPatch) -> StubCI:
    wire(monkeypatch, stub)
    return stub


def _call(stub: StubCI, **overrides: object) -> ci_client.Call:
    return ci_client.call(request(**overrides), url=stub.url + "/v1/hook")


# --- the switches ------------------------------------------------------------------


def test_off_is_the_default() -> None:
    assert ci_client.enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", " On "])
def test_the_recognised_on_values_enable(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(ci_client.ENABLED_ENV_VAR, value)
    assert ci_client.enabled() is True


@pytest.mark.parametrize(
    "value", ["0", "false", "off", "no", "disabled", "disable", "none", "garbage"]
)
def test_anything_else_is_off_even_when_config_says_on(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """The kill switch someone reaches for in a hurry must not fail open on a
    plausible spelling. ``disabled`` is exactly what a config field named
    ``enabled`` invites."""
    save_config(AppConfig(experiment=ExperimentSettings(enabled=True)))
    monkeypatch.setenv(ci_client.ENABLED_ENV_VAR, value)
    assert ci_client.enabled() is False


def test_an_empty_variable_defers_to_config(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_config(AppConfig(experiment=ExperimentSettings(enabled=True)))
    monkeypatch.setenv(ci_client.ENABLED_ENV_VAR, "")
    assert ci_client.enabled() is True


def test_a_broken_config_enables_nothing(isolated_home: Path) -> None:
    from aisquare.core import paths

    paths.ensure_home()
    paths.config_path().write_text("this is not = toml [", encoding="utf-8")
    assert ci_client.enabled() is False
    assert ci_client.endpoint() == ""


def test_off_reads_no_config_and_costs_no_measurable_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """A config read on the off path would be paid by every prompt of every
    user who never opted in. Proven by making the read explode, and bounded by
    a generous wall clock rather than a literal."""
    monkeypatch.setenv(ci_client.ENABLED_ENV_VAR, "0")
    monkeypatch.setattr(
        ci_client, "load_config", lambda: (_ for _ in ()).throw(RuntimeError("read"))
    )
    started = time.perf_counter()
    for _ in range(200):
        assert ci_client.enabled() is False
    assert time.perf_counter() - started < 0.2


@pytest.mark.parametrize(
    "url", ["example.com", "example.com/v1", "ftp://x", "file:///etc/passwd", ""]
)
def test_a_url_without_an_http_scheme_is_not_an_endpoint(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    """A scheme-less URL used to raise out of the request constructor, past every
    reason the ladder knows, and cost the hook its whole output."""
    monkeypatch.setenv(ci_client.URL_ENV_VAR, url)
    assert ci_client.endpoint() == ""
    assert ci_client.raw_endpoint() == url.strip()


def test_the_endpoint_keeps_its_scheme_and_drops_the_trailing_slash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ci_client.URL_ENV_VAR, "HTTPS://ci.example/base/")
    assert ci_client.endpoint() == "HTTPS://ci.example/base"


def test_the_environment_url_beats_the_config_url(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_config(AppConfig(experiment=ExperimentSettings(url="http://from-config")))
    assert ci_client.endpoint() == "http://from-config"
    monkeypatch.setenv(ci_client.URL_ENV_VAR, "http://from-env")
    assert ci_client.endpoint() == "http://from-env"


@pytest.mark.parametrize("value", ["run_kernel0001", "run_A-b_9"])
def test_a_well_formed_run_id_is_accepted(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(ci_client.RUN_ENV_VAR, value)
    assert ci_client.run_id() == value


@pytest.mark.parametrize(
    "value", ["kernel0001", "run_", "run_ has space", "ws_kernel01", "run_" + "x" * 70]
)
def test_a_malformed_run_id_is_no_run(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """The CLI never mints or repairs a run id; a value the contract rejects is
    ``no_run`` here rather than a request the server rejects on shape."""
    monkeypatch.setenv(ci_client.RUN_ENV_VAR, value)
    assert ci_client.run_id() == ""
    assert ci_client.raw_run_id() == value


def test_the_run_id_falls_back_to_config(isolated_home: Path) -> None:
    save_config(AppConfig(experiment=ExperimentSettings(run=RUN)))
    assert ci_client.run_id() == RUN


def test_the_key_comes_from_the_environment_only(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert ci_client.api_key() == ""
    monkeypatch.setenv(ci_client.KEY_ENV_VAR, " sekrit ")
    assert ci_client.api_key() == "sekrit"
    assert "key" not in ExperimentSettings.model_fields


# --- the request on the wire ----------------------------------------------------------


def _header(stub: StubCI, name: str) -> str | None:
    wanted = name.lower()
    return next((v for k, v in stub.headers[0].items() if k.lower() == wanted), None)


def test_the_payload_validates_against_the_servers_schema(wired: StubCI) -> None:
    _call(wired)
    assert errors("hook-request.experimental-v2", wired.requests[0]) == []


def test_the_bearer_travels_and_the_v1_header_does_not(
    wired: StubCI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ci_client.KEY_ENV_VAR, "sekrit")
    _call(wired)
    assert _header(wired, "Authorization") == "Bearer sekrit"
    assert _header(wired, "Content-Type") == "application/json"
    assert _header(wired, "X-CI-Contract") is None, "v2 carries the contract in the body"
    assert wired.requests[0]["contract"] == 2


def test_no_key_means_no_authorization_header(
    wired: StubCI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ci_client.KEY_ENV_VAR, raising=False)
    _call(wired)
    assert _header(wired, "Authorization") is None


def test_the_request_hits_the_path_it_was_given(wired: StubCI) -> None:
    ci_client.call(request(), url=wired.url + "/custom/hook")
    assert wired.hooks[0].path == "/custom/hook"


# --- the happy path ------------------------------------------------------------------


def test_a_served_response_comes_back_intact(wired: StubCI) -> None:
    result = _call(wired)
    assert result.reason is ClientReason.none
    assert result.action == "inject"
    assert result.status == "served"
    assert result.briefing is not None and len(result.briefing.items) == 1
    assert result.server_ms == 63
    assert result.deadline_breached is False
    assert result.error_codes == []


def test_network_cost_stays_separable_from_server_cost(wired: StubCI) -> None:
    """Folded together, a slow link is indistinguishable from a slow server."""
    result = _call(wired)
    assert result.network_ms is not None
    assert result.network_ms == result.round_trip_ms - 63


def test_a_degraded_response_records_its_error_codes(wired: StubCI) -> None:
    raw = fixture("hook-response.experimental-v2.valid")
    raw.update(status="degraded", errors=[fixture("error.v1.valid")])
    wired.respond_json(raw)
    result = _call(wired)
    assert result.status == "degraded"
    assert result.action == "inject"
    assert result.error_codes == ["trace_batch_span_mismatch"]


# --- every failure resolves to noop with a reason -------------------------------------


def test_a_500_is_an_http_error(wired: StubCI) -> None:
    wired.respond(status=500, body="internal error")
    result = _call(wired)
    assert result.action == "noop"
    assert result.reason is ClientReason.http_error
    assert "500" in result.outcome.detail


def test_a_429_body_is_still_read_and_still_an_http_error(wired: StubCI) -> None:
    wired.respond(status=429, body=fixture_text("hook-response.experimental-v2.valid"))
    assert _call(wired).reason is ClientReason.http_error


def test_garbage_is_malformed(wired: StubCI) -> None:
    wired.respond(status=200, body="<html>not json</html>")
    assert _call(wired).reason is ClientReason.malformed_body


def test_a_contract_skew_is_a_mismatch(wired: StubCI) -> None:
    body = fixture_text("hook-response.experimental-v2.valid").replace(
        '"contract": 2', '"contract": 1', 1
    )
    wired.respond(status=200, body=body)
    assert _call(wired).reason is ClientReason.contract_mismatch


def test_a_refused_connection_is_a_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    result = ci_client.call(request(), url="http://127.0.0.1:1/v1/hook")
    assert result.action == "noop"
    assert result.reason is ClientReason.transport_error


def test_a_stalled_server_is_cut_off_at_the_ceiling(wired: StubCI) -> None:
    wired.respond(status=200, body=fixture_text("hook-response.experimental-v2.valid"), delay_s=2.0)
    started = time.monotonic()
    result = _call(wired, client_safety_ms=300)
    elapsed = time.monotonic() - started
    assert result.reason is ClientReason.deadline_exceeded
    assert result.deadline_breached is True
    assert elapsed < 1.2, f"the hook waited {elapsed:.2f}s for a 0.3s ceiling"


def test_a_dripping_body_cannot_hold_the_hook_past_the_ceiling(wired: StubCI) -> None:
    """The per-socket-operation timeout resets on every read; a server sending a
    chunk every half-ceiling never trips it and holds the hook indefinitely.
    The wall clock does not care how the bytes arrive."""
    ceiling_ms = 600
    wired.respond(
        status=200,
        body=fixture_text("hook-response.experimental-v2.valid"),
        drip=(8, ceiling_ms / 1000 * 0.5),
    )
    started = time.monotonic()
    result = _call(wired, client_safety_ms=ceiling_ms)
    elapsed_ms = (time.monotonic() - started) * 1000
    assert result.reason is ClientReason.deadline_exceeded
    assert elapsed_ms < ceiling_ms + 600, f"returned after {elapsed_ms:.0f} ms"
    assert wired.call_count == 1


def test_a_response_that_arrives_after_the_ceiling_is_still_a_breach(
    wired: StubCI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The late-arrival branch, taken deterministically: the exchange completes,
    the clock says the ceiling passed, and counting it would hide exactly the
    latency being measured."""
    monkeypatch.setattr(ci_client, "late", lambda elapsed_ms, deadline_ms: True)
    result = _call(wired)
    assert result.reason is ClientReason.deadline_exceeded
    assert "after the" in result.outcome.detail
    assert wired.call_count == 1


def test_late_is_at_or_past_the_ceiling() -> None:
    assert ci_client.late(600, 600)
    assert ci_client.late(601, 600)
    assert not ci_client.late(599, 600)


def test_an_oversized_body_is_malformed_not_read(
    wired: StubCI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ci_client, "MAX_BODY_BYTES", 1024)
    wired.respond(status=200, body=fixture_text("hook-response.experimental-v2.valid") + " " * 4096)
    result = _call(wired)
    assert result.reason is ClientReason.malformed_body
    assert "exceeds" in result.outcome.detail


def test_nothing_the_server_does_raises(wired: StubCI) -> None:
    """One test for the property the whole hot path depends on."""
    for status, body in [
        (200, ""),
        (200, "null"),
        (200, "[]"),
        (200, "{}"),
        (200, '{"contract": 2}'),
        (200, '{"contract": 2, "action": "block"}'),
        (200, "[" * 20_000 + "]" * 20_000),
        (204, ""),
        (500, "boom"),
        (503, "<html/>"),
    ]:
        wired.respond(status=status, body=body)
        result = _call(wired)
        assert result.action == "noop", (status, body[:40])
        assert result.degraded


# --- no retries ------------------------------------------------------------------------


def test_a_failure_is_attempted_exactly_once(wired: StubCI) -> None:
    """A retry doubles the latency being measured — it makes a slow endpoint
    slower and contaminates the number the experiment exists to collect."""
    wired.respond(status=500, body="boom")
    _call(wired)
    assert wired.call_count == 1


def test_a_deadline_breach_is_not_retried_either(wired: StubCI) -> None:
    wired.respond(status=200, body="{}", delay_s=1.0)
    _call(wired, client_safety_ms=200)
    time.sleep(1.2)  # let the abandoned worker's request land on the stub
    assert wired.call_count == 1


# --- the generic exchange -------------------------------------------------------------


def test_exchange_reports_any_status_that_arrives(wired: StubCI) -> None:
    result = ci_client.exchange(wired.url + "/ready", method="GET", deadline_ms=2_000)
    assert result.reason is None
    assert result.status == 200
    assert json.loads(result.body) == {"status": "ready"}


def test_exchange_never_raises_on_a_bad_url() -> None:
    result = ci_client.exchange("nonsense://x", method="GET", deadline_ms=500)
    assert result.reason is ClientReason.transport_error


def test_deadline_breached_is_unknown_when_nothing_arrived_for_another_reason() -> None:
    result = ci_client.call(request(), url="http://127.0.0.1:1/v1/hook")
    assert result.deadline_breached is None
