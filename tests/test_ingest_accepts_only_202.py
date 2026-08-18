"""`ingest` says "test span accepted" for any 2xx, and the contract is 202.

§5 calls this row "the line that matters" — "the only one that proves the key,
the gateway and the identity all work together" — and `probe_ingest`'s own
docstring says "a 202 means the key was accepted AND the identity routed AND
ingest is healthy". But the verdict is built by `_request`, which decides
`ok = 200 <= status < 300` for every gateway call, because `/ready` legitimately
answers 200.

So a 200 from something that is not the ingest endpoint — a reverse proxy, an
auth portal, an API gateway's default route, a version skew where the path
moved — renders as `✓ explainability ingest: test span accepted as
'aisquare-planner' (HTTP 200)`. Measured before this file existed. The status is
printed, so an attentive reader could notice, but the word the row commits to is
"accepted", and that is the conclusion an operator carries away from the one
check that is supposed to prove the whole path.

This does NOT change `/ready`, which is a plain 200 and correct as such. It
narrows the one probe whose meaning is tied to a specific code.

Same shape as the `status` field this file's author added to `probe_proxy`
earlier tonight: a check that inspected `service` and `mode` and discarded the
field whose entire job was reporting health. A 2xx is not an acceptance any more
than a 200 with `{"status":"degraded"}` is a healthy proxy.
"""

from __future__ import annotations

import pytest

from aisquare.models import CheckStatus, DoctorCheck
from aisquare.services import explainability_ops as ops
from tests.test_explainability_ops import _PROXY_HEALTHY, _READY, _gateway


def _target(url: str) -> ops.ResolvedTarget:
    return ops.ResolvedTarget(
        name="stg",
        gateway_url=url,
        gateway_source="config",
        api_key_env="EXPLAINABILITY_API_KEY",
        api_key="k",
        proxy_url=url,
        proxy_source="config",
        agent_name_template="aisquare-{role}",
        studio_id="21",
        roles=("planner", "coder", "runner"),
    )


def _ingest_row(status: int, body: dict[str, object]) -> DoctorCheck:
    server, url, _ = _gateway(
        {"/ready": _READY, "/v1/traces/ingest": (status, body), "/health": _PROXY_HEALTHY}
    )
    try:
        rows = ops._live_checks(_target(url), on=True)
    finally:
        server.shutdown()
    row = next((r for r in rows if r.name == "explainability ingest"), None)
    assert row is not None, "the ingest row vanished; the probe was never reached"
    return row


def test_202_is_accepted() -> None:
    """The control, first: the contract status must still read green.

    Without this a fix that rejected everything would satisfy every assertion
    below, and "ingest never passes" is a worse defect than the one being fixed.
    """
    row = _ingest_row(202, {"status": "accepted", "trace_id": "t", "span_count": 1})

    assert row.status is CheckStatus.ok, row.detail
    assert "accepted" in row.detail


def test_a_200_is_not_an_acceptance() -> None:
    """The defect. A 2xx that is not 202 must not say "accepted"."""
    row = _ingest_row(200, {"status": "ok"})

    assert row.status is not CheckStatus.ok, f"a plain 200 rendered as an acceptance: {row.detail}"
    assert "200" in row.detail, row.detail


@pytest.mark.parametrize(
    ("status", "expect_key_language"),
    [(401, True), (403, True), (404, False), (422, False), (500, False)],
)
def test_refusals_stay_diagnosable(status: int, expect_key_language: bool) -> None:
    """The rows an operator is most likely to meet, pinned as they read today.

    401 and 403 already say "the gateway rejected the key", which is the
    sentence that sends someone to their key rather than to their span. The
    others carry the code and the body. Pinned so narrowing 2xx does not
    quietly reword the failure side.
    """
    row = _ingest_row(status, {"detail": "nope"})

    assert row.status is CheckStatus.fail, row.detail
    assert str(status) in row.detail
    assert ("rejected the key" in row.detail) is expect_key_language, row.detail
