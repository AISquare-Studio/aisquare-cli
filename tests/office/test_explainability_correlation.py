"""Joining a local session to a remote run — proven, or not claimed at all.

These tests use P02's real :class:`ObservationDatabase` under ``tmp_path``
rather than a fake store. The join rules are statements about what was
*persisted*, and a fake that returns whatever the test asked for would prove
only that the adapter can read a list.

Nothing here opens a private SQLite connection, changes the schema or writes
through anything but the store's own public helpers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from aisquare.office.adapters.explainability import HttpExplainabilityClient
from aisquare.office.config import OfficeConfig
from aisquare.office.models import (
    PlatformBinding,
    PlatformQuery,
    TransportResponse,
    TransportResult,
)
from aisquare.office.storage import CorrelationRecord, ObservationDatabase

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
WORKSPACE = "1042"
SESSION = "27b57716"
OTHER_SESSION = "56c3d516"
RUN = "b7c1e4a9f03d4e2ab8915c6d7e0f2a31"
OTHER_RUN = "3f9a2d5c81b64e77a0c3e918d24b5e60"
MARKER = "run_key_7c1e4a9f03d4"


class FrozenClock:
    def __init__(self, now: datetime = NOW) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return 0.0


class StubTransport:
    """Answers one queued body. The join path must not call it at all."""

    def __init__(self, body: object = None) -> None:
        self.body = body
        self.requests: list[str] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        query: PlatformQuery | None = None,
        body: object = None,
        binding: PlatformBinding,
        timeout_class: str = "ordinary",
    ) -> TransportResult:
        self.requests.append(path)
        return TransportResult(
            response=TransportResponse(
                status_code=200,
                allowed_headers={"content-type": "application/json"},
                json_body=self.body,
                received_at=NOW,
            )
        )

    def last_success_at(self, binding: PlatformBinding) -> datetime | None:
        return NOW

    def invalidate(self, *, binding_id: str | None = None, revision: int | None = None) -> int:
        return 0


def make_binding(*, revision: int = 7) -> PlatformBinding:
    return PlatformBinding(
        binding_id="pb_synthetic",
        revision=revision,
        project_id="prj_office",
        profile_name="stg",
        workspace_id=WORKSPACE,
    )


def store(home: Path) -> ObservationDatabase:
    return ObservationDatabase.from_config(OfficeConfig(home=home), FrozenClock())


def record(
    *,
    status: str = "verified",
    session_id: str | None = SESSION,
    run_id: str | None = RUN,
    binding: PlatformBinding | None = None,
    marker: str = MARKER,
    expires_at: datetime | None = None,
    source: str = "hook",
) -> CorrelationRecord:
    bound = binding if binding is not None else make_binding()
    return CorrelationRecord(
        project_id=bound.project_id,
        binding_id=bound.binding_id,
        binding_revision=bound.revision,
        pipeline_marker=marker,
        observed_at=NOW,
        status=status,  # type: ignore[arg-type]
        session_id=session_id,
        run_id=run_id,
        join_evidence={"operation": "ship_once"},
        last_verified_at=NOW,
        expires_at=expires_at,
        source=source,
    )


def adapter(home: Path, *, body: object = None) -> tuple[HttpExplainabilityClient, StubTransport]:
    transport = StubTransport(body)
    return (
        HttpExplainabilityClient(
            transport=transport, clock=FrozenClock(), correlations=store(home)
        ),
        transport,
    )


def runs_body(*run_ids: str) -> dict[str, object]:
    return {
        "status": "ok",
        "workspace_id": WORKSPACE,
        "total_count": len(run_ids),
        "studios_read": ["482"],
        "studios_failed": [],
        "studios_omitted": 0,
        "total_is_exact": True,
        "page_limit": 1000,
        "runs_reachable": len(run_ids),
        "next_offset": None,
        "runs": [
            {
                "studio_id": "482",
                "run_id": run_id,
                "status": "ready",
                "run_verdict": "completed",
                "agent_name": "aisquare-coder",
                "started_at": "2026-09-10T14:02:11.418000+00:00",
                "ended_at": "2026-09-10T14:09:53.771000+00:00",
                "updated_at": "2026-09-10T14:09:54.002000+00:00",
                "duration_ms": 1000.0,
                "cost_usd": 1.0,
                "summary_counts": {"spans": 1, "events": 0, "artifacts": 0, "policies": 0},
            }
            for run_id in run_ids
        ],
    }


# --------------------------------------------------------------------------
# The three persisted states
# --------------------------------------------------------------------------


def test_a_verified_correlation_joins_the_session_to_its_run(tmp_path: Path) -> None:
    database = store(tmp_path)
    database.upsert_correlation(record())
    client, transport = adapter(tmp_path)

    join = client.join(SESSION, make_binding())

    assert join.state == "joined"
    assert join.run_id == RUN
    assert join.evidence is not None and "verified correlation" in join.evidence
    assert join.scope is not None and join.scope.workspace_id == WORKSPACE
    assert join.observed_at == NOW
    assert transport.requests == [], "a join is read from the sidecar, not from the platform"


def test_an_unjoined_record_is_an_answer_and_not_an_absence(tmp_path: Path) -> None:
    """ "We looked and there is no run" is a finding. It must be distinguishable
    from "nothing is happening" and from "the platform is down"."""
    database = store(tmp_path)
    database.upsert_correlation(record(status="unjoined", run_id=None))
    client, _ = adapter(tmp_path)

    join = client.join(SESSION, make_binding())

    assert join.state == "unjoined"
    assert join.run_id is None
    assert join.evidence is not None
    assert "none is currently verified" in join.evidence
    assert "unjoined" in join.evidence


def test_a_stale_record_does_not_keep_asserting_a_join(tmp_path: Path) -> None:
    database = store(tmp_path)
    database.upsert_correlation(record(status="stale", run_id=RUN))
    client, _ = adapter(tmp_path)

    join = client.join(SESSION, make_binding())

    assert join.state == "unjoined"
    assert join.evidence is not None and "stale" in join.evidence


def test_a_session_with_no_record_at_all_is_unjoined_with_its_own_reason(
    tmp_path: Path,
) -> None:
    client, _ = adapter(tmp_path)

    join = client.join(SESSION, make_binding())

    assert join.state == "unjoined"
    assert join.evidence is not None
    assert "no correlation was recorded" in join.evidence


def test_a_join_survives_a_restart_of_the_process(tmp_path: Path) -> None:
    """The evidence is persisted, so a restarted Office does not lose the
    joins it had proven — and does not have to re-prove them by guessing."""
    store(tmp_path).upsert_correlation(record())

    client, _ = adapter(tmp_path)
    join = client.join(SESSION, make_binding())

    assert join.state == "joined"
    assert join.run_id == RUN


# --------------------------------------------------------------------------
# What is not a join
# --------------------------------------------------------------------------


def test_two_verified_records_naming_different_runs_are_ambiguous(tmp_path: Path) -> None:
    """Ambiguous reports as unjoined on the wire. Picking one would be a guess
    wearing a verified badge."""
    database = store(tmp_path)
    database.upsert_correlation(record(run_id=RUN))
    database.upsert_correlation(record(run_id=OTHER_RUN))
    client, _ = adapter(tmp_path)

    join = client.join(SESSION, make_binding())

    assert join.state == "ambiguous"
    assert join.run_id is None
    assert join.wire_join == "unjoined"


def test_a_join_recorded_under_another_binding_revision_is_not_this_binding_s(
    tmp_path: Path,
) -> None:
    """The revision moves when the profile, endpoint, workspace, studio, agent
    or credential moves. A join proven under the old one was never proven
    here."""
    database = store(tmp_path)
    database.upsert_correlation(record(binding=make_binding(revision=7)))
    client, _ = adapter(tmp_path)

    same = client.join(SESSION, make_binding(revision=7))
    rotated = client.join(SESSION, make_binding(revision=8))

    assert same.state == "joined"
    assert rotated.state == "unjoined"
    assert rotated.evidence is not None and "no correlation was recorded" in rotated.evidence


def test_an_expired_verification_is_no_longer_a_verification(tmp_path: Path) -> None:
    database = store(tmp_path)
    database.upsert_correlation(record(expires_at=NOW - timedelta(minutes=1)))
    client, _ = adapter(tmp_path)

    join = client.join(SESSION, make_binding())

    assert join.state == "unjoined"


def test_a_verified_record_with_no_pipeline_marker_is_not_a_proof(tmp_path: Path) -> None:
    """The marker is what ties the local launch to the run the producer opened.
    Without it there is a run id and no chain of custody for it."""
    database = store(tmp_path)
    database.upsert_correlation(record(marker=""))
    client, _ = adapter(tmp_path)

    join = client.join(SESSION, make_binding())

    assert join.state == "unjoined"


def test_another_session_s_join_is_not_this_session_s(tmp_path: Path) -> None:
    database = store(tmp_path)
    database.upsert_correlation(record(session_id=OTHER_SESSION))
    client, _ = adapter(tmp_path)

    join = client.join(SESSION, make_binding())

    assert join.state == "unjoined"


def test_without_a_correlation_store_nothing_is_ever_joined(tmp_path: Path) -> None:
    client = HttpExplainabilityClient(transport=StubTransport(), clock=FrozenClock())

    join = client.join(SESSION, make_binding())

    assert join.state == "unjoined"
    assert join.evidence is not None and "no correlation store" in join.evidence


# --------------------------------------------------------------------------
# What the page does with a join
# --------------------------------------------------------------------------


def test_a_listed_run_is_attached_to_a_local_agent_only_through_a_verified_join(
    tmp_path: Path,
) -> None:
    database = store(tmp_path)
    database.upsert_correlation(record(run_id=RUN))
    client, _ = adapter(tmp_path, body=runs_body(RUN, OTHER_RUN))

    page = client.list_runs(make_binding(), PlatformQuery()).data

    assert page is not None
    joined = {run.run_id: run for run in page.items}
    assert joined[RUN].join == "verified"
    assert joined[RUN].local_agent_id == SESSION
    assert joined[OTHER_RUN].join == "unjoined"
    assert joined[OTHER_RUN].local_agent_id is None


def test_a_run_two_sessions_both_claim_is_attached_to_neither(tmp_path: Path) -> None:
    database = store(tmp_path)
    database.upsert_correlation(record(session_id=SESSION, run_id=RUN))
    database.upsert_correlation(record(session_id=OTHER_SESSION, run_id=RUN))
    client, _ = adapter(tmp_path, body=runs_body(RUN))

    page = client.list_runs(make_binding(), PlatformQuery()).data

    assert page is not None
    assert page.items[0].join == "unjoined"
    assert page.items[0].local_agent_id is None


def test_a_verified_record_that_never_recorded_a_session_attaches_to_nothing(
    tmp_path: Path,
) -> None:
    database = store(tmp_path)
    database.upsert_correlation(record(session_id=None, run_id=RUN))
    client, _ = adapter(tmp_path, body=runs_body(RUN))

    page = client.list_runs(make_binding(), PlatformQuery()).data

    assert page is not None
    assert page.items[0].local_agent_id is None


def test_a_matching_agent_name_is_never_a_join(tmp_path: Path) -> None:
    """The listed row's ``agent_name`` is ``aisquare-coder`` and the local
    session is a coder. Resemblance is not identity and must buy nothing."""
    client, _ = adapter(tmp_path, body=runs_body(RUN))

    page = client.list_runs(make_binding(), PlatformQuery()).data

    assert page is not None
    assert page.items[0].stable_agent_id == "aisquare-coder"
    assert page.items[0].join == "unjoined"
    assert page.items[0].local_agent_id is None


def test_a_join_from_the_previous_binding_does_not_survive_a_rotation(tmp_path: Path) -> None:
    database = store(tmp_path)
    database.upsert_correlation(record(binding=make_binding(revision=7), run_id=RUN))
    client, _ = adapter(tmp_path, body=runs_body(RUN))

    page = client.list_runs(make_binding(revision=8), PlatformQuery()).data

    assert page is not None
    assert page.items[0].join == "unjoined"
