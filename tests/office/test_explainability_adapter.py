"""The Explainability adapter's assertion matrix.

Every test drives the real :class:`HttpExplainabilityClient` through a fake
transport that behaves the way P12's does in the two respects this adapter
depends on: it preserves the HTTP status on the response rather than collapsing
it, and it drops its freshness record when the platform answers 401 or 403.

**No live call is made by this file, and none was made to write it.** The
bodies are the redacted shapes recorded in the Office repository's platform
fixture inventory — keys, types and nullability from source-mined evidence, and
every value invented.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest

from aisquare.office.adapters.explainability import (
    NOT_FOUND_OR_MASKED,
    HttpExplainabilityClient,
    compare_costs,
    decode_cursor,
    encode_cursor,
    merge_summaries,
    not_found_meaning,
    policy_status_of,
    run_state_of,
    run_summary_of,
    status_for_error,
)
from aisquare.office.models import (
    PlatformBinding,
    PlatformQuery,
    ServiceError,
    TransportResponse,
    TransportResult,
)

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
WORKSPACE = "1042"
RUN = "b7c1e4a9f03d4e2ab8915c6d7e0f2a31"
OTHER_RUN = "3f9a2d5c81b64e77a0c3e918d24b5e60"

PROMPT_TEXT = "[synthetic prompt text that must never leave the adapter]"
BLOCKED_OUTPUT = "[synthetic model output that a gate rewrote]"


class FakeClock:
    def __init__(self, now: datetime = NOW) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return 0.0


class FakeTransport:
    """P12's transport, reduced to the behaviour this adapter relies on.

    Two of those behaviours are load-bearing and are reproduced exactly rather
    than simplified: an error status still arrives as a *response* carrying its
    status code, and a 401 or 403 drops the freshness record so a revoked
    binding cannot keep painting previously authorised data.
    """

    def __init__(self, *answers: tuple[int, object]) -> None:
        self.answers = list(answers)
        self.requests: list[tuple[str, str, PlatformQuery | None]] = []
        self.invalidated: list[str | None] = []
        self._last_success: dict[tuple[str, int], datetime] = {}
        self.received_at = NOW

    def request(
        self,
        method: str,
        path: str,
        *,
        query: PlatformQuery | None = None,
        body: Mapping[str, object] | None = None,
        binding: PlatformBinding,
        timeout_class: str = "ordinary",
    ) -> TransportResult:
        self.requests.append((method, path, query))
        if not self.answers:
            raise AssertionError(f"the adapter asked for {path} and no answer was queued")
        status, payload = self.answers.pop(0)
        if status == 0:  # a transport-level failure: no response at all
            assert isinstance(payload, ServiceError)
            if payload.code in ("unauthorized", "forbidden"):
                self._last_success.pop((binding.binding_id, binding.revision), None)
            return TransportResult(error=payload)
        if status in (401, 403):
            self._last_success.pop((binding.binding_id, binding.revision), None)
        elif 200 <= status < 300:
            self._last_success[(binding.binding_id, binding.revision)] = self.received_at
        return TransportResult(
            response=TransportResponse(
                status_code=status,
                allowed_headers={"content-type": "application/json"},
                json_body=payload,
                received_at=self.received_at,
            )
        )

    def last_success_at(self, binding: PlatformBinding) -> datetime | None:
        return self._last_success.get((binding.binding_id, binding.revision))

    def invalidate(self, *, binding_id: str | None = None, revision: int | None = None) -> int:
        self.invalidated.append(binding_id)
        return 0


def make_binding(*, revision: int = 7, studio: str | None = None) -> PlatformBinding:
    return PlatformBinding(
        binding_id="pb_synthetic",
        revision=revision,
        project_id="prj_office",
        profile_name="stg",
        workspace_id=WORKSPACE,
        studio_id=studio,
    )


def run_row(
    run_id: str = RUN,
    *,
    status: str = "ready",
    verdict: str | None = "completed",
    cost: float | None = 1.8734,
    duration_ms: float | None = 462353.0,
    updated_at: str = "2026-09-10T14:09:54.002000+00:00",
    parent: str | None = None,
    studio: str | None = "482",
) -> dict[str, Any]:
    """One ``WorkspaceRunItem``, with the nullable columns kept nullable."""
    return {
        "studio_id": studio,
        "run_id": run_id,
        "status": status,
        "run_verdict": verdict,
        "agent_name": "aisquare-coder",
        "parent_run_id": parent,
        "run_kind": "one_shot",
        "session_id": None,
        "started_at": "2026-09-10T14:02:11.418000+00:00",
        "ended_at": "2026-09-10T14:09:53.771000+00:00" if verdict else None,
        "updated_at": updated_at,
        "duration_ms": duration_ms,
        "node_count": 148,
        "token_count": 91422,
        "cost_usd": cost,
        "error_count": 0,
        "flagged_count": 0,
        "policy_passed_count": 12,
        "policy_unverified_count": 0,
        "policy_needs_review_count": 0,
        "runtime_decision_count": 0,
        "is_governed": True,
        "last_error": None,
        "summary_counts": {"spans": 148, "events": 22, "artifacts": 6, "policies": 43},
    }


def runs_body(
    *rows: dict[str, Any],
    studios_read: list[str] | None = None,
    studios_failed: list[str] | None = None,
    studios_omitted: int = 0,
    total_is_exact: bool = True,
    next_offset: int | None = None,
    total_count: int = 213,
) -> dict[str, Any]:
    return {
        "status": "ok",
        "workspace_id": WORKSPACE,
        "since": None,
        "total_count": total_count,
        "studios_read": ["482", "517"] if studios_read is None else studios_read,
        "studios_failed": [] if studios_failed is None else studios_failed,
        "studios_omitted": studios_omitted,
        "total_is_exact": total_is_exact,
        "page_limit": 1000,
        "runs_reachable": 108,
        "next_offset": next_offset,
        "runs": list(rows),
    }


def detail_body(row: dict[str, Any], *, poll_after_ms: int | None = None) -> dict[str, Any]:
    """The detail envelope, including the two prompt-bearing fields."""
    run = dict(row)
    run.pop("studio_id", None)
    run["graph_available"] = True
    run["root_input"] = {"text": PROMPT_TEXT}
    run["root_output"] = {"text": PROMPT_TEXT}
    run["root_output_missing_reason"] = None
    return {
        "status": run["status"],
        "updated_at": run["updated_at"],
        "poll_after_ms": poll_after_ms,
        "run": run,
    }


def client(*answers: tuple[int, object]) -> tuple[HttpExplainabilityClient, FakeTransport]:
    transport = FakeTransport(*answers)
    return (
        HttpExplainabilityClient(transport=transport, clock=FakeClock()),
        transport,
    )


# --------------------------------------------------------------------------
# The list
# --------------------------------------------------------------------------


def test_a_two_hundred_list_is_a_typed_page_with_its_coverage_preserved() -> None:
    adapter, _ = client((200, runs_body(run_row(), run_row(OTHER_RUN, status="processing"))))

    reading = adapter.read_runs(make_binding(), PlatformQuery(limit=10))
    page = reading.result.data

    assert reading.result.status == "ok"
    assert page is not None
    assert [run.run_id for run in page.items] == [RUN, OTHER_RUN]
    assert page.coverage is not None
    assert page.coverage.reported_total == 213
    assert page.coverage.reachable_scope_count == 2
    assert page.coverage.failed_scope_count == 0
    assert page.coverage.total_is_exact is True
    assert reading.page_limit == 1000
    assert reading.runs_reachable == 108


def test_an_empty_workspace_is_a_successful_read_and_never_an_outage() -> None:
    """A workspace owning no studios answers 200 with nothing in it."""
    adapter, _ = client((200, runs_body(studios_read=[], total_count=0)))

    result = adapter.list_runs(make_binding(), PlatformQuery())

    assert result.status == "ok"
    assert result.data is not None
    assert result.data.items == ()
    assert result.data.partial is False
    assert result.error is None


def test_a_failed_studio_is_a_partial_success_that_still_carries_its_runs() -> None:
    """The runs that were read are the whole point: dropping them would turn
    one studio's outage into a workspace that looks empty."""
    adapter, _ = client(
        (
            200,
            runs_body(
                run_row(),
                studios_read=["482"],
                studios_failed=["517"],
                total_is_exact=False,
            ),
        )
    )

    reading = adapter.read_runs(make_binding(), PlatformQuery())
    page = reading.result.data

    assert reading.result.status == "partial"
    assert page is not None and [run.run_id for run in page.items] == [RUN]
    assert page.partial is True
    assert page.coverage is not None and page.coverage.failed_scope_count == 1
    assert page.coverage.total_is_exact is False
    assert reading.result.error is not None
    assert reading.result.error.retryable is True
    assert reading.studios_failed == ("517",)


