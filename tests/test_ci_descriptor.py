"""The delivery descriptor: fetched once, cached until it expires, every refusal named."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aisquare.core import paths
from aisquare.models import ClientReason
from aisquare.services import ci_descriptor
from tests.ci_schemas import fixture
from tests.ci_support import RUN
from tests.stub_ci_server import StubCI, error_v1, live_descriptor, serve


@pytest.fixture
def stub() -> Iterator[StubCI]:
    yield from serve()


def _current(stub: StubCI, run: str = RUN) -> ci_descriptor.DescriptorResult:
    return ci_descriptor.current(run, base=stub.url, key="k")


def test_a_descriptor_is_fetched_and_understood(stub: StubCI, isolated_home: Path) -> None:
    result = _current(stub)
    assert result.reason is ClientReason.none
    assert result.descriptor is not None
    assert result.descriptor.run_id == RUN
    assert result.descriptor.pushes("prompt_submit")
    assert result.from_cache is False


def test_the_fetch_carries_the_bearer_and_hits_the_runs_route(
    stub: StubCI, isolated_home: Path
) -> None:
    _current(stub)
    fetch = next(r for r in stub.seen if r.method == "GET")
    assert fetch.path == f"/v1/experiment/runs/{RUN}"
    assert fetch.headers.get("Authorization") == "Bearer k"


def test_a_fetched_descriptor_is_cached_and_reused_without_a_second_request(
    stub: StubCI, isolated_home: Path
) -> None:
    first = _current(stub)
    second = _current(stub)
    assert second.from_cache is True
    assert second.descriptor == first.descriptor
    assert stub.descriptor_fetches == 1
    assert paths.ci_descriptor_path(RUN).exists()


def test_an_expired_descriptor_is_refused_and_not_cached(stub: StubCI, isolated_home: Path) -> None:
    """The vendored fixture's own expiry has passed — the client must say so
    rather than drive a run whose descriptor the controller retired."""
    stub.descriptor_json(fixture("client-delivery-descriptor.v1.valid"))
    result = _current(stub)
    assert result.descriptor is None
    assert result.reason is ClientReason.descriptor_unavailable
    assert "expired" in result.detail
    assert not paths.ci_descriptor_path(RUN).exists()


def test_an_expired_cache_entry_is_refetched(stub: StubCI, isolated_home: Path) -> None:
    _current(stub)
    later = datetime(2100, 1, 1, tzinfo=UTC)
    result = ci_descriptor.current(RUN, base=stub.url, key="k", now=later)
    assert result.descriptor is None  # the refetched one is also expired at that clock
    assert stub.descriptor_fetches == 2


@pytest.mark.parametrize(
    ("status", "phrase"),
    [(401, "token rejected"), (403, "not allowed"), (404, "run not found"), (500, "http 500")],
)
def test_each_http_refusal_has_its_own_detail(
    stub: StubCI, isolated_home: Path, status: int, phrase: str
) -> None:
    stub.descriptor_json({"error": "no"}, status=status)
    result = _current(stub)
    assert result.descriptor is None
    assert phrase in result.detail
    assert result.reason is ClientReason.descriptor_unavailable


def test_a_refusal_with_an_error_body_quotes_the_servers_code_and_sentence(
    stub: StubCI, isolated_home: Path
) -> None:
    """Live, an unauthenticated descriptor fetch answers a proper error.v1 401;
    the line a person reads should carry the server's own reason, and the
    status should travel separately so ``doctor`` can pick its fix from it."""
    stub.descriptor_json(
        error_v1("scope_resolution_failed", 401, "no valid experiment token."), status=401
    )
    result = _current(stub)
    assert result.descriptor is None and result.status == 401
    assert result.detail == (
        "token rejected (401) — scope_resolution_failed: no valid experiment token."
    )


def test_a_refusal_without_an_error_body_keeps_the_bare_status(
    stub: StubCI, isolated_home: Path
) -> None:
    stub.descriptor_json({"error": "no"}, status=503)
    result = _current(stub)
    assert result.detail == "http 503" and result.status == 503


def test_a_contract_skew_is_named_before_the_shape_is_checked(
    stub: StubCI, isolated_home: Path
) -> None:
    stub.descriptor_json(live_descriptor(contract_version=3))
    result = _current(stub)
    assert result.descriptor is None
    assert "contract_version" in result.detail and "3" in result.detail


def test_a_blinding_leak_in_the_descriptor_is_refused(stub: StubCI, isolated_home: Path) -> None:
    stub.descriptor_json(live_descriptor(arm_kind="architecture_candidate"))
    result = _current(stub)
    assert result.descriptor is None
    assert "descriptor:" in result.detail


def test_a_descriptor_for_another_run_is_refused(stub: StubCI, isolated_home: Path) -> None:
    stub.descriptor_json(live_descriptor(run_id="run_other"))
    result = _current(stub)
    assert result.descriptor is None
    assert "names run_other" in result.detail


def test_a_body_that_is_not_json_is_refused(stub: StubCI, isolated_home: Path) -> None:
    stub.descriptor_status = 200
    stub.descriptor_body = "<html/>"
    assert "not JSON" in _current(stub).detail


def test_an_unreachable_server_is_a_named_failure_not_an_exception(isolated_home: Path) -> None:
    result = ci_descriptor.current(RUN, base="http://127.0.0.1:1", key="k")
    assert result.descriptor is None
    assert result.detail.startswith(("transport_error", "deadline_exceeded"))


def test_doctor_style_fetch_leaves_no_cache_behind(stub: StubCI, isolated_home: Path) -> None:
    result = ci_descriptor.fetch(RUN, base=stub.url, key="k", cache=False)
    assert result.descriptor is not None
    assert not paths.ci_descriptor_path(RUN).exists()


def test_a_corrupt_cache_file_is_refetched(stub: StubCI, isolated_home: Path) -> None:
    target = paths.ci_descriptor_path(RUN)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{not json", encoding="utf-8")
    result = _current(stub)
    assert result.descriptor is not None
    assert result.from_cache is False
    assert json.loads(target.read_text(encoding="utf-8"))["run_id"] == RUN


def test_a_cache_entry_naming_another_run_is_ignored(stub: StubCI, isolated_home: Path) -> None:
    target = paths.ci_descriptor_path(RUN)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(live_descriptor(run_id="run_other")), encoding="utf-8")
    result = _current(stub)
    assert result.from_cache is False
    assert stub.descriptor_fetches == 1


def test_forget_drops_the_cache(stub: StubCI, isolated_home: Path) -> None:
    _current(stub)
    ci_descriptor.forget(RUN)
    assert not paths.ci_descriptor_path(RUN).exists()
    ci_descriptor.forget(RUN)  # absent is fine
    assert stub.descriptor_fetches == 1


def test_the_cache_path_stays_inside_the_cache_directory() -> None:
    for hostile in ("../../escape", "run_/x", "a:b", ""):
        assert paths.ci_descriptor_path(hostile).resolve().parent == paths.ci_cache_dir().resolve()


def test_parse_descriptor_reports_the_first_shape_error() -> None:
    raw = live_descriptor(client_safety_ms=0)
    descriptor, detail = ci_descriptor.parse_descriptor(json.dumps(raw))
    assert descriptor is None
    assert "client_safety_ms" in detail
