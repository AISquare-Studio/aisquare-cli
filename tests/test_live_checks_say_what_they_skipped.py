"""A row that vanishes reads exactly like a row that passed.

`doctor --live` gates the ingest probe behind `/ready`, which is correct — you
must not POST a span to a URL that just 404'd. But when `/ready` fails the
ingest row is not reported as skipped, it is ABSENT. @8dd460fb measured it:

    ✗ explainability gateway: …/ready — HTTP 404: {"detail":"no route"}
    …and no ingest row at all.

So an operator with a red gateway row cannot tell whether ingest is also broken
or merely unasked, which is whether fixing the URL is the whole job. Same shape
as §3's "a payload proves a proxy answered, not whose": a check whose silence is
indistinguishable from a pass it never attempted.

THE SIBLING SWEEP, because fixing only the reported row would leave the class
open. `_live_checks` has THREE early returns and each drops everything after it:

  * `not target.configured`  -> drops ingest, governance AND the sdk rows
  * `/ready` fails           -> drops ingest, governance AND the sdk rows  <- reported
  * identity renders none    -> already reports ingest as skipped; drops the sdk rows

`_sdk_checks()` calls the SDK's own doctor — a LOCAL lookup with no gateway in
it — so it is gateway-independent and was disappearing on gateway failures.
Governance is different and is deliberately left gated: it is a statement about
a trace that landed, so with no accepted span there is nothing to say about it.

The marker follows the convention this same function already uses twice: a
`warn` whose detail opens `skipped —`. That is visibly distinct from `✓` and
from `✗`, distinct from ok/fail in `--json`, and does not move the exit code,
which is right — an unasked question is not a failure.
"""

from __future__ import annotations

from typing import Any

import pytest

from aisquare.models import CheckStatus, DoctorCheck
from aisquare.services import explainability_ops as ops
from tests.test_explainability_ops import _ACCEPTED, _PROXY_HEALTHY, _READY, _gateway

_NO_ROUTE = (404, {"detail": "no route"})


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
        roles=("planner",),
    )


def _rows(ready: tuple[int, dict[str, Any]]) -> list[DoctorCheck]:
    server, url, _ = _gateway(
        {"/ready": ready, "/v1/traces/ingest": _ACCEPTED, "/health": _PROXY_HEALTHY}
    )
    try:
        return ops._live_checks(_target(url), on=True)
    finally:
        server.shutdown()


def test_a_failing_gateway_still_reports_an_ingest_row() -> None:
    """The defect: the row was absent, not skipped."""
    rows = _rows(_NO_ROUTE)

    assert any(r.name == "explainability ingest" for r in rows), (
        f"ingest vanished; rows were {[r.name for r in rows]}"
    )


def test_the_skipped_ingest_row_is_neither_a_pass_nor_a_failure() -> None:
    """A skipped check rendered as a tick is worse than the omission."""
    row = next(r for r in _rows(_NO_ROUTE) if r.name == "explainability ingest")

    assert row.status is CheckStatus.warn, row
    assert row.detail.startswith("skipped"), row.detail


def test_the_skipped_row_names_the_gateway_as_the_cause() -> None:
    """ "skipped" without a reason moves the question rather than answering it."""
    row = next(r for r in _rows(_NO_ROUTE) if r.name == "explainability ingest")

    assert "gateway" in row.detail, row.detail
    assert row.fix, "a skipped row with no next step is a dead end"


def test_the_local_sdk_rows_survive_a_gateway_failure() -> None:
    """The sibling. `_sdk_checks` asks the SDK's own doctor — no gateway in it.

    They were dropped by the same early return, so a red gateway row also
    silently removed every `sdk:*` row from the output.
    """
    healthy = [r.name for r in _rows(_READY)]
    broken = [r.name for r in _rows(_NO_ROUTE)]

    sdk_when_healthy = [n for n in healthy if n.startswith("sdk:")]
    sdk_when_broken = [n for n in broken if n.startswith("sdk:")]
    if not sdk_when_healthy:
        pytest.skip("no SDK installed in this environment; nothing to compare")

    assert sdk_when_broken == sdk_when_healthy, (
        f"a gateway failure dropped local SDK rows: {sdk_when_healthy} -> {sdk_when_broken}"
    )


def test_a_healthy_gateway_is_unchanged() -> None:
    """The control: the green path must not acquire a skipped row."""
    rows = _rows(_READY)
    ingest = next(r for r in rows if r.name == "explainability ingest")

    assert ingest.status is CheckStatus.ok, ingest.detail
    assert not any(r.detail.startswith("skipped") for r in rows), [
        r.detail for r in rows if r.detail.startswith("skipped")
    ]


def test_a_gateway_failure_costs_exactly_one_row_and_it_is_named() -> None:
    """The denominator, pinned by NAME because there is no summary line to pin.

    The contract asked for the summary arithmetic — "21 ok, 6 warn, 0 fail" —
    to account for the skipped row. Measured: the CLI emits NO summary. `doctor`
    ends with its last check row, and that tally was a reader counting rows by
    hand. So the denominator claim has to be made about the ROW SET itself.

    A failing gateway should now cost exactly one row — `governance`, which is
    deliberately still gated because it is a statement about a trace that
    landed and there is none. Everything else must survive, named.
    """
    healthy = {r.name for r in _rows(_READY)}
    broken = {r.name for r in _rows(_NO_ROUTE)}

    assert healthy - broken == {"explainability governance"}, (
        f"a gateway failure silently removed {sorted(healthy - broken)}"
    )
    assert broken - healthy == set(), f"it also invented rows: {sorted(broken - healthy)}"