def test_omitted_studios_are_partial_coverage_without_being_a_failure() -> None:
    """A ceiling on how many studios are read is not a studio going down."""
    adapter, _ = client((200, runs_body(run_row(), studios_omitted=14)))

    reading = adapter.read_runs(make_binding(), PlatformQuery())
    page = reading.result.data

    assert reading.result.status == "ok"
    assert page is not None and page.partial is True
    assert page.coverage is not None and page.coverage.omitted_scope_count == 14
    assert reading.studios_omitted == 14


def test_overlapping_pages_collapse_one_run_to_one_row() -> None:
    """``since`` filters on start time, so pages must overlap; the price of
    overlapping is that the duplicate has to collapse."""
    older = run_summary(run_row(updated_at="2026-09-10T14:00:00+00:00"))
    newer = run_summary(run_row(updated_at="2026-09-10T15:00:00+00:00", cost=2.5))

    merged = merge_summaries([older, newer, run_summary(run_row(OTHER_RUN))])

    assert [run.run_id for run in merged] == [RUN, OTHER_RUN]
    assert merged[0].platform_usd == 2.5


def test_a_merge_does_not_lose_a_studio_the_detail_route_never_returns() -> None:
    """The detail route returns no ``studio_id``. A newer row must not erase
    the one the list already established."""
    from_list = run_summary(run_row(studio="482"))
    from_detail = run_summary(run_row(studio=None, updated_at="2026-09-10T16:00:00+00:00"))

    merged = merge_summaries([from_list, from_detail])

    assert merged[0].studio_id == "482"


def run_summary(row: dict[str, Any], **kwargs: Any) -> Any:
    normalised = run_summary_of(row, make_binding(), **kwargs)
    assert normalised is not None
    return normalised[0]


# --------------------------------------------------------------------------
# Known active runs
# --------------------------------------------------------------------------


def test_a_long_running_older_run_is_refreshed_rather_than_dropped() -> None:
    """The failure this prevents: an agent that has been working for hours
    falls out of the discovery window and vanishes from the office."""
    running = run_row(OTHER_RUN, status="processing", verdict=None, cost=None, duration_ms=None)
    adapter, transport = client(
        (200, runs_body(running)),
        (200, runs_body(run_row())),
        (200, detail_body(running)),
    )
    binding = make_binding()

    adapter.read_runs(binding, PlatformQuery())
    assert adapter.active_run_ids(binding) == (OTHER_RUN,)

    reading = adapter.read_runs(binding, PlatformQuery())
    page = reading.result.data

    assert page is not None
    assert OTHER_RUN in [run.run_id for run in page.items]
    assert reading.refreshed_run_ids == (OTHER_RUN,)
    assert transport.requests[-1][1].endswith(f"/runs/{OTHER_RUN}")


def test_a_finished_run_stops_being_tracked_as_active() -> None:
    adapter, _ = client(
        (200, runs_body(run_row(status="processing", verdict=None))),
        (200, runs_body(run_row())),
    )
    binding = make_binding()

    adapter.read_runs(binding, PlatformQuery())
    adapter.read_runs(binding, PlatformQuery())

    assert adapter.active_run_ids(binding) == ()


def test_a_masked_run_is_dropped_from_the_refresh_set_rather_than_retried_forever() -> None:
    running = run_row(OTHER_RUN, status="processing", verdict=None)
    adapter, _ = client(
        (200, runs_body(running)),
        (200, runs_body(run_row())),
        (404, {"detail": f"Run {OTHER_RUN} not found"}),
    )
    binding = make_binding()

    adapter.read_runs(binding, PlatformQuery())
    adapter.read_runs(binding, PlatformQuery())

    assert adapter.active_run_ids(binding) == ()


# --------------------------------------------------------------------------
# The four meanings of a 404
# --------------------------------------------------------------------------


def test_a_masked_run_never_becomes_a_forbidden_and_never_confirms_existence() -> None:
    """A 403 here would tell the caller the run exists in someone else's
    workspace, which is the leak the upstream's identical wording prevents."""
    adapter, _ = client((404, {"detail": f"Run {RUN} not found"}))

    reading = adapter.read_run(make_binding(), RUN)

    assert reading.result.status == "unavailable"
    assert reading.result.data is None
    assert reading.result.error is not None
    assert reading.result.error.code == "upstream_error", "a masked 404 must not become forbidden"
    assert reading.result.error.detail.startswith(NOT_FOUND_OR_MASKED)
    assert reading.result.error.retryable is False
    assert reading.not_found == "masked"


def test_a_derived_document_that_is_not_built_yet_is_a_pollable_success() -> None:
    """Not a false empty success and not an outage: the read worked and the
    document is not there yet."""
    adapter, _ = client(
        (
            404,
            {
                "detail": (
                    f"Run {RUN} is in workspace {WORKSPACE} but has no policy audit yet. "
                    "The run graph may still be building, or this run was never graded. "
                    "This is not a bad run id — the run resolved."
                )
            },
        )
    )

    reading = adapter.read_policies(make_binding(), RUN, run_state="running")

    assert reading.result.status == "ok"
    assert reading.result.data is not None
    assert reading.result.data.state == "processing"
    assert reading.result.data.rows == ()
    assert reading.pollable is True
    assert reading.not_found == "not_ready"


def test_a_derived_document_that_is_gone_stops_the_poller() -> None:
    """The upstream's terminal helper deliberately never says "yet"."""
    adapter, _ = client(
        (
            404,
            {
                "detail": (
                    f"Run {RUN} is in workspace {WORKSPACE} and its RML analysis is gone. "
                    "The trace was pruned. Polling will not change this. This is not a bad "
                    "run id — the run resolved."
                )
            },
        )
    )

    reading = adapter.read_reasoning(make_binding(), RUN, "rml", run_state="running")

    assert reading.result.status == "ok"
    assert reading.result.data is not None
    assert reading.result.data.state == "not_available"
    assert reading.pollable is False
    assert reading.not_found == "gone"


def test_a_finished_run_makes_a_not_ready_document_unpollable() -> None:
    """Polling is polling on the run's own status. When that status has
    stopped moving there is nothing left to poll on."""
    detail = (
        f"Run {RUN} is in workspace {WORKSPACE} but has no policy audit yet. "
        "This is not a bad run id — the run resolved."
    )
    adapter, _ = client((404, {"detail": detail}), (404, {"detail": detail}))

    still_going = adapter.read_policies(make_binding(), RUN, run_state="running")
    finished = adapter.read_policies(make_binding(), RUN, run_state="completed")

    assert still_going.pollable is True
    assert finished.pollable is False
    assert finished.result.data is not None
    assert finished.result.data.state == "processing"


def test_an_unrouted_path_is_told_apart_from_a_masked_object() -> None:
    """A bare "Not Found" names no run; every object-scoped 404 on this API
    does. That difference is the only cheap way to spot an older gateway."""
    assert not_found_meaning("Not Found") == "route_absent"
    assert not_found_meaning("Workspace not found") == "workspace_absent"
    assert not_found_meaning(f"Run {RUN} not found") == "masked"
    assert not_found_meaning(None) == "route_absent"


# --------------------------------------------------------------------------
# Availability
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("timeout", "unavailable"),
        ("rate_limited", "unavailable"),
        ("service_unavailable", "unavailable"),
        ("unauthorized", "unauthorized"),
        ("forbidden", "forbidden"),
        ("service_unconfigured", "unconfigured"),
        ("binding_required", "unconfigured"),
        ("unsupported_capability", "unsupported"),
    ],
)
def test_a_transport_error_maps_to_one_availability(code: str, expected: str) -> None:
    assert status_for_error(code) == expected  # type: ignore[arg-type]


def test_a_timeout_is_a_bounded_unavailable_result_and_not_an_exception() -> None:
    """A remote outage must leave the local projection usable, so nothing here
    is allowed to raise."""
    adapter, _ = client(
        (0, ServiceError(code="timeout", detail="the platform did not answer", retryable=True))
    )

    result = adapter.list_runs(make_binding(), PlatformQuery())

    assert result.status == "unavailable"
    assert result.data is None
    assert result.error is not None and result.error.retryable is True


def test_a_rate_limited_read_stays_retryable_and_carries_no_data() -> None:
    adapter, _ = client((429, {"detail": "slow down"}))

    result = adapter.list_runs(make_binding(), PlatformQuery())

    assert result.status == "unavailable"
    assert result.error is not None and result.error.code == "rate_limited"
    assert result.error.retryable is True


def test_a_revoked_binding_cannot_keep_painting_previously_authorised_data() -> None:
    """The freshness record is dropped by the transport on a 403, so there is
    nothing left for a stale view to date itself against."""
    adapter, transport = client(
        (200, runs_body(run_row())),
        (403, {"detail": "API key is not scoped to this workspace"}),
    )
    binding = make_binding()

    first = adapter.list_runs(binding, PlatformQuery())
    assert first.last_success_at == NOW

    second = adapter.list_runs(binding, PlatformQuery())

    assert second.status == "forbidden"
    assert second.data is None
    assert second.stale is False
    assert second.last_success_at is None
    assert transport.last_success_at(binding) is None


def test_an_unauthorized_read_drops_data_rather_than_showing_it() -> None:
    adapter, _ = client((401, {"detail": "Missing Authorization header or access cookie"}))

    result = adapter.list_runs(make_binding(), PlatformQuery())

    assert result.status == "unauthorized"
    assert result.data is None
    assert result.stale is False


# --------------------------------------------------------------------------
# Binding scope
# --------------------------------------------------------------------------


def test_a_cursor_from_another_binding_revision_is_refused() -> None:
    """A cursor is this adapter's token, not a parameter: one minted under a
    rotated credential must not continue into the new scope."""
    old = encode_cursor(40, make_binding(revision=7))
    adapter, transport = client()

    result = adapter.list_runs(make_binding(revision=8), PlatformQuery(cursor=old))

    assert result.status == "unconfigured"
    assert result.error is not None and result.error.code == "binding_required"
    assert transport.requests == [], "a refused cursor must not reach the platform"


def test_a_cursor_round_trips_only_within_its_own_binding() -> None:
    binding = make_binding()
    cursor = encode_cursor(40, binding)

    assert decode_cursor(cursor, binding) == 40
    assert decode_cursor(cursor, make_binding(revision=9)) is None
    assert decode_cursor("not-a-cursor", binding) is None


def test_a_cursor_becomes_an_offset_and_a_caller_s_offset_is_dropped() -> None:
    adapter, transport = client((200, runs_body(run_row())), (200, runs_body(run_row())))
    binding = make_binding()

    adapter.read_runs(binding, PlatformQuery(cursor=encode_cursor(40, binding)))
    adapter.read_runs(binding, PlatformQuery(filters={"offset": "9999", "since": "2026-09-01"}))

    first = transport.requests[0][2]
    second = transport.requests[1][2]
    assert first is not None and first.filters["offset"] == "40"
    assert second is not None and "offset" not in second.filters
    assert second.filters["since"] == "2026-09-01"


def test_a_cursor_that_names_the_first_page_is_still_sent_as_an_offset() -> None:
    """Zero is a position, not an absence. A falsy-offset check would make the
    page a handed-out cursor names unreachable by that cursor."""
    adapter, transport = client((200, runs_body(run_row())))
    binding = make_binding()

    adapter.read_runs(binding, PlatformQuery(cursor=encode_cursor(0, binding)))

    query = transport.requests[0][2]
    assert query is not None
    assert query.filters["offset"] == "0"


def test_changing_the_selected_binding_discards_what_the_old_one_established() -> None:
    adapter, transport = client((200, runs_body(run_row(status="processing", verdict=None))))
    binding = make_binding()

    adapter.read_runs(binding, PlatformQuery())
    assert adapter.active_run_ids(binding) == (RUN,)

    adapter.discard(binding)

    assert adapter.active_run_ids(binding) == ()
    assert transport.invalidated == [binding.binding_id]


def test_a_run_seen_under_one_revision_is_not_active_under_another() -> None:
    adapter, _ = client((200, runs_body(run_row(status="processing", verdict=None))))

    adapter.read_runs(make_binding(revision=7), PlatformQuery())

    assert adapter.active_run_ids(make_binding(revision=7)) == (RUN,)
    assert adapter.active_run_ids(make_binding(revision=8)) == ()


# --------------------------------------------------------------------------
# Run state and cost
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"status": "processing"}, "running"),
        ({"status": "received"}, "queued"),
        ({"status": "ready", "run_verdict": "completed"}, "completed"),
        ({"status": "ready", "run_verdict": "failed"}, "failed"),
        ({"status": "failed"}, "failed"),
        ({"status": "ready", "ended_at": "2026-09-10T14:00:00+00:00"}, "completed"),
        ({"status": "ready"}, "unknown"),
        ({"status": "something-new"}, "unknown"),
    ],
)
def test_the_pipeline_state_and_the_run_verdict_are_read_as_two_facts(
    row: dict[str, Any], expected: str
) -> None:
    state, label = run_state_of(row)

    assert state == expected
    assert label == row["status"]


def test_a_null_cost_and_a_measured_zero_stay_different_values() -> None:
    """Fabricating 0 for a null is the defect the upstream's own model comment
    records as having dropped 83 runs from a production query."""
    unmeasured = run_summary(run_row(cost=None, duration_ms=None))
    measured_zero = run_summary(run_row(cost=0.0, duration_ms=0.0))

    assert unmeasured.platform_usd is None
    assert unmeasured.duration_s is None
    assert measured_zero.platform_usd == 0.0
    assert measured_zero.duration_s == 0


def test_gateway_cost_and_local_cost_are_shown_side_by_side_and_never_summed() -> None:
    summary = run_summary(run_row(cost=1.5))

    comparison = compare_costs(summary, local_usd=0.9)

    assert comparison.platform_usd == 1.5
    assert comparison.local_usd == 0.9
    assert comparison.state == "both"
    assert not hasattr(comparison, "total_usd")
    assert 2.4 not in (comparison.platform_usd, comparison.local_usd)


def test_an_unpriced_local_figure_is_a_floor_rather_than_free_usage() -> None:
    summary = run_summary(run_row(cost=None))

    comparison = compare_costs(summary, local_usd=0.0, local_price_known=False)

    assert comparison.state == "local_only"
    assert comparison.local_is_lower_bound is True
    assert comparison.platform_measured_zero is False


def test_a_nested_run_s_cost_is_shared_rather_than_attributed_to_the_child() -> None:
    normalised = run_summary_of(run_row(parent=RUN), make_binding())
    assert normalised is not None
    summary, facts = normalised

    comparison = compare_costs(summary, facts, local_usd=0.2)

    assert facts.cost_attribution == "shared"
    assert comparison.attribution == "shared"


def test_a_top_level_run_is_attributed_to_itself() -> None:
    normalised = run_summary_of(run_row(parent=None), make_binding())
    assert normalised is not None

    assert normalised[1].cost_attribution == "run"


# --------------------------------------------------------------------------
# Detail
# --------------------------------------------------------------------------


def test_a_run_detail_carries_no_prompt_or_model_text_at_all() -> None:
    """``root_input`` and ``root_output`` are verbatim human and model text.
    They are not bounded here, they are not admitted."""
    adapter, _ = client((200, detail_body(run_row())))

    reading = adapter.read_run(make_binding(), RUN)
    detail = reading.result.data

    assert detail is not None
    assert detail.summary_text is None
    assert PROMPT_TEXT not in detail.model_dump_json()


def test_a_detail_offers_only_the_tabs_the_run_actually_has() -> None:
    row = run_row()
    row["summary_counts"] = {"spans": 0, "events": 0, "artifacts": 0, "policies": 0}
    body = detail_body(row)
    assert isinstance(body["run"], dict)
    body["run"]["graph_available"] = False
    adapter, _ = client((200, body))

    detail = adapter.get_run(make_binding(), RUN).data

    assert detail is not None
    assert detail.available_details == ()


def test_a_detail_with_unmeasured_counts_offers_no_tab_rather_than_guessing() -> None:
    row = run_row()
    row["summary_counts"] = None
    body = detail_body(row)
    assert isinstance(body["run"], dict)
    body["run"]["graph_available"] = False
    adapter, _ = client((200, body))

    detail = adapter.get_run(make_binding(), RUN).data

    assert detail is not None
    assert detail.available_details == ()


def test_a_still_processing_detail_is_a_successful_read_of_an_unfinished_run() -> None:
    processing = run_row(status="processing", verdict=None, cost=None, duration_ms=None)
    adapter, _ = client((200, detail_body(processing, poll_after_ms=2000)))

    reading = adapter.read_run(make_binding(), RUN)
    detail = reading.result.data

    assert reading.result.status == "ok"
    assert detail is not None and detail.run.state == "running"
    assert detail.run.platform_usd is None
    assert reading.poll_after_ms == 2000


# --------------------------------------------------------------------------
# Policies
# --------------------------------------------------------------------------


def policies_body(*gates: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "run_id": RUN,
        "gates": list(gates),
        "deductions": [],
        "enforcements": [],
        "enforcement_summary": None,
        "is_governed": True,
        "aisquare_audit_at": "2026-09-10T14:11:02.914000+00:00",
    }
    body.update(overrides)
    return body


def test_an_unverified_gate_is_never_folded_into_a_pass() -> None:
    """``unverified`` and ``needs_review`` both carry ``passed: true`` for
    legacy readers. Reading that as green is a documented past defect."""
    adapter, _ = client(
        (
            200,
            policies_body(
                {"rule_id": "qa_cite", "passed": True, "outcome": "unverified"},
                {"rule_id": "qa_flag", "passed": True, "outcome": "needs_review"},
                {"rule_id": "sec_key", "passed": True, "outcome": "pass"},
            ),
        )
    )

    reading = adapter.read_policies(make_binding(), RUN)
    summary = reading.result.data

    assert summary is not None
    assert [row.status for row in summary.rows] == ["unknown", "unknown", "passed"]
    assert reading.unverified_count == 1
    assert reading.needs_review_count == 1
    assert [gate.outcome for gate in reading.gates] == ["unverified", "needs_review", "pass"]


def test_the_outcome_wins_over_the_boolean_when_both_are_present() -> None:
    assert policy_status_of({"passed": True, "outcome": "fail"}) == ("failed", "fail")
    assert policy_status_of({"passed": True, "outcome": "unverified"}) == (
        "unknown",
        "unverified",
    )
    assert policy_status_of({"passed": False}) == ("failed", None)
    assert policy_status_of({}) == ("unknown", None)


def test_an_ungoverned_run_is_a_positive_fact_and_not_a_clean_one() -> None:
    adapter, _ = client((200, policies_body(is_governed=False)))

    reading = adapter.read_policies(make_binding(), RUN)

    assert reading.is_governed is False
    assert reading.result.data is not None
    assert reading.result.data.state == "ready"


def test_a_never_audited_run_is_told_apart_from_a_clean_one() -> None:
    """An empty gate list alone does not carry that distinction; the governance
    flag and the audit timestamp do."""
    adapter, _ = client((200, policies_body(is_governed=None, aisquare_audit_at=None)))

    reading = adapter.read_policies(make_binding(), RUN)

    assert reading.result.data is not None
    assert reading.result.data.state == "not_available"
    assert reading.is_governed is None
    assert reading.audited_at is None


def test_runtime_enforcements_stay_separate_from_the_audit_verdicts() -> None:
    """Two different systems share one body. Merging them would report a live
    block and a post-hoc pass as one number."""
    adapter, _ = client(
        (
            200,
            policies_body(
                {"rule_id": "sec_key", "passed": True, "outcome": "pass"},
                enforcements=[
                    {
                        "span_id": "9c3d1f77a2b04e51",
                        "action": "warn",
                        "outcome": "allow",
                        "violation_count": 1,
                        "degraded": False,
                        "before": BLOCKED_OUTPUT,
                        "after": BLOCKED_OUTPUT,
                    }
                ],
            ),
        )
    )

    reading = adapter.read_policies(make_binding(), RUN)

    assert len(reading.gates) == 1
    assert len(reading.enforcements) == 1
    assert reading.enforcements[0].action == "warn"
    assert BLOCKED_OUTPUT not in repr(reading)


def test_a_gate_with_no_identity_is_dropped_rather_than_given_one() -> None:
    adapter, _ = client((200, policies_body({"passed": True, "outcome": "pass"})))

    reading = adapter.read_policies(make_binding(), RUN)

    assert reading.gates == ()
    assert reading.result.data is not None
    assert reading.result.data.rows == ()


# --------------------------------------------------------------------------
# Reasoning
# --------------------------------------------------------------------------


STORY_BODY: dict[str, Any] = {
    "studio_id": "482",
    "run_id": RUN,
    "moments": [
        {
            "id": "m1",
            "type": "intake",
            "title": "Task received",
            "summary": "The agent was given a repository task.",
            "metadata": {"user_prompt": PROMPT_TEXT, "system_prompt": PROMPT_TEXT},
            "severity": "info",
        }
    ],
    "enriched_moments": None,
    "is_enriching": False,
    "coverage": {"spans_total": 148, "spans_projected": 61, "by_kind": {"llm": 22}},
    "updated_at": "2026-09-10T14:10:44.002000+00:00",
}


def test_the_story_read_never_asks_the_platform_to_start_work() -> None:
    """``enrich`` defaults to true upstream and spawns a detached background
    LLM task. Opening a drawer must not spend money on the platform."""
    adapter, transport = client((200, STORY_BODY))

    adapter.read_reasoning(make_binding(), RUN, "story")

    query = transport.requests[0][2]
    assert query is not None
    assert query.filters["enrich"] == "false"
    assert transport.requests[0][1].endswith(f"/runs/{RUN}/story")


def test_the_story_admits_titles_and_summaries_and_not_its_metadata() -> None:
    adapter, _ = client((200, STORY_BODY))

    reading = adapter.read_reasoning(make_binding(), RUN, "story")
    summary = reading.result.data

    assert summary is not None
    assert summary.state == "ready"
    assert [section.title for section in summary.sections] == ["Task received"]
    assert PROMPT_TEXT not in summary.model_dump_json()
    assert reading.spans_total == 148
    assert reading.spans_projected == 61


def test_unmeasured_story_coverage_is_null_rather_than_complete() -> None:
    body = dict(STORY_BODY)
    body["coverage"] = None
    adapter, _ = client((200, body))

    reading = adapter.read_reasoning(make_binding(), RUN, "story")

    assert reading.spans_total is None
    assert reading.spans_projected is None


def test_the_rml_row_is_read_for_two_known_keys_and_nothing_else() -> None:
    """This route runs ``SELECT *`` and returns the database row, so its shape
    is whatever the table currently has. Only admitted keys are read."""
    adapter, _ = client(
        (
            200,
            {
                "analysis_id": "rml_7c1e4a9f03d4",
                "run_id": RUN,
                "rml_version": "3",
                "extraction_confidence": 0.82,
                "low_confidence": False,
                "claims": ["the test suite was green before the change"],
                "assumptions": ["the fixture reflects the deployed revision"],
                "a_column_added_next_week": PROMPT_TEXT,
            },
        )
    )

    reading = adapter.read_reasoning(make_binding(), RUN, "rml")
    summary = reading.result.data

    assert summary is not None
    assert [section.title for section in summary.sections] == ["Claims", "Assumptions"]
    assert reading.extraction_confidence == 0.82
    assert reading.low_confidence is False
    assert PROMPT_TEXT not in summary.model_dump_json()


def test_the_reasoning_chain_is_unsupported_without_a_studio_and_spends_no_request() -> None:
    """There is no workspace-scoped reasoning-chain route. Saying so is the
    answer; serving the story under that name would not be."""
    adapter, transport = client()

    result = adapter.get_reasoning(make_binding(studio=None), RUN, "chain")

    assert result.status == "unsupported"
    assert result.error is not None and result.error.code == "unsupported_capability"
    assert result.error.retryable is False
    assert transport.requests == []


def test_the_reasoning_chain_is_read_from_the_studio_route_when_one_is_bound() -> None:
    adapter, transport = client(
        (
            200,
            {
                "status": "ready",
                "run_id": RUN,
                "chain": [
                    {"span_id": "1a", "span_name": "AgentRun", "has_rml": False},
                    {
                        "span_id": "2b",
                        "span_name": "llm_call",
                        "has_rml": True,
                        "rml": {"claims": ["the change is scoped to one module"]},
                    },
                ],
            },
        )
    )

    reading = adapter.read_reasoning(make_binding(studio="482"), RUN, "chain")
    summary = reading.result.data

    assert transport.requests[0][1] == f"/v1/studios/482/ui/runs/{RUN}/reasoning"
    assert summary is not None
    assert [section.title for section in summary.sections] == ["llm_call"]


def test_an_unknown_reasoning_kind_is_refused_before_a_request_is_spent() -> None:
    adapter, transport = client()

    result = adapter.get_reasoning(make_binding(studio="482"), RUN, "xtrace")

    assert result.status == "unsupported"
    assert transport.requests == []
